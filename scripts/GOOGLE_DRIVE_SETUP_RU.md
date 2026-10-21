# Подключение Google Drive через Apps Script

Эта схема не использует Google OAuth refresh token в Render. Небольшой Apps Script работает от имени владельца Google-аккаунта, а бот вызывает его по URL с общим случайным ключом. Скрипт сам создаёт в Drive папку `UtilBot Backups`.

## 1. Создать скрипт

1. Открой <https://script.google.com/home> под нужным Google-аккаунтом.
2. Нажми **New project**.
3. Открой локальный файл `scripts/google_drive_apps_script.gs` в Блокноте, скопируй всё содержимое и вставь его вместо шаблона в `Code.gs`.
4. В настройках проекта включи показ файла манифеста `appsscript.json`. Открой локальный `scripts/appsscript.json` и замени содержимое одноимённого файла в редакторе Apps Script его текстом.
5. Нажми **Save project** и назови проект, например `UtilBot Drive Backup`.

## 2. Задать общий секрет

В PowerShell создай случайный ключ:

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$secret = -join ($bytes | ForEach-Object { $_.ToString('x2') })
$secret
```

Скопируй выведенную строку. В Apps Script открой **Project Settings** (значок шестерёнки) → **Script Properties** → **Add script property**. Добавь свойство:

- Property: `SHARED_SECRET`
- Value: строка, созданная PowerShell

Сохрани свойства. Не вставляй ключ в исходный код скрипта.

## 3. Один раз разрешить доступ к Drive

1. В верхнем списке функций редактора выбери `authorizeDrive`.
2. Нажми **Run**.
3. Выбери Google-аккаунт и подтверди доступ к файлам, созданным этим приложением, и исходящим запросам скрипта. Манифест запрашивает `drive.file`, а не доступ ко всему Диску.

## 4. Опубликовать веб-приложение

1. Нажми **Deploy** → **New deployment**.
2. Рядом с типом развертывания нажми шестерёнку и выбери **Web app**.
3. **Execute as**: `Me`.
4. **Who has access**: `Anyone` (анонимный доступ нужен Render; сам запрос проверяется общим секретом).
5. Нажми **Deploy** и скопируй URL веб-приложения, заканчивающийся на `/exec`.

Если пункта `Anyone` нет или Google запрещает такое развертывание, остановись и пришли скриншот этого окна — не выбирай режим, который требует входа в Google у каждого запроса.

## 5. Добавить настройки в Render

1. Открой Render → сервис `gembabot` → **Environment**.
2. Добавь `GDRIVE_WEBAPP_URL` со скопированным `/exec` URL.
3. Добавь `GDRIVE_SHARED_SECRET` с той же строкой, что сохранена в Script Properties.
4. Нажми **Save and deploy** и дождись завершения деплоя.

## 6. Проверить из Telegram

- `/drive_backup` — создать снимок текущей непустой базы Turso и сохранить его в Drive.
- `/drive_backups` — показать последние копии; кнопка попросит подтверждение перед восстановлением.
- Перед восстановлением текущая непустая база сначала копируется в Drive.

Это ручные команды. Обычный автобэкап в Telegram остаётся отдельным. Google Drive хранит базу с текстом, папками и Telegram file ID; сами медиафайлы в Drive не копируются.
