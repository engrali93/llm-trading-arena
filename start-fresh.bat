@echo off
rem Wipes the ledger and starts a brand-new life (all past lives are deleted), in its own console window titled
rem "LLM Trading Arena". Extra flags pass through. Stop it with stop.bat, or press Ctrl+C in that window.
cd /d "%~dp0"
rem Flash attention + a quantized KV cache let the 14b fit fully on a 12 GB GPU (only used if this script starts Ollama).
set OLLAMA_FLASH_ATTENTION=1
set OLLAMA_KV_CACHE_TYPE=q8_0
powershell -NoProfile -Command "try { Invoke-WebRequest http://localhost:11434/api/tags -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting Ollama...
  start "" /min ollama serve
  timeout /t 6 /nobreak >nul
)
start "LLM Trading Arena" cmd /k "title LLM Trading Arena & python arena.py --model deepseek-r1:14b --port 8001 --interval 120 --reset %*"
echo The agent is starting in its own window ("LLM Trading Arena") with a brand-new ledger. Dashboard: http://localhost:8001
echo Stop it with stop.bat.
timeout /t 4 /nobreak >nul
