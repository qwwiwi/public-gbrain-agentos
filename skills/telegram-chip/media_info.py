"""Media description helpers for telegram-chip.

Pure functions (no network, no client) that turn a Telethon ``message.media``
object into a small metadata dict and a human-readable marker such as
``[файл: report.pdf, 1.2 MB]`` or ``[голосовое 0:10]``.

Type checks go by class name (not isinstance) so the module works across
Telethon versions and can be unit-tested with real or fake TL objects.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

# Media kinds that carry speech and can be sent to transcription.
TRANSCRIBABLE_KINDS = {"voice", "video_note", "audio", "video"}


@dataclass
class MediaInfo:
    """Normalized description of a message attachment."""

    kind: str  # photo|voice|video_note|video|gif|audio|sticker|document|poll|geo|contact|webpage|other
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    duration: Optional[int] = None  # seconds
    title: Optional[str] = None  # poll question / audio title / contact name / sticker emoji
    media_type: Optional[str] = None  # raw Telethon class name
    via_webpage: bool = False  # file comes from a link preview (no list marker)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _cls(obj: Any) -> str:
    return type(obj).__name__


def format_size(size: Optional[int]) -> str:
    """Format a byte count as ``512 B`` / ``340 KB`` / ``1.2 MB`` / ``1.5 GB``."""
    if size is None or size < 0:
        return "?"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


def format_duration(seconds: Optional[int]) -> str:
    """Format seconds as ``m:ss`` (or ``h:mm:ss`` for long media)."""
    if seconds is None or seconds < 0:
        return "?:??"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _text_of(value: Any) -> str:
    """Extract plain text from str or TextWithEntities-like objects."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return getattr(value, "text", "") or ""


def _photo_size(photo: Any) -> Optional[int]:
    """Best-effort byte size of the largest photo variant."""
    best: Optional[int] = None
    for ps in getattr(photo, "sizes", None) or []:
        candidate = getattr(ps, "size", None)
        if candidate is None:
            progressive = getattr(ps, "sizes", None)
            if progressive:
                candidate = max(progressive)
        if isinstance(candidate, int) and (best is None or candidate > best):
            best = candidate
    return best


def describe_media(media: Any) -> Optional[MediaInfo]:
    """Describe ``message.media``; returns None when there is no media."""
    if media is None:
        return None
    name = _cls(media)

    if name == "MessageMediaPhoto":
        return _describe_photo(getattr(media, "photo", None), name)

    if name == "MessageMediaDocument":
        return _describe_document(getattr(media, "document", None), name)

    if name == "MessageMediaWebPage":
        # Link preview: expose the embedded document/photo (downloadable), if any.
        webpage = getattr(media, "webpage", None)
        doc = getattr(webpage, "document", None) if webpage is not None else None
        photo = getattr(webpage, "photo", None) if webpage is not None else None
        if doc is not None and _cls(doc) != "DocumentEmpty":
            info = _describe_document(doc, name)
        elif photo is not None and _cls(photo) != "PhotoEmpty":
            info = _describe_photo(photo, name)
        else:
            return MediaInfo(kind="webpage", media_type=name)
        info.via_webpage = True
        return info

    if name == "MessageMediaPoll":
        poll = getattr(media, "poll", None)
        question = _text_of(getattr(poll, "question", None)) if poll is not None else ""
        return MediaInfo(kind="poll", title=question or None, media_type=name)

    if name in ("MessageMediaGeo", "MessageMediaGeoLive", "MessageMediaVenue"):
        title = getattr(media, "title", None) if name == "MessageMediaVenue" else None
        return MediaInfo(kind="geo", title=title or None, media_type=name)

    if name == "MessageMediaContact":
        parts = [getattr(media, "first_name", "") or "", getattr(media, "last_name", "") or ""]
        full = " ".join(p for p in parts if p).strip()
        return MediaInfo(kind="contact", title=full or None, media_type=name)

    return MediaInfo(kind="other", media_type=name)


def _describe_photo(photo: Any, media_type: str) -> MediaInfo:
    return MediaInfo(
        kind="photo",
        mime_type="image/jpeg",
        size=_photo_size(photo) if photo is not None else None,
        media_type=media_type,
    )


def _describe_document(doc: Any, media_type: str) -> MediaInfo:
    info = MediaInfo(kind="document", media_type=media_type)
    if doc is None or _cls(doc) == "DocumentEmpty":
        return info
    info.mime_type = getattr(doc, "mime_type", None)
    info.size = getattr(doc, "size", None)
    audio = video = None
    is_sticker = is_animated = False
    for attr in getattr(doc, "attributes", None) or []:
        an = _cls(attr)
        if an == "DocumentAttributeFilename":
            info.file_name = getattr(attr, "file_name", None)
        elif an == "DocumentAttributeAudio":
            audio = attr
        elif an == "DocumentAttributeVideo":
            video = attr
        elif an == "DocumentAttributeSticker":
            is_sticker = True
            info.title = getattr(attr, "alt", None) or None
        elif an == "DocumentAttributeAnimated":
            is_animated = True
    if is_sticker:
        info.kind = "sticker"
    elif audio is not None and getattr(audio, "voice", False):
        info.kind = "voice"
        info.duration = int(getattr(audio, "duration", 0) or 0)
    elif video is not None and getattr(video, "round_message", False):
        info.kind = "video_note"
        info.duration = int(getattr(video, "duration", 0) or 0)
    elif is_animated:
        info.kind = "gif"
    elif video is not None:
        info.kind = "video"
        info.duration = int(getattr(video, "duration", 0) or 0)
    elif audio is not None:
        info.kind = "audio"
        info.duration = int(getattr(audio, "duration", 0) or 0)
        performer = getattr(audio, "performer", None)
        title = getattr(audio, "title", None)
        joined = " – ".join(p for p in (performer, title) if p)
        info.title = joined or None
    return info


def media_marker(info: Optional[MediaInfo]) -> str:
    """Render a short Russian marker for the attachment (empty for none/webpage)."""
    if info is None or info.kind == "webpage" or info.via_webpage:
        return ""
    k = info.kind
    if k == "photo":
        return "[фото]"
    if k == "voice":
        return f"[голосовое {format_duration(info.duration)}]"
    if k == "video_note":
        return f"[кружок {format_duration(info.duration)}]"
    if k == "video":
        name = f"{info.file_name}, " if info.file_name else ""
        return f"[видео {name}{format_duration(info.duration)}]"
    if k == "gif":
        return "[GIF]"
    if k == "audio":
        label = info.title or info.file_name
        prefix = f"{label}, " if label else ""
        return f"[аудио: {prefix}{format_duration(info.duration)}]"
    if k == "sticker":
        return f"[стикер {info.title}]" if info.title else "[стикер]"
    if k == "document":
        name = info.file_name or "без имени"
        return f"[файл: {name}, {format_size(info.size)}]"
    if k == "poll":
        return f"[опрос: {info.title}]" if info.title else "[опрос]"
    if k == "geo":
        return f"[геолокация: {info.title}]" if info.title else "[геолокация]"
    if k == "contact":
        return f"[контакт: {info.title}]" if info.title else "[контакт]"
    return f"[вложение: {info.media_type}]"


def message_text_with_marker(text: Optional[str], media: Any) -> Optional[str]:
    """Message text for list views: ``<marker> <caption>`` for media, plain text otherwise.

    Non-media messages return their text unchanged (``None`` stays ``None`` so
    the legacy ``Message: None`` rendering is preserved byte-for-byte).
    """
    marker = media_marker(describe_media(media))
    if not marker:
        return text
    if text:
        return f"{marker} {text}"
    return marker
