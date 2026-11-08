@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0bridge"
echo 实时同步会一直占着这个窗口，关掉窗口就停。
where py >nul 2>nul && (py -3 realtime.py %* & goto :end)
where python >nul 2>nul && (python realtime.py %* & goto :end)
echo 没找到 Python。
pause
:end
