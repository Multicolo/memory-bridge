@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0bridge"
where py >nul 2>nul && (py -3 desktop.py %* & goto :end)
where python >nul 2>nul && (python desktop.py %* & goto :end)
echo 没找到 Python。
pause
:end
