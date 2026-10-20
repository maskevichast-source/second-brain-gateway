@echo off
rem Общие настройки. Подключается другими .bat. Токен в файлах не хранится: только в переменной пользователя Windows.
chcp 65001 >nul
set PYTHONUTF8=1
set "TOOLS=%~dp0.."
set "CFG=%~dp0config.txt"
set "URL="
set "VAULT="
if exist "%CFG%" for /f "usebackq tokens=1,* delims==" %%A in ("%CFG%") do call :cfgline "%%A" "%%B"
goto :cfg_done
:cfgline
if /i "%~1"=="URL" set "URL=%~2"
if /i "%~1"=="VAULT" set "VAULT=%~2"
exit /b 0
:cfg_done
where python >nul 2>nul
if not errorlevel 1 goto :py_ok
echo Python не найден. Установите его с python.org и поставьте галочку "Add python.exe to PATH".
pause
exit /b 1
:py_ok
if "%URL%"=="" set "URL=https://web-production-514f9.up.railway.app"
if not "%VAULT%"=="" goto :vault_given
echo Введите полный путь к папке SecondBrain, например C:\Users\Вы\iCloudDrive\SecondBrain
set /p VAULT=Путь: 
:vault_given
set VAULT=%VAULT:"=%
if "%VAULT:~-1%"=="\" set "VAULT=%VAULT:~0,-1%"
if exist "%VAULT%\" goto :vault_ok
echo Папка не найдена: %VAULT%
pause
exit /b 1
:vault_ok
> "%CFG%" echo URL=%URL%
>> "%CFG%" echo VAULT=%VAULT%
if not "%SECOND_BRAIN_TOKEN%"=="" goto :token_ok
echo Введите токен CAPTURE_TOKEN из Railway: проект wholesome-prosperity, сервис web, вкладка Variables.
echo Он сохранится только в вашей учётной записи Windows, в файлы и в интернет не попадёт.
set /p SECOND_BRAIN_TOKEN=Токен: 
if "%SECOND_BRAIN_TOKEN%"=="" goto :no_token
setx SECOND_BRAIN_TOKEN "%SECOND_BRAIN_TOKEN%" >nul
goto :token_ok
:no_token
echo Токен не введён.
pause
exit /b 1
:token_ok
exit /b 0
