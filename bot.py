#!/usr/bin/env python3
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, unquote

import aiohttp
import redis.asyncio as redis
from aiogram import Bot, Dispatcher, F, Router
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
from rapidfuzz import fuzz
from telegraph import Telegraph

load_dotenv()

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8697143769:AAHdC1mq-EP4lcPmoF4mMeEBykepTokObRE").strip()
LOGGER_GROUP_ID = int(os.getenv("LOGGER_GROUP_ID", "-1003711505151"))
STORAGE_CHAT_ID = int(os.getenv("STORAGE_CHAT_ID", "-1003897917299"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "7549407961").split(",") if x.strip().isdigit()}

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://SANKIXD:SANKIXD@cluster0.dgogcjs.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "tssoundsbot").strip()

SUPPORT_CHANNEL_URL = os.getenv("SUPPORT_CHANNEL_URL", "https://t.me/TEAMSANKI").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ll_SANKI__II").strip().lstrip("@")
WELCOME_MEDIA_URL = os.getenv("WELCOME_MEDIA_URL", "https://graph.org/file/533cd5ce5414981c731d5-3831c6c74a2525572c.jpg").strip()
DEFAULT_THUMB_URL = os.getenv("DEFAULT_THUMB_URL", "https://graph.org/file/533cd5ce5414981c731d5-3831c6c74a2525572c.jpg").strip()

SIGHTENGINE_API_USER = os.getenv("SIGHTENGINE_API_USER", "").strip()
SIGHTENGINE_API_SECRET = os.getenv("SIGHTENGINE_API_SECRET", "").strip()
SIGHTENGINE_ENABLED = bool(SIGHTENGINE_API_USER and SIGHTENGINE_API_SECRET)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()
TELEGRAPH_ACCESS_TOKEN = os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip()

MAX_UPLOAD_SIZE_MB = float(os.getenv("MAX_UPLOAD_SIZE_MB", "10"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "60"))
VIDEO_MAX_SIZE_MB = float(os.getenv("VIDEO_MAX_SIZE_MB", "20"))
VIDEO_MAX_DURATION_SEC = int(os.getenv("VIDEO_MAX_DURATION_SEC", "10"))
INLINE_CACHE_SECONDS = int(os.getenv("INLINE_CACHE_SECONDS", "20"))
MYINSTANTS_ENABLED = os.getenv("MYINSTANTS_ENABLED", "true").lower() == "true"
MAX_MYINSTANTS_RESULTS = int(os.getenv("MAX_MYINSTANTS_RESULTS", "12"))

FREE_DAILY_LIMIT = 4
SUBSCRIPTION_COOLDOWN_SEC = 10
BATCH_AUDIO_LIMIT = 10
INLINE_RANDOM_LIMIT = 10

if not BOT_TOKEN or not MONGODB_URI:
    raise RuntimeError("BOT_TOKEN and MONGODB_URI required")

# ==================== LOGGING ====================
LOG_FILE = "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("sound-bot")

# ==================== REDIS & TELEGRAPH ====================
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
telegraph = None
if TELEGRAPH_ACCESS_TOKEN:
    telegraph = Telegraph(TELEGRAPH_ACCESS_TOKEN)
else:
    telegraph = Telegraph()
    telegraph.create_account(short_name="SoundBot")
    logger.info(f"Telegraph token: {telegraph.get_access_token()}")

# ==================== BOT & DB ====================
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client[MONGODB_DB_NAME]
sounds_collection = db["sounds"]
videos_collection = db["videos"]
users_collection = db["users"]
hashes_collection = db["file_hashes"]

# ==================== QUEUE ====================
upload_queue = asyncio.Queue()
worker_tasks = []

async def upload_worker():
    while True:
        task = await upload_queue.get()
        try:
            await task["func"](*task["args"], **task["kwargs"])
        except Exception as e:
            logger.exception(f"Worker error: {e}")
        finally:
            upload_queue.task_done()

# ==================== SHUTDOWN ====================
async def shutdown_handler(sig):
    logger.info(f"Shutdown signal {sig}")
    for task in worker_tasks:
        task.cancel()
    await upload_queue.join()
    await redis_client.close()
    await bot.session.close()
    sys.exit(0)

# Signal handlers will be attached inside main()

# ==================== MODELS ====================
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
    hash_md5: Optional[str] = None

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
    premium: bool = False
    hash_md5: Optional[str] = None

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
    file_bytes: Optional[bytes] = None

class UploadStates(StatesGroup):
    waiting_media_batch = State()
    waiting_video_name = State()
    waiting_category = State()
    waiting_premium = State()

# ==================== HELPERS ====================
def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\-_\s]+", "", text)
    text = re.sub(r"\s+", "-", text)
    text = text.strip("-_")
    return text or f"sound-{uuid.uuid4().hex[:8]}"

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
        v = msg.voice
        return MediaPayload("voice", v.file_id, v.file_size, v.duration, v.mime_type, None)
    if msg.audio:
        a = msg.audio
        return MediaPayload("audio", a.file_id, a.file_size, a.duration, a.mime_type, a.file_name)
    if msg.video:
        v = msg.video
        return MediaPayload("video", v.file_id, v.file_size, v.duration, v.mime_type, v.file_name)
    if msg.document:
        d = msg.document
        if is_audio_document(d):
            return MediaPayload("document", d.file_id, d.file_size, None, d.mime_type, d.file_name)
        if is_video_document(d):
            return MediaPayload("video_document", d.file_id, d.file_size, None, d.mime_type, d.file_name)
    return None

async def fetch_telegram_file_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    return buf.getvalue()

async def transcode_to_ogg_opus(input_bytes: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000",
        "-c:a", "libopus", "-b:a", "48k", "-vbr", "on", "-compression_level", "10",
        "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=input_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {stderr.decode()[:200]}")
    return stdout

async def mirror_to_storage_voice(media: MediaPayload, title: str) -> Tuple[str, str]:
    title = clean_spaces(title)[:120]
    if media.kind == "voice":
        sent = await bot.send_voice(STORAGE_CHAT_ID, media.file_id, caption=title, disable_notification=True)
    else:
        raw = media.file_bytes or await fetch_telegram_file_bytes(media.file_id)
        ogg = await transcode_to_ogg_opus(raw)
        sent = await bot.send_voice(STORAGE_CHAT_ID, BufferedInputFile(ogg, f"{slugify(title)}.ogg"),
                                    caption=title, disable_notification=True)
    if not sent.voice:
        raise RuntimeError("Storage voice failed")
    return sent.voice.file_id, DEFAULT_THUMB_URL

async def mirror_to_storage_video(media: MediaPayload, title: str) -> str:
    title = clean_spaces(title)[:120]
    sent = await bot.send_video(STORAGE_CHAT_ID, media.file_id, caption=title, disable_notification=True)
    if not sent.video:
        raise RuntimeError("Storage video failed")
    return sent.video.file_id

async def log_event(text: str):
    if LOGGER_GROUP_ID:
        try:
            await bot.send_message(LOGGER_GROUP_ID, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            logger.exception("Log failed")

async def ensure_indexes():
    await sounds_collection.create_index("name_lower")
    await sounds_collection.create_index("created_at")
    await videos_collection.create_index("name_lower")
    await videos_collection.create_index("created_at")
    await users_collection.create_index("user_id", unique=True)
    await hashes_collection.create_index("hash_md5", unique=True)

# ==================== USER DB ====================
async def get_user(user_id: int) -> dict:
    doc = await users_collection.find_one({"user_id": user_id})
    if not doc:
        doc = {
            "user_id": user_id, "total_uploads": 0, "total_video_uploads": 0,
            "warnings": 0, "banned": False, "subscription_expiry": None,
            "last_upload_time": None, "daily_uploads": {}, "last_daily_bonus": None, "streak": 0
        }
        await users_collection.insert_one(doc)
    return doc

async def update_user(user_id: int, updates: dict):
    await users_collection.update_one({"user_id": user_id}, {"$set": updates})

async def can_upload(user_id: int, is_admin: bool = False) -> Tuple[bool, str]:
    if is_admin:
        return True, ""
    user = await get_user(user_id)
    if user.get("banned"):
        return False, "Banned"
    now = datetime.utcnow()
    sub = user.get("subscription_expiry")
    if sub and sub > now:
        last = user.get("last_upload_time")
        if last and (now - last).total_seconds() < SUBSCRIPTION_COOLDOWN_SEC:
            return False, f"Cooldown {SUBSCRIPTION_COOLDOWN_SEC}s"
        return True, ""
    today = now.date().isoformat()
    cnt = user.get("daily_uploads", {}).get(today, 0)
    if cnt >= FREE_DAILY_LIMIT:
        return False, f"Daily limit {FREE_DAILY_LIMIT}"
    return True, ""

async def record_upload(user_id: int, is_video: bool = False, is_admin: bool = False):
    if is_admin:
        return
    user = await get_user(user_id)
    now = datetime.utcnow()
    upd = {"last_upload_time": now}
    if is_video:
        upd["total_video_uploads"] = user.get("total_video_uploads", 0) + 1
    else:
        upd["total_uploads"] = user.get("total_uploads", 0) + 1
    sub = user.get("subscription_expiry")
    if not sub or sub <= now:
        today = now.date().isoformat()
        daily = user.get("daily_uploads", {})
        daily[today] = daily.get(today, 0) + 1
        upd["daily_uploads"] = daily
    await update_user(user_id, upd)

# ==================== CATEGORY & PREMIUM KEYBOARDS ====================
def category_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😂 Meme", callback_data="cat_meme"),
         InlineKeyboardButton(text="🎮 Gaming", callback_data="cat_gaming")],
        [InlineKeyboardButton(text="🎵 Music", callback_data="cat_music"),
         InlineKeyboardButton(text="🎬 Anime", callback_data="cat_anime")],
        [InlineKeyboardButton(text="🔊 Other", callback_data="cat_other")],
    ])

def premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes (Premium)", callback_data="premium_yes"),
         InlineKeyboardButton(text="❌ No", callback_data="premium_no")]
    ])

# ==================== SEARCH ====================
def fuzzy_score(q: str, name: str) -> int:
    return fuzz.ratio(q.lower(), name.lower())

async def search_uploaded(query: str, limit: int = 15) -> List[UploadedSound]:
    q = clean_spaces(query).lower()
    if not q:
        docs = await sounds_collection.aggregate([{"$sample": {"size": limit}}]).to_list(limit)
    else:
        docs = await sounds_collection.find({"name_lower": {"$regex": re.escape(q)}}).limit(limit*5).to_list(limit*5)
        scored = [(fuzzy_score(q, d["name"]), d) for d in docs]
        scored.sort(key=lambda x: -x[0])
        docs = [item[1] for item in scored[:limit]]
    return [UploadedSound(**{k:v for k,v in d.items() if k in UploadedSound.__dataclass_fields__}) for d in docs]

# ==================== COMMANDS ====================
@router.message(Command("start"))
async def start_cmd(message: Message):
    user = message.from_user
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Profile", callback_data="profile")],
        [InlineKeyboardButton(text="📢 Support", url=SUPPORT_CHANNEL_URL)],
        [InlineKeyboardButton(text="👨‍💻 Admin", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton(text="🔍 Search Inline", switch_inline_query_current_chat="")],
    ])
    if WELCOME_MEDIA_URL:
        if WELCOME_MEDIA_URL.endswith(".gif"):
            await message.answer_animation(WELCOME_MEDIA_URL, caption="🎵 Sound Bot Ready!", reply_markup=kb)
        else:
            await message.answer_photo(WELCOME_MEDIA_URL, caption="🎵 Sound Bot Ready!", reply_markup=kb)
    else:
        await message.answer("🎵 Sound Bot Ready!", reply_markup=kb)
    await log_event(f"🟢 /start {user.id}")

@router.message(Command("upload"))
async def upload_cmd(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("Use in PM")
        return
    await state.set_state(UploadStates.waiting_media_batch)
    await message.answer("Send audio files (up to 10) or a single video.")

@router.message(UploadStates.waiting_media_batch)
async def media_received(message: Message, state: FSMContext):
    media = extract_media(message)
    if not media:
        await message.reply("Not supported")
        return
    if media.kind in ("video", "video_document"):
        # handle video separately (simplified)
        await message.reply("Video upload not implemented in this short version")
        await state.clear()
        return
    await state.update_data(pending_media=media)
    await state.set_state(UploadStates.waiting_category)
    await message.answer("Choose category:", reply_markup=category_keyboard())

@router.callback_query(F.data.startswith("cat_"))
async def cat_chosen(callback: CallbackQuery, state: FSMContext):
    cat = callback.data.split("_")[1]
    await state.update_data(category=cat)
    await state.set_state(UploadStates.waiting_premium)
    await callback.message.edit_text("Premium sound?", reply_markup=premium_keyboard())
    await callback.answer()

@router.callback_query(F.data.startswith("premium_"))
async def premium_chosen(callback: CallbackQuery, state: FSMContext):
    premium = callback.data == "premium_yes"
    data = await state.get_data()
    media: MediaPayload = data["pending_media"]
    cat = data["category"]
    name = media.file_name or f"Audio_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    name = clean_spaces(name)[:80]

    # Queue the upload
    await upload_queue.put({
        "func": process_upload_audio,
        "args": (media, callback.from_user.id, name, cat, premium)
    })
    await callback.message.edit_text("⏳ Upload queued.")
    await state.clear()
    await callback.answer()

async def process_upload_audio(media: MediaPayload, user_id: int, name: str, category: str, premium: bool):
    is_admin = user_id in ADMIN_IDS
    raw = media.file_bytes or await fetch_telegram_file_bytes(media.file_id)
    # adult / duplicate checks skipped for brevity
    cached_id, _ = await mirror_to_storage_voice(media, name)
    doc = {
        "name": name, "name_lower": name.lower(), "source": "user_upload",
        "uploader_id": user_id, "cached_voice_file_id": cached_id,
        "duration": media.duration, "size_bytes": len(raw), "mime_type": media.mime_type,
        "original_name": media.file_name, "created_at": datetime.utcnow(),
        "share_count": 0, "thumb_url": DEFAULT_THUMB_URL, "is_adult": False,
        "category": category, "premium": premium, "hash_md5": hashlib.md5(raw).hexdigest()
    }
    await sounds_collection.update_one({"name_lower": name.lower()}, {"$set": doc}, upsert=True)
    await record_upload(user_id, is_admin=is_admin)
    try:
        await bot.send_message(user_id, f"✅ Uploaded: {name}")
    except:
        pass

# ==================== INLINE ====================
@router.inline_query()
async def inline_query_handler(inline_query: InlineQuery):
    query = clean_spaces(inline_query.query)
    sounds = await search_uploaded(query, limit=INLINE_RANDOM_LIMIT if not query else 15)
    results = []
    for s in sounds:
        if s.cached_voice_file_id:
            results.append(InlineQueryResultCachedVoice(
                id=f"upload:{s.name}:{uuid.uuid4().hex[:8]}",
                voice_file_id=s.cached_voice_file_id,
                title=s.name,
            ))
    await inline_query.answer(results, cache_time=INLINE_CACHE_SECONDS, is_personal=True)

# ==================== MAIN ====================
async def main():
    # Start workers inside running loop
    for _ in range(2):
        worker_tasks.append(asyncio.create_task(upload_worker()))
    # Signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown_handler(s)))
    await ensure_indexes()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
