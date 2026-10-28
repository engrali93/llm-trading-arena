#!/usr/bin/env bash
# Starts Ollama if needed, then the arena. Continues the same life.
# Extra flags pass through, e.g.  ./start.sh --lan   or   ./start.sh --reset
# Pick another model with:  MODEL=deepseek-r1:1.5b ./start.sh
cd "$(dirname "$0")" || exit 1

if ! curl -fs http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "Starting Ollama..."
  ollama serve >/dev/null 2>&1 &
  sleep 6
fi

exec python3 arena.py --model "${MODEL:-deepseek-r1:8b}" --port 8001 --interval 120 "$@"
