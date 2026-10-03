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

Bot-sent messages in private and group chats are queued for deletion after 24 hours. Connected groups and supergroups also use Telegram's native 24-hour auto-delete timer, which applies to every member's new messages when the bot has permission. Set `GROUP_AUTO_DELETE_SECONDS=0` to disable the group-wide timer; private-chat cleanup remains bot-message-only.

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

## Vercel

This checkout is not currently configured as a Vercel deployment: it has no Vercel function configuration, and its bot starts background tasks intended for a persistent process. Do not treat a successful GitHub push as a Vercel-ready deployment. A serverless lifecycle and scheduled-task design must be validated before deploying it there.

Koi error aaye Railway ke **Deployments → Logs** me, wahi paste kar dena — turant fix karunga.
