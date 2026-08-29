@echo off
REM LLM Gateway — Windows CMD / Double-Click Launcher
REM Delegates execution to start-gateway.ps1 with execution policy bypass.

title LLM Gateway Launcher

echo Starting LLM Gateway...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-gateway.ps1" %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Launch encountered an error.
    pause
)
