"""
Google OAuth2 - одноразовый скрипт для получения refresh token.
Включает: Calendar + Gmail + Drive

  pip install google-auth-oauthlib
  python google_auth.py
"""

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Установи: pip install google-auth-oauthlib")
    exit(1)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
]

print("=" * 50)
print("Google OAuth2 Setup (Calendar + Gmail + Drive)")
print("=" * 50)
print()

client_id = input("Введи CLIENT_ID: ").strip()
client_secret = input("Введи CLIENT_SECRET: ").strip()

if not client_id or not client_secret:
    print("CLIENT_ID и CLIENT_SECRET обязательны!")
    exit(1)

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8080"],
    }
}

print()
print("Откроется браузер — разреши доступ к Calendar, Gmail и Drive.")
print()

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=8080, prompt="consent")

print()
print("=" * 50)
print("ГОТОВО! Обнови GOOGLE_REFRESH_TOKEN в Railway:")
print("=" * 50)
print()
print(f"GOOGLE_CLIENT_ID={client_id}")
print(f"GOOGLE_CLIENT_SECRET={client_secret}")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
print()
print("=" * 50)
