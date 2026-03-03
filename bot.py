import os
import re
import json
import logging
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
pending_updates: dict[int, dict] = {}  # chat_id -> pending hubspot update data

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

== INTERESTS & TOOLS ==
- Technology, gaming (Steam, Battle.net), filmmaking and content creation, cooking, outdoor exploration.
- Uses DJI equipment, OBS Studio, Adobe Photoshop for content creation.
- Drives a 2017 Mini Cooper F56 base model.

== COMMUNICATION STYLE ==
- Direct communication without filler words, marketing language, or unnecessary validation.
- Factual accuracy and straightforward responses.
- When data is unavailable, clear acknowledgment rather than speculation.
- Responds in the same language Daniel writes in.
- Keep messages concise for Telegram.

== CURRENT BUSINESS CONTEXT ==
- VisaNow.AI HubSpot has 362 total contacts, 224 active.
- Main communication with leads is via Telegram.
- Daniel manages CRM himself.

== HUBSPOT INTEGRATION ==
You have access to HubSpot CRM. When Daniel forwards a conversation with a lead:
1. Analyze the conversation to understand the outcome.
2. The system will try to find the contact by Telegram username.
3. Suggest what to update in HubSpot (lifecycle stage, lead status, notes).
4. Execute the update when Daniel confirms.

When analyzing forwarded messages, extract:
- Key discussion points
- Any agreements or next steps
- Suggested lifecycle stage change
- Suggested lead status change
- A brief note summarizing the conversation

Always respond with a clear summary and proposed CRM updates in this exact JSON format wrapped in <hubspot_update> tags:
<hubspot_update>
{
  "summary": "Brief conversation summary",
  "suggested_lifecycle": "salesqualifiedlead",
  "suggested_lead_status": "IN_PROGRESS",
  "suggested_note": "Note text for HubSpot",
  "confidence": "high/medium/low"
}
</hubspot_update>

If the message is NOT a forwarded conversation or not related to a lead, just respond normally without the tags.
"""


# === HUBSPOT API FUNCTIONS ===

def hubspot_request(method, endpoint, data=None):
    """Make a request to HubSpot API."""
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
    """Search HubSpot contact by Telegram username in the website field."""
    # Try multiple variations of the username
    variations = [
        f"t.me/{tg_username}",
        f"https://t.me/{tg_username}",
        f"http://t.me/{tg_username}",
        tg_username,
    ]
    
    for query in variations:
        data = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "website",
                    "operator": "CONTAINS_TOKEN",
                    "value": tg_username.lower(),
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
    
    # Fallback: search by fulltext
    data = {
        "query": tg_username,
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
    """Update a HubSpot contact's properties."""
    data = {"properties": properties}
    return hubspot_request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", data)


def add_note_to_contact(contact_id, note_text):
    """Add a note to a HubSpot contact."""
    data = {
        "properties": {
            "hs_note_body": note_text,
            "hs_timestamp": str(int(__import__("time").time() * 1000)),
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


def extract_telegram_username(text):
    """Extract Telegram username from forwarded message or text."""
    # Pattern: @username or t.me/username
    patterns = [
        r"@(\w{5,32})",
        r"t\.me/(\w{5,32})",
        r"From[:\s]+(\w+)",
    ]
    # Check "Forwarded from" pattern first
    fwd_match = re.search(r"(?:Forwarded from|Forward from|Переслано от)\s+(.+?)(?:\n|$)", text)
    if fwd_match:
        name = fwd_match.group(1).strip()
        return name
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def extract_hubspot_update(text):
    """Extract HubSpot update suggestion from Claude's response."""
    match = re.search(r"<hubspot_update>\s*(.*?)\s*</hubspot_update>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def clean_response(text):
    """Remove hubspot_update tags from response for display."""
    return re.sub(r"<hubspot_update>.*?</hubspot_update>", "", text, flags=re.DOTALL).strip()


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
        "Привет, Даниил! Я твой AI-ассистент с HubSpot интеграцией.\n\n"
        "📋 Команды:\n"
        "/reset — очистить историю диалога\n"
        "/model — текущая модель\n"
        "/setmodel <model> — сменить модель\n"
        "/find <username> — найти контакт в HubSpot\n\n"
        "💡 Перешли мне переписку с клиентом — я проанализирую и предложу обновления в HubSpot."
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
    """Find a contact in HubSpot by Telegram username."""
    if not is_allowed(update.effective_user.username):
        return
    if not context.args:
        await update.message.reply_text("Укажи username: `/find username`", parse_mode="Markdown")
        return
    
    username = context.args[0].lstrip("@")
    await update.message.reply_text(f"🔍 Ищу {username} в HubSpot...")
    
    contacts = search_contact_by_telegram(username)
    if not contacts:
        await update.message.reply_text(f"Контакт с Telegram @{username} не найден в HubSpot.")
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
            f"👤 *{name}*\n"
            f"ID: {cid}\n"
            f"📧 {email}\n"
            f"📱 {phone}\n"
            f"🌐 {website}\n"
            f"📊 Stage: {stage}\n"
            f"🏷 Status: {status}\n"
            f"🔗 [Открыть в HubSpot](https://app.hubspot.com/contacts/47345195/record/0-1/{cid})"
        )
        await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text:
        return

    # Check if this is a forwarded message
    is_forwarded = update.message.forward_origin is not None or update.message.forward_date is not None
    
    # Build message with forwarding context
    if is_forwarded:
        fwd_name = ""
        if hasattr(update.message, "forward_origin") and update.message.forward_origin:
            origin = update.message.forward_origin
            if hasattr(origin, "sender_user") and origin.sender_user:
                u = origin.sender_user
                fwd_name = f"{u.first_name or ''} {u.last_name or ''}".strip()
                if u.username:
                    fwd_name += f" (@{u.username})"
            elif hasattr(origin, "sender_user_name"):
                fwd_name = origin.sender_user_name or ""
        
        msg_content = f"[FORWARDED MESSAGE from {fwd_name}]\n{user_text}"
    else:
        msg_content = user_text

    conversations[chat_id].append({"role": "user", "content": msg_content})

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
        clean_reply = clean_response(reply)

        if hs_update and is_forwarded:
            # Try to find the contact
            tg_username = None
            if hasattr(update.message, "forward_origin") and update.message.forward_origin:
                origin = update.message.forward_origin
                if hasattr(origin, "sender_user") and origin.sender_user and origin.sender_user.username:
                    tg_username = origin.sender_user.username
            
            if not tg_username:
                tg_username = extract_telegram_username(user_text)

            contacts = []
            if tg_username:
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
                    f"📋 *HubSpot обновление для {name}*\n\n"
                    f"📊 Stage: {current_stage} → *{new_stage}*\n"
                    f"🏷 Status: {current_status} → *{new_status}*\n"
                    f"📝 Заметка: {hs_update.get('suggested_note', '—')}\n"
                )
                
                # Store pending update
                pending_updates[chat_id] = {
                    "contact_id": cid,
                    "contact_name": name,
                    "lifecycle": hs_update.get("suggested_lifecycle"),
                    "lead_status": hs_update.get("suggested_lead_status"),
                    "note": hs_update.get("suggested_note"),
                }
                
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Обновить", callback_data="hs_confirm"),
                        InlineKeyboardButton("❌ Отмена", callback_data="hs_cancel"),
                    ],
                    [
                        InlineKeyboardButton("📝 Только заметку", callback_data="hs_note_only"),
                    ]
                ]
                
                await update.message.reply_text(
                    update_text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                # Contact not found
                search_info = f" (@{tg_username})" if tg_username else ""
                await update.message.reply_text(
                    f"{clean_reply}\n\n"
                    f"⚠️ Контакт{search_info} не найден в HubSpot. "
                    f"Можешь найти вручную: /find username",
                )
        else:
            # Regular message, no HubSpot update
            if len(clean_reply) <= 4096:
                await update.message.reply_text(clean_reply)
            else:
                for i in range(0, len(clean_reply), 4096):
                    await update.message.reply_text(clean_reply[i:i + 4096])

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    pending = pending_updates.get(chat_id)
    
    if not pending:
        await query.edit_message_text("⚠️ Нет активного обновления. Перешли переписку заново.")
        return
    
    cid = pending["contact_id"]
    name = pending["contact_name"]
    
    if query.data == "hs_confirm":
        # Update lifecycle stage and lead status
        props = {}
        if pending.get("lifecycle"):
            props["lifecyclestage"] = pending["lifecycle"]
        if pending.get("lead_status"):
            props["hs_lead_status"] = pending["lead_status"]
        
        result = update_contact(cid, props) if props else {}
        note_result = {}
        if pending.get("note"):
            note_result = add_note_to_contact(cid, pending["note"])
        
        if "error" not in result and "error" not in note_result:
            await query.edit_message_text(
                f"✅ *{name}* обновлён в HubSpot!\n\n"
                f"📊 Stage → {LIFECYCLE_STAGES.get(pending.get('lifecycle', ''), '—')}\n"
                f"🏷 Status → {LEAD_STATUSES.get(pending.get('lead_status', ''), '—')}\n"
                f"📝 Заметка добавлена\n\n"
                f"🔗 [Открыть в HubSpot](https://app.hubspot.com/contacts/47345195/record/0-1/{cid})",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            error = result.get("message", "") or note_result.get("message", "")
            await query.edit_message_text(f"❌ Ошибка обновления: {error}")
    
    elif query.data == "hs_note_only":
        if pending.get("note"):
            result = add_note_to_contact(cid, pending["note"])
            if "error" not in result:
                await query.edit_message_text(
                    f"📝 Заметка добавлена для *{name}*\n\n"
                    f"🔗 [Открыть в HubSpot](https://app.hubspot.com/contacts/47345195/record/0-1/{cid})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await query.edit_message_text(f"❌ Ошибка: {result.get('message', '')}")
        else:
            await query.edit_message_text("Нет заметки для добавления.")
    
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
