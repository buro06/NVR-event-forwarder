@echo off
rem Windows launcher for nvr-event-push. Double-click for the interactive menu,
rem or run from a prompt with the usual arguments, e.g. `nvr-event-push.cmd run`.
rem The first run creates .venv next to this file and installs the package.
setlocal

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PY=%VENV%\Scripts\python.exe"

rem Keep config.yaml next to this file, whichever directory we're started from.
if not defined NVR_EVENT_PUSH_CONFIG set "NVR_EVENT_PUSH_CONFIG=%ROOT%config.yaml"

if exist "%PY%" goto run

echo First run: setting up a Python environment in %VENV% ...
set "BASE="
where py >nul 2>&1 && set "BASE=py -3"
if not defined BASE (where python >nul 2>&1 && set "BASE=python")
if not defined BASE (
    echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    goto fail
)
%BASE% -c "import sys; sys.exit(sys.version_info < (3, 10))" || (
    echo Python 3.10 or newer is required. Found:
    %BASE% --version
    goto fail
)
%BASE% -m venv "%VENV%" || goto fail_setup
"%PY%" -m pip install --disable-pip-version-check -e "%ROOT%." || goto fail_setup

:run
"%PY%" -m nvr_event_push %*
set "RC=%ERRORLEVEL%"
rem Keep a double-clicked window open long enough to read the error.
if not "%RC%"=="0" pause
exit /b %RC%

:fail_setup
rem Remove the half-built environment so the next run starts over.
if exist "%VENV%" rmdir /s /q "%VENV%"
:fail
pause
exit /b 1
