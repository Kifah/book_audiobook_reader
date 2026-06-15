#!/usr/bin/env python3
"""
convert_books.py — Convert EPUB files in ./epubs/ into zipped MP3 audiobooks
in ./audiobooks/ via an OpenAI-compatible TTS endpoint (default: OpenRouter).

Usage:
    1. Drop one or more .epub files in ./epubs/
    2. Copy .env.example to .env and fill in your TTS_API_KEY
    3. python3 convert_books.py

Output:
    ./audiobooks/<book-slug>/  - per-chapter MP3s + final concatenated MP3
    ./audiobooks/<book-slug>.zip - zipped audiobook ready to share
"""

import os
import sys
import re
import io
import time
import shutil
import zipfile
import logging
import unicodedata
from pathlib import Path
from urllib import request, error

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
TTS_CHUNK_CHARS = int(os.getenv("TTS_CHUNK_CHARS", "4000"))
TTS_MAX_RETRIES = int(os.getenv("TTS_MAX_RETRIES", "4"))
TTS_RETRY_BACKOFF = float(os.getenv("TTS_RETRY_BACKOFF", "2.0"))
TTS_REQUEST_TIMEOUT = int(os.getenv("TTS_REQUEST_TIMEOUT", "180"))

INPUT_DIR = Path(os.getenv("INPUT_DIR", "epubs"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "audiobooks"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

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
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "book"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def slug_to_accent_label(slug: str) -> str:
    return slug.replace("-", " ").title()


def chars_for_audio_seconds(seconds: float) -> int:
    """
    Budget how many characters of input text are needed to produce ~`seconds`
    of spoken audio at TTS_SPEED.

    English speech at 150 wpm, ~5 chars/word (including spaces) = 750 chars/min
    at 1.0× speed. For `seconds` at TTS_SPEED:
        chars = 750 * (seconds / 60) / TTS_SPEED.
    A 20% safety margin makes the sample slightly shorter than the target
    (TTS engines often read a bit slower than naive math, especially at
    sentence boundaries where they pause).
    """
    base_chars_per_min = 750.0
    safe = max(0.1, seconds) * 0.8  # 20% margin
    budget = base_chars_per_min * (safe / 60.0) / max(0.25, TTS_SPEED)
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

        # Strip everything non-textual, keep paragraph breaks
        for br in soup.find_all("br"):
            br.replace_with("\n")
        text = soup.get_text("\n", strip=True)
        # Collapse runs of blank lines
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            continue

        chapters.append({"title": title or f"Chapter {idx}", "text": text})

    log.info("Extracted %d chapter(s) from '%s'", len(chapters), book_title)
    return {"title": book_title, "chapters": chapters}


# ---------------------------------------------------------------------------
# Text chunking (sentence-aware, character-bounded)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


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
                # Last-resort: hard-split a single huge sentence
                while len(sent) > max_chars:
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
    Returns raw audio bytes (mp3 by default).
    """
    if not TTS_API_KEY:
        raise RuntimeError("TTS_API_KEY is not set. Copy .env.example to .env and add your key.")

    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
        "response_format": TTS_FORMAT,
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
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=TTS_REQUEST_TIMEOUT) as resp:
        return resp.read()


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


# Provider dispatch table — add new providers here.
_TTS_PROVIDERS = {
    "openai_compatible": _tts_openai_compatible,
    "elevenlabs": _tts_elevenlabs,
}


def tts_one_chunk(text: str) -> bytes:
    """Dispatch a TTS request to the configured provider, with retry on transient errors."""
    if TTS_PROVIDER not in _TTS_PROVIDERS:
        raise RuntimeError(
            f"Unknown TTS_PROVIDER={TTS_PROVIDER!r}. "
            f"Supported: {', '.join(sorted(_TTS_PROVIDERS))}"
        )
    impl = _TTS_PROVIDERS[TTS_PROVIDER]

    last_err = None
    for attempt in range(1, TTS_MAX_RETRIES + 1):
        try:
            return impl(text)
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


# ---------------------------------------------------------------------------
# Chapter → MP3 (chunked + concatenated)
# ---------------------------------------------------------------------------

def render_chapter(title: str, text: str, out_path: Path, tmp_dir: Path) -> None:
    """
    Render a single chapter to one MP3 file. Chunks the text, calls TTS,
    concatenates with ffmpeg (no re-encode if possible).
    """
    chunks = chunk_text(text, TTS_CHUNK_CHARS)
    log.info("  Chapter '%s' → %d chunk(s)", title, len(chunks))

    tmp_dir.mkdir(parents=True, exist_ok=True)
    part_files: list[Path] = []

    for i, chunk in enumerate(chunks, start=1):
        log.info("    [%d/%d]  TTS %d chars…", i, len(chunks), len(chunk))
        audio = tts_one_chunk(chunk)
        part_path = tmp_dir / f"part_{i:04d}.{TTS_FORMAT}"
        with open(part_path, "wb") as f:
            f.write(audio)
        part_files.append(part_path)

    # Concatenate. ffmpeg concat-demuxer is the safest no-recode path.
    if not part_files:
        log.warning("    No audio parts produced for chapter '%s' — skipping", title)
        return

    if len(part_files) == 1:
        shutil.move(str(part_files[0]), str(out_path))
    elif ffmpeg_available():
        concat_list = tmp_dir / "concat.txt"
        with open(concat_list, "w") as f:
            for p in part_files:
                # Escape single quotes per ffmpeg concat demuxer spec
                f.write(f"file '{p.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(out_path),
        ]
        rc = os.spawnvp(os.P_WAIT, "ffmpeg", cmd)
        if rc != 0:
            raise RuntimeError(f"ffmpeg concat failed with exit code {rc}")
    else:
        # Pure-Python fallback: raw byte concat. Works for MP3 because the
        # container has no header — each frame is independently decodable.
        log.warning("    ffmpeg not found; doing raw MP3 byte-append")
        with open(out_path, "wb") as out:
            for p in part_files:
                with open(p, "rb") as f:
                    out.write(f.read())


# ---------------------------------------------------------------------------
# Book → folder + zip
# ---------------------------------------------------------------------------

def convert_book(epub_path: Path) -> Path:
    """Convert one EPUB to a per-chapter folder, a concatenated MP3, and a zip.

    In DRY_RUN mode, only the first ~DRY_RUN_SECONDS of the first chapter are
    converted (1 small TTS call). No zip is created. Output goes to a
    `dry-run/` subfolder under OUTPUT_DIR so it doesn't pollute real runs.
    """
    parsed = extract_chapters(epub_path)
    book_title = parsed["title"]
    chapters = parsed["chapters"]
    if not chapters:
        raise RuntimeError(f"No chapters extracted from {epub_path}")

    book_slug = slugify(book_title)
    if DRY_RUN:
        work_dir = OUTPUT_DIR / "dry-run" / book_slug
    else:
        work_dir = OUTPUT_DIR / book_slug
    work_dir.mkdir(parents=True, exist_ok=True)
    chapter_dir = work_dir / "chapters"
    chapter_dir.mkdir(exist_ok=True)
    tmp_dir = work_dir / "_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()

    if DRY_RUN:
        log.warning("=" * 60)
        log.warning("DRY RUN — only the first ~%.0fs of the first non-empty chapter will be rendered.", DRY_RUN_SECONDS)
        log.warning("DRY RUN — no zip will be produced, full book will be skipped.")
        log.warning("DRY RUN — your TTS key IS still used (this calls the real TTS API).")
        log.warning("=" * 60)
        # Build a synthetic single-chapter "sample" from the first chapter with real text
        sample_source = next(
            (c for c in chapters if len(c["text"].strip()) > 500),
            chapters[0],
        )
        sample_text = slice_for_dry_run(sample_source["text"], DRY_RUN_SECONDS)
        log.info("Sample text: %d chars (from chapter '%s')", len(sample_text), sample_source["title"])
        sample_chapter = {
            "title": f"{sample_source['title']} (sample)",
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
            "",
            "## Chapters",
            "",
        ]
        for i, ch in enumerate(chapters, start=1):
            manifest_lines.append(f"{i:02d}. {ch['title']}  →  `chapters/{i:02d} - {slugify(ch['title'])}.{TTS_FORMAT}`")
        (work_dir / "README.md").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    # Render each chapter
    chapter_mp3s: list[Path] = []
    for i, ch in enumerate(chapters, start=1):
        ch_title = ch["title"]
        ch_slug = slugify(ch_title)
        out_path = chapter_dir / f"{i:02d} - {ch_slug}.{TTS_FORMAT}"
        render_chapter(ch_title, ch["text"], out_path, tmp_dir / f"ch{i:02d}")
        chapter_mp3s.append(out_path)

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
    if len(chapter_mp3s) == 1:
        shutil.copy2(chapter_mp3s[0], full_path)
    elif ffmpeg_available():
        concat_list = tmp_dir / "concat_full.txt"
        with open(concat_list, "w") as f:
            for p in chapter_mp3s:
                f.write(f"file '{p.as_posix()}'\n")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(full_path),
        ]
        rc = os.spawnvp(os.P_WAIT, "ffmpeg", cmd)
        if rc != 0:
            raise RuntimeError(f"ffmpeg full-book concat failed with exit code {rc}")
    else:
        log.warning("ffmpeg not found; doing raw MP3 byte-append for full book")
        with open(full_path, "wb") as out:
            for p in chapter_mp3s:
                with open(p, "rb") as f:
                    out.write(f.read())

    # Cleanup temp
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Zip the audiobook (folder, not nested zip)
    zip_path = OUTPUT_DIR / f"{book_slug}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(work_dir):
            for name in files:
                full = Path(root) / name
                arc = full.relative_to(work_dir.parent)
                zf.write(full, arc)
    log.info("Wrote %s (%.1f MB)", zip_path, zip_path.stat().st_size / (1024 * 1024))
    return zip_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> dict:
    """Parse CLI args. Returns dict with: dry_run (bool), dry_run_seconds (float), book (Path|None)."""
    import argparse
    p = argparse.ArgumentParser(
        prog="convert_books.py",
        description="Convert EPUB files in ./epubs/ to zipped MP3 audiobooks.",
    )
    p.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Render only the first ~30s of the first book (or --book) and stop. "
             "No zip, no full-book output. Useful for testing voice/quality before "
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
    args = p.parse_args(argv)
    return {
        "dry_run": args.dry_run,
        "dry_run_seconds": args.dry_run_seconds,
        "book": Path(args.book) if args.book else None,
        "list_books": args.list_books,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Make dry-run flags module-global so convert_book/render_chapter can see them
    global DRY_RUN, DRY_RUN_SECONDS
    DRY_RUN = args["dry_run"]
    DRY_RUN_SECONDS = args["dry_run_seconds"]

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
        log.info("DRY RUN mode — will render ~%.0fs per book, no zips.", DRY_RUN_SECONDS)

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
