import os
import re
import json
import logging
import time
import tempfile
import base64
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

== FILE AND IMAGE HANDLING ==
When Daniel sends files (xlsx, csv, txt) or images/screenshots, the system extracts content and includes it in the message. You CAN see file contents and images — acknowledge them and work with the data.

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
- If Daniel says "да" or "обнови" to confirm, remind him to click the button, or include new tags.
- If the message is a normal question not related to CRM/leads, respond normally WITHOUT any hubspot tags.
- The /find command works independently — it searches HubSpot directly without needing you.
"""


def esc(text):
    """Escape HTML special characters."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# === FILE PARSING ===

def parse_xlsx(file_path):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        result = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            result.append(f"=== Sheet: {sheet_name} ===")
            for row in rows[:500]:
                cells = [str(c) if c is not None else "" for c in row]
                result.append(" | ".join(cells))
        wb.close()
        text = "\n".join(result)
        if len(text) > 30000:
            text = text[:30000] + "\n... (truncated)"
        return text
    except Exception as e:
        logger.error(f"Failed to parse xlsx: {e}")
        return f"[Error reading xlsx: {e}]"


def parse_csv(file_path):
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(50000)
    except Exception as e:
        return f"[Error reading csv: {e}]"


def parse_text(file_path):
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(50000)
    except Exception as e:
        return f"[Error reading file: {e}]"


def parse_file(file_path, file_name):
    name_lower = file_name.lower()
    if name_lower.endswith(".xlsx") or name_lower.endswith(".xls"):
        return parse_xlsx(file_path)
    elif name_lower.endswith(".csv"):
        return parse_csv(file_path)
    elif name_lower.endswith((".txt", ".md", ".json", ".py", ".js", ".html")):
        return parse_text(file_path)
    else:
        return f"[Unsupported file type: {file_name}. Supported: xlsx, csv, txt, md, json]"


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
            result = json.loads(resp.read().decode())
            logger.info(f"HubSpot {method} {endpoint}: OK")
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"HubSpot API error {e.code} for {method} {endpoint}: {error_body}")
        return {"error": e.code, "message": error_body}
    except Exception as e:
        logger.error(f"HubSpot request failed for {method} {endpoint}: {e}")
        return {"error": str(e)}


def search_contact_by_telegram(tg_username):
    tg_username_clean = tg_username.lower().strip().lstrip("@")
    logger.info(f"Searching for contact: {tg_username_clean}")

    # Method 1: CONTAINS_TOKEN
    data = {
        "filterGroups": [{"filters": [{"propertyName": "website", "operator": "CONTAINS_TOKEN", "value": tg_username_clean}]}],
        "properties": ["firstname", "lastname", "email", "phone", "lifecyclestage", "hs_lead_status", "website"],
        "limit": 5,
    }
    result = hubspot_request("POST", "/crm/v3/objects/contacts/search", data)
    if result and "results" in result and result["results"]:
        return result["results"]

    # Method 2: EQ with URL variations
    for url_pattern in [f"https://t.me/{tg_username_clean}", f"http://t.me/{tg_username_clean}", f"t.me/{tg_username_clean}"]:
        data = {
            "filterGroups": [{"filters": [{"propertyName": "website", "operator": "EQ", "value": url_pattern}]}],
            "properties": ["firstname", "lastname", "email", "phone", "lifecyclestage", "hs_lead_status", "website"],
            "limit": 5,
        }
        result = hubspot_request("POST", "/crm/v3/objects/contacts/search", data)
        if result and "results" in result and result["results"]:
            return result["results"]

    # Method 3: Fulltext
    data = {
        "query": tg_username_clean,
        "properties": ["firstname", "lastname", "email", "phone", "lifecyclestage", "hs_lead_status", "website"],
        "limit": 5,
    }
    result = hubspot_request("POST", "/crm/v3/objects/contacts/search", data)
    if result and "results" in result and result["results"]:
        return result["results"]

    # Method 4: Name search
    if "_" in tg_username_clean:
        data = {
            "query": tg_username_clean.replace("_", " "),
            "properties": ["firstname", "lastname", "email", "phone", "lifecyclestage", "hs_lead_status", "website"],
            "limit": 5,
        }
        result = hubspot_request("POST", "/crm/v3/objects/contacts/search", data)
        if result and "results" in result and result["results"]:
            return result["results"]

    return []


def update_contact(contact_id, properties):
    return hubspot_request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties})


def add_note_to_contact(contact_id, note_text):
    data = {
        "properties": {"hs_note_body": note_text, "hs_timestamp": str(int(time.time() * 1000))},
        "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]
    }
    return hubspot_request("POST", "/crm/v3/objects/notes", data)


def extract_hubspot_update(text):
    match = re.search(r"<hubspot_update>\s*(.*?)\s*</hubspot_update>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.error(f"Failed to parse hubspot_update JSON")
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
        "/setmodel — сменить модель\n"
        "/find username — найти контакт\n"
        "/debug — проверить HubSpot\n\n"
        "Можно:\n"
        "- Переслать переписку с клиентом\n"
        "- Отправить файл (xlsx, csv, txt)\n"
        "- Отправить скриншот или фото\n"
        "- Задать любой вопрос"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    pending_updates.pop(chat_id, None)
    await update.message.reply_text("История очищена.")


async def debug_hubspot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    await update.message.reply_text("Тестирую подключение к HubSpot...")
    if not HUBSPOT_API_KEY:
        await update.message.reply_text("HUBSPOT_API_KEY не установлен!")
        return
    key_preview = HUBSPOT_API_KEY[:8] + "..." + HUBSPOT_API_KEY[-4:]
    result = hubspot_request("POST", "/crm/v3/objects/contacts/search", {"query": "a", "properties": ["firstname"], "limit": 1})
    if "error" in result:
        await update.message.reply_text(f"HubSpot ОШИБКА!\nКлюч: {key_preview}\nОшибка: {result.get('error')} — {result.get('message', '')[:200]}")
    else:
        await update.message.reply_text(f"HubSpot подключен!\nКлюч: {key_preview}\nКонтактов: {result.get('total', '?')}")


async def model_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    await update.message.reply_text(f"Текущая модель: {MODEL}")


async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    global MODEL
    if context.args:
        MODEL = context.args[0]
        await update.message.reply_text(f"Модель изменена: {MODEL}")
    else:
        await update.message.reply_text(
            "Укажи модель:\n/setmodel claude-sonnet-4-20250514\n/setmodel claude-opus-4-20250514\n/setmodel claude-haiku-4-20250506"
        )


async def find_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    if not context.args:
        await update.message.reply_text("Укажи username: /find username")
        return
    username = context.args[0].lstrip("@")
    msg = await update.message.reply_text(f"Ищу {username} в HubSpot...")
    contacts = search_contact_by_telegram(username)
    if not contacts:
        await msg.edit_text(f"Контакт {username} не найден.\nПроверь подключение: /debug")
        return
    for c in contacts:
        props = c.get("properties", {})
        name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or "—"
        stage = LIFECYCLE_STAGES.get(props.get("lifecyclestage", ""), props.get("lifecyclestage", "—"))
        status = LEAD_STATUSES.get(props.get("hs_lead_status", ""), props.get("hs_lead_status", "—"))
        cid = c["id"]
        link = f"https://app.hubspot.com/contacts/47345195/record/0-1/{cid}"
        text = (
            f"<b>{esc(name)}</b>\n"
            f"Email: {esc(props.get('email', '—'))}\n"
            f"Phone: {esc(props.get('phone', '—'))}\n"
            f"Web: {esc(props.get('website', '—'))}\n"
            f"Stage: {esc(stage)}\n"
            f"Status: {esc(status)}\n"
            f'<a href="{link}">Открыть в HubSpot</a>'
        )
        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos and screenshots - download, encode to base64, send to Claude Vision."""
    if not is_allowed(update.effective_user.username):
        return

    chat_id = update.effective_chat.id
    caption = update.message.caption or "Что на этом изображении?"

    # Get the largest photo
    photo = update.message.photo[-1]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        tg_file = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

        logger.info(f"Downloaded photo: {photo.file_id} ({photo.width}x{photo.height})")

        # Read and encode to base64
        with open(tmp_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")

        os.unlink(tmp_path)

        # Determine media type
        media_type = "image/jpeg"

        # Build multimodal message
        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                }
            },
            {
                "type": "text",
                "text": caption,
            }
        ]

        conversations[chat_id].append({"role": "user", "content": user_content})

        if len(conversations[chat_id]) > MAX_HISTORY:
            conversations[chat_id] = conversations[chat_id][-MAX_HISTORY:]

        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=conversations[chat_id],
        )
        reply = response.content[0].text
        conversations[chat_id].append({"role": "assistant", "content": reply})

        hs_update = extract_hubspot_update(reply)
        tg_username = extract_hubspot_contact(reply)
        clean_reply = clean_response(reply)

        if hs_update and tg_username:
            await _send_hubspot_update(update, chat_id, hs_update, tg_username, clean_reply)
        else:
            await _send_reply(update, clean_reply)

    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await update.message.reply_text(f"Ошибка при обработке изображения: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file uploads - download, parse, and send content to Claude."""
    if not is_allowed(update.effective_user.username):
        return

    chat_id = update.effective_chat.id
    doc = update.message.document
    caption = update.message.caption or ""

    if not doc:
        return

    file_name = doc.file_name or "unknown"
    file_size = doc.file_size or 0
    mime_type = doc.mime_type or ""

    if file_size > 10 * 1024 * 1024:
        await update.message.reply_text("Файл слишком большой (макс 10MB).")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        tg_file = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=f"_{file_name}", delete=False) as tmp:
            tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

        logger.info(f"Downloaded file: {file_name} ({file_size} bytes)")

        # Check if it's an image sent as document
        is_image = mime_type.startswith("image/") or file_name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

        if is_image:
            with open(tmp_path, "rb") as f:
                image_data = base64.standard_b64encode(f.read()).decode("utf-8")
            os.unlink(tmp_path)

            # Detect media type
            if file_name.lower().endswith(".png"):
                media_type = "image/png"
            elif file_name.lower().endswith(".gif"):
                media_type = "image/gif"
            elif file_name.lower().endswith(".webp"):
                media_type = "image/webp"
            else:
                media_type = "image/jpeg"

            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                {"type": "text", "text": caption or f"Файл: {file_name}. Что на этом изображении?"}
            ]
        else:
            file_content = parse_file(tmp_path, file_name)
            os.unlink(tmp_path)
            msg_text = f"[FILE: {file_name}]\n{file_content}"
            if caption:
                msg_text += f"\n\n[User message]: {caption}"
            user_content = msg_text

        conversations[chat_id].append({"role": "user", "content": user_content})

        if len(conversations[chat_id]) > MAX_HISTORY:
            conversations[chat_id] = conversations[chat_id][-MAX_HISTORY:]

        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=conversations[chat_id],
        )
        reply = response.content[0].text
        conversations[chat_id].append({"role": "assistant", "content": reply})

        hs_update = extract_hubspot_update(reply)
        tg_username = extract_hubspot_contact(reply)
        clean_reply = clean_response(reply)

        if hs_update and tg_username:
            await _send_hubspot_update(update, chat_id, hs_update, tg_username, clean_reply)
        else:
            await _send_reply(update, clean_reply)

    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await update.message.reply_text(f"Ошибка при обработке файла: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text:
        return

    fwd_username = None
    try:
        if update.message.forward_origin is not None:
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

        hs_update = extract_hubspot_update(reply)
        tg_username = extract_hubspot_contact(reply) or fwd_username
        clean_reply = clean_response(reply)

        if hs_update and tg_username:
            await _send_hubspot_update(update, chat_id, hs_update, tg_username, clean_reply)
        elif hs_update and not tg_username:
            await update.message.reply_text(
                f"{clean_reply}\n\n"
                f"Не могу определить username контакта.\n"
                f'Укажи его: "обнови @username — договорились о звонке"',
            )
        else:
            await _send_reply(update, clean_reply)

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def _send_reply(update, text):
    if len(text) <= 4096:
        await update.message.reply_text(text)
    else:
        for i in range(0, len(text), 4096):
            await update.message.reply_text(text[i:i + 4096])


async def _send_hubspot_update(update, chat_id, hs_update, tg_username, clean_reply):
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
            f"{esc(clean_reply)}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>HubSpot: {esc(name)}</b>\n\n"
            f"Stage: {esc(current_stage)} → <b>{esc(new_stage)}</b>\n"
            f"Status: {esc(current_status)} → <b>{esc(new_status)}</b>\n"
            f"Заметка: {esc(hs_update.get('suggested_note', '—'))}\n\n"
            f"<b>Нажми кнопку:</b>"
        )

        pending_updates[chat_id] = {
            "contact_id": cid,
            "contact_name": name,
            "lifecycle": hs_update.get("suggested_lifecycle"),
            "lead_status": hs_update.get("suggested_lead_status"),
            "note": hs_update.get("suggested_note"),
        }

        keyboard = [
            [InlineKeyboardButton("✅ Обновить всё", callback_data="hs_confirm"), InlineKeyboardButton("❌ Отмена", callback_data="hs_cancel")],
            [InlineKeyboardButton("📝 Только заметку", callback_data="hs_note_only"), InlineKeyboardButton("📊 Только статус", callback_data="hs_status_only")]
        ]

        await update.message.reply_text(update_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(
            f"{clean_reply}\n\nКонтакт {tg_username} не найден в HubSpot.\nПроверь: /find {tg_username}"
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    pending = pending_updates.get(chat_id)
    if not pending:
        await query.edit_message_text("Нет активного обновления.")
        return

    cid = pending["contact_id"]
    name = pending["contact_name"]
    link = f"https://app.hubspot.com/contacts/47345195/record/0-1/{cid}"

    if query.data == "hs_confirm":
        props = {}
        if pending.get("lifecycle"):
            props["lifecyclestage"] = pending["lifecycle"]
        if pending.get("lead_status"):
            props["hs_lead_status"] = pending["lead_status"]
        result = update_contact(cid, props) if props else {"ok": True}
        note_result = add_note_to_contact(cid, pending["note"]) if pending.get("note") else {"ok": True}
        if "error" not in result and "error" not in note_result:
            stage_label = LIFECYCLE_STAGES.get(pending.get("lifecycle", ""), "—")
            status_label = LEAD_STATUSES.get(pending.get("lead_status", ""), "—")
            await query.edit_message_text(
                f"✅ <b>{esc(name)}</b> обновлён!\n\nStage → {esc(stage_label)}\nStatus → {esc(status_label)}\nЗаметка добавлена\n\n"
                f'<a href="{link}">Открыть в HubSpot</a>', parse_mode="HTML", disable_web_page_preview=True)
        else:
            error = result.get("message", "") or note_result.get("message", "")
            await query.edit_message_text(f"Ошибка: {error[:500]}")

    elif query.data == "hs_note_only":
        if pending.get("note"):
            result = add_note_to_contact(cid, pending["note"])
            if "error" not in result:
                await query.edit_message_text(
                    f"📝 Заметка добавлена для <b>{esc(name)}</b>\n\n"
                    f'<a href="{link}">Открыть в HubSpot</a>', parse_mode="HTML", disable_web_page_preview=True)
            else:
                await query.edit_message_text(f"Ошибка: {result.get('message', '')[:500]}")
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
                    f"📊 Статус обновлён: <b>{esc(name)}</b>\n\nStage → {esc(stage_label)}\nStatus → {esc(status_label)}\n\n"
                    f'<a href="{link}">Открыть в HubSpot</a>', parse_mode="HTML", disable_web_page_preview=True)
            else:
                await query.edit_message_text(f"Ошибка: {result.get('message', '')[:500]}")
        else:
            await query.edit_message_text("Нет данных для обновления.")

    elif query.data == "hs_cancel":
        await query.edit_message_text(f"Обновление для {name} отменено.")

    pending_updates.pop(chat_id, None)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("debug", debug_hubspot))
    app.add_handler(CommandHandler("model", model_info))
    app.add_handler(CommandHandler("setmodel", set_model))
    app.add_handler(CommandHandler("find", find_contact))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot started with HubSpot + files + vision. Model: {MODEL}")
    app.run_polling()


if __name__ == "__main__":
    main()
