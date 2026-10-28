@echo off
rem Wipes the ledger and starts a brand-new life (all past lives are deleted). Extra flags pass through.
cd /d "%~dp0"
powershell -NoProfile -Command "try { Invoke-WebRequest http://localhost:11434/api/tags -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting Ollama...
  start "" /min ollama serve
  timeout /t 6 /nobreak >nul
)
python arena.py --model deepseek-r1:8b --port 8001 --interval 120 --reset %*
pause
