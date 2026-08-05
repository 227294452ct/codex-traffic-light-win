@echo off
setlocal EnableExtensions
rem ============================================================
rem  Codex Traffic Light MXP (Windows port) - installer
rem  Installs to %LOCALAPPDATA%\CodexTrafficLight\app
rem  Creates .cmd shims in %USERPROFILE%\.codex\bin
rem  Generates hooks config + autostart VBS (GUI + Hermes watcher)
rem ============================================================

set "PORT_DIR=%~dp0"
set "APP_DIR=%LOCALAPPDATA%\CodexTrafficLight\app"
set "BIN_DIR=%USERPROFILE%\.codex\bin"
set "DATA_DIR=%LOCALAPPDATA%\CodexTrafficLight"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

echo [1/5] Copying app files to %APP_DIR% ...
if not exist "%APP_DIR%" mkdir "%APP_DIR%"
xcopy /E /I /Y "%PORT_DIR%codex_light" "%APP_DIR%\codex_light" >nul
copy /Y "%PORT_DIR%codex-light-mxp.py" "%APP_DIR%\" >nul
copy /Y "%PORT_DIR%codex-light-hook-mxp.py" "%APP_DIR%\" >nul
copy /Y "%PORT_DIR%run_app.pyw" "%APP_DIR%\" >nul
copy /Y "%PORT_DIR%hermes-watcher.py" "%APP_DIR%\" >nul
copy /Y "%PORT_DIR%README.md" "%APP_DIR%\" >nul

echo [2/5] Creating command shims in %BIN_DIR% ...
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"
> "%BIN_DIR%\codex-light-mxp.cmd" echo @echo off
>> "%BIN_DIR%\codex-light-mxp.cmd" echo @python "%APP_DIR%\codex-light-mxp.py" %%*
> "%BIN_DIR%\codex-light-hook-mxp.cmd" echo @echo off
>> "%BIN_DIR%\codex-light-hook-mxp.cmd" echo @python "%APP_DIR%\codex-light-hook-mxp.py" %%*

echo [2b/5] Adding %BIN_DIR% to user PATH ...
powershell -NoProfile -Command "$p=[Environment]::GetEnvironmentVariable('Path','User'); if ($p -notlike '*\.codex\bin*') { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';%BIN_DIR%'), 'User'); Write-Host 'PATH updated' } else { Write-Host 'PATH already contains .codex\bin' }"

echo [3/5] Detecting pythonw for autostart ...
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW set "PYW=pythonw.exe"
echo    using %PYW%

echo [4/5] Creating autostart entries in Startup folder ...
> "%STARTUP_DIR%\codex-traffic-light-mxp.vbs" echo Set ws = CreateObject("WScript.Shell")
>> "%STARTUP_DIR%\codex-traffic-light-mxp.vbs" echo ws.Run ^"%PYW%^" ^"%APP_DIR%\run_app.pyw^", 0, False
>> "%STARTUP_DIR%\codex-traffic-light-mxp.vbs" echo ws.Run ^"%PYW%^" ^"%APP_DIR%\hermes-watcher.py^", 0, False

echo [5/5] Generating hooks config: %DATA_DIR%\hooks.win.generated.toml ...
> "%DATA_DIR%\hooks.win.generated.toml" echo # Codex Traffic Light MXP - Windows hooks config
>> "%DATA_DIR%\hooks.win.generated.toml" echo # Merge the [hooks] block below into %USERPROFILE%\.codex\config.toml
>> "%DATA_DIR%\hooks.win.generated.toml" echo # then run /hooks in Codex and trust the commands.
>> "%DATA_DIR%\hooks.win.generated.toml" echo.
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.UserPromptSubmit]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.UserPromptSubmit.hooks]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo type = "command"
>> "%DATA_DIR%\hooks.win.generated.toml" echo command = "%BIN_DIR:\=/%/codex-light-hook-mxp.cmd UserPromptSubmit"
>> "%DATA_DIR%\hooks.win.generated.toml" echo timeout = 5
>> "%DATA_DIR%\hooks.win.generated.toml" echo statusMessage = "Codex traffic light: working"
>> "%DATA_DIR%\hooks.win.generated.toml" echo.
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.PreToolUse]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo matcher = ".*"
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.PreToolUse.hooks]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo type = "command"
>> "%DATA_DIR%\hooks.win.generated.toml" echo command = "%BIN_DIR:\=/%/codex-light-hook-mxp.cmd PreToolUse"
>> "%DATA_DIR%\hooks.win.generated.toml" echo timeout = 5
>> "%DATA_DIR%\hooks.win.generated.toml" echo statusMessage = "Codex traffic light: working"
>> "%DATA_DIR%\hooks.win.generated.toml" echo.
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.PermissionRequest]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo matcher = ".*"
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.PermissionRequest.hooks]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo type = "command"
>> "%DATA_DIR%\hooks.win.generated.toml" echo command = "%BIN_DIR:\=/%/codex-light-hook-mxp.cmd PermissionRequest"
>> "%DATA_DIR%\hooks.win.generated.toml" echo timeout = 5
>> "%DATA_DIR%\hooks.win.generated.toml" echo statusMessage = "Codex traffic light: waiting"
>> "%DATA_DIR%\hooks.win.generated.toml" echo.
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.Stop]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.Stop.hooks]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo type = "command"
>> "%DATA_DIR%\hooks.win.generated.toml" echo command = "%BIN_DIR:\=/%/codex-light-hook-mxp.cmd Stop"
>> "%DATA_DIR%\hooks.win.generated.toml" echo timeout = 5
>> "%DATA_DIR%\hooks.win.generated.toml" echo statusMessage = "Codex traffic light: done"
>> "%DATA_DIR%\hooks.win.generated.toml" echo.
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.SubagentStop]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo matcher = ".*"
>> "%DATA_DIR%\hooks.win.generated.toml" echo [[hooks.SubagentStop.hooks]]
>> "%DATA_DIR%\hooks.win.generated.toml" echo type = "command"
>> "%DATA_DIR%\hooks.win.generated.toml" echo command = "%BIN_DIR:\=/%/codex-light-hook-mxp.cmd SubagentStop"
>> "%DATA_DIR%\hooks.win.generated.toml" echo timeout = 5
>> "%DATA_DIR%\hooks.win.generated.toml" echo statusMessage = "Codex traffic light: subagent done"

echo.
echo ============================================================
echo  Installed. Next steps:
echo   1. Start the light:  pythonw "%APP_DIR%\run_app.pyw"
echo      (autostart VBS is already in your Startup folder)
echo   2. Merge hooks into config:
echo        copy "%DATA_DIR%\hooks.win.generated.toml" %USERPROFILE%\.codex\config.toml
echo      (or append the [hooks] block manually, then run /hooks)
echo   3. Test:  codex-light-mxp working ^&^& codex-light-mxp status
echo ============================================================
endlocal
