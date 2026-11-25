@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto trylauncher
python -c "import sys,serial;assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto trylauncher
python -X utf8 workshop.py %*
if errorlevel 1 pause
exit /b
:trylauncher
py -3 -c "import sys,serial;assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto missing
py -3 -X utf8 workshop.py %*
if errorlevel 1 pause
exit /b
:missing
echo Python 3.10+ and pyserial 3.5 are needed. See README.md.
pause
exit /b 1
