@echo off
rem 能用的搜索引擎 · 统一搜索框 —— 双击启动（然后浏览器打开 http://127.0.0.1:18788/）
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONPATH=src
python -m usable_search serve --port 18788
pause
