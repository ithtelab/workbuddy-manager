@echo off
REM WorkBuddy Manager - double-click to start (Windows).
REM Runs in the foreground; closing this window stops the service.
REM
REM KEEP THIS FILE ASCII-ONLY: cmd.exe parses batch files using the system ANSI
REM codepage (936/GBK on Chinese Windows), so UTF-8 comments get mangled and the
REM mangled fragments are executed as commands - the user sees spurious
REM "not recognized as an internal or external command" errors on startup.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
