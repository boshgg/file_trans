@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -B lan_file_hub.py
) else (
  python -B lan_file_hub.py
)
pause
