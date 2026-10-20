@echo off
call "%~dp0common.bat" || exit /b 1
echo Синхронизация каждые 5 минут. Окно не закрывайте. Остановить: Ctrl+C.
python "%TOOLS%\vault_sync.py" --url "%URL%" --vault "%VAULT%" --loop 300
pause
