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
| `NOTION_API_KEY` | Optional Notion internal integration secret; only used server-side for CA sync |
| `NOTION_DATABASE_ID` | Optional ID of the Notion database shared with that integration |
| `NOTION_API_VERSION` | Optional API version; defaults to `2022-06-28` |

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

CA and Notion use the same annual entitlement. The portal stores four independent datasets: `daily_ca`, `editorial`, `place_in_news`, and `international_orgs`. Users can browse with `/ca dataset | YYYY-MM-DD | topic | subtopic | keyword`, bookmark/read/revise records, and save private notes with `/canote`.

For bidirectional sync, set `NOTION_API_KEY` and `NOTION_DATABASE_ID` in the server environment, invite the integration to the Notion database with read/insert/update permissions, then restart the bot. Admin commands are `/caadd`, `/notionsync [dataset=daily_ca] [from=YYYY-MM-DD] [to=YYYY-MM-DD]`, `/notionstatus`, and `/notionresolve <record_id> portal|notion`. Pushes are queued and retried; pulls report success only after Notion returns a successful API response. Conflicts stay visible until an Admin selects a side.

Notion page zoom is controlled by the Notion client/browser accessibility settings; the Notion API cannot change an individual user's zoom level. The Telegram CA record view provides the same source/content data in a compact message view.

Admins can use `/resync` to revalidate users and chats already stored by the bot, and `/chatadd <chat_id>` to register an existing chat after Telegram confirms the bot is a member. Telegram does not expose an API to enumerate users who have never started the bot or groups the bot has never observed.

The Notion database must contain properties named `Title` (title), `Date` (date), `Dataset` (select), `Topic`, `Subtopic`, `Content`, `Source`, `UPSC Mapping`, `Prelims Mapping`, `Mains Mapping`, `PYQ Mapping`, and `Attachments` (rich text), `Tags` (multi-select), and `Source URL` / `Image URL` (URL). Add these `Dataset` select options: `daily_ca`, `editorial`, `place_in_news`, and `international_orgs`. The Notion API integration token does not provision or revoke individual workspace membership. A public share link may remain usable after portal expiry if a user saved or shared it; strict revocation needs private per-user Notion permissions outside this sync API.

## Referrals

Users can open **Invite Friends** or use `/referral` to get an opaque invite link and see attributed new-user joins. Attribution is immutable after a user is created. Referral rewards are not issued until an explicit reward policy is configured.

## Admin broadcasts

Use `/broadcast`, send up to 10 messages/media items, then `/done` to preview. The broadcast only starts after pressing **Send broadcast**; `/cancel` discards the draft. Each item is copied to known, unbanned users and bot-sent copies are scheduled for deletion after 24 hours.

## Vercel

`vercel.json` and `api/index.py` route requests to the existing FastAPI app. Configure `BOT_TOKEN`, `ADMIN_ID`, `DATABASE_URL`, `WEBHOOK_SECRET_TOKEN`, `WEBAPP_BASE_URL`, and any Gemini/Notion settings in Vercel Project Settings; set `WEBAPP_BASE_URL` to the deployed HTTPS domain.

This adapter serves request/response routes, but the app also starts long-running workers for Notion retries, deletion scheduling, backups, and broadcasts. Vercel functions may freeze or terminate after a response, so those workers are not production-reliable there without a durable external queue/scheduler or persistent worker host. The routing files alone do not make the full bot production-ready on Vercel.

Koi error aaye Railway ke **Deployments → Logs** me, wahi paste kar dena — turant fix karunga.
