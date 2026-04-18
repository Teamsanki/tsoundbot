import asyncio
import base64
import io
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
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
    InputTextMessageContent,
    Message,
)
from bs4 import BeautifulSoup

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LOGGER_GROUP_ID = int(os.getenv("LOGGER_GROUP_ID", "0"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_BASE_DIR = os.getenv("GITHUB_BASE_DIR", "sounds")

MAX_UPLOAD_SIZE_MB = float(os.getenv("MAX_UPLOAD_SIZE_MB", "10"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "60"))
INLINE_CACHE_SECONDS = int(os.getenv("INLINE_CACHE_SECONDS", "20"))
MYINSTANTS_ENABLED = os.getenv("MYINSTANTS_ENABLED", "true").lower() == "true"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # Optional CDN/base URL for hosted uploads.

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not (GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO):
    raise RuntimeError("GitHub config missing: GITHUB_TOKEN / GITHUB_OWNER / GITHUB_REPO")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("sound-bot")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ------------------------------------------------------------
# Models / states
# ------------------------------------------------------------

@dataclass
class UploadedSound:
    name: str
    file_path: str
    raw_url: str
    source: str
    uploader_id: int
    duration: Optional[int] = None
    size_bytes: Optional[int] = None
    telegram_file_id: Optional[str] = None
    mime_type: Optional[str] = None


class UploadStates(StatesGroup):
    waiting_audio = State()
    waiting_name = State()


# ------------------------------------------------------------
# GitHub storage
# ------------------------------------------------------------

class GitHubStorage:
    def __init__(self, token: str, owner: str, repo: str, branch: str, base_dir: str):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_dir = base_dir.strip("/")
        self.api_root = f"https://api.github.com/repos/{owner}/{repo}/contents"
        self.raw_root = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "telegram-sound-bot",
        }

    async def _get_file(self, session: aiohttp.ClientSession, path: str) -> Optional[Dict[str, Any]]:
        url = f"{self.api_root}/{path}"
        params = {"ref": self.branch}
        async with session.get(url, headers=self.headers, params=params) as resp:
            if resp.status == 404:
                return None
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"GitHub GET failed for {path}: {resp.status} {text}")
            return json.loads(text)

    async def _put_file(self, session: aiohttp.ClientSession, path: str, content_bytes: bytes, message: str, sha: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.api_root}/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("utf-8"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        async with session.put(url, headers=self.headers, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"GitHub PUT failed for {path}: {resp.status} {text}")
            return json.loads(text)

    async def load_index(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        path = f"{self.base_dir}/index.json"
        data = await self._get_file(session, path)
        if not data:
            return []
        content = data.get("content", "")
        if data.get("encoding") == "base64":
            decoded = base64.b64decode(content)
            try:
                return json.loads(decoded.decode("utf-8"))
            except Exception:
                logger.exception("Failed to parse index.json, using empty list")
                return []
        return []

    async def save_index(self, session: aiohttp.ClientSession, rows: List[Dict[str, Any]]) -> None:
        path = f"{self.base_dir}/index.json"
        current = await self._get_file(session, path)
        sha = current.get("sha") if current else None
        body = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
        await self._put_file(session, path, body, "Update sounds index", sha)

    async def upload_sound(self, *, content_bytes: bytes, original_ext: str, display_name: str, uploader_id: int, duration: Optional[int], size_bytes: int, telegram_file_id: Optional[str], mime_type: Optional[str]) -> UploadedSound:
        async with aiohttp.ClientSession() as session:
            safe_name = slugify(display_name)
            ext = normalize_extension(original_ext, mime_type)
            file_rel_path = f"{self.base_dir}/uploads/{safe_name}{ext}"

            existing = await self._get_file(session, file_rel_path)
            sha = existing.get("sha") if existing else None
            await self._put_file(
                session,
                file_rel_path,
                content_bytes,
                f"Upload sound: {display_name}",
                sha,
            )

            raw_url = PUBLIC_BASE_URL.rstrip("/") + f"/{file_rel_path}" if PUBLIC_BASE_URL else f"{self.raw_root}/{file_rel_path}"

            rows = await self.load_index(session)
            rows = [row for row in rows if row.get("name", "").lower() != display_name.lower()]
            rows.append(
                {
                    "name": display_name,
                    "file_path": file_rel_path,
                    "raw_url": raw_url,
                    "source": "user_upload",
                    "uploader_id": uploader_id,
                    "duration": duration,
                    "size_bytes": size_bytes,
                    "telegram_file_id": telegram_file_id,
                    "mime_type": mime_type,
                }
            )
            rows.sort(key=lambda x: x.get("name", "").lower())
            await self.save_index(session, rows)

            return UploadedSound(
                name=display_name,
                file_path=file_rel_path,
                raw_url=raw_url,
                source="user_upload",
                uploader_id=uploader_id,
                duration=duration,
                size_bytes=size_bytes,
                telegram_file_id=telegram_file_id,
                mime_type=mime_type,
            )

    async def search_uploaded(self, query: str, limit: int = 15) -> List[UploadedSound]:
        async with aiohttp.ClientSession() as session:
            rows = await self.load_index(session)
        q = query.strip().lower()
        scored: List[tuple[int, Dict[str, Any]]] = []
        for row in rows:
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
        scored.sort(key=lambda item: (-item[0], item[1].get("name", "").lower()))
        return [UploadedSound(**item[1]) for item in scored[:limit]]


storage = GitHubStorage(
    token=GITHUB_TOKEN,
    owner=GITHUB_OWNER,
    repo=GITHUB_REPO,
    branch=GITHUB_BRANCH,
    base_dir=GITHUB_BASE_DIR,
)

# ------------------------------------------------------------
# Myinstants scraping
# ------------------------------------------------------------

MYINSTANTS_BASE = "https://www.myinstants.com"
MYINSTANTS_SEARCH = "https://www.myinstants.com/search/?name={query}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
}


@dataclass
class ExternalSound:
    name: str
    page_url: str
    audio_url: str
    source: str = "myinstants"


async def search_myinstants(query: str, limit: int = 15) -> List[ExternalSound]:
    if not MYINSTANTS_ENABLED or not query.strip():
        return []

    url = MYINSTANTS_SEARCH.format(query=quote_plus(query.strip()))
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.warning("Myinstants search failed: %s %s", resp.status, text[:250])
                return []

    soup = BeautifulSoup(text, "html.parser")
    results: List[ExternalSound] = []

    # Strategy 1: anchors pointing to /instant/.../
    seen = set()
    for a in soup.select('a[href*="/instant/"]'):
        href = (a.get("href") or "").strip()
        title = a.get_text(" ", strip=True)
        if not href or not title:
            continue
        page_url = href if href.startswith("http") else f"{MYINSTANTS_BASE}{href}"
        if page_url in seen:
            continue
        slug = page_url.rstrip("/").split("/")[-1]
        if not slug:
            continue
        audio_url = f"{MYINSTANTS_BASE}/media/sounds/{slug}.mp3"
        seen.add(page_url)
        results.append(ExternalSound(name=clean_spaces(title), page_url=page_url, audio_url=audio_url))
        if len(results) >= limit:
            break

    return results[:limit]


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\-_\s]+", "", text)
    text = re.sub(r"\s+", "-", text)
    text = text.strip("-_")
    return text or f"sound-{uuid.uuid4().hex[:8]}"


def normalize_extension(original_ext: str, mime_type: Optional[str]) -> str:
    ext = (original_ext or "").lower().strip()
    if ext in {".mp3", ".ogg", ".wav", ".m4a"}:
        return ext
    mime_map = {
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
    }
    return mime_map.get(mime_type or "", ".mp3")


def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def format_size_mb(size_bytes: Optional[int]) -> str:
    if not size_bytes:
        return "unknown"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def upload_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Search Inline", switch_inline_query_current_chat="")],
            [InlineKeyboardButton(text="Use in any chat", switch_inline_query="")],
        ]
    )


async def log_start_event(message: Message) -> None:
    if not LOGGER_GROUP_ID:
        return
    user = message.from_user
    txt = (
        "🟢 <b>/start used</b>\n"
        f"👤 Name: {user.full_name}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"🔗 Username: @{user.username if user.username else 'none'}\n"
        f"💬 Chat ID: <code>{message.chat.id}</code>\n"
        f"🏷 Chat Type: <code>{message.chat.type}</code>"
    )
    try:
        await bot.send_message(LOGGER_GROUP_ID, txt, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to log /start")


async def fetch_telegram_file_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    return buffer.getvalue()


def extract_media(msg: Message) -> Optional[Audio | Document]:
    if msg.audio:
        return msg.audio
    if msg.document and (msg.document.mime_type or "").startswith("audio/"):
        return msg.document
    return None


# ------------------------------------------------------------
# Commands
# ------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await log_start_event(message)
    text = (
        "🎵 <b>Sound Inline Bot</b>\n\n"
        "Use me in any chat like:\n"
        "<code>@YourBotName vine boom</code>\n\n"
        "Commands:\n"
        "• /upload — upload your own sound\n"
        "• /start — show help\n\n"
        f"Upload rules:\n• max size: {MAX_UPLOAD_SIZE_MB:g} MB\n• max duration: {MAX_DURATION_SECONDS} sec"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=upload_keyboard())


@router.message(Command("upload"))
async def cmd_upload(message: Message, state: FSMContext) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("/upload sirf private chat me use karo.")
        return
    await state.set_state(UploadStates.waiting_audio)
    await message.answer(
        f"Audio bhej de.\nRules: max {MAX_UPLOAD_SIZE_MB:g} MB, max {MAX_DURATION_SECONDS} sec.\nAccepted: audio/document(audio/*)."
    )


@router.message(UploadStates.waiting_audio)
async def upload_receive_audio(message: Message, state: FSMContext) -> None:
    media = extract_media(message)
    if not media:
        await message.reply("Audio file bhej bhai. Normal file nahi.")
        return

    size_limit = int(MAX_UPLOAD_SIZE_MB * 1024 * 1024)
    if (media.file_size or 0) > size_limit:
        await message.reply(f"File {MAX_UPLOAD_SIZE_MB:g} MB se badi hai.")
        return

    duration = getattr(media, "duration", None)
    if duration and duration > MAX_DURATION_SECONDS:
        await message.reply(f"Duration {MAX_DURATION_SECONDS} sec se zyada hai.")
        return

    await state.update_data(
        pending_file_id=media.file_id,
        pending_unique_id=media.file_unique_id,
        pending_duration=duration,
        pending_size=media.file_size,
        pending_mime=media.mime_type,
        pending_filename=getattr(media, "file_name", None),
    )
    await state.set_state(UploadStates.waiting_name)
    await message.answer("Ab is sound ka naam bhej de. Example: Vine Boom Ultra")


@router.message(UploadStates.waiting_name)
async def upload_receive_name(message: Message, state: FSMContext) -> None:
    name = clean_spaces(message.text or "")
    if not name:
        await message.reply("Naam text me bhej.")
        return
    if len(name) > 80:
        await message.reply("Naam 80 characters ke andar rakh.")
        return

    data = await state.get_data()
    file_id = data["pending_file_id"]
    duration = data.get("pending_duration")
    size_bytes = data.get("pending_size")
    mime_type = data.get("pending_mime")
    original_filename = data.get("pending_filename") or "sound"
    original_ext = os.path.splitext(original_filename)[1]

    msg = await message.answer("Upload ho raha hai...")
    try:
        content = await fetch_telegram_file_bytes(file_id)
        uploaded = await storage.upload_sound(
            content_bytes=content,
            original_ext=original_ext,
            display_name=name,
            uploader_id=message.from_user.id,
            duration=duration,
            size_bytes=size_bytes or len(content),
            telegram_file_id=file_id,
            mime_type=mime_type,
        )
        await state.clear()
        await msg.edit_text(
            "✅ Upload done\n"
            f"Name: <b>{uploaded.name}</b>\n"
            f"Size: <code>{format_size_mb(uploaded.size_bytes)}</code>\n"
            f"Duration: <code>{uploaded.duration or 'unknown'} sec</code>\n\n"
            "Ab inline me search karke kahin bhi send kar sakta hai.",
            parse_mode="HTML",
            reply_markup=upload_keyboard(),
        )
    except Exception as exc:
        logger.exception("Upload failed")
        await msg.edit_text(f"❌ Upload fail ho gaya: {exc}")


# ------------------------------------------------------------
# Inline mode
# ------------------------------------------------------------

@router.inline_query()
async def inline_handler(inline_query: InlineQuery) -> None:
    query = clean_spaces(inline_query.query)
    results = []

    uploaded_results = await storage.search_uploaded(query, limit=15)
    for item in uploaded_results:
        results.append(
            InlineQueryResultAudio(
                id=f"upload:{slugify(item.name)}:{uuid.uuid4().hex[:8]}",
                audio_url=item.raw_url,
                title=f"📁 {item.name}",
                caption=f"{item.name}\nSource: user uploads",
                performer="User Upload",
                audio_duration=item.duration,
            )
        )

    remaining = max(0, 40 - len(results))
    if remaining and query:
        try:
            ext_results = await search_myinstants(query, limit=min(remaining, 20))
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
            logger.exception("Inline Myinstants search failed")

    if not results:
        results = [
            InlineQueryResultArticle(
                id=f"empty:{uuid.uuid4().hex}",
                title="No sounds found",
                description="Try another keyword or upload your own sound in PM with /upload",
                input_message_content=InputTextMessageContent(
                    message_text="No sounds found. Open bot PM and use /upload to add your own sound."
                ),
            )
        ]

    await inline_query.answer(results, cache_time=INLINE_CACHE_SECONDS, is_personal=True)


@router.chosen_inline_result()
async def chosen_result_handler(chosen: ChosenInlineResult) -> None:
    logger.info("Chosen inline result: user=%s query=%r result_id=%s", chosen.from_user.id, chosen.query, chosen.result_id)


# ------------------------------------------------------------
# Admin helpers
# ------------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    rows = await storage.search_uploaded("", limit=9999)
    await message.reply(f"Uploaded sounds in index: {len(rows)}")


# ------------------------------------------------------------
# Runner
# ------------------------------------------------------------

async def main() -> None:
    me = await bot.get_me()
    logger.info("Bot started as @%s", me.username)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
