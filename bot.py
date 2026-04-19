import asyncio
import io
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin

import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Audio,
    BufferedInputFile,
    ChosenInlineResult,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
    InputTextMessageContent,
    Message,
    Video,
    Voice,
)
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

# ============================================================
# CONFIG (Replace with your own values)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8697143769:AAHdC1mq-EP4lcPmoF4mMeEBykepTokObRE").strip()
LOGGER_GROUP_ID = int(os.getenv("LOGGER_GROUP_ID", "-1003711505151"))
STORAGE_CHAT_ID = int(os.getenv("STORAGE_CHAT_ID", "-1003897917299"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "7549407961").split(",") if x.strip().isdigit()}

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://SANKIXD:SANKIXD@cluster0.dgogcjs.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "tsoundbot").strip()

SUPPORT_CHANNEL_URL = os.getenv("SUPPORT_CHANNEL_URL", "https://t.me/TEAMSANKI").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip().lstrip("@")
WELCOME_IMAGE_URL = os.getenv("WELCOME_IMAGE_URL", "https://graph.org/file/533cd5ce5414981c731d5-3831c6c74a2525572c.jpg").strip()
DEFAULT_THUMB_URL = os.getenv("DEFAULT_THUMB_URL", "https://graph.org/file/533cd5ce5414981c731d5-3831c6c74a2525572c.jpg").strip()

MAX_UPLOAD_SIZE_MB = float(os.getenv("MAX_UPLOAD_SIZE_MB", "10"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "60"))
VIDEO_MAX_SIZE_MB = float(os.getenv("VIDEO_MAX_SIZE_MB", "20"))
VIDEO_MAX_DURATION_SEC = int(os.getenv("VIDEO_MAX_DURATION_SEC", "10"))
INLINE_CACHE_SECONDS = int(os.getenv("INLINE_CACHE_SECONDS", "20"))
MYINSTANTS_ENABLED = os.getenv("MYINSTANTS_ENABLED", "true").lower() == "true"
MAX_MYINSTANTS_RESULTS = int(os.getenv("MAX_MYINSTANTS_RESULTS", "12"))

FREE_DAILY_LIMIT = 4
SUBSCRIPTION_COOLDOWN_SEC = 10

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
videos_collection = db["videos"]

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

@dataclass
class UploadedVideo:
    name: str
    uploader_id: int
    cached_video_file_id: str
    duration: Optional[int] = None
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    original_name: Optional[str] = None
    created_at: Optional[datetime] = None
    share_count: int = 0
    thumb_url: Optional[str] = None
    is_adult: bool = False

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

# ============================================================
# HELPER FUNCTIONS
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

def is_video_document(doc: Document) -> bool:
    mime = (doc.mime_type or "").lower()
    filename = (doc.file_name or "").lower()
    video_exts = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")
    return mime.startswith("video/") or filename.endswith(video_exts)

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
    if msg.video:
        v: Video = msg.video
        return MediaPayload(
            kind="video",
            file_id=v.file_id,
            file_size=v.file_size,
            duration=v.duration,
            mime_type=v.mime_type,
            file_name=v.file_name,
        )
    if msg.document:
        doc = msg.document
        if is_audio_document(doc):
            return MediaPayload(
                kind="document",
                file_id=doc.file_id,
                file_size=doc.file_size,
                duration=None,
                mime_type=doc.mime_type,
                file_name=doc.file_name,
            )
        if is_video_document(doc):
            return MediaPayload(
                kind="video_document",
                file_id=doc.file_id,
                file_size=doc.file_size,
                duration=None,
                mime_type=doc.mime_type,
                file_name=doc.file_name,
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
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not installed") from exc

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

async def mirror_to_storage_video(media: MediaPayload, title: str) -> str:
    title = clean_spaces(title)[:120]
    if media.kind == "video":
        sent = await bot.send_video(
            chat_id=STORAGE_CHAT_ID,
            video=media.file_id,
            caption=title,
            disable_notification=True,
        )
    else:
        sent = await bot.send_video(
            chat_id=STORAGE_CHAT_ID,
            video=media.file_id,
            caption=title,
            disable_notification=True,
        )
    if not sent.video:
        raise RuntimeError("Storage chat did not return video")
    return sent.video.file_id

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
    await videos_collection.create_index([("name_lower", 1)])
    await videos_collection.create_index([("created_at", -1)])
    await videos_collection.create_index([("uploader_id", 1)])
    await videos_collection.create_index([("share_count", -1)])
    await users_collection.create_index([("user_id", 1)], unique=True)

# ============================================================
# USER MANAGEMENT
# ============================================================

async def get_user(user_id: int) -> dict:
    doc = await users_collection.find_one({"user_id": user_id})
    if not doc:
        doc = {
            "user_id": user_id,
            "total_uploads": 0,
            "total_video_uploads": 0,
            "warnings": 0,
            "banned": False,
            "subscription_expiry": None,
            "last_upload_time": None,
            "daily_uploads": {},
        }
        await users_collection.insert_one(doc)
    return doc

async def update_user(user_id: int, updates: dict) -> None:
    await users_collection.update_one({"user_id": user_id}, {"$set": updates})

async def can_upload(user_id: int) -> tuple[bool, str]:
    user = await get_user(user_id)
    if user.get("banned", False):
        return False, "You are banned from uploading."
    now = datetime.utcnow()
    sub_exp = user.get("subscription_expiry")
    if sub_exp and sub_exp > now:
        last_up = user.get("last_upload_time")
        if last_up and (now - last_up).total_seconds() < SUBSCRIPTION_COOLDOWN_SEC:
            return False, f"Cooldown: wait {SUBSCRIPTION_COOLDOWN_SEC - int((now - last_up).total_seconds())} seconds."
        return True, ""
    else:
        today = now.date().isoformat()
        daily = user.get("daily_uploads", {})
        count = daily.get(today, 0)
        if count >= FREE_DAILY_LIMIT:
            return False, f"Daily limit ({FREE_DAILY_LIMIT}) reached. Subscribe for unlimited."
        return True, ""

async def record_upload(user_id: int, is_video: bool = False) -> None:
    user = await get_user(user_id)
    now = datetime.utcnow()
    updates = {"last_upload_time": now}
    if is_video:
        updates["total_video_uploads"] = user.get("total_video_uploads", 0) + 1
    else:
        updates["total_uploads"] = user.get("total_uploads", 0) + 1

    sub_exp = user.get("subscription_expiry")
    if not sub_exp or sub_exp <= now:
        today = now.date().isoformat()
        daily = user.get("daily_uploads", {})
        daily[today] = daily.get(today, 0) + 1
        updates["daily_uploads"] = daily

    await update_user(user_id, updates)

async def add_warning(user_id: int, reason: str) -> int:
    user = await get_user(user_id)
    warnings = user.get("warnings", 0) + 1
    updates = {"warnings": warnings}
    if warnings >= 5:
        updates["banned"] = True
    await update_user(user_id, updates)
    await log_event(f"⚠️ User {user_id} warned ({warnings}/5): {reason}")
    return warnings

# ============================================================
# SAVE TO DB (Fixed field filtering)
# ============================================================

def _filter_dataclass_fields(data: dict, cls):
    """Keep only keys that are fields of the dataclass."""
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
    }
    await sounds_collection.update_one(
        {"name_lower": name.lower()},
        {"$set": doc},
        upsert=True,
    )
    return UploadedSound(**_filter_dataclass_fields(doc, UploadedSound))

async def save_uploaded_video(
    name: str,
    uploader_id: int,
    cached_video_file_id: str,
    duration: Optional[int],
    size_bytes: Optional[int],
    mime_type: Optional[str],
    original_name: Optional[str],
    thumb_url: Optional[str] = None,
) -> UploadedVideo:
    doc = {
        "name": name,
        "name_lower": name.lower(),
        "uploader_id": uploader_id,
        "cached_video_file_id": cached_video_file_id,
        "duration": duration,
        "size_bytes": size_bytes,
        "mime_type": mime_type,
        "original_name": original_name,
        "created_at": datetime.utcnow(),
        "share_count": 0,
        "thumb_url": thumb_url or DEFAULT_THUMB_URL,
        "is_adult": False,
    }
    await videos_collection.update_one(
        {"name_lower": name.lower()},
        {"$set": doc},
        upsert=True,
    )
    return UploadedVideo(**_filter_dataclass_fields(doc, UploadedVideo))

# ============================================================
# SEARCH
# ============================================================

async def search_uploaded(query: str, limit: int = 15) -> List[UploadedSound]:
    q = clean_spaces(query).lower()
    if q:
        docs = await sounds_collection.find(
            {"name_lower": {"$regex": re.escape(q)}},
            limit=limit * 5,
        ).to_list(length=limit * 5)
    else:
        docs = await sounds_collection.find({}).sort("created_at", -1).limit(limit).to_list(length=limit)

    scored = []
    for row in docs:
        name = str(row.get("name", ""))
        lowered = name.lower()
        score = 0
        if not q:
            score = 1
        elif lowered == q:
            score = 100
        elif lowered.startswith(q):
            score = 70
        elif q in lowered:
            score = 50
        if score:
            scored.append((score, row))

    scored.sort(key=lambda x: (-x[0], x[1].get("name", "").lower()))
    return [UploadedSound(**_filter_dataclass_fields(item, UploadedSound)) for _, item in scored[:limit]]

async def search_videos(query: str, limit: int = 10) -> List[UploadedVideo]:
    q = clean_spaces(query).lower()
    if q:
        docs = await videos_collection.find(
            {"name_lower": {"$regex": re.escape(q)}},
            limit=limit * 3,
        ).to_list(length=limit * 3)
    else:
        docs = await videos_collection.find({}).sort("created_at", -1).limit(limit).to_list(length=limit)

    scored = []
    for row in docs:
        name = str(row.get("name", ""))
        lowered = name.lower()
        score = 0
        if not q:
            score = 1
        elif lowered == q:
            score = 100
        elif lowered.startswith(q):
            score = 70
        elif q in lowered:
            score = 50
        if score:
            scored.append((score, row))

    scored.sort(key=lambda x: (-x[0], x[1].get("name", "").lower()))
    return [UploadedVideo(**_filter_dataclass_fields(item, UploadedVideo)) for _, item in scored[:limit]]

# ============================================================
# MYINSTANTS PARSING (FULLY IMPLEMENTED)
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

    # Find MP3 link
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
    if WELCOME_IMAGE_URL:
        caption = (
            "🎵 <b>Sound & Video Inline Bot</b>\n\n"
            "Use me inline: <code>@YourBotName query</code>\n\n"
            "📤 Upload sounds or short videos.\n"
            "🔞 Adult content blocked.\n"
            "📊 Check /profile and /top"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Support Channel", url=SUPPORT_CHANNEL_URL)],
            [InlineKeyboardButton(text="👤 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME}")],
            [InlineKeyboardButton(text="🔍 Search Inline", switch_inline_query_current_chat="")],
        ])
        await message.answer_photo(
            photo=WELCOME_IMAGE_URL,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    else:
        await message.answer(
            "🎵 <b>Sound & Video Inline Bot</b>\n\n"
            "Use /upload to add content.",
            parse_mode=ParseMode.HTML,
            reply_markup=upload_keyboard(),
        )

@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    user_id = message.from_user.id
    user = await get_user(user_id)
    total_uploads = user.get("total_uploads", 0) + user.get("total_video_uploads", 0)
    warnings = user.get("warnings", 0)
    banned = user.get("banned", False)
    sub_exp = user.get("subscription_expiry")
    sub_text = "Not subscribed"
    if sub_exp and sub_exp > datetime.utcnow():
        sub_text = f"Active until {sub_exp.strftime('%Y-%m-%d %H:%M UTC')}"
    text = (
        f"👤 <b>Profile</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📤 Total uploads: {total_uploads}\n"
        f"⚠️ Warnings: {warnings}/5\n"
        f"🚫 Banned: {banned}\n"
        f"💎 Subscription: {sub_text}\n"
    )

    top_sounds = await sounds_collection.find().sort("share_count", -1).limit(3).to_list(3)
    top_videos = await videos_collection.find().sort("share_count", -1).limit(3).to_list(3)
    user_top_sound = next((s for s in top_sounds if s.get("uploader_id") == user_id), None)
    user_top_video = next((v for v in top_videos if v.get("uploader_id") == user_id), None)

    if user_top_sound:
        text += f"\n🏆 Your top sound: <b>{user_top_sound['name']}</b> (shared {user_top_sound['share_count']} times)"
    if user_top_video:
        text += f"\n🎬 Your top video: <b>{user_top_video['name']}</b> (shared {user_top_video['share_count']} times)"

    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    top_sounds = await sounds_collection.find().sort("share_count", -1).limit(3).to_list(3)
    top_videos = await videos_collection.find().sort("share_count", -1).limit(3).to_list(3)
    text = "🏆 <b>Leaderboard</b>\n\n<b>🎵 Top Sounds</b>\n"
    if top_sounds:
        for i, s in enumerate(top_sounds, 1):
            text += f"{i}. {s['name']} – {s['share_count']} shares\n"
    else:
        text += "No sounds yet.\n"
    text += "\n<b>🎬 Top Videos</b>\n"
    if top_videos:
        for i, v in enumerate(top_videos, 1):
            text += f"{i}. {v['name']} – {v['share_count']} shares\n"
    else:
        text += "No videos yet.\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    await message.answer(
        "💎 <b>Subscription</b>\n\n"
        "1 week unlimited uploads (10s cooldown) – ₹20\n\n"
        "To subscribe, send ₹20 to UPI: <code>yourupi@okhdfcbank</code>\n"
        "After payment, send screenshot to admin @YourAdminUsername.\n"
        "Admin will activate your subscription.",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("addsub"))
async def cmd_addsub(message: Message, command: CommandObject) -> None:
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
    await message.reply(f"✅ Subscription added for {target_id} until {expiry.strftime('%Y-%m-%d')}")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.reply("Cancelled.")

@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.reply(f"Chat ID: <code>{message.chat.id}</code>", parse_mode=ParseMode.HTML)

@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext) -> None:
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
        f"Send me a voice note, audio file, or short video (max {VIDEO_MAX_DURATION_SEC}s, {VIDEO_MAX_SIZE_MB}MB).\n"
        f"Audio max {MAX_DURATION_SECONDS}s, {MAX_UPLOAD_SIZE_MB}MB.\n"
        "I'll ask for a name next."
    )

@router.message(UploadStates.waiting_media)
async def upload_receive_media(message: Message, state: FSMContext) -> None:
    media = extract_media(message)
    if not media:
        await message.reply("Please send a valid media file.")
        return

    user_id = message.from_user.id
    can_up, reason = await can_upload(user_id)
    if not can_up:
        await state.clear()
        await message.reply(f"❌ {reason}")
        return

    is_video = media.kind in ("video", "video_document")
    max_size = VIDEO_MAX_SIZE_MB if is_video else MAX_UPLOAD_SIZE_MB
    max_dur = VIDEO_MAX_DURATION_SEC if is_video else MAX_DURATION_SECONDS

    if media.file_size and media.file_size > max_size * 1024 * 1024:
        await state.clear()
        await message.reply(f"❌ File exceeds {max_size}MB.")
        return
    if media.duration and media.duration > max_dur:
        await state.clear()
        await message.reply(f"❌ Duration exceeds {max_dur} seconds.")
        return

    # Adult detection (simple filename check)
    if await is_adult_content(b"", media.file_name or ""):
        warns = await add_warning(user_id, f"Adult content detected in upload attempt")
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
        is_video=is_video,
    )
    await state.set_state(UploadStates.waiting_name)
    await message.answer("Now send a name for this upload (max 80 chars).")

@router.message(UploadStates.waiting_name)
async def upload_receive_name(message: Message, state: FSMContext) -> None:
    name = clean_spaces(message.text or "")
    if not name:
        await message.reply("Please send a text name.")
        return
    if len(name) > 80:
        await state.clear()
        await message.reply("❌ Name too long (max 80).")
        return

    data = await state.get_data()
    kind = data.get("pending_kind")
    file_id = data.get("pending_file_id")
    duration = data.get("pending_duration")
    size_bytes = data.get("pending_size")
    mime_type = data.get("pending_mime")
    original_filename = data.get("pending_filename")
    is_video = data.get("is_video", False)

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

        if is_video:
            cached_file_id = await mirror_to_storage_video(media, name)
            uploaded = await save_uploaded_video(
                name=name,
                uploader_id=message.from_user.id,
                cached_video_file_id=cached_file_id,
                duration=duration,
                size_bytes=size_bytes,
                mime_type=mime_type,
                original_name=original_filename,
            )
        else:
            cached_file_id = await mirror_to_storage_voice(media, name)
            uploaded = await save_uploaded_sound(
                name=name,
                uploader_id=message.from_user.id,
                cached_voice_file_id=cached_file_id,
                duration=duration,
                size_bytes=size_bytes,
                mime_type=mime_type,
                original_name=original_filename,
            )

        await record_upload(message.from_user.id, is_video=is_video)
        await state.clear()
        await msg.edit_text(
            f"✅ Uploaded: <b>{uploaded.name}</b>\n"
            f"Size: {format_size_mb(size_bytes)}\n"
            f"Now use inline mode to share.",
            parse_mode=ParseMode.HTML,
            reply_markup=upload_keyboard(),
        )
        await log_event(
            f"📤 User {message.from_user.id} uploaded {'video' if is_video else 'sound'}: {name}"
        )
    except Exception as e:
        logger.exception("Upload failed")
        await state.clear()
        await msg.edit_text(f"❌ Upload failed: {e}")

# ============================================================
# INLINE MODE
# ============================================================

@router.inline_query()
async def inline_handler(inline_query: InlineQuery) -> None:
    query = clean_spaces(inline_query.query)
    results = []

    # Sounds
    sound_results = await search_uploaded(query, limit=15)
    for item in sound_results:
        if item.cached_voice_file_id:
            results.append(
                InlineQueryResultCachedVoice(
                    id=f"upload:{slugify(item.name)}:{uuid.uuid4().hex[:8]}",
                    voice_file_id=item.cached_voice_file_id,
                    title=item.name,
                    caption=f"{item.name}\n🔊 Sound",
                )
            )

    # Videos
    video_results = await search_videos(query, limit=5)
    for item in video_results:
        if item.cached_video_file_id:
            results.append(
                InlineQueryResultCachedVideo(
                    id=f"video:{slugify(item.name)}:{uuid.uuid4().hex[:8]}",
                    video_file_id=item.cached_video_file_id,
                    title=item.name,
                    description=f"🎬 Video · {item.duration}s",
                    caption=f"{item.name}",
                )
            )

    # Myinstants
    remaining = max(0, 40 - len(results))
    if remaining and query and MYINSTANTS_ENABLED:
        try:
            ext_results = await search_myinstants(query, limit=min(remaining, MAX_MYINSTANTS_RESULTS))
            for item in ext_results:
                results.append(
                    InlineQueryResultAudio(
                        id=f"mi:{slugify(item.name)}:{uuid.uuid4().hex[:8]}",
                        audio_url=item.audio_url,
                        title=f"🔊 {item.name}",
                        caption=f"{item.name}\nSource: Myinstants",
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
async def chosen_result_handler(chosen: ChosenInlineResult) -> None:
    rid = chosen.result_id
    if rid.startswith("upload:"):
        parts = rid.split(":")
        slug = parts[1] if len(parts) > 1 else ""
        await sounds_collection.update_one(
            {"name_lower": slug.replace("-", " ").lower()},
            {"$inc": {"share_count": 1}}
        )
    elif rid.startswith("video:"):
        parts = rid.split(":")
        slug = parts[1] if len(parts) > 1 else ""
        await videos_collection.update_one(
            {"name_lower": slug.replace("-", " ").lower()},
            {"$inc": {"share_count": 1}}
        )
    logger.info(f"Chosen: user={chosen.from_user.id} result={rid}")

# ============================================================
# ADMIN STATS
# ============================================================

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    sound_count = await sounds_collection.count_documents({})
    video_count = await videos_collection.count_documents({})
    user_count = await users_collection.count_documents({})
    await message.reply(
        f"📊 Stats:\n"
        f"Users: {user_count}\n"
        f"Sounds: {sound_count}\n"
        f"Videos: {video_count}"
    )

# ============================================================
# RUNNER
# ============================================================

async def main() -> None:
    await ensure_indexes()
    me = await bot.get_me()
    logger.info("Bot started as @%s", me.username)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
