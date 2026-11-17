
# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile as _tempfile
import unicodedata
from datetime import datetime, timedelta, timezone

import structlog
import pytz
from cachetools import TTLCache
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    ChatMemberUpdated,
    CopyTextButton,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings
from Database import (
    db,
    ensure_settings,
    is_bot_user_blocked,
    audit,
    ensure_banned_seeded,
)
from plugins.Temporary import (
    BOT_HOME_EMOJI,
    BTN_STYLE_MAP,
    WELCOME_COOLDOWN,
    clickable_user_link,
    edit_or_replace,
    fancy_success,
    fancy_error,
    help_kb,
    parse_transfer_request,
    public_kb,
    referral_link,
    safe_html,
    transfer_codes,
    transfer_kb,
    transfer_text,
    try_calculate,
    get_font_style_html,
)

log = structlog.get_logger(__name__)
router = Router(name="inform")
UTC = timezone.utc
CAIRO_TZ = pytz.timezone("Africa/Cairo")


_banned_words_cache: TTLCache = TTLCache(maxsize=2000, ttl=300)
_force_channels_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)
_settings_cache: TTLCache = TTLCache(maxsize=1000, ttl=120)


async def _get_banned_words_cached(
    owner_id: int, kind: str = "swear"
) -> list[dict]:
    key = (owner_id, kind)
    cached = _banned_words_cache.get(key)
    if cached is not None:
        return cached
    rows = await db.fetch(
        "SELECT word, pattern, is_regex FROM banned_words "
        "WHERE owner_id=$1 AND kind=$2",
        owner_id, kind,
    )
    words = []
    for r in rows:
        words.append({
            "word": (r["word"] or "").lower(),
            "pattern": r["pattern"] or "",
            "is_regex": bool(r["is_regex"]),
        })
    _banned_words_cache[key] = words
    return words


def invalidate_banned_words(owner_id: int) -> None:
    for k in list(_banned_words_cache.keys()):
        if k[0] == owner_id:
            _banned_words_cache.pop(k, None)


async def _get_force_channels_cached(owner_id: int, scope: str) -> list:
    key = (owner_id, scope)
    cached = _force_channels_cache.get(key)
    if cached is not None:
        return cached
    rows = await db.fetch(
        "SELECT channel, chat_id, title FROM force_channels "
        "WHERE owner_id=$1 AND scope=$2 ORDER BY id",
        owner_id, scope,
    )
    result = [(r["channel"], r["chat_id"], r["title"]) for r in rows]
    _force_channels_cache[key] = result
    return result


def invalidate_force_channels(owner_id: int | None = None) -> None:
    if owner_id is None:
        _force_channels_cache.clear()
    else:
        for key in list(_force_channels_cache.keys()):
            if key[0] == owner_id:
                _force_channels_cache.pop(key, None)


_SWEAR_SEED_FLAG_INFORM: dict[int, bool] = {}


async def _ensure_swear_seeded(owner_id: int) -> None:
    if _SWEAR_SEED_FLAG_INFORM.get(owner_id):
        return
    await ensure_settings(owner_id)
    existing_words: set[str] = set()
    rows = await db.fetch(
        "SELECT LOWER(word) AS w FROM banned_words "
        "WHERE owner_id=$1 AND kind='swear'",
        owner_id,
    )
    for r in rows:
        existing_words.add(r["w"] or "")
    from Database import DEFAULT_SWEAR_WORDS
    for w in DEFAULT_SWEAR_WORDS:
        word = w.lower()
        if word in existing_words:
            continue
        try:
            await db.execute(
                "INSERT INTO banned_words(owner_id, word, kind) "
                "VALUES($1, $2, 'swear')",
                owner_id, word,
            )
        except Exception:
            pass
    _SWEAR_SEED_FLAG_INFORM[owner_id] = True


BOT_FORCE_SUB_TEXT = (
    "<b>⚠️ خطوة واحدة تفصلك عن البدء</b>\n\n"
    "اشترك في القنوات المطلوبة ثم اضغط زر التحقق."
)

ACCOUNT_FORCE_SUB_TEXT = (
    "<b>⚠️ لإكمال المحادثة</b>\n\n"
    "اشترك أولاً في القنوات التالية."
)


REQUIRED_PERMISSIONS = [
    ("can_reply", "إدارة الرسائل", "💬"),
    ("can_edit_name", "تعديل الاسم", "✏️"),
    ("can_edit_bio", "تعديل البايو", "📝"),
]


def missing_permissions(rights) -> list[tuple[str, str, str]]:
    if rights is None:
        return list(REQUIRED_PERMISSIONS)
    missing = []
    for field, label, icon in REQUIRED_PERMISSIONS:
        if not bool(getattr(rights, field, False)):
            missing.append((field, label, icon))
    return missing


def permissions_warning_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ فتح الإعدادات", url="tg://settings/edit")],
        [InlineKeyboardButton(text="🔄 تحقّق مرة أخرى", callback_data="recheck_permissions")],
    ])


def permissions_warning_text(missing: list[tuple[str, str, str]]) -> str:
    lines = [
        "<b>⚠️ لتفعيل البوت بالكامل</b>",
        "لازم تعطيني <b>كل الأذونات</b> دي:",
        "",
    ]
    for field, label, icon in missing:
        lines.append(f"{icon} <b>{label}</b>")
    lines.extend([
        "",
        "<b>📌 الخطوات:</b>",
        "<b>1️⃣ اضغط زر فتح الإعدادات</b>",
        "<b>2️⃣ اختار Telegram Business</b>",
        "<b>3️⃣ اضغط على المحادثة الآلية</b>",
        "<b>4️⃣ فعّل كل الأذونات</b>",
        "<b>5️⃣ ارجع للبوت واضغط تحقّق مرة أخرى</b>",
    ])
    return "\n".join(lines)


MEDIA_TYPE_LABELS = {
    "photo": "صورة",
    "sticker": "ملصق",
    "custom_emoji": "ملصق مميز (Custom Emoji)",
    "premium_sticker": "ملصق بريميوم",
    "video": "فيديو",
    "voice": "رسالة صوتية",
    "audio": "ملف صوتي",
    "document": "ملف",
    "animation": "رسالة متحركة",
    "video_note": "فيديو نوت",
    "text": "نص",
}


def _extract_custom_emoji_ids(message: Message) -> list[str]:
    ids: list[str] = []
    for entities in (
        getattr(message, "entities", None),
        getattr(message, "caption_entities", None),
    ):
        if not entities:
            continue
        for ent in entities:
            ent_type = str(getattr(ent, "type", "") or "")
            if ent_type == "custom_emoji":
                eid = getattr(ent, "custom_emoji_id", None)
                if eid:
                    ids.append(str(eid))
    return ids


def _detect_media_type_full(
    message: Message,
) -> tuple[str | None, str | None]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.document:
        return "document", message.document.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.sticker:
        sticker = message.sticker
        if getattr(sticker, "is_custom_emoji", False):
            return "custom_emoji", sticker.file_id
        if getattr(sticker, "is_premium", False):
            return "premium_sticker", sticker.file_id
        return "sticker", sticker.file_id
    emoji_ids = _extract_custom_emoji_ids(message)
    if emoji_ids:
        return "custom_emoji", emoji_ids[0]
    return None, None


def tracked_message_display(
    text_content: str, media_type: str, extra: str = ""
) -> str:
    label = MEDIA_TYPE_LABELS.get(media_type or "")
    text_content = (text_content or "").strip()
    parts: list[str] = []
    if label:
        parts.append(f"<b>{safe_html(label)}</b>")
    if text_content:
        parts.append(f"<b>النص المرفق:</b>\n{safe_html(text_content)}")
    elif label:
        parts.append("<i>(بدون نص مرفق)</i>")
    else:
        parts.append("<i>(بدون محتوى)</i>")
    if extra:
        parts.append(extra)
    return "\n".join(parts)


async def required_subs(
    bot: Bot, user_id: int, owner_id: int, scope: str = "account",
):
    try:
        rows = await _get_force_channels_cached(owner_id, scope)
    except Exception:
        return []
    if not rows:
        return []

    async def check_one(channel: str, chat_id: int, title: str):
        target = chat_id if chat_id else channel
        try:
            member = await asyncio.wait_for(
                bot.get_chat_member(target, user_id), timeout=6,
            )
            status = str(
                getattr(member.status, "value", member.status)
            ).lower()
            if status in ("left", "kicked"):
                return (channel, chat_id, title)
        except asyncio.TimeoutError:
            return None
        except Exception as exc:
            err = str(exc).lower()
            if (
                "member list is inaccessible" in err
                or "bot is not a member" in err
                or "chat not found" in err
            ):
                log.error(
                    "required_subs.bot_not_admin",
                    channel=channel,
                    hint=(
                        "أضف البوت كأدمن في القناة مع صلاحيات "
                        "«دعوة المستخدمين» أو «حذف الرسائل»."
                    ),
                )
                return None
            if (
                "user_not_participant" in err
                or "user not participant" in err
                or "participant_id_invalid" in err
            ):
                return (channel, chat_id, title)
            log.debug(
                "required_subs.skipped", channel=channel, err=err[:80],
            )
        return None

    results = await asyncio.gather(
        *[check_one(ch, cid, t) for ch, cid, t in rows]
    )
    return [r for r in results if r]


def _channel_url(channel: str, chat_id: int) -> str | None:
    ch = str(channel or "")
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    if chat_id:
        return f"https://t.me/c/{str(chat_id).replace('-100', '', 1)}"
    return None


def bot_sub_kb(channels) -> InlineKeyboardMarkup:
    rows = []
    for ch, cid, title in channels:
        url = _channel_url(ch, cid)
        if url:
            rows.append([InlineKeyboardButton(
                text=f"اشترك في {title}", url=url,
            )])
    rows.append([InlineKeyboardButton(
        text="تحقق من الاشتراك", callback_data="bot_force_verify",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sub_kb(channels) -> InlineKeyboardMarkup | None:
    rows = []
    for ch, cid, title in channels:
        url = _channel_url(ch, cid)
        if url:
            rows.append([InlineKeyboardButton(
                text=f"اشترك في {title}", url=url,
            )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def paid_access_allowed(user_id: int) -> bool:
    if user_id == settings.owner_id:
        return True
    mode = await db.fetchval(
        "SELECT bot_access_mode FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    if mode != "paid":
        return True
    row = await db.fetchrow(
        "SELECT expires_at FROM bot_subscriptions WHERE user_id=$1",
        user_id,
    )
    if not row:
        return False
    return row["expires_at"] > datetime.now(UTC)


async def activate_first_trial(user_id: int):
    if user_id == settings.owner_id:
        return None
    mode = await db.fetchval(
        "SELECT bot_access_mode FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    if mode != "paid":
        return None
    hours = await db.fetchval(
        "SELECT bot_trial_hours FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    hours = max(1, int(hours or 24))
    expires = datetime.now(UTC) + timedelta(hours=hours)
    result = await db.execute(
        """INSERT INTO bot_subscriptions(user_id, expires_at, trial, created_at)
           VALUES($1, $2, TRUE, NOW())
           ON CONFLICT(user_id) DO NOTHING""",
        user_id, expires,
    )
    if result == "INSERT 0 1":
        return hours, expires
    return None


async def send_paid_gate(target) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="𓏺 فيجا .", url=settings.developer_url or "https://t.me/Topvega",
        )
    ]])
    await target.answer(
        "💬 عذراً، لا يوجد لديك اشتراك مفعل في البوت.\n\n"
        "👤 لمعرفة الأسعار راسل المطور.",
        reply_markup=kb, parse_mode="HTML",
    )


async def get_profile_details(
    bot: Bot, user_id: int,
    fallback_name: str = "", fallback_username: str = "",
) -> tuple[str, str, str]:
    name = fallback_name or "غير معروف"
    username = fallback_username or ""
    try:
        chat = await bot.get_chat(user_id)
        name = (
            getattr(chat, "full_name", None)
            or getattr(chat, "first_name", None) or name
        )
        username = getattr(chat, "username", None) or username
    except Exception:
        pass
    username_text = f"@{safe_html(username)}" if username else "غير متوفر"
    return safe_html(name), username_text, str(user_id)


def profile_copy_kb(name: str, username_text: str, user_id_text: str):
    username_value = (
        username_text if username_text and username_text != "غير متوفر"
        else "غير متوفر"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="نسخ اليوزر",
                copy_text=CopyTextButton(text=username_value),
            ),
            InlineKeyboardButton(
                text="نسخ الآيدي",
                copy_text=CopyTextButton(text=str(user_id_text)),
            ),
        ],
        [InlineKeyboardButton(
            text="نسخ الاسم", copy_text=CopyTextButton(text=name),
        )],
    ])


async def send_profile_card(
    message: Message, target_user_id: int,
    fallback_name: str = "", fallback_username: str = "",
) -> None:
    name, username_text, user_id_text = await get_profile_details(
        message.bot, target_user_id, fallback_name, fallback_username
    )
    caption = (
        "<b>معلومات الحساب</b>\n\n"
        f"الاسم: {name}\n"
        f"اليوزر: {username_text}\n"
        f"الايدي: <code>{user_id_text}</code>"
    )
    if message.business_connection_id:
        try:
            stats = await db.get_customer_stats(
                message.business_connection_id, target_user_id
            )
            if stats and (stats.get("total_messages") or 0) > 0:
                first_seen = stats.get("first_seen")
                first_seen_text = (
                    first_seen.strftime("%Y-%m-%d")
                    if first_seen else "غير معروف"
                )
                caption += (
                    "\n\n<b>📊 إحصائيات العميل:</b>\n"
                    f"الرسائل: <b>{stats.get('total_messages', 0)}</b>\n"
                    f"مرات الكتم: <b>{stats.get('mute_count', 0)}</b>\n"
                    f"عدد المخالفات: <b>{stats.get('violations_count', 0)}</b>\n"
                    f"آخر كلمة ممنوعة: "
                    f"<b>{safe_html(stats.get('last_banned_word') or 'لا يوجد')}</b>\n"
                    f"أول ظهور: <b>{first_seen_text}</b>"
                )
        except Exception:
            log.exception("profile.stats.failed")
    reply_markup = profile_copy_kb(name, username_text, user_id_text)
    try:
        photos = await message.bot.get_user_profile_photos(
            user_id=target_user_id, limit=1
        )
        if photos.total_count and photos.photos:
            await message.bot.send_photo(
                chat_id=message.chat.id,
                photo=photos.photos[0][-1].file_id,
                caption=caption, reply_markup=reply_markup,
                business_connection_id=message.business_connection_id,
                parse_mode="HTML",
            )
            return
    except Exception:
        log.exception("profile.photo.failed", target=target_user_id)
    await message.bot.send_message(
        chat_id=message.chat.id, text=caption,
        reply_markup=reply_markup,
        business_connection_id=message.business_connection_id,
        parse_mode="HTML",
    )


def build_connection_message(
    kind: str, user_id: int, name: str, username: str, connection_id: str,
) -> tuple[str, InlineKeyboardMarkup]:
    safe_name = safe_html(name or "بدون اسم")
    safe_username = safe_html(username or "")
    safe_cid = safe_html(connection_id or "")
    clickable_name = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
    username_line = (
        f'<a href="https://t.me/{safe_username}">@{safe_username}</a>'
        if username else "غير متوفر"
    )
    if kind == "connected":
        title = "✅ <b>تم ربط حساب جديد بالبوت</b>"
    else:
        title = "⚠️ <b>تم إلغاء ربط حساب بالبوت</b>"
    text = (
        f"{title}\n\n"
        f"🆔 <b>الآيدي:</b> <code>{user_id}</code>\n"
        f"👤 <b>الاسم:</b> {clickable_name}\n"
        f"🔗 <b>اليوزر:</b> {username_line}\n"
        f"🔑 <b>معرف الاتصال:</b> <code>{safe_cid}</code>"
    )
    rows = [[
        InlineKeyboardButton(
            text=f"👤 {name[:30] or 'المستخدم'}",
            url=f"tg://user?id={user_id}",
        ),
        InlineKeyboardButton(
            text="📋 نسخ الآيدي",
            copy_text=CopyTextButton(text=str(user_id)),
        ),
    ]]
    if username:
        rows.append([InlineKeyboardButton(
            text=f"🔗 @{username[:30]}", url=f"https://t.me/{username}",
        )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_connection_notification(
    bot: Bot, kind: str, user_id: int, name: str,
    username: str, connection_id: str,
) -> None:
    if user_id == settings.owner_id:
        return

    text, kb = build_connection_message(
        kind, user_id, name, username, connection_id
    )
    try:
        await bot.send_message(
            settings.owner_id, text,
            reply_markup=kb, parse_mode="HTML",
            disable_web_page_preview=True,
        )
        log.info(
            "connection.notification.sent",
            kind=kind, target=settings.owner_id, about=user_id,
        )
    except Exception:
        log.exception(
            "connection.notification.failed",
            kind=kind, target=settings.owner_id, about=user_id,
        )


_MEDIA_EXT = {
    "photo": "jpg", "video": "mp4", "animation": "mp4",
    "voice": "ogg", "audio": "mp3", "document": "bin",
    "video_note": "mp4", "sticker": "webp",
    "premium_sticker": "webp", "custom_emoji": "webp",
}


def _detect_media_type(message: Message) -> tuple[str | None, str | None]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.document:
        return "document", message.document.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    return None, None


async def _download_business_media(
    bot: Bot, message: Message
) -> tuple[str | None, str | None]:
    media_type, file_id = _detect_media_type_full(message)
    if not file_id or not media_type:
        return None, None
    if media_type == "custom_emoji":
        return None, media_type
    if media_type in ("sticker", "premium_sticker"):
        return None, media_type
    ext = _MEDIA_EXT.get(media_type, "bin")
    path = None
    try:
        fd, path = _tempfile.mkstemp(
            prefix="biz_media_", suffix=f".{ext}",
            dir=_tempfile.gettempdir(),
        )
        os.close(fd)
        try:
            await bot.download(file_id, destination=path)
        except Exception:
            try:
                file = await bot.get_file(file_id)
                await bot.download_file(file.file_path, destination=path)
            except Exception:
                log.warning("business_media.download.failed",
                            media_type=media_type)
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
                return None, media_type
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            return None, media_type
        return path, media_type
    except Exception:
        log.exception("business_media.download.unexpected")
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return None, media_type


async def _send_media_from_file(
    bot: Bot, target: int, path: str, media_type: str,
    header: str, kb: InlineKeyboardMarkup | None, source: Message,
) -> bool:
    file_obj = FSInputFile(path)
    caption = header + (
        "\n\n💬 " + safe_html((source.caption or "")[:500])
        if source.caption else ""
    )
    try:
        if media_type == "photo":
            await bot.send_photo(target, file_obj, caption=caption,
                                 reply_markup=kb, parse_mode="HTML")
        elif media_type == "video":
            await bot.send_video(target, file_obj, caption=caption,
                                 reply_markup=kb, parse_mode="HTML")
        elif media_type == "animation":
            await bot.send_animation(target, file_obj, caption=caption,
                                     reply_markup=kb, parse_mode="HTML")
        elif media_type == "voice":
            await bot.send_voice(target, file_obj, caption=header,
                                 reply_markup=kb, parse_mode="HTML")
        elif media_type == "audio":
            await bot.send_audio(target, file_obj, caption=header,
                                 reply_markup=kb, parse_mode="HTML")
        elif media_type == "document":
            await bot.send_document(target, file_obj, caption=caption,
                                    reply_markup=kb, parse_mode="HTML")
        elif media_type == "video_note":
            await bot.send_video_note(target, file_obj)
            await bot.send_message(target, header, reply_markup=kb,
                                   parse_mode="HTML")
        elif media_type in ("sticker", "premium_sticker", "custom_emoji"):
            await bot.send_sticker(target, file_obj)
            await bot.send_message(target, header, reply_markup=kb,
                                   parse_mode="HTML")
        else:
            await bot.send_document(target, file_obj, caption=caption,
                                    reply_markup=kb, parse_mode="HTML")
        return True
    except Exception as exc:
        log.warning("business_media.send_from_file.failed",
                    media_type=media_type, error=str(exc)[:200])
        return False


async def _send_via_copy_or_forward(
    bot: Bot, target: int, source: Message, header: str,
    kb: InlineKeyboardMarkup | None, try_forward: bool = True,
) -> bool:
    if not source.chat or not source.chat.id:
        return False
    if try_forward:
        try:
            await bot.forward_message(
                chat_id=target, from_chat_id=source.chat.id,
                message_id=source.message_id,
            )
            try:
                await bot.send_message(target, header,
                                       reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            return True
        except Exception as exc:
            log.warning("business_media.forward.failed",
                        error=str(exc)[:200])
    try:
        await bot.copy_message(
            chat_id=target, from_chat_id=source.chat.id,
            message_id=source.message_id,
        )
        try:
            await bot.send_message(target, header,
                                   reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        return True
    except Exception as exc:
        log.warning("business_media.copy.failed", error=str(exc)[:200])
    return False


async def _forward_any_business_message(
    bot: Bot, target: int, source: Message, header: str,
    kb: InlineKeyboardMarkup | None = None, try_forward: bool = True,
) -> bool:
    if try_forward and source.chat and source.chat.id:
        try:
            await bot.forward_message(
                chat_id=target, from_chat_id=source.chat.id,
                message_id=source.message_id,
            )
            try:
                await bot.send_message(target, header,
                                       reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            return True
        except Exception as exc:
            log.warning("business_media.forward.failed",
                        error=str(exc)[:200])

    downloaded_path, media_type = await _download_business_media(bot, source)

    if media_type in ("sticker", "premium_sticker"):
        _, file_id = _detect_media_type_full(source)
        if file_id:
            try:
                await bot.send_sticker(target, file_id)
                try:
                    await bot.send_message(
                        target, header, reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return True
            except Exception as exc:
                log.warning(
                    "business_media.sticker.send_failed",
                    error=str(exc)[:200], media_type=media_type,
                )

    if source.chat and source.chat.id:
        try:
            await bot.copy_message(
                chat_id=target, from_chat_id=source.chat.id,
                message_id=source.message_id,
            )
            try:
                await bot.send_message(target, header,
                                       reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            return True
        except Exception as exc:
            log.warning("business_media.copy.failed", error=str(exc)[:200])

    if downloaded_path and media_type:
        sent = await _send_media_from_file(
            bot, target, downloaded_path, media_type, header, kb, source,
        )
        try:
            os.remove(downloaded_path)
        except Exception:
            pass
        if sent:
            return True

    if media_type == "custom_emoji":
        emoji_ids = _extract_custom_emoji_ids(source)
        eid = emoji_ids[0] if emoji_ids else ""
        body = header + (
            f"\n\n🎴 <b>ملصق مميز (Custom Emoji)</b>"
            + (f"\n<code>{safe_html(eid)}</code>" if eid else "")
        )
        try:
            await bot.send_message(target, body, reply_markup=kb,
                                   parse_mode="HTML")
            return True
        except Exception:
            log.exception("custom_emoji.text.fallback.failed")

    if source.text and not media_type:
        try:
            await bot.send_message(
                target,
                header + "\n\n💬 <b>النص:</b>\n"
                + safe_html(source.text[:800]),
                reply_markup=kb, parse_mode="HTML",
            )
            return True
        except Exception:
            log.exception("business_text.send.failed")

    if source.caption and not media_type:
        try:
            await bot.send_message(
                target,
                header + "\n\n💬 <b>النص:</b>\n"
                + safe_html(source.caption[:800]),
                reply_markup=kb, parse_mode="HTML",
            )
            return True
        except Exception:
            pass

    try:
        if media_type:
            body = header + (
                f"\n\n<i>(وسيط: {safe_html(MEDIA_TYPE_LABELS.get(media_type, media_type))} "
                f"— تعذّر إرسال الملف)</i>"
            )
        else:
            body = header + "\n\n<i>(رسالة غير نصية)</i>"
        await bot.send_message(target, body, reply_markup=kb,
                               parse_mode="HTML")
        return True
    except Exception as exc:
        log.exception("business_media.text.fallback.failed",
                      error=str(exc)[:200])
        return False


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid != settings.owner_id and await is_bot_user_blocked(uid):
        await message.answer("⛔ تم حظرك من استخدام هذا البوت.")
        return
    is_first_start = False
    first_trial = None
    if uid != settings.owner_id:
        result = await db.execute(
            """INSERT INTO bot_users(user_id, first_name, username, first_seen)
               VALUES($1, $2, $3, NOW())
               ON CONFLICT(user_id) DO NOTHING""",
            uid,
            message.from_user.first_name or "",
            message.from_user.username or "",
        )
        is_first_start = result == "INSERT 0 1"
        if is_first_start:
            first_trial = await activate_first_trial(uid)
    if not await paid_access_allowed(uid):
        await send_paid_gate(message)
        return
    if is_first_start:
        await _notify_new_user(message, uid)
    enabled = await db.fetchval(
        "SELECT bot_force_enabled FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    if enabled:
        missing = await required_subs(
            message.bot, uid, settings.owner_id, scope="bot"
        )
        if missing:
            await message.answer(
                BOT_FORCE_SUB_TEXT,
                reply_markup=bot_sub_kb(missing), parse_mode="HTML",
            )
            return
    if first_trial:
        hours, expires = first_trial
        await message.answer(
            f"<b>🎉 تم تفعيل النسخة التجريبية لك</b>\n\n"
            f"🕐 المدة: <b>{hours} ساعة</b>\n"
            f"📅 تاريخ الانتهاء: <b>{expires.strftime('%Y-%m-%d %H:%M')}</b>",
            parse_mode="HTML",
        )
    name = safe_html(message.from_user.first_name or "صديقي")
    text = (
        f"<b>👋 أهلاً بك يا {name} في نظام الإدارة المتقدم.</b>\n\n"
        "👤 أنا مساعدك الشخصي لإدارة حسابك باحترافية."
    )
    image_id = await db.fetchval(
        "SELECT bot_welcome_image_file_id FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    kb = await public_kb(uid, message.bot)
    if image_id:
        await message.answer_photo(image_id, caption=text,
                                   reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    if settings.referral_enabled and uid != settings.owner_id:
        await _maybe_send_referral_info(message, uid)


async def _notify_new_user(message: Message, uid: int) -> None:
    try:
        user_number = await db.fetchval(
            "SELECT user_number FROM bot_users WHERE user_id=$1", uid
        ) or "?"
        text = (
            f"👤 <b>مستخدم جديد دخل البوت رقم {user_number}</b>\n\n"
            f"🆔 الآيدي: <code>{uid}</code>\n"
            f"👤 الاسم: "
            f"{clickable_user_link(uid, message.from_user.full_name)}\n"
            f"🔗 اليوزر: "
            + (
                f'<a href="https://t.me/{safe_html(message.from_user.username)}">'
                f'@{safe_html(message.from_user.username)}</a>'
                if message.from_user.username else "غير متوفر"
            )
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"👤 {message.from_user.first_name or 'المستخدم'}",
                url=f"tg://user?id={uid}",
            ),
        ]])
        await message.bot.send_message(
            settings.owner_id, text, reply_markup=kb, parse_mode="HTML",
        )
    except Exception:
        log.exception("new_user.notify.failed")


async def _maybe_send_referral_info(message: Message, uid: int) -> None:
    try:
        me = await message.bot.get_me()
        if not me.username:
            return
        link = referral_link(me.username, uid)
        count = await db.count_referrals(uid)
        remaining = max(0, settings.referral_target - count)
        text = (
            "<b>🎁 نظام الإحالة</b>\n\n"
            f"رابطك الخاص:\n<code>{safe_html(link)}</code>\n\n"
            f"عدد إحالاتك: <b>{count}</b>\n"
            f"الهدف: <b>{settings.referral_target}</b>\n"
            f"متبقي: <b>{remaining}</b>\n"
            f"المكافأة: <b>{settings.referral_reward_days} أيام</b>"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception:
        log.exception("referral.info.failed")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    is_owner = uid == settings.owner_id
    text = (
        "<b>📖 المساعدة</b>\n\n"
        "هذا البوت يساعدك على إدارة حساب Telegram Business الخاص بك.\n\n"
        "استخدم الأزرار بالأسفل للتنقل بين الأقسام."
    )
    await message.answer(text, reply_markup=help_kb(is_owner),
                         parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    await message.answer(
        "✅ تم إلغاء العملية الحالية.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="رجوع", callback_data="home")
        ]]),
        parse_mode="HTML",
    )


@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message) -> None:
    if not message.from_user:
        return
    if message.from_user.id != settings.owner_id:
        return
    new_value = not settings.maintenance_mode
    settings.maintenance_mode = new_value
    await db.execute(
        "UPDATE settings SET maintenance_enabled=$1, "
        "maintenance_message=$2 WHERE owner_id=$3",
        new_value, settings.maintenance_message, settings.owner_id,
    )
    db.cache_invalidate(f"setting:{settings.owner_id}:")
    await audit(message.from_user.id, "maintenance_toggle",
                value=new_value)
    status = "مفعّل ⚠️" if new_value else "معطّل ✅"
    await message.answer(f"<b>🛠 وضع الصيانة: {status}</b>",
                         parse_mode="HTML")


@router.callback_query(F.data == "help_start")
async def help_start(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    text = (
        "<b>🚀 البدء السريع</b>\n\n"
        "1️⃣ اضغط على زر <b>كيف يعمل البوت</b> من القائمة الرئيسية.\n"
        "2️⃣ اربط حساب Business الخاص بك.\n"
        "3️⃣ امنح البوت كل الأذونات المطلوبة.\n"
        "4️⃣ اضغط <b>لوحة التحكم</b> وابدأ الإدارة."
    )
    await edit_or_replace(
        call, text, help_kb(call.from_user.id == settings.owner_id)
    )
    await call.answer()


@router.callback_query(F.data == "help_link")
async def help_link(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    try:
        me = await call.bot.get_me()
        username = f"@{me.username}" if me.username else "يوزر البوت"
    except Exception:
        username = "يوزر البوت"
    text = (
        "<b>🔗 كيفية الربط</b>\n\n"
        f"1️⃣ انسخ يوزر البوت: <code>{safe_html(username)}</code>\n"
        "2️⃣ اذهب إلى إعدادات Telegram Business.\n"
        "3️⃣ اختر <b>المحادثة الآلية</b>.\n"
        "4️⃣ الصق يوزر البوت.\n"
        "5️⃣ امنح كل الأذونات."
    )
    await edit_or_replace(
        call, text, help_kb(call.from_user.id == settings.owner_id)
    )
    await call.answer()


@router.callback_query(F.data == "help_commands")
async def help_commands(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    is_owner = call.from_user.id == settings.owner_id
    lines = [
        "<b>💬 الأوامر المتاحة</b>\n",
        "/start — بدء استخدام البوت",
        "/help — عرض هذه القائمة",
        "/cancel — إلغاء العملية الحالية",
        "/price — أسعار العملات",
        "/ton — سعر TON",
        "/usd — سعر الدولار",
        "/egp — سعر الجنيه",
        "/gram — سعر GRAM",
    ]
    if is_owner:
        lines.extend([
            "",
            "<b>أوامر المالك:</b>",
            "/stats — إحصائيات سريعة",
            "/pool — حالة اتصالات قاعدة البيانات",
            "/maintenance — تفعيل/تعطيل وضع الصيانة",
        ])
    await edit_or_replace(call, "\n".join(lines), help_kb(is_owner))
    await call.answer()


@router.callback_query(F.data == "help_referral")
async def help_referral(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    if not settings.referral_enabled:
        await call.answer("نظام الإحالة غير مفعّل.", show_alert=True)
        return
    try:
        me = await call.bot.get_me()
        if not me.username:
            await call.answer("يوزر البوت غير متوفر.", show_alert=True)
            return
        link = referral_link(me.username, call.from_user.id)
        count = await db.count_referrals(call.from_user.id)
        text = (
            "<b>🎁 نظام الإحالة</b>\n\n"
            f"رابطك الخاص:\n<code>{safe_html(link)}</code>\n\n"
            f"عدد إحالاتك: <b>{count}</b>\n"
            f"الهدف: <b>{settings.referral_target}</b>\n"
            f"المكافأة: <b>{settings.referral_reward_days} أيام مجانية</b>\n\n"
            "شارك الرابط مع أصدقائك، وعند وصولك للهدف "
            "ستُضاف الأيام تلقائيًا لاشتراكك."
        )
    except Exception:
        log.exception("help.referral.failed")
        await call.answer("تعذّر تحميل رابط الإحالة.", show_alert=True)
        return
    await edit_or_replace(
        call, text, help_kb(call.from_user.id == settings.owner_id)
    )
    await call.answer()


@router.callback_query(F.data == "bot_force_verify")
async def bot_force_verify(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    invalidate_force_channels(settings.owner_id)
    missing = await required_subs(
        call.bot, call.from_user.id, settings.owner_id, scope="bot"
    )
    if missing:
        await call.message.edit_text(
            BOT_FORCE_SUB_TEXT, reply_markup=bot_sub_kb(missing),
            parse_mode="HTML",
        )
        await call.answer("لم يكتمل الاشتراك.", show_alert=True)
        return
    await call.answer("تم التحقق.")
    kb = await public_kb(call.from_user.id, call.bot)
    await edit_or_replace(
        call, fancy_success("تم التحقق من الاشتراك."), kb,
    )


@router.callback_query(F.data == "public_home")
async def public_home(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    name = safe_html(call.from_user.first_name or "صديقي")
    text = f"<b>👋 أهلاً بك يا {name}</b>"
    image_id = await db.fetchval(
        "SELECT bot_welcome_image_file_id FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    kb = await public_kb(call.from_user.id, call.bot)
    if image_id:
        await call.message.answer_photo(
            image_id, caption=text, reply_markup=kb, parse_mode="HTML",
        )
    else:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "how_it_works")
async def how_it_works(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    try:
        bot_info = await call.bot.get_me()
        bot_username = f"@{bot_info.username}" if bot_info.username else "يوزر البوت"
    except Exception:
        bot_username = "يوزر البوت"
        
    text = (
        "<b>⚙️ كيف أفعل البوت ؟</b>\n\n"
        f"1️⃣ انسخ يوزر البوت: من الزر \n"
        "2️⃣ اضغط على زر <b>اذهب للاعدادات</b> بالأسفل.\n"
        "3️⃣ توجه إلى خيار <b>المحادثة الآلية</b>.\n"
        "4️⃣ الصق يوزر البوت في حقل الإضافة.\n"
        "5️⃣ ارجع للبوت واضغط <b>لوحة التحكم</b>."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="نسخ معرف البوت",
            copy_text=CopyTextButton(text=bot_username)
        )],
        [InlineKeyboardButton(text="⚙️ اذهب للاعدادات", url="tg://settings")],
        [InlineKeyboardButton(text="رجوع", callback_data="public_home")],
    ])
    
    row = await db.fetchrow(
        "SELECT dashboard_video_file_id, dashboard_video_type FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    dashboard_video = row["dashboard_video_file_id"] if row else ""
    dashboard_video_type = row["dashboard_video_type"] if row else "video"

    from plugins.callbacks import _send_dashboard_video_or_text

    await _send_dashboard_video_or_text(
        call,
        dashboard_video or "",
        dashboard_video_type or "video",
        text,
        kb,
    )
    await call.answer()


@router.callback_query(F.data == "recheck_permissions")
async def recheck_permissions(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    uid = call.from_user.id
    row = await db.fetchrow(
        "SELECT connection_id FROM business "
        "WHERE user_id=$1 AND enabled=TRUE "
        "ORDER BY connection_id DESC LIMIT 1",
        uid,
    )
    if not row:
        await call.answer(
            "لم يتم العثور على اتصال. اربط حساب Business أولًا.",
            show_alert=True,
        )
        return
    try:
        current = await call.bot.get_business_connection(
            business_connection_id=row["connection_id"]
        )
    except Exception:
        await call.answer("تعذر التحقق. حاول بعد قليل.", show_alert=True)
        return
    if not current.is_enabled:
        await call.answer("الاتصال غير مفعّل حاليًا.", show_alert=True)
        return
    await db.execute(
        "UPDATE business SET can_reply=$1, can_edit_name=$2, "
        "can_edit_bio=$3, enabled=$4 WHERE connection_id=$5",
        bool(current.rights and current.rights.can_reply),
        bool(current.rights and current.rights.can_edit_name),
        bool(current.rights and getattr(current.rights, "can_edit_bio", False)),
        bool(current.is_enabled),
        row["connection_id"],
    )
    missing = missing_permissions(current.rights)
    if missing:
        await edit_or_replace(
            call,
            permissions_warning_text(missing),
            permissions_warning_kb(),
        )
        await call.answer("لسه فيه أذونات ناقصة.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎛 التحكم بحسابك",
                             callback_data="control_panel")
    ]])
    await edit_or_replace(
        call,
        "<b>✅ تم تفعيل كل الأذونات بنجاح</b>\n\n"
        "يمكنك الآن التحكم في الردود والترحيب والميديا.",
        kb,
    )
    await call.answer("✅ تم التحقق بنجاح.")


@router.business_connection()
async def on_business_connection(update: BusinessConnection) -> None:
    rights = update.rights
    user = update.user
    await db.execute("""
        INSERT INTO business(connection_id, user_id, enabled, can_reply,
                             can_edit_name, can_edit_bio,
                             original_name, original_last_name)
        VALUES($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT(connection_id) DO UPDATE SET
          enabled=EXCLUDED.enabled,
          can_reply=EXCLUDED.can_reply,
          can_edit_name=EXCLUDED.can_edit_name,
          can_edit_bio=EXCLUDED.can_edit_bio
    """,
        update.id, user.id, update.is_enabled,
        bool(rights and rights.can_reply),
        bool(rights and rights.can_edit_name),
        bool(rights and getattr(rights, "can_edit_bio", False)),
        user.first_name or "", user.last_name,
    )
    await ensure_settings(user.id)
    db.perm_cache_invalidate(user.id)
    await audit(
        user.id,
        "business_connection",
        update.id,
        is_enabled=update.is_enabled,
    )

    if not update.is_enabled:
        await send_connection_notification(
            update.bot, "disconnected",
            user.id, user.first_name or "",
            user.username or "", update.id,
        )
        try:
            await update.bot.send_message(
                user.id,
                "⚠️ <b>تم إلغاء ربط حسابك بالبوت.</b>",
                parse_mode="HTML",
            )
        except Exception:
            log.exception("business.unlink.notify.user.failed")
        return

    await send_connection_notification(
        update.bot, "connected",
        user.id, user.first_name or "",
        user.username or "", update.id,
    )

    if not await paid_access_allowed(user.id):
        return

    missing = missing_permissions(rights)
    try:
        if missing:
            await update.bot.send_message(
                user.id, permissions_warning_text(missing),
                reply_markup=permissions_warning_kb(),
                parse_mode="HTML",
            )
            log.info(
                "business.connection.missing_perms",
                user_id=user.id, missing=[m[0] for m in missing],
            )
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎛 التحكم بحسابك",
                                     callback_data="control_panel")
            ]])
            await update.bot.send_message(
                user.id,
                "<b>✅ تم ربط حسابك بنجاح</b>\n\n"
                "يمكنك الآن التحكم في الردود والترحيب والميديا.",
                reply_markup=kb, parse_mode="HTML",
            )
            log.info("business.connection.ready", user_id=user.id)
    except Exception:
        log.exception("business.notify.failed")


@router.business_message(
    ~F.text.regexp(
        r"^\.?(?:انتحال|رجوع|مسح\s+\d+|(?:سبام|تكرار)\s+.+\s+\d+|"
        r"(?:فتح|قفل)\s+الكول|(?:تفعيل|تعطيل)\s+تعديل\s+الخط|رد\s+.+)$"
    )
)
async def on_business_message(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id or not message.chat:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled, can_reply, can_edit_name, can_edit_bio "
        "FROM business WHERE connection_id=$1",
        bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    if await is_bot_user_blocked(owner_id):
        return

    _is_pv = message.chat.type == "private"
    _sender_id = (
        message.from_user.id if message.from_user else message.chat.id
    )
    if (
        _is_pv
        and _sender_id != owner_id
        and _sender_id != 777000
        and _sender_id != settings.owner_id
    ):
        _pv_muted = await db.fetchval(
            "SELECT 1 FROM muted WHERE connection_id=$1 AND chat_id=$2",
            bc_id, message.chat.id,
        )
        if _pv_muted:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            _m_name = message.from_user.first_name if message.from_user else ""
            _m_user = message.from_user.username if message.from_user else ""
            _m_txt = (
                "🔇 <b>رسالة من مستخدم مكتوم (محذوفة)</b>\n\n"
                f"👤 <b>الاسم:</b> {clickable_user_link(message.chat.id, _m_name or 'بدون اسم')}\n"
                f"🔗 <b>اليوزر:</b> "
                + (f"@{safe_html(_m_user)}" if _m_user else "غير متوفر")
                + f"\n🆔 <b>الآيدي:</b> <code>{message.chat.id}</code>"
            )
            if message.text or message.caption:
                _m_txt += (
                    f"\n\n📄 <b>النص:</b>\n"
                    f"<blockquote>{safe_html((message.text or message.caption or '')[:500])}</blockquote>"
                )
            _m_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"👤 {(_m_name or 'العميل')[:30]}",
                    url=f"tg://user?id={message.chat.id}",
                )
            ]])
            try:
                await message.bot.send_message(
                    chat_id=owner_id, text=_m_txt,
                    reply_markup=_m_kb, parse_mode="HTML",
                )
            except Exception:
                pass
            return
        _target = owner_id
        _sender_name = message.from_user.first_name if message.from_user else ""
        _sender_username = message.from_user.username if message.from_user else ""
        _txt = (
            "📩 <b>رسالة خاصة جديدة</b>\n\n"
            f"👤 <b>الاسم:</b> "
            f"{clickable_user_link(_sender_id, _sender_name or 'بدون اسم')}\n"
            f"🔗 <b>اليوزر:</b> "
            + (f"@{safe_html(_sender_username)}"
               if _sender_username else "غير متوفر")
            + "\n"
            f"🆔 <b>الآيدي:</b> <code>{_sender_id}</code>"
        )
        _kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"👤 {(_sender_name or 'العميل')[:30]}",
                url=f"tg://user?id={_sender_id}",
            )
        ]])
        await _forward_any_business_message(
            message.bot, _target, message, _txt, _kb,
            try_forward=True,
        )
        log.info("pv_log.sent", target=_target)

    text_raw = message.text or message.caption or ""
    text = text_raw.lower().strip()

    media_type, media_file_id = _detect_media_type_full(message)
    media_type = media_type or ""
    media_file_id = media_file_id or ""

    await db.execute("""
        INSERT INTO tracked_messages
          (connection_id, chat_id, message_id, sender_id,
           sender_name, sender_username, text_content,
           media_type, media_file_id, created_at)
        VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
        ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET
          text_content=EXCLUDED.text_content,
          media_type=COALESCE(EXCLUDED.media_type, tracked_messages.media_type),
          media_file_id=COALESCE(EXCLUDED.media_file_id, tracked_messages.media_file_id)
    """,
        bc_id, message.chat.id, message.message_id,
        message.from_user.id if message.from_user else message.chat.id,
        message.from_user.first_name if message.from_user else "",
        message.from_user.username if message.from_user else "",
        text_raw, media_type, media_file_id,
    )
    await db.execute("""
        INSERT INTO customers
          (connection_id, chat_id, first_name, username, last_seen)
        VALUES($1, $2, $3, $4, NOW())
        ON CONFLICT(connection_id, chat_id) DO UPDATE SET
          first_name=EXCLUDED.first_name,
          username=EXCLUDED.username,
          last_seen=NOW()
    """,
        bc_id, message.chat.id,
        message.chat.first_name or "",
        message.chat.username or "",
    )
    is_owner_message = bool(
        (message.from_user and message.from_user.id == owner_id)
        or message.chat.id == owner_id
    )
    cmd = re.sub(r"[\s.!؟!?،؛:]+$", "", text).strip().lstrip("/")
    owner_missing = []
    if is_owner_message and not (
        biz["can_reply"] and biz["can_edit_name"] and biz["can_edit_bio"]
    ):
        owner_missing = [
            (field, label, icon)
            for field, label, icon in REQUIRED_PERMISSIONS
            if not bool(biz[field])
        ]

    if not is_owner_message:
        muted = await db.fetchval(
            "SELECT 1 FROM muted WHERE connection_id=$1 AND chat_id=$2",
            bc_id, message.chat.id,
        )
        if muted:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            _m_name = message.from_user.first_name if message.from_user else ""
            _m_user = message.from_user.username if message.from_user else ""
            _m_txt = (
                "🔇 <b>رسالة من مستخدم مكتوم (محذوفة)</b>\n\n"
                f"👤 <b>الاسم:</b> {clickable_user_link(message.chat.id, _m_name or 'بدون اسم')}\n"
                f"🔗 <b>اليوزر:</b> "
                + (f"@{safe_html(_m_user)}" if _m_user else "غير متوفر")
                + f"\n🆔 <b>الآيدي:</b> <code>{message.chat.id}</code>"
            )
            if message.text or message.caption:
                _m_txt += (
                    f"\n\n📄 <b>النص:</b>\n"
                    f"<blockquote>{safe_html((message.text or message.caption or '')[:500])}</blockquote>"
                )
            _m_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"👤 {(_m_name or 'العميل')[:30]}",
                    url=f"tg://user?id={message.chat.id}",
                )
            ]])
            try:
                await message.bot.send_message(
                    chat_id=owner_id, text=_m_txt,
                    reply_markup=_m_kb, parse_mode="HTML",
                )
            except Exception:
                pass
            return

    if is_owner_message and text:
        is_command = text.startswith(".") or text.startswith("/")
        if not is_command:
            font_style = await db.fetchval(
                "SELECT font_style FROM settings WHERE owner_id=$1", owner_id
            )
            if font_style and font_style > 0:
                formatted_text = get_font_style_html(message.text, font_style)
                try:
                    await message.bot.send_message(
                        chat_id=message.chat.id,
                        text=formatted_text,
                        business_connection_id=bc_id,
                        parse_mode="HTML",
                    )
                    await _delete_biz_msg(
                        message.bot, bc_id, message.chat.id,
                        message.message_id,
                    )
                except Exception as e:
                    log.error("font_typing_failed", error=str(e))
                return

    if is_owner_message and cmd in {"حسابي", "id", "ايدي", "آيدي"}:
        if owner_missing:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=permissions_warning_text(owner_missing),
                    reply_markup=permissions_warning_kb(),
                    business_connection_id=bc_id, parse_mode="HTML",
                )
            except Exception:
                pass
            return
        target = owner_id if cmd == "حسابي" else message.chat.id
        fallback_name = ""
        fallback_username = ""
        if target == message.chat.id:
            fallback_name = (
                getattr(message.chat, "full_name", None)
                or getattr(message.chat, "first_name", None) or ""
            )
            fallback_username = getattr(message.chat, "username", None) or ""
        await _delete_biz_msg(
            message.bot, bc_id, message.chat.id, message.message_id
        )
        try:
            await send_profile_card(
                message, target, fallback_name, fallback_username
            )
        except Exception:
            log.exception("profile_card.failed")
        return

    if is_owner_message and cmd == "كتم":
        if message.chat.id == settings.owner_id:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            return
        if not biz["can_reply"]:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=permissions_warning_text([
                        ("can_reply", "إدارة الرسائل", "💬")
                    ]),
                    reply_markup=permissions_warning_kb(),
                    business_connection_id=bc_id, parse_mode="HTML",
                )
            except Exception:
                pass
            return
        await db.execute(
            "INSERT INTO muted(connection_id, chat_id) "
            "VALUES($1, $2) ON CONFLICT DO NOTHING",
            bc_id, message.chat.id,
        )
        await _delete_biz_msg(
            message.bot, bc_id, message.chat.id, message.message_id
        )
        try:
            await message.bot.send_message(
                chat_id=message.chat.id, text="تم كتم العميل.",
                business_connection_id=bc_id,
            )
        except Exception:
            pass
        return

    if is_owner_message and cmd in {"الغاء الكتم", "إلغاء الكتم"}:
        await db.execute(
            "DELETE FROM muted WHERE connection_id=$1 AND chat_id=$2",
            bc_id, message.chat.id,
        )
        await db.execute(
            "DELETE FROM violations WHERE connection_id=$1 AND chat_id=$2",
            bc_id, message.chat.id,
        )
        await _delete_biz_msg(
            message.bot, bc_id, message.chat.id, message.message_id
        )
        try:
            await message.bot.send_message(
                chat_id=message.chat.id, text="تم إلغاء كتم العميل.",
                business_connection_id=bc_id,
            )
        except Exception:
            pass
        return

    row = await db.fetchrow(
        "SELECT enabled, welcome, welcome_enabled, welcome_mode, "
        "welcome_sticker_file_id, welcome_image_file_id, "
        "account_force_enabled, media_enabled, media_delete_types, "
        "calculator_enabled, currency_enabled, crypto_enabled, "
        "replies_enabled, transfer_enabled, social_enabled, payment_enabled "
        "FROM settings WHERE owner_id=$1",
        owner_id,
    )
    if not row or not row["enabled"]:
        return

    transfer_req = (
        parse_transfer_request(text_raw) if row["transfer_enabled"] else None
    )
    if transfer_req:
        phone, amount = transfer_req
        codes = transfer_codes(phone, amount)
        try:
            await message.bot.send_message(
                chat_id=message.chat.id,
                text=transfer_text(phone, amount),
                reply_markup=transfer_kb(codes),
                business_connection_id=bc_id, parse_mode="HTML",
            )
        except Exception:
            log.exception("transfer.failed")
        return

    if not is_owner_message and row["media_enabled"]:
        active_types = set(row["media_delete_types"] or [])
        incoming = media_type or ("text" if text_raw else "")
        if incoming in ("premium_sticker", "custom_emoji"):
            incoming_match = "sticker"
        else:
            incoming_match = incoming
        if incoming_match != "text" and incoming_match in active_types:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            return

    if (not is_owner_message and row["account_force_enabled"]):
        try:
            missing = await required_subs(
                message.bot, message.chat.id, owner_id, scope="account"
            )
        except Exception:
            missing = []
        if missing:
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=ACCOUNT_FORCE_SUB_TEXT,
                    reply_markup=sub_kb(missing),
                    business_connection_id=bc_id, parse_mode="HTML",
                )
            except Exception:
                pass
            return

    swear_enabled = await db.fetchval(
        "SELECT swear_filter_enabled FROM settings WHERE owner_id=$1",
        owner_id,
    )
    if text and not is_owner_message and swear_enabled:
        await _ensure_swear_seeded(owner_id)
        swear_words = await _get_banned_words_cached(owner_id, kind="swear")
        normalized = unicodedata.normalize("NFKC", text)
        stripped = "".join(
            c for c in normalized if unicodedata.category(c) != "Mn"
        )
        matched = None
        for entry in swear_words:
            w = entry["word"]
            if w:
                pattern = r"(?<!\w)" + re.escape(w) + r"(?!\w)"
                if (re.search(pattern, text)
                        or re.search(pattern, stripped)):
                    matched = w
                    break
        if matched:
            auto_mute = await db.fetchval(
                "SELECT swear_auto_mute FROM settings WHERE owner_id=$1",
                owner_id,
            )
            mute_after = await db.fetchval(
                "SELECT swear_mute_after FROM settings WHERE owner_id=$1",
                owner_id,
            ) or 3
            await db.execute("""
                INSERT INTO violations
                  (connection_id, chat_id, owner_id, count,
                   last_word, updated_at)
                VALUES($1, $2, $3, 1, $4, NOW())
                ON CONFLICT(connection_id, chat_id) DO UPDATE SET
                  count=violations.count+1,
                  last_word=EXCLUDED.last_word,
                  updated_at=NOW()
            """, bc_id, message.chat.id, owner_id, matched)
            count = await db.fetchval(
                "SELECT count FROM violations "
                "WHERE connection_id=$1 AND chat_id=$2",
                bc_id, message.chat.id,
            )
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            if auto_mute and count >= mute_after:
                await db.execute(
                    "INSERT INTO muted(connection_id, chat_id) "
                    "VALUES($1, $2) ON CONFLICT DO NOTHING",
                    bc_id, message.chat.id,
                )
                notice = "🔇 تم كتمك بسبب تكرار السب."
            else:
                remaining = mute_after - count + 1
                notice = f"⚠️ ممنوع السب - ليك {remaining} مرات."
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id, text=notice,
                    business_connection_id=bc_id,
                )
            except Exception:
                pass
            return

    if (is_owner_message and row["payment_enabled"]
            and cmd in {"تحويل", "طرق الدفع", "كاش", "كاشك", "الكاش"}):
        if owner_missing:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=permissions_warning_text(owner_missing),
                    reply_markup=permissions_warning_kb(),
                    business_connection_id=bc_id, parse_mode="HTML",
                )
            except Exception:
                pass
            return
        await _delete_biz_msg(
            message.bot, bc_id, message.chat.id, message.message_id
        )
        from plugins.PayBus import build_payment_keyboard
        kb = await build_payment_keyboard(owner_id)
        if kb:
            await message.bot.send_message(
                chat_id=message.chat.id,
                text="<b>طرق الدفع المتاحة</b>\n\nاختر الطريقة:",
                reply_markup=kb,
                business_connection_id=bc_id, parse_mode="HTML",
            )
        else:
            await message.bot.send_message(
                chat_id=message.chat.id,
                text="لا توجد طرق دفع مضافة حاليًا.",
                business_connection_id=bc_id,
            )
        return

    if is_owner_message and row["payment_enabled"]:
        if owner_missing:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=permissions_warning_text(owner_missing),
                    reply_markup=permissions_warning_kb(),
                    business_connection_id=bc_id, parse_mode="HTML",
                )
            except Exception:
                pass
            return
        from plugins.PayBus import try_send_payment_details
        sent = await try_send_payment_details(
            message, owner_id, text_raw, bc_id
        )
        if sent:
            try:
                await _delete_biz_msg(
                    message.bot, bc_id, message.chat.id, message.message_id
                )
            except Exception:
                pass
            return

    social_map = {
        "تيك توك": "tiktok", "تيكتوك": "tiktok",
        "انستقرام": "instagram", "انستجرام": "instagram",
        "تليجرام": "telegram", "فيسبوك": "facebook",
        "يوتيوب": "youtube",
    }
    if text and row["social_enabled"] and text in social_map:
        key = social_map[text]
        link = await db.fetchval(
            f"SELECT social_{key} FROM settings WHERE owner_id=$1",
            owner_id,
        )
        if link:
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=f"رابط حساب {text}:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=text, url=link)
                    ]]),
                    business_connection_id=bc_id,
                )
            except Exception:
                pass
            return

    if row["crypto_enabled"]:
        from plugins.Crypto import (
            parse_amount_query, build_values, build_keyboard, header,
            fetch_live_prices,
        )
        parsed = parse_amount_query(text_raw)
        if parsed:
            amount, base = parsed
            prices = await fetch_live_prices()
            if prices:
                values = build_values(amount, base, prices)
                try:
                    await message.bot.send_message(
                        chat_id=message.chat.id,
                        text=header(amount, base, prices),
                        reply_markup=build_keyboard(values, base),
                        business_connection_id=bc_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

    if row["calculator_enabled"] and text_raw:
        calc = try_calculate(text_raw)
        if calc:
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=f"<b>{safe_html(calc)}</b>",
                    business_connection_id=bc_id, parse_mode="HTML",
                )
            except Exception:
                pass
            return

    if text and row["replies_enabled"]:
        reply = await db.fetchrow(
            "SELECT id, response FROM replies "
            "WHERE owner_id=$1 AND LOWER(trigger)=$2 LIMIT 1",
            owner_id, text,
        )
        if reply:
            await _delete_biz_msg(
                message.bot, bc_id, message.chat.id, message.message_id
            )
            buttons = await db.fetch(
                "SELECT id, label, btn_type, btn_color, "
                "icon_custom_emoji_id, btn_value "
                "FROM reply_buttons WHERE reply_id=$1 ORDER BY position",
                reply["id"],
            )
            kb = _build_reply_kb(buttons)
            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=reply["response"],
                    reply_markup=kb,
                    business_connection_id=bc_id, parse_mode="HTML",
                )
            except Exception:
                log.exception("auto_reply.failed")
            return

    if row["welcome_enabled"] and not is_owner_message:
        now = datetime.now(UTC)
        cust = await db.fetchrow(
            "SELECT last_welcome FROM customers "
            "WHERE connection_id=$1 AND chat_id=$2",
            bc_id, message.chat.id,
        )
        can_send = True
        if cust and cust["last_welcome"]:
            if now - cust["last_welcome"] < WELCOME_COOLDOWN:
                can_send = False
        if can_send:
            await db.execute(
                "UPDATE customers SET last_welcome=NOW() "
                "WHERE connection_id=$1 AND chat_id=$2",
                bc_id, message.chat.id,
            )
            try:
                if row["welcome_image_file_id"]:
                    await message.bot.send_photo(
                        message.chat.id, row["welcome_image_file_id"],
                        caption=row["welcome"] or None,
                        business_connection_id=bc_id, parse_mode="HTML",
                    )
                elif row["welcome"]:
                    await message.bot.send_message(
                        message.chat.id, row["welcome"],
                        business_connection_id=bc_id, parse_mode="HTML",
                    )
                if row["welcome_sticker_file_id"]:
                    await message.bot.send_sticker(
                        message.chat.id, row["welcome_sticker_file_id"],
                        business_connection_id=bc_id,
                    )
            except Exception:
                log.exception("welcome.failed")


async def _delete_biz_msg(
    bot: Bot, bc_id: str, chat_id: int, message_id: int
) -> None:
    try:
        await bot.delete_business_messages(
            business_connection_id=bc_id, message_ids=[message_id],
        )
    except Exception:
        pass


def _build_reply_kb(buttons) -> InlineKeyboardMarkup:
    if not buttons:
        return None
    btns = []
    for b in buttons:
        try:
            base = {"text": b["label"]}
            if b["btn_type"] == "url":
                base["url"] = b["btn_value"]
            else:
                base["callback_data"] = f"rcopy_{b['id']}"
            style = BTN_STYLE_MAP.get(b["btn_color"] or "")
            if style:
                base["style"] = style
            btns.append(InlineKeyboardButton(**base))
        except Exception:
            continue
    if not btns:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)]
    )


@router.edited_business_message()
async def on_edited_business_message(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id or not message.chat:
        return
    biz = await db.fetchrow(
        "SELECT user_id FROM business WHERE connection_id=$1", bc_id
    )
    if not biz:
        return
    owner_id = biz["user_id"]
    enabled = await db.fetchval(
        "SELECT track_edited_enabled FROM settings WHERE owner_id=$1",
        owner_id,
    )
    if not enabled:
        return

    old = await db.fetchrow(
        "SELECT text_content, media_type FROM tracked_messages "
        "WHERE connection_id=$1 AND chat_id=$2 AND message_id=$3",
        bc_id, message.chat.id, message.message_id,
    )
    old_text = (old["text_content"] if old else "") or ""
    old_media = (old["media_type"] if old else "") or ""

    new_text = (
        message.html_text or message.text or message.caption or ""
    ).strip()

    new_media_type, new_media_id = _detect_media_type_full(message)
    new_media_type = new_media_type or ""
    new_media_id = new_media_id or ""

    if not new_text and not new_media_type:
        emoji_ids = _extract_custom_emoji_ids(message)
        if emoji_ids:
            new_media_type = "custom_emoji"
            new_media_id = emoji_ids[0]

    await db.execute(
        "UPDATE tracked_messages SET text_content=$4, "
        "media_type=COALESCE($5, tracked_messages.media_type), "
        "media_file_id=COALESCE($6, tracked_messages.media_file_id) "
        "WHERE connection_id=$1 AND chat_id=$2 AND message_id=$3",
        bc_id, message.chat.id, message.message_id,
        new_text, new_media_type or None, new_media_id or None,
    )
    await db.execute("""
        INSERT INTO tracking_logs
          (owner_id, connection_id, chat_id, message_id,
           event_type, old_text, new_text, media_type,
           media_file_id, created_at)
        VALUES($1, $2, $3, $4, 'edited', $5, $6, $7, $8, NOW())
    """, owner_id, bc_id, message.chat.id, message.message_id,
        old_text, new_text,
        new_media_type or old_media, new_media_id)

    sender_id = (
        message.from_user.id if message.from_user else message.chat.id
    )
    if sender_id == owner_id:
        return

    sender_name = message.from_user.first_name if message.from_user else ""
    sender_username = message.from_user.username if message.from_user else ""

    old_display = tracked_message_display(old_text, old_media)
    new_display = tracked_message_display(
        new_text, new_media_type or old_media
    )

    txt = (
        "✏️ <b>تم تعديل رسالة</b>\n\n"
        f"👤 <b>الاسم:</b> "
        f"{clickable_user_link(sender_id, sender_name or 'بدون اسم')}\n"
        f"🔗 <b>اليوزر:</b> "
        + (f"@{safe_html(sender_username)}"
           if sender_username else "غير متوفر")
        + "\n"
        f"🆔 <b>الآيدي:</b> <code>{sender_id}</code>\n\n"
        f"📄 <b>قبل التعديل:</b>\n"
        f"<blockquote>{old_display}</blockquote>\n\n"
        f"📝 <b>بعد التعديل:</b>\n"
        f"<blockquote>{new_display}</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"👤 {sender_name[:30] or 'العميل'}",
            url=f"tg://user?id={sender_id}",
        )
    ]])
    try:
        await _forward_any_business_message(
            message.bot, owner_id, message, txt, kb,
            try_forward=False,
        )
    except Exception:
        log.exception("edit.notify.failed", target=owner_id)


@router.deleted_business_messages()
async def on_deleted_business_messages(
    update: BusinessMessagesDeleted,
) -> None:
    bc_id = update.business_connection_id
    if not bc_id or not update.chat:
        return
    biz = await db.fetchrow(
        "SELECT user_id FROM business WHERE connection_id=$1", bc_id
    )
    if not biz:
        return
    owner_id = biz["user_id"]
    enabled = await db.fetchval(
        "SELECT track_deleted_enabled FROM settings WHERE owner_id=$1",
        owner_id,
    )
    if not enabled:
        return
    for msg_id in update.message_ids:
        row = await db.fetchrow(
            "SELECT * FROM tracked_messages "
            "WHERE connection_id=$1 AND chat_id=$2 AND message_id=$3",
            bc_id, update.chat.id, msg_id,
        )
        if not row:
            continue
        sender_id = row["sender_id"] or update.chat.id
        if sender_id == owner_id:
            await db.execute(
                "DELETE FROM tracked_messages WHERE id=$1", row["id"]
            )
            continue
        sender_name = row["sender_name"] or ""
        sender_username = row["sender_username"] or ""
        text_content = row["text_content"] or ""
        media_type = row["media_type"] or ""
        media_file_id = row["media_file_id"] or ""
        last_row = await db.fetchrow(
            "SELECT text_content, media_type FROM tracked_messages "
            "WHERE connection_id=$1 AND chat_id=$2 AND message_id<>$3 "
            "AND sender_id=$4 ORDER BY created_at DESC LIMIT 1",
            bc_id, update.chat.id, msg_id, sender_id,
        )
        deleted_display = tracked_message_display(text_content, media_type)
        last_text = tracked_message_display(
            last_row["text_content"] if last_row else "",
            last_row["media_type"] if last_row else "",
        ) if last_row else "(لا توجد رسالة سابقة)"
        txt = (
            "🗑️ <b>تم حذف رسالة</b>\n\n"
            f"👤 <b>الاسم:</b> "
            f"{clickable_user_link(sender_id, sender_name or 'بدون اسم')}\n"
            f"🔗 <b>اليوزر:</b> "
            + (f"@{safe_html(sender_username)}"
               if sender_username else "غير متوفر")
            + "\n"
            f"🆔 <b>الآيدي:</b> <code>{sender_id}</code>\n\n"
            f"📄 <b>الرسالة المحذوفة:</b>\n"
            f"<blockquote>{deleted_display}</blockquote>\n\n"
            f"⏮ <b>آخر رسالة بعتها:</b>\n"
            f"<blockquote>{last_text}</blockquote>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"👤 {sender_name[:30] or 'العميل'}",
                url=f"tg://user?id={sender_id}",
            )
        ]])
        await db.execute("""
            INSERT INTO tracking_logs
              (owner_id, connection_id, chat_id, message_id,
               event_type, old_text, new_text, media_type,
               media_file_id, created_at)
            VALUES($1, $2, $3, $4, 'deleted', $5, '', $6, $7, NOW())
        """, owner_id, bc_id, update.chat.id, msg_id,
            text_content, media_type, media_file_id)
        if media_file_id:
            try:
                if media_type == "photo":
                    await update.bot.send_photo(owner_id, media_file_id,
                                                parse_mode="HTML")
                elif media_type == "video":
                    await update.bot.send_video(owner_id, media_file_id,
                                                parse_mode="HTML")
                elif media_type == "voice":
                    await update.bot.send_voice(owner_id, media_file_id,
                                                parse_mode="HTML")
                elif media_type == "audio":
                    await update.bot.send_audio(owner_id, media_file_id,
                                                parse_mode="HTML")
                elif media_type == "document":
                    await update.bot.send_document(owner_id, media_file_id,
                                                   parse_mode="HTML")
                elif media_type == "animation":
                    await update.bot.send_animation(owner_id, media_file_id,
                                                    parse_mode="HTML")
                elif media_type in ("sticker", "premium_sticker"):
                    await update.bot.send_sticker(owner_id, media_file_id)
                elif media_type == "video_note":
                    await update.bot.send_video_note(owner_id, media_file_id)
                elif media_type == "custom_emoji":
                    try:
                        await update.bot.send_message(
                            owner_id,
                            f"🎴 <b>Custom Emoji ID:</b>\n"
                            f"<code>{safe_html(media_file_id)}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except Exception:
                log.warning("deleted.media.send.failed", target=owner_id,
                            media_type=media_type)
        try:
            await update.bot.send_message(
                owner_id, txt, reply_markup=kb, parse_mode="HTML"
            )
        except Exception:
            log.exception("delete.notify.failed", target=owner_id)
        await db.execute(
            "DELETE FROM tracked_messages WHERE id=$1", row["id"]
        )


@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, bot: Bot) -> None:
    if update.chat.type != "channel":
        return
    if (not update.new_chat_member
            or update.new_chat_member.user.id != bot.id):
        return
    status = str(
        getattr(update.new_chat_member.status, "value",
                update.new_chat_member.status) or ""
    ).lower()
    title = update.chat.title or "بدون اسم"
    username = (
        f"@{update.chat.username}" if update.chat.username
        else str(update.chat.id)
    )
    old = await db.fetchrow(
        "SELECT chat_id FROM bot_channels WHERE chat_id=$1",
        update.chat.id,
    )
    active = status in {"administrator", "member"}
    if not active:
        await db.execute(
            "DELETE FROM bot_channels WHERE chat_id=$1", update.chat.id
        )
        try:
            await bot.send_message(
                settings.owner_id,
                f"<b>تم تنزيل البوت من قناة</b>\n\n"
                f"القناة: <b>{safe_html(title)}</b>\n"
                f"المعرف: <code>{safe_html(username)}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return
    if status == "administrator":
        await db.execute(
            "INSERT INTO bot_channels(chat_id, title, username, added_at) "
            "VALUES($1, $2, $3, NOW()) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "title=EXCLUDED.title, username=EXCLUDED.username",
            update.chat.id, title, update.chat.username or "",
        )
    if not old:
        try:
            role = "أدمن" if status == "administrator" else "عضو"
            await bot.send_message(
                settings.owner_id,
                f"<b>تم رفع البوت في قناة جديدة</b>\n\n"
                f"القناة: <b>{safe_html(title)}</b>\n"
                f"المعرف: <code>{safe_html(username)}</code>\n"
                f"الصلاحية: <b>{role}</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("rcopy_"))
async def reply_copy_button(call: CallbackQuery) -> None:
    try:
        bid = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    row = await db.fetchrow(
        "SELECT btn_value FROM reply_buttons "
        "WHERE id=$1 AND btn_type='copy'",
        bid,
    )
    if row:
        await call.answer(f"انسخ النص:\n{row['btn_value']}", show_alert=True)
    else:
        await call.answer()


from plugins.Temporary import (
    _impersonation_cache,
    parse_delete_command,
    parse_spam_command,
    save_original_profile,
    impersonate_user,
    restore_original_profile,
    get_target_user_info,
    get_user_bio,
    check_call_permission,
    open_group_call,
    close_group_call,
)


@router.business_message(F.text.regexp(r"^\.?انتحال$"))
async def biz_impersonate(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled, can_edit_name FROM business "
        "WHERE connection_id=$1", bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        return
    try:
        await message.bot.delete_business_messages(
            business_connection_id=bc_id,
            message_ids=[message.message_id],
        )
    except Exception:
        pass
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=(
                "⚠️ <b>قم بالرد على رسالة العضو أولاً</b>\n\n"
                "اعمل Reply على أي رسالة من العضو ثم اكتب <code>انتحال</code>"
            ),
            business_connection_id=bc_id, parse_mode="HTML",
        )
        return
    target = message.reply_to_message.from_user
    if target.id == settings.owner_id:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="⚠️ لا يمكن انتحال حساب المالك.",
            business_connection_id=bc_id,
        )
        return
    if not biz["can_edit_name"]:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="⚠️ تحتاج صلاحية «تعديل الاسم».",
            business_connection_id=bc_id,
        )
        return
    target_info = await get_target_user_info(message.bot, target.id)
    success, result_msg = await impersonate_user(
        message.bot, owner_id, bc_id,
        target_info["first_name"] or target.first_name or "",
        target_info["last_name"] or target.last_name or "",
        target_bio=target_info["bio"],
        target_photo_file_id=target_info["photo_file_id"],
    )
    await audit(owner_id, "impersonate", str(target.id), success=success)
    await message.bot.send_message(
        chat_id=message.chat.id, text=result_msg,
        business_connection_id=bc_id, parse_mode="HTML",
    )


@router.business_message(F.text.regexp(r"^\.?رجوع$"))
async def biz_restore(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled FROM business WHERE connection_id=$1",
        bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        return
    try:
        await message.bot.delete_business_messages(
            business_connection_id=bc_id,
            message_ids=[message.message_id],
        )
    except Exception:
        pass
    success, result_msg = await restore_original_profile(
        message.bot, owner_id, bc_id
    )
    await audit(owner_id, "restore_profile", success=success)
    await message.bot.send_message(
        chat_id=message.chat.id, text=result_msg,
        business_connection_id=bc_id, parse_mode="HTML",
    )


@router.business_message(F.text.regexp(r"^\.?مسح\s+\d+$"))
async def biz_delete_messages(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled FROM business WHERE connection_id=$1",
        bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        return
    count = parse_delete_command(message.text or "")
    if not count:
        return
    if not message.reply_to_message:
        try:
            await message.bot.delete_business_messages(
                business_connection_id=bc_id,
                message_ids=[message.message_id],
            )
        except Exception:
            pass
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="⚠️ قم بالرد على رسالة لبدء المسح.",
            business_connection_id=bc_id,
        )
        return
    base_id = message.reply_to_message.message_id
    ids_to_delete = [base_id - i for i in range(count) if base_id - i > 0]
    ids_to_delete.append(message.message_id)
    try:
        await message.bot.delete_business_messages(
            business_connection_id=bc_id, message_ids=ids_to_delete,
        )
    except Exception:
        log.exception("business_command.delete.failed", owner_id=owner_id)
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="❌ تعذّر حذف الرسائل.",
            business_connection_id=bc_id,
        )


@router.business_message(
    F.text.regexp(r"^\.?(سبام|تكرار)\s+.+\s+\d+$")
)
async def biz_spam(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled, can_reply FROM business "
        "WHERE connection_id=$1", bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        return
    if not biz["can_reply"]:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="⚠️ لا يملك البوت صلاحية إرسال الرسائل.",
            business_connection_id=bc_id,
        )
        return
    spam_text, spam_count = parse_spam_command(message.text or "")
    if not spam_text or not spam_count:
        return
    try:
        await message.bot.delete_business_messages(
            business_connection_id=bc_id,
            message_ids=[message.message_id],
        )
    except Exception:
        pass
    for _ in range(spam_count):
        try:
            await message.bot.send_message(
                chat_id=message.chat.id, text=spam_text,
                business_connection_id=bc_id,
            )
            await asyncio.sleep(0.3)
        except Exception:
            break


@router.business_message(F.text.regexp(r"^\.?(فتح|قفل)\s+الكول$"))
async def biz_call_control(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled FROM business WHERE connection_id=$1",
        bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        return
    try:
        await message.bot.delete_business_messages(
            business_connection_id=bc_id,
            message_ids=[message.message_id],
        )
    except Exception:
        pass
    has_perm, perm_error = await check_call_permission(message.bot, bc_id)
    if not has_perm:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=f"⚠️ {perm_error}",
            business_connection_id=bc_id,
        )
        return
    text = (message.text or "").strip().lstrip(".")
    is_open = text.startswith("فتح")
    if is_open:
        success, err = await open_group_call(message.bot, message.chat.id)
        action = "فتح"
    else:
        success, err = await close_group_call(message.bot, message.chat.id)
        action = "قفل"
    if success:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=f"✅ تم {action} المكالمة.",
            business_connection_id=bc_id,
        )
    else:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=f"⚠️ {err}",
            business_connection_id=bc_id,
        )


@router.business_message(F.text.regexp(r"^\.?تعطيل تعديل الخط$"))
async def biz_font_disable(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id FROM business WHERE connection_id=$1", bc_id
    )
    if not biz:
        return
    owner_id = biz["user_id"]
    if message.from_user and message.from_user.id != owner_id:
        return
    await db.execute(
        "UPDATE settings SET font_style=0 WHERE owner_id=$1", owner_id
    )
    db.cache_invalidate(f"setting:{owner_id}:")
    try:
        await message.bot.delete_business_messages(
            business_connection_id=bc_id,
            message_ids=[message.message_id],
        )
    except Exception:
        pass
    await message.bot.send_message(
        chat_id=message.chat.id,
        text="✅ تم تعطيل تعديل الخط.",
        business_connection_id=bc_id,
    )


@router.business_message(
    F.text.regexp(r"^\.?تفعيل تعديل الخط(?:\s+(\d))?$")
)
async def biz_font_enable(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id FROM business WHERE connection_id=$1", bc_id
    )
    if not biz:
        return
    owner_id = biz["user_id"]
    if message.from_user and message.from_user.id != owner_id:
        return
    parts = message.text.strip().split()
    style = 0
    if len(parts) >= 4:
        try:
            style = int(parts[-1])
        except ValueError:
            style = 0
    if style == 0:
        text = (
            "◐ <b>أنواع الخطوط والأرقام الدالة عليها:</b>\n\n"
            "1 - <b>خط غامق</b>\n"
            "2 - <i>خط مائل</i>\n"
            "3 - <u>خط تحته خط</u>\n"
            "4 - <s>خط مشطوب</s>\n"
            "5 - <tg-spoiler>خط مخفي</tg-spoiler>\n\n"
            "◐ <b>مثال:</b>\n<code>.تفعيل تعديل الخط 2</code>"
        )
        try:
            await message.bot.delete_business_messages(
                business_connection_id=bc_id,
                message_ids=[message.message_id],
            )
        except Exception:
            pass
        await message.bot.send_message(
            chat_id=message.chat.id, text=text,
            business_connection_id=bc_id, parse_mode="HTML",
        )
        return
    await db.execute(
        "UPDATE settings SET font_style=$1 WHERE owner_id=$2",
        style, owner_id,
    )
    db.cache_invalidate(f"setting:{owner_id}:")
    try:
        await message.bot.delete_business_messages(
            business_connection_id=bc_id,
            message_ids=[message.message_id],
        )
    except Exception:
        pass
    await message.bot.send_message(
        chat_id=message.chat.id,
        text=f"✅ تم تفعيل تعديل الخط رقم {style}.",
        business_connection_id=bc_id,
    )


@router.callback_query(F.data == "font_menu")
async def font_menu_callback(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner_id = call.from_user.id
    current_style = await db.fetchval(
        "SELECT font_style FROM settings WHERE owner_id=$1", owner_id
    ) or 0
    text = (
        "<b>🔤 إعدادات تعديل الخط</b>\n\n"
        "عند اختيار خط، سيتم تطبيقه تلقائياً على أي رسالة ترسلها "
        "في محادثات Business.\n\n"
        "اختر الخط الذي تريده من الأزرار أدناه:"
    )
    from plugins.Temporary import font_menu_kb
    await edit_or_replace(call, text, font_menu_kb(current_style))
    await call.answer()


@router.callback_query(F.data.startswith("font_set_"))
async def font_set_callback(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner_id = call.from_user.id
    try:
        style = int(call.data.split("_")[-1])
    except ValueError:
        await call.answer("خطأ في الاختيار.", show_alert=True)
        return
    await db.execute(
        "UPDATE settings SET font_style=$1 WHERE owner_id=$2",
        style, owner_id,
    )
    db.cache_invalidate(f"setting:{owner_id}:")
    text = (
        "<b>🔤 إعدادات تعديل الخط</b>\n\n"
        "عند اختيار خط، سيتم تطبيقه تلقائياً على أي رسالة ترسلها "
        "في محادثات Business.\n\n"
        "اختر الخط الذي تريده من الأزرار أدناه:"
    )
    from plugins.Temporary import font_menu_kb
    try:
        await call.message.edit_text(
            text, reply_markup=font_menu_kb(style), parse_mode="HTML"
        )
    except Exception:
        pass
    await call.answer("✅ تم تحديث نوع الخط بنجاح.")


@router.business_message(F.text.regexp(r"^\.?رد\s+.+$"))
async def biz_reply_to(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled, can_reply FROM business "
        "WHERE connection_id=$1", bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        return
    if not biz["can_reply"]:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="⚠️ تحتاج صلاحية «إدارة الرسائل» للرد.",
            business_connection_id=bc_id,
        )
        return
    if not message.reply_to_message:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="⚠️ اعمل Reply على الرسالة ثم اكتب <code>.رد نصك</code>",
            business_connection_id=bc_id, parse_mode="HTML",
        )
        return
    body = re.sub(r"^\.?رد\s+", "", message.text or "", count=1).strip()
    if not body:
        return
    try:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=body,
            reply_to_message_id=message.reply_to_message.message_id,
            business_connection_id=bc_id,
            parse_mode="HTML",
        )
        try:
            await message.bot.delete_business_messages(
                business_connection_id=bc_id,
                message_ids=[message.message_id],
            )
        except Exception:
            pass
    except Exception:
        log.exception("biz_reply.failed")
