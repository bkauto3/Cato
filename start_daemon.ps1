$env:CATO_VAULT_PASSWORD = 'mypassword123'
Start-Process -FilePath python -ArgumentList 'cato_svc_runner.py' -WorkingDirectory 'C:\Users\Administrator\Desktop\Cato' -WindowStyle Hidden
Write-Host "Daemon launched"
