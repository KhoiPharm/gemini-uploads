@echo off
setlocal
cd /d "%~dp0"

echo.
echo Google Drive workspace setup
echo ----------------------------
echo.
echo A folder picker will open.
echo Select the "Pharmacy AI Workspace" folder inside Google Drive for Desktop.
echo Your existing local Gemini folders will NOT be removed or replaced.
echo.

set "DRIVE_WORKSPACE="
for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -STA -Command "$shell = New-Object -ComObject Shell.Application; $folder = $shell.BrowseForFolder(0, 'Select Pharmacy AI Workspace in Google Drive for Desktop', 0); if ($folder) { $folder.Self.Path }"`) do set "DRIVE_WORKSPACE=%%I"

if not defined DRIVE_WORKSPACE (
  echo.
  echo No folder selected. Nothing was changed.
  pause
  exit /b 1
)

echo.
echo Selected:
echo %DRIVE_WORKSPACE%
echo.
call :runpy sync_github_for_gemini.py --configure-drive "%DRIVE_WORKSPACE%"
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
  echo Google Drive workspace configured successfully.
) else (
  echo Google Drive setup failed. Your existing local outputs were left intact.
)
pause
exit /b %RC%

:runpy
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 %*
) else (
  python %*
)
exit /b %errorlevel%
