import asyncio
import io
import logging
import os
import random
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, unquote

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Audio,
    BufferedInputFile,
    CallbackQuery,
    ChosenInlineResult,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultCachedVoice,
    InputTextMessageContent,
    Message,
    Voice,
)
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8697143769:AAH78_fzfyUt8c3ecxpTM4IlV_Y3nr0sFMg").strip()
LOGGER_GROUP_ID = int(os.getenv("LOGGER_GROUP_ID", "-1003711505151"))
STORAGE_CHAT_ID = int(os.getenv("STORAGE_CHAT_ID", "-1003897917299"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "7549407961").split(",") if x.strip().isdigit()}

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://SANKIXD:SANKIXD@cluster0.dgogcjs.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "tsinlinebots").strip()

SUPPORT_CHANNEL_URL = os.getenv("SUPPORT_CHANNEL_URL", "https://t.me/TEAMSANKI").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ll_SANKI__II").strip().lstrip("@")
WELCOME_MEDIA_URL = os.getenv("WELCOME_IMAGE_URL", "https://graph.org/file/533cd5ce5414981c731d5-3831c6c74a2525572c.jpg").strip()
DEFAULT_THUMB_URL = os.getenv("DEFAULT_THUMB_URL", "https://graph.org/file/533cd5ce5414981c731d5-3831c6c74a2525572c.jpg").strip()


MAX_UPLOAD_SIZE_MB = float(os.getenv("MAX_UPLOAD_SIZE_MB", "10"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "60"))
INLINE_CACHE_SECONDS = int(os.getenv("INLINE_CACHE_SECONDS", "20"))
MYINSTANTS_ENABLED = os.getenv("MYINSTANTS_ENABLED", "true").lower() == "true"
MAX_MYINSTANTS_RESULTS = int(os.getenv("MAX_MYINSTANTS_RESULTS", "12"))

FREE_DAILY_LIMIT = 4
SUBSCRIPTION_COOLDOWN_SEC = 10
INLINE_RANDOM_LIMIT = 10

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI missing")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("sound-bot")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client[MONGODB_DB_NAME]
sounds_collection = db["sounds"]
users_collection = db["users"]
categories_collection = db["categories"]

# ============================================================
# MODELS / STATES
# ============================================================
@dataclass
class UploadedSound:
    name: str
    source: str
    uploader_id: int
    cached_voice_file_id: Optional[str] = None
    duration: Optional[int] = None
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    original_name: Optional[str] = None
    created_at: Optional[datetime] = None
    share_count: int = 0
    thumb_url: Optional[str] = None
    is_adult: bool = False
    category: Optional[str] = None
    premium: bool = False
    featured_until: Optional[datetime] = None

@dataclass
class ExternalSound:
    name: str
    page_url: str
    audio_url: str
    source: str = "myinstants"

@dataclass
class MediaPayload:
    kind: str
    file_id: str
    file_size: Optional[int]
    duration: Optional[int]
    mime_type: Optional[str]
    file_name: Optional[str]

class UploadStates(StatesGroup):
    waiting_media = State()
    waiting_name = State()
    waiting_category = State()

# ============================================================
# HELPERS
# ============================================================
def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\-_\s]+", "", text)
    text = re.sub(r"\s+", "-", text)
    text = text.strip("-_")
    return text or f"sound-{uuid.uuid4().hex[:8]}"

def upload_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Search Inline", switch_inline_query_current_chat="")],
            [InlineKeyboardButton(text="Use in any chat", switch_inline_query="")],
        ]
    )

def format_size_mb(size_bytes: Optional[int]) -> str:
    if not size_bytes:
        return "unknown"
    return f"{size_bytes / (1024 * 1024):.2f} MB"

def is_audio_document(doc: Document) -> bool:
    mime = (doc.mime_type or "").lower()
    filename = (doc.file_name or "").lower()
    audio_exts = (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac", ".opus")
    return mime.startswith("audio/") or filename.endswith(audio_exts)

def extract_media(msg: Message) -> Optional[MediaPayload]:
    if msg.voice:
        v: Voice = msg.voice
        return MediaPayload(
            kind="voice",
            file_id=v.file_id,
            file_size=v.file_size,
            duration=v.duration,
            mime_type=v.mime_type,
            file_name=None,
        )
    if msg.audio:
        a: Audio = msg.audio
        return MediaPayload(
            kind="audio",
            file_id=a.file_id,
            file_size=a.file_size,
            duration=a.duration,
            mime_type=a.mime_type,
            file_name=a.file_name,
        )
    if msg.document and is_audio_document(msg.document):
        d: Document = msg.document
        return MediaPayload(
            kind="document",
            file_id=d.file_id,
            file_size=d.file_size,
            duration=None,
            mime_type=d.mime_type,
            file_name=d.file_name,
        )
    return None

async def fetch_telegram_file_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    return buffer.getvalue()

async def transcode_to_ogg_opus(input_bytes: bytes) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-map", "0:a:0",
            "-vn",
            "-ac", "1",
            "-ar", "48000",
            "-c:a", "libopus",
            "-b:a", "48k",
            "-vbr", "on",
            "-compression_level", "10",
            "-f", "ogg",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not installed")

    stdout, stderr = await proc.communicate(input=input_bytes)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"ffmpeg failed: {err[:500]}")
    return stdout

async def is_adult_content(file_bytes: bytes, filename: str = "") -> bool:
    suspicious = ["porn", "xxx", "adult", "sex", "nude"]
    return any(word in filename.lower() for word in suspicious)

async def mirror_to_storage_voice(media: MediaPayload, title: str) -> str:
    title = clean_spaces(title)[:120]
    if media.kind == "voice":
        sent = await bot.send_voice(
            chat_id=STORAGE_CHAT_ID,
            voice=media.file_id,
            caption=title,
            disable_notification=True,
        )
    else:
        raw = await fetch_telegram_file_bytes(media.file_id)
        ogg_bytes = await transcode_to_ogg_opus(raw)
        sent = await bot.send_voice(
            chat_id=STORAGE_CHAT_ID,
            voice=BufferedInputFile(ogg_bytes, filename=f"{slugify(title)}.ogg"),
            caption=title,
            disable_notification=True,
        )
    if not sent.voice:
        raise RuntimeError("Storage chat did not return voice")
    return sent.voice.file_id

async def log_event(text: str, parse_mode: str = "HTML") -> None:
    if not LOGGER_GROUP_ID:
        return
    try:
        await bot.send_message(LOGGER_GROUP_ID, text, parse_mode=parse_mode)
    except Exception:
        logger.exception("Failed to log event")

async def ensure_indexes() -> None:
    await sounds_collection.create_index([("name_lower", 1)])
    await sounds_collection.create_index([("created_at", -1)])
    await sounds_collection.create_index([("uploader_id", 1)])
    await sounds_collection.create_index([("share_count", -1)])
    await sounds_collection.create_index([("category", 1)])
    await users_collection.create_index([("user_id", 1)], unique=True)
    await categories_collection.create_index([("user_id", 1), ("name", 1)])

# ============================================================
# USER MANAGEMENT (with badges & categories)
# ============================================================
async def get_user(user_id: int) -> dict:
    doc = await users_collection.find_one({"user_id": user_id})
    if not doc:
        doc = {
            "user_id": user_id,
            "total_uploads": 0,
            "warnings": 0,
            "banned": False,
            "subscription_expiry": None,
            "last_upload_time": None,
            "daily_uploads": {},
            "badges": [],
            "last_seen": datetime.utcnow(),
        }
        await users_collection.insert_one(doc)
    return doc

async def update_user(user_id: int, updates: dict) -> None:
    await users_collection.update_one({"user_id": user_id}, {"$set": updates})

async def can_upload(user_id: int, is_admin: bool = False) -> tuple[bool, str]:
    if is_admin:
        return True, ""
    user = await get_user(user_id)
    if user.get("banned", False):
        return False, "You are banned from uploading."
    now = datetime.utcnow()
    sub_exp = user.get("subscription_expiry")
    if sub_exp and sub_exp > now:
        last_up = user.get("last_upload_time")
        if last_up and (now - last_up).total_seconds() < SUBSCRIPTION_COOLDOWN_SEC:
            remain = SUBSCRIPTION_COOLDOWN_SEC - int((now - last_up).total_seconds())
            return False, f"Cooldown: wait {remain} seconds."
        return True, ""
    else:
        today = now.date().isoformat()
        daily = user.get("daily_uploads", {})
        count = daily.get(today, 0)
        if count >= FREE_DAILY_LIMIT:
            return False, f"Daily limit ({FREE_DAILY_LIMIT}) reached."
        return True, ""

async def record_upload(user_id: int, is_admin: bool = False) -> None:
    if is_admin:
        return
    user = await get_user(user_id)
    now = datetime.utcnow()
    updates = {"last_upload_time": now, "total_uploads": user.get("total_uploads", 0) + 1}
    sub_exp = user.get("subscription_expiry")
    if not sub_exp or sub_exp <= now:
        today = now.date().isoformat()
        daily = user.get("daily_uploads", {})
        daily[today] = daily.get(today, 0) + 1
        updates["daily_uploads"] = daily
    await update_user(user_id, updates)
    # Badge: First Upload
    if updates["total_uploads"] == 1:
        await add_badge(user_id, "🆕 First Upload")

async def add_warning(user_id: int, reason: str) -> int:
    user = await get_user(user_id)
    warnings = user.get("warnings", 0) + 1
    updates = {"warnings": warnings}
    if warnings >= 5:
        updates["banned"] = True
    await update_user(user_id, updates)
    await log_event(f"⚠️ User {user_id} warned ({warnings}/5): {reason}")
    return warnings

async def add_badge(user_id: int, badge: str) -> None:
    user = await get_user(user_id)
    badges = set(user.get("badges", []))
    if badge not in badges:
        badges.add(badge)
        await update_user(user_id, {"badges": list(badges)})
        logger.info(f"Badge added to user {user_id}: {badge}")

async def get_user_categories(user_id: int) -> List[str]:
    docs = await categories_collection.find({"user_id": user_id}).to_list(length=100)
    return [doc["name"] for doc in docs]

async def create_category(user_id: int, name: str) -> bool:
    name = clean_spaces(name)[:30]
    if not name:
        return False
    existing = await categories_collection.find_one({"user_id": user_id, "name": name})
    if existing:
        return True  # already exists
    # Check limit for normal users
    user = await get_user(user_id)
    is_subscribed = user.get("subscription_expiry") and user["subscription_expiry"] > datetime.utcnow()
    if not is_subscribed and user_id not in ADMIN_IDS:
        count = await categories_collection.count_documents({"user_id": user_id})
        if count >= 1:
            return False
    await categories_collection.insert_one({"user_id": user_id, "name": name})
    # Badge: Created 5 categories
    new_count = await categories_collection.count_documents({"user_id": user_id})
    if new_count >= 5:
        await add_badge(user_id, "📂 Created 5 Categories")
    return True

# ============================================================
# SAVE TO DB
# ============================================================
def _filter_dataclass_fields(data: dict, cls):
    allowed = {f.name for f in cls.__dataclass_fields__.values()}
    return {k: v for k, v in data.items() if k in allowed}

async def save_uploaded_sound(
    name: str,
    uploader_id: int,
    cached_voice_file_id: str,
    duration: Optional[int],
    size_bytes: Optional[int],
    mime_type: Optional[str],
    original_name: Optional[str],
    category: Optional[str] = None,
    thumb_url: Optional[str] = None,
) -> UploadedSound:
    doc = {
        "name": name,
        "name_lower": name.lower(),
        "source": "user_upload",
        "uploader_id": uploader_id,
        "cached_voice_file_id": cached_voice_file_id,
        "duration": duration,
        "size_bytes": size_bytes,
        "mime_type": mime_type,
        "original_name": original_name,
        "created_at": datetime.utcnow(),
        "share_count": 0,
        "thumb_url": thumb_url or DEFAULT_THUMB_URL,
        "is_adult": False,
        "category": category,
        "premium": False,
        "featured_until": None,
    }
    await sounds_collection.update_one(
        {"name_lower": name.lower()},
        {"$set": doc},
        upsert=True,
    )
    return UploadedSound(**_filter_dataclass_fields(doc, UploadedSound))

# ============================================================
# SEARCH (with category filter)
# ============================================================
async def search_uploaded(query: str, limit: int = 15) -> List[UploadedSound]:
    q = clean_spaces(query).lower()
    filter_dict = {}
    # FIXED: Category search case‑insensitive exact match
    if q.startswith("category:"):
        cat = q[9:].strip()
        if cat:
            filter_dict["category"] = {"$regex": f"^{re.escape(cat)}$", "$options": "i"}
        q = ""
    if not q:
        pipeline = [{"$match": filter_dict}, {"$sample": {"size": limit}}]
        docs = await sounds_collection.aggregate(pipeline).to_list(length=limit)
        return [UploadedSound(**_filter_dataclass_fields(doc, UploadedSound)) for doc in docs]

    docs = await sounds_collection.find(
        {**filter_dict, "name_lower": {"$regex": re.escape(q)}},
        limit=limit * 5,
    ).to_list(length=limit * 5)

    scored = []
    for row in docs:
        name = str(row.get("name", ""))
        lowered = name.lower()
        score = 0
        if lowered == q:
            score = 100
        elif lowered.startswith(q):
            score = 70
        elif q in lowered:
            score = 50
        if score:
            scored.append((score, row))

    scored.sort(key=lambda x: (-x[0], x[1].get("name", "").lower()))
    return [UploadedSound(**_filter_dataclass_fields(item, UploadedSound)) for _, item in scored[:limit]]

# ============================================================
# MYINSTANTS (unchanged)
# ============================================================
MYINSTANTS_BASE = "https://www.myinstants.com"
MYINSTANTS_SEARCH = "https://www.myinstants.com/en/search/?name={query}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def _title_from_anchor(a) -> str:
    text = clean_spaces(a.get_text(" ", strip=True))
    if text:
        return text
    for attr in ("title", "aria-label", "data-name", "data-title"):
        val = clean_spaces(a.get(attr) or "")
        if val:
            return val
    href = (a.get("href") or "").strip()
    slug = href.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    slug = unquote(slug).replace("-", " ").replace("_", " ")
    return clean_spaces(slug) or "Unknown sound"

async def parse_myinstants_detail(session: aiohttp.ClientSession, page_url: str, fallback_title: str) -> Optional[ExternalSound]:
    try:
        async with session.get(page_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status >= 400:
                return None
            html = await resp.text()
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")
    audio_url = None
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if "/media/sounds/" in href and ".mp3" in href.lower():
            audio_url = urljoin(MYINSTANTS_BASE, href)
            break
    if not audio_url:
        for a in soup.find_all("a", href=True):
            label = clean_spaces(a.get_text(" ", strip=True)).lower()
            href = a.get("href", "").strip()
            if "download mp3" in label and "/media/sounds/" in href and ".mp3" in href.lower():
                audio_url = urljoin(MYINSTANTS_BASE, href)
                break
    if not audio_url:
        m = re.search(r'(?:https?://[^"\']+)?(/media/sounds/[^"\']+\.mp3(?:\?[^"\']*)?)', html, re.I)
        if m:
            audio_url = urljoin(MYINSTANTS_BASE, m.group(1))
    if not audio_url:
        return None

    title = clean_spaces(fallback_title)
    h1 = soup.find("h1")
    if h1:
        h1_text = clean_spaces(h1.get_text(" ", strip=True))
        if h1_text:
            title = h1_text
    return ExternalSound(name=title, page_url=page_url, audio_url=audio_url)

async def search_myinstants(query: str, limit: int = 12) -> List[ExternalSound]:
    if not MYINSTANTS_ENABLED or not query.strip():
        return []
    search_url = MYINSTANTS_SEARCH.format(query=quote_plus(query.strip()))
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        try:
            async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status >= 400:
                    return []
                html = await resp.text()
        except Exception:
            return []

        soup = BeautifulSoup(html, "html.parser")
        seen_urls = set()
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if "/instant/" not in href:
                continue
            page_url = urljoin(MYINSTANTS_BASE, href)
            if page_url in seen_urls:
                continue
            title = _title_from_anchor(a)
            if not title:
                continue
            seen_urls.add(page_url)
            candidates.append((page_url, title))
            if len(candidates) >= max(limit * 2, 20):
                break

        if not candidates:
            return []

        sem = asyncio.Semaphore(4)
        async def worker(page_url: str, title: str) -> Optional[ExternalSound]:
            async with sem:
                return await parse_myinstants_detail(session, page_url, title)

        tasks = [worker(url, title) for url, title in candidates]
        resolved = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    used_urls = set()
    for item in resolved:
        if isinstance(item, Exception) or not item:
            continue
        if item.audio_url in used_urls:
            continue
        used_urls.add(item.audio_url)
        results.append(item)
        if len(results) >= limit:
            break
    return results

# ============================================================
# COMMANDS
# ============================================================
@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user = message.from_user
    await log_event(
        f"🟢 <b>/start</b>\n"
        f"👤 {user.full_name} (<code>{user.id}</code>)\n"
        f"💬 Chat: <code>{message.chat.id}</code>"
    )
    await update_user(user.id, {"last_seen": datetime.utcnow()})

    kb_rows = []
    kb_rows.append([
        InlineKeyboardButton(text="👤 PROFILE", callback_data="profile"),
        InlineKeyboardButton(text="📢 SUPPORT", url=SUPPORT_CHANNEL_URL)
    ])
    kb_rows.append([
        InlineKeyboardButton(text="💎 SUBSCRIPTION", callback_data="subscribe"),
        InlineKeyboardButton(text="🏆 TOP 3", callback_data="top")
    ])
    third_row = [InlineKeyboardButton(text="📂 CATEGORIES", callback_data="categories")]
    if user.id in ADMIN_IDS:
        third_row.append(InlineKeyboardButton(text="🛠️ ADMIN", callback_data="admin_panel"))
    kb_rows.append(third_row)
    kb_rows.append([InlineKeyboardButton(text="🔍 SEARCH INLINE", switch_inline_query_current_chat="")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if WELCOME_MEDIA_URL:
        await message.answer_photo(
            photo=WELCOME_MEDIA_URL,
            caption="🎵 <b>Sound Inline Bot</b>\n\nUse /upload to add sounds.\nInline: @tssoundbot",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    else:
        await message.answer(
            "🎵 <b>Sound Inline Bot</b>\n\nUse /upload to add sounds.\nInline: @tssoundbot",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    total_uploads = user.get("total_uploads", 0)
    warnings = user.get("warnings", 0)
    banned = user.get("banned", False)
    sub_exp = user.get("subscription_expiry")
    sub_text = "Not subscribed"
    if sub_exp and sub_exp > datetime.utcnow():
        sub_text = f"Active until {sub_exp.strftime('%Y-%m-%d %H:%M UTC')}"
    badges = " ".join(user.get("badges", [])) or "No badges"
    text = (
        f"👤 <b>Profile</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📤 Total uploads: {total_uploads}\n"
        f"⚠️ Warnings: {warnings}/5\n"
        f"🚫 Banned: {banned}\n"
        f"💎 Subscription: {sub_text}\n"
        f"🏅 Badges: {badges}"
    )
    # FIXED: If message has no caption (text message), use edit_text
    if callback.message.caption:
        await callback.message.edit_caption(caption=text, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "subscribe")
async def subscribe_callback(callback: CallbackQuery):
    text = "💎 <b>Subscription</b>\n\n1 week unlimited uploads – ₹20\n\nUPI: yourupi@okhdfcbank\nAfter payment, contact admin."
    if callback.message.caption:
        await callback.message.edit_caption(caption=text, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "top")
async def top_callback(callback: CallbackQuery):
    top_sounds = await sounds_collection.find().sort("share_count", -1).limit(3).to_list(3)
    text = "🏆 <b>Top 3 Sounds</b>\n"
    if top_sounds:
        for i, s in enumerate(top_sounds, 1):
            text += f"{i}. {s['name']} – {s['share_count']} shares\n"
    else:
        text += "No sounds yet.\n"
    if callback.message.caption:
        await callback.message.edit_caption(caption=text, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "categories")
async def categories_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    cats = await get_user_categories(user_id)
    if not cats:
        text = "You have no categories. Upload a sound to create one."
    else:
        text = "📂 <b>Your Categories:</b>\n" + "\n".join(f"• {c}" for c in cats)
    if callback.message.caption:
        await callback.message.edit_caption(caption=text, parse_mode=ParseMode.HTML)
    else:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Unauthorized", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Sounds List", callback_data="admin_sounds")],
        [InlineKeyboardButton(text="👥 Users List", callback_data="admin_users")],
        [InlineKeyboardButton(text="⭐ Set Sound of Day", switch_inline_query_current_chat="feature:")],
    ])
    if callback.message.caption:
        await callback.message.edit_caption(caption="🛠️ Admin Panel", reply_markup=kb)
    else:
        await callback.message.edit_text("🛠️ Admin Panel", reply_markup=kb)
    await callback.answer()

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    total_uploads = user.get("total_uploads", 0)
    warnings = user.get("warnings", 0)
    banned = user.get("banned", False)
    sub_exp = user.get("subscription_expiry")
    sub_text = "Not subscribed"
    if sub_exp and sub_exp > datetime.utcnow():
        sub_text = f"Active until {sub_exp.strftime('%Y-%m-%d %H:%M UTC')}"
    badges = " ".join(user.get("badges", [])) or "No badges"
    text = (
        f"👤 <b>Profile</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📤 Total uploads: {total_uploads}\n"
        f"⚠️ Warnings: {warnings}/5\n"
        f"🚫 Banned: {banned}\n"
        f"💎 Subscription: {sub_text}\n"
        f"🏅 Badges: {badges}"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("top"))
async def cmd_top(message: Message):
    top_sounds = await sounds_collection.find().sort("share_count", -1).limit(3).to_list(3)
    text = "🏆 <b>Top 3 Sounds</b>\n"
    if top_sounds:
        for i, s in enumerate(top_sounds, 1):
            text += f"{i}. {s['name']} – {s['share_count']} shares\n"
    else:
        text += "No sounds yet.\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    await message.answer(
        "💎 <b>Subscription</b>\n\n"
        "1 week unlimited uploads – ₹20\n\n"
        "UPI: yourupi@okhdfcbank\nAfter payment, contact admin.",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("addsub"))
async def cmd_addsub(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    args = command.args.split() if command.args else []
    if len(args) < 1:
        await message.reply("Usage: /addsub user_id [days=7]")
        return
    try:
        target_id = int(args[0])
        days = int(args[1]) if len(args) > 1 else 7
    except ValueError:
        await message.reply("Invalid user_id or days.")
        return
    expiry = datetime.utcnow() + timedelta(days=days)
    await update_user(target_id, {"subscription_expiry": expiry})
    await add_badge(target_id, "💎 Premium Subscriber")
    await message.reply(f"✅ Subscription added for {target_id} until {expiry.strftime('%Y-%m-%d')}")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Cancelled.")

@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.reply(f"Chat ID: <code>{message.chat.id}</code>", parse_mode=ParseMode.HTML)

@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("/upload only in private chat.")
        return
    user_id = message.from_user.id
    user = await get_user(user_id)
    if user.get("banned", False):
        await message.reply("You are banned.")
        return
    await state.set_state(UploadStates.waiting_media)
    await message.answer(
        f"Send me an audio file (voice, audio, document).\n"
        f"Max {MAX_UPLOAD_SIZE_MB}MB, {MAX_DURATION_SECONDS}s.\n"
        "I'll ask for a name then category."
    )

@router.message(UploadStates.waiting_media)
async def upload_receive_media(message: Message, state: FSMContext):
    media = extract_media(message)
    if not media:
        await message.reply("Please send a valid audio file.")
        return

    user_id = message.from_user.id
    is_admin = user_id in ADMIN_IDS
    can_up, reason = await can_upload(user_id, is_admin)
    if not can_up:
        await state.clear()
        await message.reply(f"❌ {reason}")
        return

    if media.file_size and media.file_size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        await state.clear()
        await message.reply(f"❌ File exceeds {MAX_UPLOAD_SIZE_MB}MB.")
        return
    if media.duration and media.duration > MAX_DURATION_SECONDS:
        await state.clear()
        await message.reply(f"❌ Duration exceeds {MAX_DURATION_SECONDS}s.")
        return

    if not is_admin and await is_adult_content(b"", media.file_name or ""):
        warns = await add_warning(user_id, "Adult content detected")
        await state.clear()
        await message.reply(f"🔞 Adult content not allowed. Warning {warns}/5.")
        return

    await state.update_data(
        pending_kind=media.kind,
        pending_file_id=media.file_id,
        pending_duration=media.duration,
        pending_size=media.file_size,
        pending_mime=media.mime_type,
        pending_filename=media.file_name,
    )
    await state.set_state(UploadStates.waiting_name)
    await message.answer("Now send a name for this sound (max 80 chars).")

@router.message(UploadStates.waiting_name)
async def upload_receive_name(message: Message, state: FSMContext):
    name = clean_spaces(message.text or "")
    if not name:
        await message.reply("Please send a text name.")
        return
    if len(name) > 80:
        await state.clear()
        await message.reply("❌ Name too long (max 80).")
        return

    await state.update_data(pending_name=name)
    user_id = message.from_user.id
    cats = await get_user_categories(user_id)

    if cats:
        builder = InlineKeyboardMarkup(inline_keyboard=[])
        for cat in cats:
            builder.inline_keyboard.append([InlineKeyboardButton(text=cat, callback_data=f"cat_existing_{cat}")])
        builder.inline_keyboard.append([InlineKeyboardButton(text="➕ Create New Category", callback_data="cat_new")])
        await state.set_state(UploadStates.waiting_category)
        await message.answer("Choose a category for this sound:", reply_markup=builder)
    else:
        await state.set_state(UploadStates.waiting_category)
        await message.answer("Send a category name (e.g., Meme, Gaming, Funny):")

@router.callback_query(F.data.startswith("cat_existing_"))
async def category_chosen_existing(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("_", 2)[2]
    await state.update_data(pending_category=category)
    await finalize_upload(callback.message, state, callback.from_user.id)
    await callback.answer()

@router.callback_query(F.data == "cat_new")
async def category_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UploadStates.waiting_category)
    await callback.message.edit_text("Send a new category name:")
    await callback.answer()

@router.message(UploadStates.waiting_category)
async def upload_receive_category(message: Message, state: FSMContext):
    category = clean_spaces(message.text or "")
    if not category:
        await message.reply("Please send a category name.")
        return
    if len(category) > 30:
        await message.reply("Category name too long (max 30 chars).")
        return

    user_id = message.from_user.id
    success = await create_category(user_id, category)
    if not success:
        await message.reply("❌ You can only create 1 category. Subscribe to create more.")
        return

    await state.update_data(pending_category=category)
    await finalize_upload(message, state, user_id)

async def finalize_upload(message: Message, state: FSMContext, user_id: int):
    data = await state.get_data()
    kind = data.get("pending_kind")
    file_id = data.get("pending_file_id")
    duration = data.get("pending_duration")
    size_bytes = data.get("pending_size")
    mime_type = data.get("pending_mime")
    original_filename = data.get("pending_filename")
    name = data.get("pending_name")
    category = data.get("pending_category")

    is_admin = user_id in ADMIN_IDS
    msg = await message.answer("Uploading...")
    try:
        media = MediaPayload(
            kind=kind,
            file_id=file_id,
            file_size=size_bytes,
            duration=duration,
            mime_type=mime_type,
            file_name=original_filename,
        )

        cached_id = await mirror_to_storage_voice(media, name)
        await save_uploaded_sound(
            name=name,
            uploader_id=user_id,
            cached_voice_file_id=cached_id,
            duration=duration,
            size_bytes=size_bytes,
            mime_type=mime_type,
            original_name=original_filename,
            category=category,
        )
        await record_upload(user_id, is_admin=is_admin)
        await state.clear()
        await msg.edit_text(
            f"✅ Uploaded: <b>{name}</b>\nCategory: {category}\nSize: {format_size_mb(size_bytes)}",
            parse_mode=ParseMode.HTML,
            reply_markup=upload_keyboard(),
        )
        await log_event(f"📤 User {user_id} uploaded sound: {name} (Category: {category})")
    except Exception as e:
        logger.exception("Upload failed")
        await state.clear()
        await msg.edit_text(f"❌ Upload failed: {e}")

# ============================================================
# ADMIN: SOUNDS & USERS & FEATURED
# ============================================================
@router.callback_query(F.data == "admin_sounds")
async def admin_sounds_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Unauthorized", show_alert=True)
        return
    await show_admin_page(callback.message.chat.id, "sounds", 0, edit_msg_id=callback.message.message_id)

@router.callback_query(F.data == "admin_users")
async def admin_users_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Unauthorized", show_alert=True)
        return
    users = await users_collection.find().to_list(length=None)
    lines = [f"👥 <b>Total Users: {len(users)}</b>\n"]
    for u in users[:20]:
        sub = "✅" if u.get("subscription_expiry") and u["subscription_expiry"] > datetime.utcnow() else "❌"
        lines.append(f"<code>{u['user_id']}</code> - uploads: {u.get('total_uploads',0)} {sub}")
    text = "\n".join(lines)
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML)

async def show_admin_page(chat_id: int, coll: str, page: int, edit_msg_id: int = None):
    per_page = 5
    collection = sounds_collection
    total = await collection.count_documents({})
    total_pages = (total + per_page - 1) // per_page
    if total_pages == 0:
        text = "No items found."
        if edit_msg_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=edit_msg_id)
        else:
            await bot.send_message(chat_id, text)
        return

    cursor = collection.find().sort("created_at", -1).skip(page * per_page).limit(per_page)
    items = await cursor.to_list(length=per_page)
    lines = [f"📋 <b>Sounds (Page {page+1}/{total_pages})</b>\n"]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. <b>{item['name']}</b> – {item.get('share_count',0)} shares (Cat: {item.get('category','None')})")
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔊 Listen", callback_data=f"admin_view_sounds_{items[0]['_id']}")],
        [InlineKeyboardButton(text="🗑 Delete", callback_data=f"admin_delete_sounds_{items[0]['_id']}_{page}")],
        *admin_pagination_keyboard("sounds", page, total_pages).inline_keyboard
    ])
    if edit_msg_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=edit_msg_id, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)

def admin_pagination_keyboard(coll: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"admin_page_{coll}_{page-1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton(text="➡️ Next", callback_data=f"admin_page_{coll}_{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

@router.callback_query(F.data.startswith("admin_page_"))
async def admin_page_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    parts = callback.data.split("_")
    coll = parts[2]
    page = int(parts[3])
    await show_admin_page(callback.message.chat.id, coll, page, edit_msg_id=callback.message.message_id)
    await callback.answer()

@router.callback_query(F.data.startswith("admin_view_"))
async def admin_view_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    parts = callback.data.split("_")
    coll = parts[2]
    item_id = parts[3]
    collection = sounds_collection
    item = await collection.find_one({"_id": item_id})
    if not item:
        await callback.answer("Item not found")
        return
    await bot.send_voice(callback.message.chat.id, item["cached_voice_file_id"], caption=item["name"])
    await callback.answer()

@router.callback_query(F.data.startswith("admin_delete_"))
async def admin_delete_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    parts = callback.data.split("_")
    coll = parts[2]
    item_id = parts[3]
    page = int(parts[4])
    collection = sounds_collection
    result = await collection.delete_one({"_id": item_id})
    if result.deleted_count:
        await callback.answer("Deleted", show_alert=True)
        await show_admin_page(callback.message.chat.id, coll, page, edit_msg_id=callback.message.message_id)
    else:
        await callback.answer("Delete failed", show_alert=True)

# ============================================================
# FEATURED SOUND OF THE DAY
# ============================================================
@router.message(Command("setfeatured"))
async def cmd_setfeatured(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    name = command.args
    if not name:
        await message.reply("Usage: /setfeatured <sound name>")
        return
    await sounds_collection.update_many({"featured_until": {"$ne": None}}, {"$set": {"featured_until": None}})
    result = await sounds_collection.update_one(
        {"name": name},
        {"$set": {"featured_until": datetime.utcnow() + timedelta(days=1)}}
    )
    if result.modified_count:
        await message.reply(f"✅ '{name}' is now Sound of the Day for 24h.")
    else:
        await message.reply("Sound not found.")

@router.message(Command("senddaily"))
async def cmd_senddaily(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    featured = await sounds_collection.find_one({"featured_until": {"$gt": datetime.utcnow()}})
    if not featured:
        await message.reply("No featured sound set.")
        return
    users = await users_collection.find({"subscription_expiry": {"$gt": datetime.utcnow()}}).to_list(length=None)
    count = 0
    for u in users:
        try:
            await bot.send_voice(u["user_id"], featured["cached_voice_file_id"],
                                 caption=f"🌟 Sound of the Day: {featured['name']}")
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.reply(f"Sent to {count} subscribed users.")

# ============================================================
# BROADCAST SEGMENTATION
# ============================================================
@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    args = command.args.split(maxsplit=1) if command.args else []
    if len(args) < 2:
        await message.reply("Usage: /broadcast <premium|active|all> <message>")
        return
    target, text = args[0], args[1]
    filter_dict = {}
    if target == "premium":
        filter_dict = {"subscription_expiry": {"$gt": datetime.utcnow()}}
    elif target == "active":
        week_ago = datetime.utcnow() - timedelta(days=7)
        filter_dict = {"last_seen": {"$gte": week_ago}}
    elif target == "all":
        filter_dict = {}
    else:
        await message.reply("Invalid target. Use premium, active, or all.")
        return

    users = await users_collection.find(filter_dict).to_list(length=None)
    success = 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], text)
            success += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.reply(f"Broadcast sent to {success}/{len(users)} users.")

# ============================================================
# INLINE MODE
# ============================================================
@router.inline_query()
async def inline_handler(inline_query: InlineQuery):
    query = clean_spaces(inline_query.query)
    results = []

    sound_results = await search_uploaded(query, limit=INLINE_RANDOM_LIMIT if not query else 15)
    for item in sound_results:
        if item.cached_voice_file_id:
            results.append(
                InlineQueryResultCachedVoice(
                    id=f"upload:{item.name}:{uuid.uuid4().hex[:8]}",
                    voice_file_id=item.cached_voice_file_id,
                    title=item.name,
                    caption=item.name,  # FIXED: sirf naam dikhao
                )
            )

    remaining = max(0, 40 - len(results))
    if remaining and query and MYINSTANTS_ENABLED and not query.startswith("category:"):
        try:
            ext_results = await search_myinstants(query, limit=min(remaining, MAX_MYINSTANTS_RESULTS))
            for item in ext_results:
                results.append(
                    InlineQueryResultAudio(
                        id=f"mi:{item.name}:{uuid.uuid4().hex[:8]}",
                        audio_url=item.audio_url,
                        title=f"🔊 {item.name}",
                        caption=item.name,
                        performer="Myinstants",
                    )
                )
        except Exception:
            logger.exception("Myinstants search failed")

    if not results:
        results = [
            InlineQueryResultArticle(
                id=f"empty:{uuid.uuid4().hex}",
                title="No results",
                description="Try another keyword or upload your own.",
                input_message_content=InputTextMessageContent(
                    message_text="No sounds found. Use /upload in PM."
                ),
            )
        ]

    await inline_query.answer(results, cache_time=INLINE_CACHE_SECONDS, is_personal=True)

@router.chosen_inline_result()
async def chosen_result_handler(chosen: ChosenInlineResult):
    rid = chosen.result_id
    parts = rid.split(":")
    if len(parts) >= 2 and parts[0] == "upload":
        name = parts[1]
        result = await sounds_collection.update_one(
            {"name": name},
            {"$inc": {"share_count": 1}}
        )
        if result.modified_count:
            doc = await sounds_collection.find_one({"name": name})
            if doc and doc["share_count"] >= 100:
                await add_badge(doc["uploader_id"], "🎉 100 Shares Milestone")

# ============================================================
# STATS & UPDATE
# ============================================================
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    sound_count = await sounds_collection.count_documents({})
    user_count = await users_collection.count_documents({})
    await message.reply(f"📊 Stats:\nUsers: {user_count}\nSounds: {sound_count}")

@router.message(Command("update"))
async def cmd_update(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    msg = await message.answer("⏳ Fetching updates...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode() + stderr.decode()
        if "Already up to date" in output:
            await msg.edit_text("✅ Already up-to-date.")
        else:
            await msg.edit_text(f"✅ Updated:\n<pre>{output[:300]}</pre>", parse_mode=ParseMode.HTML)
            await asyncio.sleep(1)
            await msg.edit_text("🔄 Restarting...")
            os.kill(os.getpid(), 15)
    except Exception as e:
        await msg.edit_text(f"❌ Update failed: {e}")

# ============================================================
# RUNNER
# ============================================================
async def main():
    await ensure_indexes()
    me = await bot.get_me()
    logger.info("Bot started as @%s", me.username)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
