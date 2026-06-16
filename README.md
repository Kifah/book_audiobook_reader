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

The defaults are tuned for **OpenRouter + `openai/gpt-4o-mini-tts-2025-12-15`** with a **calm British female voice at 1.2× speed**. Tweak to taste — see the file for every knob.

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

### Resume an aborted run (`--continue`)

If a full-book conversion is interrupted (Ctrl-C, crash, network outage, kicked offline), don't restart from scratch — pick up where you left off:

```bash
# Run as normal, get interrupted
python3 convert_books.py
# ... Ctrl-C mid-book ...

# Resume — only renders missing chapters, keeps everything else
python3 convert_books.py --continue
# or equivalently
python3 convert_books.py --resume
```
**What it does (dry-run):**

- Picks the first chapter with real text (skips blank title pages)
- Cuts to roughly `--dry-run-seconds` worth of characters (sentence-aware)
- Sends **1 TTS API call** (cheap — ~$0.001 on OpenRouter, ~$0.05 on ElevenLabs)
- Writes a single MP3 + a `DRY-RUN.md` explaining what it did
- Exits without touching anything else

**Heads-up:** dry-run STILL uses your real TTS key (1 API call). If you want a true "no-cost" preview first, see [Voice examples](#voice-examples) below for raw API snippets you can paste into your terminal.

### Resume an aborted run (`--continue`)

If a full-book conversion is interrupted (Ctrl-C, crash, network outage, kicked offline), don't restart from scratch — pick up where you left off:

```bash
# Run as normal, get interrupted
python3 convert_books.py
# ... Ctrl-C mid-book ...

# Resume — only renders missing chapters, keeps everything else
python3 convert_books.py --continue
# or equivalently
python3 convert_books.py --resume
```

**What it does (`--continue`):**
- Scans `audiobooks/<book>/chapters/` for already-rendered MP3s
- Skips TTS+concat for any chapter with a non-empty MP3 (logs `SKIP 'Title' — already exists (X.X MB)`)
- Rebuilds the full-book MP3 only if at least one chapter was re-rendered
- Mutually exclusive with `--dry-run`

**When NOT to use `--continue`:** if you've edited the EPUB since the abort. The script matches chapter files by index (`01 - <slug>.mp3`), so adding/removing/reordering chapters in the source will leave the script in a confused state. Do a fresh run instead.

---

## Configuration

Every setting lives in `.env` (gitignored). The full list:

| Var | Default | Notes |
|---|---|---|
| `TTS_API_KEY` | *(required)* | Your provider API key |
| `TTS_API_URL` | `https://openrouter.ai/api/v1/audio/speech` | OpenAI-compatible endpoint |
| `TTS_MODEL` | `openai/gpt-4o-mini-tts-2025-12-15` | `openai/tts-1` and `openai/tts-1-hd` also work |
| `TTS_VOICE` | `shimmer` | Any of the 13 voices supported by your model |
| `TTS_SPEED` | `1.2` | 0.25 – 4.0 depending on provider |
| `TTS_FORMAT` | `mp3` | mp3, opus, aac, flac, wav, pcm |
| `TTS_INSTRUCTIONS` | British female calm narration | Only sent to `gpt-4o-mini-tts-2025-12-15` (steerable prosody) |
| `TTS_CHUNK_CHARS` | `4000` | Per-request character cap |
| `TTS_MAX_RETRIES` | `4` | Retries on 429/5xx/network errors |
| `TTS_RETRY_BACKOFF` | `2.0` | Exponential backoff base (seconds) |
| `TTS_REQUEST_TIMEOUT` | `180` | Per-request timeout |
| `INPUT_DIR` | `epubs` | Where to look for `.epub` files |
| `OUTPUT_DIR` | `audiobooks` | Where to write the zipped output |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |

### Voice examples

`gpt-4o-mini-tts-2025-12-15` ships with 13 voices. For audiobooks, common picks:

| Voice | Vibe |
|---|---|
| `shimmer` *(default)* | warm, calm, female — the classic audiobook narrator |
| `nova` | bright, friendly, female — good for YA or fiction |
| `coral` | warm, conversational, female — non-fiction, essays |
| `sage` | thoughtful, calm, androgynous — philosophy, dense prose |
| `ballad` | expressive, narrative — long-form storytelling |
| `onyx` | deep, male, authoritative — thrillers, noir |

**Note on accents:** none of the `gpt-4o-mini-tts-2025-12-15` voices have an explicit "British" tag, but the model's `instructions` field steers prosody *and* accent. The default instructions explicitly request a British female voice. To get an American male voice, for example:

```bash
TTS_VOICE=onyx
TTS_INSTRUCTIONS="Speak in a deep, calm American male voice. Pace the narration naturally."
```

### Three ready-to-use `.env` files (pick one)

The defaults are tuned for **OpenRouter + `openai/gpt-4o-mini-tts-2025-12-15`** with a calm female voice at 1.2× speed. To switch providers, replace the contents of your `.env` with one of the blocks below.

**Option 1 — OpenAI direct (simplest, official, $0.30/M chars)**

```bash
# .env — OpenAI direct
TTS_PROVIDER=openai_compatible
TTS_API_URL=https://api.openai.com/v1/audio/speech
TTS_API_KEY=YOUR_OPENAI_KEY_HERE
TTS_MODEL=gpt-4o-mini-tts
TTS_VOICE=shimmer
TTS_SPEED=1.2
TTS_INSTRUCTIONS="Speak in a calm, warm British female voice. Pace the narration naturally for audiobook listening."
TTS_FORMAT=mp3
TTS_CHUNK_CHARS=4000
TTS_MAX_RETRIES=4
TTS_RETRY_BACKOFF=2.0
TTS_REQUEST_TIMEOUT=180
INPUT_DIR=epubs
OUTPUT_DIR=audiobooks
LOG_LEVEL=INFO
```

Get your key at <https://platform.openai.com/api-keys>. Drop the `openai/` prefix on the model — OpenAI takes bare names.

---

**Option 2 — OpenRouter (cheapest, many model choices, $0.30/M chars on `gpt-4o-mini-tts-2025-12-15`)**

```bash
# .env — OpenRouter
TTS_PROVIDER=openai_compatible
TTS_API_URL=https://openrouter.ai/api/v1/audio/speech
TTS_API_KEY=YOUR_OPENROUTER_KEY_HERE
TTS_MODEL=openai/gpt-4o-mini-tts-2025-12-15
TTS_VOICE=shimmer
TTS_SPEED=1.2
TTS_INSTRUCTIONS="Speak in a calm, warm British female voice. Pace the narration naturally for audiobook listening."
TTS_FORMAT=mp3
TTS_CHUNK_CHARS=4000
TTS_MAX_RETRIES=4
TTS_RETRY_BACKOFF=2.0
TTS_REQUEST_TIMEOUT=180
INPUT_DIR=epubs
OUTPUT_DIR=audiobooks
LOG_LEVEL=INFO
```

Get your key at <https://openrouter.ai/keys> (prepaid credits, ~$1-2 is enough for most books). The model name needs the `provider/` prefix — that's the only difference from Option 1.

**Confirmed-working OpenRouter TTS models** (verified June 2026, all on `/v1/audio/speech`):

| Model | OpenRouter ID | Cost (per 1M input / output tokens) | Voices | Notes |
|---|---|---|---|---|
| **Kokoro 82M** *(cheapest)* | `hexgrad/kokoro-82m` | $0.62 in / **$0 out** | 54 | 8 languages. Effectively free audio. Open-weight, fastest cold-start. |
| **Google Gemini 3.1 Flash TTS** *(recommended)* | `google/gemini-3.1-flash-tts-preview` | $1 in / $20 out | 30+ | 70+ languages, 200+ inline audio tags, 2-speaker support. **Best text normalization of any TTS model** — barely needs `clean_for_tts()`. |
| **OpenAI gpt-4o-mini-tts** *(default)* | `openai/gpt-4o-mini-tts-2025-12-15` | ~$3 in / ~$12 out | 13 | Best cost/quality among OpenAI voices. Steerable prosody via `instructions`. |
| Orpheus 3B | `canopylabs/orpheus-3b-0.1-ft` | $7 in / $0 out | 7 | English-only, natural prosody, expressive. |
| Mistral Voxtral Mini TTS | `mistralai/voxtral-mini-tts-2603` | $16 in / $0 out | varies | Voice cloning, multilingual. |
| xAI Grok Voice TTS 1.0 | `x-ai/grok-voice-tts-1.0` | $15 in / $0 out | 5 | Inline speech tags (pauses, emphasis). |
| Microsoft MAI-Voice-2 | `microsoft/mai-voice-2` | $22 in / $0 out | Azure voices | Expressive SSML styles (cheerful, sad, etc.). |

**Heads-up:** OpenRouter's TTS model list changes often. New TTS models are added monthly. Check the live list at:
- [openrouter.ai/models?modality=text-to-speech](https://openrouter.ai/models?modality=text-to-speech)
- Or query the API: `curl "https://openrouter.ai/api/v1/models?output_modalities=speech"`
- filter: output modality = "speech" → sort by price

**Tip for very long books:** OpenRouter has rate limits per model. If you hit a 429, the script's built-in retry will back off and continue. For >20-hour books, you may want to lower `TTS_CHUNK_CHARS` to 2000 and add a small `time.sleep(0.5)` between requests to stay under the per-second quota.

---

**Option 3 — ElevenLabs (highest quality, $5/M chars)**

ElevenLabs is the gold standard for audiobook-grade TTS. Set `TTS_PROVIDER=elevenlabs` and the script switches to ElevenLabs' native `/v1/text-to-speech/{voice_id}` API (not the OpenAI-compatible path).

```bash
# .env — ElevenLabs
TTS_PROVIDER=elevenlabs
TTS_API_KEY=YOUR_ELEVENLABS_KEY_HERE
TTS_FORMAT=mp3
TTS_CHUNK_CHARS=4000
TTS_MAX_RETRIES=4
TTS_RETRY_BACKOFF=2.0
TTS_REQUEST_TIMEOUT=180
INPUT_DIR=epubs
OUTPUT_DIR=audiobooks
LOG_LEVEL=INFO

# ElevenLabs-specific (ignored when TTS_PROVIDER != "elevenlabs")
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb    # George (British, male, calm)
ELEVENLABS_MODEL_ID=eleven_multilingual_v2  # best quality; use eleven_turbo_v2_5 for ~70% cheaper
ELEVENLABS_STABILITY=0.5
ELEVENLABS_SIMILARITY=0.75
ELEVENLABS_STYLE=0.0
```

Get your key at <https://elevenlabs.io> → Profile → API Keys.

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

**ElevenLabs pricing (2026):** ~$0.18 per 1000 characters on Starter/Pro. A 3-hour book = ~$30-40, a 10-hour book = ~$100-130. The `multilingual_v2` model is the most expensive; `turbo_v2_5` is ~70% cheaper and still very good. Quality is noticeably better than `gpt-4o-mini-tts-2025-12-15` for long-form narration, especially for non-English content.

**Option 4 — Google Cloud TTS via OpenRouter (good text normalization, ~$1/M chars)**

Google's TTS models have **excellent text normalization out of the box** — they handle em-dashes, parentheses, abbreviations, numbers, and quote marks far more gracefully than Kokoro, with very few audible breaths. This is the lowest-friction way to get "professional narrator" quality without paying for ElevenLabs.

The catch: Google doesn't expose an OpenAI-compatible TTS endpoint directly, so we route through [OpenRouter](https://openrouter.ai/google), which gives us Google's `gemini-3.1-flash-tts-preview` model on the same `/v1/audio/speech` schema. The `TTS_PROVIDER=openai_compatible` setting (the default) already supports this — you only need to change `TTS_MODEL`.

```bash
# .env — Google Cloud TTS via OpenRouter
TTS_PROVIDER=openai_compatible
TTS_API_URL=https://openrouter.ai/api/v1/audio/speech
TTS_API_KEY=YOUR_OPENROUTER_KEY_HERE
TTS_MODEL=google/gemini-3.1-flash-tts-preview
TTS_FORMAT=mp3
TTS_CHUNK_CHARS=4000
TTS_MAX_RETRIES=4
TTS_RETRY_BACKOFF=2.0
TTS_REQUEST_TIMEOUT=180
INPUT_DIR=epubs
OUTPUT_DIR=audiobooks
LOG_LEVEL=INFO

# Voice: pass via TTS_VOICE; Gemini TTS supports 30+ voice names.
# See https://ai.google.dev/gemini-api/docs/speech-generation#voice-options
TTS_VOICE=Kore   # calm female, good default for non-fiction
TTS_SPEED=1.2
```

Popular Gemini TTS voices for audiobooks:

| Voice | Vibe |
|---|---|
| `Kore` *(default-ish)* | female, calm, clear — good for non-fiction / academic |
| `Orus` | male, deep, narrative — classic audiobook feel |
| `Aoede` | female, bright, expressive — good for fiction |
| `Charon` | male, British, warm — good for historical / literary |
| `Fenrir` | male, mid-range, neutral — versatile |
| `Puck` | male, energetic — good for younger audiences / dialogue-heavy |

You can list all available voices with:

```bash
curl -s "https://ai.google.dev/gemini-api/docs/speech-generation" | grep -oE '`[A-Z][a-z]+`' | sort -u
```

**Why this is worth trying if you came from Kokoro:**

The `clean_for_tts()` pass still helps (especially the em-dash and parentheses normalization), but Google's TTS engine does most of the heavy lifting itself. If you were at ~4% weird pauses with Kokoro, expect <1% with Gemini TTS — and the `clean_for_tts` regexes become "nice to have" rather than "essential".

**Pricing (2026):** OpenRouter charges ~$1/M output characters for `gemini-3.1-flash-tts-preview`. A 3-hour book = ~$2-3, a 10-hour book = ~$7-10. Substantially cheaper than ElevenLabs, more expensive than Kokoro (which is $0). The quality/price tradeoff sits between OpenAI `gpt-4o-mini-tts` and ElevenLabs `eleven_turbo_v2_5`.

**Want to use Google Cloud TTS directly (not via OpenRouter)?** That requires Google's native REST API (`/v1/text:synthesize`) with OAuth 2.0 or a Google Cloud API key, and isn't OpenAI-compatible. The script doesn't support it natively yet — PRs welcome. For most audiobook use cases, the OpenRouter route is simpler and gives the same underlying model.

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

A 3-hour audiobook (≈ 180 000 chars) on `gpt-4o-mini-tts-2025-12-15`:

| Provider | 3-hour book | 10-hour book |
|---|---|---|
| **OpenRouter `kokoro-82m`** | **~$0.001** (effectively free) | **~$0.005** |
| **OpenRouter `gpt-4o-mini-tts-2025-12-15`** | **~$1.50** | **~$5.00** |
| **OpenRouter `google/gemini-3.1-flash-tts-preview`** | **~$0.20** | **~$0.60** |
| OpenAI `gpt-4o-mini-tts` | ~$1.10 | ~$3.70 |
| OpenAI `tts-1` | ~$2.70 | ~$9.00 |
| OpenAI `tts-1-hd` | ~$5.40 | ~$18.00 |
| ElevenLabs `turbo_v2_5` | ~$30 | ~$100 |
| ElevenLabs `multilingual_v2` | ~$32 | ~$108 |
| Self-hosted (Kokoro) | **$0** (your GPU) | **$0** |

Token-based models bill audio output at ~$12/M tokens. `tts-1` and `tts-1-hd` bill per character ($15 and $30 per million).

**Sweet spot for non-fiction audiobooks:** Google Gemini TTS via OpenRouter. ~$0.20 for a 3-hour book, much better text normalization than Kokoro, no ElevenLabs-tier pricing.

---

## Limitations

- **No character voices on OpenAI-compatible providers** — single narrator for the whole book. For distinct voices per character, use ElevenLabs (which supports voice cloning) or [Orpheus TTS](https://github.com/canopyai/Orpheus-TTS) (self-hosted).
- **No SSML on OpenAI-compatible providers** — `gpt-4o-mini-tts-2025-12-15` doesn't accept SSML. Use the `instructions` field for prosody hints.
- **EPUB only** — no MOBI, AZW3, or PDF. Convert first with [Calibre](https://calibre-ebook.com) (`ebook-convert input.mobi output.epub`).
- **DRM-locked books won't work** — only DRM-free EPUBs.
- **Resume on crash** — use `--continue` (or `--resume`) to pick up where an aborted run left off. Skips already-rendered chapters; only renders the missing ones.
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
- EPUB chapter detection improvements (handles nav.xhtml better)
- Configurable pause-between-chapters
- Per-chapter metadata (ID3 tags)

---

## License

MIT. Use it, fork it, ship it.

## Credits

Built by [Kifah](https://github.com/Kifah) — Python + `ffmpeg` + a TTS API. That's it.
