@echo off
rem Starts Ollama if needed, then the arena. Continues the same life. Extra flags pass through, e.g. start.bat --lan
cd /d "%~dp0"
powershell -NoProfile -Command "try { Invoke-WebRequest http://localhost:11434/api/tags -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting Ollama...
  start "" /min ollama serve
  timeout /t 6 /nobreak >nul
)
python arena.py --model deepseek-r1:8b --port 8001 --interval 120 %*
pause
