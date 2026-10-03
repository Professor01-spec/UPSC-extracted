"""Load required credentials and optional runtime settings from the environment."""
import os
from dotenv import load_dotenv

load_dotenv("app.env")  # explicit filename — avoids the hidden-dotfile problem on mobile

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]

BACKUP_CHANNEL = os.environ.get("BACKUP_CHANNEL", "https://t.me/upscse27")
HELP_BOT_USERNAME = os.environ.get("HELP_BOT_USERNAME", "@csewala_bot")
PROFESSOR_CONTACT_LINK = os.environ.get("PROFESSOR_CONTACT_LINK", "https://t.me/csevala")
PAYMENT_HELP_LINK = os.environ.get("PAYMENT_HELP_LINK", "https://t.me/paymentformenti")

_raw_webapp_url = os.environ.get("WEBAPP_BASE_URL", "").strip().rstrip("/")
if _raw_webapp_url and not _raw_webapp_url.startswith("http"):
    # placeholder text (e.g. "PASTE_AFTER_GENERATING_DOMAIN") or a bare domain
    _raw_webapp_url = ""

if not _raw_webapp_url:
    # Fallback: Railway auto-injects this once a public domain is generated,
    # so the bot self-configures its webhook without any manual variable.
    _railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    _raw_webapp_url = f"https://{_railway_domain}" if _railway_domain else ""

WEBAPP_BASE_URL = _raw_webapp_url
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN", "").strip()

BOT_NAME = "UPSC Course Zone by Professor"
