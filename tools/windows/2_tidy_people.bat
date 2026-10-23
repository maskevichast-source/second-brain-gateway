@echo off
call "%~dp0common.bat" || exit /b 1
echo === ПЛАН склейки дублей карточек людей (ничего не меняется) ===
python "%TOOLS%\vault_people.py" "%VAULT%" --tidy
echo.
echo Прочитайте план выше. Внимание: тёзки с разными телефонами или компаниями склеивать не нужно.
set "OK="
set /p OK=Применить? Введите ДА (или Y, или 1) и Enter, иначе просто Enter: 
echo Вы ввели: [%OK%]
if /i "%OK%"=="ДА" goto :apply
if /i "%OK%"=="DA" goto :apply
if /i "%OK%"=="Y" goto :apply
if /i "%OK%"=="YES" goto :apply
if "%OK%"=="1" goto :apply
echo Отменено, ничего не изменено.
pause
exit /b 0
:apply
echo === ПРИМЕНЯЮ ===
python "%TOOLS%\vault_people.py" "%VAULT%" --tidy --apply --push "%URL%"
echo.
echo Готово. Дубли ушли в папку 3-Архив\Люди-дубли. Теперь запустите 1_vault_sync.bat.
pause
