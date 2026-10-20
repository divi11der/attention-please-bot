# Исправление кнопок Telegram после сна Render

1. Распакуй ZIP.
2. В GitHub открой репозиторий `attention-please-bot` и файл `bot.py`.
3. Нажми на карандаш (Edit this file). Замени весь код на текст из нового `bot.py`.
4. Внизу нажми `Commit changes`.
5. Render → твой Web Service → Deploys. Подожди статус Live или
   Manual Deploy → Deploy latest commit (если не запускается автоматически).
6. Render → Logs: проверь сообщения `PostgreSQL connected` и
   `Telegram webhook enabled`.
7. Открой Telegram → /start → 🐞 Сообщить об ошибке.

Изменения: подтверждение нажатия происходит раньше обращения в базу;
ошибки устаревшего callback Telegram не прерывают отправку приглашения;
ошибки Neon показываются пользователю и записываются в логи;
вебхук HTTP отвечает быстрее после пробуждения сервера.

Важно: переменные BOT_TOKEN, DATABASE_URL и TEAM_CHAT_ID в Render
не меняй. Никакие секреты в GitHub не публикуй.

Render Free по-прежнему засыпает. Первое сообщение после пробуждения
может прийти с задержкой. Если кнопка не ответила, проверь Logs.
