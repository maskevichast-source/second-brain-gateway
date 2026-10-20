@echo off
call "%~dp0common.bat" || exit /b 1
echo Забираю заметки из очереди в хранилище...
python "%TOOLS%\vault_sync.py" --url "%URL%" --vault "%VAULT%"
echo.
echo Готово.
pause
