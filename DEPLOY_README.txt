LINKCHECK PLUS — пакет для Bothost

СОСТАВ АРХИВА
- main.py
- miniapp.html
- privacy.html
- requirements.txt
- bothost.json
- LINKCHECK_PLUS_CHANGELOG.txt

БЕЗОПАСНОСТЬ
В архив не входят .env, BOT_TOKEN, Telegram API keys, linkcheck.db,
Telethon .session, кэши, файлы доказательств владения и персональные данные.
Не публикуйте секреты в GitHub.

ОБНОВЛЕНИЕ BOTHOST
1. Перед обновлением сделайте резервные копии текущих main.py,
   miniapp.html, privacy.html и /app/data/linkcheck.db.
2. Оставьте приложение типа Python, точка входа — main.py.
3. Загрузите файлы из архива в каталог приложения с заменой старых
   main.py, miniapp.html, privacy.html, requirements.txt и bothost.json.
4. Сохраните постоянный каталог данных /app/data и существующую БД.
5. Убедитесь, что настроены переменные:
   BOT_TOKEN
   MINIAPP_URL
   ADMIN_IDS
   TELEGRAM_API_ID
   TELEGRAM_API_HASH
   DB_PATH=/app/data/linkcheck.db
   TG_SESSION=/app/data/linkcheck_telegram
6. Не удаляйте linkcheck.db и Telethon session. Новые таблицы LINKCHECK PLUS
   создаются автоматически при старте и не требуют сброса существующей БД.
7. Перезапустите приложение.

ДОПОЛНИТЕЛЬНЫЕ ПАРАМЕТРЫ
- WATCHLIST_SCAN_INTERVAL=60 — период фоновой проверки изменений в секундах.
  Допустимый диапазон: 30–900. По умолчанию 60.
- PORT — порт веб-сервера; по умолчанию 8080.
- DATA_DIR — постоянный каталог; на Bothost рекомендуется /app/data.

ОСОБЕННОСТИ
- Уведомления показываются внутри Mini App. Telegram DM/push в этой версии
  не отправляются.
- Аналитика считает авторизованные переходы из каталога к Telegram-ссылкам,
  не просмотры/подписчиков самих каналов.
- Для корректной работы фонового мониторинга запускайте один экземпляр
  приложения на одну БД и одну Telethon session.
- После загрузки проверьте /, /privacy, каталог, избранное и статистику
  владельца в реальном Telegram Mini App.

ИСТОЧНИК И ОГРАНИЧЕНИЕ СВЕРКИ
Для этой сборки использован полный сохранённый снимок Bothost-развёртывания
LINKCHECK из файлов проекта от 10.11.2026. Снимок не был побайтно сверен с
текущей веткой GitHub; по просмотренным на GitHub размерам/строкам они
отличаются. Поэтому архив нельзя считать доказанной копией текущего коммита.
Перед production-заменой сравните файлы с текущим GitHub checkout.


UI REDESIGN (10.11.2026)
Обновлён только интерфейс miniapp.html. main.py, privacy.html, requirements.txt, bothost.json и структура SQLite сохранены из предыдущего архива LINKCHECK PLUS. Для обновления достаточно заменить miniapp.html, однако полный ZIP содержит комплект того же проекта.
