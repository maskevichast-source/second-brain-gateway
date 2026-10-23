@echo off
call "%~dp0common.bat" || exit /b 1
echo Собираю архив заметок для чата с Claude (без приватных, без вложений, номера скрыты)...
python "%TOOLS%\export_for_claude.py" "%VAULT%"
echo.
echo Архив лежит на рабочем столе. Прикрепите его в чат и напишите, что нужно сделать.
pause
