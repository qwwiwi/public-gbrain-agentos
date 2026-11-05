"""
Telegram HTTP API - REST endpoints for Telegram functionality.

This provides an HTTP API that can be called by local scripts while
running in Docker alongside the MCP server.
"""

import asyncio
import os
import httpx
from contextlib import asynccontextmanager
from typing import Optional, List, Union

import json
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from telethon import events
from pydantic import BaseModel, Field

from telegram_core import telegram, MediaFetchError, FolderError, SendRefusedError, log_and_format_error

SALES_WEBHOOK_URL = os.getenv("SALES_WEBHOOK_URL", "")
SALES_MONITORED_USERS = os.getenv("SALES_MONITORED_USERS", "")

def _parse_monitored_users(raw: str):
    raw = raw.strip()
    if not raw:
        return set()
    try:
        import json as _json
        if raw.startswith("["):
            return set(_json.loads(raw))
    except Exception:
        pass
    return set([int(x) for x in raw.split(",") if x.strip().isdigit()])

MONITORED_USERS = _parse_monitored_users(SALES_MONITORED_USERS)



# ==================== Request/Response Models ====================

class SendMessageRequest(BaseModel):
    chat_id: Union[int, str]
    message: str
    reply_to: Optional[int] = None
    parse_mode: Optional[str] = None


class SendFileRequest(BaseModel):
    chat_id: Union[int, str]
    file_path: str
    caption: Optional[str] = None
    reply_to: Optional[int] = None
    parse_mode: Optional[str] = None


class EditMessageRequest(BaseModel):
    chat_id: Union[int, str]
    message_id: int
    new_text: str


class DeleteMessageRequest(BaseModel):
    chat_id: Union[int, str]
    message_id: int
    revoke: bool = True


class ForwardMessageRequest(BaseModel):
    from_chat_id: Union[int, str]
    to_chat_id: Union[int, str]
    message_id: int


class SearchMessagesRequest(BaseModel):
    chat_id: Union[int, str]
    query: str
    limit: int = 20
    from_user: Optional[Union[int, str]] = None


class AddContactRequest(BaseModel):
    phone: str
    first_name: str
    last_name: Optional[str] = None


class CreateGroupRequest(BaseModel):
    title: str
    users: List[Union[int, str]]


class CreateSupergroupRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=128)
    about: Optional[str] = Field(None, max_length=255)
    users: List[Union[int, str]] = Field(default_factory=list)


class HistoryVisibleRequest(BaseModel):
    visible: bool = True


class InviteToGroupRequest(BaseModel):
    chat_id: Union[int, str]
    user_ids: List[Union[int, str]]


class AdminRequest(BaseModel):
    chat_id: Union[int, str]
    user_id: Union[int, str]
    title: Optional[str] = None


class BanUserRequest(BaseModel):
    chat_id: Union[int, str]
    user_id: Union[int, str]
    until_date: Optional[int] = None


class SaveDraftRequest(BaseModel):
    chat_id: Union[int, str]
    message: str
    reply_to: Optional[int] = None


class FolderUpsertRequest(BaseModel):
    title: str
    color: Optional[int] = Field(None, ge=0, le=6)
    emoticon: Optional[str] = None
    include_chat_ids: List[Union[int, str]] = Field(default_factory=list)
    mode: str = Field("replace", pattern="^(replace|add|remove)$")


class ApiResponse(BaseModel):
    success: bool
    data: Optional[str] = None
    error: Optional[str] = None


# ==================== FastAPI App ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    print("Starting Telegram client...")
    await telegram.start()
    print("Telegram client started. API ready.")
    if SALES_WEBHOOK_URL:
        print(f"Sales webhook enabled: {SALES_WEBHOOK_URL}")

        async def _sales_hook(event):
            if not event.is_private:
                return
            uid = event.sender_id
            if MONITORED_USERS and uid not in MONITORED_USERS:
                return
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        SALES_WEBHOOK_URL,
                        json={"text": f"/sell {uid}", "source": "telegram-core"},
                        timeout=5.0,
                    )
            except Exception as e:
                print(f"Sales webhook error: {e}")

        telegram.client.add_event_handler(_sales_hook, events.NewMessage(incoming=True))
    yield
    print("Shutting down Telegram client...")
    await telegram.stop()


app = FastAPI(
    title="Telegram API",
    description="HTTP API for Telegram functionality",
    version="1.0.0",
    lifespan=lifespan,
)


def make_response(result: str) -> ApiResponse:
    """Create a standardized API response."""
    if result.startswith("An error occurred") or "Error" in result[:50]:
        return ApiResponse(success=False, error=result)
    return ApiResponse(success=True, data=result)


@app.exception_handler(SendRefusedError)
async def send_refused_handler(_request, exc: SendRefusedError):
    """Never-write-first refusal: HTTP 403 in the legacy ApiResponse shape."""
    return JSONResponse(status_code=403, content={"success": False, "data": None, "error": exc.message})


# ==================== Health Check ====================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "telegram_connected": telegram._started}


# ==================== Chat Endpoints ====================

@app.get("/chats", response_model=ApiResponse)
async def get_chats(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """Get a paginated list of chats."""
    result = await telegram.get_chats(page=page, page_size=page_size)
    return make_response(result)


@app.get("/chats/list", response_model=ApiResponse)
async def list_chats(
    limit: int = Query(50, ge=1, le=500),
    chat_type: Optional[str] = Query(None, pattern="^(user|group|channel)$"),
    archived: bool = False,
    unread_only: bool = False,
):
    """Get a filtered list of chats with metadata."""
    result = await telegram.list_chats(
        limit=limit, chat_type=chat_type, archived=archived, unread_only=unread_only
    )
    return make_response(result)


@app.get("/folders", response_model=ApiResponse)
async def list_folders(include_chats: bool = Query(True)):
    """List Telegram folders (dialog filters) with optional chats inside each."""
    result = await telegram.list_folders(include_chats=include_chats)
    return make_response(result)


@app.post("/folders/upsert", response_model=ApiResponse)
async def upsert_folder(request: FolderUpsertRequest):
    """Create/update ONE folder by exact title and set its chats (mode replace|add|remove).

    Other folders are never touched; there is no delete. color/emoticon change only when sent
    (color null clears it). Errors: 422 validation / >200 chats / unresolved ids / empty result,
    409 shared chatlist or no free id, 429 FloodWait, 502 upstream.
    """
    sent = request.model_fields_set if hasattr(request, "model_fields_set") else request.__fields_set__
    try:
        result = await telegram.upsert_folder(
            title=request.title,
            include_chat_ids=request.include_chat_ids,
            mode=request.mode,
            color=request.color,
            set_color="color" in sent,
            emoticon=request.emoticon,
            set_emoticon="emoticon" in sent,
        )
    except FolderError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return ApiResponse(success=True, data=json.dumps(result, ensure_ascii=False))


@app.get("/chats/{chat_id}", response_model=ApiResponse)
async def get_chat(chat_id: str):
    """Get detailed information about a specific chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.get_chat(parsed_id)
    return make_response(result)


@app.get("/chats/{chat_id}/read_status", response_model=ApiResponse)
async def get_read_status(chat_id: str):
    """Read-receipt status for a chat.

    Returns read_outbox_max_id (peer прочитал наши исходящие <= этому id),
    read_inbox_max_id, top_message_id, unread_count.
    """
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.get_read_status(parsed_id)
    return make_response(result)


# ==================== Message Endpoints ====================

@app.get("/chats/{chat_id}/messages", response_model=ApiResponse)
async def get_messages(
    chat_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get paginated messages from a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.get_messages(parsed_id, page=page, page_size=page_size)
    return make_response(result)


@app.get("/chats/{chat_id}/messages/{message_id}", response_model=ApiResponse)
async def get_message(
    chat_id: str,
    message_id: int,
):
    """Get a single message by ID from a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.get_message(parsed_id, message_id)
    return make_response(result)


@app.post("/messages/send", response_model=ApiResponse)
async def send_message(request: SendMessageRequest):
    """Send a message to a chat."""
    result = await telegram.send_message(
        chat_id=request.chat_id,
        message=request.message,
        reply_to=request.reply_to,
        parse_mode=request.parse_mode,
    )
    return make_response(result)


@app.post("/messages/send-file", response_model=ApiResponse)
async def send_file(request: SendFileRequest):
    """Send a file/photo to a chat (local file path on server)."""
    result = await telegram.send_file(
        chat_id=request.chat_id,
        file_path=request.file_path,
        caption=request.caption,
        reply_to=request.reply_to,
        parse_mode=request.parse_mode,
    )
    return make_response(result)


@app.put("/messages/edit", response_model=ApiResponse)
async def edit_message(request: EditMessageRequest):
    """Edit an existing message."""
    result = await telegram.edit_message(
        chat_id=request.chat_id,
        message_id=request.message_id,
        new_text=request.new_text,
    )
    return make_response(result)


@app.delete("/messages/delete", response_model=ApiResponse)
async def delete_message(request: DeleteMessageRequest):
    """Delete a message."""
    result = await telegram.delete_message(
        chat_id=request.chat_id,
        message_id=request.message_id,
        revoke=request.revoke,
    )
    return make_response(result)


@app.post("/messages/forward", response_model=ApiResponse)
async def forward_message(request: ForwardMessageRequest):
    """Forward a message from one chat to another."""
    result = await telegram.forward_message(
        from_chat_id=request.from_chat_id,
        to_chat_id=request.to_chat_id,
        message_id=request.message_id,
    )
    return make_response(result)


@app.post("/messages/search", response_model=ApiResponse)
async def search_messages(request: SearchMessagesRequest):
    """Search for messages in a chat."""
    result = await telegram.search_messages(
        chat_id=request.chat_id,
        query=request.query,
        limit=request.limit,
        from_user=request.from_user,
    )
    return make_response(result)


@app.get("/chats/{chat_id}/messages/{message_id}/media", response_model=ApiResponse)
async def download_media(
    chat_id: str,
    message_id: int,
    output_path: Optional[str] = Query(None, description="Legacy: explicit save path (trailing '/' = save inside dir). Default: media-cache/<chat>/<msg>.<ext> inside the skill dir (chmod 600)"),
    transcribe: bool = Query(False, description="Also transcribe voice/video-note/audio/video via Groq Whisper (cached). Implies v=2"),
    v: int = Query(1, ge=1, le=2, description="1 = legacy contract (HTTP 200, errors as {success:false,error} in data); 2 = HTTP status codes"),
):
    """Download media from a message.

    v=1 (default, legacy): always HTTP 200; data = JSON string
      {success: true, path, file_name, mime_type, size, kind, duration, cached} or {success: false, error}.
    v=2 / transcribe=1: same success payload (+ transcript, transcript_cached), errors as HTTP codes:
      404 no message/media, 413 over size cap, 422 not downloadable/transcribable,
      429 Telegram FloodWait, 503 transcription key missing, 502 upstream failure, 504 timeout.
    """
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    if not transcribe and v == 1:
        return make_response(await telegram.download_media(parsed_id, message_id, output_path=output_path))
    if transcribe and output_path:
        raise HTTPException(status_code=400, detail="output_path is not supported with transcribe=1")
    try:
        if transcribe:
            result = await telegram.transcribe_media(parsed_id, message_id)
        else:
            result = await telegram.fetch_media(parsed_id, message_id, output_path=output_path)
    except MediaFetchError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=log_and_format_error("download_media", e, chat_id=chat_id, message_id=message_id))
    return ApiResponse(success=True, data=json.dumps(result, ensure_ascii=False))


@app.get("/chats/{chat_id}/messages/{message_id}/media/file")
async def download_media_file(chat_id: str, message_id: int):
    """Stream the (cached) media file bytes – for agents not on the chip host. HTTP codes as v=2."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    try:
        result = await telegram.fetch_media(parsed_id, message_id)
    except MediaFetchError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=log_and_format_error("download_media_file", e, chat_id=chat_id, message_id=message_id))
    return FileResponse(
        result["path"],
        media_type=result.get("mime_type") or "application/octet-stream",
        filename=result.get("file_name") or None,
    )


# ==================== Contact Endpoints ====================

@app.get("/contacts", response_model=ApiResponse)
async def list_contacts():
    """Get all contacts."""
    result = await telegram.list_contacts()
    return make_response(result)


@app.get("/contacts/search", response_model=ApiResponse)
async def search_contacts(query: str, limit: int = Query(10, ge=1, le=100)):
    """Search contacts by name or username."""
    result = await telegram.search_contacts(query=query, limit=limit)
    return make_response(result)


@app.post("/contacts", response_model=ApiResponse)
async def add_contact(request: AddContactRequest):
    """Add a new contact."""
    result = await telegram.add_contact(
        phone=request.phone,
        first_name=request.first_name,
        last_name=request.last_name,
    )
    return make_response(result)


@app.delete("/contacts/{user_id}", response_model=ApiResponse)
async def delete_contact(user_id: str):
    """Delete a contact."""
    try:
        parsed_id = int(user_id)
    except ValueError:
        parsed_id = user_id
    result = await telegram.delete_contact(parsed_id)
    return make_response(result)


# ==================== User Endpoints ====================

@app.get("/me", response_model=ApiResponse)
async def get_me():
    """Get information about the current user."""
    result = await telegram.get_me()
    return make_response(result)


@app.get("/users/{user_id}/status", response_model=ApiResponse)
async def get_user_status(user_id: str):
    """Get the online status of a user."""
    try:
        parsed_id = int(user_id)
    except ValueError:
        parsed_id = user_id
    result = await telegram.get_user_status(parsed_id)
    return make_response(result)


@app.get("/resolve/{username}", response_model=ApiResponse)
async def resolve_username(username: str):
    """Resolve a username to get entity information."""
    result = await telegram.resolve_username(username)
    return make_response(result)


# ==================== Group Endpoints ====================

@app.post("/groups", response_model=ApiResponse)
async def create_group(request: CreateGroupRequest):
    """Create a new group chat."""
    result = await telegram.create_group(title=request.title, users=request.users)
    return make_response(result)


@app.post("/supergroups", response_model=ApiResponse)
async def create_supergroup(request: CreateSupergroupRequest):
    """Create a supergroup (megagroup), invite users, make history visible to new members.

    data = JSON: created, chat_id (marked -100…), title, invites[{user, status, error?}], history.
    One create attempt only: do NOT retry when success=true, even if some invites failed.
    """
    result = await telegram.create_supergroup(
        title=request.title, about=request.about, users=request.users
    )
    return make_response(result)


@app.post("/chats/{chat_id}/history-visible", response_model=ApiResponse)
async def set_history_visible(chat_id: str, request: Optional[HistoryVisibleRequest] = None):
    """Show (visible=true, default) or hide chat history for new members. Supergroups/channels only."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    visible = request.visible if request is not None else True
    result = await telegram.set_history_visible(parsed_id, visible=visible)
    return make_response(result)


@app.post("/groups/invite", response_model=ApiResponse)
async def invite_to_group(request: InviteToGroupRequest):
    """Invite users to a group or channel."""
    result = await telegram.invite_to_group(chat_id=request.chat_id, user_ids=request.user_ids)
    return make_response(result)


@app.post("/chats/{chat_id}/leave", response_model=ApiResponse)
async def leave_chat(chat_id: str):
    """Leave a group or channel."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.leave_chat(parsed_id)
    return make_response(result)


@app.get("/chats/{chat_id}/participants", response_model=ApiResponse)
async def get_participants(
    chat_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Get participants of a group or channel."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.get_participants(parsed_id, limit=limit, offset=offset)
    return make_response(result)


# ==================== Admin Endpoints ====================

@app.get("/chats/{chat_id}/admins", response_model=ApiResponse)
async def get_admins(chat_id: str):
    """Get administrators of a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.get_admins(parsed_id)
    return make_response(result)


@app.post("/admin/promote", response_model=ApiResponse)
async def promote_admin(request: AdminRequest):
    """Promote a user to admin."""
    result = await telegram.promote_admin(
        chat_id=request.chat_id,
        user_id=request.user_id,
        title=request.title,
    )
    return make_response(result)


@app.post("/admin/ban", response_model=ApiResponse)
async def ban_user(request: BanUserRequest):
    """Ban a user from a chat."""
    result = await telegram.ban_user(
        chat_id=request.chat_id,
        user_id=request.user_id,
        until_date=request.until_date,
    )
    return make_response(result)


@app.post("/admin/unban", response_model=ApiResponse)
async def unban_user(request: AdminRequest):
    """Unban a user from a chat."""
    result = await telegram.unban_user(chat_id=request.chat_id, user_id=request.user_id)
    return make_response(result)


# ==================== Channel Endpoints ====================

@app.get("/chats/{chat_id}/invite-link", response_model=ApiResponse)
async def get_invite_link(chat_id: str):
    """Get the invite link for a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.get_invite_link(parsed_id)
    return make_response(result)


# ==================== Notification Endpoints ====================

@app.post("/chats/{chat_id}/mute", response_model=ApiResponse)
async def mute_chat(chat_id: str, mute_until: Optional[int] = None):
    """Mute notifications for a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.mute_chat(parsed_id, mute_until=mute_until)
    return make_response(result)


@app.post("/chats/{chat_id}/unmute", response_model=ApiResponse)
async def unmute_chat(chat_id: str):
    """Unmute notifications for a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.unmute_chat(parsed_id)
    return make_response(result)


# ==================== Archive Endpoints ====================

@app.post("/chats/{chat_id}/archive", response_model=ApiResponse)
async def archive_chat(chat_id: str):
    """Archive a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.archive_chat(parsed_id)
    return make_response(result)


@app.post("/chats/{chat_id}/unarchive", response_model=ApiResponse)
async def unarchive_chat(chat_id: str):
    """Unarchive a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.unarchive_chat(parsed_id)
    return make_response(result)


# ==================== Draft Endpoints ====================

@app.post("/drafts/save", response_model=ApiResponse)
async def save_draft(request: SaveDraftRequest):
    """Save a draft message to a chat."""
    result = await telegram.save_draft(
        chat_id=request.chat_id,
        message=request.message,
        reply_to=request.reply_to,
    )
    return make_response(result)


@app.delete("/drafts/{chat_id}", response_model=ApiResponse)
async def clear_draft(chat_id: str):
    """Clear a draft from a chat."""
    try:
        parsed_id = int(chat_id)
    except ValueError:
        parsed_id = chat_id
    result = await telegram.clear_draft(parsed_id)
    return make_response(result)


# ==================== Poll Endpoints ====================

@app.get("/poll/new-dms", response_model=ApiResponse)
async def poll_new_dms(
    since_ts: float = Query(..., description="Unix timestamp — return messages newer than this"),
    limit_per_chat: int = Query(10, ge=1, le=50),
):
    """Return new private DMs since since_ts. Uses chip session — no new Telethon instance."""
    result = await telegram.poll_new_dms(since_ts=since_ts, limit_per_chat=limit_per_chat)
    return make_response(result)


# ==================== TGDL Export ====================

@app.post("/tgdl/export", response_model=ApiResponse)
async def tgdl_export(chat_id: str, limit: int = 500, out_path: Optional[str] = None):
    """Export messages from a chat to JSON file."""
    result = await telegram.export_messages(chat_id, limit=limit, out_path=out_path)
    return make_response(result)

# ==================== Main ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
