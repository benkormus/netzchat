@echo off
chcp 65001 >nul
title Messenger
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
    echo Node.js wurde nicht gefunden. Bitte installieren: https://nodejs.org  ^(Version 20 oder neuer^)
    echo.
    pause
    exit /b 1
)
node client.js %*
echo.
pause
