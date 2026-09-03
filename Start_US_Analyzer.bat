@echo off
setlocal
chcp 65001 >nul
title Real2 US Ichimoku Analyzer
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher was not found. Install Python 3.11 or newer.
    pause
    exit /b 1
)

py -3 -c "import pandas, numpy, requests" >nul 2>nul
if errorlevel 1 (
    echo Installing required Python packages...
    py -3 -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Package installation failed.
        pause
        exit /b 1
    )
)

py -3 us_ichimoku_analyzer.py %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Analyzer exited with error code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
