import os
import re
import json
import logging
import time
import tempfile
import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from anthropic import Anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import urllib.request
import urllib.error
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "50"))

# Google OAuth2
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

client = Anthropic(api_key=ANTHROPIC_API_KEY)
conversations: dict[int, list] = defaultdict(list)
pending_updates: dict[int, dict] = {}

# Cached Google access token
_google_token_cache = {"token": "", "expires": 0}

HUBSPOT_BASE = "https://api.hubapi.com"

LIFECYCLE_STAGES = {
    "subscriber": "Subscriber", "lead": "Lead", "marketingqualifiedlead": "MQL",
    "salesqualifiedlead": "SQL", "opportunity": "Opportunity", "customer": "Customer",
    "evangelist": "Evangelist", "1578580705": "Not interested", "other": "Other",
}

LEAD_STATUSES = {
    "NEW": "New", "OPEN": "Open", "IN_PROGRESS": "In Progress",
    "OPEN_DEAL": "Open Deal", "UNQUALIFIED": "Unqualified",
    "ATTEMPTED_TO_CONTACT": "Attempted to Contact", "CONNECTED": "Connected",
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

== CAPABILITIES ==
You can see: text messages, forwarded messages, files (xlsx, csv, txt), photos/screenshots.

When Daniel asks about his schedule, calendar, meetings — the system will provide Google Calendar data.
When Daniel asks about emails, inbox, messages from someone — the system will provide Gmail data.

== HUBSPOT INTEGRATION ==
When Daniel shares conversations with leads or mentions updating contacts:

Include these tags to suggest CRM updates:
<hubspot_contact>username_here</hubspot_contact>
<hubspot_update>
{
  "summary": "Brief summary",
  "suggested_lifecycle": "salesqualifiedlead",
  "suggested_lead_status": "IN_PROGRESS",
  "suggested_note": "Note text"
}
</hubspot_update>

Valid lifecycle stages: subscriber, lead, marketingqualifiedlead, salesqualifiedlead, opportunity, customer, evangelist, other
Valid lead statuses: NEW, OPEN, IN_PROGRESS, OPEN_DEAL, UNQUALIFIED, ATTEMPTED_TO_CONTACT, CONNECTED, BAD_TIMING

CRITICAL: NEVER say "I updated HubSpot" — you cannot. Only the system can after Daniel clicks ✅.

== CALENDAR INTEGRATION ==
When calendar data is provided in [CALENDAR DATA], analyze it and answer Daniel's questions.
You can suggest creating events by including:
<calendar_create>
{
  "summary": "Event title",
  "start": "2026-03-05T10:00:00-08:00",
  "end": "2026-03-05T11:00:00-08:00",
  "description": "Optional description"
}
</calendar_create>

== GMAIL INTEGRATION ==
When email data is provided in [GMAIL DATA], analyze and summarize.
You can draft replies by including:
<gmail_send>
{
  "to": "email@example.com",
  "subject": "Subject line",
  "body": "Email body text"
}
</gmail_send>
Daniel must confirm before any email is sent.
"""


def esc(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# === GOOGLE AUTH ===

def get_google_token():
    """Get valid Google access token, refreshing if needed."""
    global _google_token_cache
    
    if not GOOGLE_CLIENT_ID or not GOOGLE_REFRESH_TOKEN:
        return None

    now = time.time()
    if _google_token_cache["token"] and _google_token_cache["expires"] > now + 60:
        return _google_token_cache["token"]

    data = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            _google_token_cache["token"] = result["access_token"]
            _google_token_cache["expires"] = now + result.get("expires_in", 3600)
            logger.info("Google token refreshed")
            return result["access_token"]
    except Exception as e:
        logger.error(f"Google token refresh failed: {e}")
        return None


def google_api(method, url, data=None):
    """Call Google API with auto-refreshed token."""
    token = get_google_token()
    if not token:
        return {"error": "Google not configured"}
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Google API error {e.code}: {error_body[:300]}")
        return {"error": e.code, "message": error_body[:300]}
    except Exception as e:
        logger.error(f"Google API failed: {e}")
        return {"error": str(e)}


# === GOOGLE CALENDAR ===

def get_calendar_events(days=1, max_results=15):
    """Get upcoming calendar events."""
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    params = urllib.parse.urlencode({
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    })
    
    result = google_api("GET", f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}")
    
    if "error" in result:
        return None, result.get("message", str(result["error"]))
    
    events = result.get("items", [])
    return events, None


def format_calendar_events(events):
    """Format events for display."""
    if not events:
        return "Нет событий."
    
    lines = []
    for ev in events:
        start = ev.get("start", {})
        end = ev.get("end", {})
        summary = ev.get("summary", "Без названия")
        
        if "dateTime" in start:
            start_dt = start["dateTime"]
            # Parse and format nicely
            try:
                dt = datetime.fromisoformat(start_dt)
                time_str = dt.strftime("%H:%M")
                date_str = dt.strftime("%d.%m")
            except:
                time_str = start_dt
                date_str = ""
            
            end_dt = end.get("dateTime", "")
            try:
                edt = datetime.fromisoformat(end_dt)
                end_time = edt.strftime("%H:%M")
            except:
                end_time = ""
            
            lines.append(f"{date_str} {time_str}-{end_time}  {summary}")
        else:
            # All-day event
            date = start.get("date", "")
            lines.append(f"{date} (весь день)  {summary}")
    
    return "\n".join(lines)


def format_calendar_for_claude(events):
    """Format events as context for Claude."""
    if not events:
        return "[CALENDAR DATA]\nNo upcoming events."
    
    lines = ["[CALENDAR DATA]"]
    for ev in events:
        start = ev.get("start", {})
        end = ev.get("end", {})
        summary = ev.get("summary", "No title")
        desc = ev.get("description", "")
        attendees = [a.get("email", "") for a in ev.get("attendees", [])]
        location = ev.get("location", "")
        
        line = f"- {start.get('dateTime', start.get('date', '?'))}"
        if end.get("dateTime"):
            line += f" to {end['dateTime']}"
        line += f": {summary}"
        if location:
            line += f" (location: {location})"
        if attendees:
            line += f" [attendees: {', '.join(attendees[:5])}]"
        if desc:
            line += f" — {desc[:200]}"
        lines.append(line)
    
    return "\n".join(lines)


def create_calendar_event(summary, start, end, description=""):
    """Create a calendar event."""
    data = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end, "timeZone": "America/Los_Angeles"},
    }
    if description:
        data["description"] = description
    
    return google_api("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events", data)


# === GMAIL ===

def get_emails(query="is:unread", max_results=10):
    """Get emails matching query."""
    params = urllib.parse.urlencode({
        "q": query,
        "maxResults": max_results,
    })
    
    result = google_api("GET", f"https://www.googleapis.com/gmail/v1/users/me/messages?{params}")
    
    if "error" in result:
        return None, result.get("message", str(result["error"]))
    
    messages = result.get("messages", [])
    if not messages:
        return [], None
    
    # Fetch details for each message
    detailed = []
    for msg in messages[:max_results]:
        detail = google_api("GET", f"https://www.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date")
        if "error" not in detail:
            detailed.append(detail)
    
    return detailed, None


def get_email_body(msg_id):
    """Get full email body."""
    result = google_api("GET", f"https://www.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format=full")
    if "error" in result:
        return None
    
    # Extract body
    payload = result.get("payload", {})
    body_text = ""
    
    def extract_text(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            t = extract_text(sub)
            if t:
                return t
        return ""
    
    body_text = extract_text(payload)
    if not body_text and payload.get("body", {}).get("data"):
        body_text = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    
    return body_text[:5000]  # Limit


def format_emails(messages):
    """Format emails for display."""
    if not messages:
        return "Нет писем."
    
    lines = []
    for msg in messages:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        frm = headers.get("From", "?")
        subject = headers.get("Subject", "Без темы")
        date = headers.get("Date", "")
        # Shorten from
        if "<" in frm:
            frm = frm.split("<")[0].strip().strip('"')
        snippet = msg.get("snippet", "")[:80]
        lines.append(f"From: {frm}\nSubject: {subject}\n{snippet}\n")
    
    return "\n".join(lines)


def format_emails_for_claude(messages):
    """Format emails as context for Claude."""
    if not messages:
        return "[GMAIL DATA]\nNo emails found."
    
    lines = ["[GMAIL DATA]"]
    for msg in messages:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        frm = headers.get("From", "?")
        subject = headers.get("Subject", "No subject")
        date = headers.get("Date", "")
        snippet = msg.get("snippet", "")
        msg_id = msg.get("id", "")
        labels = msg.get("labelIds", [])
        
        line = f"- [{date}] From: {frm} | Subject: {subject}"
        if "UNREAD" in labels:
            line += " [UNREAD]"
        line += f"\n  Preview: {snippet[:200]}"
        line += f"\n  ID: {msg_id}"
        lines.append(line)
    
    return "\n".join(lines)


def send_email(to, subject, body):
    """Send an email via Gmail API."""
    import email.mime.text
    
    msg = email.mime.text.MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    
    return google_api("POST", "https://www.googleapis.com/gmail/v1/users/me/messages/send", {"raw": raw})


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
        return text[:30000] if len(text) > 30000 else text
    except Exception as e:
        return f"[Error reading xlsx: {e}]"


def parse_file(file_path, file_name):
    name_lower = file_name.lower()
    if name_lower.endswith((".xlsx", ".xls")):
        return parse_xlsx(file_path)
    elif name_lower.endswith((".csv", ".txt", ".md", ".json", ".py", ".js", ".html")):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(50000)
        except Exception as e:
            return f"[Error: {e}]"
    return f"[Unsupported: {file_name}]"


# === HUBSPOT API ===

def hubspot_request(method, endpoint, data=None):
    url = f"{HUBSPOT_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"HubSpot error {e.code}: {error_body[:300]}")
        return {"error": e.code, "message": error_body[:300]}
    except Exception as e:
        return {"error": str(e)}


def search_contact_by_telegram(tg_username):
    tg_username_clean = tg_username.lower().strip().lstrip("@")
    props = ["firstname", "lastname", "email", "phone", "lifecyclestage", "hs_lead_status", "website"]
    
    for search in [
        {"filterGroups": [{"filters": [{"propertyName": "website", "operator": "CONTAINS_TOKEN", "value": tg_username_clean}]}], "properties": props, "limit": 5},
        {"filterGroups": [{"filters": [{"propertyName": "website", "operator": "EQ", "value": f"https://t.me/{tg_username_clean}"}]}], "properties": props, "limit": 5},
        {"query": tg_username_clean, "properties": props, "limit": 5},
    ]:
        result = hubspot_request("POST", "/crm/v3/objects/contacts/search", search)
        if result and "results" in result and result["results"]:
            return result["results"]
    
    if "_" in tg_username_clean:
        result = hubspot_request("POST", "/crm/v3/objects/contacts/search", {"query": tg_username_clean.replace("_", " "), "properties": props, "limit": 5})
        if result and "results" in result and result["results"]:
            return result["results"]
    return []


def update_contact(contact_id, properties):
    return hubspot_request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties})


def add_note_to_contact(contact_id, note_text):
    return hubspot_request("POST", "/crm/v3/objects/notes", {
        "properties": {"hs_note_body": note_text, "hs_timestamp": str(int(time.time() * 1000))},
        "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]
    })


def extract_hubspot_update(text):
    match = re.search(r"<hubspot_update>\s*(.*?)\s*</hubspot_update>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    return None

def extract_hubspot_contact(text):
    match = re.search(r"<hubspot_contact>\s*(.*?)\s*</hubspot_contact>", text, re.DOTALL)
    return match.group(1).strip().lstrip("@") if match else None

def extract_calendar_create(text):
    match = re.search(r"<calendar_create>\s*(.*?)\s*</calendar_create>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    return None

def extract_gmail_send(text):
    match = re.search(r"<gmail_send>\s*(.*?)\s*</gmail_send>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    return None

def clean_response(text):
    for tag in ["hubspot_update", "hubspot_contact", "calendar_create", "gmail_send"]:
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)
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
    google_status = "подключен" if GOOGLE_REFRESH_TOKEN else "не настроен"
    await update.message.reply_text(
        "Привет, Даниил! AI-ассистент с HubSpot, Calendar и Gmail.\n\n"
        "Команды:\n"
        "/cal — расписание на сегодня\n"
        "/cal3 — расписание на 3 дня\n"
        "/mail — непрочитанные письма\n"
        "/find username — найти контакт в HubSpot\n"
        "/debug — проверить подключения\n"
        "/reset — очистить историю\n"
        "/model — текущая модель\n\n"
        f"Google: {google_status}"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    conversations[update.effective_chat.id] = []
    pending_updates.pop(update.effective_chat.id, None)
    await update.message.reply_text("История очищена.")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    lines = []
    
    # HubSpot
    if HUBSPOT_API_KEY:
        result = hubspot_request("POST", "/crm/v3/objects/contacts/search", {"query": "a", "properties": ["firstname"], "limit": 1})
        if "error" in result:
            lines.append(f"HubSpot: ОШИБКА — {result.get('error')}")
        else:
            lines.append(f"HubSpot: OK ({result.get('total', '?')} контактов)")
    else:
        lines.append("HubSpot: не настроен")
    
    # Google
    if GOOGLE_REFRESH_TOKEN:
        token = get_google_token()
        if token:
            events, err = get_calendar_events(days=1, max_results=1)
            if err:
                lines.append(f"Google Calendar: ОШИБКА — {err[:100]}")
            else:
                lines.append(f"Google Calendar: OK")
            
            emails, err = get_emails("is:unread", max_results=1)
            if err:
                lines.append(f"Gmail: ОШИБКА — {err[:100]}")
            else:
                lines.append(f"Gmail: OK")
        else:
            lines.append("Google: ошибка токена")
    else:
        lines.append("Google: не настроен")
    
    await update.message.reply_text("\n".join(lines))


async def calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    if not GOOGLE_REFRESH_TOKEN:
        await update.message.reply_text("Google Calendar не настроен. Нужно добавить GOOGLE_* переменные в Railway.")
        return
    
    days = 1
    cmd_text = update.message.text.strip()
    if cmd_text.startswith("/cal"):
        num = cmd_text[4:].strip()
        if num.isdigit():
            days = min(int(num), 14)
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    events, err = get_calendar_events(days=days, max_results=20)
    
    if err:
        await update.message.reply_text(f"Ошибка Calendar: {err[:200]}")
        return
    
    if not events:
        await update.message.reply_text(f"Нет событий на {'сегодня' if days == 1 else f'ближайшие {days} дней'}.")
        return
    
    text = f"Расписание ({days} {'день' if days == 1 else 'дней'}):\n\n{format_calendar_events(events)}"
    await update.message.reply_text(text)


async def mail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    if not GOOGLE_REFRESH_TOKEN:
        await update.message.reply_text("Gmail не настроен. Нужно добавить GOOGLE_* переменные в Railway.")
        return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    query = "is:unread"
    if context.args:
        query = " ".join(context.args)
    
    emails, err = get_emails(query, max_results=10)
    
    if err:
        await update.message.reply_text(f"Ошибка Gmail: {err[:200]}")
        return
    
    if not emails:
        await update.message.reply_text("Нет непрочитанных писем.")
        return
    
    text = f"Письма ({query}):\n\n{format_emails(emails)}"
    if len(text) > 4096:
        text = text[:4093] + "..."
    await update.message.reply_text(text)


async def model_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    await update.message.reply_text(f"Модель: {MODEL}")

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    global MODEL
    if context.args:
        MODEL = context.args[0]
        await update.message.reply_text(f"Модель: {MODEL}")
    else:
        await update.message.reply_text("/setmodel claude-sonnet-4-20250514\n/setmodel claude-opus-4-20250514")


async def find_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    if not context.args:
        await update.message.reply_text("Укажи username: /find username")
        return
    username = context.args[0].lstrip("@")
    msg = await update.message.reply_text(f"Ищу {username}...")
    contacts = search_contact_by_telegram(username)
    if not contacts:
        await msg.edit_text(f"Контакт {username} не найден. /debug")
        return
    for c in contacts:
        props = c.get("properties", {})
        name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or "—"
        cid = c["id"]
        link = f"https://app.hubspot.com/contacts/47345195/record/0-1/{cid}"
        stage = LIFECYCLE_STAGES.get(props.get("lifecyclestage", ""), "—")
        status = LEAD_STATUSES.get(props.get("hs_lead_status", ""), "—")
        text = (
            f"<b>{esc(name)}</b>\n"
            f"Email: {esc(props.get('email', '—'))}\n"
            f"Web: {esc(props.get('website', '—'))}\n"
            f"Stage: {esc(stage)} | Status: {esc(status)}\n"
            f'<a href="{link}">HubSpot</a>'
        )
        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    chat_id = update.effective_chat.id
    caption = update.message.caption or "Что на этом изображении?"
    photo = update.message.photo[-1]
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        tg_file = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")
        os.unlink(tmp_path)

        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
            {"type": "text", "text": caption}
        ]
        await _process_message(update, chat_id, user_content)
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username):
        return
    chat_id = update.effective_chat.id
    doc = update.message.document
    if not doc:
        return
    file_name = doc.file_name or "unknown"
    mime_type = doc.mime_type or ""
    caption = update.message.caption or ""
    if (doc.file_size or 0) > 10 * 1024 * 1024:
        await update.message.reply_text("Файл > 10MB.")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        tg_file = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=f"_{file_name}", delete=False) as tmp:
            tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

        is_image = mime_type.startswith("image/") or file_name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        if is_image:
            with open(tmp_path, "rb") as f:
                image_data = base64.standard_b64encode(f.read()).decode("utf-8")
            os.unlink(tmp_path)
            mt = "image/png" if file_name.lower().endswith(".png") else "image/jpeg"
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": mt, "data": image_data}},
                {"type": "text", "text": caption or file_name}
            ]
        else:
            file_content = parse_file(tmp_path, file_name)
            os.unlink(tmp_path)
            user_content = f"[FILE: {file_name}]\n{file_content}" + (f"\n\n{caption}" if caption else "")

        await _process_message(update, chat_id, user_content)
    except Exception as e:
        logger.error(f"Doc error: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


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
    except:
        pass

    # Auto-fetch calendar/email context if question seems related
    extra_context = ""
    text_lower = user_text.lower()
    
    calendar_keywords = ["календар", "расписан", "встреч", "событ", "schedule", "calendar", "meeting", "сегодня план", "что у меня", "свободен", "занят"]
    email_keywords = ["почт", "письм", "email", "mail", "inbox", "gmail", "от кого", "непрочитан", "написал"]
    
    if GOOGLE_REFRESH_TOKEN:
        if any(kw in text_lower for kw in calendar_keywords):
            events, err = get_calendar_events(days=3, max_results=15)
            if events:
                extra_context += "\n\n" + format_calendar_for_claude(events)
        
        if any(kw in text_lower for kw in email_keywords):
            emails, err = get_emails("is:unread", max_results=10)
            if emails:
                extra_context += "\n\n" + format_emails_for_claude(emails)

    msg_content = user_text + extra_context
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    await _process_message(update, chat_id, msg_content, fwd_username=fwd_username)


async def _process_message(update, chat_id, content, fwd_username=None):
    """Process message through Claude and handle response."""
    conversations[chat_id].append({"role": "user", "content": content})
    if len(conversations[chat_id]) > MAX_HISTORY:
        conversations[chat_id] = conversations[chat_id][-MAX_HISTORY:]

    try:
        response = client.messages.create(
            model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT,
            messages=conversations[chat_id],
        )
        reply = response.content[0].text
        conversations[chat_id].append({"role": "assistant", "content": reply})

        # Extract all possible actions
        hs_update = extract_hubspot_update(reply)
        tg_username = extract_hubspot_contact(reply) or fwd_username
        cal_create = extract_calendar_create(reply)
        email_send = extract_gmail_send(reply)
        clean_reply = clean_response(reply)

        # HubSpot update
        if hs_update and tg_username:
            await _send_hubspot_update(update, chat_id, hs_update, tg_username, clean_reply)
            return

        # Calendar event creation
        if cal_create:
            pending_updates[chat_id] = {"type": "calendar", "data": cal_create}
            keyboard = [[
                InlineKeyboardButton("✅ Создать", callback_data="cal_confirm"),
                InlineKeyboardButton("❌ Отмена", callback_data="cal_cancel"),
            ]]
            await update.message.reply_text(
                f"{esc(clean_reply)}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"<b>Создать событие:</b>\n"
                f"{esc(cal_create.get('summary', ''))}\n"
                f"{esc(cal_create.get('start', ''))} — {esc(cal_create.get('end', ''))}\n",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # Email send
        if email_send:
            pending_updates[chat_id] = {"type": "email", "data": email_send}
            keyboard = [[
                InlineKeyboardButton("✅ Отправить", callback_data="email_confirm"),
                InlineKeyboardButton("❌ Отмена", callback_data="email_cancel"),
            ]]
            await update.message.reply_text(
                f"{esc(clean_reply)}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"<b>Отправить письмо:</b>\n"
                f"To: {esc(email_send.get('to', ''))}\n"
                f"Subject: {esc(email_send.get('subject', ''))}\n"
                f"Body: {esc(email_send.get('body', '')[:200])}...\n",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # Regular reply
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

        pending_updates[chat_id] = {
            "type": "hubspot", "contact_id": cid, "contact_name": name,
            "lifecycle": hs_update.get("suggested_lifecycle"),
            "lead_status": hs_update.get("suggested_lead_status"),
            "note": hs_update.get("suggested_note"),
        }
        keyboard = [
            [InlineKeyboardButton("✅ Обновить всё", callback_data="hs_confirm"), InlineKeyboardButton("❌ Отмена", callback_data="hs_cancel")],
            [InlineKeyboardButton("📝 Заметку", callback_data="hs_note_only"), InlineKeyboardButton("📊 Статус", callback_data="hs_status_only")]
        ]
        await update.message.reply_text(
            f"{esc(clean_reply)}\n\n━━━━━━━━━━━━━━━\n"
            f"<b>HubSpot: {esc(name)}</b>\n\n"
            f"Stage: {esc(current_stage)} → <b>{esc(new_stage)}</b>\n"
            f"Status: {esc(current_status)} → <b>{esc(new_status)}</b>\n"
            f"Заметка: {esc(hs_update.get('suggested_note', '—'))}\n\n<b>Нажми кнопку:</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(f"{clean_reply}\n\nКонтакт {tg_username} не найден. /find {tg_username}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    pending = pending_updates.get(chat_id)
    if not pending:
        await query.edit_message_text("Нет активного действия.")
        return

    action_type = pending.get("type", "hubspot")

    # === HUBSPOT CALLBACKS ===
    if action_type == "hubspot":
        cid = pending["contact_id"]
        name = pending["contact_name"]
        link = f"https://app.hubspot.com/contacts/47345195/record/0-1/{cid}"

        if query.data == "hs_confirm":
            props = {}
            if pending.get("lifecycle"): props["lifecyclestage"] = pending["lifecycle"]
            if pending.get("lead_status"): props["hs_lead_status"] = pending["lead_status"]
            result = update_contact(cid, props) if props else {"ok": True}
            note_result = add_note_to_contact(cid, pending["note"]) if pending.get("note") else {"ok": True}
            if "error" not in result and "error" not in note_result:
                await query.edit_message_text(
                    f"✅ <b>{esc(name)}</b> обновлён!\n\n"
                    f"Stage → {esc(LIFECYCLE_STAGES.get(pending.get('lifecycle', ''), '—'))}\n"
                    f"Status → {esc(LEAD_STATUSES.get(pending.get('lead_status', ''), '—'))}\n"
                    f"Заметка добавлена\n\n<a href=\"{link}\">HubSpot</a>",
                    parse_mode="HTML", disable_web_page_preview=True)
            else:
                await query.edit_message_text(f"Ошибка: {(result.get('message', '') or note_result.get('message', ''))[:500]}")

        elif query.data == "hs_note_only":
            if pending.get("note"):
                result = add_note_to_contact(cid, pending["note"])
                if "error" not in result:
                    await query.edit_message_text(f"📝 Заметка добавлена для <b>{esc(name)}</b>\n<a href=\"{link}\">HubSpot</a>", parse_mode="HTML", disable_web_page_preview=True)
                else:
                    await query.edit_message_text(f"Ошибка: {result.get('message', '')[:500]}")
            else:
                await query.edit_message_text("Нет заметки.")

        elif query.data == "hs_status_only":
            props = {}
            if pending.get("lifecycle"): props["lifecyclestage"] = pending["lifecycle"]
            if pending.get("lead_status"): props["hs_lead_status"] = pending["lead_status"]
            if props:
                result = update_contact(cid, props)
                if "error" not in result:
                    await query.edit_message_text(
                        f"📊 <b>{esc(name)}</b> обновлён\n"
                        f"Stage → {esc(LIFECYCLE_STAGES.get(pending.get('lifecycle', ''), '—'))}\n"
                        f"Status → {esc(LEAD_STATUSES.get(pending.get('lead_status', ''), '—'))}\n"
                        f"<a href=\"{link}\">HubSpot</a>", parse_mode="HTML", disable_web_page_preview=True)
                else:
                    await query.edit_message_text(f"Ошибка: {result.get('message', '')[:500]}")

        elif query.data == "hs_cancel":
            await query.edit_message_text(f"Отменено для {name}.")

    # === CALENDAR CALLBACKS ===
    elif action_type == "calendar":
        if query.data == "cal_confirm":
            d = pending["data"]
            result = create_calendar_event(d["summary"], d["start"], d["end"], d.get("description", ""))
            if "error" not in result:
                await query.edit_message_text(f"✅ Событие создано: {d['summary']}")
            else:
                await query.edit_message_text(f"Ошибка: {result.get('message', '')[:300]}")
        elif query.data == "cal_cancel":
            await query.edit_message_text("Создание события отменено.")

    # === EMAIL CALLBACKS ===
    elif action_type == "email":
        if query.data == "email_confirm":
            d = pending["data"]
            result = send_email(d["to"], d["subject"], d["body"])
            if "error" not in result:
                await query.edit_message_text(f"✅ Письмо отправлено: {d['to']}")
            else:
                await query.edit_message_text(f"Ошибка: {result.get('message', '')[:300]}")
        elif query.data == "email_cancel":
            await query.edit_message_text("Отправка отменена.")

    pending_updates.pop(chat_id, None)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("cal", calendar_cmd))
    app.add_handler(CommandHandler("mail", mail_cmd))
    app.add_handler(CommandHandler("model", model_info))
    app.add_handler(CommandHandler("setmodel", set_model))
    app.add_handler(CommandHandler("find", find_contact))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot started. Model: {MODEL}")
    google_ok = "YES" if GOOGLE_REFRESH_TOKEN else "NO"
    logger.info(f"Google: {google_ok} | HubSpot: {'YES' if HUBSPOT_API_KEY else 'NO'}")
    app.run_polling()


if __name__ == "__main__":
    main()
