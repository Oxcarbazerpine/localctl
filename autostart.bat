@echo off
REM Run localctl at logon. On any non-zero exit, leave two failure signals:
REM   1. Append a timestamped line to autostart-failures.log (always works)
REM   2. Try to write to the Windows Application event log under source
REM      `localctl-autostart` (silent fail if source not pre-registered — see
REM      register-eventsource.ps1 for one-time setup as admin)

setlocal
set "LOG=%~dp0autostart.log"
set "FAILLOG=%~dp0autostart-failures.log"
"C:\Program Files\Python313\python.exe" -m localctl start all >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    >> "%FAILLOG%" echo [%date% %time%] localctl start all exited with code %RC%; see %LOG%
    eventcreate /T ERROR /ID 100 /L APPLICATION /SO localctl-autostart /D "localctl start all exited with code %RC%; see %LOG%" >nul 2>&1
)
exit /b %RC%
