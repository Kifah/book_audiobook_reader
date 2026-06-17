#!/usr/bin/env python3
"""
convert_books.py — Convert EPUB files in ./epubs/ into MP3 audiobooks
in ./audiobooks/ via an OpenAI-compatible TTS endpoint (default: OpenRouter).

Usage:
    1. Drop one or more .epub files in ./epubs/
    2. Copy .env.example to .env and fill in your TTS_API_KEY
    3. python3 convert_books.py

Output:
    ./audiobooks/<book-slug>/  - per-chapter MP3s + final concatenated MP3
"""

import os
import sys
import re
import io
import time
import shutil
import logging
import unicodedata
import subprocess
from pathlib import Path
from urllib import request, error
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from ebooklib import epub, ITEM_DOCUMENT
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "Missing dependencies. Install with:\n"
        "  pip install -r requirements.txt"
    )

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit(
        "Missing python-dotenv. Install with:\n"
        "  pip install python-dotenv"
    )

# ---------------------------------------------------------------------------
# Config (loaded from .env)
# ---------------------------------------------------------------------------

load_dotenv()

# Dry-run flag (set by --dry-run CLI flag in main(); read by convert_book/render_chapter)
DRY_RUN = False
DRY_RUN_SECONDS = 30.0  # approximate; we budget by chars not actual time
# Continue flag (set by --continue CLI flag; when True, skip chapters that already
# have an MP3 in audiobooks/<book>/chapters/, so an aborted run can be resumed)
CONTINUE_RUN = False
# Skip-front-matter flag (set by --skip-front-matter CLI flag; default True)
SKIP_FRONT_MATTER = True
# Clean-for-tts flag (set by --clean / --no-clean CLI flag; default True)
CLEAN_FOR_TTS = True

# Chapter title patterns to skip when SKIP_FRONT_MATTER is on. Case-insensitive
# substring match. The defaults cover the structural front-matter and back-matter
# that most academic/non-fiction books have:
#   front: cover, halftitle, title, copyright, contents (TOC)
#   back:  bibliography, index, nav (EPUB navigation doc)
# Keep: acknowledgements (often personal), introduction (author's framing),
#       notes (author's commentary), all real chapter files.
# Override with SKIP_FRONT_MATTER_PATTERNS env var (comma-separated, e.g.
# "cover,copyright,bibliography" to skip only those).
DEFAULT_SKIP_PATTERNS = [
    "cover", "halftitle", "title page", "title", "copyright",
    "contents", "table of contents", "bibliography", "index", "nav",
]

TTS_PROVIDER = os.getenv("TTS_PROVIDER", "openai_compatible").strip().lower()
TTS_API_URL = os.getenv("TTS_API_URL", "https://openrouter.ai/api/v1/audio/speech")
TTS_API_KEY = os.getenv("TTS_API_KEY", "")
TTS_MODEL = os.getenv("TTS_MODEL", "openai/gpt-4o-mini-tts-2025-12-15")
TTS_VOICE = os.getenv("TTS_VOICE", "shimmer")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.2"))
TTS_FORMAT = os.getenv("TTS_FORMAT", "mp3")
# ElevenLabs-specific (only used when TTS_PROVIDER=elevenlabs)
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
ELEVENLABS_STABILITY = float(os.getenv("ELEVENLABS_STABILITY", "0.5"))
ELEVENLABS_SIMILARITY = float(os.getenv("ELEVENLABS_SIMILARITY", "0.75"))
ELEVENLABS_STYLE = float(os.getenv("ELEVENLABS_STYLE", "0.0"))
TTS_INSTRUCTIONS = os.getenv(
    "TTS_INSTRUCTIONS",
    "Speak in a calm, warm British female voice. Pace the narration naturally, "
    "with gentle pauses at paragraph breaks. Suitable for an audiobook recording."
)
# Max characters per TTS request. History: 4000 caused Gemini to silently
# truncate audio at the chunk tail; 2500 fixed that but left INTERNAL skips
# (the model dropping a span mid-request in long dense prose). ~600 chars keeps
# each request short enough that the model can't lose its place. Smaller = more
# API calls, mitigated by TTS_PARALLEL.
TTS_CHUNK_CHARS = int(os.getenv("TTS_CHUNK_CHARS", "600"))
TTS_MAX_RETRIES = int(os.getenv("TTS_MAX_RETRIES", "4"))
TTS_RETRY_BACKOFF = float(os.getenv("TTS_RETRY_BACKOFF", "2.0"))
TTS_REQUEST_TIMEOUT = int(os.getenv("TTS_REQUEST_TIMEOUT", "180"))

# Silent-truncation guard. Some providers (notably Google Gemini on large
# chunks) return HTTP 200 with audio that stops before the input text is
# finished, producing abrupt mid-sentence cuts at chunk boundaries. The API
# call "succeeds", so the HTTP-error retry path never fires. To catch this we
# measure the returned audio duration and compare it to the duration we'd
# expect from the input character count. If the audio is shorter than
# (expected * TTS_MIN_AUDIO_RATIO), we treat it as a truncated render and retry.
#
# Empirically the engine consumes ~2400 chars per minute of *output* audio
# regardless of TTS_SPEED (see chars_for_audio_seconds), i.e. ~40 chars/sec.
# A short chunk legitimately has fewer "speakable" chars (whitespace, stripped
# markup), so the ratio is intentionally loose (0.5 = audio must be at least
# half the expected length) to avoid false positives on punctuation-heavy text.
# Set TTS_VERIFY_AUDIO_LEN=0 to disable the check entirely.
TTS_VERIFY_AUDIO_LEN = os.getenv("TTS_VERIFY_AUDIO_LEN", "1").strip().lower() not in ("0", "false", "no", "")
TTS_MIN_AUDIO_RATIO = float(os.getenv("TTS_MIN_AUDIO_RATIO", "0.5"))
# Chunks shorter than this many chars are exempt from the length check — very
# short inputs (a heading, a one-line paragraph) have unreliable char→seconds
# ratios and would trip false positives.
TTS_VERIFY_MIN_CHARS = int(os.getenv("TTS_VERIFY_MIN_CHARS", "200"))

# Parallel TTS requests per chapter. Cloud TTS is I/O-bound (waiting on
# the network), so threads work well — they don't need to release the GIL
# because most of the time is spent in urlopen(), not Python bytecode.
# At the 600-char default chunk size, a typical chapter has 50-500+ chunks,
# so 8-12 workers is the sweet spot (cloud providers handle 8-16 concurrent
# requests fine; above that you may hit rate limits).
# Set to 1 to disable parallelism (e.g. for rate-limited OpenRouter tiers).
TTS_PARALLEL = max(1, int(os.getenv("TTS_PARALLEL", "8")))

# Format of intermediate TTS part files saved to disk before chapter concat.
# For openai_compatible providers (OpenAI, OpenRouter/Kokoro/Orpheus, Gemini),
# we save raw PCM (24kHz 16-bit mono) — no ffmpeg overhead per chunk.
# The chapter-level concat encodes all parts to MP3 in a single pass.
# For ElevenLabs, we save MP3 (their API returns MP3 directly).
TTS_PART_FORMAT = "pcm" if TTS_PROVIDER == "openai_compatible" else TTS_FORMAT

# Concat strategy for joining TTS-rendered MP3 chunks:
#   "auto"     - try -c copy (fast, ~1s/chapter) first; on DTS error, fall back
#                to PCM re-encode (~25s/chapter). Best for most books.
#   "copy"     - always use -c copy. Fastest, but fails on malformed MP3s
#                from some TTS providers (Kokoro, some ElevenLabs configs).
#   "reencode" - always go through PCM intermediate. Slowest but bulletproof.
#   Default "auto" gives 6x speedup on well-formed inputs with safe fallback.
CHAPTER_CONCAT_MODE = os.getenv("CHAPTER_CONCAT_MODE", "auto").strip().lower()

# MP3 output bitrate. Spoken word (audiobooks, podcasts) is intelligible
# down to 48k mono; 64k is the industry standard (Audible uses 64k).
# Default "64k" cuts file size ~60% vs 128k with no audible quality loss
# for narration. Set to "96k" or "128k" for music/sound-effect content.
TTS_BITRATE = os.getenv("TTS_BITRATE", "64k").strip()

# Number of audio channels for output MP3. Spoken word is mono (1).
# Set to 2 only if you have genuine stereo content (rare for TTS).
TTS_CHANNELS = int(os.getenv("TTS_CHANNELS", "1"))

INPUT_DIR = Path(os.getenv("INPUT_DIR", "epubs"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "audiobooks"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Comma-separated list of chapter-title patterns to skip. If the env var is
# set, it OVERRIDES the DEFAULT_SKIP_PATTERNS entirely. If empty/unset, the
# defaults are used (when SKIP_FRONT_MATTER is on).
SKIP_FRONT_MATTER_PATTERNS = [
    p.strip().lower() for p in os.getenv("SKIP_FRONT_MATTER_PATTERNS", "").split(",") if p.strip()
] or list(DEFAULT_SKIP_PATTERNS)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("convert_books")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Filesystem-safe ASCII slug from arbitrary text."""
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "book"


def is_front_matter(title: str, patterns: list[str]) -> bool:
    """Return True if a chapter title matches any skip pattern (case-insensitive
    substring match). Used by --skip-front-matter to drop structural pages like
    cover/copyright/TOC/bibliography from the rendered audiobook."""
    t = title.lower().strip()
    return any(p in t for p in patterns)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def slug_to_accent_label(slug: str) -> str:
    return slug.replace("-", " ").title()


def chars_for_audio_seconds(seconds: float) -> int:
    """
    Budget how many characters of input text are needed to produce ~`seconds`
    of output audio.

    Empirically measured: Gemini/OpenAI TTS consumes ~2400 chars/min of output
    audio regardless of TTS_SPEED (speed affects prosody/tempo but the chars-to-
    seconds ratio is fixed by the engine, not the speed parameter).
        chars = 2400 * (seconds / 60)
    A 10% safety margin keeps the sample slightly under the target duration.
    """
    base_chars_per_min = 2400.0
    safe = max(0.1, seconds) * 0.9  # 10% margin
    budget = base_chars_per_min * (safe / 60.0)
    return max(200, int(budget))


def slice_for_dry_run(text: str, seconds: float) -> str:
    """Return a prefix of `text` that should yield ~`seconds` of audio."""
    n = chars_for_audio_seconds(seconds)
    if len(text) <= n:
        return text
    # Cut at nearest sentence boundary to avoid mid-sentence truncation
    cut = text[:n]
    for sep in (". ", "! ", "? ", "\n\n"):
        idx = cut.rfind(sep)
        if idx > n * 0.6:  # don't cut too early
            return cut[: idx + len(sep)].rstrip() + ("…" if idx + len(sep) < len(text) else "")
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# EPUB parsing
# ---------------------------------------------------------------------------

def extract_chapters(epub_path: Path) -> dict:
    """
    Returns {"title": str, "chapters": [{"title": str, "text": str}, ...]} in reading order.
    Uses the EPUB's spine; falls back to walking the manifest in order.
    """
    log.info("Parsing EPUB: %s", epub_path.name)
    book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})

    # Title fallback
    book_title = "Untitled"
    try:
        md = book.get_metadata("DC", "title")
        if md and md[0] and md[0][0]:
            book_title = str(md[0][0])
    except Exception:
        pass

    # Get spine items in reading order
    spine_ids = [s[0] for s in book.spine] if book.spine else []
    items = []
    if spine_ids:
        for sid in spine_ids:
            item = book.get_item_with_id(sid)
            if item is not None and item.get_type() == ITEM_DOCUMENT:
                items.append(item)
    if not items:
        # Fallback: walk manifest
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            items.append(item)

    chapters: list[dict] = []
    for idx, item in enumerate(items, start=1):
        raw = item.get_content().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(raw, "html.parser")

        # Best-effort chapter title: first h1/h2/h3, or filename
        title_tag = soup.find(["h1", "h2", "h3"])
        if title_tag:
            title = title_tag.get_text(strip=True)
            title_tag.decompose()
        else:
            title = Path(item.get_name()).stem

        # Convert in-body subheadings to spoken sentences so they don't end
        # up as dangling punctuation-free fragments at chunk boundaries.
        # e.g. <h3>Ancient Gaza as a trading hub</h3>  ->  "Ancient Gaza as a trading hub."
        for htag in soup.find_all(["h2", "h3", "h4"]):
            heading_text = htag.get_text(" ", strip=True).strip()
            if heading_text and not heading_text.endswith((".", "!", "?")):
                heading_text += "."
            htag.replace_with(f"\n\n{heading_text}\n\n")

        # Remove page-break spans (e.g. <span epub:type="pagebreak"/>) — they
        # contain no text but leave stray whitespace after get_text().
        for span in soup.find_all("span", attrs={"epub:type": "pagebreak"}):
            span.decompose()

        # Merge blockquotes into the preceding paragraph so the lead-in sentence
        # ("Author writes:") and the quoted text stay in the same chunk and the
        # TTS reads them together without an abrupt break between them.
        for bq in soup.find_all("blockquote"):
            bq.unwrap()

        # Insert explicit paragraph breaks before extracting text.
        # get_text("\n") collapses <p>...</p><p>...</p> into a single newline,
        # making entire chapters one giant paragraph and breaking chunk_text.
        for tag in soup.find_all(["p", "h1", "li"]):
            tag.insert_before("\n\n")
            tag.insert_after("\n\n")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        text = soup.get_text(" ", strip=False)
        # Collapse runs of blank lines and stray whitespace-only lines
        text = re.sub(r"\n[ \t]+\n", "\n\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            continue

        chapters.append({"title": title or f"Chapter {idx}", "text": text})

    log.info("Extracted %d chapter(s) from '%s'", len(chapters), book_title)
    return {"title": book_title, "chapters": chapters}


# ---------------------------------------------------------------------------
# Text cleanup for TTS
# ---------------------------------------------------------------------------

# These characters cause audible pauses/breaths in TTS engines (especially
# Kokoro, but also OpenAI and ElevenLabs). The clean_for_tts() function
# normalizes the text so short common words like "of" and "the" don't get
# stuck next to awkward punctuation, and runs of whitespace don't get
# interpreted as sentence breaks.
_MULTI_SPACE = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]{2,}")
# TOC bleed: a page number wedged between two title words via non-breaking
# spaces, e.g. "Palestine\u00a018\u00a0\u00a0City of Oranges". The lone number is a
# page reference, not prose — drop it so TTS doesn't read "...Palestine 18 City".
_TOC_PAGENUM = re.compile(r"(?<=[A-Za-z\u00bf-\u024f])[\u00a0\s]+\d{1,4}[\u00a0\s]+(?=[A-Z])")
_SMALL_WORDS = (
    r"(?:a|an|the|of|in|on|at|to|for|by|with|and|or|but|is|was|are|were|"
    r"be|been|has|have|had|it|its|he|she|his|her|they|them|their|"
    r"this|that|these|those|as|if|so|not|no|do|does|did|"
    r"I|you|we|us|our|my|me)"
)
_SMALL_WORD_PUNCT = re.compile(
    rf"(\b{_SMALL_WORDS})\s+([.,;:!?])",
    re.IGNORECASE,
)
_ELLIPSIS = re.compile(r"\u2026|\.{2,}")
# Numeric ranges like "1750–1550" or "61–73" — keep semantic meaning, but
# Kokoro/most TTS engines pause audibly on en-dashes, so say "to" instead.
_EN_DASH_NUMERIC = re.compile(r"(\d[\d,]*)\s*[\u2013\u2014]\s*(\d[\d,]*)")
# Word en-dashes — clause-level breaks. Replace with ", " (a mild pause).
# Catches patterns like "Palestine–Egypt" or "ancient–modern" but not
# numeric ranges (handled above) or hyphenated words (which use a regular
# hyphen-minus, not en/em-dash).
# Parentheses with content: "(text)" -> ", text, "
# Engine adds ~200ms breath on each paren. Wrap in commas for smooth clause.
_PARENS_WITH_CONTENT = re.compile(r"\s*\(([^()]+?)\)\s*")
# Smart double quotes around phrases: "word" / "phrase" -> word / phrase
# (curly quotes: \u201c \u201d)
_QUOTED_DQUOTE_SMART = re.compile(r"\u201c([^\u201d\n]{1,200}?)\u201d")
# Smart single quotes around words: 'word' -> word
# (curly quotes: \u2018 \u2019). Negative lookahead avoids contractions.
_QUOTED_SQUOTE_SMART = re.compile(
    r"\u2018(\w[\w\s]{0,50}?\w)\u2019"
)
# Straight double quotes: "word" -> word
# Be conservative — only strip if content is short (<200 chars) to avoid
# eating dialogue across multiple paragraphs.
_QUOTED_DQUOTE_STRAIGHT = re.compile(r'"([^"\n]{1,200}?)"')
# Straight single quotes around a single word: 'word' -> word
# (must be a real word inside, not a contraction like it's or don't)
_QUOTED_SQUOTE_STRAIGHT = re.compile(r"'(\w[\w\s]{0,50}?\w)'")
# Spaced contractions: "it 's" / "we 're" / "don ' t" -> "it's" / "we're" / "don't"
# The single-quote and trailing letter have at most one space between them.
_SPACED_CONTRACTION = re.compile(r"\b(\w+)\s+'\s*(\w+)\b")
# Decade abbreviation: '80s / '90s -> 80s / 90s (don't read the quote)
_DECADE_ABBR = re.compile(r"'(\d{2}s)\b")
# Spaced hyphen: "word - word" -> "word, word" (clause break)
_SPACED_HYPHEN = re.compile(r"(\w)\s+-\s+(\w)")
# Adverb-adverb compound: "socially-culturally" -> "socially and culturally"
# Both halves ending in -ly is a strong signal that the hyphen separates
# two adverbs (not a compound word like "well-known" or "ice-cream").
# Catches: socially-culturally, politically-economically, physically-mentally
# Doesn't touch: well-known, long-term, ice-cream, mother-in-law
_LY_HYPHEN = re.compile(r"(\w+ly)-(\w+ly)\b")


def clean_for_tts(text: str) -> str:
    """Normalize text for TTS to avoid audible pauses on common short words.

    Kokoro (and most TTS engines) add ~100-300ms of silence when they see:
      - Em-dashes (\u2014) and en-dashes (\u2013)
      - Ellipses (\u2026 or "...")
      - Double spaces (engine reads as sentence break)
      - Common short words like "of", "the", "and" followed by punctuation
        (engine over-emphasizes the breath, creating a noticeable gap)
      - Parentheses (engine adds breath on both sides)
      - Quote marks around phrases (engine reads them as "open-quote ... close-quote")
      - Spaced contractions like "it 's" (engine stumbles on the spaces)

    This function:
      1. Replaces ellipses with a period (real sentence break)
      2. Replaces em/en-dashes with comma-space
      3. Collapses multiple spaces
      4. Removes the space between a short word and the following punctuation
      5. Wraps parentheses content in ", " (engine reads smoothly as a clause)
      6. Strips quote marks around words and short phrases
      7. Fixes spaced contractions ("it 's" -> "it's")
      8. Strips decade abbreviations ('80s -> 80s)
      9. Replaces spaced hyphens ("word - word") with comma-space
    """
    # 1. Ellipses -> period (real sentence break, not the dramatic "..." pause).
    #    Run the substitution on the literal ellipsis chars first, then collapse
    #    any run of 2+ remaining dots. Also collapse "spaced ellipses" like
    #    ". . ." which many typists use.
    text = text.replace("\u2026", ".")
    text = re.sub(r"(\.)(\s\.)+", ".", text)  # ". . ." -> "."
    text = _ELLIPSIS.sub(".", text)
    # 2. Numeric ranges: "1750\u20131550" -> "1750 to 1550". Kokoro pauses on
    #    the en-dash, which makes page numbers and year ranges sound stilted.
    #    Must run BEFORE the generic dash replacement below.
    text = _EN_DASH_NUMERIC.sub(r"\1 to \2", text)
    # 3. Em-dash / en-dash -> ", " (mild pause, no breath). This catches the
    #    remaining word-level dashes like "Palestine\u2013Egypt".
    text = text.replace("\u2014", ", ").replace("\u2013", ", ")
    # 4. Strip TOC page-number bleed ("Title 18  Next Title" -> "Title Next
    #    Title"). Runs BEFORE whitespace collapse so the NBSP run still matches.
    text = _TOC_PAGENUM.sub(" ", text)
    # 5. Collapse runs of whitespace (incl. tabs and non-breaking spaces)
    text = _MULTI_SPACE.sub(" ", text)
    text = text.replace("\u00a0", " ")  # any lone non-breaking space -> space
    # 5. Remove the space between a short word and the punctuation that follows.
    #    The regex matches `<word> <punct>` and we just drop the space.
    text = _SMALL_WORD_PUNCT.sub(r"\1\2", text)
    # 6. Parentheses: wrap content in ", " so TTS reads as a parenthetical
    #    clause with a brief pause on each side, instead of a full breath.
    #    "(see chapter 3)" -> ", see chapter 3, "
    text = _PARENS_WITH_CONTENT.sub(r", \1, ", text)
    # 7. Smart + straight double quotes around phrases: drop them entirely.
    #    TTS engines read "quoted" as "open-quote quoted close-quote" with
    #    audible breaths at the quote marks.
    text = _QUOTED_DQUOTE_SMART.sub(r"\1", text)
    text = _QUOTED_SQUOTE_SMART.sub(r"\1", text)
    text = _QUOTED_DQUOTE_STRAIGHT.sub(r"\1", text)
    # 8. Smart + straight single quotes around single words: drop them.
    #    Catches 'word' but not apostrophes in contractions (it's, don't).
    text = _QUOTED_SQUOTE_STRAIGHT.sub(r"\1", text)
    # 9. Spaced contractions: "it 's" -> "it's", "we 're" -> "we 're" already
    #    handled by the word-group above. This catches the rarer "don ' t" form.
    text = _SPACED_CONTRACTION.sub(r"\1'\2", text)
    # 10. Decade abbreviations: "'80s" -> "80s" (no quote read).
    text = _DECADE_ABBR.sub(r"\1", text)
    # 11. Spaced hyphens: "word - word" -> "word, word" (clause break).
    text = _SPACED_HYPHEN.sub(r"\1, \2", text)
    # 11b. Adverb-adverb compounds: "socially-culturally" -> "socially and culturally"
    #     Run before _SPACED_HYPHEN to avoid double-processing. The -ly
    #     heuristic is safe: compounds like "well-known" don't match because
    #     "well" doesn't end in -ly.
    text = _LY_HYPHEN.sub(r"\1 and \2", text)
    # 12. Strip non-Latin/non-ASCII characters that TTS engines cannot pronounce
    #     (Greek, Arabic, Hebrew, CJK, etc. from inline <em> scholarly terms).
    #     Keep: basic Latin, extended Latin (accented chars like é, ü, ñ),
    #     curly quotes/apostrophes, common punctuation, digits, and whitespace.
    text = re.sub(r"[^\x00-\x7F\u00C0-\u024F\u2018-\u201F\u2013\u2014\u2026\s]", "", text)
    # 13. Clean up artefacts left by earlier substitutions:
    #     ", ."  -> "."   (comma-space before period from paren/dash cleanup)
    #     ", ,"  -> ","   (double comma from nested paren cleanup)
    #     " ."   -> "."   (stray space before period)
    text = re.sub(r",\s+\.", ".", text)
    text = re.sub(r",\s+,", ",", text)
    text = re.sub(r"\s+\.", ".", text)
    # 14. Merge paragraph breaks where the preceding paragraph ends without
    #     sentence-ending punctuation (lead-in sentences that introduce a
    #     blockquote or other continuation). Replace \n\n with a single space
    #     so the TTS reads through without a gap.
    #     e.g. "...Palestinian scholar Shukri 'Arraf\n\ntakes us on a rich..." ->
    #          "...Palestinian scholar Shukri 'Arraf takes us on a rich..."
    text = re.sub(r"([^.!?])\n\n+", r"\1 ", text)
    # 15. Final whitespace tidy (collapse newlines we may have created)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Text chunking (sentence-aware, character-bounded)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[)])")


def chunk_text(text: str, max_chars: int) -> list[str]:
    """
    Split `text` into chunks of at most ~max_chars characters, preferring
    paragraph > sentence > word boundaries. Always returns non-empty strings.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    buf = ""
    for para in paragraphs:
        # If a single paragraph is huge, sentence-split it
        if len(para) > max_chars:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            sentences = _SENTENCE_SPLIT.split(para)
            for sent in sentences:
                if len(buf) + len(sent) + 1 > max_chars:
                    if buf:
                        chunks.append(buf.strip())
                        buf = ""
                # Last-resort: split a huge sentence at sentence > word > char boundary
                while len(sent) > max_chars:
                    window = sent[:max_chars]
                    cut = -1
                    for sep in (". ", "! ", "? "):
                        idx = window.rfind(sep)
                        if idx > cut:
                            cut = idx + len(sep) - 1  # keep the punctuation, drop the space
                    if cut > max_chars * 0.3:
                        chunks.append(sent[: cut + 1].strip())
                        sent = sent[cut + 1 :].strip()
                    else:
                        # No sentence boundary — try word boundary
                        idx = window.rfind(" ")
                        if idx > max_chars * 0.3:
                            chunks.append(sent[:idx].strip())
                            sent = sent[idx:].strip()
                        else:
                            # True last resort: hard character split
                            chunks.append(sent[:max_chars])
                            sent = sent[max_chars:]
                buf = (buf + " " + sent).strip() if buf else sent
            continue

        if len(buf) + len(para) + 2 > max_chars:
            chunks.append(buf.strip())
            buf = para
        else:
            buf = (buf + "\n\n" + para).strip() if buf else para

    if buf:
        chunks.append(buf.strip())
    return chunks


# ---------------------------------------------------------------------------
# TTS providers
# ---------------------------------------------------------------------------

def _tts_openai_compatible(text: str) -> bytes:
    """
    POST a single text chunk to an OpenAI-compatible /audio/speech endpoint.
    Returns raw PCM bytes (24kHz, 16-bit signed little-endian, mono).

    All major OpenAI-compatible TTS providers (OpenAI, OpenRouter/Kokoro/
    Orpheus, Google Gemini) support `response_format="pcm"`. We always
    request PCM and let the chapter-level concat do the format conversion
    to MP3 in a single pass, rather than paying ffmpeg overhead on every
    chunk (144ms × 12 chunks = 1.7s wasted per chapter).
    """
    if not TTS_API_KEY:
        raise RuntimeError("TTS_API_KEY is not set. Copy .env.example to .env and add your key.")

    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
        "response_format": "pcm",
        "speed": TTS_SPEED,
    }
    # Some providers accept 'instructions' for steerable prosody (gpt-4o-mini-tts)
    if TTS_INSTRUCTIONS and "gpt-4o-mini-tts" in TTS_MODEL:
        payload["instructions"] = TTS_INSTRUCTIONS

    body = json_dumps(payload).encode("utf-8")
    req = request.Request(
        TTS_API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {TTS_API_KEY}",
            "Content-Type": "application/json",
            # Some providers (Gemini) are strict about the Accept header for PCM
            "Accept": "audio/L16;codec=pcm;rate=24000, audio/pcm, audio/*",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=TTS_REQUEST_TIMEOUT) as resp:
        return resp.read()


def _pcm_to_format(pcm_bytes: bytes, out_format: str) -> bytes:
    """Convert raw PCM bytes (24kHz, 16-bit, mono) to the given audio format.

    Kept for compatibility with the ElevenLabs path which may still
    receive MP3/etc. directly. The openai_compatible path now saves
    raw PCM and converts at chapter-concat time, which is much faster.
    """
    if not ffmpeg_available():
        raise RuntimeError(
            f"ffmpeg is required to convert PCM to {out_format} but was not found in PATH"
        )
    codec_map = {
        "mp3":  ["-c:a", "libmp3lame", "-b:a", TTS_BITRATE],
        "opus": ["-c:a", "libopus", "-b:a", "64k"],
        "aac":  ["-c:a", "aac", "-b:a", "64k"],
        "flac": ["-c:a", "flac"],
        "wav":  ["-c:a", "pcm_s16le"],
    }
    codec_args = codec_map.get(out_format)
    if codec_args is None:
        raise ValueError(
            f"Unsupported TTS_FORMAT={out_format!r}. Supported: {sorted(codec_map)}"
        )

    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "s16le", "-ar", "24000", "-ac", "1",  # input: PCM 24kHz mono 16-bit
            "-i", "pipe:0",
            *codec_args,
            "-f", out_format, "pipe:1",
        ],
        input=pcm_bytes,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg PCM->{out_format} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='ignore')[:300]}"
        )
    if not proc.stdout:
        raise RuntimeError(f"ffmpeg PCM->{out_format} returned no audio bytes")
    return proc.stdout


def _pcm_chunks_to_mp3(pcm_files: list[Path], out_path: Path) -> None:
    """Concatenate multiple raw PCM files into a single MP3.

    Fast path for chapter-level concat: open all PCM files, read them in
    sequence, pipe the combined stream to ffmpeg, encode to MP3 in one
    pass. This is significantly faster than:
      (a) per-chunk ffmpeg encode to MP3 + concat demuxer (old PCM code)
      (b) concat demuxer with .pcm files (doesn't work — demuxer can't
          infer format)
      (c) PCM-to-WAV intermediate + WAV-to-MP3 (current concat_mp3_parts
          slow path)

    Assumes all PCM files share the standard TTS-PCM signature:
    24kHz, 16-bit signed little-endian, mono. Same as _pcm_to_format.
    
    Inserts 200ms of silence between chunks to create smooth transitions
    and prevent abrupt sentence breaks.
    """
    if not pcm_files:
        raise ValueError("_pcm_chunks_to_mp3 called with empty pcm_files list")
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg required for PCM chunk concat")

    ffmpeg_cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "s16le", "-ar", "24000", "-ac", "1",  # input PCM signature
        "-i", "pipe:0",
        "-c:a", "libmp3lame", "-b:a", TTS_BITRATE,
        "-ac", str(TTS_CHANNELS),
        "-id3v2_version", "3",
        str(out_path),
    ]
    log.debug("    [pcm-chunks] ffmpeg cmd=%s, files=%d", ffmpeg_cmd, len(pcm_files))

    # Create 50ms silence gap between chunks for smooth transitions.
    # 24kHz, 16-bit mono: 24000 samples/sec × 0.05 sec × 2 bytes/sample = 2400 bytes
    # This provides a natural pause without making the audio feel disconnected.
    silence_gap = b"\x00" * 2400  # 50ms of silence at 24kHz 16-bit mono

    # Read all PCM chunks into memory with silence gaps between them.
    # For a 30-min chapter at 24kHz 16-bit mono, the combined PCM is
    # ~86MB — fits comfortably in RAM and avoids the complexity of
    # streaming via communicate(input=generator). communicate() doesn't
    # actually support generators in all Python versions; loading is
    # simpler and the encoding step is the bottleneck anyway.
    parts = []
    for i, p in enumerate(pcm_files):
        parts.append(p.read_bytes())
        # Add silence gap between chunks (but not after the last chunk)
        if i < len(pcm_files) - 1:
            parts.append(silence_gap)
    combined = b"".join(parts)

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _, stderr_bytes = proc.communicate(input=combined, timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr_bytes = proc.communicate()
        raise RuntimeError(
            f"PCM chunk concat timed out after 300s. stderr: "
            f"{stderr_bytes.decode(errors='ignore')[:300]}"
        )
    if proc.returncode != 0:
        stderr_text = (stderr_bytes or b"").decode(errors="ignore")[:300]
        raise RuntimeError(
            f"PCM chunk concat failed (exit {proc.returncode}): {stderr_text}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("PCM chunk concat produced no output file")


def _tts_elevenlabs(text: str) -> bytes:
    """
    POST to ElevenLabs' /v1/text-to-speech/{voice_id} endpoint.
    Returns raw audio bytes (mp3).
    ElevenLabs' free/Starter tier caps a single text request at ~5000 chars;
    the caller is expected to have chunked appropriately.
    """
    if not TTS_API_KEY:
        raise RuntimeError("TTS_API_KEY is not set. Copy .env.example to .env and add your ElevenLabs key.")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    body = json_dumps({
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": ELEVENLABS_STABILITY,
            "similarity_boost": ELEVENLABS_SIMILARITY,
            "style": ELEVENLABS_STYLE,
        },
    }).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": TTS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=TTS_REQUEST_TIMEOUT) as resp:
        return resp.read()


# Path to the MLX standalone script (relative to this file). Only used
# when TTS_PROVIDER == "mlx_local". The script lives next to convert_books.py
# and can be invoked from any Python venv that has mlx-audio installed.
MLX_STANDALONE_SCRIPT = Path(__file__).parent / "tts_mlx_standalone.py"


def _tts_mlx_local(text: str) -> bytes:
    """TTS via local MLX inference on Apple Silicon (M1/M2/M3/M4/M5).

    Invokes tts_mlx_standalone.py as a subprocess. This keeps the heavy
    MLX dependency isolated in its own venv, so the main convert_books.py
    stays light and works on any machine (Linux, Mac, Windows).

    Returns raw PCM bytes (24kHz, 16-bit signed little-endian, mono) —
    same format as openai_compatible with Gemini, so the existing
    chapter-concat pipeline handles it without changes.

    ⚠️  Hardware requirements (this is NOT optional):
        - Apple Silicon Mac (M1/M2/M3/M4/M5). Intel Macs and Linux/Windows
          are NOT supported by MLX. On those platforms, use
          TTS_PROVIDER=openai_compatible instead.
        - macOS 13.5+ recommended
        - 8 GB RAM minimum (16 GB+ for the larger models)
        - First run downloads the model weights (500MB-2GB depending on
          which MLX_MODEL you choose)

    ⏱️  Speed varies a lot with hardware (per 12-chunk chapter):
        - M5 base:           ~3-4s  (real-time on GPU/ANE)
        - M5 Pro/Max/Ultra:  ~2-3s
        - M4 / M3:           ~5-6s
        - M2 / M1:           ~8-10s
        - Older Apple Silicon: still works, but real-time factor ~1-2x
        The same script will work on all Apple Silicon, just slower on
        older chips. The M5 is fast enough that this is now competitive
        with cloud TTS (and free, with no rate limits).

    Required setup (one-time, on Apple Silicon Mac):
        python3 -m venv ~/.venvs/mlx-audio
        source ~/.venvs/mlx-audio/bin/activate
        pip install mlx-audio
        # First run downloads the model (~500MB-2GB depending on choice)

    Configuration (in .env):
        TTS_PROVIDER=mlx_local
        MLX_MODEL=mlx-community/Kokoro-82M-bf16   # 54 voices, 8 languages, 355MB (default)
        # or: mlx-community/Spark-TTS-0.5B-bf16 (English + Chinese, ~1GB)
        # or: mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16 (high quality, ~3.5GB)
        MLX_VOICE=af_bella   # Kokoro voice name; see model's HuggingFace card for others
        MLX_PYTHON=~/.venvs/mlx-audio/bin/python  # path to MLX venv Python
    """
    if not MLX_STANDALONE_SCRIPT.exists():
        raise RuntimeError(
            f"MLX standalone script not found at {MLX_STANDALONE_SCRIPT}. "
            f"This file ships with book_audiobook_reader — please reinstall."
        )

    # Resolve the Python interpreter to use. MLX requires Python 3.9-3.12
    # and a specific venv; using system Python would fail.
    mlx_python = os.getenv("MLX_PYTHON", "")
    if not mlx_python:
        # Try common locations before erroring
        for candidate in [
            os.path.expanduser("~/.venvs/mlx-audio/bin/python"),
            os.path.expanduser("~/venvs/mlx-audio/bin/python"),
            "/opt/homebrew/bin/python3",
        ]:
            if os.path.exists(candidate):
                mlx_python = candidate
                break
    if not mlx_python or not os.path.exists(mlx_python):
        raise RuntimeError(
            "MLX_PYTHON not set and no default MLX venv found. "
            "Set MLX_PYTHON=/path/to/your/mlx-venv/bin/python in .env, "
            "or run: python3 -m venv ~/.venvs/mlx-audio && "
            "source ~/.venvs/mlx-audio/bin/activate && pip install mlx-audio"
        )

    model = os.getenv("MLX_MODEL", "mlx-community/Kokoro-82M-bf16")
    voice = os.getenv("MLX_VOICE", "af_bella")
    speed = float(os.getenv("TTS_SPEED", "1.0"))
    # MLX_LANG: language code for Kokoro (default "a" = American English).
    # This is required because mlx-audio 0.2.10's KokoroPipeline ALIASES dict
    # does not include "en", so the default lang_code="en" raises:
    #   AssertionError: ('en', {'a': 'American English', 'b': 'British English', ...})
    # See https://github.com/Blaizzy/mlx-audio/issues/378
    lang = os.getenv("MLX_LANG", "a")

    proc = subprocess.run(
        [
            mlx_python,
            str(MLX_STANDALONE_SCRIPT),
            "--text", text,
            "--model", model,
            "--voice", voice,
            "--speed", str(speed),
            "--lang", lang,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        # Print full stderr (truncated to 5000 chars to avoid huge logs).
        # The standalone script now includes traceback.format_exc() in its
        # error messages, so the user can see the exact failure.
        raise RuntimeError(
            f"MLX TTS failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:5000]}"
        )
    # The standalone script writes PCM bytes to a temp file and prints the
    # path on stdout. This keeps the JSON-control-plane separate from
    # the binary audio data, avoiding shell-escaping issues with non-UTF8 bytes.
    pcm_path = Path(proc.stdout.strip())
    if not pcm_path.exists():
        raise RuntimeError(
            f"MLX TTS reported success but output file {pcm_path} not found. "
            f"stderr was: {proc.stderr.strip()[:500]}"
        )
    try:
        return pcm_path.read_bytes()
    finally:
        # Always clean up the temp file
        try:
            pcm_path.unlink()
        except OSError:
            pass


# Provider dispatch table — add new providers here.
_TTS_PROVIDERS = {
    "openai_compatible": _tts_openai_compatible,
    "elevenlabs": _tts_elevenlabs,
    "mlx_local": _tts_mlx_local,
}


_PCM_BYTES_PER_SECOND = 24000 * 2 * 1  # 24kHz, 16-bit, mono


def _audio_duration_seconds(audio: bytes) -> float:
    """Measure the playback duration of TTS output bytes.

    openai_compatible and mlx_local return raw PCM (24kHz/16-bit/mono), whose
    duration is exact byte arithmetic. ElevenLabs returns MP3, which has no
    fixed bytes-per-second, so we probe it with ffprobe. Returns 0.0 when the
    duration cannot be determined (treated as "unknown", never as truncation).
    """
    if not audio:
        return 0.0
    if TTS_PART_FORMAT == "pcm":
        return len(audio) / _PCM_BYTES_PER_SECOND
    if not ffmpeg_available():
        return 0.0
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", "pipe:0"],
            input=audio, capture_output=True, timeout=30,
        )
        return float(proc.stdout.decode(errors="ignore").strip() or 0.0)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def _is_truncated(text: str, audio: bytes) -> bool:
    """Heuristic: True if `audio` is implausibly short for `text`'s length.

    Catches the silent-truncation failure mode where a provider returns a 200
    response with audio that stops before the input text finishes. Inverts the
    chars_for_audio_seconds budget (~2400 chars/min) to derive the expected
    duration, then flags audio shorter than TTS_MIN_AUDIO_RATIO of it.
    """
    if not TTS_VERIFY_AUDIO_LEN or len(text) < TTS_VERIFY_MIN_CHARS:
        return False
    expected = (len(text) / 2400.0) * 60.0
    actual = _audio_duration_seconds(audio)
    if actual <= 0.0:
        return False  # could not measure — don't false-positive
    return actual < expected * TTS_MIN_AUDIO_RATIO


def tts_one_chunk(text: str) -> bytes:
    """Dispatch a TTS request to the configured provider, retrying on transient
    HTTP/network errors AND on silently-truncated audio (see _is_truncated)."""
    if TTS_PROVIDER not in _TTS_PROVIDERS:
        raise RuntimeError(
            f"Unknown TTS_PROVIDER={TTS_PROVIDER!r}. "
            f"Supported: {', '.join(sorted(_TTS_PROVIDERS))}"
        )
    impl = _TTS_PROVIDERS[TTS_PROVIDER]

    last_err = None
    for attempt in range(1, TTS_MAX_RETRIES + 1):
        try:
            audio = impl(text)
            if _is_truncated(text, audio):
                got = _audio_duration_seconds(audio)
                want = (len(text) / 2400.0) * 60.0
                last_err = (
                    f"truncated audio: got {got:.1f}s for {len(text)} chars "
                    f"(expected ~{want:.1f}s)"
                )
                if attempt < TTS_MAX_RETRIES:
                    wait = TTS_RETRY_BACKOFF * attempt
                    log.warning(
                        "Chunk truncated (attempt %d/%d, %s) — retrying in %.1fs",
                        attempt, TTS_MAX_RETRIES, last_err, wait,
                    )
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"TTS returned {last_err} after {TTS_MAX_RETRIES} attempts. "
                    f"Lower TTS_CHUNK_CHARS (current {TTS_CHUNK_CHARS}) — this "
                    f"provider truncates long inputs."
                )
            return audio
        except error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", errors="ignore")
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in (429, 500, 502, 503, 504):
                wait = TTS_RETRY_BACKOFF * attempt
                log.warning(
                    "Chunk failed (attempt %d/%d, %s) — retrying in %.1fs",
                    attempt, TTS_MAX_RETRIES, last_err, wait,
                )
                time.sleep(wait)
                continue
            raise RuntimeError(f"TTS HTTP {e.code}: {detail}") from e
        except (error.URLError, TimeoutError) as e:
            last_err = str(e)
            wait = TTS_RETRY_BACKOFF * attempt
            log.warning(
                "Chunk network error (attempt %d/%d: %s) — retrying in %.1fs",
                attempt, TTS_MAX_RETRIES, last_err, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"TTS request failed after {TTS_MAX_RETRIES} attempts: {last_err}")

def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _write_concat_list(part_files: list[Path], list_path: Path) -> None:
    """Write an ffmpeg concat demuxer manifest for the given MP3 parts.

    Resolves each path to absolute (sidesteps cwd-resolution quirks when
    ffmpeg and the script have different cwds, e.g. macOS Finder "Open With",
    launchd, etc.) and escapes single quotes per the ffmpeg concat demuxer
    spec (`'` → `'\''`).
    """
    with open(list_path, "w") as f:
        for p in part_files:
            abs_p = p.resolve()
            escaped = abs_p.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))
            f.write(f"file '{escaped}'\n")


def concat_mp3_parts(
    part_files: list[Path],
    out_path: Path,
    log_obj=None,
    label: str = "concat",
) -> None:
    """Concatenate a list of MP3 part files into one MP3, with a smart strategy.

    Honors the CHAPTER_CONCAT_MODE env var:

    * ``"auto"`` (default) — try ``ffmpeg -c copy`` first (fast: ~1s/chapter).
      If the MP3 muxer rejects with a non-monotonic-DTS error (common with
      Kokoro and some ElevenLabs/OpenAI responses), fall back to a
      PCM-intermediate pipeline that re-encodes through libmp3lame at
      TTS_BITRATE. This gives a 6x speedup on well-formed inputs while
      remaining bulletproof for the malformed-input case.

    * ``"copy"`` — always use ``-c copy``. Fastest (~1s/chapter) but fails
      on malformed MP3s. Use only when you've verified your TTS provider
      always returns clean MP3s.

    * ``"reencode"`` — always go through the PCM intermediate pipeline.
      Slowest (~25s/chapter for a 3h chapter) but guaranteed to work.

    The output MP3 is always encoded at TTS_BITRATE (default 64k) and
    TTS_CHANNELS (default 1, mono) for consistent playback across chapters.

    Raises ``RuntimeError`` if all strategies fail.
    """
    _log = log_obj or log
    if not part_files:
        raise ValueError("concat_mp3_parts called with empty part_files list")
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not found in PATH")

    list_path = part_files[0].parent / "_concat_manifest.txt"
    _write_concat_list(part_files, list_path)

    # Honor explicit mode or "auto" (try-copy-then-reencode)
    mode = CHAPTER_CONCAT_MODE
    if mode not in ("auto", "copy", "reencode"):
        _log.warning("Unknown CHAPTER_CONCAT_MODE=%r, defaulting to 'auto'", mode)
        mode = "auto"

    last_err: str | None = None

    # ---- Fast path: try -c copy first (works on most well-formed inputs) ----
    if mode in ("auto", "copy"):
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path.resolve()),
            "-c", "copy",  # pure stream copy, no re-encode
            str(out_path),
        ]
        _log.debug("    [%s] ffmpeg (copy) cmd=%s", label, cmd)
        proc = subprocess.run(cmd, cwd=os.getcwd(), capture_output=True, text=True)
        if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            _log.info("    [%s] concat OK (copy mode, %d parts)", label, len(part_files))
            return
        last_err = proc.stderr.strip() or f"exit {proc.returncode}"
        if mode == "copy":
            # User explicitly asked for copy; don't fall back
            raise RuntimeError(
                f"ffmpeg -c copy failed for {label}: {last_err[:200]}"
            )
        _log.warning(
            "    [%s] -c copy failed (%s) — falling back to PCM re-encode",
            label, last_err[:120],
        )

    # ---- Slow path: PCM intermediate re-encode (bulletproof) ----
    # Stage 1: concat parts → WAV (uncompressed PCM, no MP3 muxer)
    # Stage 2: WAV → final MP3 with explicit libmp3lame settings
    wav_path = part_files[0].parent / "_concat_intermediate.wav"
    try:
        stage1 = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path.resolve()),
                "-c:a", "pcm_s16le", "-ar", "24000", "-ac", str(TTS_CHANNELS),
                str(wav_path),
            ],
            cwd=os.getcwd(), capture_output=True, text=True,
        )
        if stage1.returncode != 0:
            raise RuntimeError(
                f"ffmpeg stage1 (concat→WAV) failed for {label}: "
                f"exit {stage1.returncode}, stderr={stage1.stderr[:200]}"
            )

        # Stage 2: WAV → MP3 with configured bitrate and channels
        stage2 = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(wav_path),
                "-c:a", "libmp3lame", "-b:a", TTS_BITRATE,
                "-ac", str(TTS_CHANNELS),
                "-id3v2_version", "3",
                str(out_path),
            ],
            cwd=os.getcwd(), capture_output=True, text=True,
        )
        if stage2.returncode != 0:
            raise RuntimeError(
                f"ffmpeg stage2 (WAV→MP3) failed for {label}: "
                f"exit {stage2.returncode}, stderr={stage2.stderr[:200]}"
            )

        _log.info(
            "    [%s] concat OK (reencode mode, %d parts, %s %dch)",
            label, len(part_files), TTS_BITRATE, TTS_CHANNELS,
        )
    finally:
        # Always clean up the intermediate WAV, even on failure
        try:
            wav_path.unlink()
        except OSError:
            pass


def _render_chunks_parallel(
    chunks: list[str],
    tmp_dir: Path,
    workers: int,
    part_format: str,
) -> list[Path]:
    """Render a chapter's TTS chunks in parallel using a thread pool.

    Cloud TTS is I/O-bound (waiting on the network), so threads give a
    near-linear speedup up to the connection/bandwidth limit. Typical
    numbers for OpenRouter + Gemini:
        1 worker:   ~24s for 12 chunks
        4 workers:  ~6-8s
        8 workers:  ~3-4s
       16 workers:  ~2-3s (may hit rate limits)

    Args:
        chunks: list of text chunks to render.
        tmp_dir: where to write the .pcm/.mp3 part files.
        workers: number of concurrent threads.
        part_format: file extension for the parts (e.g. 'pcm' or 'mp3').

    Returns:
        List of part file paths in original chunk order (part_0001,
        part_0002, ...). Order is preserved regardless of completion
        order, so the concat at the end is correct.
    """
    n = len(chunks)
    indexed_results: dict[int, Path] = {}
    completed = 0
    failed: list[Exception] = []
    t0 = time.perf_counter()

    def _render_one(idx: int, chunk: str) -> tuple[int, Path]:
        part_path = tmp_dir / f"part_{idx + 1:04d}.{part_format}"
        if part_path.exists() and part_path.stat().st_size > 0:
            return idx, part_path  # already on disk — skip API call
        audio = tts_one_chunk(chunk)
        with open(part_path, "wb") as f:
            f.write(audio)
        return idx, part_path

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_render_one, i, c): i for i, c in enumerate(chunks)}
        for future in as_completed(futures):
            try:
                idx, path = future.result()
                indexed_results[idx] = path
                completed += 1
                elapsed = time.perf_counter() - t0
                # Per-chunk progress logging (thread-safe via logging module)
                log.info(
                    "    [%d/%d]  TTS done (%.1fs elapsed)",
                    completed, n, elapsed,
                )
            except Exception as e:
                # Capture the original chunk index for diagnostics.
                failed.append(e)
                log.error("    TTS chunk failed: %s", e)
                # Cancel pending futures; we won't have a complete set.
                for f in futures:
                    f.cancel()
                break

    if failed:
        # Re-raise the first failure (preserve original exception type)
        raise failed[0]

    # Return paths in original chunk order so the concat is correct.
    return [indexed_results[i] for i in range(n)]


# ---------------------------------------------------------------------------
# Chapter → MP3 (chunked + concatenated)
# ---------------------------------------------------------------------------

def render_chapter(title: str, text: str, out_path: Path, tmp_dir: Path) -> None:
    """
    Render a single chapter to one MP3 file. Chunks the text, calls TTS,
    concatenates with ffmpeg (no re-encode if possible).
    """
    # Clean text for TTS: collapse em-dashes, ellipses, double spaces, and
    # remove space between small words (of/the/and) and following punctuation.
    # Numeric ranges like "1750–1550" become "1750 to 1550" so the TTS engine
    # doesn't add an audible breath on the en-dash. This eliminates the
    # ~100-300ms pauses that Kokoro and other engines add on these patterns.
    if CLEAN_FOR_TTS:
        text = clean_for_tts(text)
    chunks = chunk_text(text, TTS_CHUNK_CHARS)
    log.info("  Chapter '%s' → %d chunk(s)", title, len(chunks))

    tmp_dir.mkdir(parents=True, exist_ok=True)

    # On resume: delete the highest-numbered part file that exists — it was
    # the chunk in-flight when the previous run was interrupted and may be
    # partially written. All lower-numbered parts are safe to reuse.
    existing = sorted(tmp_dir.glob(f"part_*.{TTS_PART_FORMAT}"))
    if existing:
        last = existing[-1]
        last.unlink()
        log.info("    Dropped possibly-incomplete part %s — will re-render it", last.name)

    if TTS_PARALLEL == 1 or len(chunks) == 1:
        # Sequential path (no parallelism). Same as before.
        part_files: list[Path] = []
        for i, chunk in enumerate(chunks, start=1):
            part_path = tmp_dir / f"part_{i:04d}.{TTS_PART_FORMAT}"
            if part_path.exists() and part_path.stat().st_size > 0:
                log.info("    [%d/%d]  cached (skipping API call)", i, len(chunks))
                part_files.append(part_path)
                continue
            log.info("    [%d/%d]  TTS %d chars…", i, len(chunks), len(chunk))
            audio = tts_one_chunk(chunk)
            with open(part_path, "wb") as f:
                f.write(audio)
            part_files.append(part_path)
    else:
        # Parallel path: dispatch all chunks at once to a thread pool.
        # Cloud TTS is I/O-bound (waiting on the network), so threads
        # give a near-linear speedup up to the network/connection limit.
        log.info("    TTS parallelism: %d workers", TTS_PARALLEL)
        part_files = _render_chunks_parallel(
            chunks, tmp_dir, TTS_PARALLEL, TTS_PART_FORMAT
        )

    # Concatenate parts into a single output file.
    if not part_files:
        log.warning("    No audio parts produced for chapter '%s' — skipping", title)
        return

    if len(part_files) == 1:
        # Single part: if it's already the right format, just move it;
        # otherwise transcode.
        single = part_files[0]
        if TTS_PART_FORMAT == TTS_FORMAT:
            shutil.move(str(single), str(out_path))
        else:
            # Cross-format (e.g. PCM part, MP3 output). Transcode with ffmpeg.
            _pcm_chunks_to_mp3([single], out_path)
    elif ffmpeg_available():
        if TTS_PART_FORMAT == "pcm" and TTS_FORMAT == "mp3":
            # Fast path: cat all PCM chunks and encode to MP3 in one pass.
            _pcm_chunks_to_mp3(part_files, out_path)
        else:
            # MP3 or other format: use the standard concat pipeline.
            concat_mp3_parts(part_files, out_path, log_obj=log, label=f"chapter '{title}'")
    else:
        # Pure-Python fallback: raw byte concat. Works for MP3 because the
        # container has no header — each frame is independently decodable.
        # Does NOT work for PCM (frames are 16-bit samples with no header).
        if TTS_PART_FORMAT != "mp3":
            raise RuntimeError(
                f"ffmpeg required for {TTS_PART_FORMAT} concat but not found in PATH"
            )
        log.warning("    ffmpeg not found; doing raw MP3 byte-append")
        with open(out_path, "wb") as out:
            for p in part_files:
                with open(p, "rb") as f:
                    out.write(f.read())


# ---------------------------------------------------------------------------
# Book → folder
# ---------------------------------------------------------------------------

def convert_book(epub_path: Path) -> Path:
    """Convert one EPUB to a per-chapter folder and a concatenated full-book MP3.

    In DRY_RUN mode, only the first ~DRY_RUN_SECONDS of the first chapter are
    converted (1 small TTS call). Output goes to a `dry-run/` subfolder under
    OUTPUT_DIR so it doesn't pollute real runs.
    """
    parsed = extract_chapters(epub_path)
    book_title = parsed["title"]
    chapters = parsed["chapters"]
    if not chapters:
        raise RuntimeError(f"No chapters extracted from {epub_path}")

    # --skip-front-matter: drop structural front/back-matter (cover, copyright,
    # TOC, bibliography, index, nav, etc.) by default. See SKIP_FRONT_MATTER_PATTERNS.
    if SKIP_FRONT_MATTER:
        before = len(chapters)
        chapters = [c for c in chapters if not is_front_matter(c["title"], SKIP_FRONT_MATTER_PATTERNS)]
        skipped = before - len(chapters)
        if skipped:
            log.info(
                "Skipped %d front/back-matter chapter(s) matching patterns %s",
                skipped, SKIP_FRONT_MATTER_PATTERNS,
            )
        if not chapters:
            raise RuntimeError(
                f"All {before} chapters matched front-matter patterns — nothing to render. "
                f"Use --no-skip-front-matter or set SKIP_FRONT_MATTER_PATTERNS to override."
            )

    book_slug = slugify(book_title)
    if DRY_RUN:
        work_dir = OUTPUT_DIR / "dry-run" / book_slug
    else:
        work_dir = OUTPUT_DIR / book_slug
    work_dir.mkdir(parents=True, exist_ok=True)
    chapter_dir = work_dir / "chapters"
    chapter_dir.mkdir(exist_ok=True)
    tmp_dir = work_dir / "_tmp"
    if tmp_dir.exists() and not CONTINUE_RUN:
        # Wipe tmp on a fresh run only — on --continue we may want the
        # tmp dir to exist for new chapters, but old per-chapter subdirs
        # from a previous run are harmless (we always use unique chXX dirs).
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(exist_ok=True)

    if DRY_RUN:
        log.warning("=" * 60)
        log.warning("DRY RUN — only the first ~%.0fs of the first non-empty chapter will be rendered.", DRY_RUN_SECONDS)
        log.warning("DRY RUN — the full book will be skipped.")
        log.warning("DRY RUN — your TTS key IS still used (this calls the real TTS API).")
        log.warning("=" * 60)
        # Build a synthetic single-chapter "sample" by concatenating chapters
        # until the character budget is met (the first chapter alone may be
        # shorter than the requested dry-run duration).
        budget = chars_for_audio_seconds(DRY_RUN_SECONDS)
        collected: list[str] = []
        collected_chars = 0
        first_title = None
        for c in chapters:
            t = c["text"].strip()
            if not t or len(t) < 50:
                continue
            if first_title is None:
                first_title = c["title"]
            remaining = budget - collected_chars
            if len(t) <= remaining:
                collected.append(t)
                collected_chars += len(t)
            else:
                # Slice the last chapter at a sentence boundary
                collected.append(slice_for_dry_run(t, DRY_RUN_SECONDS * (remaining / budget)))
                collected_chars = budget
                break
            if collected_chars >= budget:
                break
        sample_text = "\n\n".join(collected)
        log.info(
            "Sample text: %d chars across %d chapter(s), targeting ~%.0fs",
            len(sample_text), len(collected), DRY_RUN_SECONDS,
        )
        sample_chapter = {
            "title": f"{first_title or 'Sample'} (sample)",
            "text": sample_text,
        }
        chapters = [sample_chapter]
    else:
        # Write a tiny README for the audiobook folder
        manifest_lines = [
            f"# {book_title}",
            "",
            f"Source: `{epub_path.name}`",
            f"Provider: `{TTS_PROVIDER}`",
            f"Model: `{TTS_MODEL}`",
            f"Voice: `{TTS_VOICE}`",
            f"Speed: {TTS_SPEED}x",
            f"Chapters: {len(chapters)}",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if CONTINUE_RUN:
            manifest_lines.append("Mode: continued from a previous run")
        manifest_lines.extend(["", "## Chapters", ""])
        for i, ch in enumerate(chapters, start=1):
            manifest_lines.append(f"{i:02d}. {ch['title']}  →  `chapters/{i:02d} - {slugify(ch['title'])}.{TTS_FORMAT}`")
        (work_dir / "README.md").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    # Render each chapter
    chapter_mp3s: list[Path] = []
    skipped_existing = 0
    for i, ch in enumerate(chapters, start=1):
        ch_title = ch["title"]
        ch_slug = slugify(ch_title)
        out_path = chapter_dir / f"{i:02d} - {ch_slug}.{TTS_FORMAT}"

        # --continue: if the chapter MP3 already exists with nonzero size,
        # assume it's a valid prior render and skip TTS + concat for it.
        # This lets an aborted run pick up where it left off.
        if CONTINUE_RUN and out_path.exists() and out_path.stat().st_size > 0:
            log.info("  [%d/%d] SKIP '%s' — already exists (%.1f MB)",
                     i, len(chapters), ch_title, out_path.stat().st_size / (1024 * 1024))
            chapter_mp3s.append(out_path)
            skipped_existing += 1
            continue

        try:
            render_chapter(ch_title, ch["text"], out_path, tmp_dir / f"ch{i:02d}")
        except Exception as e:
            # If this was a dry-run, tell the user where the sample WOULD have
            # been written — otherwise they have no idea which folder to look
            # in. Re-raise so the script still exits non-zero.
            if DRY_RUN:
                log.error("DRY RUN — no sample was written (TTS failed: %s)", e)
                log.error("DRY RUN — expected output path: %s", out_path)
            raise
        chapter_mp3s.append(out_path)

    if CONTINUE_RUN and skipped_existing:
        log.info("Skipped %d already-rendered chapter(s) (--continue).", skipped_existing)

    # Dry-run: report and stop here
    if DRY_RUN:
        sample_path = chapter_mp3s[0] if chapter_mp3s else None
        if sample_path and sample_path.exists():
            size_mb = sample_path.stat().st_size / (1024 * 1024)
            log.info("DRY RUN — sample written: %s (%.2f MB)", sample_path, size_mb)
            log.info("DRY RUN — to play it: ffplay '%s'", sample_path)
        # Cleanup tmp
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Write a small dry-run manifest
        (work_dir / "DRY-RUN.md").write_text(
            f"# DRY RUN sample — {book_title}\n\n"
            f"- Provider: `{TTS_PROVIDER}`\n- Model: `{TTS_MODEL}`\n"
            f"- Voice: `{TTS_VOICE}`\n- Speed: {TTS_SPEED}x\n"
            f"- Approx sample length: {DRY_RUN_SECONDS:.0f}s\n"
            f"- Sample file: `chapters/{chapter_mp3s[0].name if chapter_mp3s else 'n/a'}`\n\n"
            f"Run without `--dry-run` to convert the full book.\n",
            encoding="utf-8",
        )
        return work_dir

    # Build the full-book MP3
    full_path = work_dir / f"{book_slug} - complete.{TTS_FORMAT}"

    # --continue: if the full-book MP3 already exists AND every chapter is
    # accounted for (i.e. nothing was rendered this run), skip the concat.
    # If any chapter was rendered, we MUST rebuild full_path because the
    # content has changed.
    if (
        CONTINUE_RUN
        and full_path.exists()
        and full_path.stat().st_size > 0
        and skipped_existing == len(chapters)
    ):
        log.info("Full-book MP3 already exists (%.1f MB) and no chapters needed rendering — skipping concat.",
                 full_path.stat().st_size / (1024 * 1024))
    elif len(chapter_mp3s) == 1:
        shutil.copy2(chapter_mp3s[0], full_path)
    elif ffmpeg_available():
        concat_mp3_parts(
            chapter_mp3s, full_path,
            log_obj=log, label=f"full book ({book_title})",
        )
    else:
        log.warning("ffmpeg not found; doing raw MP3 byte-append for full book")
        with open(full_path, "wb") as out:
            for p in chapter_mp3s:
                with open(p, "rb") as f:
                    out.write(f.read())

    # Cleanup temp
    shutil.rmtree(tmp_dir, ignore_errors=True)

    log.info("Wrote audiobook folder: %s", work_dir)
    return work_dir


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> dict:
    """Parse CLI args. Returns dict with: dry_run (bool), dry_run_seconds (float), book (Path|None)."""
    import argparse
    p = argparse.ArgumentParser(
        prog="convert_books.py",
        description="Convert EPUB files in ./epubs/ to MP3 audiobooks.",
    )
    p.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Render only the first ~30s of the first book (or --book) and stop. "
             "No full-book output. Useful for testing voice/quality before "
             "committing to a full conversion. STILL USES YOUR TTS KEY (1 API call).",
    )
    p.add_argument(
        "--dry-run-seconds", dest="dry_run_seconds", type=float, default=30.0,
        help="Length of the dry-run sample in seconds (default: 30).",
    )
    p.add_argument(
        "--book", dest="book", type=str, default=None,
        help="Only convert this one epub (filename or stem) instead of all in INPUT_DIR. "
             "Combine with --dry-run to sample a specific book.",
    )
    p.add_argument(
        "--list", dest="list_books", action="store_true",
        help="List epubs in INPUT_DIR and exit.",
    )
    p.add_argument(
        "--continue", "--resume", dest="continue_run", action="store_true",
        help="Resume an aborted run: skip chapters that already have an MP3 in "
             "audiobooks/<book>/chapters/ and start from the first missing one. "
             "Also skips re-rendering the full-book MP3 if it already exists. "
             "If you've changed the EPUB since the abort, do a fresh run instead.",
    )
    p.add_argument(
        "--skip-front-matter", dest="skip_front_matter", action="store_true", default=True,
        help="Skip structural front/back-matter chapters (cover, copyright, TOC, "
             "bibliography, index, nav) by default. Pass --no-skip-front-matter to "
             "render everything. Customize the patterns with the SKIP_FRONT_MATTER_PATTERNS "
             "env var (comma-separated).",
    )
    p.add_argument(
        "--no-skip-front-matter", dest="skip_front_matter", action="store_false",
        help="Render every chapter including cover, copyright, TOC, etc.",
    )
    p.add_argument(
        "--clean", dest="clean", action="store_true", default=True,
        help="Normalize text before TTS to eliminate audible pauses (default). "
             "Replaces em/en-dashes with commas, ellipses with periods, collapses "
             "double spaces, and converts numeric ranges like '1750–1550' to "
             "'1750 to 1550'.",
    )
    p.add_argument(
        "--no-clean", dest="clean", action="store_false",
        help="Send raw EPUB text straight to TTS. Use this if the cleanup changes "
             "the meaning or you want maximum control over the input.",
    )
    args = p.parse_args(argv)
    return {
        "dry_run": args.dry_run,
        "dry_run_seconds": args.dry_run_seconds,
        "book": Path(args.book) if args.book else None,
        "list_books": args.list_books,
        "continue_run": args.continue_run,
        "skip_front_matter": args.skip_front_matter,
        "clean": args.clean,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Make dry-run / continue / skip-front-matter / clean flags module-global so convert_book can see them
    global DRY_RUN, DRY_RUN_SECONDS, CONTINUE_RUN, SKIP_FRONT_MATTER, CLEAN_FOR_TTS
    DRY_RUN = args["dry_run"]
    DRY_RUN_SECONDS = args["dry_run_seconds"]
    CONTINUE_RUN = args["continue_run"]
    SKIP_FRONT_MATTER = args["skip_front_matter"]
    CLEAN_FOR_TTS = args["clean"]

    if not INPUT_DIR.exists():
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        log.info("Created %s — drop .epub files in there and re-run.", INPUT_DIR.resolve())
        return 0

    epubs = sorted(INPUT_DIR.glob("*.epub"))
    if args["book"]:
        target = args["book"]
        if target.suffix == "":
            # Treat as a stem — match any file with that stem
            epubs = [p for p in epubs if p.stem == target.stem or p.name == target.name]
        else:
            epubs = [p for p in epubs if p.name == target.name]
        if not epubs:
            log.error("No epub matching %r in %s", str(target), INPUT_DIR.resolve())
            log.info("Available:")
            for p in sorted(INPUT_DIR.glob("*.epub")):
                log.info("  - %s", p.name)
            return 1

    if args["list_books"]:
        if not epubs:
            log.info("No .epub files in %s", INPUT_DIR.resolve())
        else:
            log.info("Found %d epub(s):", len(epubs))
            for p in epubs:
                size_mb = p.stat().st_size / (1024 * 1024)
                log.info("  - %s  (%.2f MB)", p.name, size_mb)
        return 0

    if not epubs:
        log.info("No .epub files in %s — nothing to do.", INPUT_DIR.resolve())
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Found %d EPUB(s). Provider=%s  Model=%s  Voice=%s  Speed=%sx",
             len(epubs), TTS_PROVIDER, TTS_MODEL, TTS_VOICE, TTS_SPEED)
    if not ffmpeg_available():
        log.warning("ffmpeg not found in PATH — falling back to raw byte concat. "
                    "Install ffmpeg for cleaner chapter joins.")
    if DRY_RUN:
        log.info("DRY RUN mode — will render ~%.0fs per book.", DRY_RUN_SECONDS)
    if CONTINUE_RUN:
        if DRY_RUN:
            log.error("--continue and --dry-run are incompatible. Pick one.")
            return 1
        log.info("CONTINUE mode — will skip chapters that already have an MP3 in chapters/.")
    if not SKIP_FRONT_MATTER:
        log.info("Front-matter filtering disabled — will render cover, copyright, TOC, etc.")
    if not CLEAN_FOR_TTS:
        log.info("Text cleanup disabled — sending raw EPUB text straight to TTS.")

    t0 = time.time()
    successes, failures = 0, 0
    for epub_path in epubs:
        log.info("=" * 60)
        log.info("Book: %s", epub_path.name)
        try:
            convert_book(epub_path)
            successes += 1
        except Exception as e:
            failures += 1
            log.error("FAILED %s: %s", epub_path.name, e)

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info("Done in %.1fs — %d ok, %d failed. Output in %s",
             elapsed, successes, failures, OUTPUT_DIR.resolve())
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
