#!/usr/bin/env python3
"""Eenmalig: nieuwe Google refresh token ophalen (na publicatie naar production)."""
import json, os, urllib.parse, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "secrets.local.json")) as f:
    secrets = json.load(f)

CLIENT_ID     = secrets["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = secrets["GOOGLE_CLIENT_SECRET"]
REDIRECT      = "http://localhost:8765"
SCOPE         = "https://www.googleapis.com/auth/calendar"

auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id":     CLIENT_ID,
    "redirect_uri":  REDIRECT,
    "response_type": "code",
    "scope":         SCOPE,
    "access_type":   "offline",
    "prompt":        "consent",
})

result = {}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result["code"] = (qs.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "✅ Gelukt — je mag dit venster sluiten." if result["code"] else "❌ Geen code ontvangen."
        self.wfile.write(f"<h2>{msg}</h2>".encode())
    def log_message(self, *a):
        pass

print("Browser openen voor Google-login...")
webbrowser.open(auth_url)
print(f"(Geen browser? Open zelf: {auth_url[:80]}...)")
HTTPServer(("localhost", 8765), Handler).handle_request()

if not result.get("code"):
    raise SystemExit("Geen authorization code ontvangen.")

r = requests.post("https://oauth2.googleapis.com/token", data={
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code":          result["code"],
    "grant_type":    "authorization_code",
    "redirect_uri":  REDIRECT,
}, timeout=15)
tokens = r.json()
refresh = tokens.get("refresh_token")
if not refresh:
    raise SystemExit(f"Geen refresh token in antwoord: {tokens}")

secrets["GOOGLE_REFRESH_TOKEN"] = refresh
with open(os.path.join(BASE, "secrets.local.json"), "w") as f:
    json.dump(secrets, f, indent=2)
print("Nieuwe refresh token opgeslagen in secrets.local.json")
print(f"REFRESH_TOKEN={refresh}")
