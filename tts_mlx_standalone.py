#!/usr/bin/env python3
"""
MLX TTS standalone inference script for book_audiobook_reader.

This is a thin wrapper around mlx-audio. It lives as a separate file so
the heavy MLX dependency (mlx, mlx-audio, transformers, etc.) can be
installed in an isolated venv. The main convert_books.py invokes this
script as a subprocess and reads back the generated audio.

⚠️  Hardware requirements:
    - Apple Silicon Mac (M1/M2/M3/M4/M5) ONLY.
    - Intel Macs and Linux/Windows are not supported by MLX.
    - On non-Apple-Silicon machines, use TTS_PROVIDER=openai_compatible
      in convert_books.py instead.

⏱️  Performance depends heavily on which Apple Silicon chip you have:
    - M5 base:           ~3-4s per 12-chunk chapter
    - M5 Pro/Max/Ultra:  ~2-3s
    - M4 / M3:           ~5-6s
    - M2 / M1:           ~8-10s
    - Older Apple Silicon: works, but slower (real-time factor ~1-2x)
    Older chips are still usable, just slower than the M5. We don't
    promise exact speeds — your mileage will vary based on:
      - Model size (Orpheus-1b vs Kokoro-82M differ ~10x)
      - Chunk length (longer chunks = more compute per request)
      - Other GPU workloads running concurrently
      - macOS power management settings (low-power mode = slower)

First-run model download: 355MB for Kokoro-82M, larger for the bigger models.
Models are cached in ~/.cache/huggingface/ after first run.

Known-good models (verified to exist on HuggingFace):
  - mlx-community/Kokoro-82M-bf16   (355 MB, 54 voices, 8 languages, RECOMMENDED)
  - mlx-community/Spark-TTS-0.5B-bf16  (~1 GB, English + Chinese)
  - mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16  (~3.5 GB, high quality)

Voice names are model-specific:
  - Kokoro-82M:   af_bella, am_adam, bf_emma, etc. (see model card)
  - Spark-TTS:    no voice arg needed, just text
  - Qwen3-TTS:    voice names per the model card

Usage:
    python3 tts_mlx_standalone.py \\
        --text "Hello, world" \\
        --model mlx-community/Kokoro-82M-bf16 \\
        --voice af_bella \\
        --speed 1.0

Output:
    Prints the path to a temporary .pcm file on stdout. The PCM is
    24kHz, 16-bit signed little-endian, mono — same format as Gemini
    TTS, so convert_books.py can concatenate it without re-encoding.
    Any error messages go to stderr (exit code != 0 on failure).
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MLX TTS inference for book_audiobook_reader",
    )
    parser.add_argument(
        "--text", required=True,
        help="Text to synthesize",
    )
    parser.add_argument(
        "--model", default="mlx-community/Kokoro-82M-bf16",
        help="HuggingFace model ID or local path. Default: Kokoro-82M (smallest, fastest, real).",
    )
    parser.add_argument(
        "--voice", default="af_bella",
        help="Voice name (model-specific). Default: af_bella (Kokoro American female).",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Speech speed multiplier (0.5-2.0). Default: 1.0",
    )
    parser.add_argument(
        "--lang", default="a",
        help="Language code (Kokoro only). Default: 'a' (American English). "
             "Other options: 'b' (British), 'e' (Spanish), 'f' (French), "
             "'h' (Hindi), 'i' (Italian), 'p' (Portuguese), 'j' (Japanese), "
             "'z' (Mandarin). Ignored by non-Kokoro models.",
    )
    parser.add_argument(
        "--temp-dir", default=None,
        help="Where to write the temporary PCM file. Default: system temp",
    )
    args = parser.parse_args()

    # Lazy import — only fails when actually called, not on module load.
    # This lets the script be importable for --help without MLX installed.
    try:
        from mlx_audio.tts import load
    except ImportError as e:
        print(
            f"ERROR: mlx-audio not installed: {e}\n"
            f"Install with: pip install mlx-audio\n"
            f"(Inside an isolated venv is recommended — see README)",
            file=sys.stderr,
        )
        return 2

    # MLX requires Apple Silicon. Catch the runtime error early with a
    # clear message so the user knows to switch to openai_compatible
    # on non-Apple-Silicon machines.
    try:
        import mlx.core as mx  # noqa: F401  (import check only)
    except ImportError:
        print(
            "ERROR: mlx not installed. Install with: pip install mlx",
            file=sys.stderr,
        )
        return 2
    except Exception as e:
        # MLX raises a clear error on Intel Macs / Linux
        print(
            f"ERROR: MLX requires Apple Silicon (M1/M2/M3/M4/M5).\n"
            f"  {type(e).__name__}: {e}\n"
            f"  If you're on an Intel Mac or Linux/Windows, this won't work.\n"
            f"  Use TTS_PROVIDER=openai_compatible in convert_books.py instead.",
            file=sys.stderr,
        )
        return 3

    # First-run model load (slow: ~10-30s for 1B model, then cached)
    # Speed depends on network bandwidth for download and disk speed for caching.
    try:
        model = load(args.model)
    except Exception as e:
        print(
            f"ERROR: failed to load model {args.model!r}: {e}\n"
            f"  Check the model name is correct (see https://huggingface.co/mlx-community)\n"
            f"  And that you have enough disk space (~2-5GB for 1B models)",
            file=sys.stderr,
        )
        return 4

    # Generate audio
    # NOTE: mlx-audio's generate() signature varies by model. Kokoro takes
    # (text, voice, speed, lang_code), Orpheus takes (text, voice, speed),
    # Spark-TTS takes just (text). We pass voice and speed; if the model
    # doesn't accept voice, we fall back to a no-voice call.
    #
    # CRITICAL Kokoro note (mlx-audio 0.2.10): KokoroPipeline's ALIASES
    # dict does not include "en", so the default lang_code="en" triggers:
    #   AssertionError: ('en', {'a': 'American English', 'b': 'British English', ...})
    # We pass lang_code="a" (American English) explicitly. See:
    # https://github.com/Blaizzy/mlx-audio/issues/378
    def _generate(text, **kwargs):
        """Call model.generate with voice+speed+lang_code, fall back to text-only."""
        for attempt in [
            {"voice": args.voice, "speed": args.speed, "lang_code": args.lang},
            {"voice": args.voice, "speed": args.speed},
            {"lang_code": args.lang},
            {},
        ]:
            try:
                return model.generate(text, **attempt)
            except TypeError as e:
                # Some kwarg wasn't accepted. Try the next attempt.
                continue
        # Final fallback: just text
        return model.generate(text)

    try:
        results = _generate(args.text)
    except Exception as e:
        import traceback
        print(
            f"ERROR: generation failed: {type(e).__name__}: {e}\n"
            f"  Model: {args.model}\n"
            f"  Voice: {args.voice!r}\n"
            f"  Text length: {len(args.text)} chars\n"
            f"  The voice name may not be valid for this model.\n"
            f"  Check the model's HuggingFace page for supported voices.\n"
            f"\n"
            f"  Full traceback:\n{traceback.format_exc()}",
            file=sys.stderr,
        )
        return 5

    if not results:
        print("ERROR: model.generate() returned no results", file=sys.stderr)
        return 6

    # model.generate() returns an iterator/generator, not a list.
    # mlx-audio's own generate_audio() does `for i, result in enumerate(results):`
    # so we follow that pattern: get the first result via next().
    try:
        first = next(iter(results))
    except StopIteration:
        print("ERROR: model.generate() iterator was empty", file=sys.stderr)
        return 6

    result = first
    audio = result.audio
    sample_rate = getattr(result, "sample_rate", 24000)

    # Convert numpy array to raw 16-bit signed little-endian PCM.
    # Standardize on 24kHz mono for the on-disk format (matches what
    # Gemini returns, so convert_books.py can concat seamlessly).
    try:
        import numpy as np
    except ImportError:
        print("ERROR: numpy not installed (mlx-audio should bring it)", file=sys.stderr)
        return 7

    # Resample to 24kHz if needed (mlx-audio models may return 22kHz or 32kHz)
    if sample_rate != 24000:
        # Simple linear resample. For better quality, scipy.signal.resample,
        # but we don't want a hard dep on scipy. Linear is fine for speech.
        ratio = 24000 / sample_rate
        new_length = int(len(audio) * ratio)
        audio = np.interp(
            np.linspace(0, len(audio), new_length),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)
        sample_rate = 24000

    # Mono: average channels if stereo
    if audio.ndim > 1:
        audio = audio.mean(axis=-1)

    # Convert float32 [-1, 1] to int16 PCM
    audio_int16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    pcm_bytes = audio_int16.tobytes()

    # Write to temp file. Path printed on stdout for the parent script.
    fd, pcm_path = tempfile.mkstemp(
        suffix=".pcm",
        prefix="mlx_tts_",
        dir=args.temp_dir,
    )
    try:
        os.write(fd, pcm_bytes)
    finally:
        os.close(fd)

    # On success, print the output path to stdout. Parent script reads this.
    print(pcm_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
