@echo off
rem Starts the audiofx interface. You can double click this file.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw -m audiofx gui
) else (
    python -m audiofx gui
)
