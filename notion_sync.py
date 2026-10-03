import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, or_, select

from config import NOTION_API_KEY, NOTION_API_VERSION, NOTION_DATABASE_ID
from database import CurrentAffair, NotionSyncJob, NotionSyncState, async_session

logger = logging.getLogger(__name__)
NOTION_API_BASE = "https://api.notion.com/v1"
CA_DATASETS = {"daily_ca", "editorial", "place_in_news", "international_orgs"}


class NotionSyncConflict(Exception):
    pass


def notion_sync_configured() -> bool:
    return bool(NOTION_API_KEY and NOTION_DATABASE_ID)


def _chunks(value: str, size: int = 1900) -> list[dict]:
    if not value:
        return []
    return [{"type": "text", "text": {"content": value[index:index + size]}}
            for index in range(0, len(value), size)]


def _text_property(value: str | None) -> dict:
    return {"rich_text": _chunks(value or "")}


def _page_properties(record: CurrentAffair) -> dict:
    tags = [tag.strip() for tag in (record.tags or "").split(",") if tag.strip()]
    attachments = record.attachments or ""
    return {
        "Title": {"title": _chunks(record.title[:2000])},
        "Date": {"date": {"start": record.affair_date.isoformat()}},
        "Dataset": {"select": {"name": record.dataset}},
        "Topic": _text_property(record.topic),
        "Subtopic": _text_property(record.subtopic),
        "Tags": {"multi_select": [{"name": tag[:100]} for tag in tags[:100]]},
        "Content": _text_property(record.content),
        "Source": _text_property(record.source_name),
        "Source URL": {"url": record.source_url or None},
        "UPSC Mapping": _text_property(record.upsc_mapping),
        "Prelims Mapping": _text_property(record.prelims_mapping),
        "Mains Mapping": _text_property(record.mains_mapping),
        "PYQ Mapping": _text_property(record.pyq_mapping),
        "Attachments": _text_property(attachments),
        "Image URL": {"url": record.image_url or None},
    }


def _read_text(properties: dict, name: str, kind: str = "rich_text") -> str:
    prop = properties.get(name) or {}
    items = prop.get(kind) or []
    return "".join(item.get("plain_text", item.get("text", {}).get("content", "")) for item in items)


def _read_page(page: dict) -> dict | None:
    properties = page.get("properties") or {}
    title = _read_text(properties, "Title", "title") or _read_text(properties, "Name", "title")
    raw_date = (properties.get("Date") or {}).get("date") or {}
    raw_dataset = (properties.get("Dataset") or {}).get("select") or {}
    try:
        affair_date = date.fromisoformat(raw_date.get("start", "")[:10])
    except ValueError:
        return None
    dataset = raw_dataset.get("name", "")
    if not title or dataset not in CA_DATASETS:
        return None
    tags = (properties.get("Tags") or {}).get("multi_select") or []
    source_url = (properties.get("Source URL") or {}).get("url")
    image_url = (properties.get("Image URL") or {}).get("url")
    return {
        "notion_page_id": page.get("id"),
        "notion_edited_at": _parse_notion_time(page.get("last_edited_time")),
        "dataset": dataset,
        "affair_date": affair_date,
        "title": title[:300],
        "topic": _read_text(properties, "Topic")[:150] or "Uncategorized",
        "subtopic": _read_text(properties, "Subtopic")[:150] or None,
        "tags": ",".join(tag.get("name", "") for tag in tags),
        "content": _read_text(properties, "Content"),
        "source_name": _read_text(properties, "Source")[:200] or None,
        "source_url": source_url,
        "upsc_mapping": _read_text(properties, "UPSC Mapping") or None,
        "prelims_mapping": _read_text(properties, "Prelims Mapping") or None,
        "mains_mapping": _read_text(properties, "Mains Mapping") or None,
        "pyq_mapping": _read_text(properties, "PYQ Mapping") or None,
        "attachments": _read_text(properties, "Attachments") or None,
        "image_url": image_url,
    }


def _parse_notion_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


async def _notion_request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    response = await client.request(method, f"{NOTION_API_BASE}{path}", **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"notion_http_{response.status_code}")
    return response.json()


def _push_properties(record: CurrentAffair) -> dict:
    return {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": _page_properties(record),
    }


async def _push_record(client: httpx.AsyncClient, record: CurrentAffair, force_portal: bool = False) -> str:
    now = datetime.utcnow()
    if record.notion_page_id:
        remote = await _notion_request(client, "GET", f"/pages/{record.notion_page_id}")
        remote_edited = _parse_notion_time(remote.get("last_edited_time"))
        if not force_portal and (
            record.portal_dirty
            and remote_edited
            and record.notion_edited_at
            and remote_edited > record.notion_edited_at
        ):
            raise NotionSyncConflict("conflict")
        page = await _notion_request(
            client,
            "PATCH",
            f"/pages/{record.notion_page_id}",
            json={"properties": _page_properties(record)},
        )
    else:
        page = await _notion_request(client, "POST", "/pages", json=_push_properties(record))
        record.notion_page_id = page.get("id")
    record.notion_edited_at = _parse_notion_time(page.get("last_edited_time"))
    record.last_synced_at = now
    record.portal_dirty = False
    return "synced"


async def _pull_database(client: httpx.AsyncClient) -> dict:
    cursor = None
    imported = updated = conflicts = skipped = 0
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response = await _notion_request(
            client, "POST", f"/databases/{NOTION_DATABASE_ID}/query", json=payload
        )
        pages = response.get("results", [])
        async with async_session() as session:
            for page in pages:
                fields = _read_page(page)
                if not fields:
                    skipped += 1
                    continue
                record = await session.scalar(
                    select(CurrentAffair).where(CurrentAffair.notion_page_id == fields["notion_page_id"])
                )
                if record:
                    remote_edited = fields["notion_edited_at"]
                    if (
                        record.portal_dirty
                        and remote_edited
                        and record.notion_edited_at
                        and remote_edited > record.notion_edited_at
                    ):
                        conflict_job = await session.scalar(
                            select(NotionSyncJob).where(
                                NotionSyncJob.affair_id == record.id,
                                NotionSyncJob.direction == "push",
                            )
                        )
                        if conflict_job:
                            conflict_job.status = "conflict"
                            conflict_job.last_error = "conflict"
                        else:
                            session.add(NotionSyncJob(
                                affair_id=record.id,
                                direction="push",
                                status="conflict",
                                last_error="conflict",
                            ))
                        conflicts += 1
                        continue
                    for key, value in fields.items():
                        if key not in ("notion_page_id", "notion_edited_at"):
                            setattr(record, key, value)
                    record.notion_edited_at = remote_edited
                    record.last_synced_at = datetime.utcnow()
                    record.portal_dirty = False
                    conflict_job = await session.scalar(
                        select(NotionSyncJob).where(
                            NotionSyncJob.affair_id == record.id,
                            NotionSyncJob.direction == "push",
                            NotionSyncJob.status == "conflict",
                        )
                    )
                    if conflict_job:
                        conflict_job.status = "synced"
                        conflict_job.last_error = None
                    updated += 1
                else:
                    record = CurrentAffair(**fields, last_synced_at=datetime.utcnow(), portal_dirty=False)
                    session.add(record)
                    imported += 1
            await session.commit()
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
        if not cursor:
            break
    return {"imported": imported, "updated": updated, "conflicts": conflicts, "skipped": skipped}


async def pull_notion_database() -> dict:
    if not notion_sync_configured():
        raise RuntimeError("notion_not_configured")
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            result = await _pull_database(client)
    except Exception as error:
        async with async_session() as session:
            state = await session.get(NotionSyncState, 1)
            if not state:
                state = NotionSyncState(id=1)
                session.add(state)
            state.last_pull_at = datetime.utcnow()
            state.last_pull_status = "failed"
            state.last_error = str(error)[:64] if str(error).startswith("notion_http_") else "notion_pull_failed"
            await session.commit()
        raise
    async with async_session() as session:
        state = await session.get(NotionSyncState, 1)
        if not state:
            state = NotionSyncState(id=1)
            session.add(state)
        state.last_pull_at = datetime.utcnow()
        state.last_pull_status = "succeeded"
        state.imported = result["imported"]
        state.updated = result["updated"]
        state.conflicts = result["conflicts"]
        state.skipped = result["skipped"]
        state.last_error = None
        await session.commit()
    return result


async def enqueue_all_for_notion_push() -> int:
    if not notion_sync_configured():
        raise RuntimeError("notion_not_configured")
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async with async_session() as session:
        result = await session.execute(select(CurrentAffair.id))
        affair_ids = [row[0] for row in result.all()]
        if affair_ids:
            for affair_id in affair_ids:
                await session.execute(
                    pg_insert(NotionSyncJob)
                    .values(affair_id=affair_id, direction="push", status="pending")
                    .on_conflict_do_update(
                        index_elements=["affair_id", "direction"],
                        set_={"status": "pending", "attempts": 0, "next_attempt_at": datetime.utcnow(), "last_error": None},
                    )
                )
            await session.commit()
    return len(affair_ids)


async def enqueue_notion_push(session, affair_id: int) -> bool:
    if not notion_sync_configured():
        return False
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    await session.execute(
        pg_insert(NotionSyncJob)
        .values(affair_id=affair_id, direction="push", status="pending")
        .on_conflict_do_update(
            index_elements=["affair_id", "direction"],
            set_={
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": datetime.utcnow(),
                "last_error": None,
            },
        )
    )
    return True


async def _process_job(job_id: int, client: httpx.AsyncClient) -> None:
    async with async_session() as session:
        job = await session.get(NotionSyncJob, job_id)
        record = await session.get(CurrentAffair, job.affair_id) if job else None
        if not job or not record:
            return
        try:
            await _push_record(client, record, force_portal=job.force_portal)
            job.status = "synced"
            job.force_portal = False
            job.last_error = None
            await session.commit()
        except NotionSyncConflict:
            job.status = "conflict"
            job.last_error = "conflict"
            await session.commit()
        except Exception as error:
            job.attempts += 1
            job.status = "failed"
            job.last_error = str(error)[:300] if str(error).startswith("notion_http_") else "notion_sync_failed"
            job.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(3600, 30 * (2 ** min(job.attempts, 7))))
            await session.commit()
            logger.warning("Notion sync job failed (%s)", job.last_error)


async def notion_sync_worker(poll_seconds: int = 30) -> None:
    if not notion_sync_configured():
        logger.info("Notion sync is not configured")
        return
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        last_pull = 0.0
        while True:
            try:
                now = datetime.utcnow()
                stale_before = now - timedelta(minutes=10)
                async with async_session() as session:
                    result = await session.execute(
                        select(NotionSyncJob.id)
                        .where(
                            NotionSyncJob.direction == "push",
                            or_(
                                and_(
                                    NotionSyncJob.status.in_(("pending", "failed")),
                                    NotionSyncJob.next_attempt_at <= now,
                                ),
                                and_(NotionSyncJob.status == "processing", NotionSyncJob.updated_at < stale_before),
                            ),
                        )
                        .order_by(NotionSyncJob.next_attempt_at)
                        .limit(20)
                        .with_for_update(skip_locked=True)
                    )
                    job_ids = [row[0] for row in result.all()]
                    for job_id in job_ids:
                        job = await session.get(NotionSyncJob, job_id)
                        if job:
                            job.status = "processing"
                    await session.commit()
                for job_id in job_ids:
                    await _process_job(job_id, client)
                if asyncio.get_running_loop().time() - last_pull >= 300:
                    try:
                        await _pull_database(client)
                    except Exception as error:
                        message = str(error)
                        safe_error = message if message.startswith("notion_http_") else "notion_pull_failed"
                        logger.warning("Notion pull failed (%s)", safe_error)
                    last_pull = asyncio.get_running_loop().time()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notion sync worker iteration failed")
            await asyncio.sleep(poll_seconds)