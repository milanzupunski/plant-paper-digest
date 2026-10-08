@echo off
REM Runs the plant paper digest and opens the result.
REM Extra options are passed through, e.g.:  run_digest.bat --days 30
cd /d "%~dp0"
set "PY=%USERPROFILE%\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"
echo ==== %date% %time% ==== >> run_log.txt
"%PY%" digest.py %* >> run_log.txt 2>&1
REM Open the digest only if this run succeeded (otherwise see run_log.txt)
if not errorlevel 1 if exist "%~dp0digests\latest.html" start "" "%~dp0digests\latest.html"
