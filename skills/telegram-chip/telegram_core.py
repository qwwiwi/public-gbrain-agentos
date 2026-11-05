"""
Telegram Core Module - Shared functionality for MCP and HTTP API.

This module contains the core Telegram client and all business logic functions
that can be used by both the MCP server and the HTTP API.
"""

import os
import json
import logging
import re
import asyncio
import random
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Dict, Optional, Union, Any
from pathlib import Path
from functools import wraps
from contextlib import asynccontextmanager
import secrets as _secrets

from dotenv import load_dotenv
from pythonjsonlogger import jsonlogger
from telethon import TelegramClient, functions, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError as TelethonFloodWaitError
from telethon.errors import ChatNotModifiedError, RPCError, ServerError, TimedOutError
from telethon.tl.types.messages import InvitedUsers
from telethon.tl.types import (
    User,
    Chat,
    ChatForbidden,
    Channel,
    ChatAdminRights,
    ChatBannedRights,
    ChannelParticipantsKicked,
    ChannelParticipantsAdmins,
    InputChatPhotoEmpty,
)

# Load environment variables
load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "telegram_session")

LOCK_PATH = os.getenv("TELEGRAM_CORE_LOCK_PATH", "/tmp/telegram-core.lock")

from media_info import (  # noqa: E402  (local module, after env load)
    TRANSCRIBABLE_KINDS,
    describe_media,
    media_marker,
    message_text_with_marker,
)

# ---- Media download / transcription settings (additive, 2026-10-16) ----
SKILL_DIR = Path(__file__).resolve().parent
MEDIA_CACHE_DIR = Path(os.getenv("CHIP_MEDIA_CACHE_DIR", str(SKILL_DIR / "media-cache")))
MEDIA_MAX_BYTES = int(os.getenv("CHIP_MEDIA_MAX_BYTES", str(50 * 1024 * 1024)))
MEDIA_DOWNLOAD_TIMEOUT_S = 300.0
# Groq Whisper transcription.
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MAX_BYTES = 25 * 1024 * 1024
GROQ_MODEL = os.getenv("CHIP_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
GROQ_LANGUAGE = os.getenv("CHIP_TRANSCRIBE_LANGUAGE", "ru")
GROQ_KEY_FILE_CANDIDATES = [
    SKILL_DIR / "secrets" / "groq-api-key",
    Path.home() / ".secrets" / "groq-api-key",
]


class MediaFetchError(Exception):
    """Media endpoint failure with an HTTP status code for the API layer."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---- Chat folders (dialog filters), additive 2026-10-16 ----
FOLDER_ID_MIN = 2  # 0 = "All chats", 1 = archive; user folders use 2..255
FOLDER_ID_MAX = 255
FOLDER_MAX_PEERS = 200  # premium dialog_filters_chats_limit (pinned + included)
FOLDER_TITLE_MAX = 12  # Telegram folder title limit
FOLDER_MODES = ("replace", "add", "remove")


class FolderError(Exception):
    """Folder endpoint failure with an HTTP status code for the API layer."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


FIRST_WRITE_PAGE_SIZE = 100
FIRST_WRITE_MAX_PAGES = 20  # up to 2000 messages scanned for an incoming one
FIRST_WRITE_REFUSAL = "no dialog: contact never wrote first – sending is forbidden (ban risk)"


class SendRefusedError(Exception):
    """Send blocked by the never-write-first rule (hard rule, ban risk)."""

    def __init__(self, message: str = FIRST_WRITE_REFUSAL):
        super().__init__(message)
        self.status_code = 403
        self.message = message


def _folder_title_text(title_obj: Any) -> str:
    """Plain text of a folder title (str in old layers, TextWithEntities in layer >= 190)."""
    if title_obj is None:
        return ""
    return title_obj.text if hasattr(title_obj, "text") else str(title_obj)


def _make_folder_title(title: str) -> Any:
    """Build a title value matching the installed Telethon DialogFilter constructor."""
    import inspect
    from telethon.tl.types import DialogFilter

    annotation = inspect.signature(DialogFilter.__init__).parameters["title"].annotation
    if "TextWithEntities" in str(annotation):
        from telethon.tl.types import TextWithEntities
        return TextWithEntities(text=title, entities=[])
    return title


def _input_peer_key(peer: Any) -> str:
    """Stable identity for an InputPeer (marked peer id), used for dedupe."""
    try:
        return str(utils.get_peer_id(peer))
    except Exception:
        return f"{type(peer).__name__}:{getattr(peer, 'user_id', None) or getattr(peer, 'chat_id', None) or getattr(peer, 'channel_id', None)}"


def _chats_from_result(result: Any) -> List[Any]:
    """Chats from an Updates result; unwraps messages.InvitedUsers (layer >= 166)."""
    if isinstance(result, InvitedUsers):
        result = result.updates
    return list(getattr(result, "chats", None) or [])


# Telegram-side internal errors: the request may still have been executed.
_AMBIGUOUS_RPC_ERRORS = (ServerError, TimedOutError)


def _is_definite_rejection(error: Exception) -> bool:
    """True only for an RPCError by which Telegram refused the request (nothing was executed)."""
    return isinstance(error, RPCError) and not isinstance(error, _AMBIGUOUS_RPC_ERRORS)


def _safe_component(value: Any) -> str:
    """Sanitize an id for use as a single path component."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:64]
    return cleaned or "_"


def load_groq_api_key() -> Optional[str]:
    """Read the Groq key at call time: env GROQ_API_KEY, GROQ_API_KEY_FILE, then secrets files.

    Never logged, never returned in responses.
    """
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if key:
        return key
    candidates: List[Path] = []
    key_file_env = os.getenv("GROQ_API_KEY_FILE")
    if key_file_env:
        candidates.append(Path(key_file_env))
    candidates.extend(GROQ_KEY_FILE_CANDIDATES)
    for path in candidates:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue  # e.g. systemd ProtectHome: home dirs are invisible
    return None


def redact_secret(text: str, secret: Optional[str]) -> str:
    """Replace every occurrence of secret in text with [REDACTED]."""
    if not secret or not text:
        return text
    return text.replace(secret, "[REDACTED]")


def _pid_alive(pid: int) -> bool:
    try:
        import os as _os
        _os.kill(pid, 0)
        return True
    except Exception:
        return False


def _acquire_lock():
    import os as _os
    if _os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r") as f:
                pid_str = f.read().strip()
                if pid_str.isdigit() and _pid_alive(int(pid_str)):
                    raise RuntimeError(
                        f"TelegramCore lock held by PID {pid_str}. Another instance is running."
                    )
        except RuntimeError:
            raise
        except Exception:
            pass
    with open(LOCK_PATH, "w") as f:
        f.write(str(_os.getpid()))


def _release_lock():
    import os as _os
    try:
        if _os.path.exists(LOCK_PATH):
            _os.remove(LOCK_PATH)
    except Exception:
        pass

SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")


class ValidationError(Exception):
    """Custom exception for validation errors."""
    pass


class ErrorCategory(str, Enum):
    CHAT = "CHAT"
    MSG = "MSG"
    CONTACT = "CONTACT"
    GROUP = "GROUP"
    MEDIA = "MEDIA"
    PROFILE = "PROFILE"
    AUTH = "AUTH"
    ADMIN = "ADMIN"


# Setup logging
logger = logging.getLogger("telegram_core")
logger.setLevel(logging.ERROR)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# Try to set up file logging
script_dir = os.path.dirname(os.path.abspath(__file__))
log_file_path = os.path.join(script_dir, "telegram_core.log")

try:
    file_handler = logging.FileHandler(log_file_path, mode="a")
    file_handler.setLevel(logging.ERROR)
    json_formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler.setFormatter(json_formatter)
    logger.addHandler(file_handler)
except Exception:
    pass


def json_serializer(obj):
    """Helper function to convert non-serializable objects for JSON serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def log_and_format_error(
    function_name: str,
    error: Exception,
    prefix: Optional[Union[ErrorCategory, str]] = None,
    user_message: str = None,
    **kwargs,
) -> str:
    """Centralized error handling function."""
    if isinstance(prefix, str) and prefix == "VALIDATION-001":
        error_code = prefix
    else:
        if prefix is None:
            for category in ErrorCategory:
                if category.name.lower() in function_name.lower():
                    prefix = category
                    break
        prefix_str = prefix.value if isinstance(prefix, ErrorCategory) else (prefix or "GEN")
        error_code = f"{prefix_str}-ERR-{abs(hash(function_name)) % 1000:03d}"

    context = ", ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.error(f"Error in {function_name} ({context}) - Code: {error_code}", exc_info=True)

    if user_message:
        return user_message
    return f"An error occurred (code: {error_code}). Check logs for details."


def validate_id_value(value, param_name: str):
    """Validate a single ID value. Returns (validated_value, error_message)."""
    if isinstance(value, int):
        if not (-(2**63) <= value <= 2**63 - 1):
            return None, f"Invalid {param_name}: {value}. ID is out of range."
        return value, None

    if isinstance(value, str):
        try:
            int_value = int(value)
            if not (-(2**63) <= int_value <= 2**63 - 1):
                return None, f"Invalid {param_name}: {value}. ID is out of range."
            return int_value, None
        except ValueError:
            if re.match(r"^@?[a-zA-Z0-9_]{5,}$", value):
                return value, None
            else:
                return None, f"Invalid {param_name}: '{value}'. Must be integer ID or username."

    return None, f"Invalid {param_name}: {value}. Type must be int or str."


def validate_ids(param_name: str, param_value):
    """Validate ID parameter(s). Returns (validated_value, error_message)."""
    if param_value is None:
        return None, None

    if isinstance(param_value, list):
        validated_list = []
        for item in param_value:
            validated_item, error_msg = validate_id_value(item, param_name)
            if error_msg:
                return None, error_msg
            validated_list.append(validated_item)
        return validated_list, None
    else:
        return validate_id_value(param_value, param_name)


def format_entity(entity) -> Dict[str, Any]:
    """Helper function to format entity information consistently."""
    result = {"id": entity.id}

    if hasattr(entity, "title"):
        result["name"] = entity.title
        result["type"] = "group" if isinstance(entity, Chat) else "channel"
    elif hasattr(entity, "first_name"):
        name_parts = []
        if entity.first_name:
            name_parts.append(entity.first_name)
        if hasattr(entity, "last_name") and entity.last_name:
            name_parts.append(entity.last_name)
        result["name"] = " ".join(name_parts)
        result["type"] = "user"
        if hasattr(entity, "username") and entity.username:
            result["username"] = entity.username
        if hasattr(entity, "phone") and entity.phone:
            result["phone"] = entity.phone

    return result


def format_message(message) -> Dict[str, Any]:
    """Helper function to format message information consistently."""
    result = {
        "id": message.id,
        "date": message.date.isoformat(),
        "text": message.message or "",
    }

    if message.from_id:
        result["from_id"] = utils.get_peer_id(message.from_id)

    if message.media:
        result["has_media"] = True
        result["media_type"] = type(message.media).__name__

    # Extract entities with URLs (TextUrl type)
    if message.entities:
        entities_list = []
        text = message.message or ""
        for entity in message.entities:
            entity_info = {
                "type": type(entity).__name__,
                "offset": entity.offset,
                "length": entity.length,
                "text": text[entity.offset:entity.offset + entity.length] if text else "",
            }
            if hasattr(entity, "url") and entity.url:
                entity_info["url"] = entity.url
            if hasattr(entity, "user_id") and entity.user_id:
                entity_info["user_id"] = entity.user_id
            entities_list.append(entity_info)
        if entities_list:
            result["entities"] = entities_list

    return result


def get_sender_name(message) -> str:
    """Helper function to get sender name from a message."""
    if not message.sender:
        return "Unknown"

    if hasattr(message.sender, "title") and message.sender.title:
        return message.sender.title
    elif hasattr(message.sender, "first_name"):
        first_name = getattr(message.sender, "first_name", "") or ""
        last_name = getattr(message.sender, "last_name", "") or ""
        full_name = f"{first_name} {last_name}".strip()
        return full_name if full_name else "Unknown"
    else:
        return "Unknown"


def get_engagement_info(message) -> str:
    """Helper function to get engagement metrics from a message."""
    engagement_parts = []
    views = getattr(message, "views", None)
    if views is not None:
        engagement_parts.append(f"views:{views}")
    forwards = getattr(message, "forwards", None)
    if forwards is not None:
        engagement_parts.append(f"forwards:{forwards}")
    reactions = getattr(message, "reactions", None)
    if reactions is not None:
        results = getattr(reactions, "results", None)
        total_reactions = sum(getattr(r, "count", 0) or 0 for r in results) if results else 0
        engagement_parts.append(f"reactions:{total_reactions}")
    return f" | {', '.join(engagement_parts)}" if engagement_parts else ""


class TelegramCore:
    """Core Telegram functionality that can be used by both MCP and HTTP API."""

    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self._started = False
        
        # Rate limiting tracking
        self._last_request_time: Optional[float] = None
        self._last_message_time_per_chat: Dict[Union[int, str], float] = {}
        self._last_edit_time: Optional[float] = None
        self._edit_count_last_hour: int = 0
        self._edit_count_reset_time: Optional[float] = None
        self.min_request_delay: float = 0.2  # 5 req/s
        self._rate_lock = asyncio.Lock()  # serializes _wait_for_rate_limit permits
        self._folder_lock = asyncio.Lock()  # serializes folder read-modify-write (upsert)
        # At most 2 concurrent media downloads (protects the account + disk).
        self._media_semaphore = asyncio.Semaphore(2)
        # At most 2 concurrent Groq transcriptions.
        self._transcribe_semaphore = asyncio.Semaphore(2)
        # Per-key locks (refcounted) so one attachment is never fetched twice at once.
        self._media_locks: Dict[Any, List[Any]] = {}

    async def start(self):
        """Initialize and start the Telegram client."""
        if self._started:
            return

        _acquire_lock()

        if SESSION_STRING:
            self.client = TelegramClient(
                StringSession(SESSION_STRING), TELEGRAM_API_ID, TELEGRAM_API_HASH
            )
        else:
            self.client = TelegramClient(
                TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH
            )

        await self.client.start()
        self._started = True

    async def stop(self):
        """Stop the Telegram client."""
        if self.client and self._started:
            await self.client.disconnect()
            self._started = False
        _release_lock()

    # ==================== Rate Limiting ====================
    
    async def _wait_for_rate_limit(self):
        """Ожидание перед следующим запросом для соблюдения rate limits."""
        import time
        # Serialize permit issuance: read -> compute -> sleep -> record happens
        # under one lock, so concurrent callers are spaced by min_request_delay
        # instead of all passing after the same sleep (fix 2026-10-16).
        async with self._rate_lock:
            await self._space_requests_locked()

    async def _space_requests_locked(self):
        """Sleep until min_request_delay has passed since the last permit. Caller holds _rate_lock."""
        import time
        current_time = time.time()

        if self._last_request_time is not None:
            elapsed = current_time - self._last_request_time
            if elapsed < self.min_request_delay:
                sleep_time = self.min_request_delay - elapsed
                # Добавляем небольшой jitter для избежания синхронизации
                sleep_time += random.uniform(0, 0.05)
                await asyncio.sleep(sleep_time)

        self._last_request_time = time.time()

    async def _strict_call(self, request: Any, single_attempt: bool = False) -> Any:
        """Run one write RPC with FloodWait raised (never slept on) and, if asked, no resend.

        Telethon 1.43 ignores the per-call flood_sleep_threshold argument, so the client-wide
        flood_sleep_threshold / _request_retries are changed for the duration of this call and
        restored in finally. _rate_lock is held throughout, so every caller of
        _wait_for_rate_limit waits until the values are restored. Telethon makes
        _request_retries + 1 attempts (helpers.retry_range), so 0 = exactly one attempt.
        """
        async with self._rate_lock:
            await self._space_requests_locked()
            client = self.client
            saved_threshold = client.flood_sleep_threshold
            saved_retries = client._request_retries
            client.flood_sleep_threshold = 0
            if single_attempt:
                client._request_retries = 0
            try:
                return await client(request)
            finally:
                client.flood_sleep_threshold = saved_threshold
                client._request_retries = saved_retries

    async def _check_edit_rate_limit(self):
        """Проверка лимита редактирования (5 edits/s, 120 edits/hour)."""
        import time
        current_time = time.time()
        
        # Сброс счетчика каждый час
        if self._edit_count_reset_time is None or current_time >= self._edit_count_reset_time:
            self._edit_count_last_hour = 0
            self._edit_count_reset_time = current_time + 3600
        
        # Проверка лимита 120 редактирований в час
        if self._edit_count_last_hour >= 120:
            wait_time = self._edit_count_reset_time - current_time
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                # Пересчитываем после ожидания
                current_time = time.time()
                if current_time >= self._edit_count_reset_time:
                    self._edit_count_last_hour = 0
                    self._edit_count_reset_time = current_time + 3600
        
        # Проверка лимита 5 редактирований в секунду
        if self._last_edit_time is not None:
            elapsed = current_time - self._last_edit_time
            if elapsed < 0.2:  # 5 edits/s = 1 edit per 0.2s
                sleep_time = 0.2 - elapsed + random.uniform(0, 0.02)
                await asyncio.sleep(sleep_time)
        
        self._last_edit_time = time.time()
        self._edit_count_last_hour += 1

    async def _check_message_rate_limit(self, chat_id: Union[int, str]):
        """Проверка лимита отправки сообщений (1 msg/s в один чат)."""
        import time
        current_time = time.time()
        
        if chat_id in self._last_message_time_per_chat:
            elapsed = current_time - self._last_message_time_per_chat[chat_id]
            if elapsed < 1.0:  # Минимум 1 секунда между сообщениями в один чат
                sleep_time = 1.0 - elapsed + random.uniform(0, 0.1)
                await asyncio.sleep(sleep_time)
        
        self._last_message_time_per_chat[chat_id] = time.time()

    # ==================== Poll Operations ====================

    async def poll_new_dms(self, since_ts: float, limit_per_chat: int = 10) -> str:
        """Return private messages newer than since_ts (Unix timestamp).

        Walks all private dialogs via the existing chip session (no new Telethon
        instance). Rate limiting is applied per get_dialogs + per chat read.
        Only dialogs with messages after since_ts are returned.
        """
        from datetime import timezone
        since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
        cutoff_dt = datetime(2026, 4, 23, tzinfo=timezone.utc)

        results = []
        try:
            await self._wait_for_rate_limit()
            dialogs = await self.client.get_dialogs(limit=200)
        except TelethonFloodWaitError as e:
            wait_time = min(float(getattr(e, "seconds", 0)), 3600.0)
            if wait_time > 0:
                await asyncio.sleep(wait_time + random.uniform(0, 1))
                dialogs = await self.client.get_dialogs(limit=200)
            else:
                raise

        for dialog in dialogs:
            entity = dialog.entity
            if not isinstance(entity, User):
                continue  # private DMs only

            # Skip if last message is older than our cutoff
            if dialog.date and dialog.date < since_dt:
                continue

            try:
                await self._wait_for_rate_limit()
                messages = await self.client.get_messages(entity, limit=limit_per_chat)
            except TelethonFloodWaitError as e:
                wait_time = min(float(getattr(e, "seconds", 0)), 3600.0)
                if wait_time > 0:
                    await asyncio.sleep(wait_time + random.uniform(0, 1))
                    messages = await self.client.get_messages(entity, limit=limit_per_chat)
                else:
                    continue
            except Exception:
                continue

            new_msgs = []
            for msg in messages:
                if not msg.date or msg.date < cutoff_dt:
                    continue
                if msg.date <= since_dt:
                    continue
                if not msg.message:
                    continue  # skip media-only
                sender_name = get_sender_name(msg)
                new_msgs.append({
                    "msg_id": msg.id,
                    "sender_name": sender_name,
                    "date": msg.date.isoformat(),
                    "text": msg.message,
                    "out": msg.out,
                })

            if new_msgs:
                chat_info = format_entity(entity)
                results.append({
                    "chat_id": chat_info.get("id"),
                    "name": chat_info.get("name"),
                    "username": chat_info.get("username"),
                    "messages": new_msgs,
                })

        return json.dumps(results, ensure_ascii=False, default=json_serializer)

    # ==================== Chat Operations ====================

    async def get_chats(self, page: int = 1, page_size: int = 20) -> str:
        """Get a paginated list of chats."""
        try:
            await self._wait_for_rate_limit()
            dialogs = await self.client.get_dialogs()
            start = (page - 1) * page_size
            end = start + page_size
            if start >= len(dialogs):
                return "Page out of range."
            chats = dialogs[start:end]
            lines = []
            for dialog in chats:
                entity = dialog.entity
                chat_id = entity.id
                title = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")
                lines.append(f"Chat ID: {chat_id}, Title: {title}")
            return "\n".join(lines)
        except TelethonFloodWaitError as e:
            return log_and_format_error("get_chats", e, note=f"FloodWait: {e.seconds}s")
        except Exception as e:
            return log_and_format_error("get_chats", e)

    async def list_chats(
        self,
        limit: int = 50,
        chat_type: Optional[str] = None,
        archived: bool = False,
        unread_only: bool = False,
    ) -> str:
        """Get a filtered list of chats with metadata."""
        try:
            await self._wait_for_rate_limit()
            dialogs = await self.client.get_dialogs(limit=limit, archived=archived)

            results = []
            for dialog in dialogs:
                entity = dialog.entity

                if chat_type:
                    if chat_type == "user" and not isinstance(entity, User):
                        continue
                    elif chat_type == "group" and not isinstance(entity, Chat):
                        continue
                    elif chat_type == "channel" and not isinstance(entity, Channel):
                        continue

                if unread_only and dialog.unread_count == 0:
                    continue

                chat_info = format_entity(entity)
                chat_info["unread_count"] = dialog.unread_count
                chat_info["is_pinned"] = dialog.pinned
                # Read receipts (raw messages.Dialog fields).
                # read_outbox_max_id: peer прочитал наши исходящие <= этому id.
                # read_inbox_max_id: мы прочитали входящие <= этому id.
                # top_message: id последнего сообщения в чате (для сравнения).
                raw = getattr(dialog, "dialog", None)
                chat_info["read_outbox_max_id"] = getattr(raw, "read_outbox_max_id", None)
                chat_info["read_inbox_max_id"] = getattr(raw, "read_inbox_max_id", None)
                chat_info["top_message_id"] = getattr(raw, "top_message", None)

                results.append(chat_info)

            return json.dumps(results, indent=2, default=json_serializer)
        except TelethonFloodWaitError as e:
            return log_and_format_error("list_chats", e, note=f"FloodWait: {e.seconds}s")
        except Exception as e:
            return log_and_format_error("list_chats", e, limit=limit, chat_type=chat_type)

    async def list_folders(self, include_chats: bool = True) -> str:
        """List Telegram folders (dialog filters) with optional chats inside.

        Returns array of folders. Each folder has id, title, type,
        pinned_count, include_count. If include_chats=True, also `chats` array
        (pinned first, then included; resolved via get_entity).
        """
        try:
            from telethon.tl.functions.messages import GetDialogFiltersRequest
            from telethon.tl.types import DialogFilter, DialogFilterChatlist

            await self._wait_for_rate_limit()
            result = await self.client(GetDialogFiltersRequest())
            raw_filters = getattr(result, "filters", result if isinstance(result, list) else [])

            folders = []
            for f in raw_filters:
                title_obj = getattr(f, "title", "")
                title = title_obj.text if hasattr(title_obj, "text") else str(title_obj)

                if not isinstance(f, (DialogFilter, DialogFilterChatlist)):
                    folders.append({"id": getattr(f, "id", 0), "title": title or "All chats", "type": "default"})
                    continue

                folder_info = {
                    "id": f.id,
                    "title": title,
                    "type": "chatlist" if isinstance(f, DialogFilterChatlist) else "filter",
                    "color": getattr(f, "color", None),
                    "emoticon": getattr(f, "emoticon", None),
                    "pinned_count": len(getattr(f, "pinned_peers", []) or []),
                    "include_count": len(getattr(f, "include_peers", []) or []),
                    "exclude_count": len(getattr(f, "exclude_peers", []) or []),
                    "pinned_peer_ids": [_input_peer_key(p) for p in (getattr(f, "pinned_peers", []) or [])],
                    "include_peer_ids": [_input_peer_key(p) for p in (getattr(f, "include_peers", []) or [])],
                }

                if include_chats:
                    chats = []
                    peers = list(getattr(f, "pinned_peers", []) or []) + list(getattr(f, "include_peers", []) or [])
                    for peer in peers:
                        try:
                            await self._wait_for_rate_limit()
                            entity = await self.client.get_entity(peer)
                            chats.append(format_entity(entity))
                        except Exception:
                            continue
                    folder_info["chats"] = chats

                folders.append(folder_info)

            return json.dumps(folders, indent=2, default=json_serializer, ensure_ascii=False)
        except TelethonFloodWaitError as e:
            return log_and_format_error("list_folders", e, note=f"FloodWait: {e.seconds}s")
        except Exception as e:
            return log_and_format_error("list_folders", e)

    async def _resolve_input_peer(self, chat_id: Union[int, str], warmed: List[bool]) -> Any:
        """Resolve chat_id to an InputPeer; warm the entity cache once via get_dialogs on miss."""
        await self._wait_for_rate_limit()
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            if warmed[0]:
                raise
        # "Could not find the input entity": load dialogs once per request, then retry.
        warmed[0] = True
        await self._warm_entity_cache()
        await self._wait_for_rate_limit()
        return await self.client.get_input_entity(chat_id)

    async def _warm_entity_cache(self, page_size: int = 100, max_pages: int = 50) -> None:
        """Load dialogs page by page (one GetDialogs RPC per limiter permit) to fill the entity cache."""
        offset_date, offset_id, offset_peer = None, 0, None
        for _ in range(max_pages):
            await self._wait_for_rate_limit()
            kwargs: Dict[str, Any] = {"limit": page_size, "offset_date": offset_date, "offset_id": offset_id}
            if offset_peer is not None:
                kwargs["offset_peer"] = offset_peer
            page = await self.client.get_dialogs(**kwargs)
            if not page or len(page) < page_size:
                return
            last = page[-1]
            msg = getattr(last, "message", None)
            if msg is None:
                return
            offset_date, offset_id, offset_peer = msg.date, msg.id, last.input_entity

    async def upsert_folder(
        self,
        title: str,
        include_chat_ids: List[Union[int, str]],
        mode: str = "replace",
        color: Optional[int] = None,
        set_color: bool = False,
        emoticon: Optional[str] = None,
        set_emoticon: bool = False,
    ) -> Dict[str, Any]:
        """Create or update ONE folder found by exact title; only its peers/color/emoticon change.

        Other folders are never read-modified or deleted. Raises FolderError(status, msg):
        422 validation/limits/unresolved peers, 409 title is a shared chatlist or no free id,
        429 FloodWait, 502 upstream failure.
        """
        title = title or ""
        if not title.strip():
            raise FolderError(422, "title is required")
        if title != title.strip():
            raise FolderError(422, "title must not have leading/trailing spaces")
        if len(title) > FOLDER_TITLE_MAX:
            raise FolderError(422, f"title longer than {FOLDER_TITLE_MAX} chars")
        if mode not in FOLDER_MODES:
            raise FolderError(422, f"mode must be one of {list(FOLDER_MODES)}")
        if set_color and color is not None and not (0 <= color <= 6):
            raise FolderError(422, "color must be 0..6 or null")
        validated, error = validate_ids("include_chat_ids", list(include_chat_ids or []))
        if error:
            raise FolderError(422, error)

        async with self._folder_lock:
            return await self._upsert_folder_locked(
                title, validated, mode, color, set_color, emoticon, set_emoticon
            )

    async def _upsert_folder_locked(
        self,
        title: str,
        validated: List[Union[int, str]],
        mode: str,
        color: Optional[int],
        set_color: bool,
        emoticon: Optional[str],
        set_emoticon: bool,
    ) -> Dict[str, Any]:
        """Body of upsert_folder; caller holds self._folder_lock."""
        from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
        from telethon.tl.types import DialogFilter, DialogFilterChatlist

        try:
            # Resolve requested peers first: nothing is written if any id is unknown.
            warmed = [False]
            requested: Dict[str, Any] = {}
            unresolved: List[str] = []
            for cid in validated:
                try:
                    peer = await self._resolve_input_peer(cid, warmed)
                except ValueError:
                    unresolved.append(str(cid))
                    continue
                requested.setdefault(_input_peer_key(peer), peer)
            if unresolved:
                raise FolderError(422, f"could not resolve chats: {', '.join(unresolved)}")

            await self._wait_for_rate_limit()
            result = await self.client(GetDialogFiltersRequest())
            raw_filters = list(getattr(result, "filters", result if isinstance(result, list) else []) or [])

            existing = None
            used_ids = set()
            for f in raw_filters:
                if hasattr(f, "id"):
                    used_ids.add(f.id)
                if isinstance(f, (DialogFilter, DialogFilterChatlist)) and _folder_title_text(f.title) == title:
                    existing = f
                    break
            if isinstance(existing, DialogFilterChatlist):
                raise FolderError(409, f"folder '{title}' is a shared chat list; not modified")

            if existing is None:
                if mode == "remove":
                    raise FolderError(422, f"folder '{title}' does not exist")
                free = [i for i in range(FOLDER_ID_MIN, FOLDER_ID_MAX + 1) if i not in used_ids]
                if not free:
                    raise FolderError(409, "no free folder id")
                folder_id = free[0]
                pinned: Dict[str, Any] = {}
                include: Dict[str, Any] = {}
            else:
                folder_id = existing.id
                pinned = {_input_peer_key(p): p for p in (existing.pinned_peers or [])}
                include = {_input_peer_key(p): p for p in (existing.include_peers or [])}

            before = set(pinned) | set(include)
            if mode == "replace":
                pinned = {k: v for k, v in pinned.items() if k in requested}
                include = {k: v for k, v in requested.items() if k not in pinned}
            elif mode == "add":
                for k, v in requested.items():
                    if k not in pinned and k not in include:
                        include[k] = v
            else:  # remove
                pinned = {k: v for k, v in pinned.items() if k not in requested}
                include = {k: v for k, v in include.items() if k not in requested}
            after = set(pinned) | set(include)

            if len(after) > FOLDER_MAX_PEERS:
                raise FolderError(422, f"folder would hold {len(after)} chats; limit is {FOLDER_MAX_PEERS}")
            has_categories = existing is not None and any(
                getattr(existing, flag, False) for flag in ("contacts", "non_contacts", "groups", "broadcasts", "bots")
            )
            if not after and not has_categories:
                raise FolderError(422, "folder would be empty; Telegram rejects empty folders (no DELETE here)")

            if existing is not None:
                # Copy every constructor field of the existing filter, then override ours.
                import inspect
                params = [p for p in inspect.signature(DialogFilter.__init__).parameters if p != "self"]
                kwargs = {p: getattr(existing, p, None) for p in params}
                kwargs["exclude_peers"] = list(existing.exclude_peers or [])
            else:
                kwargs = {"id": folder_id, "title": _make_folder_title(title), "exclude_peers": []}
            kwargs["pinned_peers"] = list(pinned.values())
            kwargs["include_peers"] = list(include.values())
            if set_color:
                kwargs["color"] = color
            if set_emoticon:
                kwargs["emoticon"] = emoticon or None
            new_filter = DialogFilter(**kwargs)

            changed = (
                existing is None
                or before != after
                or (set_color and getattr(existing, "color", None) != color)
                or (set_emoticon and getattr(existing, "emoticon", None) != (emoticon or None))
            )
            if changed:
                await self._wait_for_rate_limit()
                await self.client(UpdateDialogFilterRequest(id=folder_id, filter=new_filter))

            return {
                "action": "created" if existing is None else ("updated" if changed else "unchanged"),
                "id": folder_id,
                "title": title,
                "mode": mode,
                "color": new_filter.color if hasattr(new_filter, "color") else None,
                "emoticon": getattr(new_filter, "emoticon", None),
                "pinned_count": len(pinned),
                "include_count": len(include),
                "added": sorted(after - before),
                "removed": sorted(before - after),
                "include_peer_ids": list(include.keys()),
            }
        except FolderError:
            raise
        except TelethonFloodWaitError as e:
            raise FolderError(429, f"Telegram FloodWait: retry after {e.seconds}s")
        except Exception as e:
            raise FolderError(502, log_and_format_error("upsert_folder", e, title=title, mode=mode))

    async def get_chat(self, chat_id: Union[int, str]) -> str:
        """Get detailed information about a specific chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            await self._wait_for_rate_limit()
            entity = await self.client.get_entity(chat_id)
            info = format_entity(entity)

            if isinstance(entity, (Chat, Channel)):
                full_chat = await self.client(functions.channels.GetFullChannelRequest(entity))
                if hasattr(full_chat, "full_chat"):
                    info["about"] = getattr(full_chat.full_chat, "about", None)
                    info["participants_count"] = getattr(
                        full_chat.full_chat, "participants_count", None
                    )

            return json.dumps(info, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("get_chat", e, chat_id=chat_id)

    async def get_read_status(self, chat_id: Union[int, str]) -> str:
        """Read receipt status for a single chat.

        Returns JSON with read_outbox_max_id (peer прочитал наши исходящие <= этому id),
        read_inbox_max_id (мы прочитали входящие <= этому id), top_message_id, unread_count.
        Используется чтобы понять прочитал ли собеседник конкретное сообщение:
        sent_message_id <= read_outbox_max_id ⇒ прочитано.
        """
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            await self._wait_for_rate_limit()
            entity = await self.client.get_input_entity(chat_id)
            peer_dialogs = await self.client(
                functions.messages.GetPeerDialogsRequest(peers=[entity])
            )
            if not peer_dialogs.dialogs:
                return json.dumps(
                    {"error": "dialog_not_found", "chat_id": chat_id},
                    default=json_serializer,
                )
            raw = peer_dialogs.dialogs[0]
            return json.dumps(
                {
                    "chat_id": chat_id,
                    "read_outbox_max_id": getattr(raw, "read_outbox_max_id", None),
                    "read_inbox_max_id": getattr(raw, "read_inbox_max_id", None),
                    "top_message_id": getattr(raw, "top_message", None),
                    "unread_count": getattr(raw, "unread_count", None),
                },
                default=json_serializer,
            )
        except TelethonFloodWaitError as e:
            return log_and_format_error("get_read_status", e, note=f"FloodWait: {e.seconds}s")
        except Exception as e:
            return log_and_format_error("get_read_status", e, chat_id=chat_id)

    # ==================== Message Operations ====================

    async def get_message(
        self, chat_id: Union[int, str], message_id: int
    ) -> str:
        """Get a single message by ID from a specific chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            await self._wait_for_rate_limit()
            try:
                entity = await self.client.get_entity(chat_id)
            except TelethonFloodWaitError as e:
                wait_time = min(float(getattr(e, 'seconds', 0)), 3600.0)
                if wait_time > 0:
                    await asyncio.sleep(wait_time + random.uniform(0, 1))
                    entity = await self.client.get_entity(chat_id)
                else:
                    raise
            
            try:
                message = await self.client.get_messages(entity, ids=message_id)
            except TelethonFloodWaitError as e:
                wait_time = min(float(getattr(e, 'seconds', 0)), 3600.0)
                if wait_time > 0:
                    await asyncio.sleep(wait_time + random.uniform(0, 1))
                    message = await self.client.get_messages(entity, ids=message_id)
                else:
                    raise
            
            if not message:
                return "Message not found"
            
            # Форматируем сообщение
            sender_name = get_sender_name(message)
            
            result = {
                "id": message.id,
                "sender": sender_name,
                # Additive (2026-11-01): direction and author id from Telegram itself.
                "out": bool(getattr(message, "out", False)),
                "sender_id": getattr(message, "sender_id", None),
                "date": message.date.isoformat(),
                "text": message.message or "",
                "reply_to": message.reply_to.reply_to_msg_id if message.reply_to else None,
                "has_media": bool(message.media),
                "media_type": type(message.media).__name__ if message.media else None,
            }
            # Additive media metadata (2026-10-16): None when not applicable.
            media_desc = describe_media(message.media)
            result["media_kind"] = media_desc.kind if media_desc else None
            result["media_marker"] = media_marker(media_desc) or None
            result["file_name"] = media_desc.file_name if media_desc else None
            result["mime_type"] = media_desc.mime_type if media_desc else None
            result["size"] = media_desc.size if media_desc else None
            result["duration"] = media_desc.duration if media_desc else None
            if message.media and type(message.media).__name__ == "MessageMediaPoll":
                poll_payload = {}
                poll = getattr(message.media, "poll", None)
                results = getattr(message.media, "results", None)
                if poll is not None:
                    question = getattr(poll, "question", None)
                    q_text = getattr(question, "text", None) if question is not None else None
                    poll_payload["question"] = q_text or (question if isinstance(question, str) else "")
                    poll_payload["closed"] = bool(getattr(poll, "closed", False))
                    poll_payload["public_voters"] = bool(getattr(poll, "public_voters", False))
                    poll_payload["multiple_choice"] = bool(getattr(poll, "multiple_choice", False))
                    answers = []
                    for ans in getattr(poll, "answers", []) or []:
                        text_obj = getattr(ans, "text", None)
                        a_text = getattr(text_obj, "text", None) if text_obj is not None else None
                        answers.append({"text": a_text or (text_obj if isinstance(text_obj, str) else "")})
                    poll_payload["answers"] = answers
                if results is not None:
                    poll_payload["total_voters"] = getattr(results, "total_voters", None)
                    res_list = []
                    for r in getattr(results, "results", []) or []:
                        res_list.append({
                            "voters": getattr(r, "voters", 0),
                            "chosen": bool(getattr(r, "chosen", False)),
                        })
                    poll_payload["results"] = res_list
                if poll is not None and not poll_payload.get("results"):
                    try:
                        from telethon.tl.functions.messages import GetPollResultsRequest
                        upd = await self.client(GetPollResultsRequest(peer=entity, msg_id=message_id, poll_hash=getattr(poll, "id", 0)))
                        for u in getattr(upd, "updates", []) or []:
                            r2 = getattr(u, "results", None)
                            if r2 is None:
                                continue
                            r2_inner = getattr(r2, "results", None)
                            if not r2_inner:
                                continue
                            res_list = []
                            for r in r2_inner:
                                res_list.append({
                                    "voters": getattr(r, "voters", 0),
                                    "chosen": bool(getattr(r, "chosen", False)),
                                })
                            poll_payload["results"] = res_list
                            tv = getattr(r2, "total_voters", None)
                            if tv is not None:
                                poll_payload["total_voters"] = tv
                            break
                    except Exception as poll_err:
                        poll_payload["results_error"] = str(poll_err)
                if poll_payload:
                    result["poll"] = poll_payload
            # Extract entities with URLs (TextUrl type)
            if message.entities:
                entities_list = []
                text = message.message or ""
                for entity in message.entities:
                    entity_info = {
                        "type": type(entity).__name__,
                        "offset": entity.offset,
                        "length": entity.length,
                        "text": text[entity.offset:entity.offset + entity.length] if text else "",
                    }
                    if hasattr(entity, "url") and entity.url:
                        entity_info["url"] = entity.url
                    if hasattr(entity, "user_id") and entity.user_id:
                        entity_info["user_id"] = entity.user_id
                    entities_list.append(entity_info)
                if entities_list:
                    result["entities"] = entities_list
            # Возвращаем JSON строку, make_response обернёт в success/error
            return json.dumps(result, indent=2, default=json_serializer)
            
        except Exception as e:
            return log_and_format_error("get_message", e, chat_id=chat_id, message_id=message_id)

    async def get_messages(
        self, chat_id: Union[int, str], page: int = 1, page_size: int = 20
    ) -> str:
        """Get paginated messages from a specific chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            # Rate limiting protection for read operations
            await self._wait_for_rate_limit()
            try:
                entity = await self.client.get_entity(chat_id)
            except TelethonFloodWaitError as e:
                wait_time = min(float(getattr(e, 'seconds', 0)), 3600.0)
                if wait_time > 0:
                    await asyncio.sleep(wait_time + random.uniform(0, 1))
                    entity = await self.client.get_entity(chat_id)
                else:
                    raise
            
            await self._wait_for_rate_limit()
            offset = (page - 1) * page_size
            try:
                messages = await self.client.get_messages(entity, limit=page_size, add_offset=offset)
            except TelethonFloodWaitError as e:
                wait_time = min(float(getattr(e, 'seconds', 0)), 3600.0)
                if wait_time > 0:
                    await asyncio.sleep(wait_time + random.uniform(0, 1))
                    messages = await self.client.get_messages(entity, limit=page_size, add_offset=offset)
                else:
                    raise
            
            if not messages:
                return "No messages found for this page."
            lines = []
            for msg in messages:
                sender_name = get_sender_name(msg)
                reply_info = ""
                if msg.reply_to and msg.reply_to.reply_to_msg_id:
                    reply_info = f" | reply to {msg.reply_to.reply_to_msg_id}"
                engagement_info = get_engagement_info(msg)
                lines.append(
                    f"ID: {msg.id} | {sender_name} | Date: {msg.date}{reply_info}{engagement_info} | Message: {message_text_with_marker(msg.message, msg.media)}"
                )
            return "\n".join(lines)
        except Exception as e:
            return log_and_format_error("get_messages", e, chat_id=chat_id, page=page)

    async def _ensure_may_write(self, entity: Any) -> None:
        """Never write first: a private chat needs at least one incoming message from the peer.

        Groups/channels and Saved Messages pass. Raises SendRefusedError otherwise.
        """
        if not isinstance(entity, User) or getattr(entity, "is_self", False):
            return
        # Page the history ourselves: Telethon filters `from_user` locally in private chats,
        # so a small limit can end the search on our own outgoing messages (false refusal).
        offset_id = 0
        for _ in range(FIRST_WRITE_MAX_PAGES):
            await self._wait_for_rate_limit()
            page = await self.client.get_messages(entity, limit=FIRST_WRITE_PAGE_SIZE, offset_id=offset_id)
            page = list(page or [])
            if any(getattr(m, "out", True) is False for m in page):
                await self._wait_for_rate_limit()  # space the guard read from the send itself
                return
            if len(page) < FIRST_WRITE_PAGE_SIZE:
                break
            offset_id = min(m.id for m in page)
        # End of history (or page cap) with no incoming message seen.
        logger.warning("send refused: contact %s never wrote first", getattr(entity, "id", "?"))
        raise SendRefusedError()

    async def send_message(
        self,
        chat_id: Union[int, str],
        message: str,
        reply_to: Optional[int] = None,
        parse_mode: Optional[str] = None,
    ) -> str:
        """Send a message to a chat with rate limiting protection."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        # Проверка лимита сообщений для конкретного чата
        await self._check_message_rate_limit(chat_id)
        
        # Ожидание перед запросом для соблюдения общего rate limit
        await self._wait_for_rate_limit()

        try:
            entity = await self.client.get_entity(chat_id)
            await self._ensure_may_write(entity)
            result = await self.client.send_message(
                entity, message, reply_to=reply_to, parse_mode=parse_mode
            )
            return f"Message sent successfully. Message ID: {result.id}"
        except SendRefusedError:
            raise
        except TelethonFloodWaitError as e:
            # Обработка FLOOD_WAIT от Telethon
            wait_time = min(float(e.seconds), 3600.0)  # Максимум 1 час
            await asyncio.sleep(wait_time + random.uniform(0, 1))
            # Повторная попытка после ожидания
            try:
                entity = await self.client.get_entity(chat_id)
                await self._ensure_may_write(entity)
                result = await self.client.send_message(
                    entity, message, reply_to=reply_to, parse_mode=parse_mode
                )
                return f"Message sent successfully. Message ID: {result.id}"
            except SendRefusedError:
                raise
            except Exception as retry_e:
                return log_and_format_error("send_message", retry_e, chat_id=chat_id)
        except Exception as e:
            return log_and_format_error("send_message", e, chat_id=chat_id)

    async def send_file(
        self,
        chat_id: Union[int, str],
        file_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[int] = None,
        parse_mode: Optional[str] = None,
    ) -> str:
        """Send a file/photo to a chat with rate limiting protection."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        import os
        if not os.path.exists(file_path):
            return f"Error: file not found: {file_path}"

        await self._check_message_rate_limit(chat_id)
        await self._wait_for_rate_limit()

        try:
            entity = await self.client.get_entity(chat_id)
            await self._ensure_may_write(entity)
            result = await self.client.send_file(
                entity,
                file_path,
                caption=caption,
                reply_to=reply_to,
                parse_mode=parse_mode,
            )
            return f"File sent successfully. Message ID: {result.id}"
        except SendRefusedError:
            raise
        except TelethonFloodWaitError as e:
            wait_time = min(float(e.seconds), 3600.0)
            await asyncio.sleep(wait_time + random.uniform(0, 1))
            try:
                entity = await self.client.get_entity(chat_id)
                await self._ensure_may_write(entity)
                result = await self.client.send_file(
                    entity,
                    file_path,
                    caption=caption,
                    reply_to=reply_to,
                    parse_mode=parse_mode,
                )
                return f"File sent successfully. Message ID: {result.id}"
            except SendRefusedError:
                raise
            except Exception as retry_e:
                return log_and_format_error("send_file", retry_e, chat_id=chat_id)
        except Exception as e:
            return log_and_format_error("send_file", e, chat_id=chat_id)

    async def edit_message(
        self, chat_id: Union[int, str], message_id: int, new_text: str
    ) -> str:
        """Edit a message with rate limiting protection."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        # Проверка лимита редактирования
        await self._check_edit_rate_limit()
        
        # Ожидание перед запросом для соблюдения общего rate limit
        await self._wait_for_rate_limit()

        try:
            entity = await self.client.get_entity(chat_id)
            await self.client.edit_message(entity, message_id, new_text)
            return f"Message {message_id} edited successfully."
        except TelethonFloodWaitError as e:
            # Обработка FLOOD_WAIT от Telethon
            wait_time = min(float(e.seconds), 3600.0)  # Максимум 1 час
            await asyncio.sleep(wait_time + random.uniform(0, 1))
            # Повторная попытка после ожидания
            try:
                entity = await self.client.get_entity(chat_id)
                await self.client.edit_message(entity, message_id, new_text)
                return f"Message {message_id} edited successfully."
            except Exception as retry_e:
                return log_and_format_error("edit_message", retry_e, chat_id=chat_id, message_id=message_id)
        except Exception as e:
            return log_and_format_error("edit_message", e, chat_id=chat_id, message_id=message_id)

    async def delete_message(
        self, chat_id: Union[int, str], message_id: int, revoke: bool = True
    ) -> str:
        """Delete a message."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            await self.client.delete_messages(entity, message_id, revoke=revoke)
            return f"Message {message_id} deleted successfully."
        except Exception as e:
            return log_and_format_error("delete_message", e, chat_id=chat_id, message_id=message_id)

    async def forward_message(
        self, from_chat_id: Union[int, str], to_chat_id: Union[int, str], message_id: int
    ) -> str:
        """Forward a message from one chat to another with rate limiting protection."""
        from_chat_id, error = validate_ids("from_chat_id", from_chat_id)
        if error:
            return error
        to_chat_id, error = validate_ids("to_chat_id", to_chat_id)
        if error:
            return error

        # Проверка лимита сообщений для целевого чата
        await self._check_message_rate_limit(to_chat_id)
        
        # Ожидание перед запросом для соблюдения общего rate limit
        await self._wait_for_rate_limit()

        try:
            from_entity = await self.client.get_entity(from_chat_id)
            to_entity = await self.client.get_entity(to_chat_id)
            await self._ensure_may_write(to_entity)
            result = await self.client.forward_messages(to_entity, message_id, from_entity)
            return f"Message forwarded successfully. New message ID: {result[0].id}"
        except SendRefusedError:
            raise
        except TelethonFloodWaitError as e:
            # Обработка FLOOD_WAIT от Telethon
            wait_time = min(float(e.seconds), 3600.0)  # Максимум 1 час
            await asyncio.sleep(wait_time + random.uniform(0, 1))
            # Повторная попытка после ожидания
            try:
                from_entity = await self.client.get_entity(from_chat_id)
                to_entity = await self.client.get_entity(to_chat_id)
                await self._ensure_may_write(to_entity)
                result = await self.client.forward_messages(to_entity, message_id, from_entity)
                return f"Message forwarded successfully. New message ID: {result[0].id}"
            except SendRefusedError:
                raise
            except Exception as retry_e:
                return log_and_format_error(
                    "forward_message", retry_e, from_chat_id=from_chat_id, to_chat_id=to_chat_id
                )
        except Exception as e:
            return log_and_format_error(
                "forward_message", e, from_chat_id=from_chat_id, to_chat_id=to_chat_id
            )

    async def search_messages(
        self,
        chat_id: Union[int, str],
        query: str,
        limit: int = 20,
        from_user: Optional[Union[int, str]] = None,
    ) -> str:
        """Search for messages in a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            from_entity = None
            if from_user:
                from_user, error = validate_ids("from_user", from_user)
                if error:
                    return error
                from_entity = await self.client.get_entity(from_user)

            messages = await self.client.get_messages(
                entity, limit=limit, search=query, from_user=from_entity
            )

            if not messages:
                return "No messages found matching the search."

            results = []
            for msg in messages:
                results.append(format_message(msg))

            return json.dumps(results, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("search_messages", e, chat_id=chat_id, query=query)

    # ==================== Contact Operations ====================

    async def list_contacts(self) -> str:
        """Get all contacts."""
        try:
            result = await self.client(functions.contacts.GetContactsRequest(hash=0))
            contacts = []
            for user in result.users:
                contacts.append(format_entity(user))
            return json.dumps(contacts, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("list_contacts", e)

    async def search_contacts(self, query: str, limit: int = 10) -> str:
        """Search contacts by name or username."""
        try:
            result = await self.client(functions.contacts.SearchRequest(q=query, limit=limit))
            contacts = []
            for user in result.users:
                contacts.append(format_entity(user))
            return json.dumps(contacts, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("search_contacts", e, query=query)

    async def add_contact(
        self,
        phone: str,
        first_name: str,
        last_name: Optional[str] = None,
    ) -> str:
        """Add a new contact."""
        try:
            result = await self.client(
                functions.contacts.ImportContactsRequest(
                    contacts=[
                        functions.InputPhoneContact(
                            client_id=0,
                            phone=phone,
                            first_name=first_name,
                            last_name=last_name or "",
                        )
                    ]
                )
            )
            if result.users:
                return f"Contact added successfully: {format_entity(result.users[0])}"
            return "Contact added but user not found on Telegram."
        except Exception as e:
            return log_and_format_error("add_contact", e, phone=phone)

    async def delete_contact(self, user_id: Union[int, str]) -> str:
        """Delete a contact."""
        user_id, error = validate_ids("user_id", user_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(user_id)
            await self.client(functions.contacts.DeleteContactsRequest(id=[entity]))
            return f"Contact {user_id} deleted successfully."
        except Exception as e:
            return log_and_format_error("delete_contact", e, user_id=user_id)

    # ==================== User & Profile Operations ====================

    async def get_me(self) -> str:
        """Get information about the current user."""
        try:
            me = await self.client.get_me()
            info = format_entity(me)
            return json.dumps(info, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("get_me", e)

    async def get_user_status(self, user_id: Union[int, str]) -> str:
        """Get the online status of a user."""
        user_id, error = validate_ids("user_id", user_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(user_id)
            if not isinstance(entity, User):
                return "Entity is not a user."

            status = entity.status
            status_info = {
                "user_id": entity.id,
                "status_type": type(status).__name__ if status else "Unknown",
            }

            if hasattr(status, "was_online"):
                status_info["was_online"] = status.was_online.isoformat()

            return json.dumps(status_info, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("get_user_status", e, user_id=user_id)

    # ==================== Group Operations ====================

    async def create_group(self, title: str, users: List[Union[int, str]]) -> str:
        """Create a new group chat."""
        users, error = validate_ids("users", users)
        if error:
            return error

        try:
            user_entities = []
            for user_id in users:
                entity = await self.client.get_entity(user_id)
                user_entities.append(entity)

            result = await self.client(
                functions.messages.CreateChatRequest(title=title, users=user_entities)
            )
        except Exception as e:
            return log_and_format_error("create_group", e, title=title)

        # The chat exists from here on: never report a failure that would invite a retry
        # (retries created duplicates when result.chats was missing on InvitedUsers).
        chats = _chats_from_result(result)
        if not chats:
            logger.error("create_group: chat created but no chats in %s", type(result).__name__)
            return (
                f"Group '{title}' created, but the chat id could not be read from the response. "
                "Do NOT retry – look it up via /chats/list."
            )
        chat_id = chats[0].id
        message = f"Group '{title}' created successfully. Chat ID: {chat_id}"
        missing = [m.user_id for m in (getattr(result, "missing_invitees", None) or [])]
        if missing:
            message += f" Not invited (privacy settings): {missing}"
        return message

    async def create_supergroup(
        self,
        title: str,
        about: Optional[str] = None,
        users: Optional[List[Union[int, str]]] = None,
    ) -> str:
        """Create a megagroup, invite users one by one and make history visible to new members.

        The create call is made exactly once (no retry, FloodWait surfaced instead of slept on),
        so a failure never leaves duplicate chats. Everything after a successful create is
        best-effort and reported in the JSON result instead of failing the call.
        """
        users, error = validate_ids("users", list(users or []))
        if error:
            return f"Error: {error}"
        title = (title or "").strip()
        if not title:
            return "Error: title must not be empty"

        try:
            result = await self._strict_call(
                functions.channels.CreateChannelRequest(
                    title=title, about=about or "", megagroup=True
                ),
                single_attempt=True,
            )
        except TelethonFloodWaitError as e:
            return log_and_format_error(
                "create_supergroup", e, title=title,
                user_message=f"Error: Telegram FloodWait {e.seconds}s – nothing was created, retry after the wait",
            )
        except Exception as e:
            if _is_definite_rejection(e):
                return log_and_format_error(
                    "create_supergroup", e, title=title,
                    user_message=f"Error: Telegram rejected the create ({type(e).__name__}) – nothing was created",
                )
            # Timeout, connection loss, Telegram internal error: the chat may exist.
            return log_and_format_error(
                "create_supergroup", e, title=title,
                user_message=(
                    f"Error: outcome unknown ({type(e).__name__}) – do NOT retry; "
                    f"check /chats/list for the title '{title}'"
                ),
            )

        # The RPC returned: the supergroup exists. Nothing below may turn into a failure.
        try:
            return await self._finish_supergroup(result, title, users)
        except Exception as e:
            log_and_format_error("create_supergroup.finish", e, title=title)
            channel = next((c for c in _chats_from_result(result) if isinstance(c, Channel)), None)
            return json.dumps(
                {
                    "created": True,
                    "chat_id": utils.get_peer_id(channel) if channel is not None else None,
                    "title": title,
                    "warning": f"created, but post-create steps failed ({type(e).__name__}) – "
                               "do NOT retry; finish invites/history via their endpoints",
                },
                ensure_ascii=False,
            )

    async def _finish_supergroup(self, result: Any, title: str, users: List[Union[int, str]]) -> str:
        """Post-create steps of create_supergroup: invites + history visibility, as JSON."""
        channel = next((c for c in _chats_from_result(result) if isinstance(c, Channel)), None)
        if channel is None:
            logger.error("create_supergroup: created but no Channel in %s", type(result).__name__)
            return json.dumps(
                {
                    "created": True,
                    "chat_id": None,
                    "title": title,
                    "warning": "supergroup created but its id is missing in the response – "
                               "do NOT retry, find it via /chats/list",
                },
                ensure_ascii=False,
            )

        invites: List[Dict[str, Any]] = []
        flood_hit: Optional[int] = None
        for uid in users:
            if flood_hit is not None:
                invites.append({"user": uid, "status": "skipped", "error": f"FloodWait {flood_hit}s earlier"})
                continue
            invites.append(await self._invite_one(channel, uid))
            if invites[-1].get("flood_wait") is not None:
                flood_hit = invites[-1].pop("flood_wait")

        try:
            changed = await self._toggle_prehistory(channel, visible=True)
            history: Dict[str, Any] = {"history_visible": True, "changed": changed}
        except TelethonFloodWaitError as e:
            history = {"history_visible": None, "error": f"FloodWait {e.seconds}s"}
        except Exception as e:
            log_and_format_error("create_supergroup.history", e, title=title)
            history = {"history_visible": None, "error": f"{type(e).__name__}: {e}"}

        return json.dumps(
            {
                "created": True,
                "chat_id": utils.get_peer_id(channel),
                "title": title,
                "invites": invites,
                "history": history,
            },
            ensure_ascii=False,
        )

    async def _invite_one(self, channel: Any, uid: Union[int, str]) -> Dict[str, Any]:
        """Invite one user to a channel; never raises. Result carries status and reason."""
        entry: Dict[str, Any] = {"user": uid}
        try:
            await self._wait_for_rate_limit()
            user_entity = await self.client.get_entity(uid)
            res = await self._strict_call(
                functions.channels.InviteToChannelRequest(channel=channel, users=[user_entity])
            )
            missing = getattr(res, "missing_invitees", None) or []
            if missing:
                entry["status"] = "not_invited"
                entry["error"] = "privacy settings forbid inviting this user"
                entry["premium_would_allow_invite"] = bool(getattr(missing[0], "premium_would_allow_invite", False))
            else:
                entry["status"] = "invited"
        except TelethonFloodWaitError as e:
            entry["status"] = "error"
            entry["error"] = f"FloodWait {e.seconds}s"
            entry["flood_wait"] = e.seconds
        except Exception as e:
            log_and_format_error("create_supergroup.invite", e, user=uid)
            entry["status"] = "error"
            entry["error"] = f"{type(e).__name__}: {e}"
        return entry

    async def _toggle_prehistory(self, channel: Any, visible: bool) -> bool:
        """Set 'history visible to new members'. Returns False if it already had that value."""
        try:
            await self._strict_call(
                functions.channels.TogglePreHistoryHiddenRequest(channel=channel, enabled=not visible)
            )
        except ChatNotModifiedError:
            return False
        return True

    async def set_history_visible(self, chat_id: Union[int, str], visible: bool = True) -> str:
        """Toggle chat history visibility for new members (supergroups/channels only)."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return f"Error: {error}"

        try:
            await self._wait_for_rate_limit()
            entity = await self.client.get_entity(chat_id)
        except TelethonFloodWaitError as e:
            return f"Error: Telegram FloodWait {e.seconds}s, retry later"
        except Exception as e:
            return log_and_format_error("set_history_visible", e, chat_id=chat_id)

        if isinstance(entity, (Chat, ChatForbidden)):
            return (
                f"Error: chat {chat_id} is a basic group – history visibility for new members "
                "exists only in supergroups. Create the chat via POST /supergroups instead."
            )
        if not isinstance(entity, Channel):
            return f"Error: chat {chat_id} is not a supergroup or channel"

        try:
            changed = await self._toggle_prehistory(entity, visible)
        except TelethonFloodWaitError as e:
            return f"Error: Telegram FloodWait {e.seconds}s, retry later"
        except Exception as e:
            return log_and_format_error("set_history_visible", e, chat_id=chat_id)

        return json.dumps(
            {"chat_id": utils.get_peer_id(entity), "history_visible": visible, "changed": changed}
        )

    async def invite_to_group(
        self, chat_id: Union[int, str], user_ids: List[Union[int, str]]
    ) -> str:
        """Invite users to a group or channel."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error
        user_ids, error = validate_ids("user_ids", user_ids)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            user_entities = []
            for user_id in user_ids:
                user_entity = await self.client.get_entity(user_id)
                user_entities.append(user_entity)

            if isinstance(entity, Channel):
                await self.client(
                    functions.channels.InviteToChannelRequest(channel=entity, users=user_entities)
                )
            else:
                for user in user_entities:
                    await self.client(
                        functions.messages.AddChatUserRequest(
                            chat_id=entity.id, user_id=user, fwd_limit=50
                        )
                    )

            return f"Users invited to chat {chat_id} successfully."
        except Exception as e:
            return log_and_format_error("invite_to_group", e, chat_id=chat_id)

    async def leave_chat(self, chat_id: Union[int, str]) -> str:
        """Leave a group or channel."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            if isinstance(entity, Channel):
                await self.client(functions.channels.LeaveChannelRequest(channel=entity))
            else:
                await self.client(functions.messages.DeleteChatUserRequest(
                    chat_id=entity.id, user_id="me"
                ))
            return f"Left chat {chat_id} successfully."
        except Exception as e:
            return log_and_format_error("leave_chat", e, chat_id=chat_id)

    async def get_participants(
        self, chat_id: Union[int, str], limit: int = 100, offset: int = 0
    ) -> str:
        """Get participants of a group or channel."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            # Telethon 1.43 has no offset kwarg: skip the first `offset` items manually
            participants = list(await self.client.get_participants(entity, limit=limit + offset))[offset:]

            results = []
            for user in participants:
                results.append(format_entity(user))

            return json.dumps(results, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("get_participants", e, chat_id=chat_id)

    # ==================== Admin Operations ====================

    async def get_admins(self, chat_id: Union[int, str]) -> str:
        """Get administrators of a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            if isinstance(entity, Channel):
                participants = await self.client.get_participants(
                    entity, filter=ChannelParticipantsAdmins
                )
            else:
                full_chat = await self.client(functions.messages.GetFullChatRequest(entity.id))
                admin_ids = [
                    p.user_id for p in full_chat.full_chat.participants.participants
                    if hasattr(p, "admin_rights") or getattr(p, "is_admin", False)
                ]
                participants = [
                    user for user in full_chat.users if user.id in admin_ids
                ]

            results = []
            for user in participants:
                results.append(format_entity(user))

            return json.dumps(results, indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("get_admins", e, chat_id=chat_id)

    async def promote_admin(
        self,
        chat_id: Union[int, str],
        user_id: Union[int, str],
        title: Optional[str] = None,
    ) -> str:
        """Promote a user to admin."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error
        user_id, error = validate_ids("user_id", user_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            user = await self.client.get_entity(user_id)

            rights = ChatAdminRights(
                change_info=True,
                delete_messages=True,
                ban_users=True,
                invite_users=True,
                pin_messages=True,
                manage_call=True,
            )

            await self.client(
                functions.channels.EditAdminRequest(
                    channel=entity, user_id=user, admin_rights=rights, rank=title or ""
                )
            )

            return f"User {user_id} promoted to admin in chat {chat_id}."
        except Exception as e:
            return log_and_format_error("promote_admin", e, chat_id=chat_id, user_id=user_id)

    async def ban_user(
        self, chat_id: Union[int, str], user_id: Union[int, str], until_date: Optional[int] = None
    ) -> str:
        """Ban a user from a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error
        user_id, error = validate_ids("user_id", user_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            user = await self.client.get_entity(user_id)

            rights = ChatBannedRights(
                until_date=until_date,
                view_messages=True,
                send_messages=True,
                send_media=True,
                send_stickers=True,
                send_gifs=True,
            )

            await self.client(
                functions.channels.EditBannedRequest(channel=entity, participant=user, banned_rights=rights)
            )

            return f"User {user_id} banned from chat {chat_id}."
        except Exception as e:
            return log_and_format_error("ban_user", e, chat_id=chat_id, user_id=user_id)

    async def unban_user(self, chat_id: Union[int, str], user_id: Union[int, str]) -> str:
        """Unban a user from a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error
        user_id, error = validate_ids("user_id", user_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            user = await self.client.get_entity(user_id)

            rights = ChatBannedRights(until_date=None, view_messages=False)

            await self.client(
                functions.channels.EditBannedRequest(channel=entity, participant=user, banned_rights=rights)
            )

            return f"User {user_id} unbanned from chat {chat_id}."
        except Exception as e:
            return log_and_format_error("unban_user", e, chat_id=chat_id, user_id=user_id)

    # ==================== Channel Operations ====================

    async def get_invite_link(self, chat_id: Union[int, str]) -> str:
        """Get the invite link for a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)

            if isinstance(entity, Channel):
                result = await self.client(
                    functions.messages.ExportChatInviteRequest(peer=entity)
                )
                return f"Invite link: {result.link}"
            else:
                result = await self.client(
                    functions.messages.ExportChatInviteRequest(peer=entity)
                )
                return f"Invite link: {result.link}"
        except Exception as e:
            return log_and_format_error("get_invite_link", e, chat_id=chat_id)

    async def resolve_username(self, username: str) -> str:
        """Resolve a username to get entity information."""
        try:
            entity = await self.client.get_entity(username)
            return json.dumps(format_entity(entity), indent=2, default=json_serializer)
        except Exception as e:
            return log_and_format_error("resolve_username", e, username=username)

    # ==================== Notification Operations ====================

    async def mute_chat(self, chat_id: Union[int, str], mute_until: Optional[int] = None) -> str:
        """Mute notifications for a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            settings = functions.InputPeerNotifySettings(
                mute_until=mute_until or 2147483647  # Max int = forever
            )
            await self.client(
                functions.account.UpdateNotifySettingsRequest(
                    peer=functions.InputNotifyPeer(peer=entity), settings=settings
                )
            )
            return f"Chat {chat_id} muted successfully."
        except Exception as e:
            return log_and_format_error("mute_chat", e, chat_id=chat_id)

    async def unmute_chat(self, chat_id: Union[int, str]) -> str:
        """Unmute notifications for a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            settings = functions.InputPeerNotifySettings(mute_until=0)
            await self.client(
                functions.account.UpdateNotifySettingsRequest(
                    peer=functions.InputNotifyPeer(peer=entity), settings=settings
                )
            )
            return f"Chat {chat_id} unmuted successfully."
        except Exception as e:
            return log_and_format_error("unmute_chat", e, chat_id=chat_id)

    # ==================== Archive Operations ====================

    async def archive_chat(self, chat_id: Union[int, str]) -> str:
        """Archive a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            await self.client(
                functions.folders.EditPeerFoldersRequest(
                    folder_peers=[functions.InputFolderPeer(peer=entity, folder_id=1)]
                )
            )
            return f"Chat {chat_id} archived successfully."
        except Exception as e:
            return log_and_format_error("archive_chat", e, chat_id=chat_id)

    async def unarchive_chat(self, chat_id: Union[int, str]) -> str:
        """Unarchive a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            entity = await self.client.get_entity(chat_id)
            await self.client(
                functions.folders.EditPeerFoldersRequest(
                    folder_peers=[functions.InputFolderPeer(peer=entity, folder_id=0)]
                )
            )
            return f"Chat {chat_id} unarchived successfully."
        except Exception as e:
            return log_and_format_error("unarchive_chat", e, chat_id=chat_id)

    # ==================== Draft Operations ====================

    async def save_draft(
        self, chat_id: Union[int, str], message: str, reply_to: Optional[int] = None
    ) -> str:
        """Save a draft message to a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            peer = await self.client.get_input_entity(chat_id)
            await self.client(
                functions.messages.SaveDraftRequest(
                    peer=peer, message=message, reply_to=reply_to
                )
            )
            return f"Draft saved to chat {chat_id}."
        except Exception as e:
            return log_and_format_error("save_draft", e, chat_id=chat_id)

    async def clear_draft(self, chat_id: Union[int, str]) -> str:
        """Clear a draft from a chat."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            peer = await self.client.get_input_entity(chat_id)
            await self.client(functions.messages.SaveDraftRequest(peer=peer, message=""))
            return f"Draft cleared from chat {chat_id}."
        except Exception as e:
            return log_and_format_error("clear_draft", e, chat_id=chat_id)

    # ==================== Media Operations ====================

    async def _read_with_floodwait(self, factory):
        """Run a read call under the global rate limit; on FloodWait sleep (<=1h) and retry once."""
        await self._wait_for_rate_limit()
        try:
            return await factory()
        except TelethonFloodWaitError as e:
            wait_time = min(float(getattr(e, "seconds", 0)), 3600.0)
            if wait_time <= 0:
                raise
            await asyncio.sleep(wait_time + random.uniform(0, 1))
            await self._wait_for_rate_limit()
            return await factory()

    @asynccontextmanager
    async def _key_lock(self, key: Any):
        """Refcounted per-key asyncio lock; entry is dropped when nobody uses it."""
        entry = self._media_locks.get(key)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._media_locks[key] = entry
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._media_locks.get(key) is entry:
                del self._media_locks[key]

    async def _download_with_slot(self, message: Any, file_arg: str) -> Any:
        """Telegram download under the download semaphore + global rate limit.

        On FloodWait the semaphore slot is RELEASED while sleeping (<=1h), then
        re-acquired for a single retry; a second FloodWait propagates.

        Known limit: Telethon auto-sleeps FloodWaits <= client.flood_sleep_threshold
        (default 60 s) INSIDE download_media, and download_media has no per-call
        override (only client(request, flood_sleep_threshold=...) does). We do not
        change the client-wide threshold (other callers rely on it), so a slot may
        be held up to ~60 s by that internal sleep. Other downloads simply queue on
        the semaphore (no timeout, no failure) until a slot frees up.
        """
        for attempt in range(2):
            flood_wait = 0.0
            async with self._media_semaphore:
                await self._wait_for_rate_limit()
                try:
                    return await asyncio.wait_for(
                        self.client.download_media(message, file=file_arg),
                        timeout=MEDIA_DOWNLOAD_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    raise MediaFetchError(504, "Download timeout (5 minutes exceeded)")
                except TelethonFloodWaitError as e:
                    flood_wait = min(float(getattr(e, "seconds", 0)), 3600.0)
                    if flood_wait <= 0 or attempt == 1:
                        raise
            # Slot released: other downloads proceed while this one backs off.
            await asyncio.sleep(flood_wait + random.uniform(0, 1))
        raise MediaFetchError(502, "Failed to download")  # unreachable

    def _media_cache_paths(self, chat_id: Union[int, str], message_id: int) -> Dict[str, Path]:
        chat_dir = MEDIA_CACHE_DIR / _safe_component(chat_id)
        base = _safe_component(message_id)
        return {
            "dir": chat_dir,
            "meta": chat_dir / f"{base}.meta.json",
            "transcript": chat_dir / f"{base}.transcript.txt",
            "base": chat_dir / base,
        }

    def _read_cached_media(self, paths: Dict[str, Path]) -> Optional[Dict[str, Any]]:
        if not paths["meta"].is_file():
            return None
        try:
            meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
            cached_path = Path(meta.get("path", ""))
            if cached_path.is_file() and cached_path.stat().st_size == meta.get("size"):
                meta["cached"] = True
                return meta
        except (OSError, ValueError):
            pass  # corrupt sidecar -> re-download
        return None

    async def fetch_media(
        self,
        chat_id: Union[int, str],
        message_id: int,
        output_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Download message media (cached) and return metadata.

        Returns {success, path, file_name, mime_type, size, kind, duration, cached}.
        Raises MediaFetchError(status_code, message) on failure.
        """
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            raise MediaFetchError(400, error)

        paths = self._media_cache_paths(chat_id, message_id)

        # Cache hit outside the lock: no Telegram call at all.
        if not output_path:
            cached = self._read_cached_media(paths)
            if cached:
                return cached

        async with self._key_lock(("media", str(chat_id), message_id)):
            if not output_path:
                cached = self._read_cached_media(paths)  # another caller may have finished
                if cached:
                    return cached
            try:
                return await self._fetch_media_locked(chat_id, message_id, paths, output_path)
            except TelethonFloodWaitError as e:
                raise MediaFetchError(429, f"Telegram FloodWait {getattr(e, 'seconds', '?')}s, retry later")

    async def _fetch_media_locked(
        self,
        chat_id: Union[int, str],
        message_id: int,
        paths: Dict[str, Path],
        output_path: Optional[str],
    ) -> Dict[str, Any]:
        entity = await self._read_with_floodwait(lambda: self.client.get_entity(chat_id))
        message = await self._read_with_floodwait(
            lambda: self.client.get_messages(entity, ids=message_id)
        )
        if not message:
            raise MediaFetchError(404, "Message not found")
        if not message.media:
            raise MediaFetchError(404, "Message has no media")

        info = describe_media(message.media)
        if info is None or info.kind in ("poll", "geo", "webpage", "other"):
            kind = info.kind if info else "none"
            raise MediaFetchError(422, f"Media kind '{kind}' has no downloadable file")

        size_hint = info.size
        if size_hint is None:
            size_hint = getattr(getattr(message, "file", None), "size", None)
        if isinstance(size_hint, int) and size_hint > MEDIA_MAX_BYTES:
            raise MediaFetchError(
                413, f"Media too large: {size_hint} bytes > cap {MEDIA_MAX_BYTES} bytes"
            )

        if output_path:
            # Legacy semantics kept 1:1: create the parent dir and hand the raw
            # string to Telethon (a trailing "/" means "save inside this dir").
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            downloaded = await self._download_with_slot(message, output_path)
            if not downloaded:
                raise MediaFetchError(502, "Failed to download")
            final_path = Path(downloaded)
        else:
            ext = ""
            if info.kind == "contact":
                ext = ".vcf"
            elif info.file_name:
                ext = os.path.splitext(info.file_name)[1]
            if not ext:
                try:
                    ext = utils.get_extension(message.media) or ""
                except Exception:
                    ext = ""
            if info.kind == "voice" and ext.lower() in ("", ".oga"):
                ext = ".ogg"  # Telegram voice = Ogg/Opus; .ogg is what Whisper accepts
            ext = re.sub(r"[^A-Za-z0-9.]", "", ext)[:10]

            paths["dir"].mkdir(parents=True, exist_ok=True)
            os.chmod(paths["dir"], 0o700)
            # Download to a unique temp name, then atomic rename into place.
            tmp_target = Path(f"{paths['base']}.part-{_secrets.token_hex(4)}{ext or '.bin'}")
            try:
                downloaded = await self._download_with_slot(message, str(tmp_target))
                if not downloaded:
                    raise MediaFetchError(502, "Failed to download")
                tmp_path = Path(downloaded)
                os.chmod(tmp_path, 0o600)
                final_path = Path(f"{paths['base']}{ext or tmp_path.suffix}")
                os.replace(tmp_path, final_path)
            finally:
                for leftover in paths["dir"].glob(f"{paths['base'].name}.part-*"):
                    if leftover.name.startswith(tmp_target.stem):
                        leftover.unlink(missing_ok=True)

        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
        real_size = final_path.stat().st_size
        if real_size > MEDIA_MAX_BYTES:
            final_path.unlink(missing_ok=True)
            raise MediaFetchError(
                413, f"Media too large: {real_size} bytes > cap {MEDIA_MAX_BYTES} bytes"
            )

        file_obj = getattr(message, "file", None)
        result: Dict[str, Any] = {
            "success": True,
            "path": str(final_path),
            "file_name": info.file_name or final_path.name,
            "mime_type": info.mime_type or ("text/vcard" if info.kind == "contact" else getattr(file_obj, "mime_type", None)),
            "size": real_size,
            "kind": info.kind,
            "duration": info.duration,
            "cached": False,
        }
        if not output_path:
            tmp = paths["meta"].with_suffix(f".json.tmp-{_secrets.token_hex(4)}")
            tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, paths["meta"])
        return result

    async def transcribe_media(
        self, chat_id: Union[int, str], message_id: int
    ) -> Dict[str, Any]:
        """Download (cached) and transcribe voice/video-note/audio/video via Groq Whisper.

        Returns the fetch_media payload plus {transcript, transcript_cached}.
        Raises MediaFetchError on any failure (key never appears in messages).
        """
        media = await self.fetch_media(chat_id, message_id)
        if media.get("kind") not in TRANSCRIBABLE_KINDS:
            raise MediaFetchError(
                422, f"Media kind '{media.get('kind')}' cannot be transcribed"
            )

        chat_id_v, _ = validate_ids("chat_id", chat_id)
        paths = self._media_cache_paths(chat_id_v, message_id)

        async with self._key_lock(("transcript", str(chat_id_v), message_id)):
            if paths["transcript"].is_file():
                media["transcript"] = paths["transcript"].read_text(encoding="utf-8")
                media["transcript_cached"] = True
                return media

            if media["size"] > GROQ_MAX_BYTES:
                raise MediaFetchError(
                    413, f"File too large for Whisper ({media['size']} > {GROQ_MAX_BYTES} bytes)"
                )

            key = load_groq_api_key()
            if not key:
                raise MediaFetchError(
                    503,
                    "Transcription unavailable: Groq API key not configured "
                    "(set GROQ_API_KEY or secrets/groq-api-key in the skill dir). Download still works.",
                )

            import httpx  # already a dependency of api.py

            src = Path(media["path"])
            upload_name = src.name
            if upload_name.lower().endswith(".oga"):
                upload_name = upload_name[:-4] + ".ogg"  # Groq rejects .oga by extension
            mime = media.get("mime_type") or "application/octet-stream"
            async with self._transcribe_semaphore:
                try:
                    # Stream the open file handle (multipart is chunked by httpx).
                    with src.open("rb") as fh:
                        async with httpx.AsyncClient(timeout=120.0) as http:
                            resp = await http.post(
                                GROQ_TRANSCRIBE_URL,
                                headers={"Authorization": f"Bearer {key}"},
                                data={
                                    "model": GROQ_MODEL,
                                    "language": GROQ_LANGUAGE,
                                    "response_format": "text",
                                },
                                files={"file": (upload_name, fh, mime)},
                            )
                except Exception as e:
                    raise MediaFetchError(
                        502, "Groq request failed: " + redact_secret(str(e), key)[:200]
                    )
            if resp.status_code != 200:
                body = redact_secret(resp.text or "", key)[:200]
                raise MediaFetchError(502, f"Groq HTTP {resp.status_code}: {body}")

            transcript = (resp.text or "").strip()
            tmp = paths["transcript"].with_suffix(f".txt.tmp-{_secrets.token_hex(4)}")
            tmp.write_text(transcript, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, paths["transcript"])
            media["transcript"] = transcript
            media["transcript_cached"] = False
            return media

    async def download_media(
        self, chat_id: Union[int, str], message_id: int, output_path: Optional[str] = None
    ) -> str:
        """Legacy contract: JSON string {"success": true, "path", ...} or {"success": false, "error"}.

        Never raises; unexpected errors go through log_and_format_error like before.
        """
        try:
            result = await self.fetch_media(chat_id, message_id, output_path=output_path)
            return json.dumps(result, ensure_ascii=False)
        except MediaFetchError as e:
            return json.dumps({"success": False, "error": e.message}, ensure_ascii=False)
        except Exception as e:
            return log_and_format_error("download_media", e, chat_id=chat_id, message_id=message_id)


    async def export_messages(
        self,
        chat_id: Union[int, str],
        limit: int = 500,
        out_path: Optional[str] = None,
    ) -> str:
        """Export messages to a JSON file using the active client."""
        chat_id, error = validate_ids("chat_id", chat_id)
        if error:
            return error

        try:
            await self._wait_for_rate_limit()
            entity = await self.client.get_entity(chat_id)

            messages = []
            count = 0
            async for msg in self.client.iter_messages(entity):
                messages.append({
                    "id": msg.id,
                    "date": msg.date.isoformat() if msg.date else None,
                    "text": msg.message or "",
                    "sender_id": msg.sender_id,
                    "has_media": bool(msg.media),
                    "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
                })
                count += 1
                if limit and count >= limit:
                    break

            if not out_path:
                safe_id = str(chat_id).replace("/", "_")
                out_dir = Path("./exports")
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = str(out_dir / f"{safe_id}.json")

            tmp_path = out_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2, default=json_serializer)
            os.replace(tmp_path, out_path)

            return json.dumps({"status": "ok", "count": len(messages), "path": out_path}, indent=2)
        except Exception as e:
            return log_and_format_error("export_messages", e, chat_id=chat_id)



# Global singleton instance
telegram = TelegramCore()
