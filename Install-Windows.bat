@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0native_host\install_windows.ps1"
set "INSTALL_RESULT=%ERRORLEVEL%"
if not "%INSTALL_RESULT%"=="0" (
    echo.
    echo ERROR: Installation failed. Read the PowerShell error above.
) else (
    echo.
    echo Installation verified. Reload/reconnect the extension before downloading.
)
pause
exit /b %INSTALL_RESULT%
