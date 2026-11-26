@echo off
rem One-time setup: install requirements, create .env, add a desktop shortcut.
cd /d "%~dp0"

set PY=python
where py >nul 2>nul && set PY=py -3

echo Installing requirements...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo pip install failed. Is Python 3 installed and on PATH?
    pause
    exit /b 1
)
rem Remember what was installed, so the start shortcut only reinstalls after an update.
copy /y requirements.txt .requirements-installed >nul

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Created .env - open it and paste your JSearch API key after JSEARCH_API_KEY=
)

rem Safety check before every commit: blocks databases, .env and API keys (the GitHub repo is public).
git config core.hooksPath hooks >nul 2>nul

echo Creating desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\Job Search.lnk\"); $s.TargetPath='%~dp0start-jobsearch.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation=\"$env:SystemRoot\System32\imageres.dll,15\"; $s.Description='Start the Job Search app'; $s.Save()"

echo.
echo Done. Double-click "Job Search" on your desktop to start the app.
pause
