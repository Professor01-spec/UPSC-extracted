import logging
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, ErrorEvent, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from config import BOT_TOKEN, WEBAPP_BASE_URL, PORT, BOT_NAME, ADMIN_ID
from database import init_db, seed_sections, seed_courses, migrate_v2, migrate_v3, async_session, Section, Course, ConnectedChat
from keyboards import get_line
from webapp_template import render_section_page
from security import (
    SecurityMiddleware, BackupGateCallbackMiddleware,
    register_dispatcher_auto_heal, state_backup_loop, restore_state_backup,
    _save_state_snapshot, daily_data_backup_task, morning_motivation_task,
    night_motivation_task,
)
from admin_handlers import AI_STATE
import user_handlers
import admin_handlers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# aiogram 3.7+ requires parse_mode via DefaultBotProperties, not a direct kwarg.
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# 🛡️ ZERO-TRUST SECURITY FIREWALL ACTIVATION
dp.message.middleware(SecurityMiddleware())
dp.callback_query.middleware(BackupGateCallbackMiddleware())  # gates inline-button taps too, not just commands

# Auto-recovers the dispatcher from certain classes of failure instead of the
# whole container needing a manual restart-loop.
register_dispatcher_auto_heal(dp)

# Fetched once at startup — used so the Mini App's "Buy Now" button can build
# a https://t.me/<username>?start=buy_<id> deep link back into this exact bot.
BOT_USERNAME = ""

# Admin commands registered before the generic user fallback (which lives at
# the bottom of user_handlers' router) so they're never swallowed by it.
dp.include_router(admin_handlers.router)
dp.include_router(user_handlers.router)


@dp.error()
async def global_error_handler(event: ErrorEvent):
    """Catches any exception raised inside a handler so a bug never shows up
    to the user (or Professor) as total silence — logs it AND pings the
    admin with the short reason, which makes 'command not working' reports
    self-diagnosing instead of a mystery."""
    logger.exception(f"Unhandled error while processing update: {event.exception}")
    try:
        who = None
        update = event.update
        if update.message:
            who = update.message.from_user.id
        elif update.callback_query:
            who = update.callback_query.from_user.id
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ <b>Bot error</b>\n\nUser: {who}\nError: <code>{str(event.exception)[:500]}</code>",
        )
    except Exception:
        pass
    return True


USER_COMMANDS = [
    BotCommand(command="start", description="Open the course menu"),
    BotCommand(command="contact", description="Message Professor directly"),
    BotCommand(command="help", description="Help & FAQ"),
    BotCommand(command="myid", description="Show my Telegram ID"),
    BotCommand(command="trending", description="Trending courses"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="adminhelp", description="Full admin command list"),
    BotCommand(command="addcourse", description="Add course (guided)"),
    BotCommand(command="quickadd", description="Add course (one message)"),
    BotCommand(command="addforall", description="Broadcast Ad to all groups (72h delete)"),
    BotCommand(command="grant", description="Manually unlock a course for a user"),
    BotCommand(command="price", description="Change a course's price"),
    BotCommand(command="removecourse", description="Hide a course"),
    BotCommand(command="movecourse", description="Move a course to other sections"),
    BotCommand(command="listsectionkeys", description="List section keys"),
    BotCommand(command="listsections", description="List section IDs"),
    BotCommand(command="listcourses", description="List all courses"),
    BotCommand(command="pending", description="Pending orders"),
    BotCommand(command="userinfo", description="Look up a user"),
    BotCommand(command="stats", description="Bot stats"),
    BotCommand(command="broadcast", description="Message all users"),
    # Previously missing from this menu (commands existed in admin_handlers.py
    # and worked fine if typed manually, but never showed up in Telegram's "/"
    # autocomplete list since they were never added here).
    BotCommand(command="toggle_ai", description="Turn AI Auto-Reply ON/OFF"),
    BotCommand(command="createpromo", description="Create a promo/discount code"),
    BotCommand(command="weekly_report", description="Generate AI business report"),
    BotCommand(command="setlink", description="Set a course's group link"),
    BotCommand(command="trending_add", description="Add a course to trending"),
    BotCommand(command="trending_remove", description="Remove a course from trending"),
    BotCommand(command="activity", description="Full step trail for a user"),
    BotCommand(command="contacthistory", description="Contact-Professor thread with a user"),
    BotCommand(command="revenue", description="Approved-sales revenue estimate"),
    BotCommand(command="ban", description="Block a user"),
    BotCommand(command="unban", description="Unblock a user"),
    BotCommand(command="findcourse", description="Search courses by name/faculty"),
    BotCommand(command="courseinfo", description="Full detail for one course"),
    BotCommand(command="topcourses", description="Best-selling courses"),
    BotCommand(command="recentusers", description="Last 15 users who joined"),
    BotCommand(command="export", description="Download all users as CSV"),
    BotCommand(command="restart", description="Clear temporary cache (no data lost)"),
]

# ========================================================
# ⚙️ 24-HOUR AUTO PROMOTION TASK (ZERO-COST MARKETING)
# ========================================================
async def daily_promotional_task(bot_instance: Bot):
    while True:
        await asyncio.sleep(24 * 3600)  # Runs every 24 Hours
        try:
            me = await bot_instance.get_me()
            bot_url = f"https://t.me/{me.username}?start=start"
            btn = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Message Official Bot", url=bot_url)]
            ])
            text = (
                f"🎓 <b>Welcome to the Official {BOT_NAME} Community!</b>\n\n"
                f"For premium courses, instant support, and exclusive study material, "
                f"interact with our Official Bot below. 👇"
            )
            
            async with async_session() as session:
                result = await session.execute(select(ConnectedChat))
                chats = result.scalars().all()
                for chat in chats:
                    try:
                        await bot_instance.send_message(chat.id, text, reply_markup=btn, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Failed to send 24h message to {chat.id}: {e}")
        except Exception as e:
            logger.error(f"Daily task loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_sections()
    await seed_courses()
    await migrate_v2()  # idempotent — restructures an existing DB to the v2 home-screen layout
    await migrate_v3()  # idempotent — collapses to the simplified v3 home screen

    # Restore shadow-bans/freezes/mutes/threat-fingerprints AND the /toggle_ai
    # ON-OFF state from the last periodic snapshot. These functions already
    # existed in security.py but were never actually called from anywhere —
    # so every container restart silently reset all of it, including the AI
    # toggle flipping back to its default.
    restore_state_backup(AI_STATE)

    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username or ""
    except Exception:
        logger.exception("Failed to fetch bot username via get_me() — Buy Now deep links will break!")

    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except TelegramBadRequest:
        logger.exception("Failed to set bot commands")

    if WEBAPP_BASE_URL:
        webhook_url = f"{WEBAPP_BASE_URL}/webhook"
        try:
            await bot.set_webhook(webhook_url, drop_pending_updates=True)
            logger.info(f"Webhook set to {webhook_url}")
        except Exception:
            # Don't let a bad/unresolvable WEBAPP_BASE_URL crash the whole
            # container — log it loudly and keep the web server (and health
            # check) alive so the deployment doesn't restart-loop.
            logger.exception(f"Failed to set webhook to '{webhook_url}' — check WEBAPP_BASE_URL")
    else:
        logger.warning("WEBAPP_BASE_URL not set and RAILWAY_PUBLIC_DOMAIN unavailable — webhook NOT configured yet.")

    # Start the 24h background loop task automatically
    asyncio.create_task(daily_promotional_task(bot))
    # Periodic snapshot so bans/freezes/AI-toggle survive the next restart
    # (every STATE_BACKUP_INTERVAL_SEC — was defined in security.py but
    # never scheduled, so restores above always had nothing to restore from
    # on a fresh container until this ran at least once).
    asyncio.create_task(state_backup_loop(AI_STATE))
    # Daily full data export to admin + morning/night motivational broadcasts
    asyncio.create_task(daily_data_backup_task(bot))
    asyncio.create_task(morning_motivation_task(bot))
    asyncio.create_task(night_motivation_task(bot))

    yield

    _save_state_snapshot(AI_STATE)
    await bot.delete_webhook()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/")
async def health():
    return {"status": "ok", "bot": BOT_NAME}


@app.get("/webapp/section/{section_key}", response_class=HTMLResponse)
async def webapp_section(section_key: str):
    if section_key == "all":
        return render_section_page("all", "All Courses", get_line(), BOT_USERNAME)
    async with async_session() as session:
        result = await session.execute(select(Section).where(Section.key == section_key))
        section = result.scalar_one_or_none()
    title = section.name if section else section_key.replace("_", " ").title()
    return render_section_page(section_key, title, get_line(), BOT_USERNAME)


@app.get("/api/courses")
async def api_courses(section_key: str):
    async with async_session() as session:
        if section_key == "all":
            result = await session.execute(select(Course).where(Course.is_active == True))  # noqa: E712
            courses = result.scalars().all()
        else:
            result = await session.execute(select(Section).where(Section.key == section_key))
            section = result.scalar_one_or_none()
            if not section:
                return JSONResponse([])

            # Aggregate this section's own courses PLUS every descendant
            # section's courses (e.g. "UPSC Optional" has no courses of its
            # own — they live on its per-subject children) so a parent
            # section is never shown as empty when its children have data.
            all_sections_result = await session.execute(select(Section))
            all_sections = all_sections_result.scalars().all()
            children_by_parent = {}
            for s in all_sections:
                children_by_parent.setdefault(s.parent_id, []).append(s)

            def collect_ids(root_id):
                ids = [root_id]
                for child in children_by_parent.get(root_id, []):
                    ids.extend(collect_ids(child.id))
                return ids

            section_ids = set(collect_ids(section.id))
            seen = {}
            for s in all_sections:
                if s.id in section_ids:
                    for c in s.courses:
                        if c.is_active:
                            seen[c.id] = c
            courses = list(seen.values())

        payload = [
            {
                "id": c.id, "name": c.name, "faculty": c.faculty, "medium": c.medium,
                "notes": c.notes, "price": float(c.price) if c.price is not None else None,
            }
            for c in courses
        ]
    return JSONResponse(payload)


if __name__ == "__main__":
    # Pass the app object directly (not the "main:app" string) — using the
    # string form makes uvicorn re-import this file as a second module,
    # which re-runs all the router registration code and crashes with
    # "Router is already attached" the second time around.
    uvicorn.run(app, host="0.0.0.0", port=PORT)
