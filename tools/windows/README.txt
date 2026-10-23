Запуск на Windows-ноутбуке (нужен Python с галочкой Add python.exe to PATH).
Положите папку windows рядом с остальными файлами папки tools (vault_sync.py, vault_people.py,
vault_contacts.py, vault_organize.py должны лежать на уровень выше).

1_vault_sync.bat        забрать очередь из Telegram в хранилище (первый запуск спросит токен и путь)
2_tidy_people.bat       склейка дублей людей: сначала план, потом подтверждение ДА
3_import_contacts.bat   импорт контактов .vcf (и переписки Telegram): сначала пробный запуск, потом ДА
4_vault_sync_loop.bat   синхронизация каждые 5 минут
5_export_for_claude.bat архив заметок на рабочий стол, чтобы прикрепить его в чат с Claude (без приватных и вложений, длинные номера скрыты)

Токен CAPTURE_TOKEN сохраняется только в переменной пользователя Windows (setx). Адрес и путь к хранилищу
запоминаются в config.txt рядом с батниками; сам config.txt в репозиторий не коммитьте.
