import os
import re
import json
import logging
import time
import asyncio
import tempfile
import base64
import email.mime.text
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from anthropic import Anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import urllib.request
import urllib.error
import urllib.parse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "50"))

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

client = Anthropic(api_key=ANTHROPIC_API_KEY)
conversations: dict[int, list] = defaultdict(list)
pending_updates: dict[int, dict] = {}
scheduled_jobs: dict[int, list] = defaultdict(list)
scheduler = AsyncIOScheduler()
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
- CEO of VisaNow.AI — AI-based legal immigration services. HubSpot CRM is actively used.
- Founder/CEO of Delfast — electric bicycle company, Guinness World Record holder.
- Founder of Core Element AI — AI for geological exploration.
- Co-founder of FilmArtMovies. Founder of In Charge One, Inc.

== COMMUNICATION STYLE ==
- Direct, no filler words. Responds in the same language Daniel writes in.
- Keep messages concise for Telegram.
- NEVER suggest the user to visit websites, open apps, or do anything manually. You are here to automate tasks.
- If you cannot do something directly, explain HOW to make you capable of it (what code change or integration is needed), not what the user should do themselves.
- Never end responses with "you can check...", "I recommend visiting...", "you can use..." type phrases.
- If asked to fetch data from a site and it fails, try alternative methods (different URL, API endpoint, search) before giving up.
- Respond to the question you've been asked. For example if you've been asked about the news from the website — answer with the news from the website and don't describe the website itself.

== CAPABILITIES ==
Text, forwarded messages, files (xlsx, csv, txt), photos/screenshots, calendar, email, Google Drive.

== HUBSPOT ==
For CRM updates, include these tags:
<hubspot_contact>username</hubspot_contact>
<hubspot_update>{"summary":"...","suggested_lifecycle":"...","suggested_lead_status":"...","suggested_note":"..."}</hubspot_update>

Lifecycle: subscriber, lead, marketingqualifiedlead, salesqualifiedlead, opportunity, customer, evangelist, other
Lead status: NEW, OPEN, IN_PROGRESS, OPEN_DEAL, UNQUALIFIED, ATTEMPTED_TO_CONTACT, CONNECTED, BAD_TIMING
NEVER say "I updated HubSpot" — only the system can after button click.

== CALENDAR ==
When [CALENDAR DATA] is provided, analyze and answer. To create events:
<calendar_create>{"summary":"...","start":"2026-03-05T10:00:00-08:00","end":"2026-03-05T11:00:00-08:00","description":"..."}</calendar_create>

== GMAIL ==
When [GMAIL DATA] is provided, analyze and summarize. To send:
<gmail_send>{"to":"...","subject":"...","body":"..."}</gmail_send>
To save as draft (use when email address is unknown or user wants to review first):
<gmail_draft>{"to":"...","subject":"...","body":"..."}</gmail_draft>
Daniel must confirm before sending. For drafts, "to" can be empty string if address unknown.
Daniel must confirm before sending.

== GOOGLE DRIVE ==
When [DRIVE DATA] is provided, analyze the files/content. You can see file names, types, and content when provided.
"""


def esc(text):
    if not text: return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# === GOOGLE AUTH ===

def get_google_token():
    global _google_token_cache
    if not GOOGLE_CLIENT_ID or not GOOGLE_REFRESH_TOKEN: return None
    now = time.time()
    if _google_token_cache["token"] and _google_token_cache["expires"] > now + 60:
        return _google_token_cache["token"]
    data = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN, "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            _google_token_cache["token"] = result["access_token"]
            _google_token_cache["expires"] = now + result.get("expires_in", 3600)
            return result["access_token"]
    except Exception as e:
        logger.error(f"Google token refresh failed: {e}")
        return None


def google_api(method, url, data=None):
    token = get_google_token()
    if not token: return {"error": "Google not configured"}
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
        return {"error": str(e)}


# === CALENDAR ===

def get_calendar_events(days=1, max_results=15):
    now = datetime.now(timezone.utc)
    params = urllib.parse.urlencode({
        "timeMin": now.isoformat(), "timeMax": (now + timedelta(days=days)).isoformat(),
        "maxResults": max_results, "singleEvents": "true", "orderBy": "startTime",
    })
    result = google_api("GET", f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}")
    if "error" in result: return None, result.get("message", str(result["error"]))
    return result.get("items", []), None


def format_calendar_events(events):
    if not events: return "Нет событий."
    lines = []
    for ev in events:
        start = ev.get("start", {})
        end = ev.get("end", {})
        summary = ev.get("summary", "Без названия")
        if "dateTime" in start:
            try:
                dt = datetime.fromisoformat(start["dateTime"])
                edt = datetime.fromisoformat(end.get("dateTime", ""))
                lines.append(f"{dt.strftime('%d.%m %H:%M')}-{edt.strftime('%H:%M')}  {summary}")
            except:
                lines.append(f"{start['dateTime']}  {summary}")
        else:
            lines.append(f"{start.get('date', '')} (весь день)  {summary}")
    return "\n".join(lines)


def format_calendar_for_claude(events):
    if not events: return "[CALENDAR DATA]\nNo upcoming events."
    lines = ["[CALENDAR DATA]"]
    for ev in events:
        start = ev.get("start", {})
        end = ev.get("end", {})
        s = ev.get("summary", "No title")
        att = [a.get("email", "") for a in ev.get("attendees", [])]
        loc = ev.get("location", "")
        line = f"- {start.get('dateTime', start.get('date', '?'))}: {s}"
        if loc: line += f" @ {loc}"
        if att: line += f" [{', '.join(att[:5])}]"
        lines.append(line)
    return "\n".join(lines)


def create_calendar_event(summary, start, end, description=""):
    data = {"summary": summary, "start": {"dateTime": start, "timeZone": "America/Los_Angeles"}, "end": {"dateTime": end, "timeZone": "America/Los_Angeles"}}
    if description: data["description"] = description
    return google_api("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events", data)


# === GMAIL ===

def get_emails(query="is:unread", max_results=10):
    params = urllib.parse.urlencode({"q": query, "maxResults": max_results})
    result = google_api("GET", f"https://www.googleapis.com/gmail/v1/users/me/messages?{params}")
    if "error" in result: return None, result.get("message", str(result["error"]))
    messages = result.get("messages", [])
    if not messages: return [], None
    detailed = []
    for msg in messages[:max_results]:
        detail = google_api("GET", f"https://www.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date")
        if "error" not in detail: detailed.append(detail)
    return detailed, None


def format_emails(messages):
    if not messages: return "Нет писем."
    lines = []
    for msg in messages:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        frm = headers.get("From", "?")
        if "<" in frm: frm = frm.split("<")[0].strip().strip('"')
        lines.append(f"From: {frm}\nSubject: {headers.get('Subject', '—')}\n{msg.get('snippet', '')[:80]}\n")
    return "\n".join(lines)


def format_emails_for_claude(messages):
    if not messages: return "[GMAIL DATA]\nNo emails found."
    lines = ["[GMAIL DATA]"]
    for msg in messages:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        line = f"- [{headers.get('Date', '')}] From: {headers.get('From', '?')} | Subject: {headers.get('Subject', '—')}"
        if "UNREAD" in msg.get("labelIds", []): line += " [UNREAD]"
        line += f"\n  {msg.get('snippet', '')[:200]}"
        lines.append(line)
    return "\n".join(lines)


def send_email(to, subject, body):
    msg = email.mime.text.MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return google_api("POST", "https://www.googleapis.com/gmail/v1/users/me/messages/send", {"raw": raw})


def save_draft(to, subject, body):
    msg = email.mime.text.MIMEText(body)
    if to: msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return google_api("POST", "https://www.googleapis.com/gmail/v1/users/me/drafts", {"message": {"raw": raw}})


# === GOOGLE DRIVE ===

def drive_search(query, max_results=10):
    params = urllib.parse.urlencode({
        "q": f"name contains '{query}' and trashed = false",
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink,size)",
        "pageSize": max_results,
        "orderBy": "modifiedTime desc",
    })
    result = google_api("GET", f"https://www.googleapis.com/drive/v3/files?{params}")
    if "error" in result:
        return None, result.get("message", str(result["error"]))
    return result.get("files", []), None


def drive_list_recent(max_results=10):
    params = urllib.parse.urlencode({
        "q": "trashed = false",
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink,size)",
        "pageSize": max_results,
        "orderBy": "modifiedTime desc",
    })
    result = google_api("GET", f"https://www.googleapis.com/drive/v3/files?{params}")
    if "error" in result:
        return None, result.get("message", str(result["error"]))
    return result.get("files", []), None


def drive_get_content(file_id, mime_type):
    if "google-apps.document" in mime_type:
        result = google_api("GET", f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/plain")
        if isinstance(result, dict) and "error" in result:
            return None
        return result if isinstance(result, str) else json.dumps(result)[:10000]
    elif "google-apps.spreadsheet" in mime_type:
        result = google_api("GET", f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/csv")
        if isinstance(result, dict) and "error" in result:
            return None
        return result if isinstance(result, str) else json.dumps(result)[:10000]
    token = get_google_token()
    if not token: return None
    try:
        req = urllib.request.Request(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
            headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
            try:
                return content.decode("utf-8")[:10000]
            except:
                return f"[Binary file, {len(content)} bytes]"
    except Exception as e:
        logger.error(f"Drive download error: {e}")
        return None


def format_drive_files(files):
    if not files: return "Файлов не найдено."
    lines = []
    type_map = {
        "application/vnd.google-apps.document": "Doc",
        "application/vnd.google-apps.spreadsheet": "Sheet",
        "application/vnd.google-apps.presentation": "Slides",
        "application/vnd.google-apps.folder": "Folder",
        "application/pdf": "PDF",
    }
    for f in files:
        mt = f.get("mimeType", "")
        ftype = type_map.get(mt, mt.split("/")[-1][:10] if "/" in mt else "?")
        mod = f.get("modifiedTime", "")[:10]
        name = f.get("name", "?")
        lines.append(f"[{ftype}] {name}  ({mod})")
    return "\n".join(lines)


def format_drive_for_claude(files, content_map=None):
    if not files: return "[DRIVE DATA]\nNo files found."
    lines = ["[DRIVE DATA]"]
    for f in files:
        fid = f.get("id", "")
        name = f.get("name", "?")
        mt = f.get("mimeType", "")
        mod = f.get("modifiedTime", "")
        link = f.get("webViewLink", "")
        line = f"- {name} (type: {mt}, modified: {mod})"
        if link: line += f" link: {link}"
        if content_map and fid in content_map and content_map[fid]:
            line += f"\n  Content: {content_map[fid][:2000]}"
        lines.append(line)
    return "\n".join(lines)


# === FILE PARSING ===

def parse_xlsx(file_path):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        result = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows: continue
            result.append(f"=== Sheet: {sheet_name} ===")
            for row in rows[:500]:
                cells = [str(c) if c is not None else "" for c in row]
                result.append(" | ".join(cells))
        wb.close()
        text = "\n".join(result)
        return text[:30000]
    except Exception as e:
        return f"[Error: {e}]"


def parse_pdf(file_path):
    try:
        import pdfplumber
        result = []
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages[:50]):
                text = page.extract_text()
                if text:
                    result.append(f"--- Page {i+1} ---\n{text}")
        return "\n".join(result)[:30000] if result else "[PDF: no text found]"
    except Exception as e:
        return f"[PDF Error: {e}]"


def parse_docx(file_path):
    try:
        from docx import Document
        doc = Document(file_path)
        result = []
        for para in doc.paragraphs:
            if para.text.strip():
                result.append(para.text)
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                result.append(" | ".join(cells))
        return "\n".join(result)[:30000] if result else "[DOCX: no text found]"
    except Exception as e:
        return f"[DOCX Error: {e}]"


def parse_file(file_path, file_name):
    nl = file_name.lower()
    if nl.endswith((".xlsx", ".xls")): return parse_xlsx(file_path)
    elif nl.endswith(".pdf"): return parse_pdf(file_path)
    elif nl.endswith(".docx"): return parse_docx(file_path)
    elif nl.endswith((".csv", ".txt", ".md", ".json", ".py", ".js", ".html")):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f: return f.read(50000)
        except Exception as e: return f"[Error: {e}]"
    return f"[Unsupported: {file_name}]"


def fetch_url_text(url):
    """Fetch URL content. Supports Reddit RSS, LinkedIn via search, and general pages."""
    try:
        if "linkedin.com/in/" in url:
            # LinkedIn blocks direct access — use web search to get profile info
            username = re.search(r'linkedin\.com/in/([^/?#]+)', url)
            query = f"site:linkedin.com/in/{username.group(1)}" if username else url
            headers = {"User-Agent": "Mozilla/5.0 (compatible; bot/1.0)"}
            search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(search_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                # Extract visible text snippets
                snippets = re.findall(r'<span[^>]*>(.*?)</span>', content)
                clean = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets if len(s) > 30]
                return "\n".join(clean[:30])[:5000] if clean else "[LinkedIn: profile not accessible]"
        if "reddit.com" in url:
            if re.search(r'reddit\.com/?$', url):
                url = "https://www.reddit.com/r/popular/.rss?limit=25"
            elif "/r/" in url:
                base = re.search(r'(reddit\.com/r/[^/?#]+)', url)
                if base:
                    url = f"https://www.{base.group(1)}/.rss?limit=25"
            headers = {"User-Agent": "Mozilla/5.0 (compatible; bot/1.0)"}
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', content)
                links = re.findall(r'<link>(https://www\.reddit\.com/r/\S+?)</link>', content)
                titles = [t for t in titles if t != "reddit: the front page of the internet"][:25]
                lines = []
                for i, title in enumerate(titles):
                    link = links[i] if i < len(links) else ""
                    lines.append(f"• {title} {link}")
                if lines:
                    return "\n".join(lines)
                return content[:3000]
        headers = {"User-Agent": "Mozilla/5.0 (compatible; bot/1.0)"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")[:8000]
    except Exception as e:
        return f"[Ошибка загрузки: {e}]"


# === HUBSPOT ===

def hubspot_request(method, endpoint, data=None):
    url = f"{HUBSPOT_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp: return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": e.code, "message": error_body[:300]}
    except Exception as e:
        return {"error": str(e)}


def search_contact_by_telegram(tg_username):
    u = tg_username.lower().strip().lstrip("@")
    props = ["firstname", "lastname", "email", "phone", "lifecyclestage", "hs_lead_status", "website"]
    for search in [
        {"filterGroups": [{"filters": [{"propertyName": "website", "operator": "CONTAINS_TOKEN", "value": u}]}], "properties": props, "limit": 5},
        {"filterGroups": [{"filters": [{"propertyName": "website", "operator": "EQ", "value": f"https://t.me/{u}"}]}], "properties": props, "limit": 5},
        {"query": u, "properties": props, "limit": 5},
    ]:
        result = hubspot_request("POST", "/crm/v3/objects/contacts/search", search)
        if result and "results" in result and result["results"]: return result["results"]
    if "_" in u:
        result = hubspot_request("POST", "/crm/v3/objects/contacts/search", {"query": u.replace("_", " "), "properties": props, "limit": 5})
        if result and "results" in result and result["results"]: return result["results"]
    return []

def update_contact(cid, props): return hubspot_request("PATCH", f"/crm/v3/objects/contacts/{cid}", {"properties": props})
def add_note_to_contact(cid, note):
    return hubspot_request("POST", "/crm/v3/objects/notes", {
        "properties": {"hs_note_body": note, "hs_timestamp": str(int(time.time() * 1000))},
        "associations": [{"to": {"id": cid}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]})


# === TAG EXTRACTION ===

def extract_tag_json(text, tag):
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL)
    if match:
        try: return json.loads(match.group(1))
        except: pass
    return None

def extract_tag_text(text, tag):
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL)
    return match.group(1).strip() if match else None

def clean_response(text):
    for tag in ["hubspot_update", "hubspot_contact", "calendar_create", "gmail_send", "gmail_draft"]:
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)
    return text.strip()


# === TELEGRAM HANDLERS ===

def is_allowed(username):
    if not ALLOWED_USERS: return True
    allowed = [u.strip().lower().lstrip("@") for u in ALLOWED_USERS.split(",")]
    return username and username.lower() in allowed


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    g = "подключен" if GOOGLE_REFRESH_TOKEN else "не настроен"
    await update.message.reply_text(
        "AI-ассистент с HubSpot, Calendar, Gmail и Drive.\n\n"
        "/cal — расписание на сегодня\n"
        "/cal3 — на 3 дня\n"
        "/mail — непрочитанные письма\n"
        "/mail from:someone — поиск писем\n"
        "/drive — последние файлы\n"
        "/drive запрос — поиск файлов\n"
        "/find username — контакт в HubSpot\n"
        "/debug — проверить подключения\n"
        "/reset — очистить историю\n\n"
        f"Google: {g}")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    conversations[update.effective_chat.id] = []
    pending_updates.pop(update.effective_chat.id, None)
    await update.message.reply_text("История очищена.")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    lines = []
    if HUBSPOT_API_KEY:
        r = hubspot_request("POST", "/crm/v3/objects/contacts/search", {"query": "a", "properties": ["firstname"], "limit": 1})
        lines.append(f"HubSpot: {'OK (' + str(r.get('total', '?')) + ')' if 'error' not in r else 'ОШИБКА — ' + str(r.get('error'))}")
    else:
        lines.append("HubSpot: не настроен")
    if GOOGLE_REFRESH_TOKEN:
        token = get_google_token()
        if token:
            ev, err = get_calendar_events(1, 1)
            lines.append(f"Calendar: {'OK' if not err else 'ОШИБКА — ' + str(err)[:80]}")
            em, err = get_emails("is:unread", 1)
            lines.append(f"Gmail: {'OK' if not err else 'ОШИБКА — ' + str(err)[:80]}")
            df, err = drive_list_recent(1)
            lines.append(f"Drive: {'OK' if not err else 'ОШИБКА — ' + str(err)[:80]}")
        else:
            lines.append("Google: ошибка токена")
    else:
        lines.append("Google: не настроен")
    await update.message.reply_text("\n".join(lines))


async def calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    if not GOOGLE_REFRESH_TOKEN:
        await update.message.reply_text("Google не настроен.")
        return
    days = 1
    cmd = update.message.text.strip()
    num = cmd[4:].strip() if cmd.startswith("/cal") else ""
    if num.isdigit(): days = min(int(num), 14)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    events, err = get_calendar_events(days, 20)
    if err:
        await update.message.reply_text(f"Ошибка: {err[:200]}")
        return
    label = "сегодня" if days == 1 else f"{days} дней"
    await update.message.reply_text(f"Расписание ({label}):\n\n{format_calendar_events(events)}" if events else f"Нет событий на {label}.")


async def mail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    if not GOOGLE_REFRESH_TOKEN:
        await update.message.reply_text("Google не настроен.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    query = " ".join(context.args) if context.args else "is:unread"
    emails_list, err = get_emails(query, 10)
    if err:
        await update.message.reply_text(f"Ошибка: {err[:200]}")
        return
    if not emails_list:
        await update.message.reply_text(f"Нет писем ({query}).")
        return
    text = f"Письма ({query}):\n\n{format_emails(emails_list)}"
    await update.message.reply_text(text[:4096])


async def drive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    if not GOOGLE_REFRESH_TOKEN:
        await update.message.reply_text("Google не настроен.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    if context.args:
        query = " ".join(context.args)
        files, err = drive_search(query, 10)
        label = f"Поиск: {query}"
    else:
        files, err = drive_list_recent(10)
        label = "Последние файлы"
    if err:
        await update.message.reply_text(f"Ошибка: {err[:200]}")
        return
    if not files:
        await update.message.reply_text("Файлов не найдено.")
        return
    text = f"{label}:\n\n{format_drive_files(files)}"
    await update.message.reply_text(text[:4096])


async def model_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    await update.message.reply_text(f"Модель: {MODEL}")

async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    global MODEL
    if context.args:
        MODEL = context.args[0]
        await update.message.reply_text(f"Модель: {MODEL}")
    else:
        await update.message.reply_text("/setmodel claude-sonnet-4-20250514\n/setmodel claude-opus-4-20250514")


async def find_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    if not context.args:
        await update.message.reply_text("/find username")
        return
    username = context.args[0].lstrip("@")
    msg = await update.message.reply_text(f"Ищу {username}...")
    contacts = search_contact_by_telegram(username)
    if not contacts:
        await msg.edit_text(f"{username} не найден. /debug")
        return
    for c in contacts:
        p = c.get("properties", {})
        name = f"{p.get('firstname', '')} {p.get('lastname', '')}".strip() or "—"
        cid = c["id"]
        link = f"https://app.hubspot.com/contacts/47345195/record/0-1/{cid}"
        stage = LIFECYCLE_STAGES.get(p.get("lifecyclestage", ""), "—")
        status = LEAD_STATUSES.get(p.get("hs_lead_status", ""), "—")
        await update.message.reply_text(
            f"<b>{esc(name)}</b>\nEmail: {esc(p.get('email', '—'))}\nWeb: {esc(p.get('website', '—'))}\n"
            f"Stage: {esc(stage)} | Status: {esc(status)}\n<a href=\"{link}\">HubSpot</a>",
            parse_mode="HTML", disable_web_page_preview=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    chat_id = update.effective_chat.id
    caption = update.message.caption or "Что на этом изображении?"
    photo = update.message.photo[-1]
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        tg_file = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            img = base64.standard_b64encode(f.read()).decode("utf-8")
        os.unlink(tmp_path)
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}, {"type": "text", "text": caption}]
        await _process_message(update, chat_id, content)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    chat_id = update.effective_chat.id
    doc = update.message.document
    if not doc: return
    fn = doc.file_name or "unknown"
    mt = doc.mime_type or ""
    cap = update.message.caption or ""
    if (doc.file_size or 0) > 10 * 1024 * 1024:
        await update.message.reply_text("Файл > 10MB.")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        tg_file = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=f"_{fn}", delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        is_img = mt.startswith("image/") or fn.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        if is_img:
            with open(tmp_path, "rb") as f:
                img = base64.standard_b64encode(f.read()).decode("utf-8")
            os.unlink(tmp_path)
            imt = "image/png" if fn.lower().endswith(".png") else "image/jpeg"
            content = [{"type": "image", "source": {"type": "base64", "media_type": imt, "data": img}}, {"type": "text", "text": cap or fn}]
        else:
            fc = parse_file(tmp_path, fn)
            os.unlink(tmp_path)
            content = f"[FILE: {fn}]\n{fc}" + (f"\n\n{cap}" if cap else "")
        await _process_message(update, chat_id, content)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.username): return
    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text: return

    fwd_username = None
    try:
        if update.message.forward_origin:
            origin = update.message.forward_origin
            if hasattr(origin, "sender_user") and origin.sender_user:
                u = origin.sender_user
                fwd_name = f"{u.first_name or ''} {u.last_name or ''}".strip()
                fwd_username = u.username
                if u.username: fwd_name += f" (@{u.username})"
                user_text = f"[FORWARDED from {fwd_name}]\n{user_text}"
    except: pass

    extra = ""
    tl = user_text.lower()
    if GOOGLE_REFRESH_TOKEN:
        cal_kw = ["календар", "расписан", "встреч", "событ", "schedule", "calendar", "meeting", "что у меня", "свободен", "занят", "план на"]
        mail_kw = ["почт", "письм", "email", "mail", "inbox", "gmail", "непрочитан", "написал"]
        drive_kw = ["файл", "документ", "drive", "диск", "найди файл", "doc", "presentation", "таблиц", "sheet"]
        if any(kw in tl for kw in cal_kw):
            ev, _ = get_calendar_events(3, 15)
            if ev: extra += "\n\n" + format_calendar_for_claude(ev)
        if any(kw in tl for kw in mail_kw):
            em, _ = get_emails("is:unread", 10)
            if em: extra += "\n\n" + format_emails_for_claude(em)
        if any(kw in tl for kw in drive_kw):
            df, _ = drive_list_recent(10)
            if df: extra += "\n\n" + format_drive_for_claude(df)

    url_match = re.search(r'https?://\S+', user_text)
    if url_match:
        fetched = fetch_url_text(url_match.group(0))
        extra += f"\n\n[URL CONTENT]\n{fetched}"
    elif any(kw in tl for kw in ["linkedin.com/in/", "linkedin профил", "linkedin profile"]):
        ln_match = re.search(r'linkedin\.com/in/[\w-]+', user_text)
        if ln_match:
            fetched = fetch_url_text(f"https://www.{ln_match.group(0)}")
            extra += f"\n\n[LINKEDIN PROFILE]\n{fetched}"
    elif any(kw in tl for kw in ["реддит", "reddit", "r/popular", "r/all"]):
        reddit_match = re.search(r'r/(\w+)', user_text)
        if reddit_match:
            fetched = fetch_url_text(f"https://www.reddit.com/r/{reddit_match.group(1)}/")
        else:
            fetched = fetch_url_text("https://www.reddit.com/r/popular/")
        extra += f"\n\n[REDDIT RSS]\n{fetched}"

    schedule_kw = ["каждый день в", "каждое утро", "каждый вечер", "ежедневно в", "every day at", "every morning"]
    cancel_kw = ["отмени задачу", "удали задачу", "список задач", "cancel job", "my jobs"]
    if any(kw in tl for kw in schedule_kw):
        reply = await handle_schedule_command(update, chat_id, user_text)
        await update.message.reply_text(reply, parse_mode="HTML")
        return
    if any(kw in tl for kw in cancel_kw):
        reply = await handle_cancel_schedule(update, chat_id, user_text)
        await update.message.reply_text(reply, parse_mode="HTML")
        return

    await _process_message(update, chat_id, user_text + extra, fwd_username=fwd_username)


async def _keep_typing(bot, chat_id, stop_event):
    while not stop_event.is_set():
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(4)


async def _process_message(update, chat_id, content, fwd_username=None):
    conversations[chat_id].append({"role": "user", "content": content})
    if len(conversations[chat_id]) > MAX_HISTORY:
        conversations[chat_id] = conversations[chat_id][-MAX_HISTORY:]
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        _keep_typing(update.get_bot(), chat_id, stop_typing)
    )
    try:
        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=conversations[chat_id],
                    tools=[{"type": "web_search_20250305", "name": "web_search"}]
                )
            ),
            timeout=60
        )
        stop_typing.set()
        await typing_task
        reply = "\n".join(
            block.text for block in response.content
            if hasattr(block, "text")
        )
        conversations[chat_id].append({"role": "assistant", "content": reply})
        hs_update = extract_tag_json(reply, "hubspot_update")
        tg_username = extract_tag_text(reply, "hubspot_contact") or fwd_username
        if tg_username: tg_username = tg_username.lstrip("@")
        cal_create = extract_tag_json(reply, "calendar_create")
        email_send = extract_tag_json(reply, "gmail_send")
        email_draft = extract_tag_json(reply, "gmail_draft")
        clean = clean_response(reply)
        if hs_update and tg_username:
            await _send_hubspot_update(update, chat_id, hs_update, tg_username, clean)
        elif cal_create:
            pending_updates[chat_id] = {"type": "calendar", "data": cal_create}
            kb = [[InlineKeyboardButton("✅ Создать", callback_data="cal_confirm"), InlineKeyboardButton("❌ Отмена", callback_data="cal_cancel")]]
            await update.message.reply_text(
                f"{esc(clean)}\n\n——————————\n<b>Создать событие:</b>\n"
                f"{esc(cal_create.get('summary',''))}\n{esc(cal_create.get('start',''))} — {esc(cal_create.get('end',''))}",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        elif email_send:
            pending_updates[chat_id] = {"type": "email", "data": email_send}
            kb = [[InlineKeyboardButton("✅ Отправить", callback_data="email_confirm"), InlineKeyboardButton("❌ Отмена", callback_data="email_cancel")]]
            await update.message.reply_text(
                f"{esc(clean)}\n\n——————————\n<b>Письмо:</b>\nTo: {esc(email_send.get('to',''))}\n"
                f"Subject: {esc(email_send.get('subject',''))}\n{esc(email_send.get('body','')[:200])}...",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        elif email_draft:
            pending_updates[chat_id] = {"type": "draft", "data": email_draft}
            kb = [[InlineKeyboardButton("💾 Сохранить черновик", callback_data="draft_confirm"), InlineKeyboardButton("❌ Отмена", callback_data="draft_cancel")]]
            await update.message.reply_text(
                f"{esc(clean)}\n\n——————————\n<b>Черновик:</b>\nTo: {esc(email_draft.get('to','(не указан)'))}\n"
                f"Subject: {esc(email_draft.get('subject',''))}\n{esc(email_draft.get('body','')[:200])}...",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await _send_reply(update, clean)
    except asyncio.TimeoutError:
        stop_typing.set()
        await typing_task
        logger.error("Request timed out after 60s")
        await update.message.reply_text("⏱ Запрос занял слишком долго (>60 сек). Попробуй ещё раз.")
    except Exception as e:
        stop_typing.set()
        await typing_task
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Ошибка: {esc(str(e)[:200])}")


def md_to_html(text):
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^#{1,3}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*-{3,}\s*$', '——————————', text, flags=re.MULTILINE)
    return text.strip()


async def _send_reply(update, text):
    if not text: text = "(пустой ответ)"
    text = md_to_html(text)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i+4096], parse_mode="HTML")


async def _send_hubspot_update(update, chat_id, hs_update, tg_username, clean):
    contacts = search_contact_by_telegram(tg_username)
    if contacts:
        c = contacts[0]
        p = c.get("properties", {})
        name = f"{p.get('firstname', '')} {p.get('lastname', '')}".strip() or tg_username
        cid = c["id"]
        link = f"https://app.hubspot.com/contacts/47345195/record/0-1/{cid}"
        cs = LIFECYCLE_STAGES.get(p.get("lifecyclestage", ""), "—")
        cst = LEAD_STATUSES.get(p.get("hs_lead_status", ""), "—")
        ns = LIFECYCLE_STAGES.get(hs_update.get("suggested_lifecycle", ""), "—")
        nst = LEAD_STATUSES.get(hs_update.get("suggested_lead_status", ""), "—")
        pending_updates[chat_id] = {"type": "hubspot", "contact_id": cid, "contact_name": name,
            "lifecycle": hs_update.get("suggested_lifecycle"), "lead_status": hs_update.get("suggested_lead_status"), "note": hs_update.get("suggested_note")}
        kb = [[InlineKeyboardButton("✅ Всё", callback_data="hs_confirm"), InlineKeyboardButton("❌", callback_data="hs_cancel")],
              [InlineKeyboardButton("📝 Заметку", callback_data="hs_note_only"), InlineKeyboardButton("📊 Статус", callback_data="hs_status_only")]]
        await update.message.reply_text(
            f"{esc(clean)}\n\n━━━━━━━━━━━━━━━\n<b>HubSpot: {esc(name)}</b>\n\n"
            f"Stage: {esc(cs)} → <b>{esc(ns)}</b>\nStatus: {esc(cst)} → <b>{esc(nst)}</b>\n"
            f"Заметка: {esc(hs_update.get('suggested_note', '—'))}\n\n<b>Нажми кнопку:</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(f"{clean}\n\nКонтакт {tg_username} не найден. /find {tg_username}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    pending = pending_updates.get(chat_id)
    if not pending:
        await q.edit_message_text("Нет активного действия.")
        return

    t = pending.get("type", "hubspot")

    if t == "hubspot":
        cid = pending["contact_id"]
        name = pending["contact_name"]
        link = f"https://app.hubspot.com/contacts/47345195/record/0-1/{cid}"
        if q.data == "hs_confirm":
            props = {}
            if pending.get("lifecycle"): props["lifecyclestage"] = pending["lifecycle"]
            if pending.get("lead_status"): props["hs_lead_status"] = pending["lead_status"]
            r = update_contact(cid, props) if props else {}
            nr = add_note_to_contact(cid, pending["note"]) if pending.get("note") else {}
            if "error" not in r and "error" not in nr:
                await q.edit_message_text(f"✅ <b>{esc(name)}</b> обновлён!\n<a href=\"{link}\">HubSpot</a>", parse_mode="HTML", disable_web_page_preview=True)
            else:
                await q.edit_message_text(f"Ошибка: {(r.get('message','') or nr.get('message',''))[:500]}")
        elif q.data == "hs_note_only":
            r = add_note_to_contact(cid, pending["note"]) if pending.get("note") else {"error": "no note"}
            if "error" not in r:
                await q.edit_message_text(f"📝 Заметка для <b>{esc(name)}</b>\n<a href=\"{link}\">HubSpot</a>", parse_mode="HTML", disable_web_page_preview=True)
            else: await q.edit_message_text(f"Ошибка: {r.get('message','')[:500]}")
        elif q.data == "hs_status_only":
            props = {}
            if pending.get("lifecycle"): props["lifecyclestage"] = pending["lifecycle"]
            if pending.get("lead_status"): props["hs_lead_status"] = pending["lead_status"]
            if props:
                r = update_contact(cid, props)
                if "error" not in r:
                    await q.edit_message_text(f"📊 <b>{esc(name)}</b> обновлён\n<a href=\"{link}\">HubSpot</a>", parse_mode="HTML", disable_web_page_preview=True)
                else: await q.edit_message_text(f"Ошибка: {r.get('message','')[:500]}")
        elif q.data == "hs_cancel":
            await q.edit_message_text("Отменено.")
    elif t == "calendar":
        if q.data == "cal_confirm":
            d = pending["data"]
            r = create_calendar_event(d["summary"], d["start"], d["end"], d.get("description", ""))
            await q.edit_message_text(f"✅ Событие создано: {d['summary']}" if "error" not in r else f"Ошибка: {r.get('message','')[:300]}")
        else: await q.edit_message_text("Отменено.")
    elif t == "email":
        if q.data == "email_confirm":
            d = pending["data"]
            r = send_email(d["to"], d["subject"], d["body"])
            await q.edit_message_text(f"✅ Письмо отправлено: {d['to']}" if "error" not in r else f"Ошибка: {r.get('message','')[:300]}")
        else: await q.edit_message_text("Отменено.")
    elif t == "draft":
        if q.data == "draft_confirm":
            d = pending["data"]
            r = save_draft(d.get("to", ""), d["subject"], d["body"])
            await q.edit_message_text("💾 Черновик сохранён в Gmail." if "error" not in r else f"Ошибка: {r.get('message','')[:300]}")
        else: await q.edit_message_text("Отменено.")

    pending_updates.pop(chat_id, None)


async def handle_schedule_command(update, chat_id, user_text):
    hour, minute = 9, 0
    match = re.search(r'в\s+(\d{1,2})(?::(\d{2}))?\s*(утра|вечера|am|pm)?', user_text.lower())
    if not match:
        match = re.search(r'at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', user_text.lower())
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        suffix = match.group(3) or ""
        if suffix in ("вечера", "pm") and hour < 12:
            hour += 12

    job_id = f"job_{chat_id}_{hour}_{minute}"
    task = user_text

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        scheduled_jobs[chat_id] = [j for j in scheduled_jobs[chat_id] if j["id"] != job_id]

    async def scheduled_task():
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": task}],
                tools=[{"type": "web_search_20250305", "name": "web_search"}]
            )
            reply = "\n".join(block.text for block in response.content if hasattr(block, "text"))
            reply = md_to_html(reply)
            from telegram import Bot
            bot = Bot(token=TELEGRAM_TOKEN)
            for i in range(0, len(reply), 4096):
                await bot.send_message(chat_id=chat_id, text=reply[i:i+4096], parse_mode="HTML")
        except Exception as e:
            logger.error(f"Scheduled task error: {e}")

    scheduler.add_job(
        scheduled_task,
        CronTrigger(hour=hour, minute=minute, timezone="America/Los_Angeles"),
        id=job_id,
        replace_existing=True
    )
    scheduled_jobs[chat_id].append({"id": job_id, "time": f"{hour:02d}:{minute:02d}", "task": task[:80]})
    return f"✅ Задача создана: каждый день в <b>{hour:02d}:{minute:02d}</b> PT\nЗадание: {esc(task[:100])}\n\nЧтобы отменить: напиши <b>отмени задачу {hour:02d}:{minute:02d}</b>"


async def handle_cancel_schedule(update, chat_id, user_text):
    match = re.search(r'(\d{1,2}):?(\d{2})?', user_text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        job_id = f"job_{chat_id}_{hour}_{minute}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            scheduled_jobs[chat_id] = [j for j in scheduled_jobs[chat_id] if j["id"] != job_id]
            return f"✅ Задача в {hour:02d}:{minute:02d} отменена."
    jobs = scheduled_jobs.get(chat_id, [])
    if not jobs:
        return "Нет активных задач."
    lines = ["Активные задачи:"]
    for j in jobs:
        lines.append(f"• {j['time']} — {j['task']}")
    return "\n".join(lines)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [("start", start), ("reset", reset), ("debug", debug_cmd), ("cal", calendar_cmd),
                    ("mail", mail_cmd), ("drive", drive_cmd), ("model", model_info), ("setmodel", set_model), ("find", find_contact)]:
        app.add_handler(CommandHandler(cmd, fn))
    for i in range(1, 15):
        app.add_handler(CommandHandler(f"cal{i}", calendar_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info(f"Bot started. Model: {MODEL} | Google: {'YES' if GOOGLE_REFRESH_TOKEN else 'NO'} | HubSpot: {'YES' if HUBSPOT_API_KEY else 'NO'}")
    async def post_init(application):
        scheduler.start()

    app.post_init = post_init
    app.run_polling()

if __name__ == "__main__":
    main()
