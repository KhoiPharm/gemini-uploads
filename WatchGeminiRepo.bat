@echo off
setlocal
cd /d "%~dp0"
call :runpy sync_github_for_gemini.py --watch 120
set "RC=%errorlevel%"
echo.
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
