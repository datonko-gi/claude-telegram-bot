import os
import re
import json
import logging
import time
from collections import defaultdict
from anthropic import Anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "50"))

client = Anthropic(api_key=ANTHROPIC_API_KEY)
conversations: dict[int, list] = defaultdict(list)
pending_updates: dict[int, dict] = {}

HUBSPOT_BASE = "https://api.hubapi.com"

LIFECYCLE_STAGES = {
    "subscriber": "Subscriber",
    "lead": "Lead",
    "marketingqualifiedlead": "MQL",
    "salesqualifiedlead": "SQL",
    "opportunity": "Opportunity",
    "customer": "Customer",
    "evangelist": "Evangelist",
    "1578580705": "Not interested",
    "other": "Other",
}

LEAD_STATUSES = {
    "NEW": "New",
    "OPEN": "Open",
    "IN_PROGRESS": "In Progress",
    "OPEN_DEAL": "Open Deal",
    "UNQUALIFIED": "Unqualified",
    "ATTEMPTED_TO_CONTACT": "Attempted to Contact",
    "CONNECTED": "Connected",
    "BAD_TIMING": "Bad Timing",
}

SYSTEM_PROMPT = """You are a personal AI assistant for Daniel (Danil) Tonkopiy.

== WHO DANIEL IS ==
- Serial entrepreneur and CEO based in Los Altos, CA. Stanford University graduate.
- Founder and CEO of Delfast — electric bicycle company that set a Guinness World Record.
- CEO of VisaNow.AI — AI-based legal immigration services. HubSpot CRM is actively used.
- Founder/General Director of Core Element AI, Inc. — AI for geological exploration.
- Co-founder of FilmArtMovies. Founder of In Charge One, Inc.
- Lives in Los Altos with wife Leah and two sons, plus a cat named Basiko.

== COMMUNICATION STYLE ==
- Direct communication without filler words or marketing language.
- Responds in the same language Daniel writes in.
- Keep messages concise for Telegram.

== HUBSPOT INTEGRATION ==
You have access to HubSpot CRM. When Daniel shares or forwards a conversation with a lead, or mentions updating a contact:

1. Analyze the conversation to understand the outcome.
2. Suggest what to update in HubSpot.

IMPORTANT: When you want to suggest a HubSpot update, you MUST include the following JSON block in your response wrapped in <hubspot_update> tags. The system will parse this and create confirmation buttons. Without these tags, NO actual update will happen.

Also include a <hubspot_contact> tag with the Telegram username to search for.

Example:
<hubspot_contact>username_here</hubspot_contact>
<hubspot_update>
{
  "summary": "Brief conversation summary",
  "suggested_lifecycle": "salesqualifiedlead",
  "suggested_lead_status": "IN_PROGRESS",
  "suggested_note": "Note text for HubSpot"
}
</hubspot_update>

Valid lifecycle stages: subscriber, lead, marketingqualifiedlead, salesqualifiedlead, opportunity, customer, evangelist, other
Valid lead statuses: NEW, OPEN, IN_PROGRESS, OPEN_DEAL, UNQUALIFIED, ATTEMPTED_TO_CONTACT, CONNECTED, BAD_TIMING

CRITICAL RULES:
- NEVER say "I updated HubSpot" or "Card updated" or "Обновляю карточку" — you CANNOT update HubSpot directly. Only the system can, after Daniel clicks the confirmation button.
- Instead say "Here's what I suggest updating:" and include the tags above.
- If Daniel says "да" or "обнови" to confirm a previous suggestion, remind him to click the button, or include new tags so the system creates new buttons.
- If the message is a normal question not related to CRM/leads, respond normally WITHOUT any hubspot tags.
"""


# === HUBSPOT API FUNCTIONS ===

def hubspot_request(method, endpoint, data=None):
    url = f"{HUBSPOT_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"HubSpot API error {e.code}: {error_body}")
        return {"error": e.code, "message": error_body}
    except Exception as e:
        logger.error(f"HubSpot request failed: {e}")
        return {"error": str(e)}


def search_contact_by_telegram(tg_username):
    tg_username_clean = tg_username.lower().lstrip("@")
    data = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "website",
                "operator": "CONTAINS_TOKEN",
                "value": tg_username_clean,
            }]
        }],
        "properties": [
            "firstname", "lastname", "email", "phone",
            "lifecyclestage", "hs_lead_status", "website",
        ],
        "limit": 5,
    }
    result = hubspot_request("POST", "/crm/v3/objects/contacts/search", data)
    if result and "results" in result and result["results"]:
        return result["results"]

    data = {
        "query": tg_username_clean,
        "properties": [
            "firstname", "lastname", "email", "phone",
            "lifecyclestage", "hs_lead_status", "website",
        ],
        "limit": 5,
    }
    result = hubspot_request("POST", "/crm/v3/objects/contacts/search", data)
    if result and "results" in result:
        return result["results"]
    return []


def update_contact(contact_id, properties):
    data = {"properties": properties}
    return hubspot_request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", data)


def add_note_to_contact(contact_id, note_text):
    data = {
        "properties": {
            "hs_note_body": note_text,
            "hs_timestamp": str(int(time.time() * 1000)),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 202,
            }]
        }]
    }
    return hubspot_request("POST", "/crm/v3/objects/notes", data)


def extract_hubspot_update(text):
    match = re.search(r"<hubspot_update>\s*(.*?)\s*</hubspot_update>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.error(f"Failed to parse hubspot_update JSON: {match.group(1)}")
    return None


def extract_hubspot_contact(text):
    match = re.search(r"<hubspot_contact>\s*(.*?)\s*</hubspot_contact>", text, re.DOTALL)
    if match:
        return match.group(1).strip().lstrip("@")
    return None


def clean_response(text):
    text = re.sub(r"<hubspot_update>.*?</hubspot_update>", "", text, flags=re.DOTALL)
    text = re.sub(r"<hubspot_contact>.*?</hubspot_contact>", "", text, flags=re.DOTALL)
    return text.strip()


# === TELEGRAM HANDLERS ===

def is_allowed(username):
    if not ALLOWED_USERS:
        return True
    allowed = [u.strip().lower().lstrip("@") for u in ALLOWED_USERS.split(",")]
    return username and username.lower() in allowed


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    await update.message.reply_text(
        "Привет, Даниил! Я твой AI-ассистент с HubSpot.\n\n"
        "Команды:\n"
        "/reset — очистить историю\n"
        "/model — текущая модель\n"
        "/setmodel <model> — сменить модель\n"
        "/find <username> — найти контакт\n\n"
        "Перешли переписку с клиентом или опиши результат разговора — я предложу обновления в HubSpot."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    pending_updates.pop(chat_id, None)
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


async def find_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    if not context.args:
        await update.message.reply_text("Укажи username: `/find username`", parse_mode="Markdown")
        return

    username = context.args[0].lstrip("@")
    await update.message.reply_text(f"Ищу {username} в HubSpot...")

    contacts = search_contact_by_telegram(username)
    if not contacts:
        await update.message.reply_text(f"Контакт @{username} не найден.")
        return

    for c in contacts:
        props = c.get("properties", {})
        name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or "—"
        stage = LIFECYCLE_STAGES.get(props.get("lifecyclestage", ""), props.get("lifecyclestage", "—"))
        status = LEAD_STATUSES.get(props.get("hs_lead_status", ""), props.get("hs_lead_status", "—"))
        website = props.get("website", "—")
        email = props.get("email", "—")
        phone = props.get("phone", "—")
        cid = c["id"]

        text = (
            f"*{name}*\n"
            f"Email: {email}\n"
            f"Phone: {phone}\n"
            f"Web: {website}\n"
            f"Stage: {stage}\n"
            f"Status: {status}\n"
            f"[HubSpot](https://app.hubspot.com/contacts/47345195/record/0-1/{cid})"
        )
        await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text:
        return

    # Detect forwarded messages
    is_forwarded = False
    fwd_username = None
    try:
        if update.message.forward_origin is not None:
            is_forwarded = True
            origin = update.message.forward_origin
            if hasattr(origin, "sender_user") and origin.sender_user:
                u = origin.sender_user
                fwd_name = f"{u.first_name or ''} {u.last_name or ''}".strip()
                fwd_username = u.username
                if u.username:
                    fwd_name += f" (@{u.username})"
                user_text = f"[FORWARDED from {fwd_name}]\n{user_text}"
    except Exception:
        pass

    conversations[chat_id].append({"role": "user", "content": user_text})

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

        # Check if Claude suggested a HubSpot update
        hs_update = extract_hubspot_update(reply)
        tg_username = extract_hubspot_contact(reply) or fwd_username
        clean_reply = clean_response(reply)

        if hs_update and tg_username:
            contacts = search_contact_by_telegram(tg_username)

            if contacts:
                contact = contacts[0]
                props = contact.get("properties", {})
                name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or tg_username
                cid = contact["id"]

                current_stage = LIFECYCLE_STAGES.get(props.get("lifecyclestage", ""), "—")
                current_status = LEAD_STATUSES.get(props.get("hs_lead_status", ""), "—")
                new_stage = LIFECYCLE_STAGES.get(hs_update.get("suggested_lifecycle", ""), "—")
                new_status = LEAD_STATUSES.get(hs_update.get("suggested_lead_status", ""), "—")

                update_text = (
                    f"{clean_reply}\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"*HubSpot: {name}*\n\n"
                    f"Stage: {current_stage} → *{new_stage}*\n"
                    f"Status: {current_status} → *{new_status}*\n"
                    f"Заметка: {hs_update.get('suggested_note', '—')}\n\n"
                    f"*Нажми кнопку:*"
                )

                pending_updates[chat_id] = {
                    "contact_id": cid,
                    "contact_name": name,
                    "lifecycle": hs_update.get("suggested_lifecycle"),
                    "lead_status": hs_update.get("suggested_lead_status"),
                    "note": hs_update.get("suggested_note"),
                }

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Обновить всё", callback_data="hs_confirm"),
                        InlineKeyboardButton("❌ Отмена", callback_data="hs_cancel"),
                    ],
                    [
                        InlineKeyboardButton("📝 Только заметку", callback_data="hs_note_only"),
                        InlineKeyboardButton("📊 Только статус", callback_data="hs_status_only"),
                    ]
                ]

                await update.message.reply_text(
                    update_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await update.message.reply_text(
                    f"{clean_reply}\n\n"
                    f"⚠️ Контакт @{tg_username} не найден в HubSpot.\n"
                    f"/find {tg_username}",
                )
        elif hs_update and not tg_username:
            await update.message.reply_text(
                f"{clean_reply}\n\n"
                f"⚠️ Не могу определить Telegram username контакта. Укажи его, например:\n"
                f"\"обнови @username — договорились о звонке\"",
            )
        else:
            if len(clean_reply) <= 4096:
                await update.message.reply_text(clean_reply)
            else:
                for i in range(0, len(clean_reply), 4096):
                    await update.message.reply_text(clean_reply[i:i + 4096])

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    pending = pending_updates.get(chat_id)

    if not pending:
        await query.edit_message_text("⚠️ Нет активного обновления.")
        return

    cid = pending["contact_id"]
    name = pending["contact_name"]

    if query.data == "hs_confirm":
        props = {}
        if pending.get("lifecycle"):
            props["lifecyclestage"] = pending["lifecycle"]
        if pending.get("lead_status"):
            props["hs_lead_status"] = pending["lead_status"]

        result = update_contact(cid, props) if props else {"ok": True}
        note_result = {"ok": True}
        if pending.get("note"):
            note_result = add_note_to_contact(cid, pending["note"])

        if "error" not in result and "error" not in note_result:
            stage_label = LIFECYCLE_STAGES.get(pending.get("lifecycle", ""), "—")
            status_label = LEAD_STATUSES.get(pending.get("lead_status", ""), "—")
            await query.edit_message_text(
                f"✅ *{name}* обновлён!\n\n"
                f"Stage → {stage_label}\n"
                f"Status → {status_label}\n"
                f"Заметка добавлена\n\n"
                f"[Открыть в HubSpot](https://app.hubspot.com/contacts/47345195/record/0-1/{cid})",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            error = result.get("message", "") or note_result.get("message", "")
            await query.edit_message_text(f"❌ Ошибка: {error}")

    elif query.data == "hs_note_only":
        if pending.get("note"):
            result = add_note_to_contact(cid, pending["note"])
            if "error" not in result:
                await query.edit_message_text(
                    f"📝 Заметка добавлена для *{name}*\n\n"
                    f"[HubSpot](https://app.hubspot.com/contacts/47345195/record/0-1/{cid})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await query.edit_message_text(f"❌ Ошибка: {result.get('message', '')}")
        else:
            await query.edit_message_text("Нет заметки.")

    elif query.data == "hs_status_only":
        props = {}
        if pending.get("lifecycle"):
            props["lifecyclestage"] = pending["lifecycle"]
        if pending.get("lead_status"):
            props["hs_lead_status"] = pending["lead_status"]

        if props:
            result = update_contact(cid, props)
            if "error" not in result:
                stage_label = LIFECYCLE_STAGES.get(pending.get("lifecycle", ""), "—")
                status_label = LEAD_STATUSES.get(pending.get("lead_status", ""), "—")
                await query.edit_message_text(
                    f"📊 Статус обновлён: *{name}*\n\n"
                    f"Stage → {stage_label}\n"
                    f"Status → {status_label}\n\n"
                    f"[HubSpot](https://app.hubspot.com/contacts/47345195/record/0-1/{cid})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await query.edit_message_text(f"❌ Ошибка: {result.get('message', '')}")
        else:
            await query.edit_message_text("Нет данных для обновления.")

    elif query.data == "hs_cancel":
        await query.edit_message_text(f"Обновление для {name} отменено.")

    pending_updates.pop(chat_id, None)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("model", model_info))
    app.add_handler(CommandHandler("setmodel", set_model))
    app.add_handler(CommandHandler("find", find_contact))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot started with HubSpot integration. Model: {MODEL}")
    app.run_polling()


if __name__ == "__main__":
    main()
