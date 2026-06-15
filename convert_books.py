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

TTS_API_URL = os.getenv("TTS_API_URL", "https://openrouter.ai/api/v1/audio/speech")
TTS_API_KEY = os.getenv("TTS_API_KEY", "")
TTS_MODEL = os.getenv("TTS_MODEL", "openai/gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "shimmer")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.2"))
TTS_FORMAT = os.getenv("TTS_FORMAT", "mp3")
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
# TTS call (one chunk)
# ---------------------------------------------------------------------------

def tts_one_chunk(text: str) -> bytes:
    """
    POST a single text chunk to the OpenAI-compatible /audio/speech endpoint.
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

    last_err = None
    for attempt in range(1, TTS_MAX_RETRIES + 1):
        try:
            with request.urlopen(req, timeout=TTS_REQUEST_TIMEOUT) as resp:
                return resp.read()
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
    """Convert one EPUB to a per-chapter folder, a concatenated MP3, and a zip."""
    parsed = extract_chapters(epub_path)
    book_title = parsed["title"]
    chapters = parsed["chapters"]
    if not chapters:
        raise RuntimeError(f"No chapters extracted from {epub_path}")

    book_slug = slugify(book_title)
    work_dir = OUTPUT_DIR / book_slug
    work_dir.mkdir(parents=True, exist_ok=True)
    chapter_dir = work_dir / "chapters"
    chapter_dir.mkdir(exist_ok=True)
    tmp_dir = work_dir / "_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()

    # Write a tiny README for the audiobook folder
    manifest_lines = [
        f"# {book_title}",
        "",
        f"Source: `{epub_path.name}`",
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

def main() -> int:
    if not INPUT_DIR.exists():
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        log.info("Created %s — drop .epub files in there and re-run.", INPUT_DIR.resolve())
        return 0

    epubs = sorted(INPUT_DIR.glob("*.epub"))
    if not epubs:
        log.info("No .epub files in %s — nothing to do.", INPUT_DIR.resolve())
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Found %d EPUB(s). Voice=%s  Model=%s  Speed=%sx",
             len(epubs), TTS_VOICE, TTS_MODEL, TTS_SPEED)
    if not ffmpeg_available():
        log.warning("ffmpeg not found in PATH — falling back to raw byte concat. "
                    "Install ffmpeg for cleaner chapter joins.")

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
