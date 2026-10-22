@echo off
call "%~dp0common.bat" || exit /b 1
echo Экспорт из iCloud: icloud.com - Контакты - Ctrl+A - шестерёнка - Экспорт vCard.
set /p VCF=Перетащите сюда файл .vcf (или введите путь) и нажмите Enter: 
if defined VCF set "VCF=%VCF:"=%"
if not defined VCF ( echo Файл не указан. & pause & exit /b 1 )
if not exist "%VCF%" ( echo Файл не найден. & pause & exit /b 1 )
set "DAYS=365"
set "DAYS_IN="
set /p DAYS_IN=За сколько последних дней брать контакты (Enter = 365): 
if defined DAYS_IN set "DAYS=%DAYS_IN%"
set "TGARG="
set "TG="
set /p TG=Путь к result.json из экспорта Telegram (Enter = пропустить): 
if defined TG set "TG=%TG:"=%"
if defined TG set TGARG=--telegram "%TG%"
echo.
echo === ПРОБНЫЙ запуск (ничего не пишется) ===
python "%TOOLS%\vault_contacts.py" "%VCF%" "%VAULT%" --days %DAYS% %TGARG% --dry-run
echo.
echo Ожидается примерно 50-150 новых карточек. Если сильно больше или меньше, поменяйте срок.
set /p OK=Создать карточки? Введите ДА и Enter, иначе просто Enter: 
if /i "%OK%"=="ДА" (
  python "%TOOLS%\vault_contacts.py" "%VCF%" "%VAULT%" --days %DAYS% %TGARG%
) else (
  echo Отменено, ничего не изменено.
)
pause
