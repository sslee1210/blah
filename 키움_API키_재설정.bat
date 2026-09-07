@echo off
setlocal
chcp 65001 >nul
title Real2 Kiwoom REST API Key Setup
cd /d "%~dp0"
py -3 us_ichimoku_analyzer.py --configure
if errorlevel 1 pause

