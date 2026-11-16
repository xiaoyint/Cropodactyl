@echo off
rem Launch VideoCrop 16:10 on Windows.   Usage: run.bat [INPUT_DIR] [-o OUTPUT_DIR]
rem Creates a virtual environment in .venv on first run, and (re)installs the
rem requirements whenever requirements.txt changes.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" goto :error
)
set "NEED_INSTALL="
if not exist ".venv\.requirements-installed" set NEED_INSTALL=1
if not defined NEED_INSTALL (
    for /f %%i in ('".venv\Scripts\python.exe" -c "import os;print(int(os.path.getmtime('requirements.txt')>os.path.getmtime('.venv/.requirements-installed')))"') do if "%%i"=="1" set NEED_INSTALL=1
)
if defined NEED_INSTALL (
    echo Installing requirements...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
    type nul > ".venv\.requirements-installed"
)
".venv\Scripts\python.exe" -m videocrop %*
exit /b %errorlevel%

:error
echo.
echo Setup failed. Is Python 3.10+ installed and on PATH? See README.md.
pause
exit /b 1
