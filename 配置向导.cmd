@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0bridge"
where py >nul 2>nul && (py -3 setup.py %* & goto :end)
where python >nul 2>nul && (python setup.py %* & goto :end)
echo 没找到 Python。
pause
:end
