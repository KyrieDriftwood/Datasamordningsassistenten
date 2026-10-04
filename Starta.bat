@echo off
rem Startar DatasamordningsAssistent utan konsolfönster.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Virtuell miljo saknas: .venv\Scripts\pythonw.exe
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m app.main
