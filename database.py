"""
Database: models + engine/session + initial section-tree seed + full course
catalog seed, all in one file.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean, ForeignKey,
    DateTime, Table, Numeric, UniqueConstraint, select
)
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config import DATABASE_URL
from course_seed_data import COURSE_SEED

Base = declarative_base()

course_sections = Table(
    "course_sections",
    Base.metadata,
    Column("course_id", Integer, ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True),
    Column("section_id", Integer, ForeignKey("sections.id", ondelete="CASCADE"), primary_key=True),
)


class Section(Base):
    __tablename__ = "sections"

    id = Column(Integer, primary_key=True)
    key = Column(String(80), nullable=False, unique=True)
    name = Column(String(120), nullable=False)
    emoji = Column(String(10), default="📘")
    parent_id = Column(Integer, ForeignKey("sections.id"), nullable=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    courses = relationship("Course", secondary=course_sections, back_populates="sections", lazy="selectin")


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    faculty = Column(String(150), default="")
    medium = Column(String(50), default="")
    notes = Column(Text, default="")
    price = Column(Numeric(10, 2), nullable=True)   # NULL = "Coming Soon"
    group_link = Column(String(300), nullable=True)
    is_trending = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    is_seeded = Column(Boolean, default=False, nullable=False)
    access_duration_days = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sections = relationship("Section", secondary=course_sections, back_populates="courses", lazy="selectin")


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    username = Column(String(100), nullable=True)
    first_name = Column(String(150), nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)
    is_banned = Column(Boolean, default=False)
    has_joined_backup_channel = Column(Boolean, default=False)
    backup_channel_checked_at = Column(DateTime, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    status = Column(String(20), default="pending")
    submission_type = Column(String(10), default="text")
    submission_content = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)


class UserCourse(Base):
    __tablename__ = "user_courses"
    __table_args__ = (UniqueConstraint("user_id", "course_id", name="uq_user_courses_user_course"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    granted_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


class UserActivity(Base):
    __tablename__ = "user_activity"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    step = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ContactMessage(Base):
    __tablename__ = "contact_messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    direction = Column(String(10), nullable=False)  # "in" (user->admin) or "out" (admin->user)
    content = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ConnectedChat(Base):
    __tablename__ = "connected_chats"

    id = Column(BigInteger, primary_key=True)
    type = Column(String(50)) # 'group', 'supergroup', or 'channel'
    added_at = Column(DateTime, default=datetime.utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True)
    actor_id = Column(BigInteger, nullable=False)
    action = Column(String(100), nullable=False)
    target_type = Column(String(50), nullable=False)
    target_id = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"

    update_id = Column(BigInteger, primary_key=True)
    status = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class PaymentProof(Base):
    __tablename__ = "payment_proofs"

    proof_hash = Column(String(64), primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ScheduledDeletion(Base):
    __tablename__ = "scheduled_deletions"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_scheduled_deletions_chat_message"),)

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(Integer, nullable=False)
    delete_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)


# ---------------- engine / session ----------------
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def log_step(user_id: int, step: str):
    try:
        async with async_session() as session:
            session.add(UserActivity(user_id=user_id, step=step))
            await session.commit()
    except Exception:
        pass


# ---------------- initial section tree ----------------
def _slug(s: str) -> str:
    return s.lower().replace(" ", "_").replace("&", "and").replace("/", "_")


OPTIONAL_SUBJECTS = [
    "Anthropology", "PSIR", "Sociology", "History", "Geography", "Philosophy",
    "Public Administration", "Psychology", "Commerce & Accountancy",
    "Hindi Literature", "Mathematics", "Economics", "Law", "Forestry",
    "Geology", "Agriculture",
]

SECTION_TREE = {
    ("upsc", "UPSC", "🏛"): [
        ("upsc_foundation", "Foundation", "⌂"),
        ("upsc_csat", "CSAT", "🧮"),
        ("upsc_pyq", "PYQ / Answer Practice", "🗂"),
        ("upsc_crash", "Crash Course", "⚡"),
        ("upsc_ca", "Current Affairs", "📰"),
    ],
    ("prelims_mains", "Prelims & Mains Course", "♛"): [
        ("upsc_prelims", "Prelims Specific", "◎"),
        ("upsc_mains", "Mains Specific", "♛"),
    ],
    ("test_series", "Test Series", "📝"): [
        ("upsc_ts_prelims", "Prelims Test Series", "📝"),
        ("upsc_ts_mains", "Mains Test Series", "📝"),
    ],
    ("subject_specific", "Subject Specific Course", "🧑‍🏫"): [
        ("subj_economy", "Economics (incl. Mrunal)", "💹"),
        ("subj_geography", "Geography (incl. Sudarshan Gujjar)", "🗺"),
        ("subj_polity", "Polity & Governance (incl. Jatin Gupta)", "🏛"),
        ("subj_environment_scitech", "Environment + Sci-Tech", "🌱"),
        ("subj_history", "History", "📜"),
        ("subj_ir", "International Relations", "🌐"),
        ("subj_public_admin", "Public Administration", "🏢"),
        ("upsc_ethics", "Ethics (GS-4)", "⚖️"),
        ("upsc_essay", "Essay", "✍️"),
        ("upsc_subject_specific", "Other Subject Courses", "📚"),
    ],
    ("upsc_optional", "UPSC Optional", "📗"): [
        (f"optional_{_slug(s)}", s, "📗") for s in OPTIONAL_SUBJECTS
    ],
    ("state_psc", "All State PSC", "🏢"): [
        ("uppsc", "UPPSC", "🏢"),
        ("bpsc", "BPSC", "🏢"),
        ("rpsc", "RPSC", "🏢"),
        ("jpsc", "JPSC", "🏢"),
        ("ukpsc", "UKPSC", "🏢"),
    ],
    ("net_jrf", "NET / JRF", "🎓"): [
        ("net_jrf_all", "All Subjects", "🎓"),
    ],
    ("combo", "Combo Deals & Bundles", "🎁"): [
        ("combo_deals", "Combo Deals", "🎁"),
    ],
    ("other", "Other Courses", "📦"): [
        ("other_misc", "Miscellaneous", "📦"),
    ],
}


async def seed_sections():
    async with async_session() as session:
        existing = (await session.execute(select(Section))).scalars().first()
        if existing:
            return
        for (top_key, top_name, top_emoji), children in SECTION_TREE.items():
            top = Section(key=top_key, name=top_name, emoji=top_emoji, parent_id=None)
            session.add(top)
            await session.flush()
            for key, name, emoji in children:
                session.add(Section(key=key, name=name, emoji=emoji, parent_id=top.id))
        await session.commit()


async def seed_courses():
    async with async_session() as session:
        existing = (await session.execute(select(Course))).scalars().first()
        if existing:
            return
        result = await session.execute(select(Section))
        sections_by_key = {s.key: s for s in result.scalars().all()}

        for name, faculty, medium, notes, price, section_keys, batch_id in COURSE_SEED:
            course = Course(name=name, faculty=faculty, medium=medium, notes=notes, price=price, is_seeded=True)
            for key in section_keys:
                sec = sections_by_key.get(key)
                if sec:
                    course.sections.append(sec)
            session.add(course)
        await session.commit()


# ---------------- v2 migration (safe on existing/deployed DBs) ----------------
NEW_TOP_SECTIONS = [
    ("prelims_mains", "Prelims & Mains Course", "♛"),
    ("test_series", "Test Series", "📝"),
    ("subject_specific", "Subject Specific Course", "🧑‍🏫"),
]

NEW_SUBJECT_CHILDREN = [
    ("subj_economy", "Economics (incl. Mrunal)", "💹"),
    ("subj_geography", "Geography (incl. Sudarshan Gujjar)", "🗺"),
    ("subj_polity", "Polity & Governance (incl. Jatin Gupta)", "🏛"),
    ("subj_environment_scitech", "Environment + Sci-Tech", "🌱"),
    ("subj_history", "History", "📜"),
    ("subj_ir", "International Relations", "🌐"),
    ("subj_public_admin", "Public Administration", "🏢"),
]

REPARENT = [
    ("upsc_prelims", "prelims_mains"),
    ("upsc_mains", "prelims_mains"),
    ("upsc_ts_prelims", "test_series"),
    ("upsc_ts_mains", "test_series"),
    ("upsc_ethics", "subject_specific"),
    ("upsc_essay", "subject_specific"),
    ("upsc_subject_specific", "subject_specific"),
]

KEYWORD_SECTION_MAP = [
    (["mrunal", "economy", "economics", "pcb", "shivin", "jayant", "aditya kaliya",
      "basava", "rishi jain", "bookstawa"], "subj_economy"),
    (["geography", "gujjar", "gurjar", "thapa", "himanshu"], "subj_geography"),
    (["polity", "governance", "jatin gupta", "sidharth arora", "laxmikanth"], "subj_polity"),
    (["environment", "sci-tech", "sci & tech", "science", "ecology", "pmf",
      "cp kaushik", "ravi agrahari"], "subj_environment_scitech"),
    (["history"], "subj_history"),
    (["international relations", "ir ", "chetan"], "subj_ir"),
    (["public administration", "pub ad"], "subj_public_admin"),
]

EXPLICIT_NEW_COURSES = [
    ("Polity & Governance — Jatin Gupta", "Jatin Gupta", "", "", None, ["subj_polity"]),
]


async def migrate_v2():
    async with async_session() as session:
        result = await session.execute(select(Section))
        sections = {s.key: s for s in result.scalars().all()}
        if not sections:
            return  

        changed = False

        for key, name, emoji in NEW_TOP_SECTIONS:
            if key not in sections:
                sec = Section(key=key, name=name, emoji=emoji, parent_id=None)
                session.add(sec)
                await session.flush()
                sections[key] = sec
                changed = True

        for key, name, emoji in NEW_SUBJECT_CHILDREN:
            if key not in sections:
                parent = sections["subject_specific"]
                sec = Section(key=key, name=name, emoji=emoji, parent_id=parent.id)
                session.add(sec)
                await session.flush()
                sections[key] = sec
                changed = True

        for child_key, new_parent_key in REPARENT:
            child = sections.get(child_key)
            parent = sections.get(new_parent_key)
            if child and parent and child.parent_id != parent.id:
                child.parent_id = parent.id
                changed = True

        if changed:
            await session.commit()

        catchall = sections.get("upsc_subject_specific")
        if catchall:
            result = await session.execute(select(Course))
            all_courses = result.scalars().all()
            catchall_courses = [
                c for c in all_courses if any(s.key == "upsc_subject_specific" for s in c.sections)
            ]
            for course in catchall_courses:
                haystack = f"{course.name} {course.faculty}".lower()
                existing_keys = {s.key for s in course.sections}
                for keywords, target_key in KEYWORD_SECTION_MAP:
                    if target_key in existing_keys:
                        continue
                    if any(kw in haystack for kw in keywords):
                        target_sec = sections.get(target_key)
                        if target_sec:
                            course.sections.append(target_sec)
            await session.commit()

        result = await session.execute(select(Course.name))
        existing_names = {n for (n,) in result.all()}
        for name, faculty, medium, notes, price, section_keys in EXPLICIT_NEW_COURSES:
            if name in existing_names:
                continue
            course = Course(name=name, faculty=faculty, medium=medium, notes=notes, price=price)
            for key in section_keys:
                sec = sections.get(key)
                if sec:
                    course.sections.append(sec)
            session.add(course)
        await session.commit()

        result = await session.execute(select(Course))
        db_courses = result.scalars().all()
        
        seed_dict = {}
        for name, faculty, medium, notes, price, section_keys, batch_id in COURSE_SEED:
            seed_dict[name] = {
                "faculty": faculty, "medium": medium, "notes": notes, 
                "price": price, "section_keys": section_keys
            }

        courses_changed = False
        
        for course in db_courses:
            if course.name in seed_dict:
                seed_data = seed_dict[course.name]
                course.price = seed_data["price"]
                course.faculty = seed_data["faculty"]
                course.medium = seed_data["medium"]
                course.notes = seed_data["notes"]
                course.is_active = True
                course.is_seeded = True
                del seed_dict[course.name]
                courses_changed = True
            else:
                if course.is_seeded and course.is_active:
                    course.is_active = False
                    courses_changed = True

        for name, seed_data in seed_dict.items():
            new_course = Course(
                name=name, 
                faculty=seed_data["faculty"], 
                medium=seed_data["medium"], 
                notes=seed_data["notes"], 
                price=seed_data["price"],
                is_active=True,
                is_seeded=True,
            )
            for key in seed_data["section_keys"]:
                sec = sections.get(key)
                if sec:
                    new_course.sections.append(sec)
            session.add(new_course)
            courses_changed = True
            
        if courses_changed:
            await session.commit()


# ---------------- v3 migration: simplified home screen ----------------
V3_REPARENT = [
    ("upsc_optional", "upsc"),
    ("upsc_ts_prelims", "upsc"),
    ("upsc_ts_mains", "upsc"),
    ("net_jrf_all", "subject_specific"),
    ("combo_deals", "subject_specific"),
    ("other_misc", "subject_specific"),
]

V3_ADDITIVE_TAGS = [
    ("upsc_csat", "upsc_prelims"),
    ("upsc_pyq", "upsc_prelims"),
    ("upsc_pyq", "upsc_mains"),
    ("upsc_ca", "upsc_foundation"),
]

V3_RENAME = {
    "upsc_ts_prelims": "Prelims Test Series 2027",
    "upsc_ts_mains": "Mains Test Series 2027",
}


async def migrate_v3():
    async with async_session() as session:
        result = await session.execute(select(Section))
        sections = {s.key: s for s in result.scalars().all()}
        if not sections:
            return

        changed = False
        for child_key, new_parent_key in V3_REPARENT:
            child = sections.get(child_key)
            parent = sections.get(new_parent_key)
            if child and parent and child.parent_id != parent.id:
                child.parent_id = parent.id
                changed = True

        for key, new_name in V3_RENAME.items():
            sec = sections.get(key)
            if sec and sec.name != new_name:
                sec.name = new_name
                changed = True

        if changed:
            await session.commit()

        result = await session.execute(select(Course))
        all_courses = result.scalars().all()
        for source_key, target_key in V3_ADDITIVE_TAGS:
            source_sec = sections.get(source_key)
            target_sec = sections.get(target_key)
            if not source_sec or not target_sec:
                continue
            tagged = [c for c in all_courses if any(s.key == source_key for s in c.sections)]
            for course in tagged:
                existing_keys = {s.key for s in course.sections}
                if target_key not in existing_keys:
                    course.sections.append(target_sec)
        await session.commit()


async def migrate_v4():
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS backup_channel_checked_at TIMESTAMP NULL"
        )


async def migrate_v5():
    async with engine.begin() as conn:
        constraint = await conn.exec_driver_sql(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'uq_user_courses_user_course' "
            "AND conrelid = 'user_courses'::regclass"
        )
        if constraint.scalar_one_or_none() is not None:
            return

        await conn.exec_driver_sql(
            "DELETE FROM user_courses older USING user_courses newer "
            "WHERE older.user_id = newer.user_id AND older.course_id = newer.course_id AND older.id > newer.id"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE user_courses ADD CONSTRAINT uq_user_courses_user_course UNIQUE (user_id, course_id)"
        )


async def migrate_v6():
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "ALTER TABLE courses ADD COLUMN IF NOT EXISTS is_seeded BOOLEAN NOT NULL DEFAULT FALSE"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE courses ADD COLUMN IF NOT EXISTS access_duration_days INTEGER NULL"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE user_courses ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP NULL"
        )
