import os
import logging
from collections import defaultdict
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")  # comma-separated telegram usernames
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "50"))

client = Anthropic(api_key=ANTHROPIC_API_KEY)
conversations: dict[int, list] = defaultdict(list)

SYSTEM_PROMPT = """You are a helpful AI assistant in Telegram. 
You communicate in the same language the user writes to you.
Be concise — Telegram messages should be readable on a phone screen.
If a response is long, break it into logical paragraphs."""


def is_allowed(username: str | None) -> bool:
    if not ALLOWED_USERS:
        return True
    allowed = [u.strip().lower().lstrip("@") for u in ALLOWED_USERS.split(",")]
    return username and username.lower() in allowed


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    await update.message.reply_text(
        "Привет! Я Claude — твой AI-ассистент. Пиши что угодно.\n\n"
        "Команды:\n"
        "/reset — очистить историю диалога\n"
        "/model — текущая модель\n"
        "/setmodel <model> — сменить модель"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    await update.message.reply_text("История очищена.")


async def model_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    await update.message.reply_text(f"Текущая модель: `{MODEL}`", parse_mode="Markdown")


async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    global MODEL
    if context.args:
        MODEL = context.args[0]
        await update.message.reply_text(f"Модель изменена: `{MODEL}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "Укажи модель:\n"
            "`/setmodel claude-sonnet-4-20250514`\n"
            "`/setmodel claude-opus-4-20250514`\n"
            "`/setmodel claude-haiku-4-20250506`",
            parse_mode="Markdown",
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    if not user_text:
        return

    conversations[chat_id].append({"role": "user", "content": user_text})

    # trim history
    if len(conversations[chat_id]) > MAX_HISTORY:
        conversations[chat_id] = conversations[chat_id][-MAX_HISTORY:]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=conversations[chat_id],
        )
        reply = response.content[0].text
        conversations[chat_id].append({"role": "assistant", "content": reply})

        # telegram limit 4096 chars per message
        if len(reply) <= 4096:
            await update.message.reply_text(reply)
        else:
            for i in range(0, len(reply), 4096):
                await update.message.reply_text(reply[i : i + 4096])

    except Exception as e:
        logger.error(f"Anthropic API error: {e}")
        await update.message.reply_text(f"Ошибка API: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("model", model_info))
    app.add_handler(CommandHandler("setmodel", set_model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot started. Model: {MODEL}")
    app.run_polling()


if __name__ == "__main__":
    main()
