@echo off
rem stop the local search service (port 18788) and its watchdog
cd /d "%~dp0"
echo stop > "%~dp0.stop"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":18788" ^| findstr /C:"LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1
)
echo [stop] stop flag set; watchdog will exit on next cycle.
