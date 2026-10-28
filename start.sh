#!/usr/bin/env bash
# Starts Ollama if needed, then the arena. Continues the same life.
# Extra flags pass through, e.g.  ./start.sh --lan   or   ./start.sh --reset
# Pick another model with:  MODEL=deepseek-r1:8b ./start.sh   (8 GB GPUs; deepseek-r1:1.5b for less)
cd "$(dirname "$0")" || exit 1

# Flash attention + a quantized KV cache let the 14b fit fully on a 12 GB GPU (only used if this script starts Ollama).
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0

if ! curl -fs http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "Starting Ollama..."
  ollama serve >/dev/null 2>&1 &
  sleep 6
fi

exec python3 arena.py --model "${MODEL:-deepseek-r1:14b}" --port 8001 --interval 120 "$@"
