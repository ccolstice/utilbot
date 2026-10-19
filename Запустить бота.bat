@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Не найден .venv\Scripts\python.exe.
    echo Сначала установи Python и зависимости проекта.
    pause
    exit /b 1
)

if not exist ".env" (
    echo Не найден файл .env. Создай его и укажи BOT_TOKEN.
    pause
    exit /b 1
)

echo Запускаю Telegram-бота из:
echo %CD%
echo.
".venv\Scripts\python.exe" main.py

echo.
echo Бот остановлен. Если он завершился с ошибкой, смотри сообщения выше.
pause
