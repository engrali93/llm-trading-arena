@echo off
rem Stops the LLM Trading Arena (every python process running arena.py). See stop.ps1 for the details:
rem it asks the agent to stop gracefully and only force-kills it if it is still running after about 10 seconds.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
echo.
pause
