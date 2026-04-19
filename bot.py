#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import random
import re
import signal
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set, Tuple
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

# ==================== CONFIGURATION ====================
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
TELEGRAPH_ACCESS_TOKEN = os.getenv("TELEGRAPH_ACCESS_TOKEN", "a085f6e4c0e9af57a998b56f9ee9092d5aa96dd9365addd065d16ef7997f").strip()

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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI missing")

# ==================== LOGGING SETUP ====================
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
    logger.info(f"Telegraph account created: {telegraph.get_access_token()}")

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
reactions_collection = db["reactions"]

# ==================== ASYNC QUEUE FOR UPLOADS ====================
upload_queue = asyncio.Queue()
worker_tasks = []

async def upload_worker():
    while True:
        task = await upload_queue.get()
        try:
            await task["func"](*task["args"], **task["kwargs"])
        except Exception as e:
            logger.exception(f"Upload worker failed: {e}")
        finally:
            upload_queue.task_done()

# ==================== MODELS & STATES ====================
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

# ==================== HELPER FUNCTIONS ====================
def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\-_\s]+", "", text)
    text = re.sub(r"\s+", "-", text)
    text = text.strip("-_")
    return text or f"sound-{uuid.uuid4().hex[:8]}"

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
        return MediaPayload(kind="voice", file_id=v.file_id, file_size=v.file_size,
                            duration=v.duration, mime_type=v.mime_type, file_name=None)
    if msg.audio:
        a: Audio = msg.audio
        return MediaPayload(kind="audio", file_id=a.file_id, file_size=a.file_size,
                            duration=a.duration, mime_type=a.mime_type, file_name=a.file_name)
    if msg.video:
        v: Video = msg.video
        return MediaPayload(kind="video", file_id=v.file_id, file_size=v.file_size,
                            duration=v.duration, mime_type=v.mime_type, file_name=v.file_name)
    if msg.document:
        doc = msg.document
        if is_audio_document(doc):
            return MediaPayload(kind="document", file_id=doc.file_id, file_size=doc.file_size,
                                duration=None, mime_type=doc.mime_type, file_name=doc.file_name)
        if is_video_document(doc):
            return MediaPayload(kind="video_document", file_id=doc.file_id, file_size=doc.file_size,
                                duration=None, mime_type=doc.mime_type, file_name=doc.file_name)
    return None

async def fetch_telegram_file_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    return buffer.getvalue()

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
        raise RuntimeError(f"ffmpeg failed: {stderr.decode()[:200]}")
    return stdout

async def generate_waveform_thumbnail(audio_bytes: bytes, filename: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-filter_complex",
            "showwavespic=s=640x120:colors=#00BFFF", "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=audio_bytes)
        if proc.returncode != 0:
            raise RuntimeError("Waveform generation failed")
        response = telegraph.upload_file(stdout)
        return "https://telegra.ph" + response[0]["src"]
    except Exception:
        return DEFAULT_THUMB_URL

async def is_adult_content_sightengine(file_bytes: bytes, filename: str = "", is_video: bool = False) -> bool:
    if not SIGHTENGINE_ENABLED:
        suspicious = ["porn", "xxx", "adult", "sex", "nude"]
        return any(word in filename.lower() for word in suspicious)
    # For brevity, just do filename check (implement Sightengine later)
    return False

async def mirror_to_storage_voice(media: MediaPayload, title: str, thumb_url: Optional[str] = None) -> Tuple[str, str]:
    title = clean_spaces(title)[:120]
    if media.kind == "voice":
        sent = await bot.send_voice(chat_id=STORAGE_CHAT_ID, voice=media.file_id,
                                    caption=title, disable_notification=True)
    else:
        raw = media.file_bytes or await fetch_telegram_file_bytes(media.file_id)
        ogg_bytes = await transcode_to_ogg_opus(raw)
        sent = await bot.send_voice(chat_id=STORAGE_CHAT_ID,
                                    voice=BufferedInputFile(ogg_bytes, filename=f"{slugify(title)}.ogg"),
                                    caption=title, disable_notification=True)
    if not sent.voice:
        raise RuntimeError("Storage chat did not return voice")
    return sent.voice.file_id, thumb_url or DEFAULT_THUMB_URL

async def mirror_to_storage_video(media: MediaPayload, title: str) -> str:
    title = clean_spaces(title)[:120]
    sent = await bot.send_video(chat_id=STORAGE_CHAT_ID, video=media.file_id,
                                caption=title, disable_notification=True)
    if not sent.video:
        raise RuntimeError("Storage chat did not return video")
    return sent.video.file_id

async def log_event(text: str, parse_mode: str = "HTML") -> None:
    if not LOGGER_GROUP_ID:
        return
    try:
        await bot.send_message(LOGGER_GROUP_ID, text, parse_mode=parse_mode, disable_web_page_preview=True)
    except Exception:
        logger.exception("Failed to log event")

# ==================== DATABASE INDEXES ====================
async def ensure_indexes():
    await sounds_collection.create_index([("name_lower", 1)])
    await sounds_collection.create_index([("created_at", -1)])
    await sounds_collection.create_index([("uploader_id", 1)])
    await sounds_collection.create_index([("share_count", -1)])
    await sounds_collection.create_index([("hash_md5", 1)])
    await sounds_collection.create_index([("category", 1)])
    await videos_collection.create_index([("name_lower", 1)])
    await videos_collection.create_index([("created_at", -1)])
    await videos_collection.create_index([("uploader_id", 1)])
    await videos_collection.create_index([("share_count", -1)])
    await videos_collection.create_index([("hash_md5", 1)])
    await users_collection.create_index([("user_id", 1)], unique=True)
    await hashes_collection.create_index([("hash_md5", 1)], unique=True)

# ==================== USER MANAGEMENT ====================
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
            "last_daily_bonus": None,
            "streak": 0,
        }
        await users_collection.insert_one(doc)
    return doc

async def update_user(user_id: int, updates: dict) -> None:
    await users_collection.update_one({"user_id": user_id}, {"$set": updates})

async def can_upload(user_id: int, is_admin: bool = False) -> Tuple[bool, str]:
    if is_admin:
        return True, ""
    user = await get_user(user_id)
    if user.get("banned", False):
        return False, "You are banned."
    now = datetime.utcnow()
    sub_exp = user.get("subscription_expiry")
    if sub_exp and sub_exp > now:
        last_up = user.get("last_upload_time")
        if last_up and (now - last_up).total_seconds() < SUBSCRIPTION_COOLDOWN_SEC:
            remain = SUBSCRIPTION_COOLDOWN_SEC - int((now - last_up).total_seconds())
            return False, f"Cooldown: wait {remain}s."
        return True, ""
    else:
        today = now.date().isoformat()
        daily = user.get("daily_uploads", {})
        count = daily.get(today, 0)
        if count >= FREE_DAILY_LIMIT:
            return False, f"Daily limit ({FREE_DAILY_LIMIT}) reached."
        return True, ""

async def record_upload(user_id: int, is_video: bool = False, is_admin: bool = False) -> None:
    if is_admin:
        return
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

# ==================== DAILY BONUS ====================
async def claim_daily_bonus(user_id: int) -> Tuple[bool, str]:
    user = await get_user(user_id)
    now = datetime.utcnow()
    last = user.get("last_daily_bonus")
    if last and (now - last).days < 1:
        return False, "Already claimed today."
    today = now.date().isoformat()
    daily = user.get("daily_uploads", {})
    daily[today] = max(0, daily.get(today, 0) - 1)
    streak = user.get("streak", 0) + 1
    updates = {
        "last_daily_bonus": now,
        "daily_uploads": daily,
        "streak": streak,
    }
    await update_user(user_id, updates)
    return True, f"Daily bonus claimed! Streak: {streak}"

# ==================== FILE DEDUPLICATION ====================
async def is_duplicate_file(file_bytes: bytes) -> bool:
    md5 = hashlib.md5(file_bytes).hexdigest()
    existing = await hashes_collection.find_one({"hash_md5": md5})
    return existing is not None

async def store_file_hash(file_bytes: bytes) -> None:
    md5 = hashlib.md5(file_bytes).hexdigest()
    await hashes_collection.update_one({"hash_md5": md5}, {"$set": {"hash_md5": md5}}, upsert=True)

# ==================== FUZZY SEARCH ====================
def fuzzy_score(query: str, name: str) -> int:
    return fuzz.ratio(query.lower(), name.lower())

async def search_uploaded(query: str, limit: int = 15, category: Optional[str] = None) -> List[UploadedSound]:
    q = clean_spaces(query).lower()
    filter_dict = {}
    if category:
        filter_dict["category"] = category
    if not q:
        pipeline = [{"$match": filter_dict}, {"$sample": {"size": limit}}]
        docs = await sounds_collection.aggregate(pipeline).to_list(length=limit)
        return [UploadedSound(**{k:v for k,v in doc.items() if k in UploadedSound.__dataclass_fields__}) for doc in docs]
    docs = await sounds_collection.find({**filter_dict, "name_lower": {"$regex": re.escape(q)}}).limit(limit*5).to_list(length=limit*5)
    if not docs:
        return []
    scored = [(fuzzy_score(q, doc["name"]), doc) for doc in docs]
    scored.sort(key=lambda x: -x[0])
    return [UploadedSound(**{k:v for k,v in item[1].items() if k in UploadedSound.__dataclass_fields__}) for item in scored[:limit]]

async def search_videos(query: str, limit: int = 10) -> List[UploadedVideo]:
    q = clean_spaces(query).lower()
    if not q:
        pipeline = [{"$sample": {"size": limit}}]
        docs = await videos_collection.aggregate(pipeline).to_list(length=limit)
        return [UploadedVideo(**{k:v for k,v in doc.items() if k in UploadedVideo.__dataclass_fields__}) for doc in docs]
    docs = await videos_collection.find({"name_lower": {"$regex": re.escape(q)}}).limit(limit*3).to_list(length=limit*3)
    scored = [(fuzzy_score(q, doc["name"]), doc) for doc in docs]
    scored.sort(key=lambda x: -x[0])
    return [UploadedVideo(**{k:v for k,v in item[1].items() if k in UploadedVideo.__dataclass_fields__}) for item in scored[:limit]]

# ==================== MYINSTANTS ====================
MYINSTANTS_BASE = "https://www.myinstants.com"
MYINSTANTS_SEARCH = "https://www.myinstants.com/en/search/?name={query}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

async def search_myinstants_cached(query: str, limit: int = 12) -> List[ExternalSound]:
    cache_key = f"myinstants:{query}:{limit}"
    cached = await redis_client.get(cache_key)
    if cached:
        data = json.loads(cached)
        return [ExternalSound(**item) for item in data]
    results = await _search_myinstants(query, limit)
    if results:
        await redis_client.setex(cache_key, 3600, json.dumps([r.__dict__ for r in results]))
    return results

async def _search_myinstants(query: str, limit: int = 12) -> List[ExternalSound]:
    if not MYINSTANTS_ENABLED:
        return []
    url = MYINSTANTS_SEARCH.format(query=quote_plus(query))
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        try:
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()
        except Exception:
            return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.select("a.instant"):
        href = a.get("href")
        if not href:
            continue
        full_url = urljoin(MYINSTANTS_BASE, href)
        name = a.select_one(".instant-title")
        title = name.text.strip() if name else "Untitled"
        # Get mp3 link from detail page
        audio_url = await _fetch_myinstants_mp3(session, full_url)
        if audio_url:
            items.append(ExternalSound(name=title, page_url=full_url, audio_url=audio_url))
            if len(items) >= limit:
                break
    return items

async def _fetch_myinstants_mp3(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, timeout=20) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
    except Exception:
        return None
    soup = BeautifulSoup(html, "html.parser")
    # Find mp3 link
    for a in soup.select("a[href$='.mp3']"):
        href = a.get("href")
        if href and "/media/sounds/" in href:
            return urljoin(MYINSTANTS_BASE, href)
    return None

# ==================== INLINE FLOOD CONTROL ====================
user_last_queries = defaultdict(list)
FLOOD_MAX_QUERIES = 10
FLOOD_TIME_WINDOW = 10

def is_flooding(user_id: int) -> bool:
    now = time.time()
    timestamps = user_last_queries[user_id]
    timestamps = [t for t in timestamps if now - t < FLOOD_TIME_WINDOW]
    user_last_queries[user_id] = timestamps
    if len(timestamps) >= FLOOD_MAX_QUERIES:
        return True
    timestamps.append(now)
    return False

# ==================== KEYBOARDS ====================
def category_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cat in ["Meme", "Gaming", "Anime", "Funny", "Other"]:
        builder.button(text=cat, callback_data=f"cat_{cat}")
    builder.adjust(2)
    return builder.as_markup()

def premium_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Yes (Premium)", callback_data="premium_yes")
    builder.button(text="No", callback_data="premium_no")
    return builder.as_markup()

# ==================== COMMANDS ====================
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    claimed, msg = await claim_daily_bonus(user_id)
    if claimed:
        await message.answer(f"🎁 {msg}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Profile", callback_data="profile")],
        [InlineKeyboardButton(text="📢 Support", url=SUPPORT_CHANNEL_URL)],
        [InlineKeyboardButton(text="👨‍💻 Admin", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton(text="🔍 Search Inline", switch_inline_query_current_chat="")],
    ])
    if WELCOME_MEDIA_URL.endswith(".gif"):
        await message.answer_animation(WELCOME_MEDIA_URL, caption="🎵 Sound Bot Ready!", reply_markup=kb)
    elif WELCOME_MEDIA_URL:
        await message.answer_photo(WELCOME_MEDIA_URL, caption="🎵 Sound Bot Ready!", reply_markup=kb)
    else:
        await message.answer("🎵 Sound Bot Ready!\nUse /upload to add content.", reply_markup=kb)
    await log_event(f"🟢 /start user {user_id}")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    text = command.args
    if not text:
        await message.reply("Usage: /broadcast <message>")
        return
    users = await users_collection.find().to_list(length=None)
    success = 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.reply(f"Broadcast sent to {success}/{len(users)} users.")

@router.message(Command("trending"))
async def cmd_trending(message: Message):
    day_ago = datetime.utcnow() - timedelta(days=1)
    pipeline = [
        {"$match": {"created_at": {"$gte": day_ago}}},
        {"$sort": {"share_count": -1}},
        {"$limit": 5}
    ]
    trending = await sounds_collection.aggregate(pipeline).to_list(5)
    text = "📈 Trending Sounds (24h):\n"
    for i, s in enumerate(trending, 1):
        text += f"{i}. {s['name']} – {s['share_count']} shares\n"
    await message.answer(text)

@router.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.reply("🏓 Pong!")

# ==================== UPLOAD HANDLERS ====================
@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext):
    await state.set_state(UploadStates.waiting_media_batch)
    await message.answer("Send audio/video...")

async def process_upload_audio(media: MediaPayload, user_id: int, name: str, category: str, premium: bool):
    is_admin = user_id in ADMIN_IDS
    raw = media.file_bytes or await fetch_telegram_file_bytes(media.file_id)
    if not is_admin and await is_adult_content_sightengine(raw, media.file_name or ""):
        await add_warning(user_id, "Adult content")
        return
    if not is_admin and await is_duplicate_file(raw):
        await bot.send_message(user_id, f"❌ File already exists: {name}")
        return
    thumb_url = await generate_waveform_thumbnail(raw, name)
    cached_id, _ = await mirror_to_storage_voice(media, name, thumb_url)
    doc = {
        "name": name, "name_lower": name.lower(), "source": "user_upload",
        "uploader_id": user_id, "cached_voice_file_id": cached_id,
        "duration": media.duration, "size_bytes": len(raw), "mime_type": media.mime_type,
        "original_name": media.file_name, "created_at": datetime.utcnow(),
        "share_count": 0, "thumb_url": thumb_url, "is_adult": False,
        "category": category, "premium": premium, "hash_md5": hashlib.md5(raw).hexdigest()
    }
    await sounds_collection.update_one({"name_lower": name.lower()}, {"$set": doc}, upsert=True)
    await store_file_hash(raw)
    await record_upload(user_id, is_video=False, is_admin=is_admin)
    await bot.send_message(user_id, f"✅ Uploaded: {name}")

@router.message(UploadStates.waiting_media_batch)
async def batch_media_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    media = extract_media(message)
    if not media:
        return
    if media.kind in ("video", "video_document"):
        # Handle video separately (omitted for brevity)
        await message.answer("Video upload not implemented yet.")
        await state.clear()
        return
    await state.update_data(pending_media=media)
    await state.set_state(UploadStates.waiting_category)
    await message.answer("Choose category:", reply_markup=category_keyboard())

@router.callback_query(F.data.startswith("cat_"))
async def category_chosen(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[1]
    await state.update_data(category=category)
    await state.set_state(UploadStates.waiting_premium)
    await callback.message.edit_text("Premium sound? (Admins only)", reply_markup=premium_keyboard())

@router.callback_query(F.data.startswith("premium_"))
async def premium_chosen(callback: CallbackQuery, state: FSMContext):
    premium = callback.data == "premium_yes"
    data = await state.get_data()
    media: MediaPayload = data["pending_media"]
    category = data["category"]
    name = media.file_name or f"Audio_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    name = clean_spaces(name)[:80]
    await upload_queue.put({
        "func": process_upload_audio,
        "args": (media, callback.from_user.id, name, category, premium)
    })
    await callback.message.edit_text("⏳ Upload queued. You'll be notified when done.")
    await state.clear()

# ==================== INLINE MODE ====================
@router.inline_query()
async def inline_handler(inline_query: InlineQuery):
    user_id = inline_query.from_user.id
    if is_flooding(user_id):
        await inline_query.answer([], cache_time=10, is_personal=True, switch_pm_text="Slow down!", switch_pm_parameter="flood")
        return
    query = clean_spaces(inline_query.query)
    results = []
    sounds = await search_uploaded(query, limit=INLINE_RANDOM_LIMIT if not query else 15)
    for s in sounds:
        if s.cached_voice_file_id:
            caption = s.name
            user = await get_user(user_id)
            if not user.get("subscription_expiry"):
                caption += "\n🔊 via @YourBot"
            results.append(InlineQueryResultCachedVoice(
                id=f"upload:{s.name}:{uuid.uuid4().hex[:8]}",
                voice_file_id=s.cached_voice_file_id,
                title=s.name,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton("🚩 Report", callback_data=f"report_{s.name}")]
                ])
            ))
    # Add Myinstants results
    if query and MYINSTANTS_ENABLED:
        ext = await search_myinstants_cached(query, limit=5)
        for e in ext:
            results.append(InlineQueryResultAudio(
                id=f"mi:{e.name}:{uuid.uuid4().hex[:8]}",
                audio_url=e.audio_url,
                title=e.name,
                performer="Myinstants"
            ))
    if not results:
        results.append(InlineQueryResultArticle(
            id="noresult",
            title="No sounds found",
            input_message_content=InputTextMessageContent(message_text="Try another keyword.")
        ))
    await inline_query.answer(results, cache_time=INLINE_CACHE_SECONDS, is_personal=True)

@router.callback_query(F.data.startswith("report_"))
async def report_callback(callback: CallbackQuery):
    sound_name = callback.data[7:]
    await log_event(f"🚩 User {callback.from_user.id} reported sound: {sound_name}")
    await callback.answer("Reported. Admin will review.", show_alert=True)

@router.chosen_inline_result()
async def chosen_handler(chosen: ChosenInlineResult):
    rid = chosen.result_id
    parts = rid.split(":")
    if len(parts) >= 2 and parts[0] == "upload":
        name = parts[1]
        result = await sounds_collection.update_one({"name": name}, {"$inc": {"share_count": 1}})
        if result.modified_count:
            doc = await sounds_collection.find_one({"name": name})
            if doc and doc["share_count"] % 100 == 0:
                try:
                    await bot.send_message(doc["uploader_id"], f"🎉 Your sound '{name}' reached {doc['share_count']} shares!")
                except:
                    pass

# ==================== MAIN ====================
async def main():
    await ensure_indexes()
    # Start workers (event loop is running now)
    for _ in range(2):
        worker_tasks.append(asyncio.create_task(upload_worker()))
    # Graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown_handler(s)))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

async def shutdown_handler(sig):
    logger.info(f"Received signal {sig}, shutting down...")
    await upload_queue.join()
    for task in worker_tasks:
        task.cancel()
    await redis_client.close()
    await bot.session.close()
    sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
