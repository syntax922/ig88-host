#!/bin/zsh
set -euo pipefail

# mlx-audio TTS server (Kokoro / Qwen3-TTS via MLX) for TC-14 sleep narrator.
# Binds localhost only; Caddy :8880 fronts it with the standard remote_ip gate.
# venv is uv-managed (brew pythons are dyld-broken against macOS 26.2 libexpat).

export HOME=/Users/copilot
VENV="$HOME/mlx-audio-venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "mlx-audio venv not found at $VENV" >&2
  exit 1
fi

exec "$VENV/bin/python" -m mlx_audio.server --host 127.0.0.1 --port 18880
