@echo off
REM Creates a Windows scheduled task that runs the digest every Monday at 08:30.
REM If the computer is off at that time, it runs as soon as you next log in.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$d=(Get-Location).Path;" ^
 "$a=New-ScheduledTaskAction -Execute (Join-Path $d 'run_digest.bat') -WorkingDirectory $d;" ^
 "$t=New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 8:30am;" ^
 "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;" ^
 "Register-ScheduledTask -TaskName 'Plant paper digest' -Action $a -Trigger $t -Settings $s -Description 'Weekly plant biology literature digest' -Force | Out-Null;" ^
 "Write-Host 'Scheduled: every Monday 08:30 (Task Scheduler > Plant paper digest)'"
pause
