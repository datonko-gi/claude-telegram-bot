# Claude Telegram Bot

Личный Claude в Telegram. Без посредников — напрямую через Anthropic API.

## Быстрый старт

### 1. Создай бота в Telegram

- Открой [@BotFather](https://t.me/BotFather) в Telegram
- Отправь `/newbot`
- Дай имя (например: "My Claude Assistant")
- Дай username (например: `my_claude_ai_bot`)
- Скопируй **токен** — строку вида `7123456789:AAH...`

### 2. Получи Anthropic API ключ

- Зайди на https://console.anthropic.com/settings/keys
- Создай ключ, скопируй (начинается с `sk-ant-...`)

### 3. Настрой и запусти

#### Вариант A: Docker (рекомендуется)

```bash
cp .env.example .env
# Отредактируй .env — вставь свои токены
docker compose up -d
```

Готово. Бот работает.

#### Вариант B: Без Docker

```bash
pip install -r requirements.txt
cp .env.example .env
# Отредактируй .env
export $(cat .env | grep -v '^#' | xargs)
python bot.py
```

## Команды бота

| Команда | Что делает |
|---------|-----------|
| `/start` | Приветствие |
| `/reset` | Очистить историю диалога |
| `/model` | Показать текущую модель |
| `/setmodel <model>` | Сменить модель |

## Настройки (.env)

| Переменная | Описание |
|-----------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен от BotFather |
| `ANTHROPIC_API_KEY` | Ключ Anthropic API |
| `ALLOWED_USERS` | Кому разрешён доступ (username через запятую, пусто = все) |
| `CLAUDE_MODEL` | Модель (default: claude-sonnet-4-20250514) |
| `MAX_HISTORY` | Сколько сообщений хранить в контексте (default: 50) |

## Где запускать

Бот должен работать 24/7 на сервере. Варианты:

- **VPS** — любой за $5/мес (DigitalOcean, Hetzner, etc.)
- **Свой сервер/NAS** дома
- **Railway / Fly.io / Render** — cloud platforms с бесплатными тирами
