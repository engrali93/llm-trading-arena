@echo off
rem Starts Ollama if needed, then the arena. Continues the same life. Extra flags pass through, e.g. start.bat --lan
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
python arena.py --model deepseek-r1:14b --port 8001 --interval 120 %*
pause
