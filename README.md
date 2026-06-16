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
4. **Calls** your TTS endpoint for each chunk — in parallel by default (4 workers), so a 10-hour book finishes in ~25 min instead of ~85
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

I wanted to listen to my ebook library on the go without giving Audible my credit card, and I already had an OpenRouter account. The script started as about 200 lines of Python and has grown to handle a few real-world frictions: ffmpeg chapter joins, sentence-aware chunking, resume after crash, parallel TTS requests, and the rough edges of `clean_for_tts()` text normalization.

It's deliberately **provider-agnostic** — anything that speaks the OpenAI `/v1/audio/speech` schema works:

- [OpenRouter](https://openrouter.ai) (default — one account, many TTS models)
- [OpenAI](https://platform.openai.com) directly
- Self-hosted gateways like [LocalAI](https://localai.io), [Ollama](https://ollama.com) (with a TTS adapter), or [Kokoro](https://github.com/remsky/Kokoro-82M) behind an OpenAI-compatible proxy

---

## Platform support

The script itself is **pure Python + ffmpeg** and runs anywhere Python 3.10+ runs. The constraints come from which **TTS provider** you choose, not from the script. Here's the honest matrix:

| Platform | `openai_compatible` (OpenRouter, OpenAI, etc.) | `elevenlabs` | `mlx_local` |
|---|---|---|---|
| **macOS Apple Silicon** (M1/M2/M3/M4/M5) | ✅ | ✅ | ✅ **best option** |
| **macOS Intel** (pre-2020) | ✅ | ✅ | ❌ MLX requires Apple Silicon |
| **Linux** (x86_64, any distro) | ✅ | ✅ | ❌ MLX is Apple-only |
| **Linux** (ARM64, e.g. Raspberry Pi, Graviton) | ✅ | ✅ | ❌ |
| **Windows** 10/11 (x86_64) | ✅ | ✅ | ❌ |
| **Windows** (ARM64, Surface Pro X etc.) | ✅ | ✅ | ❌ |

**What this means in practice:**

- **Apple Silicon Mac users** get the best deal: any cloud provider works, and MLX local gives you **free, fast** TTS that beats cloud in cost/quality. This is where the project shines.
- **Linux / Windows / Intel Mac users** have a smaller menu but it's still excellent: OpenRouter `gemini-2.5-pro-preview-tts` via OpenAI-compatible is ~$0.20 for a 3-hour book, and runs from any Docker container, headless server, CI runner, or your laptop. The script has no GUI, no Apple-only deps, no Metal calls — it just makes HTTPS requests and shells out to ffmpeg.
- **CI / server / NAS** use cases: install Python + ffmpeg, set `TTS_API_KEY` and `TTS_PROVIDER=openai_compatible`, done. No GPU, no Apple Silicon, no problem.

**For MLX specifically** (Apple Silicon only):

- Requires macOS 13.5+ on M1/M2/M3/M4/M5
- 8 GB RAM minimum, 16 GB recommended for the larger Orpheus model
- Speed scales with chip: M5 is ~3-4s/chapter, M1 is ~10-15s/chapter. See [Option 5](#option-5--local-mlx-tts-on-apple-silicon-free-0m-fast-on-m-series) for the full per-chip table.
- First run downloads the model weights (~500 MB – 2 GB)
- If you try `TTS_PROVIDER=mlx_local` on Linux/Windows/Intel Mac, the script exits with a clear "MLX requires Apple Silicon — use TTS_PROVIDER=openai_compatible instead" error, no stack trace

**For other hardware (no GPU, ARM servers, etc.):** the cloud TTS providers work identically on any platform. The bottleneck is network latency to the provider, not your local hardware.

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

> **Speed:** chapters are rendered with 4 parallel TTS workers by default (`TTS_PARALLEL_CHUNKS=4`), so a typical 10-hour book finishes in ~25 minutes instead of ~85. On Apple Silicon (M1+) you can skip the API entirely and run **fully local** with `TTS_PROVIDER=mlx_local` — see [Local TTS on Apple Silicon (MLX)](#local-tts-on-apple-silicon-mlx) for setup.

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

**What it does (dry-run):**
- Picks the first chapter with real text (skips blank title pages)
- Cuts to roughly `--dry-run-seconds` worth of characters (sentence-aware)
- Sends **1 TTS API call** (cheap — ~$0.001 on OpenRouter, ~$0.05 on ElevenLabs, $0 on MLX local)
- Writes a single MP3 + a `DRY-RUN.md` explaining what it did
- Exits without touching anything else

**Heads-up:** dry-run STILL uses your real TTS key (1 API call) for cloud providers. For `TTS_PROVIDER=mlx_local` it's free.

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
| `TTS_PROVIDER` | `openai_compatible` | `openai_compatible` (default — any OpenAI-speech-compatible endpoint) · `elevenlabs` · `mlx_local` (Apple Silicon only) |
| `TTS_API_KEY` | *(required)* | Your provider API key (not needed for `mlx_local`) |
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
| `TTS_PARALLEL_CHUNKS` | `4` | Concurrent TTS requests per chapter (1 = sequential). 4 is a good default; 8+ may hit rate limits on cloud providers. |
| `MLX_PYTHON` | *(unset)* | Path to Python in your MLX venv — only used when `TTS_PROVIDER=mlx_local` |
| `MLX_MODEL` | `mlx-community/orpheus-tts-0.1-finetune-bf16` | HuggingFace MLX model — only used when `TTS_PROVIDER=mlx_local` |
| `MLX_VOICE` | model-specific | Voice name for the chosen MLX model (e.g. `tara` for Orpheus, `af_bella` for Kokoro) |
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

**Confirmed-working OpenRouter TTS models** (all on `/v1/audio/speech` — check the live list at <https://openrouter.ai/models?modality=text-to-speech> for current prices, as they change often):

| Model | OpenRouter ID | Cost (per 1M chars audio) | Voices | Notes |
|---|---|---|---|---|
| **Kokoro 82M** *(cheapest)* | `hexgrad/kokoro-82m` | ~$0.62 | 54 | 8 languages. Open-weight, fastest cold-start. |
| **Google Gemini 2.5 Pro TTS** | `google/gemini-2.5-pro-preview-tts` | ~$1 | 30+ | 70+ languages, 200+ inline audio tags, 2-speaker support. **Best text normalization of any TTS model** — barely needs `clean_for_tts()`. |
| **Google Gemini Flash TTS** | `google/gemini-2.5-flash-preview-tts` | ~$0.30 | 30+ | Cheaper Gemini, slightly lower quality. |
| **OpenAI gpt-4o-mini-tts** | `openai/gpt-4o-mini-tts-2025-12-15` | ~$3 | 13 | Best cost/quality among OpenAI voices. Steerable prosody via `instructions`. |
| Orpheus 3B | `canopylabs/orpheus-3b-0.1-ft` | ~$7 | 7 | English-only, natural prosody, expressive. |
| Mistral Voxtral Mini TTS | `mistralai/voxtral-mini-tts-2603` | varies | varies | Voice cloning, multilingual. |
| xAI Grok Voice TTS 1.0 | `x-ai/grok-voice-tts-1.0` | ~$15 | 5 | Inline speech tags (pauses, emphasis). |
| Microsoft MAI-Voice-2 | `microsoft/mai-voice-2` | ~$22 | Azure voices | Expressive SSML styles (cheerful, sad, etc.). |

**Heads-up:** OpenRouter's TTS model list and prices change often. New TTS models are added monthly. Check the live list at:
- <https://openrouter.ai/models?modality=text-to-speech>
- Or query the API: `curl "https://openrouter.ai/api/v1/models?output_modalities=speech"` (filter: output modality = "speech", sort by price)

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

**Option 4 — Google Gemini TTS via OpenRouter (best text normalization, ~$1/M chars)**

Google's TTS models have **excellent text normalization out of the box** — they handle em-dashes, parentheses, abbreviations, numbers, and quote marks far more gracefully than Kokoro, with very few audible breaths. This is the lowest-friction way to get "professional narrator" quality without paying for ElevenLabs.

The catch: Google doesn't expose an OpenAI-compatible TTS endpoint directly, so we route through [OpenRouter](https://openrouter.ai/google), which gives us Google's Gemini TTS models on the same `/v1/audio/speech` schema. The `TTS_PROVIDER=openai_compatible` setting (the default) already supports this — you only need to change `TTS_MODEL`.

```bash
# .env — Google Gemini TTS via OpenRouter
TTS_PROVIDER=openai_compatible
TTS_API_URL=https://openrouter.ai/api/v1/audio/speech
TTS_API_KEY=YOUR_OPENROUTER_KEY_HERE
TTS_MODEL=google/gemini-2.5-pro-preview-tts
TTS_FORMAT=mp3
TTS_CHUNK_CHARS=4000
TTS_MAX_RETRIES=4
TTS_RETRY_BACKOFF=2.0
TTS_REQUEST_TIMEOUT=180
INPUT_DIR=epubs
OUTPUT_DIR=audiobooks
LOG_LEVEL=INFO

# Voice: pass via TTS_VOICE; Gemini TTS supports 30+ voice names.
# Full list: https://ai.google.dev/gemini-api/docs/speech-generation#voice-options
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

**Note on audio format:** Gemini TTS only returns PCM (raw 24kHz 16-bit mono). The script handles this automatically — it always requests PCM, then converts to your `TTS_FORMAT` (default `mp3`) locally with ffmpeg. Switching from OpenAI/Kokoro to Gemini requires **no config changes** beyond `TTS_MODEL` and `TTS_VOICE`.

**Pricing:** OpenRouter charges per 1M characters of audio output. See <https://openrouter.ai/models?modality=text-to-speech> for current rates. Rough guide: a 3-hour book ≈ $2-5, a 10-hour book ≈ $7-15. Substantially cheaper than ElevenLabs, more expensive than Kokoro (which is ~$0.001 for a 3-hour book).

**Option 5 — Local MLX TTS on Apple Silicon (free, $0/M, fast on M-series)**

Apple's [MLX framework](https://github.com/ml-explore/mlx) runs TTS models on the M-series GPU/ANE. With the M5, this is now competitive with cloud TTS in both speed and quality — and it's free, with no rate limits.

**Hardware requirements (this is NOT optional):**
- Apple Silicon Mac (M1 / M2 / M3 / M4 / M5)
- Intel Macs and Linux/Windows are **not supported** by MLX
- macOS 13.5+ recommended
- 8 GB RAM minimum (16 GB+ for the larger models)
- First run downloads the model weights (~500MB – 2GB depending on choice)

**Performance depends on your chip. Realistic numbers per 12-chunk chapter:**

| Mac | Approx time per chapter | Notes |
|---|---|---|
| M5 base | ~3-4s | Real-time on GPU/ANE |
| M5 Pro / Max / Ultra | ~2-3s | More GPU cores |
| M4 / M3 | ~5-6s | Real-time capable |
| M2 / M1 | ~8-10s | Real-time capable |
| Older Apple Silicon | ~10-15s | Real-time factor 1-2x |

These are rough — actual speed varies with model size, chunk length, and what else your Mac is doing. The same script works on all Apple Silicon, just slower on older chips.

**Setup (one-time, ~5 minutes):**

```bash
# 1. Create an isolated venv (keep MLX out of your main project venv)
python3 -m venv ~/.venvs/mlx-audio
source ~/.venvs/mlx-audio/bin/activate

# 2. Install mlx-audio
pip install mlx-audio

# 3. Find the venv Python path (you'll need it for .env)
which python
# Example output: /Users/you/.venvs/mlx-audio/bin/python
deactivate
```

**.env configuration:**

```bash
# .env — Local MLX TTS on Apple Silicon
TTS_PROVIDER=mlx_local
TTS_FORMAT=mp3

# Path to the MLX venv Python you just created
MLX_PYTHON=/Users/YOU/.venvs/mlx-audio/bin/python

# Model: pick one. Bigger = better quality, slower.
MLX_MODEL=mlx-community/orpheus-tts-0.1-finetune-bf16   # best quality, ~2GB
# MLX_MODEL=mlx-community/outetts-0.3-500M-bf16        # smaller, faster, ~1GB
# MLX_MODEL=mlx-community/Kokoro-82M-bf16              # 54 voices, 8 languages, ~500MB

# Voice: model-specific. See HuggingFace model page for valid voices.
MLX_VOICE=tara
TTS_SPEED=1.2
```

**Why Orpheus-1B:** best quality-to-size ratio of the MLX community models. Near-ElevenLabs quality for narration, runs at ~1.5x real-time on M5. The other two are smaller alternatives if you want faster or have RAM constraints.

**Try it:**

```bash
# First run: downloads the model (~10-30s) then does inference
python3 convert_books.py --dry-run
# Subsequent runs: just inference, ~3-4s per chapter on M5
```

**Why a separate venv and subprocess:** MLX is a heavy dependency (specific Python version, Metal runtime, model weights). Keeping it in an isolated venv via subprocess means your main `convert_books.py` stays light and works on any platform (Linux, Mac, Windows). On non-Apple-Silicon machines, the script gives a clear "use TTS_PROVIDER=openai_compatible instead" error.

**Cost comparison for a typical ~10-hour book:**

| Provider | Time | Cost | Quality |
|---|---|---|---|
| Gemini via OpenRouter | ~25 min (parallel) | ~$1-2 | Excellent |
| ElevenLabs | ~85 min | ~$30-100 | Best |
| **MLX local on M5** | **~12 min** | **$0** | **Very good** |

**The M5 makes local TTS the obvious choice** for personal audiobooks: free, fast, no rate limits, no API keys. Cloud is still better for one-off professional work where every word matters, but for converting your own library, local is the winner.

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

Prices are per **1M characters of generated audio**. A 3-hour audiobook ≈ 180 000 chars, a 10-hour book ≈ 600 000 chars. These are ballpark figures; check each provider's pricing page for current rates — they change often.

| Provider | 3-hour book | 10-hour book | Quality |
|---|---|---|---|
| **OpenRouter `kokoro-82m`** | **~$0.001** | **~$0.005** | Good (open-weight) |
| **OpenRouter `gemini-2.5-flash-preview-tts`** | **~$0.05** | **~$0.20** | Excellent |
| **OpenRouter `gemini-2.5-pro-preview-tts`** | **~$0.20** | **~$0.60** | Excellent |
| **OpenRouter `gpt-4o-mini-tts-2025-12-15`** | **~$0.50** | **~$1.80** | Very good |
| OpenAI `gpt-4o-mini-tts` | ~$1.10 | ~$3.70 | Very good |
| OpenAI `tts-1` | ~$2.70 | ~$9.00 | Good |
| OpenAI `tts-1-hd` | ~$5.40 | ~$18.00 | Very good |
| ElevenLabs `turbo_v2_5` | ~$30 | ~$100 | Best |
| ElevenLabs `multilingual_v2` | ~$32 | ~$108 | Best (most expensive) |
| **MLX local on Apple Silicon** | **$0** (your Mac) | **$0** | Very good |

**Sweet spot for non-fiction audiobooks:** Google Gemini TTS via OpenRouter. ~$0.20 for a 3-hour book, much better text normalization than Kokoro, no ElevenLabs-tier pricing.

**Sweet spot for cost-is-no-object fiction:** ElevenLabs `turbo_v2_5`. Best long-form narration quality, but $30+ for a 3-hour book.

**Sweet spot for free, fast personal library:** MLX local on M-series Mac. Zero per-book cost after initial setup, ~12-25 min for a 10-hour book depending on your chip.

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
