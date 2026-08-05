@echo off
setlocal EnableExtensions
rem ============================================================
rem  Codex Traffic Light MXP (Windows port) - uninstaller
rem  Removes autostart VBS, .cmd shims, and the app folder.
rem  Hook lines in %USERPROFILE%\.codex\config.toml are NOT
rem  touched - remove them manually if desired.
rem ============================================================

set "APP_DIR=%LOCALAPPDATA%\CodexTrafficLight\app"
set "BIN_DIR=%USERPROFILE%\.codex\bin"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

echo [1/3] Removing autostart entry ...
if exist "%STARTUP_DIR%\codex-traffic-light-mxp.vbs" del "%STARTUP_DIR%\codex-traffic-light-mxp.vbs"

echo [2/3] Removing command shims ...
if exist "%BIN_DIR%\codex-light-mxp.cmd" del "%BIN_DIR%\codex-light-mxp.cmd"
if exist "%BIN_DIR%\codex-light-hook-mxp.cmd" del "%BIN_DIR%\codex-light-hook-mxp.cmd"

echo [3/3] Removing app folder ...
if exist "%APP_DIR%" rmdir /S /Q "%APP_DIR%"

echo.
echo Done. State/hook logs under %LOCALAPPDATA%\CodexTrafficLight were kept.
endlocal
