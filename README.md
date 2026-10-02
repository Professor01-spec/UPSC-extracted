# UPSC Course Zone by Professor 🥼

Sirf **11 files** — saare secrets `app.env` me pehle se bhare hain (sirf 2 cheezein deploy ke baad daalni hain, wo Railway par pehle exist hi nahi karti).

## 🚀 Deploy Steps

### 1. GitHub par upload karo
Ye saari files (folder ke andar mat rakhna, seedha in files ko) apne repo me upload karo — GitHub "Add file → Upload files" se sab select karke ek saath drag-drop kar sakte ho, koi subfolder nahi hai ab.

### 2. Railway
1. https://railway.app → **New Project → Deploy from GitHub repo**
2. Usi project me **+ New → Database → Add PostgreSQL**

### 3. Sirf 2 Variables daalni hain (baaki sab `app.env` se auto-aa jayenge)
Railway bot service → **Variables** tab:

| Key | Kahan se milega |
|---|---|
| `DATABASE_URL` | Postgres service par click → "Connect" tab → URL copy karo → shuru me `postgresql://` ko `postgresql+asyncpg://` kar dena |
| `WEBAPP_BASE_URL` | Bot service → **Settings → Networking → Generate Domain** → jo URL mile wahi paste karo |

⚠️ Ye do values Railway **tabhi generate karta hai jab Postgres bane aur domain bane** — isliye inhe pehle se bhar ke nahi de sakte, baaki sab (`BOT_TOKEN`, `ADMIN_ID`, etc.) `app.env` file me already hain.

### 4. Redeploy
Variables save karne ke baad Railway khud redeploy karega. **Deployments** tab me "Success" dikhna chahiye.

### 5. Test
- Bot ko `/start` karo → channel join karke continue karo
- `/addcourse` se pehla course add karo (yehi Professor ID se chalega)
- `/adminhelp` — poori admin command list

---

## Files
- `main.py` — server + bot dono ka entry point
- `config.py` — env vars loader
- `database.py` — models + DB connection + starting sections
- `keyboards.py` — buttons + motivational lines + FSM states
- `user_handlers.py` — sabhi user-facing flows
- `admin_handlers.py` — sabhi admin commands
- `webapp_template.py` — Mini App ka HTML
- `app.env` — saari values pehle se bhari hain (2 chhod ke)
- `requirements.txt`, `Procfile`, `.gitignore`

Koi error aaye Railway ke **Deployments → Logs** me, wahi paste kar dena — turant fix karunga.
