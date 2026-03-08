@echo off
cd /d C:\Users\Administrator\Desktop\Cato
set CATO_VAULT_PASSWORD=mypassword123
"C:\Program Files\Python312\python.exe" scripts\watchdog.py >> C:\Users\Administrator\AppData\Roaming\cato\watchdog_task.log 2>&1
