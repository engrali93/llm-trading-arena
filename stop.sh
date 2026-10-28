#!/usr/bin/env bash
# Stops the LLM Trading Arena: every python process whose command line contains arena.py.
# It asks the agent to stop gracefully first (POST /api/shutdown on localhost, or SIGTERM if the dashboard does not
# answer), so a ledger write in progress finishes. It force-kills a process only if it is still running after 10 seconds.

pids=$(pgrep -f 'python[0-9.]*[[:space:]].*arena\.py' || true)

if [ -z "$pids" ]; then
  echo "Agent is not running"
  exit 0
fi

for pid in $pids; do
  args=$(ps -p "$pid" -o args= 2>/dev/null)
  # The dashboard port is on the command line (the last --port wins, like argparse); 8001 is the default.
  port=$(printf '%s' "$args" | grep -o -- '--port[ =][0-9]*' | tail -n 1 | grep -o '[0-9]*$')
  port=${port:-8001}
  started=$(date +%s)

  asked=no
  if command -v curl >/dev/null 2>&1 &&
     curl -fsS -m 5 -X POST -H 'Content-Type: application/json' -d '{}' "http://127.0.0.1:${port}/api/shutdown" >/dev/null 2>&1; then
    asked=yes
  fi
  [ "$asked" = no ] && kill -TERM "$pid" 2>/dev/null   # arena.py handles SIGTERM through the same graceful path

  for _ in $(seq 1 40); do                              # up to 10 s
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done

  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null
    how="was still running after 10 s, so it was force-killed"
  else
    how="stopped gracefully"
  fi
  echo "Stopped arena.py (PID $pid, dashboard port $port): $how ($(( $(date +%s) - started )) s)"
done

echo "Ollama was left running; quit it if you want to free the GPU."
