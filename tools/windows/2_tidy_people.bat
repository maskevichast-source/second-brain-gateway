@echo off
call "%~dp0common.bat" || exit /b 1
echo === ПЛАН склейки дублей карточек людей (ничего не меняется) ===
python "%TOOLS%\vault_people.py" "%VAULT%" --tidy
echo.
echo Прочитайте план выше. Внимание: тёзки с разными телефонами или компаниями склеивать не нужно.
set /p OK=Применить? Введите ДА и Enter, иначе просто Enter: 
if /i "%OK%"=="ДА" (
  python "%TOOLS%\vault_people.py" "%VAULT%" --tidy --apply --push "%URL%"
) else (
  echo Отменено, ничего не изменено.
)
pause
