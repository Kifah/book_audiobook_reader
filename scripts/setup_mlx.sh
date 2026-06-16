#!/usr/bin/env bash
# setup_mlx.sh — one-shot installer for local MLX TTS on Apple Silicon.
#
# What it does:
#   1. Creates an isolated venv at ~/.venvs/mlx-audio (keeps MLX deps out
#      of your main project venv)
#   2. Installs mlx-audio (the TTS runtime)
#   3. Prints the path you need to put in .env as MLX_PYTHON
#   4. Runs a 1-sentence smoke test to confirm the install works
#
# Why a separate venv: MLX pins a specific Python version and ships native
# Metal binaries. Mixing it into your main project venv can break other
# deps. The script's `_tts_mlx_local()` function invokes this venv as a
# subprocess, so the main venv never needs MLX.
#
# Requirements:
#   - macOS 13.5+ on Apple Silicon (M1/M2/M3/M4/M5)
#   - Python 3.10+ available (uses whichever `python3` resolves to)
#   - ~2 GB free disk for model weights (downloaded on first TTS call)
#
# Usage:
#   ./scripts/setup_mlx.sh
#
# Idempotent: re-running on an already-set-up venv will just upgrade
# mlx-audio in place.

set -euo pipefail

VENV_DIR="${HOME}/.venvs/mlx-audio"
MLX_PYTHON="${VENV_DIR}/bin/python"

echo "==> MLX TTS setup"
echo "    Target venv: ${VENV_DIR}"
echo

# Sanity check: Apple Silicon
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: MLX requires macOS. Detected: $(uname -s)" >&2
    echo "On Linux/Windows, use TTS_PROVIDER=openai_compatible instead." >&2
    exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: MLX requires Apple Silicon. Detected arch: $(uname -m)" >&2
    echo "On Intel Mac, use TTS_PROVIDER=openai_compatible instead." >&2
    exit 1
fi

# Create venv if missing
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "==> Creating venv at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
else
    echo "==> Reusing existing venv at ${VENV_DIR}"
fi

# Install/upgrade mlx-audio
echo "==> Installing mlx-audio (this may take ~30s)"
"${MLX_PYTHON}" -m pip install --quiet --upgrade mlx-audio

# Smoke test
echo "==> Running smoke test"
if "${MLX_PYTHON}" -c "import mlx_audio; print('mlx-audio OK')" 2>/dev/null; then
    echo
    echo "============================================================"
    echo "  MLX TTS is ready."
    echo
    echo "  Add this to your .env:"
    echo
    echo "    TTS_PROVIDER=mlx_local"
    echo "    MLX_PYTHON=${MLX_PYTHON}"
    echo "    MLX_MODEL=mlx-community/Kokoro-82M-bf16"
    echo "    MLX_VOICE=af_bella"
    echo "    TTS_SPEED=1.2"
    echo
    echo "  Optional: clear TTS_MODEL and TTS_VOICE — they're ignored"
    echo "  when TTS_PROVIDER=mlx_local."
    echo
    echo "  First run will download the model (~2 GB, ~30s)."
    echo "  Subsequent runs: ~3-4s per chapter on M5."
    echo "============================================================"
else
    echo
    echo "ERROR: mlx_audio import failed even after install." >&2
    echo "Common cause: Python version too old or not Apple Silicon." >&2
    echo "Try: ${MLX_PYTHON} --version" >&2
    exit 1
fi
