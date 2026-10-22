@echo off
chcp 936 >nul
cd /d "%~dp0"
set PORT=18788
set PYTHONUTF8=1
set PYTHONPATH=src

echo ==========================================
echo   能用的搜索引擎 · 一键启动
echo ==========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] 没有找到 Python。
  echo.
  echo     请先安装 Python 3.10 或更高版本：
  echo       https://www.python.org/downloads/
  echo     安装时勾选 "Add python.exe to PATH"，装完重新双击本文件。
  echo.
  pause
  exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo [X] Python 版本太旧，需要 3.10 或更高。
  python -V
  echo.
  pause
  exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python -V 2^>^&1') do set "PYVER=%%v"
echo [1/2] Python %PYVER% 已就绪（本项目不装任何第三方库）
echo [2/2] 正在启动本地服务： http://127.0.0.1:%PORT%/
echo.
echo      浏览器会自动打开。这个黑窗口要留着，关掉它 = 关掉服务。
echo      想停止：关掉窗口，或双击 stop-search-service.cmd。
echo.

start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:%PORT%/"
python -m usable_search serve --port %PORT%
pause
