LINKCHECK PLUS — Founder profile sync & owner analytics fix
Дата сборки: 10.11.2026

ЧТО ИСПРАВЛЕНО
1. Профиль создателя обновляется по постоянному Telegram ID, а не только по старому username.
2. Имя, username и collectible emoji status перечитываются из актуального GetFullUser Telegram.
3. Кэш профиля: 60 секунд; Mini App запрашивает профиль раз в 60 секунд без HTTP-кэша.
4. Фото профиля загружается через Telethon и сохраняется атомарно в постоянный DATA_DIR как founder_nifort.jpg. Кэш фото — 120 секунд; при ошибке предыдущая рабочая копия сохраняется.
5. Если активен collectible emoji status, backend запрашивает соответствующий custom-emoji document у Telegram, скачивает TGS, распаковывает GZIP, проверяет Lottie JSON и сохраняет в DATA_DIR/founder_gifts. Это заменяет прежнюю логику, которая только искала уже существующий локальный JSON.
6. Удаление/смена collectible status теперь заменяет старое состояние; старый анимированный подарок не должен восстанавливаться поверх актуального профиля.
7. Карточка основателя обновляет имя, ссылку, username и аватар автоматически; запросы аватара и профиля обходят HTTP-кэш.
8. Статистика владельца обновляется каждые 15 секунд, пока открыт экран профиля, и сразу при возвращении Mini App на экран.
9. Для статистики API отключено кеширование.

КАК РАБОТАЕТ СТАТИСТИКА
Считаются авторизованные переходы из LINKCHECK на Telegram-ссылки одобренных проектов.
Один пользователь учитывается максимум один раз на проект за UTC-день. Собственные переходы владельца исключаются, чтобы не накручивать счётчик. Это не просмотры постов Telegram, не подписчики и не внешняя статистика канала.

ОБНОВЛЕНИЕ BOTHOST
1. Сначала сделайте резервную копию /app/data, особенно linkcheck.db и Telethon session.
2. В архиве замените main.py и miniapp.html. Остальные файлы проекта оставлены в комплекте.
3. Не удаляйте и не пересоздавайте /app/data. Там сохраняются БД, Telethon session, кэш аватара и кэш gift-анимаций.
4. Убедитесь, что Bothost запускает Python 3.11, точка входа main.py, и заданы BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TG_SESSION и DB_PATH.
5. Перезапустите приложение. На старте backend попробует синхронизировать профиль основателя.
6. Проверьте Mini App внутри Telegram. Смена данных отображается автоматически с периодом опроса до 60 секунд для имени/username/gift status и до 120 секунд для фото при штатной работе Telegram API.

ВАЖНЫЕ УСЛОВИЯ
- Для загрузки collectible animation и фото требуется действующая авторизованная Telethon user session с доступом к профилю основателя.
- Если Telegram вернёт FloodWait или session отключена, backend сохраняет последний рабочий кэш и обновит данные после восстановления доступа.
- Реальный запрос к твоей Telegram-сессии и тест на Bothost в этой среде не выполнялись.
- Код компилируется, JS проходит node --check; выполнены изолированные тесты загрузки/кэширования Lottie и аватара, а также логики уникальных переходов SQLite.

ИСТОЧНИК
Правки внесены в полную локальную LINKCHECK PLUS сборку, с которой продолжалась предыдущая работа. Поведение дефектных участков сверено с публичной веткой GitHub 10.11.2026, но весь локальный проект побайтно не подтверждён как checkout текущего GitHub commit. Архив не меняет репозиторий автоматически.


LINKCHECK PLUS (Telegram Stars + Telegram Business)
==================================================
The PLUS module uses the existing BOT_TOKEN and SQLite database. No payment-provider token is required for Stars.

Configured plans (one-time payment; manual renewal):
- 1 month: 200 Telegram Stars (30 days)
- 6 months: 1,000 Telegram Stars (180 days)
- 1 year: 1,800 Telegram Stars (365 days)

The Mini App creates an XTR invoice link and opens it with Telegram.WebApp.openInvoice. The backend validates pre_checkout_query and activates the subscription only after a successful_payment update from Telegram. Do not manually activate subscriptions based on screenshots or client callbacks.

Before end users can connect the bot, enable Business Mode / Secretary Mode for @linkcheck_verify_bot in @BotFather: /mybots > select the bot > Bot Settings > Business Mode (the label may appear as Secretary Mode). Then the account owner can connect @linkcheck_verify_bot in Telegram Settings > Telegram Business > Chatbots and choose the chats/permissions. Telegram's current Connected Business Bots documentation says connecting a business bot does not itself require Telegram Premium; the Telegram Business settings available on an account depend on Telegram's current account rules. The bot only stores message updates Telegram actually delivers while PLUS is active. Saved content is retained for up to 30 days and deleted when the Business connection is disabled. The owner receives edit/delete notices in the bot's private chat; the Mini App does not expose a message archive.

Important: 6- and 12-month plans are one-time invoices, not recurring Stars subscriptions. The owner must renew manually. Confirm that Fragment withdrawal verification is available to the bot owner before accepting public paid subscriptions.


Payment safety switch
---------------------
PLUS_PAYMENTS_ENABLED defaults to false. Keep it false while Fragment identity verification / withdrawal eligibility is unresolved. Only set PLUS_PAYMENTS_ENABLED=true in the Bothost environment after the account owner has confirmed that Fragment allows withdrawal for this bot owner. The Mini App then enables the plan purchase buttons. This switch prevents customers from paying while the owner cannot yet confirm access to withdrawal.

LINKCHECK PLUS — обновление интерфейса, тарифов и тестового доступа
==================================================================

ИНТЕРФЕЙС
- Кнопка LINKCHECK PLUS перенесена на главную страницу под блок статистики.
- При нажатии открывается отдельное полноэкранное окно в стиле окна политики конфиденциальности.
- Блок подписки удалён из Профиля.
- В окне доступны статус подписки, дата окончания, подключение Telegram Business, тарифы, история платежей и инструкция.

ОПЛАТА STARS
- Оплата XTR включена по умолчанию. Если в переменных Bothost явно задано PLUS_PAYMENTS_ENABLED=false, измените его на true или удалите эту переменную.
- 1 месяц: базовая цена 200 Stars, скидка 0%, итог 200 Stars.
- 6 месяцев: базовая цена 1 000 Stars, скидка 17%, итог 830 Stars.
- 1 год: базовая цена 1 800 Stars, скидка 25%, итог 1 350 Stars.
- Это разовые платежи; автоматического списания и автопродления нет.
- Backend активирует подписку только после successful_payment от Telegram. Событие openInvoice со статусом paid само по себе не активирует подписку.
- Срок и сумма конкретного выставленного счёта фиксируются в invoice intent, поэтому изменение тарифа администратором не меняет уже созданный счёт.

ТЕСТОВЫЙ ДОСТУП
- Каждый Telegram-аккаунт может один раз активировать бесплатный тестовый период на 3 дня (0 Stars).
- Тестовый доступ включает ту же серверную проверку подписки и обработку Telegram Business событий, что и платный тариф.
- Повторно получить тестовый период на том же Telegram ID нельзя. При уже активной подписке endpoint откажет в выдаче тестового доступа.

АДМИНИСТРИРОВАНИЕ ТАРИФОВ
- Администратор определяется через существующую переменную окружения ADMIN_IDS (Telegram ID через запятую).
- В Mini App откройте LINKCHECK PLUS на главной странице. Внизу окна для администраторов появится блок «Управление тарифами».
- Для каждого тарифа можно изменить срок в днях, базовую цену в Stars и скидку в процентах (0–90%). Итоговая цена пересчитывается сервером.
- Изменения сохраняются в SQLite и переживают перезапуск приложения.
- Настройки применяются к новым счетам. Уже выставленные счета сохраняют исходную цену и срок.
- Для защиты от некорректных значений сервер ограничивает срок 1–3650 днями, базовую цену 1–1 000 000 Stars и скидку 0–90%.

ОБНОВЛЕНИЕ
1. Сделайте резервную копию /app/data перед заменой файлов.
2. Замените main.py, miniapp.html, DEPLOY_README.txt и LINKCHECK_PLUS_CHANGELOG.txt файлами из архива.
3. Не удаляйте /app/data и существующую SQLite базу.
4. Убедитесь, что ADMIN_IDS содержит ваш Telegram ID, иначе административный редактор тарифов не появится.
5. Если ранее PLUS_PAYMENTS_ENABLED был установлен в false, измените его на true или удалите переменную.
6. Перезапустите Bothost и откройте Mini App внутри Telegram.

ПРОВЕРКИ И ОГРАНИЧЕНИЯ
- python -m py_compile main.py — PASS.
- node --check для inline JavaScript — PASS.
- В текущей среде не установлена библиотека aiogram, поэтому импорт backend и интеграционный запуск SQLite через полный модуль здесь не выполнялись.
- Реальные Stars-платежи, Telegram Business connection и Bothost deployment должны быть проверены после загрузки архива в ваш репозиторий/хостинг.
- Сервис сохраняет только сообщения, фактически доставленные Telegram боту; он не восстанавливает данные, которые Telegram не отправил.
