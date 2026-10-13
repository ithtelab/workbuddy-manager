@echo off
REM WorkBuddy Manager - build script (Windows).
REM Builds exactly ONE exe: "<project root>\WorkBuddy Manager.exe".
REM Console subsystem on purpose: the exe opens the menu when double-clicked.
REM
REM KEEP THIS FILE ASCII-ONLY: cmd.exe parses batch files using the system ANSI
REM codepage (936/GBK on Chinese Windows), so UTF-8 text gets mangled and the
REM mangled fragments are executed as commands.
setlocal
cd /d "%~dp0"

for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"
set "TARGET=%ROOT%\WorkBuddy Manager.exe"

where go >nul 2>nul
if errorlevel 1 (
	echo [ERROR] go.exe not found. Install Go 1.27 or newer, then run this script again.
	exit /b 1
)

if not exist "rsrc.syso" echo [WARN] rsrc.syso is missing - the exe will have no icon.

echo Building "%TARGET%" ...
go build -trimpath -ldflags "-s -w" -o "%TARGET%" .
if errorlevel 1 (
	echo [ERROR] build failed.
	exit /b 1
)

echo Done. Double-click "%TARGET%" to open the console menu.
exit /b 0
