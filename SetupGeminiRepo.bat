@echo off
setlocal
cd /d "%~dp0"
call :runpy sync_github_for_gemini.py --setup
if errorlevel 1 (
  set "RC=%errorlevel%"
  goto :done
)
call :runpy sync_github_for_gemini.py --force
set "RC=%errorlevel%"
:done
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
