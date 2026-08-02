@echo off
REM Launcher for install_adb.py — checks Python is available first.
REM Run this from an Administrator Command Prompt or PowerShell.

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [install_adb] Python was not found on PATH.
    echo [install_adb] Install it from https://www.python.org/downloads/windows/
    echo [install_adb] During setup, make sure to check "Add python.exe to PATH".
    exit /b 1
)

python "%~dp0install_adb.py" %*
