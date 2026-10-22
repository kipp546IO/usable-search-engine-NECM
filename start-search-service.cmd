@echo off
rem ============================================================
rem usable-search-engine service watchdog (E1 autostart)
rem - starts the local search service on 127.0.0.1:18788
rem - auto-restarts if the python process exits unexpectedly
rem - exits (no restart) when port is already occupied (server exit 2)
rem - exits when a .stop flag file exists (see stop-search-service.cmd)
rem - NO background crawling: scraping only happens on user searches,
rem   so autostart adds ZERO extra requests to any platform.
rem ============================================================
cd /d "%~dp0"
del "%~dp0.stop" >nul 2>&1
set PYTHONUTF8=1
set PYTHONPATH=src
:loop
if exist "%~dp0.stop" (
  del "%~dp0.stop" >nul 2>&1
  echo [watchdog] stop flag detected - exit.
  exit /b 0
)
if not exist ".venv\Scripts\python.exe" (
  echo [watchdog] .venv python missing - abort.
  exit /b 3
)
".venv\Scripts\python.exe" -m usable_search serve --port 18788
if errorlevel 2 (
  echo [watchdog] port occupied or fatal - exit.
  exit /b 2
)
timeout /t 5 /nobreak >nul
goto loop
