@echo off
rem Double-click to install PaperBot on this PC (needs Python 3.10+ from python.org).
setlocal
cd /d "%~dp0"
title Installing PaperBot
echo Installing PaperBot in %CD%
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" || goto :nopython

if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment...
  %PY% -m venv .venv || goto :fail
)
echo Installing the parts PaperBot needs (a minute or two)...
set "LOG=%TEMP%\paperbot-install.log"
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip > "%LOG%" 2>&1
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e . >> "%LOG%" 2>&1 || (type "%LOG%" & goto :fail)
echo Adding PaperBot to the Desktop and Start menu...
".venv\Scripts\python.exe" -m paperbot.shortcut || goto :fail
echo.
echo Done. Starting PaperBot...
start "" ".venv\Scripts\pythonw.exe" -m paperbot.gui
exit /b 0

:nopython
echo Python 3.10 or newer is needed first.
echo Opening python.org - install it with "Add python.exe to PATH" ticked,
echo then double-click this file again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:fail
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
