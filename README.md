# UPSC Course Zone by Professor

This repository runs a Python FastAPI + aiogram bot with PostgreSQL. Configure credentials through the hosting provider's environment settings. Never commit `app.env` or paste credential values into logs/issues.

## 🚀 Deploy Steps

### Railway
1. https://railway.app → **New Project → Deploy from GitHub repo**
2. Usi project me **+ New → Database → Add PostgreSQL**

Bot service → **Variables** tab. Set these values there; do not commit them:

| Key | Kahan se milega |
|---|---|
| `BOT_TOKEN` | Telegram BotFather |
| `ADMIN_ID` | Authorized Telegram administrator's numeric user ID |
| `DATABASE_URL` | PostgreSQL connection URL; use the `postgresql+asyncpg://` SQLAlchemy scheme |
| `WEBHOOK_SECRET_TOKEN` | Generate a random 1-256 character value using only letters, digits, `_`, and `-` |
| `WEBAPP_BASE_URL` | Public HTTPS domain for the bot service; required for webhook setup |
| `GEMINI_API_KEY` | Optional; required only for Gemini-powered features |

`PORT` and `RAILWAY_PUBLIC_DOMAIN` are provided by Railway. Without a valid `WEBHOOK_SECRET_TOKEN`, the service disables the Telegram webhook.

Only messages sent by this bot are queued for deletion after 24 hours in private chats, groups, and supergroups. The bot attempts to disable Telegram's group-wide auto-delete timer so member messages are not deleted. This requires the bot to have permission; if Telegram rejects the change, a group admin must turn that timer off in chat settings.

### 4. Redeploy
Variables save karne ke baad Railway khud redeploy karega. **Deployments** tab me "Success" dikhna chahiye.

### 5. Test
- Bot ko `/start` karo → channel join karke continue karo
- `/addcourse` se pehla course add karo (yehi Professor ID se chalega)
- `/adminhelp` — poori admin command list

---

## Runtime files
- `main.py` — server + bot dono ka entry point
- `config.py` — env vars loader
- `database.py` — models + DB connection + starting sections
- `keyboards.py` — buttons + motivational lines + FSM states
- `user_handlers.py` — sabhi user-facing flows
- `admin_handlers.py` — sabhi admin commands
- `webapp_template.py` — Mini App ka HTML
- `requirements.txt`, `Procfile`, `.gitignore`

`app.env` is a local-only convenience file and is ignored by Git. Production secrets must be configured in the host's environment settings.

## Database access products

An Admin can add a Notion product with `/adddatabase Name | https://www.notion.so/... | [price] | [days]`. Price defaults to ₹1,000 and duration to 365 days. Products appear under **Database Access**, use the existing reviewed payment flow, and expose their link only while the portal entitlement is active. Paid renewals extend an active term; expired terms restart from the approval date.

The bot does not currently provision or revoke Notion workspace membership through Notion's API. If the configured URL is a public Notion share link, a user who saved or shared that URL may retain access after portal expiry. Strict revocable access requires a Notion workspace integration and private page permissions.

## Vercel

This checkout is not currently configured as a Vercel deployment: it has no Vercel function configuration, and its bot starts background tasks intended for a persistent process. Do not treat a successful GitHub push as a Vercel-ready deployment. A serverless lifecycle and scheduled-task design must be validated before deploying it there.

Koi error aaye Railway ke **Deployments → Logs** me, wahi paste kar dena — turant fix karunga.
