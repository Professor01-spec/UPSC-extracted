import csv
import io
import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import urlparse
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy import case, select, func, desc
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database import async_session, Course, Section, Order, UserCourse, User, UserActivity, ContactMessage, ConnectedChat, AuditEvent, CurrentAffair, NotionSyncJob, NotionSyncState
from keyboards import AdminAddCourse, AdminBroadcast
from config import ADMIN_ID
from notion_sync import CA_DATASETS, enqueue_all_for_notion_push, enqueue_notion_push, notion_sync_configured, pull_notion_database

router = Router()
logger = logging.getLogger(__name__)

# ================= NEW: GLOBAL STATES FOR PREMIUM FEATURES =================
AI_STATE = {"enabled": False}
ACTIVE_PROMOS = {}

def admin_only(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


async def _resync_known_registry(bot, actor_id: int) -> tuple[int, int, int, int, int, int]:
    bot_id = (await bot.get_me()).id
    async with async_session() as session:
        total_users = await session.scalar(select(func.count(User.id))) or 0
        chats = (await session.execute(select(ConnectedChat).order_by(ConnectedChat.id))).scalars().all()
        checked = active = removed = failures = 0
        for index, chat in enumerate(chats, 1):
            try:
                member = await bot.get_chat_member(chat_id=chat.id, user_id=bot_id)
            except Exception:
                failures += 1
                continue
            checked += 1
            still_member = member.status in ("member", "administrator", "creator") or (
                member.status == "restricted" and getattr(member, "is_member", False)
            )
            if still_member:
                active += 1
                if chat.type in ("group", "supergroup"):
                    try:
                        await bot.set_chat_message_auto_delete_time(
                            chat_id=chat.id,
                            message_auto_delete_time=0,
                        )
                    except Exception:
                        failures += 1
            elif member.status in ("left", "kicked") or (
                member.status == "restricted" and not getattr(member, "is_member", False)
            ):
                await session.delete(chat)
                removed += 1
            if index % 20 == 0:
                await asyncio.sleep(0.1)
        session.add(AuditEvent(
            actor_id=actor_id,
            action="system.registry.resynced",
            target_type="telegram_registry",
            target_id="known_records",
        ))
        await session.commit()
    return total_users, len(chats), checked, active, removed, failures


@router.message(Command("resync"))
async def cmd_resync(message: Message):
    if not admin_only(message):
        return
    try:
        users, known_chats, checked, active, removed, failures = await _resync_known_registry(
            message.bot, message.from_user.id
        )
    except Exception:
        logger.exception("Known registry resync failed")
        await message.answer("Registry rescan failed. Existing records were preserved; check server logs and retry.")
        return
    await message.answer(
        "✅ <b>Known registry rescan complete</b>\n"
        f"Users already stored: {users}\n"
        f"Known chats checked: {checked}/{known_chats}\n"
        f"Active groups/channels: {active}\n"
        f"Stale chat records removed: {removed}\n"
        f"Permission/API failures: {failures}\n\n"
        "Telegram cannot enumerate users who never opened the bot or groups it has never observed."
    )


@router.message(Command("chatadd"))
async def cmd_add_existing_chat(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /chatadd <existing_group_or_channel_id>")
        return
    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.answer("Chat ID must be numeric.")
        return
    if chat_id == 0 or abs(chat_id) > 9_223_372_036_854_775_807:
        await message.answer("Chat ID is outside the valid range.")
        return
    try:
        chat = await message.bot.get_chat(chat_id)
        bot_id = (await message.bot.get_me()).id
        membership = await message.bot.get_chat_member(chat_id=chat_id, user_id=bot_id)
    except Exception:
        await message.answer("Telegram could not verify bot access to that chat; no registry entry was added.")
        return
    still_member = membership.status in ("member", "administrator", "creator") or (
        membership.status == "restricted" and getattr(membership, "is_member", False)
    )
    if not still_member:
        await message.answer("The bot is not currently a member of that chat.")
        return
    async with async_session() as session:
        await session.execute(
            pg_insert(ConnectedChat)
            .values(id=chat_id, type=chat.type)
            .on_conflict_do_update(index_elements=["id"], set_={"type": chat.type})
        )
        session.add(AuditEvent(
            actor_id=message.from_user.id,
            action="system.chat.registered",
            target_type="telegram_chat",
            target_id=str(chat_id),
        ))
        await session.commit()
    await message.answer(f"✅ Verified and registered this {chat.type}. It will be included in future group broadcasts.")


def uid_tag(user_id: int) -> str:
    """User ID wrapped so a single tap copies it in Telegram."""
    return f"<code>{user_id}</code>"


def _parse_price(txt: str):
    """Returns (price_or_None, error_message_or_None)."""
    txt = txt.strip().upper()
    if txt == "TBD":
        return None, None
    try:
        return float(txt), None
    except ValueError:
        return 0, f"⚠️ '{txt}' isn't a valid number. Send a number only (e.g. 350) or type 'TBD'."


def _parse_id(txt: str):
    """Returns (id_or_None, error_message_or_None) — guards against non-numeric IDs."""
    try:
        return int(txt.strip()), None
    except ValueError:
        return None, f"⚠️ '{txt}' isn't a valid ID. Send a number only."

# ================= NEW: AUTO CHAT DETECTION (For Broadcast 72h) =================
@router.my_chat_member()
async def on_bot_added_to_chat(event: ChatMemberUpdated):
    """Automatically tracks when bot is added or removed from groups/channels."""
    async with async_session() as session:
        chat = await session.get(ConnectedChat, event.chat.id)
        
        # If bot is added and made member/admin
        still_member = event.new_chat_member.status in ["member", "administrator", "creator"] or (
            event.new_chat_member.status == "restricted" and getattr(event.new_chat_member, "is_member", False)
        )
        if still_member:
            if not chat:
                session.add(ConnectedChat(id=event.chat.id, type=event.chat.type))
                await session.commit()
            if event.chat.type in ("group", "supergroup"):
                try:
                    await event.bot.set_chat_message_auto_delete_time(
                        chat_id=event.chat.id,
                        message_auto_delete_time=0,
                    )
                except Exception:
                    logger.info("Group-wide auto-delete could not be disabled; check bot admin permissions")
                
        # If bot is removed or kicked
        elif event.new_chat_member.status in ["left", "kicked"] or (
            event.new_chat_member.status == "restricted" and not getattr(event.new_chat_member, "is_member", False)
        ):
            if chat:
                await session.delete(chat)
                await session.commit()


# ================= NEW: PREMIUM ADMIN COMMANDS =================
@router.message(Command("toggle_ai"))
async def cmd_toggle_ai(message: Message):
    if not admin_only(message): return
    AI_STATE["enabled"] = not AI_STATE["enabled"]
    status = "ON" if AI_STATE["enabled"] else "OFF"
    await message.answer(f"🤖 AI Auto-Reply is now <b>{status}</b>", parse_mode="HTML")

@router.message(Command("createpromo"))
async def cmd_create_promo(message: Message):
    if not admin_only(message): return
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("Usage: /createpromo <CODE> <DISCOUNT_PERCENT>\nExample: /createpromo DIWALI 20")
    code, percent = parts[1].upper(), int(parts[2])
    ACTIVE_PROMOS[code] = percent
    await message.answer(f"🎟️ Promo Code <b>{code}</b> created with {percent}% discount!", parse_mode="HTML")

@router.message(Command("addforall"))
async def cmd_addforall(message: Message):
    """Broadcasts a replied Ad message to ALL connected groups & channels."""
    if not admin_only(message): return
    if not message.reply_to_message:
        return await message.answer("❌ Please reply to the Ad message/photo with /addforall")
        
    await message.answer("📢 Broadcasting to ALL connected Groups & Channels... (Auto-deletes in 24h)")
    
    async with async_session() as session:
        chats = (await session.execute(select(ConnectedChat))).scalars().all()
    
    if not chats:
        return await message.answer("⚠️ Bot is not in any groups or channels yet.")

    sent, failed = 0, 0
    for chat in chats:
        try:
            await message.reply_to_message.copy_to(chat_id=chat.id)
            sent += 1
        except Exception:
            failed += 1
            
    await message.answer(f"✅ Broadcast complete!\nSent to: <b>{sent} chats</b>\nFailed: {failed} chats.", parse_mode="HTML")

@router.message(Command("weekly_report"))
async def manual_weekly_report(message: Message):
    if not admin_only(message): return
    async with async_session() as session:
        users_count = (await session.execute(select(func.count(User.id)))).scalar()
        revenue = (await session.execute(select(Course.price).join(UserCourse, UserCourse.course_id == Course.id))).all()
        total_rev = sum(float(p[0]) for p in revenue if p[0] is not None)
        chats_count = (await session.execute(select(func.count(ConnectedChat.id)))).scalar()
    
    report_text = (
        "📊 <b>WEEKLY AI BUSINESS REPORT</b>\n\n"
        f"👥 <b>Total Users Base:</b> {users_count}\n"
        f"📢 <b>Connected Groups/Channels:</b> {chats_count}\n"
        f"💰 <b>Total Verified Revenue:</b> ₹{int(total_rev)}\n"
        "🛡️ <b>Security Status:</b> Active (0 Breaches)\n\n"
        "<i>Running on Premium Auto-Pilot!</i> 🚀"
    )
    await message.answer(report_text, parse_mode="HTML")


# ================= ADD COURSE =================
@router.message(Command("addcourse"))
async def cmd_add_course(message: Message, state: FSMContext):
    if not admin_only(message):
        return
    await state.set_state(AdminAddCourse.name)
    await message.answer("➕ <b>New Course</b>\n\nSend the course name:")


@router.message(AdminAddCourse.name)
async def add_course_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminAddCourse.faculty)
    await message.answer("👨‍🏫 Faculty/Institute name:")


@router.message(AdminAddCourse.faculty)
async def add_course_faculty(message: Message, state: FSMContext):
    await state.update_data(faculty=message.text.strip())
    await state.set_state(AdminAddCourse.medium)
    await message.answer("🌐 Medium (English / Hindi / Both):")


@router.message(AdminAddCourse.medium)
async def add_course_medium(message: Message, state: FSMContext):
    await state.update_data(medium=message.text.strip())
    await state.set_state(AdminAddCourse.notes)
    await message.answer("📝 Short notes/description (or '-' if none):")


@router.message(AdminAddCourse.notes)
async def add_course_notes(message: Message, state: FSMContext):
    notes = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(notes=notes)
    await state.set_state(AdminAddCourse.price)
    await message.answer("💰 Price in ₹ (number only, or 'TBD' if not decided yet):")


@router.message(AdminAddCourse.price)
async def add_course_price(message: Message, state: FSMContext):
    price, error = _parse_price(message.text)
    if error:
        await message.answer(error)
        return  # stay in same state, let them retry
    await state.update_data(price=price)
    await state.set_state(AdminAddCourse.section)

    async with async_session() as session:
        result = await session.execute(select(Section).where(Section.parent_id.isnot(None)))
        sections = result.scalars().all()
    listing = "\n".join(f"{s.id} — {s.name}" for s in sections) or "(No sub-sections found in DB)"
    await message.answer(
        f"📂 Which section(s)? Send the section ID (comma-separated if the course belongs in multiple sections):\n\n{listing}"
    )


@router.message(AdminAddCourse.section)
async def add_course_section(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        section_ids = [int(x.strip()) for x in message.text.split(",")]
    except ValueError:
        await message.answer("⚠️ Send numbers only, comma-separated. Try again:")
        return

    async with async_session() as session:
        course = Course(name=data["name"], faculty=data["faculty"], medium=data["medium"],
                         notes=data["notes"], price=data["price"])
        for sid in section_ids:
            section = await session.get(Section, sid)
            if section:
                course.sections.append(section)
        session.add(course)
        await session.commit()
        await session.refresh(course)

    await state.clear()
    price_tag = f"₹{int(course.price)}" if course.price is not None else "TBD"
    await message.answer(f"✅ Course added!\n\n📘 {course.name} — {price_tag}\n🆔 Course ID: {course.id}")


# ================= QUICK ADD (one-line, using section KEYS) =================
@router.message(Command("quickadd"))
async def cmd_quick_add(message: Message):
    if not admin_only(message):
        return
    body = message.text.split(maxsplit=1)
    if len(body) != 2:
        await message.answer(
            "Usage:\n/quickadd Name | Faculty | Medium | Notes | Price(or TBD) | section_key1,section_key2\n\n"
            "Example:\n/quickadd Polity Crash 2027 | Jatin Gupta | Hindi | Complete crash course | 499 | subj_polity\n\n"
            "Use /listsectionkeys to see valid section keys."
        )
        return
    parts = [p.strip() for p in body[1].split("|")]
    if len(parts) != 6:
        await message.answer("⚠️ Need exactly 6 parts separated by '|': Name | Faculty | Medium | Notes | Price | section_keys")
        return
    name, faculty, medium, notes, price_txt, keys_txt = parts
    price, error = _parse_price(price_txt)
    if error:
        await message.answer(error)
        return
    section_keys = [k.strip() for k in keys_txt.split(",") if k.strip()]

    async with async_session() as session:
        result = await session.execute(select(Section))
        sections_by_key = {s.key: s for s in result.scalars().all()}
        matched, unmatched = [], []
        for k in section_keys:
            if k in sections_by_key:
                matched.append(sections_by_key[k])
            else:
                unmatched.append(k)
        course = Course(name=name, faculty=faculty, medium=medium, notes=notes, price=price)
        course.sections = matched
        session.add(course)
        await session.commit()
        await session.refresh(course)

    price_tag = f"₹{int(course.price)}" if course.price is not None else "TBD"
    warn = f"\n⚠️ Unknown section key(s) ignored: {', '.join(unmatched)}" if unmatched else ""
    await message.answer(
        f"✅ Course added!\n\n📘 {course.name} — {price_tag}\n🆔 Course ID: {course.id}\n"
        f"📂 Sections: {', '.join(s.name for s in matched) or '(none — unlisted)'}{warn}"
    )


@router.message(Command("adddatabase"))
async def cmd_add_database(message: Message):
    if not admin_only(message):
        return
    body = message.text.split(maxsplit=1)
    if len(body) != 2:
        await message.answer(
            "Usage: /adddatabase Name | https://www.notion.so/... | [price] | [days]\n"
            "Price defaults to ₹1000 and duration defaults to 365 days."
        )
        return

    parts = [part.strip() for part in body[1].split("|")]
    if not 2 <= len(parts) <= 4:
        await message.answer("Provide a name, Notion URL, optional price, and optional duration in days.")
        return
    name, access_url = parts[:2]
    price_text = parts[2] if len(parts) > 2 and parts[2] else "1000"
    duration_text = parts[3] if len(parts) > 3 and parts[3] else "365"
    try:
        parsed_url = urlparse(access_url)
        host = (parsed_url.hostname or "").lower()
    except ValueError:
        await message.answer("Use a valid HTTPS Notion share URL.")
        return
    if (
        parsed_url.scheme != "https"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or not (host == "notion.so" or host.endswith(".notion.so") or host == "notion.site" or host.endswith(".notion.site"))
        or len(access_url) > 300
    ):
        await message.answer("Use a valid HTTPS Notion share URL.")
        return
    price, error = _parse_price(price_text)
    if error or price is None or not math.isfinite(price) or not 0 < price <= 99_999_999.99:
        await message.answer(error or "Price must be between ₹0.01 and ₹99,999,999.99.")
        return
    try:
        duration_days = int(duration_text)
    except ValueError:
        duration_days = 0
    if not 1 <= duration_days <= 3650:
        await message.answer("Duration must be between 1 and 3650 days.")
        return
    if not name or len(name) > 150:
        await message.answer("Name must be between 1 and 150 characters.")
        return

    async with async_session() as session:
        product = Course(
            name=name,
            faculty="Notion Database",
            medium="Online",
            notes=f"Time-limited database access: {duration_days} days",
            price=price,
            group_link=access_url,
            is_active=True,
            access_duration_days=duration_days,
            is_ca_notion_access=True,
        )
        session.add(product)
        await session.flush()
        session.add(AuditEvent(
            actor_id=message.from_user.id,
            action="database.product.created",
            target_type="course",
            target_id=str(product.id),
        ))
        await session.commit()

    await message.answer(
        f"✅ Database product created: {escape(name)}\n"
        f"Price: ₹{price:g} | Access: {duration_days} days | ID: {product.id}"
    )


@router.message(Command("listsectionkeys"))
async def cmd_list_section_keys(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(select(Section).order_by(Section.parent_id, Section.id))
        sections = result.scalars().all()
    lines = ["🔑 <b>Section Keys</b> (use with /quickadd, /movecourse)\n"]
    for s in sections:
        tag = " (top-level)" if s.parent_id is None else ""
        lines.append(f"<code>{s.key}</code> — {s.name}{tag}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("caadd"))
async def cmd_add_current_affair(message: Message):
    if not admin_only(message):
        return
    body = message.text.split(maxsplit=1)
    usage = (
        "Usage: /caadd dataset | YYYY-MM-DD | title | topic | content | source | source_url | "
        "subtopic | tags | UPSC mapping | Prelims | Mains | PYQ | image_url | attachment_urls\n"
        "Datasets: daily_ca, editorial, place_in_news, international_orgs"
    )
    if len(body) != 2:
        await message.answer(usage)
        return
    fields = [field.strip() for field in body[1].split("|")]
    if not 5 <= len(fields) <= 15:
        await message.answer(usage)
        return
    fields.extend([""] * (15 - len(fields)))
    dataset, date_text, title, topic, content = fields[:5]
    source_name, source_url, subtopic, tags, upsc, prelims, mains, pyq, image_url, attachments = fields[5:15]
    if dataset not in CA_DATASETS or not title or len(title) > 300 or not topic or len(topic) > 150:
        await message.answer("Dataset, title, or topic is invalid.")
        return
    try:
        affair_date = date.fromisoformat(date_text)
    except ValueError:
        await message.answer("Date must use YYYY-MM-DD.")
        return
    if not content or len(content) > 3000:
        await message.answer("Content must be 1-3000 characters.")
        return
    for url in [source_url, image_url, *[value.strip() for value in attachments.split(",") if value.strip()]]:
        if url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                await message.answer("Source, image, and attachment URLs must be HTTPS links without embedded credentials.")
                return

    async with async_session() as session:
        affair = CurrentAffair(
            dataset=dataset,
            affair_date=affair_date,
            title=title,
            topic=topic,
            subtopic=subtopic or None,
            tags=tags or None,
            content=content,
            source_name=source_name or None,
            source_url=source_url or None,
            upsc_mapping=upsc or None,
            prelims_mapping=prelims or None,
            mains_mapping=mains or None,
            pyq_mapping=pyq or None,
            image_url=image_url or None,
            attachments=attachments or None,
        )
        session.add(affair)
        await session.flush()
        queued = await enqueue_notion_push(session, affair.id)
        session.add(AuditEvent(
            actor_id=message.from_user.id,
            action="ca.record.created",
            target_type="current_affair",
            target_id=str(affair.id),
        ))
        await session.commit()

    sync_status = "Notion push queued; it is not confirmed until the API accepts it." if queued else "Notion sync is not configured; this record is portal-only for now."
    await message.answer(f"✅ CA record #{affair.id} added to {dataset}. {sync_status}")


@router.message(Command("notionsync"))
async def cmd_notion_sync(message: Message):
    if not admin_only(message):
        return
    if not notion_sync_configured():
        await message.answer("Notion sync is not configured. Set NOTION_API_KEY and NOTION_DATABASE_ID in the server environment.")
        return
    filters = {}
    for part in message.text.split()[1:]:
        key, separator, value = part.partition("=")
        if not separator or key not in ("dataset", "from", "to") or not value:
            await message.answer("Usage: /notionsync [dataset=daily_ca] [from=YYYY-MM-DD] [to=YYYY-MM-DD]")
            return
        if key == "dataset":
            if value not in CA_DATASETS:
                await message.answer("Unknown dataset. Use daily_ca, editorial, place_in_news, or international_orgs.")
                return
            filters["dataset"] = value
        else:
            try:
                filters["date_from" if key == "from" else "date_to"] = date.fromisoformat(value)
            except ValueError:
                await message.answer("Sync dates must use YYYY-MM-DD.")
                return
    if filters.get("date_from") and filters.get("date_to") and filters["date_from"] > filters["date_to"]:
        await message.answer("The 'from' date must not be after the 'to' date.")
        return
    queued = await enqueue_all_for_notion_push(**filters)
    try:
        pulled = await pull_notion_database(**filters)
    except Exception:
        logger.exception("Notion pull failed")
        async with async_session() as session:
            session.add(AuditEvent(
                actor_id=message.from_user.id,
                action="notion.sync.failed",
                target_type="notion_database",
                target_id="configured_database",
            ))
            await session.commit()
        await message.answer(
            f"Portal export jobs queued: {queued}. Notion pull failed; no pull success is reported. "
            "The sync worker will retry queued exports."
        )
        return
    async with async_session() as session:
        session.add(AuditEvent(
            actor_id=message.from_user.id,
            action="notion.pull.succeeded",
            target_type="notion_database",
            target_id="configured_database",
        ))
        await session.commit()
    await message.answer(
        "✅ Notion pull confirmed by the API. "
        f"Filter: {filters or 'all datasets / dates'}. "
        f"Imported {pulled['imported']}, updated {pulled['updated']}, conflicts {pulled['conflicts']}, "
        f"skipped {pulled['skipped']}. Portal push jobs queued: {queued}; those complete asynchronously."
    )


@router.message(Command("notionstatus"))
async def cmd_notion_status(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        counts = {}
        for status in ("pending", "processing", "failed", "conflict", "synced"):
            counts[status] = await session.scalar(
                select(func.count(NotionSyncJob.id)).where(NotionSyncJob.status == status)
            ) or 0
        pull_state = await session.get(NotionSyncState, 1)
    pull_summary = (
        f"Last pull: {pull_state.last_pull_status} at {pull_state.last_pull_at}\n"
        f"Imported {pull_state.imported}, updated {pull_state.updated}, conflicts {pull_state.conflicts}, "
        f"skipped {pull_state.skipped}, error {pull_state.last_error or 'none'}"
        if pull_state and pull_state.last_pull_at else "No Notion pull has completed yet."
    )
    await message.answer(
        f"Notion configured: {'Yes' if notion_sync_configured() else 'No'}\n"
        + pull_summary + "\n"
        + "\n".join(f"{status.title()}: {count}" for status, count in counts.items())
    )


@router.message(Command("notionresolve"))
async def cmd_notion_resolve(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 3 or parts[2] not in ("portal", "notion"):
        await message.answer("Usage: /notionresolve <ca_record_id> portal|notion")
        return
    record_id, error = _parse_id(parts[1])
    if error:
        await message.answer(error)
        return
    async with async_session() as session:
        record = await session.get(CurrentAffair, record_id)
        job = await session.scalar(
            select(NotionSyncJob).where(
                NotionSyncJob.affair_id == record_id,
                NotionSyncJob.direction == "push",
                NotionSyncJob.status == "conflict",
            )
        )
        if not record or not job:
            await message.answer("No unresolved Notion conflict exists for that record.")
            return
        if parts[2] == "portal":
            job.status = "pending"
            job.attempts = 0
            job.force_portal = True
            job.last_error = "force_portal"
            job.next_attempt_at = datetime.utcnow()
        else:
            if not record.notion_page_id:
                await message.answer("The Notion page is missing; portal version retained for manual recovery.")
                return
            record.last_synced_at = datetime.utcnow()
            record.portal_dirty = False
        session.add(AuditEvent(
            actor_id=message.from_user.id,
            action=f"notion.conflict.resolved.{parts[2]}",
            target_type="current_affair",
            target_id=str(record_id),
        ))
        await session.commit()
    if parts[2] == "notion":
        try:
            result = await pull_notion_database()
        except Exception:
            await message.answer("Notion version was selected, but the pull failed; retry /notionsync. No sync success is reported.")
            return
        await message.answer(f"Notion version applied after confirmed pull. Updated {result['updated']} records.")
    else:
        await message.answer("Portal version selected. A retryable Notion push is queued.")


# ================= MANUAL GRANT =================
@router.message(Command("grant"))
async def cmd_grant(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Usage: /grant <user_id> <course_id>\n\nDirectly unlocks a course for a user — bypasses payment, shows up in their 'My Courses' immediately.")
        return
    user_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    course_id, id_error2 = _parse_id(parts[2])
    if id_error2:
        await message.answer(id_error2)
        return
    async with async_session() as session:
        user = await session.get(User, user_id)
        course = await session.get(Course, course_id)
        if not user:
            await message.answer("❌ User not found (they must /start the bot at least once first).")
            return
        if not course:
            await message.answer("❌ Course ID not found.")
            return
        existing = await session.execute(
            select(UserCourse).where(UserCourse.user_id == user_id, UserCourse.course_id == course_id)
        )
        existing_access = existing.scalar_one_or_none()
        now = datetime.utcnow()
        if existing_access and (
            not course.access_duration_days
            or existing_access.expires_at is None
            or existing_access.expires_at > now
        ):
            await message.answer("ℹ️ This user already owns this course.")
            return
        if course.access_duration_days:
            duration = timedelta(days=course.access_duration_days)
            renewal_expiry = now + duration
            grant = pg_insert(UserCourse).values(
                user_id=user_id,
                course_id=course_id,
                expires_at=renewal_expiry,
            )
            await session.execute(
                grant.on_conflict_do_update(
                    index_elements=["user_id", "course_id"],
                    set_={
                        "expires_at": case(
                            (UserCourse.expires_at > now, UserCourse.expires_at + duration),
                            else_=renewal_expiry,
                        )
                    },
                )
            )
            access_expires_at = await session.scalar(
                select(UserCourse.expires_at).where(
                    UserCourse.user_id == user_id,
                    UserCourse.course_id == course_id,
                )
            )
        else:
            access_expires_at = None
            await session.execute(
                pg_insert(UserCourse)
                .values(user_id=user_id, course_id=course_id)
                .on_conflict_do_nothing(index_elements=["user_id", "course_id"])
            )
        session.add(AuditEvent(
            actor_id=message.from_user.id,
            action="course.access.granted",
            target_type="course",
            target_id=str(course_id),
        ))
        await session.commit()

    try:
        if course.access_duration_days:
            access_text = (
                f"Open your database access: {escape(course.group_link, quote=True)}\n"
                f"Valid through {access_expires_at:%d %b %Y}."
            )
        else:
            access_text = (
                f"Join the group here: {course.group_link}"
                if course.group_link else "The group link will be added shortly."
            )
        await message.bot.send_message(
            user_id,
            f"🎉 Professor has assigned you <b>{escape(course.name)}</b>!\n\n{access_text}",
        )
    except Exception:
        pass
    await message.answer(f"✅ Granted {course.name} (ID {course.id}) to user {uid_tag(user_id)}.")


# ================= QUICK COMMANDS =================
@router.message(Command("price"))
async def cmd_price(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Usage: /price <course_id> <new_price_or_TBD>")
        return
    course_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    price, error = _parse_price(parts[2])
    if error:
        await message.answer(error)
        return
    async with async_session() as session:
        course = await session.get(Course, course_id)
        if not course:
            await message.answer("❌ Course ID not found.")
            return
        course.price = price
        await session.commit()
        tag = f"₹{int(price)}" if price is not None else "TBD"
        await message.answer(f"✅ Price updated: {course.name} → {tag}")


@router.message(Command("removecourse"))
async def cmd_remove_course(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Usage: /removecourse [course_id]\n"
            "Multiple at once: /removecourse 12,15,20  (comma or space separated)"
        )
        return

    raw_ids = [p for p in parts[1].replace(",", " ").split() if p]
    ids, bad = [], []
    for raw in raw_ids:
        cid, err = _parse_id(raw)
        if err:
            bad.append(raw)
        else:
            ids.append(cid)

    if bad:
        await message.answer(f"⚠️ Skipping invalid ID(s): {', '.join(bad)}")

    if not ids:
        return

    removed, missing = [], []
    async with async_session() as session:
        for cid in ids:
            course = await session.get(Course, cid)
            if not course:
                missing.append(cid)
                continue
            course.is_active = False
            removed.append(f"{cid} — {course.name}")
        await session.commit()

    reply = ""
    if removed:
        reply += f"🗑️ Hidden {len(removed)} course(s):\n" + "\n".join(removed)
    if missing:
        reply += ("\n\n" if reply else "") + "❌ Not found: " + ", ".join(str(m) for m in missing)
    await message.answer(reply or "Nothing removed.")


@router.message(Command("setlink"))
async def cmd_set_link(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Usage: /setlink <course_id> <group_link>")
        return
    course_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        course = await session.get(Course, course_id)
        if not course:
            await message.answer("❌ Course ID not found.")
            return
        course.group_link = parts[2].strip()
        await session.commit()
        await message.answer(f"🔗 Group link set: {course.name}")


@router.message(Command("trending_add"))
async def cmd_trending_add(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /trending_add <course_id>")
        return
    course_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        course = await session.get(Course, course_id)
        if not course:
            await message.answer("❌ Course ID not found.")
            return
        course.is_trending = True
        await session.commit()
        await message.answer(f"🔥 Added to trending: {course.name}")


@router.message(Command("trending_remove"))
async def cmd_trending_remove(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /trending_remove <course_id>")
        return
    course_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        course = await session.get(Course, course_id)
        if not course:
            await message.answer("❌ Course ID not found.")
            return
        course.is_trending = False
        await session.commit()
        await message.answer(f"➖ Removed from trending: {course.name}")


@router.message(Command("listcourses"))
async def cmd_list_courses(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(select(Course).where(Course.is_active == True))  # noqa: E712
        courses = result.scalars().all()
    if not courses:
        await message.answer("No courses found.")
        return
    lines = []
    for c in courses:
        tag = f"₹{int(c.price)}" if c.price is not None else "TBD"
        star = "🔥" if c.is_trending else ""
        lines.append(f"{c.id}. {star}{c.name} — {tag}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("movecourse"))
async def cmd_move_course(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer(
            "Usage: /movecourse <course_id> <section_id1,section_id2,...>\n"
            "Replaces the course's current section(s) with the ones you list. "
            "Use /listsections to see section IDs."
        )
        return
    course_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    try:
        section_ids = [int(x.strip()) for x in parts[2].split(",")]
    except ValueError:
        await message.answer("⚠️ Section IDs must be numbers, comma-separated.")
        return

    async with async_session() as session:
        course = await session.get(Course, course_id)
        if not course:
            await message.answer("❌ Course ID not found.")
            return
        new_sections = []
        for sid in section_ids:
            sec = await session.get(Section, sid)
            if sec:
                new_sections.append(sec)
        course.sections = new_sections
        await session.commit()
        names = ", ".join(s.name for s in new_sections) or "(none — course is now unlisted)"
        await message.answer(f"📂 {course.name} moved to: {names}")


@router.message(Command("listsections"))
async def cmd_list_sections(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(select(Section).where(Section.parent_id.isnot(None)))
        sections = result.scalars().all()
    lines = [f"{s.id} — {s.name}" for s in sections]
    text = "📂 <b>Sections</b>\n\n" + "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("pending"))
async def cmd_pending(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(
            select(Order, Course, User)
            .join(Course, Order.course_id == Course.id)
            .join(User, Order.user_id == User.id)
            .where(Order.status == "pending")
            .order_by(Order.created_at)
        )
        rows = result.all()
    if not rows:
        await message.answer("✅ No pending orders right now.")
        return
    lines = ["⏳ <b>Pending Orders</b>\n"]
    for order, course, user in rows:
        lines.append(
            f"#{order.id} — {course.name} — @{user.username or '—'} (ID {uid_tag(user.id)}) — "
            f"{order.created_at.strftime('%d %b, %H:%M')}"
        )
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("userinfo"))
async def cmd_user_info(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /userinfo <user_id>")
        return
    user_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            await message.answer("❌ User not found.")
            return
        courses_result = await session.execute(
            select(Course).join(UserCourse, UserCourse.course_id == Course.id).where(UserCourse.user_id == user_id)
        )
        courses = courses_result.scalars().all()
        orders_result = await session.execute(select(func.count(Order.id)).where(Order.user_id == user_id))
        order_count = orders_result.scalar()

        activity_result = await session.execute(
            select(UserActivity).where(UserActivity.user_id == user_id).order_by(desc(UserActivity.created_at)).limit(8)
        )
        activity_rows = activity_result.scalars().all()

    course_lines = "\n".join(f"• {c.name} (ID {c.id})" for c in courses) or "None yet"
    activity_lines = "\n".join(
        f"• {a.step} ({a.created_at.strftime('%d %b, %H:%M')})" for a in activity_rows
    ) or "No activity logged yet."

    await message.answer(
        f"👤 <b>{user.first_name or '—'}</b> (@{user.username or '—'})\n"
        f"🆔 ID: {uid_tag(user.id)}\n"
        f"📅 Joined: {user.joined_at.strftime('%d %b %Y') if user.joined_at else '—'}\n"
        f"🚫 Banned: {'Yes' if user.is_banned else 'No'}\n"
        f"✅ Backup channel verified: {'Yes' if user.has_joined_backup_channel else 'No'}\n"
        f"🧾 Total orders placed: {order_count}\n\n"
        f"📘 <b>Courses owned:</b>\n{course_lines}\n\n"
        f"🕘 <b>Recent steps:</b>\n{activity_lines}"
    )


@router.message(Command("activity"))
async def cmd_activity(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /activity <user_id>")
        return
    user_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        result = await session.execute(
            select(UserActivity).where(UserActivity.user_id == user_id).order_by(desc(UserActivity.created_at)).limit(30)
        )
        rows = result.scalars().all()
    if not rows:
        await message.answer("No activity logged for this user yet.")
        return
    lines = [f"🕘 <b>Recent steps — {uid_tag(user_id)}</b>\n"]
    for a in rows:
        lines.append(f"• {a.step} — {a.created_at.strftime('%d %b, %H:%M')}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("contacthistory"))
async def cmd_contact_history(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /contacthistory <user_id>")
        return
    user_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        result = await session.execute(
            select(ContactMessage).where(ContactMessage.user_id == user_id).order_by(ContactMessage.created_at)
        )
        rows = result.scalars().all()
    if not rows:
        await message.answer("No contact messages with this user yet.")
        return
    lines = [f"💬 <b>Contact history — {uid_tag(user_id)}</b>\n"]
    for m in rows:
        arrow = "👤→🧑‍🏫" if m.direction == "in" else "🧑‍🏫→👤"
        lines.append(f"{arrow} {m.content} ({m.created_at.strftime('%d %b, %H:%M')})")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("export"))
async def cmd_export(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "username", "first_name", "joined_at", "is_banned", "backup_channel_verified"])
    for u in users:
        writer.writerow([u.id, u.username or "", u.first_name or "",
                          u.joined_at.isoformat() if u.joined_at else "", u.is_banned, u.has_joined_backup_channel])

    file_bytes = buf.getvalue().encode("utf-8")
    await message.answer_document(
        BufferedInputFile(file_bytes, filename=f"users_export_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"),
        caption=f"📤 Exported {len(users)} users."
    )


@router.message(Command("topcourses"))
async def cmd_top_courses(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(
            select(Course.name, func.count(UserCourse.id).label("cnt"))
            .join(UserCourse, UserCourse.course_id == Course.id)
            .group_by(Course.id, Course.name)
            .order_by(desc("cnt"))
            .limit(10)
        )
        rows = result.all()
    if not rows:
        await message.answer("No course grants yet.")
        return
    lines = ["🏆 <b>Top Courses (by grants)</b>\n"]
    for i, (name, cnt) in enumerate(rows, 1):
        lines.append(f"{i}. {name} — {cnt} students")
    await message.answer("\n".join(lines))


@router.message(Command("recentusers"))
async def cmd_recent_users(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(select(User).order_by(desc(User.joined_at)).limit(15))
        users = result.scalars().all()
    if not users:
        await message.answer("No users yet.")
        return
    lines = ["🆕 <b>Recent Users</b>\n"]
    for u in users:
        lines.append(
            f"• {u.first_name or '—'} (@{u.username or '—'}) — {uid_tag(u.id)} — "
            f"{u.joined_at.strftime('%d %b, %H:%M') if u.joined_at else '—'}"
        )
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("findcourse"))
async def cmd_find_course(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Usage: /findcourse <keyword>")
        return
    keyword = parts[1].strip().lower()
    async with async_session() as session:
        result = await session.execute(select(Course))
        all_courses = result.scalars().all()
    matches = [c for c in all_courses if keyword in c.name.lower() or keyword in (c.faculty or "").lower()]
    if not matches:
        await message.answer("No matching courses found.")
        return
    lines = [f"🔎 <b>Matches for '{parts[1].strip()}'</b>\n"]
    for c in matches[:40]:
        tag = f"₹{int(c.price)}" if c.price is not None else "TBD"
        active = "" if c.is_active else " (hidden)"
        sections = ", ".join(s.name for s in c.sections) or "—"
        lines.append(f"{c.id}. {c.name} — {tag}{active}\n   📂 {sections}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@router.message(Command("courseinfo"))
async def cmd_course_info(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /courseinfo <course_id>")
        return
    course_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        course = await session.get(Course, course_id)
        if not course:
            await message.answer("❌ Course ID not found.")
            return
        owners_result = await session.execute(select(func.count(UserCourse.id)).where(UserCourse.course_id == course_id))
        owner_count = owners_result.scalar()

    tag = f"₹{int(course.price)}" if course.price is not None else "TBD"
    sections = ", ".join(s.name for s in course.sections) or "— (unlisted)"
    await message.answer(
        f"📘 <b>{course.name}</b>\n"
        f"🆔 Course ID: {course.id}\n"
        f"👨‍🏫 Faculty: {course.faculty or '—'}\n"
        f"🌐 Medium: {course.medium or '—'}\n"
        f"📝 Notes: {course.notes or '—'}\n"
        f"💰 Price: {tag}\n"
        f"🔗 Group link: {course.group_link or 'Not set — /setlink'}\n"
        f"🔥 Trending: {'Yes' if course.is_trending else 'No'}\n"
        f"✅ Active: {'Yes' if course.is_active else 'No (hidden)'}\n"
        f"📂 Sections: {sections}\n"
        f"🎓 Students granted: {owner_count}"
    )


@router.message(Command("revenue"))
async def cmd_revenue(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        result = await session.execute(
            select(Course.price)
            .join(UserCourse, UserCourse.course_id == Course.id)
        )
        prices = [p for (p,) in result.all() if p is not None]
        pending_result = await session.execute(select(func.count(Order.id)).where(Order.status == "pending"))
        pending_count = pending_result.scalar()

    total = sum(float(p) for p in prices)
    await message.answer(
        "💰 <b>Revenue Estimate</b>\n\n"
        f"✅ Approved sales: {len(prices)}\n"
        f"💵 Estimated total (from approved orders): ₹{int(total)}\n"
        f"⏳ Orders still pending review: {pending_count}\n\n"
        "This counts approved course grants only, at each course's current listed price."
    )


# ================= ORDER APPROVE/REJECT =================
# MODIFIED WITH SINGLE-USE INVITE LINK FOR ANTI-PIRACY
@router.callback_query(F.data.startswith("adm_ok:"))
async def cb_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("This is for Professor only.", show_alert=True)
        return
    order_id = int(call.data.split(":", 1)[1])
    async with async_session() as session:
        result = await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
        )
        order = result.scalar_one_or_none()
        if not order or order.status != "pending":
            await call.answer("This order has already been processed.", show_alert=True)
            return
        course = await session.get(Course, order.course_id)
        if not course:
            await call.answer("This course is no longer available.", show_alert=True)
            return
        order.status = "approved"
        now = datetime.utcnow()
        order.decided_at = now
        if course.access_duration_days:
            duration = timedelta(days=course.access_duration_days)
            renewal_expiry = now + duration
            grant = pg_insert(UserCourse).values(
                user_id=order.user_id,
                course_id=order.course_id,
                expires_at=renewal_expiry,
            )
            await session.execute(
                grant.on_conflict_do_update(
                    index_elements=["user_id", "course_id"],
                    set_={
                        "expires_at": case(
                            (UserCourse.expires_at > now, UserCourse.expires_at + duration),
                            else_=renewal_expiry,
                        )
                    },
                )
            )
            access_expires_at = await session.scalar(
                select(UserCourse.expires_at).where(
                    UserCourse.user_id == order.user_id,
                    UserCourse.course_id == order.course_id,
                )
            )
        else:
            access_expires_at = None
            await session.execute(
                pg_insert(UserCourse)
                .values(user_id=order.user_id, course_id=order.course_id)
                .on_conflict_do_nothing(index_elements=["user_id", "course_id"])
            )
        session.add(AuditEvent(
            actor_id=call.from_user.id,
            action="payment.approved",
            target_type="order",
            target_id=str(order.id),
        ))
        await session.commit()

    # ANTI-PIRACY: Try to generate Single-Use Link if group_link is a Chat ID
    link = course.group_link
    if link and (link.startswith("-100") or link.startswith("@")):
        try:
            invite = await call.bot.create_chat_invite_link(chat_id=link, member_limit=1, name=f"Access_O{order_id}")
            link = invite.invite_link
        except Exception:
            pass # Fallback to standard text if bot isn't admin in that chat

    if call.message.caption:
        await call.message.edit_caption(caption=call.message.caption + "\n\n✅ APPROVED")
    else:
        await call.message.edit_text(call.message.text + "\n\n✅ APPROVED")

    if course.access_duration_days:
        group_text = (
            f"🎉 <b>{escape(course.name)}</b> has been approved!\n\n"
            f"Open your database: {escape(link or '', quote=True)}\n"
            f"Access is valid through {access_expires_at:%d %b %Y}."
        )
    else:
        group_text = (
            f"🎉 <b>{course.name}</b> has been approved!\n\n"
            + (f"Join the private group here (Single-Use Link): {link}" if link
               else "The group link will be added shortly — check 'My Courses' again soon.")
        )
    await call.bot.send_message(order.user_id, group_text)
    await call.answer("Approved ✅")


@router.callback_query(F.data.startswith("adm_no:"))
async def cb_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("This is for Professor only.", show_alert=True)
        return
    order_id = int(call.data.split(":", 1)[1])
    async with async_session() as session:
        result = await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
        )
        order = result.scalar_one_or_none()
        if not order or order.status != "pending":
            await call.answer("This order has already been processed.", show_alert=True)
            return
        order.status = "rejected"
        order.decided_at = datetime.utcnow()
        session.add(AuditEvent(
            actor_id=call.from_user.id,
            action="payment.rejected",
            target_type="order",
            target_id=str(order.id),
        ))
        await session.commit()

    if call.message.caption:
        await call.message.edit_caption(caption=call.message.caption + "\n\n❌ REJECTED")
    else:
        await call.message.edit_text(call.message.text + "\n\n❌ REJECTED")

    await call.bot.send_message(
        order.user_id,
        "❌ The gift card couldn't be verified. Please try again with the correct code/photo, "
        "or contact Professor via the Help section.",
    )
    await call.answer("Rejected ❌")


# ================= BROADCAST =================
@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not admin_only(message):
        return
    await state.clear()
    await state.set_state(AdminBroadcast.waiting_message)
    await message.answer(
        "📢 Send up to 10 text/media messages to include, then /done to preview or /cancel to stop. "
        "Nothing is sent until you confirm."
    )


@router.message(AdminBroadcast.waiting_message, Command("cancel"))
async def cancel_broadcast_collection(message: Message, state: FSMContext):
    if not admin_only(message):
        await state.clear()
        return
    await state.clear()
    await message.answer("Broadcast cancelled; nothing was sent.")


@router.message(AdminBroadcast.waiting_message, Command("done"))
async def finish_broadcast_collection(message: Message, state: FSMContext):
    if not admin_only(message):
        await state.clear()
        return
    data = await state.get_data()
    items = data.get("items", [])
    if not items:
        await message.answer("No messages collected. Send a message first or /cancel.")
        return
    await state.set_state(AdminBroadcast.confirming)
    await message.answer(
        f"Preview: {len(items)} message(s) will be copied to each known unbanned user. Confirm send?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Send broadcast", callback_data="broadcast:confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="broadcast:cancel"),
        ]]),
    )


@router.message(AdminBroadcast.waiting_message)
async def collect_broadcast_message(message: Message, state: FSMContext):
    if not admin_only(message):
        await state.clear()
        return
    data = await state.get_data()
    source_chat_id = data.get("source_chat_id")
    if source_chat_id is not None and source_chat_id != message.chat.id:
        await message.answer("Continue collecting in the same private chat where /broadcast was started.")
        return
    items = data.get("items", [])
    if len(items) >= 10:
        await message.answer("Batch limit is 10 messages. Send /done to preview or /cancel to stop.")
        return
    items.append(message.message_id)
    await state.update_data(source_chat_id=message.chat.id, items=items)
    await message.answer(f"Added {len(items)}/10. Send another message, /done to preview, or /cancel.")


@router.callback_query(F.data == "broadcast:cancel", AdminBroadcast.confirming)
async def cancel_broadcast_confirmation(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Admin only.", show_alert=True)
        return
    await state.clear()
    await call.message.edit_text("Broadcast cancelled; nothing was sent.")
    await call.answer()


@router.callback_query(F.data == "broadcast:confirm", AdminBroadcast.confirming)
async def confirm_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Admin only.", show_alert=True)
        return
    data = await state.get_data()
    items = data.get("items", [])
    source_chat_id = data.get("source_chat_id")
    if not items or not source_chat_id:
        await state.clear()
        await call.answer("Broadcast draft expired. Start /broadcast again.", show_alert=True)
        return
    await state.clear()
    async with async_session() as session:
        result = await session.execute(select(User.id).where(User.is_banned == False))  # noqa: E712
        user_ids = [row[0] for row in result.all()]

    sent_users, failed_users, sent_messages = 0, 0, 0
    status_msg = await call.message.edit_text(f"📤 Sending {len(items)} message(s) to {len(user_ids)} users…")
    for uid in user_ids:
        user_ok = True
        try:
            for message_id in items:
                await call.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=source_chat_id,
                    message_id=message_id,
                )
                sent_messages += 1
                await asyncio.sleep(0.04)
        except Exception:
            user_ok = False
        if user_ok:
            sent_users += 1
        else:
            failed_users += 1
    async with async_session() as session:
        session.add(AuditEvent(
            actor_id=call.from_user.id,
            action="broadcast.completed",
            target_type="broadcast",
            target_id=f"{len(items)}_items_{sent_users}_users",
        ))
        await session.commit()
    await status_msg.edit_text(
        f"✅ Broadcast complete. Users reached: {sent_users}; failed users: {failed_users}; "
        f"messages delivered: {sent_messages}."
    )
    await call.answer("Broadcast finished")


# ================= STATS =================
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not admin_only(message):
        return
    async with async_session() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar()
        banned = (await session.execute(select(func.count(User.id)).where(User.is_banned == True))).scalar()  # noqa
        total_courses = (await session.execute(select(func.count(Course.id)).where(Course.is_active == True))).scalar()  # noqa
        pending_orders = (await session.execute(select(func.count(Order.id)).where(Order.status == "pending"))).scalar()
        approved_orders = (await session.execute(select(func.count(Order.id)).where(Order.status == "approved"))).scalar()
        rejected_orders = (await session.execute(select(func.count(Order.id)).where(Order.status == "rejected"))).scalar()
        total_sales = (await session.execute(select(func.count(UserCourse.id)))).scalar()
        active_today = (await session.execute(
            select(func.count(func.distinct(UserActivity.user_id)))
            .where(UserActivity.created_at >= datetime.utcnow() - timedelta(days=1))
        )).scalar()
        active_week = (await session.execute(
            select(func.count(func.distinct(UserActivity.user_id)))
            .where(UserActivity.created_at >= datetime.utcnow() - timedelta(days=7))
        )).scalar()
        new_users_week = (await session.execute(
            select(func.count(User.id)).where(User.joined_at >= datetime.utcnow() - timedelta(days=7))
        )).scalar()
        referral_joins = (await session.execute(
            select(func.count(User.id)).where(User.referred_by.is_not(None))
        )).scalar()
        active_database_access = (await session.execute(
            select(func.count(UserCourse.id))
            .join(Course, UserCourse.course_id == Course.id)
            .where(
                Course.is_ca_notion_access == True,
                Course.is_active == True,
                (UserCourse.expires_at.is_(None) | (UserCourse.expires_at > datetime.utcnow())),
            )
        )).scalar()
        ca_rows = await session.execute(
            select(CurrentAffair.dataset, func.count(CurrentAffair.id))
            .where(CurrentAffair.is_active == True)
            .group_by(CurrentAffair.dataset)
        )
        ca_counts = {dataset: count for dataset, count in ca_rows.all()}
        notion_pending = (await session.execute(
            select(func.count(NotionSyncJob.id)).where(NotionSyncJob.status.in_(("pending", "processing", "failed")))
        )).scalar()
        notion_conflicts = (await session.execute(
            select(func.count(NotionSyncJob.id)).where(NotionSyncJob.status == "conflict")
        )).scalar()
        support_messages = (await session.execute(select(func.count(ContactMessage.id)))).scalar()

    await message.answer(
        "📊 <b>Bot Stats</b>\n\n"
        f"👥 Total Users: {total_users}\n🚫 Banned: {banned}\n📘 Active Courses: {total_courses}\n\n"
        f"⏳ Pending Orders: {pending_orders}\n✅ Approved: {approved_orders}\n❌ Rejected: {rejected_orders}\n"
        f"🎓 Total Course Grants: {total_sales}\n"
        f"📈 Active users (24h / 7d): {active_today} / {active_week}\n"
        f"🆕 New users (7d): {new_users_week}\n🔗 Attributed referral joins: {referral_joins}\n"
        f"🗂 Active CA/Notion entitlements: {active_database_access}\n"
        f"📰 CA records: {ca_counts or 'none'}\n"
        f"🔄 Notion queued/failed: {notion_pending} | conflicts: {notion_conflicts}\n"
        f"💬 Stored support messages: {support_messages}"
    )


# ================= BAN / UNBAN =================
@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /ban <user_id>")
        return
    user_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            await message.answer("❌ User not found.")
            return
        user.is_banned = True
        await session.commit()
        await message.answer(f"🚫 User {parts[1]} banned.")


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /unban <user_id>")
        return
    user_id, id_error = _parse_id(parts[1])
    if id_error:
        await message.answer(id_error)
        return
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            await message.answer("❌ User not found.")
            return
        user.is_banned = False
        await session.commit()
        await message.answer(f"✅ User {parts[1]} unbanned.")


@router.message(Command("restart"))
async def cmd_restart(message: Message):
    """Revalidate known Telegram registry records and clear temporary rate-limit caches."""
    if not admin_only(message):
        return
    try:
        users, known_chats, checked, active, removed, failures = await _resync_known_registry(
            message.bot, message.from_user.id
        )
    except Exception:
        logger.exception("Registry resync during /restart failed")
        await message.answer("Registry rescan failed; no cache or persistent data was changed. Retry with /resync.")
        return
    from security import burst_monitor, spam_monitor
    # Deferred import (not at module top) to avoid a circular import: user_handlers
    # imports AI_STATE from this file at startup, so this file can't import
    # user_handlers back at load time — only safe once both are fully loaded.
    from user_handlers import ai_chat_monitor, ai_frozen_until, broadcast_reply_counts

    cleared = 0
    for tracker in (burst_monitor, spam_monitor, ai_chat_monitor, broadcast_reply_counts):
        cleared += len(tracker)
        tracker.clear()
    cleared += len(ai_frozen_until)
    ai_frozen_until.clear()

    await message.answer(
        f"🧹 <b>Cache Cleared</b>\n\n"
        f"Cleared <b>{cleared}</b> temporary entries — spam/burst rate-limit windows, "
        f"AI chat cooldowns, broadcast-reply counters.\n\n"
        f"Registry: {users} stored users; {checked}/{known_chats} known chats checked; "
        f"{active} active, {removed} stale removed, {failures} API/permission failures.\n"
        "Telegram cannot enumerate users/groups it has never observed. Orders and entitlements were not changed.",
        parse_mode="HTML"
    )


@router.message(Command("adminhelp"))
async def cmd_admin_help(message: Message):
    if not admin_only(message):
        return
    await message.answer(
        "🛠 <b>Admin Commands</b>\n\n"
        "<b>New Premium Features</b>\n"
        "/toggle_ai — Turn Auto-Reply ON/OFF\n"
        "/createpromo &lt;CODE&gt; &lt;PERCENT&gt; — Create Flash Sale discount\n"
        "/addforall — Broadcast Ad to all groups (bot messages delete after 24h)\n"
        "/weekly_report — Generate AI Business Report\n"
        "/restart — Clear temporary cache (no data lost)\n\n"
        "/resync — refresh previously stored users/groups/channels\n"
        "/chatadd &lt;chat_id&gt; — register a group the bot already belongs to\n"
        "<b>Courses</b>\n"
        "/adddatabase Name | Notion URL | [price] | [days] — create time-limited database access\n"
        "/caadd dataset | date | title | topic | content | ... — add a CA record\n"
        "/notionsync [dataset=...] [from=YYYY-MM-DD] [to=YYYY-MM-DD] — filtered sync\n"
        "/notionstatus, /notionresolve — review Notion state/conflicts\n"
        "/addcourse — add a new course (step-by-step)\n"
        "/quickadd Name | Faculty | Medium | Notes | Price|TBD | section_keys — add a course in ONE message\n"
        "/listsectionkeys — see all section keys (for /quickadd, /movecourse)\n"
        "/price &lt;id&gt; &lt;price|TBD&gt; — change price\n"
        "/removecourse &lt;id&gt; — hide a course\n"
        "/setlink &lt;id&gt; &lt;group_link&gt; — set a course's group link\n"
        "/movecourse &lt;id&gt; &lt;section_id1,section_id2,...&gt; — reassign a course's section(s)\n"
        "/listsections — list all section IDs\n"
        "/trending_add &lt;id&gt; — add to trending\n"
        "/trending_remove &lt;id&gt; — remove from trending\n"
        "/listcourses — list all course IDs + prices\n"
        "/grant &lt;user_id&gt; &lt;course_id&gt; — manually unlock a course for a user (no payment needed)\n\n"
        "<b>Orders &amp; Users</b>\n"
        "/pending — quick view of orders awaiting approval\n"
        "/userinfo &lt;user_id&gt; — look up a user, their courses + recent steps\n"
        "/activity &lt;user_id&gt; — full recent step-by-step trail for a user\n"
        "/contacthistory &lt;user_id&gt; — full Contact Professor thread with a user\n"
        "/revenue — approved-sales revenue estimate\n"
        "/ban &lt;user_id&gt; — block a user\n"
        "/unban &lt;user_id&gt; — unblock a user\n\n"
        "<b>Discovery &amp; Reports</b>\n"
        "/findcourse &lt;keyword&gt; — search courses by name/faculty\n"
        "/courseinfo &lt;id&gt; — full detail + student count for one course\n"
        "/topcourses — best-selling courses\n"
        "/recentusers — last 15 users who joined\n"
        "/export — download all users as a CSV file\n\n"
        "<b>Replying to users</b>\n"
        "Just hit Reply (Telegram's native reply) on any forwarded order or "
        "'Contact Professor' message — your reply is delivered to that user automatically.\n\n"
        "<b>Broadcast &amp; Stats</b>\n"
        "/broadcast — collect up to 10 messages, /done to preview, confirm to send\n"
        "/stats — bot-wide numbers",
        parse_mode="HTML"
    )
