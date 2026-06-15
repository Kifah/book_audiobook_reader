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

### Try a sample first (dry-run)

Before committing to a full book conversion (and burning through a few dollars of TTS credits), render a short sample. `--dry-run` makes the script render only the first ~30s of the first chapter's text and stop. No zip, no full book — just one MP3 in `audiobooks/dry-run/<book>/chapters/`.

```bash
# 30-second sample (default), uses your real TTS key once
python3 convert_books.py --dry-run

# 60-second sample of a specific book
python3 convert_books.py --dry-run --dry-run-seconds 60 --book "The Ancient Cities"

# Just list what's in the input dir, no conversion
python3 convert_books.py --list
```

**What it does:**

- Picks the first chapter with real text (skips blank title pages)
- Cuts to roughly `--dry-run-seconds` worth of characters (sentence-aware)
- Sends **1 TTS API call** (cheap — ~$0.001 on OpenRouter, ~$0.05 on ElevenLabs)
- Writes a single MP3 + a `DRY-RUN.md` explaining what it did
- Exits without touching anything else

**Heads-up:** dry-run STILL uses your real TTS key (1 API call). If you want a true "no-cost" preview first, see [Voice examples](#voice-examples) below for raw API snippets you can paste into your terminal.

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

### Using OpenRouter

[OpenRouter](https://openrouter.ai) routes requests to many providers with one API key. It's the cheapest way to experiment with different TTS models without juggling accounts. The model name needs the `provider/` prefix.

```bash
# .env
TTS_PROVIDER=openai_compatible       # default — keep this
TTS_API_URL=https://openrouter.ai/api/v1/audio/speech
TTS_API_KEY=sk-or-...
TTS_MODEL=openai/gpt-4o-mini-tts     # MUST include the provider prefix on OpenRouter
```

**Confirmed-working OpenRouter TTS models** (verified June 2026, all OpenAI-compatible):

| Model | OpenRouter ID | Cost (per 1M audio output tokens) | Voices | Notes |
|---|---|---|---|---|
| **OpenAI gpt-4o-mini-tts** *(default)* | `openai/gpt-4o-mini-tts` | ~$12 | 13 | Best cost/quality. Steerable prosody via `instructions`. |
| OpenAI tts-1 | `openai/tts-1` | ~$15 / 1M chars | 9 | Classic, no steerable prosody. |
| OpenAI tts-1-hd | `openai/tts-1-hd` | ~$30 / 1M chars | 9 | Highest fidelity, slowest. |
| Google Gemini 2.5 Flash (TTS) | `google/gemini-2.5-flash-preview-tts` | varies | Gemini voice set | Newer, fast, 24 languages. May need to confirm `/audio/speech` support on OpenRouter. |
| Inworld TTS-1 | `inworld/tts-1` | budget | 6+ | Cheap English TTS, OpenAI-compatible per Inworld docs. |
| LMNT | `lmnt/lmnt` | budget | varies | Low-latency English TTS, OpenAI-compatible. |
| Resemble AI | `resemble-ai/chatterbox-turbo` | budget | varies | Open-source-ish model, supports voice cloning. |
| PlayAI TTS | `playai/tts` | budget | several | Conversational English, may need newer OpenRouter schema. |
| Kokoro (local-proxy needed) | — | n/a | — | Self-hosted; not on OpenRouter, but works via LocalAI/Ollama proxy. |

**Heads-up:** OpenRouter's TTS model list changes often. New TTS models are added monthly. Check the live list at:
- [openrouter.ai/models?modality=text-to-speech](https://openrouter.ai/models?modality=text-to-speech)
- filter: modality = "Audio" → sort by price

**Model names that should work but are unconfirmed on OpenRouter as of June 2026:**

| Model | OpenRouter ID (try this) | Why unconfirmed |
|---|---|---|
| Microsoft Azure Speech (neural voices) | `azure/speech` | Not currently in OpenRouter's audio lineup |
| Amazon Polly Neural | `amazon/polly-neural` | Not on OpenRouter |
| Cartesia Sonic | `cartesia/sonic` | Listed but no `/audio/speech` route confirmed |
| Hume Octave / Voice | `hume/octave` | Hume has its own API, not OpenAI-compatible |
| Speechify | `speechify/stream` | Listed but pricing unclear |

If you find a new TTS provider on OpenRouter that exposes the OpenAI `/v1/audio/speech` schema, it should work — just set `TTS_MODEL` accordingly.

**Tip for very long books:** OpenRouter has rate limits per model. If you hit a 429, the script's built-in retry will back off and continue. For >20-hour books, you may want to lower `TTS_CHUNK_CHARS` to 2000 and add a small `time.sleep(0.5)` between requests to stay under the per-second quota.

### Using ElevenLabs (highest quality)

ElevenLabs is the gold standard for audiobook-grade TTS. Set `TTS_PROVIDER=elevenlabs` and the script switches to ElevenLabs' native `/v1/text-to-speech/{voice_id}` API (not the OpenAI-compatible path).

```bash
# .env
TTS_PROVIDER=elevenlabs
TTS_API_KEY=sk_***  # from https://elevenlabs.io → Profile → API Keys
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb   # George (British, male, calm)
ELEVENLABS_MODEL_ID=eleven_multilingual_v2  # best quality
ELEVENLABS_STABILITY=0.5
ELEVENLABS_SIMILARITY=0.75
ELEVENLABS_STYLE=0.0
```

Popular voice IDs for audiobooks:

| Voice | ID | Vibe |
|---|---|---|
| **George** *(default)* | `JBFqnCBsd6RMkjVDRZzb` | male, British, calm — the classic audiobook narrator |
| Rachel | `21m00Tcm4TlvDq8ikWAM` | female, American, calm |
| Domi | `AZnzlk1XvdvUeBnXmlld` | female, American, energetic |
| Antoni | `ErXwobaYiN019PkySvjV` | male, American, warm |
| Josh | `TxGEqnHWrfWFTfGW9XjX` | male, American, deep |
| Adam | `pNInz6obpgDQGcFmaJgB` | male, American, narrative (great for non-fiction) |
| Bella | `EXAVITQu4vr4xnSDxMaL` | female, American, calm |

**Note on speed:** ElevenLabs' API doesn't accept a `speed` parameter. The `TTS_SPEED` env var is ignored when `TTS_PROVIDER=elevenlabs`. If you want 1.2× playback, post-process the MP3s with ffmpeg:

```bash
# 1.2x speed-up after conversion
ffmpeg -i input.mp3 -filter:a "atempo=1.2" output.mp3
```

The script does not do this automatically. PRs welcome.

**ElevenLabs pricing (2026):** ~$0.18 per 1000 characters on Starter/Pro. A 3-hour book = ~$30-40, a 10-hour book = ~$100-130. The `multilingual_v2` model is the most expensive; `turbo_v2_5` is ~70% cheaper and still very good. Quality is noticeably better than `gpt-4o-mini-tts` for long-form narration, especially for non-English content.

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

- **No character voices on OpenAI-compatible providers** — single narrator for the whole book. For distinct voices per character, use ElevenLabs (which supports voice cloning) or [Orpheus TTS](https://github.com/canopyai/Orpheus-TTS) (self-hosted).
- **No SSML on OpenAI-compatible providers** — `gpt-4o-mini-tts` doesn't accept SSML. Use the `instructions` field for prosody hints.
- **EPUB only** — no MOBI, AZW3, or PDF. Convert first with [Calibre](https://calibre-ebook.com) (`ebook-convert input.mobi output.epub`).
- **DRM-locked books won't work** — only DRM-free EPUBs.
- **No resume on crash** — if the run dies mid-book, you have to restart the whole book. (Easiest workaround: rename half-finished books with a `.partial` suffix before re-running.)
- **ElevenLabs speed control is post-process only** — ElevenLabs' API doesn't accept a `speed` parameter. See the ElevenLabs section for the ffmpeg workaround.

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
