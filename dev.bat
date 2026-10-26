@echo off
rem Double-click entry point. The actual launcher lives in scripts\dev.ps1
rem so Chinese output is not broken by cmd.exe's batch-file encoding rules.
chcp 65001 >nul
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
endlocal & exit /b %CODE%
