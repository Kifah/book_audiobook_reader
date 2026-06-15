# 📚 book_audiobook_reader

> Turn a folder of `.epub` files into zipped MP3 audiobooks via any OpenAI-compatible TTS endpoint (OpenRouter, OpenAI, or self-hosted).

```
epubs/  ──►  convert_books.py  ──►  audiobooks/<book>.zip
```

---

## What it does

1. **Reads** every `.epub` you drop in `epubs/`
2. **Extracts** chapters in reading order, stripping HTML
3. **Chunks** each chapter into ~4 000-character pieces (sentence-aware, never mid-sentence)
4. **Calls** your TTS endpoint (OpenRouter by default) for each chunk
5. **Concatenates** the resulting MP3s with `ffmpeg` (no re-encoding, no quality loss)
6. **Zips** the per-chapter MP3s + a single full-book MP3 into `audiobooks/<book-slug>.zip`

Result: a ready-to-share audiobook zip, structured like this:

```
audiobooks/
├── the-time-travelers-wife.zip
└── the-time-travelers-wife/
    ├── README.md
    ├── the-time-travelers-wife - complete.mp3
    └── chapters/
        ├── 01 - prologue.mp3
        ├── 02 - chapter-one.mp3
        └── ...
```

---

## Why

I wanted to listen to my ebook library on the go without giving Audible my credit card, and I already had an OpenRouter account. The script took about 200 lines of Python.

It's deliberately **provider-agnostic** — anything that speaks the OpenAI `/v1/audio/speech` schema works:

- [OpenRouter](https://openrouter.ai) (default — one account, many TTS models)
- [OpenAI](https://platform.openai.com) directly
- Self-hosted gateways like [LocalAI](https://localai.io), [Ollama](https://ollama.com) (with a TTS adapter), or [Kokoro](https://github.com/remsky/Kokoro-82M) behind an OpenAI-compatible proxy

---

## Quick start

### 1. Requirements

- Python **3.10+**
- `ffmpeg` on your `PATH` (for clean chapter joins) — install with `brew install ffmpeg`, `apt install ffmpeg`, etc. The script falls back to raw byte concat if missing.

### 2. Clone & install

```bash
git clone https://github.com/Kifah/book_audiobook_reader.git
cd book_audiobook_reader
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and set TTS_API_KEY
```

The defaults are tuned for **OpenRouter + `gpt-4o-mini-tts`** with a **calm British female voice at 1.2× speed**. Tweak to taste — see the file for every knob.

### 4. Drop books & run

```bash
mkdir -p epubs
cp ~/Downloads/some-book.epub epubs/
python3 convert_books.py
```

That's it. Watch the logs scroll by, grab your zip from `audiobooks/` when it's done.

---

## Configuration

Every setting lives in `.env` (gitignored). The full list:

| Var | Default | Notes |
|---|---|---|
| `TTS_API_KEY` | *(required)* | Your provider API key |
| `TTS_API_URL` | `https://openrouter.ai/api/v1/audio/speech` | OpenAI-compatible endpoint |
| `TTS_MODEL` | `openai/gpt-4o-mini-tts` | `openai/tts-1` and `openai/tts-1-hd` also work |
| `TTS_VOICE` | `shimmer` | Any of the 13 voices supported by your model |
| `TTS_SPEED` | `1.2` | 0.25 – 4.0 depending on provider |
| `TTS_FORMAT` | `mp3` | mp3, opus, aac, flac, wav, pcm |
| `TTS_INSTRUCTIONS` | British female calm narration | Only sent to `gpt-4o-mini-tts` (steerable prosody) |
| `TTS_CHUNK_CHARS` | `4000` | Per-request character cap |
| `TTS_MAX_RETRIES` | `4` | Retries on 429/5xx/network errors |
| `TTS_RETRY_BACKOFF` | `2.0` | Exponential backoff base (seconds) |
| `TTS_REQUEST_TIMEOUT` | `180` | Per-request timeout |
| `INPUT_DIR` | `epubs` | Where to look for `.epub` files |
| `OUTPUT_DIR` | `audiobooks` | Where to write the zipped output |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |

### Voice examples

`gpt-4o-mini-tts` ships with 13 voices. For audiobooks, common picks:

| Voice | Vibe |
|---|---|
| `shimmer` *(default)* | warm, calm, female — the classic audiobook narrator |
| `nova` | bright, friendly, female — good for YA or fiction |
| `coral` | warm, conversational, female — non-fiction, essays |
| `sage` | thoughtful, calm, androgynous — philosophy, dense prose |
| `ballad` | expressive, narrative — long-form storytelling |
| `onyx` | deep, male, authoritative — thrillers, noir |

**Note on accents:** none of the `gpt-4o-mini-tts` voices have an explicit "British" tag, but the model's `instructions` field steers prosody *and* accent. The default instructions explicitly request a British female voice. To get an American male voice, for example:

```bash
TTS_VOICE=onyx
TTS_INSTRUCTIONS="Speak in a deep, calm American male voice. Pace the narration naturally."
```

### Using OpenAI directly

```bash
# .env
TTS_API_URL=https://api.openai.com/v1/audio/speech
TTS_API_KEY=sk-proj-...
TTS_MODEL=gpt-4o-mini-tts
```

Drop the `openai/` prefix on the model — OpenAI takes bare names.

### Using a self-hosted model

Any OpenAI-compatible audio endpoint will work. Example for [Kokoro-82M](https://github.com/remsky/Kokoro-82M) via a LocalAI-style proxy:

```bash
TTS_API_URL=http://localhost:8080/v1/audio/speech
TTS_API_KEY=local
TTS_MODEL=kokoro
TTS_VOICE=af_bella    # voice IDs vary by engine
TTS_INSTRUCTIONS=
```

---

## Cost (rough)

A 3-hour audiobook (≈ 180 000 chars) on `gpt-4o-mini-tts`:

| Provider | 3-hour book | 10-hour book |
|---|---|---|
| **OpenRouter `gpt-4o-mini-tts`** | **~$1.50** | **~$5.00** |
| OpenAI `gpt-4o-mini-tts` | ~$1.10 | ~$3.70 |
| OpenAI `tts-1` | ~$2.70 | ~$9.00 |
| OpenAI `tts-1-hd` | ~$5.40 | ~$18.00 |
| Self-hosted (Kokoro) | **$0** (your GPU) | **$0** |

Token-based models bill audio output at ~$12/M tokens. `tts-1` and `tts-1-hd` bill per character ($15 and $30 per million).

---

## Limitations

- **No character voices** — single narrator for the whole book. If you need distinct voices per character, look at [ElevenLabs](https://elevenlabs.io) or [Orpheus](https://github.com/canopyai/Orpheus-TTS).
- **No SSML** — `gpt-4o-mini-tts` doesn't accept SSML. Use the `instructions` field for prosody hints.
- **EPUB only** — no MOBI, AZW3, or PDF. Convert first with [Calibre](https://calibre-ebook.com) (`ebook-convert input.mobi output.epub`).
- **DRM-locked books won't work** — only DRM-free EPUBs.
- **No resume on crash** — if the run dies mid-book, you have to restart the whole book. (Easiest workaround: rename half-finished books with a `.partial` suffix before re-running.)

---

## Development

```bash
# Lint / type-check
python3 -m py_compile convert_books.py
python3 -c "import ast; ast.parse(open('convert_books.py').read())"

# Run with a single book and DEBUG logging
LOG_LEVEL=DEBUG python3 convert_books.py
```

The script has no test suite yet — contributions welcome. Good first issues:
- Resume support (skip already-rendered chapters)
- EPUB chapter detection improvements (handles nav.xhtml better)
- Configurable pause-between-chapters
- Per-chapter metadata (ID3 tags)

---

## License

MIT. Use it, fork it, ship it.

## Credits

Built by [Kifah](https://github.com/Kifah) — Python + `ffmpeg` + a TTS API. That's it.
