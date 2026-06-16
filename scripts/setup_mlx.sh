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
if ! "${MLX_PYTHON}" -c "import mlx_audio; print('mlx-audio OK')" 2>/dev/null; then
    echo
    echo "ERROR: mlx_audio import failed even after install." >&2
    echo "Common cause: Python version too old or not Apple Silicon." >&2
    echo "Try: ${MLX_PYTHON} --version" >&2
    exit 1
fi
echo "    mlx-audio OK"

# ── Auto-fix .env ─────────────────────────────────────────────────────────
# Migrates the .env in the project that called this script. Fixes:
#   - missing .env (creates one from .env.example + MLX block)
#   - old/broken MLX_MODEL id (orpheus-tts-0.1-finetune-bf16 → Kokoro-82M-bf16)
#   - old/broken MLX_VOICE (tara → af_bella)
#   - missing MLX_PYTHON (sets it to this venv)
#   - missing TTS_PROVIDER=mlx_local (sets it)
# Idempotent: only changes lines that are wrong or missing.
ENV_FILE=""
for candidate in .env ../.env ../../.env; do
    if [[ -f "${candidate}" ]]; then
        ENV_FILE="$(cd "$(dirname "${candidate}")" && pwd)/.env"
        break
    fi
done

if [[ -z "${ENV_FILE}" && -f .env.example ]]; then
    cp .env.example .env
    ENV_FILE="$(pwd)/.env"
    echo "==> Created .env from .env.example"
fi

if [[ -n "${ENV_FILE}" ]]; then
    echo "==> Checking ${ENV_FILE}"
    changed=0

    set_env_var() {
        local key="$1" value="$2"
        if grep -qE "^${key}=" "${ENV_FILE}"; then
            # Update existing line if value differs
            if grep -qE "^${key}=${value}$" "${ENV_FILE}"; then
                : # already correct, no-op
            else
                # Detect current value to log it (strip CR in case Windows-edited)
                local current
                current=$(grep -E "^${key}=" "${ENV_FILE}" | head -1 | cut -d= -f2- | tr -d '\r')
                # Use python for portable in-place sed (works on BSD/macOS
                # and GNU/Linux identically; BSD sed -i '' syntax differs).
                "${MLX_PYTHON:-python3}" - "${ENV_FILE}" "${key}" "${value}" <<'PYEOF'
import sys, pathlib
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
lines = p.read_text().splitlines(keepends=True)
found = False
for i, line in enumerate(lines):
    if line.startswith(key + "="):
        lines[i] = f"{key}={value}\n"
        found = True
        break
if not found:
    # Add with leading newline if file doesn't end in one
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    lines.append(f"{key}={value}\n")
p.write_text("".join(lines))
PYEOF
                echo "    fixed: ${key}=${current}  →  ${key}=${value}"
                changed=1
            fi
        else
            # Append
            echo "${key}=${value}" >> "${ENV_FILE}"
            echo "    added: ${key}=${value}"
            changed=1
        fi
    }

    set_env_var TTS_PROVIDER "mlx_local"
    set_env_var MLX_PYTHON   "${MLX_PYTHON}"
    set_env_var MLX_MODEL    "mlx-community/Kokoro-82M-bf16"
    set_env_var MLX_VOICE    "af_bella"
    set_env_var TTS_SPEED    "1.2"

    if [[ ${changed} -eq 0 ]]; then
        echo "    .env already correct — no changes"
    else
        echo "    .env updated"
    fi
else
    echo "==> No .env found — skipping env migration"
    echo "    Create one with: cp .env.example .env  (then re-run this script)"
fi

echo
echo "============================================================"
echo "  MLX TTS is ready."
echo
echo "  .env should now contain:"
echo "    TTS_PROVIDER=mlx_local"
echo "    MLX_PYTHON=${MLX_PYTHON}"
echo "    MLX_MODEL=mlx-community/Kokoro-82M-bf16"
echo "    MLX_VOICE=af_bella"
echo "    TTS_SPEED=1.2"
echo
echo "  First TTS call will download the model (~355 MB, ~30s)."
echo "  Subsequent runs: ~3-4s per chapter on M5."
echo
echo "  Run a sample now:"
echo "    python3 convert_books.py --dry-run"
echo "============================================================"
