@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0bridge"
where py >nul 2>nul && (py -3 app.py %* & goto :end)
where python >nul 2>nul && (python app.py %* & goto :end)
echo 没找到 Python。装一个 3.10+ 的版本再来，安装时记得勾选 Add to PATH。
pause
:end
