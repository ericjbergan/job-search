@echo off
title Job Search Server
cd /d "%~dp0"

rem If the server is already running, just open the browser
netstat -ano | findstr /r /c:":5001 .*LISTENING" >nul
if %errorlevel%==0 (
    start "" http://localhost:5001
    exit /b
)

rem Use the Python launcher if present, otherwise whatever python is on PATH
set PY=python
where py >nul 2>nul && set PY=py -3

rem After pulling an update that adds a package, install it before starting
rem (only when requirements.txt changed since the last install, so normal starts stay quick).
fc /b requirements.txt .requirements-installed >nul 2>nul
if errorlevel 1 (
    echo Installing updated requirements...
    %PY% -m pip install -q --disable-pip-version-check -r requirements.txt && copy /y requirements.txt .requirements-installed >nul
)

rem Open the browser once the server has had a moment to start
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start "" http://localhost:5001"

echo Starting Job Search at http://localhost:5001
echo Close this window to stop the server.
echo.
%PY% app.py
pause
