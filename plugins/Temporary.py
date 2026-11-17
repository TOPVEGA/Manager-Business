# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import tempfile
import time
import unicodedata
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import structlog
from aiogram import BaseMiddleware, Bot
from aiogram.client.default import Default
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    Chat,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    InputProfilePhotoStatic,
    FSInputFile,
)

from config import settings
from Database import db
from rich import ButtonStyle, RichMessageButton, RichMessageRow, send_rich

log = structlog.get_logger(__name__)
UTC = timezone.utc


WELCOME_COOLDOWN = timedelta(hours=1)
STYLE_LABEL = {"danger": "أحمر", "success": "أخضر", "primary": "أزرق"}
BTN_STYLE_MAP = {"danger": "danger", "success": "success", "primary": "primary"}


def safe_html(v: Any) -> str:
    return html.escape(str(v or ""), quote=False)


def fancy_success(text: str) -> str:
    return f"<blockquote><b>✅ {text}</b></blockquote>"


def fancy_error(text: str) -> str:
    return f"<blockquote><b>⚠️ {text}</b></blockquote>"


def fancy_info(text: str) -> str:
    return f"<blockquote><b>✦ {text}</b></blockquote>"


def clickable_user_link(user_id: int, name: str) -> str:
    safe_name = safe_html(name or "بدون اسم")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def back_kb(callback_data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="رجوع", callback_data=callback_data)
    ]])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="إلغاء", callback_data="home")
    ]])


def confirm_kb(action: str, item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="نعم، تأكيد", callback_data=f"confirm_{action}_{item_id}"
        )],
        [InlineKeyboardButton(text="إلغاء", callback_data="home")],
    ])


def help_kb(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text="🚀 البدء السريع", callback_data="help_start"
        )],
        [InlineKeyboardButton(
            text="🔗 كيفية الربط", callback_data="help_link"
        )],
        [InlineKeyboardButton(
            text="💬 الأوامر المتاحة", callback_data="help_commands"
        )],
    ]
    if settings.referral_enabled:
        rows.append([InlineKeyboardButton(
            text="🎁 نظام الإحالة", callback_data="help_referral"
        )])
    rows.append([InlineKeyboardButton(
        text="👨‍💻 الدعم الفني",
        url=settings.developer_url or "https://t.me/Topvega",
    )])
    rows.append([InlineKeyboardButton(
        text="رجوع", callback_data="public_home"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def public_kb(
    user_id: int | None = None, bot: Bot | None = None
) -> InlineKeyboardMarkup:
    rows = []

    dev_name = settings.developer_name
    if settings.developer_url and bot:
        try:
            dev_username = settings.developer_url.rstrip("/").split("/")[-1]
            dev_chat = await bot.get_chat(f"@{dev_username}")
            dev_name = (
                getattr(dev_chat, "full_name", None)
                or getattr(dev_chat, "first_name", None)
                or settings.developer_name
            )
        except Exception:
            pass

    ch_name = settings.bot_channel_name
    if settings.bot_channel_url and bot:
        try:
            ch_username = settings.bot_channel_url.rstrip("/").split("/")[-1]
            ch_chat = await bot.get_chat(f"@{ch_username}")
            ch_name = (
                getattr(ch_chat, "title", None)
                or settings.bot_channel_name
            )
        except Exception:
            pass

    first_row = []
    if settings.developer_url:
        first_row.append(InlineKeyboardButton(
            text=f"👨‍💻 {dev_name}",
            url=settings.developer_url,
        ))
    if settings.bot_channel_url:
        first_row.append(InlineKeyboardButton(
            text=f"📢 {ch_name}",
            url=settings.bot_channel_url,
        ))
    if first_row:
        rows.append(first_row)

    rows.append([InlineKeyboardButton(
        text="⚙️ كيف يعمل البوت ؟",
        callback_data="how_it_works",
    )])

    linked = False
    if user_id is not None:
        if user_id == settings.owner_id:
            linked = True
        else:
            try:
                row = await db.fetchrow(
                    "SELECT 1 FROM business "
                    "WHERE user_id=$1 AND enabled=TRUE LIMIT 1",
                    user_id,
                )
                linked = bool(row)
            except Exception:
                linked = False

    if linked:
        rows.append([InlineKeyboardButton(
            text="✅ لوحة التحكم",
            callback_data="control_panel",
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="❌ لوحة التحكم",
            callback_data="control_panel",
        )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def link_help_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⚙️ كيف يعمل البوت ؟",
            callback_data="how_it_works",
        )],
        [InlineKeyboardButton(
            text="رجوع",
            callback_data="public_home",
        )],
    ])


def main_kb(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text="التفعيل والتعطيل", callback_data="toggle"
        )],
        [InlineKeyboardButton(
            text="إدارة رسالة الترحيب", callback_data="welcome"
        ), InlineKeyboardButton(
            text="الردود التلقائية", callback_data="replies"
        )],
        [InlineKeyboardButton(
            text="🚫 ممنوع السب", callback_data="swear_filter"
        ), InlineKeyboardButton(
            text="المكتومين", callback_data="muted"
        )],
        [InlineKeyboardButton(
            text="الاشتراك الإجباري", callback_data="account_force"
        )],
        [InlineKeyboardButton(
            text="الإذاعة", callback_data="broadcast"
        )],
        [InlineKeyboardButton(
            text="السوشيال ميديا", callback_data="social_media"
        ), InlineKeyboardButton(
            text="إدارة الميديا", callback_data="media_manager"
        )],
        [InlineKeyboardButton(
            text="طرق الدفع", callback_data="payment_home"
        ), InlineKeyboardButton(
            text="💹 بورصة العملات", callback_data="crypto_bourse"
        )],
        [InlineKeyboardButton(
            text="أكواد التحويل", callback_data="transfer_tool"
        ), InlineKeyboardButton(
            text="الآلة الحاسبة", callback_data="calculator_tool"
        )],
        [InlineKeyboardButton(
            text="الساعة الرقمية", callback_data="live_clock"
        ), InlineKeyboardButton(
            text="البايو الرقمي", callback_data="live_bio"
        )],
        [InlineKeyboardButton(
            text="تعديل الخط", callback_data="font_menu"
        ), InlineKeyboardButton(
            text="🧰 أدوات إضافية", callback_data="tools_menu"
        )],
        [InlineKeyboardButton(
            text="👥 قائمة العملاء", callback_data="customers_list"
        )],
        [InlineKeyboardButton(
            text="تخصيص أشكال الرسائل", callback_data="custom_messages"
        )],
        [InlineKeyboardButton(
            text="رصد الحذف والتعديل", callback_data="tracking"
        )],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton(
            text="لوحة الأدمن", callback_data="admin_panel"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def copy_kb(text: str, label: str = "Click to copy") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=label,
            copy_text=CopyTextButton(text=text),
        )
    ]])


def force_add_error_kb(
    bot_username: str, add_cb: str, back_cb: str
) -> InlineKeyboardMarkup:
    rows = []
    if bot_username:
        rows.append([InlineKeyboardButton(
            text="➕ أضف البوت للقناة",
            url=(
                f"https://t.me/{bot_username}"
                f"?startchannel=true&admin=invite_users"
            ),
        )])
    rows.append([InlineKeyboardButton(
        text="🔄 حاول مرة أخرى", callback_data=add_cb,
    )])
    rows.append([InlineKeyboardButton(
        text="رجوع", callback_data=back_cb,
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_or_replace(
    call: CallbackQuery, text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    msg = call.message
    if msg is None:
        return

    if msg.text is not None:
        try:
            await msg.edit_text(
                text, reply_markup=reply_markup, parse_mode="HTML"
            )
            return
        except Exception as exc:
            err = str(exc).lower()
            if "message is not modified" in err:
                return
            log.warning(
                "edit_or_replace.edit_text.failed", error=str(exc)[:160]
            )

    if msg.caption is not None:
        try:
            await msg.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            err = str(exc).lower()
            if "message is not modified" in err:
                return
            log.warning(
                "edit_or_replace.edit_caption.failed", error=str(exc)[:160]
            )

    try:
        await msg.answer(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )
    except Exception:
        log.exception("edit_or_replace.failed")


class RateLimiter:
    def __init__(self, rate: int, per_seconds: int) -> None:
        self.rate = rate
        self.per = per_seconds
        self._buckets: OrderedDict[int, deque[float]] = OrderedDict()
        self._max_users = 20_000
        self._lock = asyncio.Lock()

    async def is_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(user_id)
            if bucket is None:
                bucket = deque()
                self._buckets[user_id] = bucket
            self._buckets.move_to_end(user_id)
            while len(self._buckets) > self._max_users:
                self._buckets.popitem(last=False)
            cutoff = now - self.per
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.rate:
                return False
            bucket.append(now)
            return True


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: int, window: int) -> None:
        self.limiter = RateLimiter(rate=rate, per_seconds=window)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        try:
            allowed = await self.limiter.is_allowed(user.id)
        except Exception:
            allowed = True
        if not allowed:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer(
                        "⏳ الرجاء الانتظار قليلاً.", show_alert=False
                    )
                except Exception:
                    pass
            return None
        return await handler(event, data)


class ErrorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:
            log.exception(
                "update.error", error=str(exc),
                update_type=type(event).__name__,
            )
            return None


class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not settings.maintenance_mode:
            return await handler(event, data)

        user = data.get("event_from_user")
        if user and user.id == settings.owner_id:
            return await handler(event, data)

        if isinstance(event, Message):
            text = (event.text or "").strip().lower()
            if text.startswith("/start") or text.startswith("/help"):
                return await handler(event, data)
            try:
                await event.answer(
                    settings.maintenance_message,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return None

        if isinstance(event, CallbackQuery):
            allowed_callbacks = {
                "help_start", "help_link", "help_commands",
                "help_referral", "public_home", "how_it_works",
            }
            if event.data in allowed_callbacks:
                return await handler(event, data)
            try:
                await event.answer(
                    settings.maintenance_message, show_alert=True
                )
            except Exception:
                pass
            return None

        return None


class ReferralMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            settings.referral_enabled
            and isinstance(event, Message)
            and event.text
            and event.from_user
        ):
            m = re.match(r"/start\s+ref_(\d+)", event.text)
            if m:
                try:
                    referrer_id = int(m.group(1))
                    if referrer_id != event.from_user.id:
                        await db.register_referral(
                            referrer_id, event.from_user.id
                        )
                except Exception:
                    log.exception("referral.middleware.failed")
        return await handler(event, data)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        start = time.perf_counter()
        user = data.get("event_from_user")
        try:
            return await handler(event, data)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            if elapsed > 500:
                log.warning(
                    "update.slow", ms=round(elapsed, 1),
                    uid=user.id if user else None,
                    type=type(event).__name__,
                )


_ALLOWED_TAGS = re.compile(
    r"</?(?:b|i|u|s|code|pre|a|blockquote|tg-spoiler)"
    r"(?:\s[^>]*)?>"
)


def _is_html(text: str) -> bool:
    if "<" not in text or ">" not in text:
        return False
    stripped = _ALLOWED_TAGS.sub("", text)
    if "<" in stripped or ">" in stripped:
        stripped = stripped.replace("&lt;", "").replace("&gt;", "")
        if "<" in stripped or ">" in stripped:
            return False
    return True


async def bold_outgoing_text_middleware(make_request, bot: Bot, method):
    method_name = type(method).__name__
    if method_name not in (
        "SendMessage", "EditMessageText", "SendPhoto",
        "SendVideo", "SendDocument", "SendAnimation",
        "SendVoice", "SendAudio",
    ):
        return await make_request(bot, method)

    fields = getattr(type(method), "model_fields", {})
    changes: dict[str, Any] = {}
    text_fields = (
        ("text", "parse_mode", "entities"),
        ("caption", "parse_mode", "caption_entities"),
    )
    for text_field, mode_field, entities_field in text_fields:
        if text_field not in fields or mode_field not in fields:
            continue
        value = getattr(method, text_field, None)
        if not isinstance(value, str) or not value.strip():
            continue
        if getattr(method, mode_field, None) is not None:
            continue
        if entities_field in fields and getattr(method, entities_field, None):
            continue
        if len(value) > 4000:
            continue
        if _is_html(value):
            continue
        changes[text_field] = f"<blockquote><b>{value}</b></blockquote>"
        changes[mode_field] = ParseMode.HTML
    if changes:
        method = method.model_copy(update=changes)
    return await make_request(bot, method)


_RICH_SEND_FIELDS = frozenset({
    "chat_id", "text", "parse_mode", "reply_markup",
    "reply_parameters", "reply_to_message_id",
})
_RICH_EDIT_FIELDS = frozenset({
    "chat_id", "message_id", "text", "parse_mode", "reply_markup",
})
_RICH_BLOCKED_BUTTON_FIELDS = (
    "copy_text", "web_app", "login_url", "switch_inline_query",
    "switch_inline_query_current_chat", "switch_inline_query_chosen_chat",
    "callback_game", "pay",
)


def _rich_style(btn: Any) -> ButtonStyle:
    raw = getattr(btn, "style", None)
    if not raw:
        return ButtonStyle.PRIMARY
    try:
        return ButtonStyle(getattr(raw, "value", raw))
    except ValueError:
        return ButtonStyle.PRIMARY


def markup_to_rich_rows(markup: Any) -> list[RichMessageRow] | None:
    rows: list[RichMessageRow] = []
    for line in markup.inline_keyboard:
        buttons: list[RichMessageButton] = []
        for btn in line:
            for name in _RICH_BLOCKED_BUTTON_FIELDS:
                if getattr(btn, name, None) not in (None, False):
                    return None
            if btn.callback_data is not None:
                buttons.append(RichMessageButton(
                    text=btn.text,
                    callback_data=btn.callback_data,
                    style=_rich_style(btn),
                ))
            elif btn.url:
                buttons.append(RichMessageButton(
                    text=btn.text,
                    url=btn.url,
                    style=_rich_style(btn),
                ))
            else:
                return None
        if buttons:
            rows.append(RichMessageRow(buttons=buttons))
    return rows


def _rich_extra_fields_set(method: Any, allowed: frozenset) -> bool:
    for name in type(method).model_fields:
        if name in allowed:
            continue
        value = getattr(method, name, None)
        if value is None or isinstance(value, Default):
            continue
        return True
    return False


def _rich_parse_mode(bot: Bot, method: Any) -> str | None:
    mode = getattr(method, "parse_mode", None)
    if isinstance(mode, Default):
        mode = getattr(bot.default, mode.name, None)
    if mode is None:
        return None
    return str(getattr(mode, "value", mode)).upper()


def _prepare_rich_call(bot: Bot, method: Any) -> dict | None:
    name = type(method).__name__
    allowed = _RICH_SEND_FIELDS if name == "SendMessage" else _RICH_EDIT_FIELDS
    if _rich_extra_fields_set(method, allowed):
        return None
    chat_id = getattr(method, "chat_id", None)
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    text = getattr(method, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    mode = _rich_parse_mode(bot, method)
    if mode is None:
        text = html.escape(text, quote=False)
    elif mode != "HTML":
        return None
    blocks: list = [text]
    markup = getattr(method, "reply_markup", None)
    if markup is not None:
        if not isinstance(markup, InlineKeyboardMarkup):
            return None
        rows = markup_to_rich_rows(markup)
        if rows is None:
            return None
        blocks.extend(rows)
    reply_to = None
    edit_message_id = None
    if name == "SendMessage":
        params = getattr(method, "reply_parameters", None)
        if params is not None:
            if (
                getattr(params, "chat_id", None) is not None
                or getattr(params, "quote", None)
                or getattr(params, "poll_option_id", None) is not None
            ):
                return None
            reply_to = params.message_id
        else:
            reply_to = getattr(method, "reply_to_message_id", None)
    else:
        edit_message_id = getattr(method, "message_id", None)
        if not edit_message_id:
            return None
    return {
        "chat_id": chat_id,
        "blocks": blocks,
        "reply_to": reply_to,
        "edit_message_id": edit_message_id,
    }


def _rich_result_to_message(bot: Bot, result: Any, chat_id: int) -> Any:
    if not isinstance(result, dict):
        return result
    try:
        return Message.model_validate(result, context={"bot": bot})
    except Exception:
        return Message.model_construct(
            message_id=int(result.get("message_id") or 0),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
        )


async def rich_outgoing_middleware(make_request, bot: Bot, method):
    name = type(method).__name__
    if name not in ("SendMessage", "EditMessageText"):
        return await make_request(bot, method)
    prepared = _prepare_rich_call(bot, method)
    if prepared is None:
        return await make_request(bot, method)
    result = await send_rich(
        bot.token,
        prepared["chat_id"],
        prepared["blocks"],
        reply_to=prepared["reply_to"],
        edit_message_id=prepared["edit_message_id"],
    )
    if result is None:
        return await make_request(bot, method)
    return _rich_result_to_message(bot, result, prepared["chat_id"])


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return utcnow().isoformat()


def format_remaining(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} يوم")
    if hours:
        parts.append(f"{hours} ساعة")
    if minutes and not days:
        parts.append(f"{minutes} دقيقة")
    return " و ".join(parts) or "أقل من دقيقة"


async def run_with_retry(coro_factory, retries: int = 3, backoff: float = 0.5):
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                await asyncio.sleep(backoff * (2 ** attempt))
    if last_exc:
        raise last_exc


def referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def parse_channel_input(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return "username", ""
    if raw.startswith("http://") or raw.startswith("https://"):
        m = re.search(r"t\.me/(.+)$", raw)
        if m:
            rest = m.group(1).strip("/").split("?")[0].split("/")
            if rest and rest[0] == "c":
                if len(rest) >= 2 and rest[1].isdigit():
                    return "id", f"-100{rest[1]}"
            elif rest:
                return "username", rest[0]
        return "username", ""
    if raw.startswith("@"):
        return "username", raw[1:]
    if "t.me/" in raw:
        rest = raw.split("t.me/")[-1].split("/")[0].split("?")[0]
        return "username", rest
    if re.fullmatch(r"-?\d+", raw):
        return "id", raw
    return "username", raw


async def verify_bot_is_admin(
    bot: Bot, channel_input: str
) -> tuple[bool, str, str]:
    kind, value = parse_channel_input(channel_input)
    if not value:
        return False, "رابط غير صالح", "❌ رابط غير صالح"

    target = f"@{value}" if kind == "username" else value

    try:
        if kind == "username":
            chat = await bot.get_chat(f"@{value}")
        else:
            chat = await bot.get_chat(int(value))
    except Exception as e:
        err_text = str(e)
        log.error(
            "verify_admin.get_chat.failed",
            channel=target,
            error=err_text,
        )
        return (
            False,
            target,
            f"❌ لا يمكن الوصول للقناة: {err_text}",
        )

    title = chat.title or getattr(chat, "full_name", None) or target
    resolved_id = str(chat.id) if chat.id else value

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        status = getattr(member.status, "value", member.status)
        status = str(status).lower()

        if status not in ("administrator", "creator"):
            return (
                False,
                title,
                "❌ البوت ليس أدمن في القناة!\n"
                "أضف البوت كأدمن أولاً ثم حاول مرة أخرى.",
            )

        can_invite = getattr(member, "can_invite_users", False)
        can_delete = getattr(member, "can_delete_messages", False)

        if not can_invite and not can_delete:
            return (
                False,
                title,
                "❌ البوت مشرف لكن بدون صلاحيات كافية!\n"
                "فعّل «دعوة المستخدمين» أو «حذف الرسائل» "
                "للبوت في القناة، ثم حاول مرة أخرى.",
            )

        return True, title, resolved_id

    except Exception as e:
        err_text = str(e)
        log.error(
            "verify_admin.get_member.failed",
            channel=target,
            error=err_text,
        )
        return (
            False,
            title,
            "❌ البوت ليس أدمن في القناة!\n"
            "أضف البوت كأدمن أولاً وفعّل صلاحياته ثم حاول مرة أخرى.",
        )


def try_calculate(text: str) -> str | None:
    import ast
    import operator

    if not text:
        return None
    t = text.strip()
    if not re.fullmatch(r"[\d\s\.\+\-\*/\(\)×÷]+", t):
        return None
    t = t.replace("×", "*").replace("÷", "/")
    if not re.search(r"[\+\-\*/]", t):
        return None
    if len(re.findall(r"\d", t)) < 2:
        return None

    ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
    }

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(
            node.value, (int, float)
        ):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return _eval(node.operand)
        raise ValueError("invalid")

    try:
        tree = ast.parse(t, mode="eval")
        result = _eval(tree)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return f"{t.strip()} = {result}"
    except Exception:
        return None


CURRENCY_EMOJIS = {}
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def parse_currency_request(value: str) -> tuple[float, str] | None:
    from plugins.Crypto import parse_amount_query
    return parse_amount_query(value or "")


async def fetch_live_prices() -> dict | None:
    from plugins.Crypto import fetch_live_prices as _f
    return await _f()


def fmt_price(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def currency_values(amount: float, currency: str, prices: dict) -> dict:
    from plugins.Crypto import build_values
    return build_values(amount, currency, prices)


def fmt_currency_value(key: str, value: float) -> str:
    decimals = {"usd": 2, "ton": 4, "egp": 2, "gram": 4}.get(key, 2)
    return f"{value:,.{decimals}f}".rstrip("0").rstrip(".")


def currency_keyboard(values: dict, requested: str) -> InlineKeyboardMarkup:
    from plugins.Crypto import build_keyboard
    return build_keyboard(values, requested)


def currency_header(amount: float, currency: str) -> str:
    from plugins.Crypto import _LABELS
    return (
        f"<blockquote><b>• Current price of : "
        f"{fmt_price(amount)} {_LABELS.get(currency, currency.upper())}"
        f"</b></blockquote>"
    )


def currency_tool_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} الرد على أسعار العملات",
            callback_data="currency_toggle"
        )],
        [InlineKeyboardButton(
            text="عرض الأسعار الآن", callback_data="crypto_bourse"
        )],
        [InlineKeyboardButton(
            text="🔔 تنبيهات الأسعار", callback_data="crypto_alert_menu"
        )],
        [InlineKeyboardButton(
            text="رجوع", callback_data="home"
        )],
    ])


def parse_transfer_request(value: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"\s*(\+?[0-9][0-9\s\-]{5,18}[0-9])\s+"
        r"([0-9]+(?:[.,][0-9]{1,2})?)\s*",
        value or "",
    )
    if not match:
        return None
    phone = re.sub(r"[\s\-]", "", match.group(1))
    digits = phone[1:] if phone.startswith("+") else phone
    if not 7 <= len(digits) <= 15:
        return None
    amount = match.group(2).replace(",", ".")
    if "." in amount:
        amount = amount.rstrip("0").rstrip(".")
    return phone, amount


def transfer_codes(phone: str, amount: str) -> dict:
    return {
        "etisalat": f"*777*2*{phone}*{amount}#",
        "orange": f"#7115*1*1*1*{phone}*{amount}#",
        "vodafone": f"*9*7*{phone}*{amount}#",
    }


def transfer_kb(codes: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Vodafone Code",
            copy_text=CopyTextButton(text=codes["vodafone"]),
        )],
        [InlineKeyboardButton(
            text="Orange Code",
            copy_text=CopyTextButton(text=codes["orange"]),
        )],
        [InlineKeyboardButton(
            text="Etisalat Code",
            copy_text=CopyTextButton(text=codes["etisalat"]),
        )],
    ])


def transfer_text(phone: str, amount: str) -> str:
    return (
        f"<blockquote><b>Transfer codes for: "
        f"<code>{phone}</code></b></blockquote>\n"
        f"<blockquote><b>Amount: {amount} EGP</b></blockquote>\n"
        "<blockquote>Click the buttons below to copy the "
        "transfer codes.</blockquote>"
    )


CLOCK_OFFSET_MINUTES = 0

CLOCK_DIGITS = [
    "𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿",
    "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫",
    "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵",
    "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗",
    "𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡",
    "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗",
    "⓪①②③④⑤⑥⑦⑧⑨",
    "⓿❶❷❸❹❺❻❼❽❾",
    "⓿⑴⑵⑶⑷⑸⑹⑺⑻⑼",
    "⓿⓵⓶⓷⓸⓹⓺⓻⓼⓽",
    "⓿🅰🅱🅲🅳🅴🅵🅶🅷🅸",
    "０１２３４５６７８９",
    "٠١٢٣٤٥٦٧٨٩",
    "۰۱۲۳۴۵۶۷۸۹",
    "⁰¹²³⁴⁵⁶⁷⁸⁹",
    "₀₁₂₃₄₅₆₇₈₉",
]

CLOCK_STYLE_LABELS = [
    "Sans Bold", "Sans", "Sans Italic", "Monospace",
    "Double Struck", "Serif Bold", "Circled White", "Circled Black",
    "Parenthesized", "Squared White", "Squared Black", "Full Width",
    "Eastern Arabic", "Persian", "Superscript", "Subscript",
]


def format_clock_time(hour24: int, minute: int, style: int) -> str:
    hour12 = hour24 % 12 or 12
    raw = f"{hour12}:{minute:02d}"
    digits = CLOCK_DIGITS[int(style) % len(CLOCK_DIGITS)]
    trans = str.maketrans("0123456789", digits[:10].ljust(10, digits[-1]))
    return raw.translate(trans)


def clock_text(style: int) -> str:
    import pytz
    cairo_tz = pytz.timezone("Africa/Cairo")
    now = datetime.now(cairo_tz) + timedelta(
        minutes=CLOCK_OFFSET_MINUTES
    )
    return format_clock_time(now.hour, now.minute, style)


def clean_clock_suffix(name: str) -> str:
    if not name:
        return ""
    all_digits = set("0123456789")
    for digits in CLOCK_DIGITS:
        all_digits.update(digits)
    digit_chars = "".join(all_digits)
    digit_class = re.escape(digit_chars)
    period = r"(?:\s*(?:[صم]|AM|PM))?"
    cleaned = re.sub(
        rf"\s+[{digit_class}]{{1,2}}:[{digit_class}]{{2}}{period}$",
        "", str(name), flags=re.IGNORECASE,
    )
    cleaned = re.sub(rf"\s+[{digit_class}]{{1,2}}$", "", cleaned)
    return cleaned.strip()


LIVE_BIO_NAMES = {"islam": "الإسلام", "christian": "المسيحية"}
LIVE_BIO_EMOJIS = {}
LIVE_BIO_INTERVAL_CHOICES = {
    15: "15 دقيقة", 30: "30 دقيقة", 60: "ساعة",
    180: "3 ساعات", 360: "6 ساعات", 720: "12 ساعة",
    1440: "24 ساعة",
}


def normalize_live_bio_religion(religion: str) -> str:
    value = str(religion or "").strip().lower()
    aliases = {
        "مسلم": "islam", "الإسلام": "islam", "اسلام": "islam",
        "مسيحي": "christian", "المسيحية": "christian",
    }
    value = aliases.get(value, value)
    return value if value in LIVE_BIO_NAMES else ""


LIVE_BIO_DEFAULTS = {
    "islam": [
        "اللَّهُمَّ إِنِّي أَسْأَلُكَ رِضَاكَ وَالْجَنَّةَ.",
        "اللَّهُمَّ بَارِكْ لِي فِي رِزْقِي، وَوَسِّعْ عَلَيَّ مِنْ فَضْلِكَ.",
        "اللَّهُمَّ اغْفِرْ لِي ذُنُوبِي، وَارْحَمْنِي.",
        "اللَّهُمَّ اغْفِرْ لِوَالِدَيَّ، وَارْحَمْهُمَا.",
        "اللَّهُمَّ عَافِنِي فِي بَدَنِي، وَسَمْعِي، وَبَصَرِي.",
    ],
    "christian": [
        "الرَّبُّ رَاعِيَّ فَلَا يُعْوِزُنِي شَيْءٌ.",
        "الرَّبُّ نُورِي وَخَلَاصِي، مِمَّنْ أَخَافُ؟",
        "اَلرَّبُّ قُوَّتِي وَتَرْنِيمَتِي.",
        "تَوَكَّلْ عَلَى الرَّبِّ بِكُلِّ قَلْبِكَ.",
        "سَلَامُ اللهِ يَحْفَظُ قُلُوبَكُمْ.",
    ],
}


def bio_verses_for(religion: str) -> list:
    religion = normalize_live_bio_religion(religion)
    return [
        str(item).strip() for item in LIVE_BIO_DEFAULTS.get(religion, [])
        if str(item).strip() and len(str(item).strip()) <= 140
    ]


_impersonation_cache: dict[int, float] = {}
_IMPERSONATION_COOLDOWN = 300


def can_impersonate_now(owner_id: int) -> tuple[bool, int]:
    now = time.time()
    last = _impersonation_cache.get(owner_id)
    if last and now - last < _IMPERSONATION_COOLDOWN:
        return False, int(_IMPERSONATION_COOLDOWN - (now - last))
    return True, 0


def mark_impersonated(owner_id: int) -> None:
    _impersonation_cache[owner_id] = time.time()


BUSINESS_COMMANDS = {
    "انتحال": "impersonate",
    "رجوع": "restore",
    "مسح": "delete_messages",
    "سبام": "spam",
    "تكرار": "repeat",
    "رد": "reply",
    "فتح الكول": "open_call",
    "قفل الكول": "close_call",
}


def parse_business_command(text: str) -> tuple[str | None, list[str]]:
    if not text:
        return None, []
    clean = text.strip()
    if clean.startswith("."):
        clean = clean[1:]
    parts = clean.split()
    if not parts:
        return None, []
    if len(parts) >= 2:
        two_word = f"{parts[0]} {parts[1]}"
        if two_word in BUSINESS_COMMANDS:
            return BUSINESS_COMMANDS[two_word], parts[2:]
    if parts[0] in BUSINESS_COMMANDS:
        return BUSINESS_COMMANDS[parts[0]], parts[1:]
    return None, []


async def get_business_owner(
    bot: Bot, business_connection_id: str
) -> int | None:
    row = await db.fetchrow(
        "SELECT user_id FROM business "
        "WHERE connection_id=$1 AND enabled=TRUE",
        business_connection_id,
    )
    return row["user_id"] if row else None


async def save_original_profile(
    bot: Bot, owner_id: int, connection_id: str
) -> bool:
    try:
        current = await bot.get_business_connection(
            business_connection_id=connection_id
        )
        if (
            not current
            or not current.is_enabled
            or not current.user
            or current.user.id != owner_id
        ):
            return False

        user = current.user

        original_bio = None
        bio_captured = False
        try:
            profile = await bot.get_chat(owner_id)
            profile_bio = getattr(profile, "bio", None)
            if profile_bio is not None:
                original_bio = str(profile_bio)
                bio_captured = True
        except Exception:
            pass

        original_photo_file_id = None
        photo_captured = False
        try:
            photos = await bot.get_user_profile_photos(
                user_id=owner_id, limit=1
            )
            if photos.total_count and photos.photos:
                original_photo_file_id = photos.photos[0][-1].file_id
                photo_captured = True
        except Exception:
            pass

        await db.execute(
            """
            INSERT INTO business_profile_snapshots
                (connection_id, owner_id, original_first_name,
                 original_last_name, original_bio, bio_captured,
                 original_photo_file_id, photo_captured)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (connection_id) DO NOTHING
            """,
            connection_id,
            owner_id,
            user.first_name or "",
            user.last_name or "",
            original_bio,
            bio_captured,
            original_photo_file_id,
            photo_captured,
        )
        return bool(await db.fetchval(
            """
            SELECT 1 FROM business_profile_snapshots
            WHERE connection_id=$1 AND owner_id=$2
            """,
            connection_id,
            owner_id,
        ))
    except Exception:
        log.exception(
            "business_profile.snapshot.save.failed", owner_id=owner_id
        )
        return False


async def get_target_user_info(bot: Bot, user_id: int) -> dict:
    info = {
        "first_name": "",
        "last_name": "",
        "bio": None,
        "photo_file_id": None,
    }

    try:
        chat = await bot.get_chat(user_id)
        info["first_name"] = getattr(chat, "first_name", "") or ""
        info["last_name"] = getattr(chat, "last_name", "") or ""
        info["bio"] = getattr(chat, "bio", None)
    except Exception:
        pass

    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if photos.total_count and photos.photos:
            info["photo_file_id"] = photos.photos[0][-1].file_id
    except Exception:
        pass

    return info


def _truncate_bio(bio: str) -> str:
    if not bio:
        return ""
    units = 0
    result = []
    for ch in bio:
        char_units = len(ch.encode('utf-16-le')) // 2
        if units + char_units > 140:
            break
        result.append(ch)
        units += char_units
    return "".join(result)


async def impersonate_user(
    bot: Bot,
    owner_id: int,
    connection_id: str,
    target_first_name: str,
    target_last_name: str,
    target_bio: str | None = None,
    target_photo_file_id: str | None = None,
) -> tuple[bool, str]:
    allowed, wait = can_impersonate_now(owner_id)
    if not allowed:
        return False, f"⚠️ انتظر {wait} ثانية قبل الانتحال مرة أخرى."

    try:
        snapshot_exists = await db.fetchval(
            """
            SELECT 1 FROM business_profile_snapshots
            WHERE owner_id=$1 AND connection_id=$2
            """,
            owner_id,
            connection_id,
        )
        if not snapshot_exists:
            if not await save_original_profile(bot, owner_id, connection_id):
                return False, "❌ تعذّر حفظ الملف الشخصي الأصلي."

        current = await bot.get_business_connection(
            business_connection_id=connection_id
        )
        if (
            not current
            or not current.is_enabled
            or not current.user
            or current.user.id != owner_id
        ):
            return False, "❌ اتصال Business غير مفعّل."

        rights = current.rights
        if not rights:
            return False, "❌ لا توجد صلاحيات."

        if not rights.can_edit_name:
            return False, "❌ تحتاج صلاحية «تعديل الاسم»."

        try:
            await bot.set_business_account_name(
                business_connection_id=connection_id,
                first_name=target_first_name or " ",
                last_name=target_last_name or "",
            )
        except Exception as exc:
            log.exception("impersonate.name.failed")
            return False, f"❌ فشل تغيير الاسم: {str(exc)[:100]}"

        mark_impersonated(owner_id)

        photo_note = ""
        if target_photo_file_id and getattr(
            rights, "can_edit_profile_photo", False
        ):
            tmp_path = None
            try:
                file = await bot.get_file(target_photo_file_id)
                tmp_path = os.path.join(
                    tempfile.gettempdir(), f"imp_{owner_id}.jpg"
                )
                await bot.download_file(
                    file.file_path, destination=tmp_path
                )
                photo_input = InputProfilePhotoStatic(
                    photo=FSInputFile(tmp_path)
                )
                await bot.set_business_account_profile_photo(
                    business_connection_id=connection_id,
                    photo=photo_input,
                )
                photo_note = " + الصورة"
            except Exception as exc:
                log.warning(
                    "impersonate.photo.failed", error=str(exc)[:120]
                )
                photo_note = " (فشلت الصورة)"
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        bio_note = ""
        snapshot = await db.fetchrow(
            """
            SELECT original_bio, bio_captured
            FROM business_profile_snapshots
            WHERE owner_id=$1 AND connection_id=$2
            """,
            owner_id,
            connection_id,
        )
        can_edit_bio = getattr(rights, "can_edit_bio", False)
        if (
            target_bio is not None
            and snapshot
            and snapshot["bio_captured"]
            and can_edit_bio
        ):
            try:
                bio_to_set = _truncate_bio(target_bio or "")
                await bot.set_business_account_bio(
                    business_connection_id=connection_id,
                    bio=bio_to_set,
                )
                bio_note = " + البايو"
            except Exception as exc:
                log.warning(
                    "impersonate.bio.failed", error=str(exc)[:100]
                )
                bio_note = " (فشل البايو)"

        return True, f"✅ تم الانتحال (الاسم{photo_note}{bio_note})."

    except Exception as exc:
        log.exception("business_profile.impersonation.failed")
        return False, f"❌ خطأ غير متوقع: {str(exc)[:100]}"


async def restore_original_profile(
    bot: Bot, owner_id: int, connection_id: str | None = None
) -> tuple[bool, str]:
    if connection_id:
        snapshot = await db.fetchrow(
            """
            SELECT connection_id, original_first_name, original_last_name,
                   original_bio, bio_captured,
                   original_photo_file_id, photo_captured
            FROM business_profile_snapshots
            WHERE owner_id=$1 AND connection_id=$2
            """,
            owner_id,
            connection_id,
        )
    else:
        snapshot = await db.fetchrow(
            """
            SELECT connection_id, original_first_name, original_last_name,
                   original_bio, bio_captured,
                   original_photo_file_id, photo_captured
            FROM business_profile_snapshots
            WHERE owner_id=$1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            owner_id,
        )

    if not snapshot:
        return False, "⚠️ لم تقم بأي انتحال سابق."

    conn_id = snapshot["connection_id"]

    try:
        current = await bot.get_business_connection(
            business_connection_id=conn_id
        )
        if not current or not current.is_enabled:
            return False, "❌ اتصال Business غير مفعّل."
        rights = current.rights
    except Exception:
        return False, "❌ تعذّر التحقق من الاتصال."

    errors = []

    try:
        await bot.set_business_account_name(
            business_connection_id=conn_id,
            first_name=snapshot["original_first_name"] or " ",
            last_name=snapshot["original_last_name"] or "",
        )
    except Exception as exc:
        log.exception("restore.name.failed")
        errors.append(f"الاسم: {str(exc)[:60]}")

    if snapshot["photo_captured"]:
        try:
            if snapshot["original_photo_file_id"]:
                await bot.set_business_account_profile_photo(
                    business_connection_id=conn_id,
                    photo=snapshot["original_photo_file_id"],
                )
            else:
                await bot.set_business_account_profile_photo(
                    business_connection_id=conn_id,
                    photo="",
                )
        except Exception as exc:
            log.warning("restore.photo.failed", error=str(exc)[:100])
            errors.append(f"الصورة: {str(exc)[:60]}")

    if snapshot["bio_captured"] and getattr(
        rights, "can_edit_bio", False
    ):
        try:
            await bot.set_business_account_bio(
                business_connection_id=conn_id,
                bio=snapshot["original_bio"] or "",
            )
        except Exception as exc:
            log.warning("restore.bio.failed", error=str(exc)[:100])
            errors.append(f"البايو: {str(exc)[:60]}")

    if not any("الاسم" in e for e in errors):
        await db.execute(
            """
            DELETE FROM business_profile_snapshots
            WHERE owner_id=$1 AND connection_id=$2
            """,
            owner_id,
            conn_id,
        )
        _impersonation_cache.pop(owner_id, None)

    if errors:
        return True, "⚠️ تم الرجوع جزئياً:\n• " + "\n• ".join(errors)
    return True, "✅ تم الرجوع لإعداداتك الأصلية (الاسم + الصورة + البايو)."


async def download_user_photo(
    bot: Bot, user_id: int
) -> str | None:
    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if not photos.total_count or not photos.photos:
            return None

        file_id = photos.photos[0][-1].file_id
        file = await bot.get_file(file_id)

        tmp_path = os.path.join(
            tempfile.gettempdir(), f"impersonate_{user_id}.jpg"
        )
        await bot.download_file(file.file_path, destination=tmp_path)
        return tmp_path
    except Exception:
        return None


async def get_user_bio(bot: Bot, user_id: int) -> str | None:
    try:
        chat = await bot.get_chat(user_id)
        return getattr(chat, "bio", None)
    except Exception:
        return None


async def delete_business_messages_range(
    bot: Bot, business_connection_id: str, message_ids: list[int]
) -> bool:
    try:
        await bot.delete_business_messages(
            business_connection_id=business_connection_id,
            message_ids=message_ids,
        )
        return True
    except Exception:
        return False


async def send_business_message(
    bot: Bot,
    business_connection_id: str,
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
) -> bool:
    try:
        kwargs = {
            "business_connection_id": business_connection_id,
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id
        await bot.send_message(**kwargs)
        return True
    except Exception:
        return False


def parse_spam_command(text: str) -> tuple[str | None, int | None]:
    if not text:
        return None, None

    clean = text.strip()
    if clean.startswith("."):
        clean = clean[1:]

    clean = re.sub(r'\s+', ' ', clean)
    parts = clean.split(' ')

    if len(parts) < 3:
        return None, None

    command = parts[0]
    if command not in ("تكرار", "سبام"):
        return None, None

    try:
        count = int(parts[-1])
        if not 1 <= count <= 100:
            return None, None
        message = " ".join(parts[1:-1])
        if not message:
            return None, None
        return message, count
    except (ValueError, IndexError):
        return None, None


def parse_delete_command(text: str) -> int | None:
    if not text:
        return None

    clean = text.strip()
    if clean.startswith("."):
        clean = clean[1:]

    parts = clean.split()
    if len(parts) != 2 or parts[0] != "مسح":
        return None

    try:
        count = int(parts[1])
        if 1 <= count <= 100:
            return count
    except ValueError:
        pass
    return None


def get_font_style_html(text: str, style: int) -> str:
    safe_text = safe_html(text)
    if style == 1:
        return f"<b>{safe_text}</b>"
    elif style == 2:
        return f"<i>{safe_text}</i>"
    elif style == 3:
        return f"<u>{safe_text}</u>"
    elif style == 4:
        return f"<s>{safe_text}</s>"
    elif style == 5:
        return f"<tg-spoiler>{safe_text}</tg-spoiler>"
    return safe_text


def font_menu_kb(current_style: int) -> InlineKeyboardMarkup:
    styles = [
        (0, "بدون تنسيق"),
        (1, "خط عريض (Bold)"),
        (2, "خط مائل (Italic)"),
        (3, "خط تحته خط (Underline)"),
        (4, "خط مشطوب (Strikethrough)"),
        (5, "خط مخفي (Spoiler)"),
    ]
    rows = []
    for style_id, label in styles:
        mark = "✅ " if style_id == current_style else "⬜ "
        rows.append([InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"font_set_{style_id}"
        )])
    rows.append([InlineKeyboardButton(
        text="رجوع", callback_data="home"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def check_call_permission(
    bot: Bot, connection_id: str
) -> tuple[bool, str]:
    try:
        current = await bot.get_business_connection(
            business_connection_id=connection_id
        )
        if not current.is_enabled:
            return False, "الاتصال غير مفعّل."

        rights = current.rights
        if not rights:
            return False, "لا توجد صلاحيات."

        if not rights.can_reply:
            return False, "تحتاج صلاحية «إدارة الرسائل» لفتح/قفل المكالمات."

        return True, ""
    except Exception as e:
        return False, f"خطأ في التحقق: {str(e)[:100]}"


async def close_group_call(
    bot: Bot, chat_id: int
) -> tuple[bool, str]:
    return False, "عذراً، Telegram Bot API لا يدعم إغلاق المكالمات الجماعية مباشرة."


async def open_group_call(
    bot: Bot, chat_id: int
) -> tuple[bool, str]:
    return False, "عذراً، Telegram Bot API لا يدعم فتح المكالمات الجماعية مباشرة."