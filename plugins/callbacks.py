# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025


from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time as _time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import structlog
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings
from Database import db, ensure_settings, is_account_linked, is_admin, audit
from plugins.Temporary import (
    WELCOME_COOLDOWN,
    back_kb,
    cancel_kb,
    clock_text,
    clean_clock_suffix,
    confirm_kb,
    currency_header,
    currency_keyboard,
    currency_values,
    edit_or_replace,
    fancy_error,
    fancy_success,
    fetch_live_prices,
    force_add_error_kb,
    format_remaining,
    LIVE_BIO_INTERVAL_CHOICES,
    LIVE_BIO_NAMES,
    main_kb,
    parse_currency_request,
    parse_channel_input,
    public_kb,
    referral_link,
    safe_html,
    try_calculate,
    verify_bot_is_admin,
)

log = structlog.get_logger(__name__)
router = Router(name="callbacks")
UTC = timezone.utc


# ========== استيراد دوال الكاش من inform (لتجنب التكرار) ==========
def _get_banned_cache_invalidator():
    from plugins.inform import invalidate_banned_words
    return invalidate_banned_words


def _get_force_cache_invalidator():
    from plugins.inform import invalidate_force_channels
    return invalidate_force_channels


# ========== الحالات ==========
class ReplySG(StatesGroup):
    add_trigger = State()
    add_response = State()
    btn_label = State()
    btn_value = State()
    edit_trigger = State()
    edit_response = State()


class MuteSG(StatesGroup):
    chat_id = State()


class SocialSG(StatesGroup):
    value = State()


class ForceSG(StatesGroup):
    add_channel = State()


class LiveBioSG(StatesGroup):
    add_verse = State()


class BroadcastSG(StatesGroup):
    add_text = State()


class CurrencySG(StatesGroup):
    amount = State()


class WelcomeSG(StatesGroup):
    edit_text = State()
    edit_sticker = State()
    edit_image = State()


class SpeechSG(StatesGroup):
    waiting_voice = State()


class YouTubeSG(StatesGroup):
    waiting_query = State()


# ========== حالة ميزة ممنوع السب ==========
class SwearSG(StatesGroup):
    add_word = State()
    mute_after = State()


# ========== الشاشة الرئيسية ==========
async def show_main(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    uid = call.from_user.id
    is_owner = uid == settings.owner_id

    row = await db.fetchrow(
        """
        SELECT 
            s.bot_access_mode,
            bs.expires_at,
            bs.trial
        FROM settings s
        LEFT JOIN bot_subscriptions bs ON bs.user_id = $1
        WHERE s.owner_id = $2
        """,
        uid, settings.owner_id,
    )

    mode = row["bot_access_mode"] if row else "free"
    account_status = "مجاني بالكامل"
    remaining = "غير محدودة"

    if not is_owner:
        if row and row["expires_at"]:
            delta = row["expires_at"] - datetime.now(UTC)
            if delta.total_seconds() > 0:
                account_status = (
                    "نسخة تجريبية" if row["trial"] else "نسخة مدفوعة"
                )
                remaining = format_remaining(delta)
            else:
                account_status = "لا يوجد اشتراك فعال"
                remaining = "منتهية"
        elif mode == "paid":
            account_status = "لا يوجد اشتراك فعال"
            remaining = "غير متاحة"

    text = (
        "<blockquote><b>👋 مرحبًا بك في لوحة التحكم</b>\n\n"
        f"💎 <b>حالة الحساب: {account_status}</b>\n"
        f"⏰ <b>المدة المتبقية: {remaining}</b></blockquote>"
    )
    await edit_or_replace(call, text, main_kb(is_owner))


@router.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.clear()
    if not await is_admin(call.from_user.id):
        kb = await public_kb(call.from_user.id, call.bot)
        await edit_or_replace(
            call,
            f"👋 أهلاً بك يا "
            f"{safe_html(call.from_user.first_name or 'صديقي')}",
            kb,
        )
        await call.answer()
        return
    await show_main(call)
    await call.answer()


# ========== إرسال فيديو لوحة التحكم (مساعد) ==========
async def _send_dashboard_video_or_text(
    call: CallbackQuery, video_file_id: str, video_type: str, caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """
    يرسل فيديو لوحة التحكم مع الكابشن باستخدام الـ file_id مباشرة.
    - لو النوع animation يرسل answer_animation
    - لو أي نوع آخر يرسل answer_video
    - لو فشل الإرسال، يسقط للنص بدون ما يبوظ التجربة.
    """
    try:
        await call.message.delete()
    except Exception:
        pass

    if video_file_id:
        try:
            # استخدام file_id مباشرة بدون أي عمليات copy أو forward
            if video_type == "animation":
                await call.message.answer_animation(
                    animation=video_file_id,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            else:
                await call.message.answer_video(
                    video=video_file_id,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            return
        except Exception as exc:
            # نسجل الخطأ ونكمل للـ Fallback (بدون كسر التجربة)
            log.warning(
                "control_panel.dashboard_video.send.failed",
                error=str(exc)[:200],
                video_type=video_type,
            )

    # Fallback: لو مفيش فيديو أو فشل إرساله، نرسل النص العادي
    await call.message.answer(
        text=caption, 
        reply_markup=reply_markup, 
        parse_mode="HTML",
    )


@router.callback_query(F.data == "control_panel")
async def control_panel(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not call.from_user:
        return
    await state.clear()
    uid = call.from_user.id

    if uid == settings.owner_id:
        await show_main(call)
        await call.answer()
        return

    if not await is_account_linked(uid):
        try:
            bot_info = await bot.get_me()
            bot_username = f"@{bot_info.username}" if bot_info.username else "يوزر البوت"
        except Exception:
            bot_username = "يوزر البوت"

        from plugins.Temporary import link_help_kb
        link_text = (
            "<b>🔄 اربط حسابك خلال دقائق:</b>\n\n"
            "1️⃣ انسخ يوزر البوت: <code>" + safe_html(bot_username) + "</code>\n"
            "2️⃣ اضغط على زر 'اذهب للاعدادات' بالأسفل.\n"
            "3️⃣ توجه إلى خيار 'المحادثة الآلية'.\n"
            "4️⃣ الصق يوزر البوت في حقل الإضافة\n"
            "5️⃣ اعطي بوت أذونات البوت للإدارة"
        )

        row = await db.fetchrow(
            "SELECT dashboard_video_file_id, dashboard_video_type FROM settings WHERE owner_id=$1",
            settings.owner_id,
        )
        dashboard_video = row["dashboard_video_file_id"] if row else ""
        dashboard_video_type = row["dashboard_video_type"] if row else "video"

        await _send_dashboard_video_or_text(
            call,
            dashboard_video or "",
            dashboard_video_type or "video",
            link_text,
            link_help_kb(),
        )
        await call.answer("اربط حسابك أولًا.", show_alert=True)
        return

    from plugins.inform import (
        missing_permissions,
        permissions_warning_kb,
        permissions_warning_text,
    )
    row = await db.fetchrow(
        "SELECT connection_id, can_reply, can_edit_name, can_edit_bio "
        "FROM business WHERE user_id=$1 AND enabled=TRUE "
        "ORDER BY connection_id DESC LIMIT 1",
        uid,
    )
    if not row:
        await call.answer("لا يوجد اتصال مفعّل.", show_alert=True)
        return

    try:
        current = await bot.get_business_connection(
            business_connection_id=row["connection_id"]
        )
        await db.execute(
            "UPDATE business SET can_reply=$1, can_edit_name=$2, "
            "can_edit_bio=$3 WHERE connection_id=$4",
            bool(current.rights and current.rights.can_reply),
            bool(current.rights and current.rights.can_edit_name),
            bool(current.rights and getattr(current.rights, "can_edit_bio", False)),
            row["connection_id"],
        )
        missing = missing_permissions(current.rights)
    except Exception:
        log.exception("control_panel.permission_check.failed")
        missing = []

    if missing:
        await edit_or_replace(
            call,
            permissions_warning_text(missing),
            permissions_warning_kb(),
        )
        await call.answer("لازم تفعّل الأذونات أولًا.", show_alert=True)
        return

    await show_main(call)
    await call.answer()


# ========== التفعيل والتعطيل ==========
def toggle_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="تعطيل" if enabled else "تفعيل",
            callback_data="toggle_switch",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "toggle")
async def toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    enabled = await db.fetchval(
        "SELECT enabled FROM settings WHERE owner_id=$1", call.from_user.id
    )
    await edit_or_replace(
        call,
        f"<b>التفعيل والتعطيل</b>\n\n"
        f"الحالة الحالية: <b>{'مفعّل ✅' if enabled else 'معطل ❌'}</b>",
        toggle_kb(bool(enabled)),
    )
    await call.answer()


@router.callback_query(F.data == "toggle_switch")
async def toggle_switch(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    enabled = await db.fetchval(
        "SELECT enabled FROM settings WHERE owner_id=$1", call.from_user.id
    )
    new_val = not bool(enabled)
    await db.execute(
        "UPDATE settings SET enabled=$1 WHERE owner_id=$2",
        new_val, call.from_user.id,
    )
    db.cache_invalidate(f"setting:{call.from_user.id}:")
    await toggle(call)
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"تم التحديث: {state_text}", show_alert=False)


# ========== إدارة رسالة الترحيب ==========
def welcome_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} الترحيب (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
            callback_data="welcome_toggle",
        )],
        [InlineKeyboardButton(
            text="📝 تعديل نص الترحيب", callback_data="welcome_edit_text",
        )],
        [InlineKeyboardButton(
            text="🖼 تعديل صورة الترحيب", callback_data="welcome_edit_image",
        ), InlineKeyboardButton(
            text="🎨 تعديل ملصق الترحيب", callback_data="welcome_edit_sticker",
        )],
        [InlineKeyboardButton(
            text="🗑 حذف الصورة", callback_data="welcome_del_image",
        ), InlineKeyboardButton(
            text="🗑 حذف الملصق", callback_data="welcome_del_sticker",
        )],
        [InlineKeyboardButton(
            text="👁 معاينة الترحيب", callback_data="welcome_preview",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "welcome")
async def welcome_menu(
    call: CallbackQuery, state: FSMContext | None = None
) -> None:
    if not call.from_user:
        return
    if state is not None:
        await state.clear()
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT welcome, welcome_enabled, welcome_image_file_id, "
        "welcome_sticker_file_id FROM settings WHERE owner_id=$1",
        owner,
    )
    if not row:
        await ensure_settings(owner)
        row = {
            "welcome": "",
            "welcome_enabled": True,
            "welcome_image_file_id": "",
            "welcome_sticker_file_id": "",
        }
    text = (
        "<b>👋 إدارة رسالة الترحيب</b>\n\n"
        f"الحالة: <b>{'مفعّل ✅' if row['welcome_enabled'] else 'معطل ❌'}</b>\n"
        f"نص الترحيب: "
        f"<b>{safe_html(row['welcome'][:60]) if row['welcome'] else 'لم يُعيّن'}</b>\n"
        f"صورة الترحيب: "
        f"<b>{'موجودة ✅' if row['welcome_image_file_id'] else 'لا توجد ❌'}</b>\n"
        f"ملصق الترحيب: "
        f"<b>{'موجود ✅' if row['welcome_sticker_file_id'] else 'لا يوجد ❌'}</b>\n\n"
        "<blockquote>يُرسل الترحيب مرة واحدة كل ساعة لكل عميل جديد.</blockquote>"
    )
    await edit_or_replace(call, text, welcome_kb(bool(row["welcome_enabled"])))
    await call.answer()


@router.callback_query(F.data == "welcome_toggle")
async def welcome_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT welcome_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET welcome_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await welcome_menu(call, None)  # type: ignore
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"الترحيب الآن: {state_text}")


@router.callback_query(F.data == "welcome_edit_text")
async def welcome_edit_text(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.set_state(WelcomeSG.edit_text)
    await edit_or_replace(
        call,
        "<b>📝 أرسل نص الترحيب الجديد.</b>\n\n"
        "<blockquote>يمكنك استخدام HTML مثل &lt;b&gt;نص&lt;/b&gt;</blockquote>",
        cancel_kb(),
    )
    await call.answer()


@router.message(WelcomeSG.edit_text)
async def welcome_save_text(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("أرسل نصًا صحيحًا.")
        return
    await db.execute(
        "UPDATE settings SET welcome=$1 WHERE owner_id=$2",
        text, message.from_user.id,
    )
    db.cache_invalidate(f"setting:{message.from_user.id}:")
    await state.clear()
    await message.answer(
        fancy_success("تم حفظ نص الترحيب."),
        reply_markup=back_kb("welcome"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "welcome_edit_image")
async def welcome_edit_image(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.set_state(WelcomeSG.edit_image)
    await edit_or_replace(call, "<b>🖼 أرسل صورة الترحيب الجديدة.</b>", cancel_kb())
    await call.answer()


@router.message(WelcomeSG.edit_image)
async def welcome_save_image(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    if not message.photo:
        await message.answer("أرسل صورة فقط.")
        return
    await db.execute(
        "UPDATE settings SET welcome_image_file_id=$1, "
        "welcome_image_type='photo' WHERE owner_id=$2",
        message.photo[-1].file_id, message.from_user.id,
    )
    db.cache_invalidate(f"setting:{message.from_user.id}:")
    await state.clear()
    await message.answer(
        fancy_success("تم حفظ صورة الترحيب."),
        reply_markup=back_kb("welcome"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "welcome_edit_sticker")
async def welcome_edit_sticker(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.set_state(WelcomeSG.edit_sticker)
    await edit_or_replace(call, "<b>🎨 أرسل ملصق الترحيب الجديد.</b>", cancel_kb())
    await call.answer()


@router.message(WelcomeSG.edit_sticker)
async def welcome_save_sticker(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    if not message.sticker:
        await message.answer("أرسل ملصقًا فقط.")
        return
    await db.execute(
        "UPDATE settings SET welcome_sticker_file_id=$1 WHERE owner_id=$2",
        message.sticker.file_id, message.from_user.id,
    )
    db.cache_invalidate(f"setting:{message.from_user.id}:")
    await state.clear()
    await message.answer(
        fancy_success("تم حفظ ملصق الترحيب."),
        reply_markup=back_kb("welcome"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "welcome_del_image")
async def welcome_del_image(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    await db.execute(
        "UPDATE settings SET welcome_image_file_id='' WHERE owner_id=$1",
        call.from_user.id,
    )
    db.cache_invalidate(f"setting:{call.from_user.id}:")
    await call.answer("تم حذف الصورة ✅", show_alert=True)
    await welcome_menu(call, None)  # type: ignore


@router.callback_query(F.data == "welcome_del_sticker")
async def welcome_del_sticker(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    await db.execute(
        "UPDATE settings SET welcome_sticker_file_id='' WHERE owner_id=$1",
        call.from_user.id,
    )
    db.cache_invalidate(f"setting:{call.from_user.id}:")
    await call.answer("تم حذف الملصق ✅", show_alert=True)
    await welcome_menu(call, None)  # type: ignore


@router.callback_query(F.data == "welcome_preview")
async def welcome_preview(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT welcome, welcome_image_file_id, welcome_sticker_file_id "
        "FROM settings WHERE owner_id=$1",
        owner,
    )
    if not row:
        await call.answer("لا توجد بيانات.", show_alert=True)
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    try:
        if row["welcome_image_file_id"]:
            await call.message.answer_photo(
                row["welcome_image_file_id"],
                caption=row["welcome"] or None,
                parse_mode="HTML",
            )
        elif row["welcome"]:
            await call.message.answer(row["welcome"], parse_mode="HTML")
        if row["welcome_sticker_file_id"]:
            await call.message.answer_sticker(row["welcome_sticker_file_id"])
        await call.message.answer(
            "👁 هذه معاينة الترحيب.",
            reply_markup=back_kb("welcome"),
        )
    except Exception:
        log.exception("welcome.preview.failed")
        await call.message.answer(
            "تعذّر عرض المعاينة.",
            reply_markup=back_kb("welcome"),
        )
    await call.answer()


# ========== الردود التلقائية ==========
def replies_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} الردود (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
            callback_data="section_toggle_replies",
        )],
        [InlineKeyboardButton(text="إضافة رد جديد", callback_data="reply_add"),
         InlineKeyboardButton(text="حذف رد", callback_data="reply_del_list")],
        [InlineKeyboardButton(text="عرض الردود", callback_data="reply_list"),
         InlineKeyboardButton(text="تعديل رد", callback_data="reply_edit_list")],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "replies")
async def replies_menu(
    call: CallbackQuery, state: FSMContext | None = None
) -> None:
    if not call.from_user:
        return
    if state is not None:
        await state.clear()
    owner = call.from_user.id
    count = await db.fetchval(
        "SELECT COUNT(*) FROM replies WHERE owner_id=$1", owner
    )
    enabled = await db.fetchval(
        "SELECT replies_enabled FROM settings WHERE owner_id=$1", owner
    )
    await edit_or_replace(
        call,
        f"<b>💬 الردود التلقائية</b>\n\n"
        f"عدد الردود: <b>{count}</b>\n"
        f"الحالة: <b>{'مفعّل ✅' if enabled else 'معطل ❌'}</b>",
        replies_kb(bool(enabled)),
    )
    await call.answer()


@router.callback_query(F.data == "section_toggle_replies")
async def section_toggle_replies(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT replies_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET replies_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await replies_menu(call, None)  # type: ignore
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"الردود الآن: {state_text}")


@router.callback_query(F.data == "reply_add")
async def reply_add(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.update_data(reply_owner=call.from_user.id, buttons=[])
    await state.set_state(ReplySG.add_trigger)
    await edit_or_replace(
        call,
        "<b>الخطوة 1 من 2</b>\n\nأرسل الكلمة المفتاحية.",
        cancel_kb(),
    )
    await call.answer()


@router.message(ReplySG.add_trigger)
async def reply_get_trigger(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    trigger = (message.text or "").strip().lower()
    if not trigger:
        await message.answer("اكتب كلمة صحيحة.")
        return
    existing = await db.fetchrow(
        "SELECT id FROM replies WHERE owner_id=$1 AND LOWER(trigger)=$2",
        message.from_user.id, trigger,
    )
    if existing:
        await message.answer(
            f"⚠️ الكلمة <code>{safe_html(trigger)}</code> موجودة مسبقاً "
            f"في الردود (ID: {existing['id']}).\n"
            f"استخدم زر <b>تعديل رد</b> لتحريرها، أو أرسل كلمة أخرى.",
            parse_mode="HTML",
        )
        return
    await state.update_data(trigger=trigger)
    await state.set_state(ReplySG.add_response)
    await message.answer(
        "<b>الخطوة 2 من 2</b>\n\nأرسل نص الرد.",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(ReplySG.add_response)
async def reply_get_response(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    response = (message.html_text or message.text or "").strip()
    if not response:
        await message.answer("اكتب ردًا صحيحًا.")
        return
    data = await state.get_data()
    owner = data.get("reply_owner") or message.from_user.id
    trigger = data.get("trigger")
    buttons = data.get("buttons", [])

    async with db.transaction() as conn:
        reply_id = await conn.fetchval(
            "INSERT INTO replies(owner_id, trigger, response) "
            "VALUES($1, $2, $3) RETURNING id",
            owner, trigger, response,
        )
        for i, b in enumerate(buttons):
            await conn.execute(
                """INSERT INTO reply_buttons
                     (reply_id, label, btn_type, btn_color,
                      btn_value, position)
                   VALUES($1, $2, $3, $4, $5, $6)""",
                reply_id, b["label"], b["type"],
                b.get("color", "primary"),
                b["value"], i,
            )
    await state.clear()
    await message.answer(
        fancy_success(f"تم حفظ الرد: <code>{safe_html(trigger)}</code>"),
        reply_markup=back_kb("replies"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "reply_list")
async def reply_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT id, trigger, response FROM replies "
        "WHERE owner_id=$1 ORDER BY id DESC LIMIT 30",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(call, "لا توجد ردود.", replies_kb(True))
        await call.answer()
        return
    parts = []
    for r in rows:
        parts.append(
            f"<b>#{r['id']}</b>\n"
            f"الكلمة: <code>{safe_html(r['trigger'])}</code>\n"
            f"الرد: {safe_html(r['response'][:100])}"
        )
    await edit_or_replace(
        call,
        "<b>قائمة الردود</b>\n\n" + "\n\n".join(parts),
        replies_kb(True),
    )
    await call.answer()


@router.callback_query(F.data == "reply_del_list")
async def reply_del_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT id, trigger FROM replies "
        "WHERE owner_id=$1 ORDER BY id DESC LIMIT 30",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(call, "لا توجد ردود.", replies_kb(True))
        return
    kb = [
        [InlineKeyboardButton(
            text=f"#{r['id']} - {r['trigger'][:30]}",
            callback_data=f"rdel_{r['id']}",
        )] for r in rows
    ]
    kb.append([InlineKeyboardButton(text="رجوع", callback_data="replies")])
    await edit_or_replace(
        call, "<b>اختر الرد للحذف:</b>",
        InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await call.answer()


@router.callback_query(F.data.startswith("rdel_"))
async def reply_del_confirm(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rid = int(call.data.split("_")[-1])
    row = await db.fetchrow(
        "SELECT trigger FROM replies WHERE id=$1 AND owner_id=$2",
        rid, call.from_user.id,
    )
    if not row:
        await call.answer("غير موجود.", show_alert=True)
        return
    await edit_or_replace(
        call,
        f"<b>تأكيد الحذف</b>\n\n<code>{safe_html(row['trigger'])}</code>",
        confirm_kb("rdel", str(rid)),
    )
    await call.answer()


@router.callback_query(F.data == "reply_edit_list")
async def reply_edit_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT id, trigger FROM replies "
        "WHERE owner_id=$1 ORDER BY id DESC LIMIT 30",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(call, "لا توجد ردود.", replies_kb(True))
        return
    kb = [
        [InlineKeyboardButton(
            text=f"#{r['id']} - {r['trigger'][:30]}",
            callback_data=f"redit_{r['id']}",
        )] for r in rows
    ]
    kb.append([InlineKeyboardButton(text="رجوع", callback_data="replies")])
    await edit_or_replace(
        call, "<b>اختر الرد:</b>",
        InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await call.answer()


@router.callback_query(F.data.regexp(r"^redit_\d+$"))
async def reply_edit_menu(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rid = int(call.data.split("_")[-1])
    row = await db.fetchrow(
        "SELECT id, trigger, response FROM replies "
        "WHERE id=$1 AND owner_id=$2",
        rid, call.from_user.id,
    )
    if not row:
        await call.answer("غير موجود.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="تعديل الكلمة", callback_data=f"redit_trigger_{rid}",
        )],
        [InlineKeyboardButton(
            text="تعديل النص", callback_data=f"redit_response_{rid}",
        )],
        [InlineKeyboardButton(
            text="حذف الأزرار", callback_data=f"redit_clearbtns_{rid}",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="reply_edit_list")],
    ])
    await edit_or_replace(
        call,
        f"<b>تعديل #{rid}</b>\n\n"
        f"الكلمة: <code>{safe_html(row['trigger'])}</code>\n"
        f"الرد: {safe_html(row['response'][:200])}",
        kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("redit_trigger_"))
async def redit_trigger(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    rid = int(call.data.split("_")[-1])
    await state.update_data(edit_rid=rid)
    await state.set_state(ReplySG.edit_trigger)
    await edit_or_replace(call, "<b>أرسل الكلمة الجديدة.</b>", cancel_kb())
    await call.answer()


@router.message(ReplySG.edit_trigger)
async def redit_trigger_save(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    text = (message.text or "").strip().lower()
    if not text:
        await message.answer("أرسل كلمة صحيحة.")
        return
    data = await state.get_data()
    rid = data.get("edit_rid")
    existing = await db.fetchrow(
        "SELECT id FROM replies WHERE owner_id=$1 AND LOWER(trigger)=$2 AND id<>$3",
        message.from_user.id, text, rid,
    )
    if existing:
        await message.answer(
            f"⚠️ الكلمة <code>{safe_html(text)}</code> موجودة مسبقاً "
            f"في رد آخر (ID: {existing['id']}).",
            parse_mode="HTML",
        )
        return
    await db.execute(
        "UPDATE replies SET trigger=$1 WHERE id=$2 AND owner_id=$3",
        text, rid, message.from_user.id,
    )
    await state.clear()
    await message.answer(
        fancy_success(f"تم التعديل: <code>{safe_html(text)}</code>"),
        reply_markup=back_kb("replies"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("redit_response_"))
async def redit_response(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    rid = int(call.data.split("_")[-1])
    await state.update_data(edit_rid=rid, bot_message_key=None)
    await state.set_state(ReplySG.edit_response)
    await edit_or_replace(call, "<b>أرسل النص الجديد.</b>", cancel_kb())
    await call.answer()


@router.message(ReplySG.edit_response)
async def redit_response_save(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    data = await state.get_data()

    if data.get("bot_message_key"):
        text = (message.html_text or message.text or "").strip()
        if not text:
            await message.answer("أرسل نصًا صحيحًا.")
            return
        from Database import set_account_message
        await set_account_message(
            message.from_user.id, data["bot_message_key"], text
        )
        await state.clear()
        await message.answer(
            fancy_success("تم حفظ الرسالة."),
            reply_markup=back_kb("custom_messages"),
            parse_mode="HTML",
        )
        return

    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("أرسل نصاً صحيحاً.")
        return
    await db.execute(
        "UPDATE replies SET response=$1 WHERE id=$2 AND owner_id=$3",
        text, data.get("edit_rid"), message.from_user.id,
    )
    await state.clear()
    await message.answer(
        fancy_success("تم تعديل نص الرد."),
        reply_markup=back_kb("replies"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("redit_clearbtns_"))
async def redit_clear(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rid = int(call.data.split("_")[-1])
    result = await db.execute("DELETE FROM reply_buttons WHERE reply_id=$1", rid)
    if result == "DELETE 0":
        await call.answer("لا توجد أزرار لحذفها.", show_alert=True)
        return
    await call.answer("تم حذف الأزرار.")


# ========== ميزة ممنوع السب ==========
_SWEAR_SEED_FLAG: dict[int, bool] = {}


async def _ensure_swear_seeded(owner_id: int) -> None:
    """يزرع الشتائم الافتراضية مرة واحدة لكل مالك."""
    if _SWEAR_SEED_FLAG.get(owner_id):
        return
    from Database import ensure_settings as _es
    await _es(owner_id)
    already = await db.fetchval(
        "SELECT 1 FROM banned_words WHERE owner_id=$1 AND kind='swear' LIMIT 1",
        owner_id,
    )
    if already:
        _SWEAR_SEED_FLAG[owner_id] = True
        return
    from Database import DEFAULT_SWEAR_WORDS
    for w in DEFAULT_SWEAR_WORDS:
        try:
            await db.execute(
                "INSERT INTO banned_words(owner_id, word, kind) "
                "VALUES($1, $2, 'swear')",
                owner_id, w.lower(),
            )
        except Exception:
            pass
    _SWEAR_SEED_FLAG[owner_id] = True


def swear_kb(enabled: bool, auto_mute: bool, count: int, mute_after: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'قفل السب ✅' if enabled else 'فتح السب ❌'}",
            callback_data="swear_toggle",
        )],
        [InlineKeyboardButton(
            text=f"الكتم التلقائي: {'مفعّل ✅' if auto_mute else 'معطّل ❌'}",
            callback_data="swear_toggle_mute",
        )],
        [InlineKeyboardButton(
            text=f"عدد المخالفات قبل الكتم: {mute_after}",
            callback_data="swear_set_mute_after",
        )],
        [InlineKeyboardButton(
            text="➕ إضافة كلمة", callback_data="swear_add"
        ), InlineKeyboardButton(
            text="🗑 حذف كلمة", callback_data="swear_del_list"
        )],
        [InlineKeyboardButton(
            text=f"📋 عرض القائمة ({count})", callback_data="swear_list"
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


async def _render_swear(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    await ensure_settings(owner)
    await _ensure_swear_seeded(owner)
    row = await db.fetchrow(
        "SELECT swear_filter_enabled, swear_auto_mute, swear_mute_after "
        "FROM settings WHERE owner_id=$1",
        owner,
    )
    enabled = bool(row["swear_filter_enabled"]) if row else True
    auto_mute = bool(row["swear_auto_mute"]) if row else True
    mute_after = int(row["swear_mute_after"] or 4) if row else 4
    count = int(await db.fetchval(
        "SELECT COUNT(*) FROM banned_words WHERE owner_id=$1 AND kind='swear'",
        owner,
    ) or 0)
    text = (
        "<b>🚫 ممنوع السب / فلتر الشتيمة</b>\n\n"
        f"الحالة: <b>{'مقفل ✅' if enabled else 'مفتوح ❌'}</b>\n"
        f"الكتم التلقائي: <b>{'مفعّل ✅' if auto_mute else 'معطّل ❌'}</b>\n"
        f"عدد المخالفات قبل الكتم: <b>{mute_after}</b>\n"
        f"كلمات الشتائم: <b>{count}</b>\n\n"
        "<blockquote>"
        "عند قفل السب: أي رسالة تحتوي شتيمة تُحذف فوراً ويُرد على العضو.\n"
        "القائمة الافتراضية تعمل تلقائياً + يمكنك إضافة كلماتك."
        "</blockquote>"
    )
    await edit_or_replace(call, text, swear_kb(enabled, auto_mute, count, mute_after))


@router.callback_query(F.data == "swear_filter")
async def swear_menu(call: CallbackQuery, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    await _render_swear(call)
    await call.answer()


@router.callback_query(F.data == "swear_toggle")
async def swear_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT swear_filter_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET swear_filter_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    _get_banned_cache_invalidator()(owner)
    await _render_swear(call)
    await call.answer("تم قفل السب ✅" if new_val else "تم فتح السب ❌")


@router.callback_query(F.data == "swear_toggle_mute")
async def swear_toggle_mute(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT swear_auto_mute FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET swear_auto_mute=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    await _render_swear(call)
    await call.answer(f"الكتم التلقائي: {'مفعّل' if new_val else 'معطّل'}")


@router.callback_query(F.data == "swear_set_mute_after")
async def swear_set_mute_after(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.set_state(SwearSG.mute_after)
    await edit_or_replace(
        call, "<b>أرسل عدد المخالفات قبل الكتم (1-20).</b>", cancel_kb(),
    )
    await call.answer()


@router.message(SwearSG.mute_after)
async def swear_mute_after_save(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    try:
        v = int(message.text.strip())
        if not 1 <= v <= 20:
            raise ValueError
    except ValueError:
        await message.answer("أرسل رقماً بين 1 و 20.")
        return
    await db.execute(
        "UPDATE settings SET swear_mute_after=$1 WHERE owner_id=$2",
        v, message.from_user.id,
    )
    await state.clear()
    await message.answer(
        fancy_success(f"تم ضبط عدد المخالفات: {v}"),
        reply_markup=back_kb("swear_filter"), parse_mode="HTML",
    )


@router.callback_query(F.data == "swear_add")
async def swear_add(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.set_state(SwearSG.add_word)
    await edit_or_replace(
        call, "<b>أرسل الكلمة أو العبارة التي تريد حظرها.</b>", cancel_kb(),
    )
    await call.answer()


@router.message(SwearSG.add_word)
async def swear_add_save(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    raw = message.text.strip().lower()
    if len(raw) < 2:
        await message.answer("أرسل كلمة صحيحة (حرفان على الأقل).")
        return
    owner = message.from_user.id
    existing = await db.fetchrow(
        "SELECT id FROM banned_words "
        "WHERE owner_id=$1 AND kind='swear' AND LOWER(word)=$2",
        owner, raw,
    )
    if existing:
        await state.clear()
        await message.answer(
            f"⚠️ الكلمة <code>{safe_html(raw)}</code> موجودة مسبقاً.",
            reply_markup=back_kb("swear_filter"), parse_mode="HTML",
        )
        return
    await db.execute(
        "INSERT INTO banned_words(owner_id, word, kind) VALUES($1, $2, 'swear')",
        owner, raw,
    )
    _get_banned_cache_invalidator()(owner)
    await state.clear()
    await audit(owner, "swear_add", raw)
    await message.answer(
        fancy_success(f"تمت إضافة: <code>{safe_html(raw)}</code>"),
        reply_markup=back_kb("swear_filter"), parse_mode="HTML",
    )


@router.callback_query(F.data == "swear_list")
async def swear_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT id, word FROM banned_words "
        "WHERE owner_id=$1 AND kind='swear' ORDER BY id DESC LIMIT 80",
        call.from_user.id,
    )
    if not rows:
        body = "القائمة فارغة."
    else:
        body = "\n".join(
            f"<b>#{r['id']}</b> - <code>{safe_html(r['word'])}</code>"
            for r in rows
        )
    await edit_or_replace(
        call,
        f"<b>📋 كلمات الشتائم ({len(rows)})</b>\n\n{body}",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ إضافة", callback_data="swear_add"),
             InlineKeyboardButton(text="🗑 حذف", callback_data="swear_del_list")],
            [InlineKeyboardButton(text="رجوع", callback_data="swear_filter")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "swear_del_list")
async def swear_del_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT id, word FROM banned_words "
        "WHERE owner_id=$1 AND kind='swear' ORDER BY id DESC LIMIT 50",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(call, "لا توجد كلمات لحذفها.", back_kb("swear_filter"))
        await call.answer()
        return
    kb = [
        [InlineKeyboardButton(
            text=f"#{r['id']} - {r['word'][:30]}",
            callback_data=f"swear_del_{r['id']}",
        )] for r in rows
    ]
    kb.append([InlineKeyboardButton(text="رجوع", callback_data="swear_filter")])
    await edit_or_replace(
        call, "<b>اختر الكلمة للحذف:</b>",
        InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await call.answer()


@router.callback_query(F.data.startswith("swear_del_"))
async def swear_del(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    wid = int(call.data.split("_")[-1])
    result = await db.execute(
        "DELETE FROM banned_words WHERE id=$1 AND owner_id=$2 AND kind='swear'",
        wid, call.from_user.id,
    )
    deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
    _get_banned_cache_invalidator()(call.from_user.id)
    if deleted:
        await call.answer("تم الحذف ✅")
    else:
        await call.answer("غير موجودة.", show_alert=True)
    await _render_swear(call)


# ========== المكتومين ==========
def muted_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} المكتومين (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
            callback_data="section_toggle_muted",
        )],
        [InlineKeyboardButton(text="كتم عميل", callback_data="mute_add"),
         InlineKeyboardButton(text="رفع كتم", callback_data="mute_unmute_list")],
        [InlineKeyboardButton(text="عرض المكتومين", callback_data="mute_list")],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "muted")
async def muted_menu(
    call: CallbackQuery, state: FSMContext | None = None
) -> None:
    if not call.from_user:
        return
    if state is not None:
        await state.clear()
    count = await db.fetchval(
        "SELECT COUNT(*) FROM muted WHERE connection_id IN "
        "(SELECT connection_id FROM business WHERE user_id=$1)",
        call.from_user.id,
    )
    enabled = await db.fetchval(
        "SELECT muted_enabled FROM settings WHERE owner_id=$1",
        call.from_user.id,
    )
    await edit_or_replace(
        call,
        f"<b>🔇 المكتومون</b>\n\n"
        f"عدد: <b>{count}</b>\n"
        f"الحالة: <b>{'مفعّل ✅' if enabled else 'معطل ❌'}</b>\n\n"
        "للكتم اكتب كلمة (كتم) في الشات.\n"
        "لإلغاء الكتم اكتب (الغاء الكتم).",
        muted_kb(bool(enabled)),
    )
    await call.answer()


@router.callback_query(F.data == "section_toggle_muted")
async def section_toggle_muted(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT muted_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET muted_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await muted_menu(call, None)  # type: ignore
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"المكتومين الآن: {state_text}")


@router.callback_query(F.data == "mute_add")
async def mute_add(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.set_state(MuteSG.chat_id)
    await edit_or_replace(call, "<b>أرسل آيدي العميل المراد كتمه.</b>", cancel_kb())
    await call.answer()


@router.message(MuteSG.chat_id)
async def mute_add_save(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    try:
        cid = int((message.text or "").strip())
    except ValueError:
        await message.answer("أرسل آيدي رقمي.")
        return
    if cid == settings.owner_id:
        await state.clear()
        await message.answer(
            "هذا الحساب محمي.",
            reply_markup=back_kb("muted"),
            parse_mode="HTML",
        )
        return
    rows = await db.fetch(
        "SELECT connection_id FROM business "
        "WHERE user_id=$1 AND enabled=TRUE",
        message.from_user.id,
    )
    added = 0
    for row in rows:
        result = await db.execute(
            "INSERT INTO muted(connection_id, chat_id) "
            "VALUES($1, $2) ON CONFLICT DO NOTHING",
            row["connection_id"], cid,
        )
        if result == "INSERT 0 1":
            added += 1
    await state.clear()
    if added == 0:
        await message.answer(
            f"⚠️ العميل <code>{cid}</code> مكتوم بالفعل.",
            reply_markup=back_kb("muted"),
            parse_mode="HTML",
        )
        return
    await message.answer(
        fancy_success(f"تم كتم العميل <code>{cid}</code>."),
        reply_markup=back_kb("muted"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "mute_list")
async def mute_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT DISTINCT m.chat_id, c.first_name, c.username "
        "FROM muted m "
        "LEFT JOIN customers c ON c.chat_id = m.chat_id "
        "WHERE m.connection_id IN "
        "(SELECT connection_id FROM business WHERE user_id=$1) LIMIT 100",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(
            call, "<b>المكتومون</b>\n\nلا يوجد مكتومون حالياً.", muted_kb(True)
        )
        await call.answer()
        return
    kb_rows = []
    for r in rows:
        name = r["first_name"] or "بدون اسم"
        username = r["username"]
        display = f"@{username}" if username else name
        kb_rows.append([InlineKeyboardButton(
            text=f"🔇 {display[:35]}",
            callback_data=f"munmute_{r['chat_id']}",
        )])
    kb_rows.append([InlineKeyboardButton(text="رجوع", callback_data="muted")])
    await edit_or_replace(
        call,
        "<b>المكتومون</b>\n\n"
        "<blockquote>اضغط على الاسم لرفع الكتم.</blockquote>",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await call.answer()


@router.callback_query(F.data == "mute_unmute_list")
async def mute_unmute_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT DISTINCT m.chat_id, c.first_name, c.username "
        "FROM muted m "
        "LEFT JOIN customers c ON c.chat_id = m.chat_id "
        "WHERE m.connection_id IN "
        "(SELECT connection_id FROM business WHERE user_id=$1) LIMIT 50",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(call, "لا يوجد مكتومون.", muted_kb(True))
        await call.answer()
        return
    kb_rows = []
    for r in rows:
        name = r["first_name"] or "بدون اسم"
        username = r["username"]
        display = f"@{username}" if username else name
        kb_rows.append([InlineKeyboardButton(
            text=f"🔊 {display[:35]}",
            callback_data=f"munmute_{r['chat_id']}",
        )])
    kb_rows.append([InlineKeyboardButton(text="رجوع", callback_data="muted")])
    await edit_or_replace(
        call,
        "<b>اختر العميل لرفع الكتم:</b>",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("munmute_"))
async def mute_unmute(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    cid = int(call.data.split("_")[-1])
    result = await db.execute(
        "DELETE FROM muted WHERE chat_id=$1 AND connection_id IN "
        "(SELECT connection_id FROM business WHERE user_id=$2)",
        cid, call.from_user.id,
    )
    if result == "DELETE 0":
        await call.answer("العميل غير مكتوم.", show_alert=True)
        return
    await db.execute(
        "DELETE FROM violations WHERE chat_id=$1 AND connection_id IN "
        "(SELECT connection_id FROM business WHERE user_id=$2)",
        cid, call.from_user.id,
    )
    await call.answer("تم رفع الكتم وتصفير العدادات.")
    await mute_unmute_list(call)


# ========== تأكيد الحذف الموحد ==========
@router.callback_query(F.data.startswith("confirm_"))
async def confirm_action(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    parts = call.data.split("_")
    if len(parts) < 3:
        await call.answer()
        return
    action, item_id = parts[1], int(parts[2])
    deleted = 0
    if action == "rdel":
        result = await db.execute(
            "DELETE FROM replies WHERE id=$1 AND owner_id=$2",
            item_id, call.from_user.id,
        )
        deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
    elif action == "fdel":
        if call.from_user.id != settings.owner_id:
            await call.answer("للمالك فقط.", show_alert=True)
            return
        result = await db.execute(
            "DELETE FROM force_channels "
            "WHERE id=$1 AND owner_id=$2 AND scope='bot'",
            item_id, settings.owner_id,
        )
        deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
        _get_force_cache_invalidator()(settings.owner_id)
    elif action == "afdel":
        result = await db.execute(
            "DELETE FROM force_channels "
            "WHERE id=$1 AND owner_id=$2 AND scope='account'",
            item_id, call.from_user.id,
        )
        deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
        _get_force_cache_invalidator()(call.from_user.id)

    if deleted == 0:
        await call.answer("العنصر غير موجود.", show_alert=True)
    else:
        await call.answer("تم الحذف.")
    await show_main(call)


# ========== السوشيال ميديا ==========
SOCIAL_PLATFORMS = {
    "tiktok": {
        "name": "تيك توك",
        "domains": ("tiktok.com",),
    },
    "instagram": {
        "name": "انستقرام",
        "domains": ("instagram.com",),
    },
    "telegram": {
        "name": "تليجرام",
        "domains": ("t.me", "telegram.me", "telegram.org"),
    },
    "facebook": {
        "name": "فيسبوك",
        "domains": ("facebook.com", "fb.com"),
    },
    "youtube": {
        "name": "يوتيوب",
        "domains": ("youtube.com", "youtu.be"),
    },
}


def social_kb(enabled: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{'إلغاء تفعيل' if enabled else 'تفعيل'} السوشيال (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
        callback_data="social_toggle",
    )]]
    platform_buttons = [
        InlineKeyboardButton(
            text=info["name"], callback_data=f"social_edit_{key}",
        )
        for key, info in SOCIAL_PLATFORMS.items()
    ]
    for i in range(0, len(platform_buttons), 2):
        rows.append(platform_buttons[i:i + 2])
    rows.append([InlineKeyboardButton(text="رجوع", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_social(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT social_enabled, social_tiktok, social_instagram, "
        "social_telegram, social_facebook, social_youtube "
        "FROM settings WHERE owner_id=$1",
        owner,
    )
    if not row:
        await ensure_settings(owner)
        row = await db.fetchrow(
            "SELECT * FROM settings WHERE owner_id=$1", owner
        )
    lines = [
        f"الحالة: <b>{'مفعّل ✅' if row['social_enabled'] else 'معطل ❌'}</b>"
    ]
    for key, info in SOCIAL_PLATFORMS.items():
        val = row[f"social_{key}"] or "غير مضاف"
        lines.append(f"{info['name']}: {safe_html(val)}")
    await edit_or_replace(
        call,
        "<b>📱 إدارة السوشيال ميديا</b>\n\n<blockquote>"
        + "\n".join(lines) + "</blockquote>",
        social_kb(bool(row["social_enabled"])),
    )


@router.callback_query(F.data == "social_media")
async def social_media(call: CallbackQuery) -> None:
    await render_social(call)
    await call.answer()


@router.callback_query(F.data == "social_toggle")
async def social_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT social_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET social_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    await render_social(call)
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"السوشيال الآن: {state_text}")


@router.callback_query(F.data.startswith("social_edit_"))
async def social_edit(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    platform = call.data[len("social_edit_"):]
    if platform not in SOCIAL_PLATFORMS:
        await call.answer()
        return
    await state.update_data(
        social_platform=platform, social_owner=call.from_user.id
    )
    await state.set_state(SocialSG.value)
    await edit_or_replace(
        call,
        f"<b>أرسل رابط {SOCIAL_PLATFORMS[platform]['name']}</b>\n\n"
        "أو اكتب <code>حذف</code>.",
        cancel_kb(),
    )
    await call.answer()


@router.message(SocialSG.value)
async def social_save(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    value = (message.text or "").strip()
    platform = data.get("social_platform")
    owner = data.get("social_owner")

    if platform not in SOCIAL_PLATFORMS:
        await state.clear()
        return

    if value == "حذف":
        current = await db.fetchval(
            f"SELECT social_{platform} FROM settings WHERE owner_id=$1",
            owner,
        )
        if not current:
            await state.clear()
            await message.answer(
                f"⚠️ رابط {SOCIAL_PLATFORMS[platform]['name']} غير موجود أصلاً.",
                reply_markup=back_kb("social_media"),
                parse_mode="HTML",
            )
            return
        value = ""
    elif value:
        try:
            parsed = urlparse(value)
            host = (
                (parsed.netloc or "").lower().split(":")[0].removeprefix("www.")
            )
            if parsed.scheme not in {"http", "https"}:
                raise ValueError
            if not any(
                host == d or host.endswith("." + d)
                for d in SOCIAL_PLATFORMS[platform]["domains"]
            ):
                raise ValueError
        except Exception:
            await message.answer("الرابط لا يطابق المنصة المطلوبة.")
            return

    await db.execute(
        f"UPDATE settings SET social_{platform}=$1 WHERE owner_id=$2",
        value, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await state.clear()
    if value:
        await message.answer(
            fancy_success(f"تم حفظ رابط {SOCIAL_PLATFORMS[platform]['name']}."),
            reply_markup=back_kb("social_media"),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            fancy_success(f"تم حذف رابط {SOCIAL_PLATFORMS[platform]['name']}."),
            reply_markup=back_kb("social_media"),
            parse_mode="HTML",
        )


# ========== إدارة الميديا ==========
MEDIA_TYPES = [
    ("text", "نص"),
    ("photo", "صور"),
    ("video", "فيديو"),
    ("sticker", "ملصقات"),
    ("document", "ملفات"),
    ("audio", "صوتيات"),
    ("voice", "بصمات"),
    ("animation", "متحركة"),
]


def media_kb(enabled: bool, types_set: set) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{'تعطيل' if enabled else 'تفعيل'} إدارة الميديا (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
        callback_data="media_toggle",
    )]]
    btns = []
    for key, label in MEDIA_TYPES:
        active = key in types_set
        btns.append(InlineKeyboardButton(
            text=f"{'✅' if active else '❌'} {label}",
            callback_data=f"media_type_{key}",
        ))
    for i in range(0, len(btns), 2):
        rows.append(btns[i:i + 2])
    rows.append([InlineKeyboardButton(text="رجوع", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "media_manager")
async def media_manager(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT media_enabled, media_delete_types "
        "FROM settings WHERE owner_id=$1",
        owner,
    )
    if not row:
        await ensure_settings(owner)
        row = {"media_enabled": True, "media_delete_types": []}
    types_set = set(row["media_delete_types"] or [])
    await edit_or_replace(
        call,
        "<b>📷 إدارة الميديا</b>\n\n"
        f"الحالة: <b>{'مفعّل ✅' if row['media_enabled'] else 'معطل ❌'}</b>\n\n"
        "<blockquote>اختر أنواع الرسائل التي تُحذف تلقائيًا "
        "من العملاء.</blockquote>",
        media_kb(bool(row["media_enabled"]), types_set),
    )
    await call.answer()


@router.callback_query(F.data == "media_toggle")
async def media_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT media_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET media_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await media_manager(call)
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"إدارة الميديا الآن: {state_text}")


@router.callback_query(F.data.startswith("media_type_"))
async def media_type_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    key = call.data[len("media_type_"):]
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT media_delete_types FROM settings WHERE owner_id=$1", owner
    )
    types_set = set(row["media_delete_types"] or []) if row else set()
    if key in types_set:
        types_set.discard(key)
        action = "تم تعطيل"
    else:
        types_set.add(key)
        action = "تم تفعيل"
    await db.execute(
        "UPDATE settings SET media_delete_types=$1 WHERE owner_id=$2",
        list(types_set), owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await media_manager(call)
    type_name = dict(MEDIA_TYPES).get(key, key)
    await call.answer(f"{action}: {type_name}")
# ========== الاشتراك الإجباري للحساب ==========
def account_force_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} الاشتراك الإجباري (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
            callback_data="account_force_toggle",
        )],
        [InlineKeyboardButton(text="إضافة قناة", callback_data="account_force_add"),
         InlineKeyboardButton(text="حذف قناة", callback_data="account_force_del_list")],
        [InlineKeyboardButton(text="عرض القنوات", callback_data="account_force_list")],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "account_force")
async def account_force_menu(
    call: CallbackQuery, state: FSMContext | None = None
) -> None:
    if not call.from_user:
        return
    if not await is_account_linked(call.from_user.id):
        await call.answer("اربط حساب Business أولًا.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    owner = call.from_user.id
    count = await db.fetchval(
        "SELECT COUNT(*) FROM force_channels "
        "WHERE owner_id=$1 AND scope='account'",
        owner,
    )
    enabled = await db.fetchval(
        "SELECT account_force_enabled FROM settings WHERE owner_id=$1",
        owner,
    )
    await edit_or_replace(
        call,
        f"<b>الاشتراك الإجباري للحساب</b>\n\n<blockquote>"
        f"الحالة: <b>{'مفعّل ✅' if enabled else 'معطل ❌'}</b>\n"
        f"عدد القنوات: <b>{count}</b>\n"
        f"هذه القنوات تخص حسابك فقط.</blockquote>",
        account_force_kb(bool(enabled)),
    )
    await call.answer()


@router.callback_query(F.data == "account_force_toggle")
async def account_force_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT account_force_enabled FROM settings WHERE owner_id=$1",
        owner,
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET account_force_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await account_force_menu(call, None)  # type: ignore
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"الاشتراك الإجباري الآن: {state_text}")


@router.callback_query(F.data == "account_force_add")
async def account_force_add(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.update_data(
        force_owner=call.from_user.id, force_scope="account"
    )
    await state.set_state(ForceSG.add_channel)
    await edit_or_replace(
        call,
        "<b>إضافة قناة للحساب</b>\n\n"
        "أرسل أحد الصيغ التالية:\n"
        "• رابط: <code>https://t.me/username</code>\n"
        "• يوزر: <code>@username</code>\n"
        "• رابط خاص: <code>https://t.me/c/123456789/10</code>\n"
        "• ID: <code>-1001234567890</code>\n\n"
        "البوت يجب أن يكون مشرفًا فيها.",
        cancel_kb(),
    )
    await call.answer()


@router.message(ForceSG.add_channel)
async def force_add_channel(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer(
            "⚠️ تم إلغاء العملية لأنك أرسلت أمر.",
            reply_markup=back_kb("account_force"),
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    scope = data.get("force_scope", "account")
    owner = data.get("force_owner") or message.from_user.id

    kind, value = parse_channel_input(text)
    if not value:
        await state.clear()
        await message.answer(
            "❌ رابط غير صالح",
            reply_markup=back_kb("account_force"),
        )
        return

    stored_key = f"@{value}" if kind == "username" else value

    existing = await db.fetchrow(
        "SELECT id FROM force_channels "
        "WHERE owner_id=$1 AND scope=$2 AND channel=$3",
        owner, scope, stored_key,
    )
    if existing:
        await state.clear()
        await message.answer(
            f"⚠️ القناة <code>{safe_html(stored_key)}</code> موجودة مسبقاً.",
            reply_markup=back_kb("account_force"),
            parse_mode="HTML",
        )
        return

    ok, info, resolved = await verify_bot_is_admin(message.bot, text)
    if not ok:
        await state.clear()
        bot_username = ""
        try:
            me = await message.bot.get_me()
            bot_username = me.username or ""
        except Exception:
            pass
        await message.answer(
            f"❌ <b>لا يمكن إضافة القناة</b>\n\n"
            f"{safe_html(info)}\n\n"
            f"<blockquote>تأكد من أن البوت مشرف (Admin) في القناة "
            f"مع تفعيل صلاحيات «دعوة المستخدمين» أو «حذف الرسائل»، "
            f"ثم أرسل الرابط مرة أخرى.</blockquote>",
            reply_markup=force_add_error_kb(
                bot_username, "account_force_add", "account_force",
            ),
            parse_mode="HTML",
        )
        return

    final_chat_id = int(resolved) if str(resolved).lstrip("-").isdigit() else 0
    await db.execute(
        "INSERT INTO force_channels(owner_id, channel, chat_id, title, scope) "
        "VALUES($1, $2, $3, $4, $5)",
        owner, stored_key, final_chat_id, info, scope,
    )
    _get_force_cache_invalidator()(owner)

    count = await db.fetchval(
        "SELECT COUNT(*) FROM force_channels WHERE owner_id=$1 AND scope=$2",
        owner, scope,
    )
    await state.clear()

    if kind == "username":
        final_link = f"https://t.me/{value}"
    elif final_chat_id:
        final_link = f"https://t.me/c/{str(final_chat_id).replace('-100', '', 1)}"
    else:
        final_link = value

    await message.answer(
        f"✅ <b>تمت إضافة القناة بنجاح</b>\n\n"
        f"📢 <b>الاسم:</b> {safe_html(info)}\n"
        f"🔗 <b>الرابط:</b> {final_link}\n"
        f"📊 <b>إجمالي القنوات:</b> <b>{count}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📋 عرض القنوات", callback_data="account_force_list",
            )],
            [InlineKeyboardButton(
                text="رجوع", callback_data="account_force",
            )],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "account_force_list")
async def force_channels_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    scope = "account"
    owner = call.from_user.id

    rows = await db.fetch(
        "SELECT id, channel, chat_id, title FROM force_channels "
        "WHERE owner_id=$1 AND scope=$2 ORDER BY id",
        owner, scope,
    )

    back_cb = "account_force"
    add_cb = "account_force_add"
    del_cb = "account_force_del_list"

    if not rows:
        await edit_or_replace(
            call,
            "<b>📋 قنوات الاشتراك الإجباري</b>\n\n"
            "<blockquote>لا توجد قنوات مضافة حالياً.</blockquote>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ إضافة قناة", callback_data=add_cb)],
                [InlineKeyboardButton(text="رجوع", callback_data=back_cb)],
            ]),
        )
        await call.answer()
        return

    lines = [f"<b>📋 قنوات الاشتراك الإجباري ({len(rows)})</b>\n"]
    for i, r in enumerate(rows, 1):
        title = safe_html(r["title"] or "بدون اسم")
        channel = str(r["channel"] or "")
        if channel.startswith("@"):
            link = f'<a href="https://t.me/{channel[1:]}">{title}</a>'
        elif r["chat_id"]:
            link = (
                f'<a href="https://t.me/c/'
                f'{str(r["chat_id"]).replace("-100", "", 1)}">{title}</a>'
            )
        else:
            link = title
        lines.append(
            f"<b>#{i}</b> — {link}\n"
            f"    🆔 <code>{safe_html(channel)}</code>\n"
        )

    kb_rows = []
    for r in rows:
        title_short = (r["title"] or "قناة")[:28]
        channel = str(r["channel"] or "")
        if channel.startswith("@"):
            url = f"https://t.me/{channel[1:]}"
        elif r["chat_id"]:
            url = f"https://t.me/c/{str(r['chat_id']).replace('-100', '', 1)}"
        else:
            url = None
        if url:
            kb_rows.append([InlineKeyboardButton(
                text=f"📢 {title_short}", url=url,
            )])

    kb_rows.append([InlineKeyboardButton(
        text="➕ إضافة قناة أخرى", callback_data=add_cb,
    )])
    kb_rows.append([InlineKeyboardButton(
        text="🗑 حذف قناة", callback_data=del_cb,
    )])
    kb_rows.append([InlineKeyboardButton(
        text="🔄 تحديث", callback_data="account_force_list",
    )])
    kb_rows.append([InlineKeyboardButton(
        text="رجوع", callback_data=back_cb,
    )])

    await edit_or_replace(
        call, "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await call.answer(f"عدد القنوات: {len(rows)}")


@router.callback_query(F.data == "account_force_del_list")
async def account_force_del_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT id, title FROM force_channels "
        "WHERE owner_id=$1 AND scope='account' ORDER BY id",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(
            call, "لا توجد قنوات للحذف.", back_kb("account_force")
        )
        return
    kb = [
        [InlineKeyboardButton(
            text=f"#{r['id']} - {r['title'][:30]}",
            callback_data=f"afdel_{r['id']}",
        )] for r in rows
    ]
    kb.append([InlineKeyboardButton(text="رجوع", callback_data="account_force")])
    await edit_or_replace(
        call, "<b>اختر القناة للحذف:</b>",
        InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await call.answer()


@router.callback_query(F.data.startswith("afdel_"))
async def account_force_del_confirm(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    fid = int(call.data.split("_")[-1])
    row = await db.fetchrow(
        "SELECT title FROM force_channels "
        "WHERE id=$1 AND owner_id=$2 AND scope='account'",
        fid, call.from_user.id,
    )
    if not row:
        await call.answer("القناة غير موجودة.", show_alert=True)
        return
    await edit_or_replace(
        call,
        f"<b>تأكيد حذف القناة</b>\n\n{safe_html(row['title'])}",
        confirm_kb("afdel", str(fid)),
    )
    await call.answer()


# ========== تخصيص أشكال الرسائل ==========
@router.callback_query(F.data == "custom_messages")
async def custom_messages_menu(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    from Database import get_account_message
    keys = [
        ("welcome", "رسالة الترحيب"),
        ("link_help", "رسالة الربط"),
        ("account_force_sub", "رسالة الاشتراك الإجباري"),
    ]
    lines = ["<b>✏️ تخصيص أشكال الرسائل</b>\n"]
    for key, label in keys:
        current = await get_account_message(owner, key) or "(افتراضي)"
        preview = safe_html(current[:80])
        lines.append(f"<b>{label}:</b>\n<code>{preview}</code>\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="تعديل ترحيب البوت", callback_data="editmsg_welcome",
        )],
        [InlineKeyboardButton(
            text="تعديل رسالة الربط", callback_data="editmsg_link_help",
        )],
        [InlineKeyboardButton(
            text="تعديل رسالة الاشتراك",
            callback_data="editmsg_account_force_sub",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])
    await edit_or_replace(call, "\n".join(lines), kb)
    await call.answer()


@router.callback_query(F.data.startswith("editmsg_"))
async def edit_bot_message_start(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user:
        return
    key = call.data[len("editmsg_"):]
    await state.update_data(bot_message_key=key, edit_rid=None)
    await state.set_state(ReplySG.edit_response)
    from Database import get_account_message
    owner = call.from_user.id
    current = await get_account_message(owner, key) or "(لم يتم التعديل بعد)"
    await edit_or_replace(
        call,
        f"<b>تعديل الرسالة</b>\n\n"
        f"الحالية:\n<code>{safe_html(current[:300])}</code>\n\n"
        f"أرسل النص الجديد.",
        cancel_kb(),
    )
    await call.answer()


# ========== الإذاعة للعملاء ==========
def broadcast_kb(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} قسم الإذاعة (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
            callback_data="section_toggle_broadcast",
        )],
        [InlineKeyboardButton(text="إرسال رسالة للعملاء", callback_data="broadcast_send")],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "broadcast")
async def broadcast_menu(
    call: CallbackQuery, state: FSMContext | None = None
) -> None:
    if not call.from_user:
        return
    if state is not None:
        await state.clear()
    count = await db.fetchval(
        "SELECT COUNT(*) FROM customers WHERE connection_id IN "
        "(SELECT connection_id FROM business WHERE user_id=$1)",
        call.from_user.id,
    )
    enabled = await db.fetchval(
        "SELECT broadcast_enabled FROM settings WHERE owner_id=$1",
        call.from_user.id,
    )
    await edit_or_replace(
        call,
        f"<b>📢 الإذاعة للعملاء</b>\n\n"
        f"عدد العملاء: <b>{count}</b>\n"
        f"الحالة: <b>{'مفعّل ✅' if enabled else 'معطل ❌'}</b>",
        broadcast_kb(bool(enabled)),
    )
    await call.answer()


@router.callback_query(F.data == "section_toggle_broadcast")
async def section_toggle_broadcast(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT broadcast_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET broadcast_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await broadcast_menu(call, None)  # type: ignore
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"الإذاعة الآن: {state_text}")


@router.callback_query(F.data == "broadcast_send")
async def broadcast_send(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.set_state(BroadcastSG.add_text)
    await edit_or_replace(
        call,
        "<b>إرسال رسالة جماعية للعملاء</b>\n\nأرسل النص أو الصورة أو الفيديو.",
        cancel_kb(),
    )
    await call.answer()


@router.message(BroadcastSG.add_text)
async def broadcast_do(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    text = (message.html_text or message.caption or "").strip()
    if not text and not any((message.photo, message.video)):
        await message.answer("أرسل نصًا أو ميديا.")
        return
    rows = await db.fetch(
        "SELECT DISTINCT connection_id, chat_id FROM customers "
        "WHERE connection_id IN "
        "(SELECT connection_id FROM business WHERE user_id=$1)",
        message.from_user.id,
    )
    if not rows:
        await state.clear()
        await message.answer(
            "⚠️ لا يوجد عملاء لإرسال الرسالة إليهم.",
            reply_markup=back_kb("broadcast"),
            parse_mode="HTML",
        )
        return
    sem = asyncio.Semaphore(20)

    async def send_one(conn_id: str, chat_id: int) -> bool:
        async with sem:
            try:
                if message.photo:
                    await message.bot.send_photo(
                        chat_id, message.photo[-1].file_id,
                        caption=text or None,
                        business_connection_id=conn_id,
                        parse_mode="HTML",
                    )
                elif message.video:
                    await message.bot.send_video(
                        chat_id, message.video.file_id,
                        caption=text or None,
                        business_connection_id=conn_id,
                        parse_mode="HTML",
                    )
                else:
                    await message.bot.send_message(
                        chat_id, text,
                        business_connection_id=conn_id,
                        parse_mode="HTML",
                    )
                return True
            except Exception:
                return False

    results = await asyncio.gather(
        *(send_one(r["connection_id"], r["chat_id"]) for r in rows)
    )
    sent = sum(results)
    await state.clear()
    await audit(message.from_user.id, "broadcast_customer_bulk",
                sent=sent, total=len(rows))
    await message.answer(
        fancy_success(f"تم الإرسال إلى {sent} من {len(rows)} عميل."),
        reply_markup=back_kb("broadcast"),
        parse_mode="HTML",
    )


# ========== أدوات: تحويل / حاسبة / عملات ==========
def single_tool_kb(
    label: str, enabled: bool, toggle_cb: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} {label} (حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
            callback_data=toggle_cb,
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "transfer_tool")
async def transfer_tool(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    enabled = bool(await db.fetchval(
        "SELECT transfer_enabled FROM settings WHERE owner_id=$1",
        call.from_user.id,
    ))
    await edit_or_replace(
        call,
        f"<b>🔄 أكواد التحويل</b>\n\n"
        f"<blockquote>الحالة: <b>{'مفعّلة ✅' if enabled else 'معطّلة ❌'}</b>\n\n"
        f"اكتب رقم الهاتف ثم المبلغ: "
        f"<code>01000000000 100</code></blockquote>",
        single_tool_kb("أكواد التحويل", enabled, "transfer_toggle"),
    )
    await call.answer()


@router.callback_query(F.data == "transfer_toggle")
async def transfer_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT transfer_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET transfer_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await transfer_tool(call)
    state_text = "مفعّلة ✅" if new_val else "معطّلة ❌"
    await call.answer(f"أكواد التحويل الآن: {state_text}")


@router.callback_query(F.data == "calculator_tool")
async def calculator_tool(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    enabled = bool(await db.fetchval(
        "SELECT calculator_enabled FROM settings WHERE owner_id=$1",
        call.from_user.id,
    ))
    await edit_or_replace(
        call,
        f"<b>🧮 الآلة الحاسبة</b>\n\n"
        f"<blockquote>الحالة: <b>{'مفعّلة ✅' if enabled else 'معطّلة ❌'}</b>\n\n"
        f"مثال: <code>25*4</code></blockquote>",
        single_tool_kb("الآلة الحاسبة", enabled, "calculator_toggle"),
    )
    await call.answer()


@router.callback_query(F.data == "calculator_toggle")
async def calculator_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT calculator_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET calculator_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await calculator_tool(call)
    state_text = "مفعّلة ✅" if new_val else "معطّلة ❌"
    await call.answer(f"الآلة الحاسبة الآن: {state_text}")


@router.callback_query(F.data == "currency_tool")
async def currency_tool(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    enabled = bool(await db.fetchval(
        "SELECT crypto_enabled FROM settings WHERE owner_id=$1",
        call.from_user.id,
    ))
    from plugins.Temporary import currency_tool_kb
    await edit_or_replace(
        call,
        f"<b>💱 أسعار العملات الحية</b>\n\n"
        f"<blockquote>الحالة: <b>{'مفعّلة ✅' if enabled else 'معطّلة ❌'}</b>\n\n"
        f"اعرف قيمة أي مبلغ بعملة TON أو الدولار أو الجنيه المصري "
        f"أو GRAM.\n\n"
        f"مثال: <code>1ton</code> أو <code>1$</code> "
        f"أو <code>1جنيه</code> أو <code>1gram</code></blockquote>",
        currency_tool_kb(enabled),
    )
    await call.answer()


@router.callback_query(F.data == "currency_toggle")
async def currency_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT crypto_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET crypto_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await currency_tool(call)
    state_text = "مفعّلة ✅" if new_val else "معطّلة ❌"
    await call.answer(f"أسعار العملات الآن: {state_text}")


@router.callback_query(F.data == "currency_show")
async def currency_show(call: CallbackQuery) -> None:
    from plugins.Crypto import crypto_bourse
    await crypto_bourse(call)


# ========== الرصد (سجل الحذف/التعديل) ==========
def tracking_kb(deleted: bool, edited: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if edited else 'تفعيل'} رصد التعديل (حالة: {'مفعّل ✅' if edited else 'معطل ❌'})",
            callback_data="tracking_toggle_edited",
        ), InlineKeyboardButton(
            text=f"{'تعطيل' if deleted else 'تفعيل'} رصد الحذف (حالة: {'مفعّل ✅' if deleted else 'معطل ❌'})",
            callback_data="tracking_toggle_deleted",
        )],
        [InlineKeyboardButton(text="عرض المعدلة", callback_data="tracking_edited"),
         InlineKeyboardButton(text="عرض المحذوفة", callback_data="tracking_deleted")],
        [InlineKeyboardButton(text="مسح السجل", callback_data="tracking_clear")],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "tracking")
async def tracking_menu(
    call: CallbackQuery, state: FSMContext | None = None
) -> None:
    if not call.from_user:
        return
    if state is not None:
        await state.clear()
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT track_deleted_enabled, track_edited_enabled "
        "FROM settings WHERE owner_id=$1",
        owner,
    )
    deleted = bool(row["track_deleted_enabled"]) if row else True
    edited = bool(row["track_edited_enabled"]) if row else True
    await edit_or_replace(
        call,
        "<b>📝 رصد الحذف والتعديل</b>\n\n"
        "<blockquote>يراقب البوت رسائل العملاء المعدّلة "
        "أو المحذوفة.</blockquote>",
        tracking_kb(deleted, edited),
    )
    await call.answer()


@router.callback_query(F.data.in_({
    "tracking_toggle_deleted", "tracking_toggle_edited"
}))
async def tracking_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    field = (
        "track_deleted_enabled"
        if "deleted" in call.data else "track_edited_enabled"
    )
    cur = await db.fetchval(
        f"SELECT {field} FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        f"UPDATE settings SET {field}=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await tracking_menu(call, None)  # type: ignore
    label = "الحذف" if "deleted" in call.data else "التعديل"
    state_text = "مفعّل ✅" if new_val else "معطل ❌"
    await call.answer(f"رصد {label} الآن: {state_text}")


@router.callback_query(F.data.in_({"tracking_deleted", "tracking_edited"}))
async def tracking_show(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    event = "deleted" if call.data == "tracking_deleted" else "edited"
    rows = await db.fetch(
        "SELECT * FROM tracking_logs WHERE owner_id=$1 AND event_type=$2 "
        "ORDER BY id DESC LIMIT 15",
        owner, event,
    )
    title = "المحذوفة" if event == "deleted" else "المعدلة"
    if not rows:
        text = f"<b>سجل الرسائل {title}</b>\n\nلا توجد سجلات."
    else:
        parts = [f"<b>آخر {len(rows)} رسالة {title}</b>"]
        for r in rows:
            body = (r["old_text"] or "")[:200]
            if event == "edited":
                body = (
                    f"قبل: {body}\nبعد: {(r['new_text'] or '')[:200]}"
                )
            parts.append(
                f"\n<b>#{r['id']}</b> — <code>{r['chat_id']}</code>\n"
                f"{safe_html(body)}"
            )
        text = "\n".join(parts)
    await edit_or_replace(call, text, back_kb("tracking"))
    await call.answer()


@router.callback_query(F.data == "tracking_clear")
async def tracking_clear(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    result = await db.execute(
        "DELETE FROM tracking_logs WHERE owner_id=$1", call.from_user.id
    )
    deleted_count = int(result.split()[-1]) if result.startswith("DELETE") else 0
    if deleted_count == 0:
        await call.answer("السجل فارغ بالفعل.", show_alert=True)
        return
    await call.answer(f"تم مسح {deleted_count} سجل.")
    await tracking_menu(call, None)  # type: ignore


# ========== الساعة الحية ==========
def live_clock_kb(enabled: bool, style: int = 0) -> InlineKeyboardMarkup:
    from plugins.Temporary import CLOCK_DIGITS, clock_text
    rows = [[InlineKeyboardButton(
        text=f"{'تعطيل' if enabled else 'تفعيل'} الساعة الحية "
             f"(حالة: {'مفعّل ✅' if enabled else 'معطل ❌'})",
        callback_data="live_clock_toggle",
    )]]
    btns: list[InlineKeyboardButton] = []
    for i in range(len(CLOCK_DIGITS)):
        is_selected = (i == style)
        prefix = "✅ " if is_selected else ""
        btns.append(InlineKeyboardButton(
            text=f"{prefix}{clock_text(i)}",
            callback_data=f"clock_style_{i}",
        ))
    for i in range(0, len(btns), 2):
        rows.append(btns[i:i + 2])
    rows.append([InlineKeyboardButton(text="رجوع", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _set_business_name(
    bot: Bot, connection_id: str, first_name: str, last_name: str
) -> bool:
    try:
        await bot.set_business_account_name(
            business_connection_id=connection_id,
            first_name=first_name,
            last_name=last_name,
        )
        return True
    except Exception:
        log.exception("set_business_name.failed")
        return False


async def _refresh_business(bot: Bot, connection_id: str):
    current = await bot.get_business_connection(
        business_connection_id=connection_id
    )
    await db.execute(
        "UPDATE business SET enabled=$1, can_edit_name=$2, "
        "can_edit_bio=$3 WHERE connection_id=$4",
        bool(current.is_enabled),
        bool(current.rights and current.rights.can_edit_name),
        bool(current.rights and getattr(current.rights, "can_edit_bio", False)),
        connection_id,
    )
    return current


@router.callback_query(F.data == "live_clock")
async def live_clock_menu(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT clock_enabled, clock_style FROM business "
        "WHERE user_id=$1 AND enabled=TRUE LIMIT 1",
        owner,
    )
    enabled = bool(row and row["clock_enabled"])
    style = int(row["clock_style"]) if row else 0
    await edit_or_replace(
        call,
        "<b>⏰ الساعة الحية</b>\n\n"
        f"الحالة: <b>{'مفعّل ✅' if enabled else 'معطل ❌'}</b>\n\n"
        "<blockquote>يضع البوت الساعة الحالية في خانة الاسم "
        "الأخير لحسابك ويحدّثها تلقائيًا كل دقيقة.\n\n"
        "اكتب اسمك في خانة الاسم الأول، وعند التعطيل "
        "يعود الاسم الأخير الأصلي.</blockquote>",
        live_clock_kb(enabled, style),
    )
    await call.answer()


@router.callback_query(F.data.startswith("clock_style_"))
async def clock_style_select(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    try:
        style = int(call.data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        await call.answer("اختيار غير صالح.", show_alert=True)
        return
    from plugins.Temporary import CLOCK_DIGITS, clock_text
    if not 0 <= style < len(CLOCK_DIGITS):
        await call.answer("اختيار غير صالح.", show_alert=True)
        return

    owner = call.from_user.id
    await db.execute(
        "UPDATE business SET clock_style=$1 "
        "WHERE user_id=$2 AND enabled=TRUE",
        style, owner,
    )
    row = await db.fetchrow(
        "SELECT connection_id, clock_enabled FROM business "
        "WHERE user_id=$1 AND enabled=TRUE LIMIT 1",
        owner,
    )
    if row and row["clock_enabled"]:
        try:
            current = await _refresh_business(call.bot, row["connection_id"])
            if (current.is_enabled and current.rights
                    and current.rights.can_edit_name):
                await _set_business_name(
                    call.bot, row["connection_id"],
                    clean_clock_suffix(current.user.first_name or "") or " ",
                    clock_text(style),
                )
        except Exception:
            log.exception("clock_style.apply.failed")
    try:
        await call.message.edit_reply_markup(
            reply_markup=live_clock_kb(
                bool(row and row["clock_enabled"]), style
            )
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("clock_style.edit_reply_markup.failed", error=str(exc))
    try:
        sample = clock_text(style)
        await call.answer(f"تم اختيار الشكل: {sample}")
    except Exception:
        await call.answer("تم اختيار الشكل.")


@router.callback_query(F.data == "live_clock_toggle")
async def live_clock_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    row = await db.fetchrow(
        "SELECT connection_id, original_name, original_last_name, "
        "clock_enabled, clock_style FROM business "
        "WHERE user_id=$1 ORDER BY enabled DESC LIMIT 1",
        owner,
    )
    if not row:
        await call.answer(
            "لا يوجد اتصال محفوظ. اربط حساب Business أولًا.",
            show_alert=True,
        )
        return
    try:
        current = await _refresh_business(call.bot, row["connection_id"])
    except Exception:
        log.exception("refresh_before_toggle.failed")
        await call.answer("تعذّر التحقق من الاتصال.", show_alert=True)
        return
    if not current.is_enabled:
        await call.answer("اتصال Business غير مفعّل حاليًا.", show_alert=True)
        return
    if not (current.rights and current.rights.can_edit_name):
        await call.answer(
            "تيليجرام لا يمنح البوت صلاحية تعديل اسم الحساب.",
            show_alert=True,
        )
        return
    new_enabled = not bool(row["clock_enabled"])
    if new_enabled:
        first_name = (
            clean_clock_suffix(current.user.first_name or "") or " "
        )
        original_last_name = current.user.last_name or ""
        last_name = clock_text(row["clock_style"] or 0)
    else:
        first_name = (
            clean_clock_suffix(
                row["original_name"] or current.user.first_name or ""
            ) or " "
        )
        original_last_name = row["original_last_name"]
        if original_last_name is None:
            original_last_name = current.user.last_name or ""
        last_name = original_last_name
    if new_enabled:
        await db.execute(
            "UPDATE business SET clock_enabled=TRUE, "
            "original_name=$1, original_last_name=$2 "
            "WHERE connection_id=$3",
            first_name, original_last_name, row["connection_id"],
        )
    else:
        await db.execute(
            "UPDATE business SET clock_enabled=FALSE "
            "WHERE connection_id=$1",
            row["connection_id"],
        )
    ok = await _set_business_name(
        call.bot, row["connection_id"], first_name, last_name
    )
    if not ok:
        await db.execute(
            "UPDATE business SET clock_enabled=$1 WHERE connection_id=$2",
            bool(row["clock_enabled"]), row["connection_id"],
        )
        await call.answer("تعذّر تعديل الاسم من Telegram.", show_alert=True)
    else:
        state_text = "مفعّل ✅" if new_enabled else "معطل ❌"
        await call.answer(f"الساعة الآن: {state_text}")
    await live_clock_menu(call)


# ========== البايو الحي ==========
def _bio_verses_for(religion: str) -> list:
    from plugins.Temporary import bio_verses_for
    return bio_verses_for(religion)


async def _get_bio_data(owner_id: int) -> dict:
    await ensure_settings(owner_id)
    row = await db.fetchrow(
        "SELECT live_bio_enabled, live_bio_religion, "
        "live_bio_custom_verses, live_bio_custom_texts, live_bio_mode, "
        "live_bio_custom_interval_minutes, "
        "live_bio_religious_interval_minutes, "
        "live_bio_last_updated, live_bio_index "
        "FROM settings WHERE owner_id=$1",
        owner_id,
    )
    if not row:
        return {
            "enabled": False, "religion": "", "religious_verses": [],
            "custom_verses": [], "custom_by_religion": {},
            "custom_texts": [], "mode": "custom",
            "custom_interval_minutes": 360,
            "religious_interval_minutes": 360,
            "last_updated": 0, "index": 0,
        }
    religion = (row["live_bio_religion"] or "").strip()
    if religion not in LIVE_BIO_NAMES:
        religion = ""
    custom_map = row["live_bio_custom_verses"] or {}
    if not isinstance(custom_map, dict):
        custom_map = {}
    custom_map = {
        k: [
            str(v).strip() for v in custom_map.get(k, [])
            if str(v).strip() and len(str(v).strip()) <= 140
        ]
        for k in LIVE_BIO_NAMES if isinstance(custom_map.get(k, []), list)
    }
    custom_texts = row["live_bio_custom_texts"] or []
    if not isinstance(custom_texts, list):
        custom_texts = []
    custom_texts = [
        str(v).strip() for v in custom_texts
        if str(v).strip() and len(str(v).strip()) <= 140
    ]
    mode = row["live_bio_mode"] or "custom"
    if mode not in ("custom", "religious"):
        mode = "custom"
    return {
        "enabled": bool(row["live_bio_enabled"]),
        "religion": religion,
        "religious_verses": _bio_verses_for(religion),
        "custom_verses": custom_map.get(religion, []),
        "custom_by_religion": custom_map,
        "custom_texts": custom_texts,
        "mode": mode,
        "custom_interval_minutes": int(
            row["live_bio_custom_interval_minutes"] or 360
        ),
        "religious_interval_minutes": int(
            row["live_bio_religious_interval_minutes"] or 360
        ),
        "last_updated": int(row["live_bio_last_updated"] or 0),
        "index": int(row["live_bio_index"] or 0),
    }


async def _set_bio_data(owner_id: int, **fields) -> None:
    await ensure_settings(owner_id)
    assignments = []
    values = []

    def add(col: str, val) -> None:
        values.append(val)
        assignments.append(f"{col}=${len(values)}")

    if fields.get("enabled") is not None:
        add("live_bio_enabled", bool(fields["enabled"]))
    if fields.get("religion") is not None:
        add("live_bio_religion", fields["religion"])
    if fields.get("custom_by_religion") is not None:
        add(
            "live_bio_custom_verses",
            json.dumps(fields["custom_by_religion"], ensure_ascii=False),
        )
    if fields.get("custom_texts") is not None:
        add(
            "live_bio_custom_texts",
            json.dumps(fields["custom_texts"], ensure_ascii=False),
        )
    if fields.get("mode") is not None:
        add("live_bio_mode", fields["mode"])
    if fields.get("interval_minutes") is not None:
        col = (
            "live_bio_religious_interval_minutes"
            if fields.get("interval_mode") == "religious"
            else "live_bio_custom_interval_minutes"
        )
        add(col, int(fields["interval_minutes"]))
    if fields.get("last_updated") is not None:
        add("live_bio_last_updated", int(fields["last_updated"]))
    if fields.get("index") is not None:
        add("live_bio_index", int(fields["index"]))
    if not assignments:
        return
    values.append(owner_id)
    query = (
        f"UPDATE settings SET {', '.join(assignments)} "
        f"WHERE owner_id=${len(values)}"
    )
    await db.execute(query, *values)


def live_bio_kb(data: dict) -> InlineKeyboardMarkup:
    enabled = data["enabled"]
    mode = data["mode"]
    if enabled and mode == "custom":
        toggle_label = "تعطيل البايو (حالة: مفعّل ✅)"
    elif enabled:
        toggle_label = "استخدام البايو العادي"
    elif mode == "custom":
        toggle_label = "تفعيل البايو (حالة: معطل ❌)"
    else:
        toggle_label = "تفعيل البايو العادي"
    interval_label = LIVE_BIO_INTERVAL_CHOICES.get(
        data["custom_interval_minutes"],
        f"{data['custom_interval_minutes']} دقيقة",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data="live_bio_toggle")],
        [InlineKeyboardButton(text="إضافة بايو", callback_data="live_bio_add"),
         InlineKeyboardButton(text="حذف بايو", callback_data="live_bio_delete")],
        [InlineKeyboardButton(text="عرض الكل", callback_data="live_bio_list")],
        [InlineKeyboardButton(
            text=f"ضبط مدة التغيير ({interval_label})",
            callback_data="live_bio_duration_custom",
        )],
        [InlineKeyboardButton(text="بايو ديني", callback_data="live_bio_religious")],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


def religious_bio_kb(data: dict) -> InlineKeyboardMarkup:
    religion = data["religion"]
    if religion not in LIVE_BIO_NAMES:
        return religious_choice_kb("live_bio")
    enabled_here = data["enabled"] and data["mode"] == "religious"
    if enabled_here:
        toggle_label = "تعطيل البايو الديني (حالة: مفعّل ✅)"
    elif data["enabled"]:
        toggle_label = "استخدام البايو الديني"
    else:
        toggle_label = "تفعيل البايو الديني (حالة: معطل ❌)"
    interval_label = LIVE_BIO_INTERVAL_CHOICES.get(
        data["religious_interval_minutes"],
        f"{data['religious_interval_minutes']} دقيقة",
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=toggle_label, callback_data="live_bio_religious_toggle",
        )],
        [InlineKeyboardButton(text="تغيير الديانة", callback_data="live_bio_change")],
        [InlineKeyboardButton(
            text=f"ضبط مدة التغيير ({interval_label})",
            callback_data="live_bio_duration_religious",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="live_bio")],
    ])


def religious_choice_kb(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="مسلم", callback_data="live_bio_religion_islam"),
         InlineKeyboardButton(text="مسيحي", callback_data="live_bio_religion_christian")],
        [InlineKeyboardButton(text="رجوع", callback_data=back_cb)],
    ])


async def _render_live_bio(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    data = await _get_bio_data(call.from_user.id)
    mode_name = (
        "البايو العادي" if data["mode"] == "custom"
        else f"البايو الديني — "
        f"{LIVE_BIO_NAMES.get(data['religion'], 'لم تُحدد')}"
    )
    text = (
        "<b>✍️ البايو الحي</b>\n\n"
        f"الحالة: <b>{'مفعّل ✅' if data['enabled'] else 'معطل ❌'}</b>\n"
        f"النوع: <b>{safe_html(mode_name)}</b>\n"
        f"البايوهات العادية: <b>{len(data['custom_texts'])}</b>\n"
        f"مدة التغيير: "
        f"<b>{LIVE_BIO_INTERVAL_CHOICES.get(data['custom_interval_minutes'], str(data['custom_interval_minutes']) + ' دقيقة')}</b>"
    )
    await edit_or_replace(call, text, live_bio_kb(data))


async def _render_religious_bio(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    data = await _get_bio_data(call.from_user.id)
    if data["religion"] in LIVE_BIO_NAMES:
        enabled_here = data["enabled"] and data["mode"] == "religious"
        text = (
            f"<b>البايو الديني — {LIVE_BIO_NAMES[data['religion']]}</b>\n\n"
            f"الحالة: <b>{'مفعّل ✅' if enabled_here else 'غير مفعّل ❌'}</b>\n"
            f"النصوص: <b>{len(data['religious_verses'])}</b>\n"
            f"مدة التغيير: "
            f"<b>{LIVE_BIO_INTERVAL_CHOICES.get(data['religious_interval_minutes'], str(data['religious_interval_minutes']) + ' دقيقة')}</b>"
        )
    else:
        text = "<b>البايو الديني</b>\n\nاختار: مسلم أم مسيحي؟"
    await edit_or_replace(call, text, religious_bio_kb(data))


async def _editable_bio_conn(bot: Bot, owner_id: int):
    rows = await db.fetch(
        "SELECT connection_id FROM business "
        "WHERE user_id=$1 AND enabled=TRUE "
        "ORDER BY can_edit_bio DESC",
        owner_id,
    )
    for row in rows:
        try:
            current = await _refresh_business(bot, row["connection_id"])
            if (current.is_enabled and current.rights
                    and getattr(current.rights, "can_edit_bio", False)):
                return row["connection_id"]
        except Exception:
            continue
    return None


def _bio_limit(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "").strip())
    text = " ".join(ch for ch in text.split())
    out = []
    units = 0
    for ch in text:
        size = len(ch.encode("utf-16-le")) // 2
        if units + size > 140:
            break
        out.append(ch)
        units += size
    return "".join(out)


async def _set_business_bio(
    bot: Bot, connection_id: str, bio: str
) -> bool:
    text = _bio_limit(bio)
    if not text:
        try:
            return bool(await bot.set_business_account_bio(
                business_connection_id=connection_id, bio=""
            ))
        except Exception:
            return False
    try:
        return bool(await bot.set_business_account_bio(
            business_connection_id=connection_id, bio=text
        ))
    except Exception:
        plain = _bio_limit(
            "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
        )
        if plain and plain != text:
            try:
                return bool(await bot.set_business_account_bio(
                    business_connection_id=connection_id, bio=plain
                ))
            except Exception:
                pass
        log.exception("set_business_bio.failed")
        return False


async def _bio_items(data: dict, mode: str = None) -> list:
    mode = mode or data["mode"]
    if mode == "religious":
        if data["religion"] not in LIVE_BIO_NAMES:
            return []
        return list(data["religious_verses"]) + list(
            data.get("custom_verses") or []
        )
    return list(data["custom_texts"])


async def _apply_bio(
    bot: Bot, owner_id: int, advance: bool = False,
    connection_id: str = None,
) -> bool:
    data = await _get_bio_data(owner_id)
    items = await _bio_items(data)
    if not data["enabled"] or not items:
        return False
    if connection_id is None:
        connection_id = await _editable_bio_conn(bot, owner_id)
    if not connection_id:
        return False
    idx = data["index"] % len(items)
    if await _set_business_bio(bot, connection_id, items[idx]):
        await _set_bio_data(
            owner_id,
            index=(idx + 1) % len(items) if advance else idx,
            last_updated=int(datetime.now(UTC).timestamp()),
        )
        return True
    return False


@router.callback_query(F.data == "live_bio")
async def live_bio_menu(call: CallbackQuery) -> None:
    await _render_live_bio(call)
    await call.answer()


@router.callback_query(F.data == "live_bio_toggle")
async def live_bio_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    data = await _get_bio_data(owner)
    if data["enabled"] and data["mode"] == "custom":
        await _set_bio_data(owner, enabled=False)
        cid = await _editable_bio_conn(call.bot, owner)
        if cid:
            await _set_business_bio(call.bot, cid, "")
        await _render_live_bio(call)
        await call.answer("تم تعطيل البايو ❌")
        return
    items = await _bio_items(data, "custom")
    if not items:
        await call.answer("أضف بايو واحدًا على الأقل أولًا.", show_alert=True)
        return
    cid = await _editable_bio_conn(call.bot, owner)
    if not cid:
        await call.answer(
            "لا يوجد اتصال بصلاحية can_edit_bio.", show_alert=True
        )
        return
    await _set_bio_data(
        owner, mode="custom", enabled=True, index=0, last_updated=0
    )
    ok = await _apply_bio(call.bot, owner, advance=True, connection_id=cid)
    if not ok:
        await call.answer("تعذّر تحديث البايو.", show_alert=True)
    else:
        await call.answer("تم تفعيل البايو ✅")
    await _render_live_bio(call)


@router.callback_query(F.data == "live_bio_religious")
async def live_bio_religious(call: CallbackQuery) -> None:
    await _render_religious_bio(call)
    await call.answer()


@router.callback_query(F.data == "live_bio_change")
async def live_bio_change(call: CallbackQuery) -> None:
    await edit_or_replace(
        call, "اختار الديانة:", religious_choice_kb("live_bio_religious"),
    )
    await call.answer()


@router.callback_query(F.data == "live_bio_religious_toggle")
async def live_bio_religious_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    data = await _get_bio_data(owner)
    if data["enabled"] and data["mode"] == "religious":
        await _set_bio_data(owner, enabled=False)
        cid = await _editable_bio_conn(call.bot, owner)
        if cid:
            await _set_business_bio(call.bot, cid, "")
        await _render_religious_bio(call)
        await call.answer("تم تعطيل البايو الديني ❌")
        return
    if data["religion"] not in LIVE_BIO_NAMES:
        await _render_religious_bio(call)
        await call.answer("اختار ديانة أولًا.", show_alert=True)
        return
    cid = await _editable_bio_conn(call.bot, owner)
    if not cid:
        await call.answer(
            "لا يوجد اتصال بصلاحية can_edit_bio.", show_alert=True
        )
        return
    await _set_bio_data(
        owner, mode="religious", enabled=True, index=0, last_updated=0
    )
    ok = await _apply_bio(call.bot, owner, advance=True, connection_id=cid)
    if not ok:
        await call.answer("تعذّر تحديث البايو.", show_alert=True)
    else:
        await call.answer("تم تفعيل البايو الديني ✅")
    await _render_religious_bio(call)


@router.callback_query(F.data.startswith("live_bio_religion_"))
async def live_bio_choose_religion(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    religion = call.data.removeprefix("live_bio_religion_")
    if religion not in LIVE_BIO_NAMES:
        await call.answer("اختيار غير صالح.", show_alert=True)
        return
    owner = call.from_user.id
    data = await _get_bio_data(owner)
    if data["religion"] == religion:
        await call.answer(f"الديانة محددة مسبقاً: {LIVE_BIO_NAMES[religion]}")
        return
    await _set_bio_data(owner, religion=religion, index=0, last_updated=0)
    if data["enabled"]:
        await _apply_bio(call.bot, owner, advance=True)
    await _render_religious_bio(call)
    await call.answer(f"تم اختيار {LIVE_BIO_NAMES[religion]}.")


@router.callback_query(F.data == "live_bio_add")
async def live_bio_add(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.update_data(bio_kind="custom")
    await state.set_state(LiveBioSG.add_verse)
    await edit_or_replace(
        call,
        "أرسل نص البايو العادي (بحد أقصى 140 حرفًا).",
        cancel_kb(),
    )
    await call.answer()


@router.message(LiveBioSG.add_verse)
async def live_bio_add_save(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    value = (message.text or "").strip()
    if not value or len(value) > 140:
        await message.answer("أرسل نصًا صحيحًا لا يتجاوز 140 حرفًا.")
        return
    state_data = await state.get_data()
    kind = state_data.get("bio_kind", "custom")
    if kind == "religious":
        await state.clear()
        await message.answer(
            "النصوص الدينية جاهزة ولا تُضاف يدويًا.",
            reply_markup=back_kb("live_bio_religious"),
        )
        return
    owner = message.from_user.id
    data = await _get_bio_data(owner)
    if value in data["custom_texts"]:
        await state.clear()
        await message.answer(
            "⚠️ البايو موجود بالفعل.",
            reply_markup=back_kb("live_bio"),
        )
        return
    custom_texts = list(data["custom_texts"]) + [value]
    await _set_bio_data(
        owner, custom_texts=custom_texts, mode="custom",
        index=len(custom_texts) - 1, last_updated=0,
    )
    await state.clear()
    if data["enabled"]:
        await _apply_bio(message.bot, owner, advance=False)
    await message.answer(
        fancy_success("تمت إضافة النص للحساب."),
        reply_markup=back_kb("live_bio"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "live_bio_list")
async def live_bio_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    data = await _get_bio_data(call.from_user.id)
    items = data["custom_texts"]
    if not items:
        await edit_or_replace(
            call, "لا توجد نصوص محفوظة.", back_kb("live_bio")
        )
        await call.answer()
        return
    text = "<b>البايوهات المحفوظة</b>\n\n" + "\n\n".join(
        safe_html(item) for item in items
    )
    await edit_or_replace(call, text, back_kb("live_bio"))
    await call.answer()


@router.callback_query(F.data == "live_bio_delete")
async def live_bio_delete(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    data = await _get_bio_data(call.from_user.id)
    items = data["custom_texts"]
    if not items:
        await edit_or_replace(
            call, "لا توجد نصوص لحذفها.", back_kb("live_bio")
        )
        await call.answer()
        return
    rows = [
        [InlineKeyboardButton(
            text=item[:45], callback_data=f"live_bio_del_{i}",
        )]
        for i, item in enumerate(items)
    ]
    rows.append([InlineKeyboardButton(text="رجوع", callback_data="live_bio")])
    await edit_or_replace(
        call, "<b>اختر النص لحذفه:</b>",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("live_bio_del_"))
async def live_bio_delete_confirm(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    try:
        idx = int(call.data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        await call.answer("اختيار غير صالح.", show_alert=True)
        return
    owner = call.from_user.id
    data = await _get_bio_data(owner)
    items = list(data["custom_texts"])
    if not 0 <= idx < len(items):
        await call.answer("النص غير موجود.", show_alert=True)
        return
    items.pop(idx)
    await _set_bio_data(owner, custom_texts=items)
    if items:
        new_idx = data["index"] % len(items)
        await _set_bio_data(owner, index=new_idx, last_updated=0)
        if data["enabled"] and data["mode"] == "custom":
            await _apply_bio(call.bot, owner, advance=False)
    else:
        await _set_bio_data(owner, enabled=False, index=0)
        cid = await _editable_bio_conn(call.bot, owner)
        if cid:
            await _set_business_bio(call.bot, cid, "")
    await _render_live_bio(call)
    await call.answer("تم الحذف.")


@router.callback_query(F.data.in_({
    "live_bio_duration_custom", "live_bio_duration_religious"
}))
async def live_bio_duration(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    mode = "religious" if call.data.endswith("religious") else "custom"
    data = await _get_bio_data(call.from_user.id)
    interval_key = (
        "religious_interval_minutes" if mode == "religious"
        else "custom_interval_minutes"
    )
    back_cb = "live_bio_religious" if mode == "religious" else "live_bio"
    bio_label = "الديني" if mode == "religious" else "العادي"
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if minutes == data[interval_key] else '⬜'} {label}",
            callback_data=f"live_bio_set_{mode}_{minutes}",
        )]
        for minutes, label in LIVE_BIO_INTERVAL_CHOICES.items()
    ]
    rows.append([InlineKeyboardButton(text="رجوع", callback_data=back_cb)])
    await edit_or_replace(
        call,
        f"اختار المدة بين كل تغيير للبايو {bio_label}:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("live_bio_set_"))
async def live_bio_set_duration(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    try:
        parts = call.data.split("_")
        interval_mode = parts[-2]
        minutes = int(parts[-1])
    except (TypeError, ValueError, IndexError):
        await call.answer("اختيار غير صالح.", show_alert=True)
        return
    if interval_mode not in ("custom", "religious"):
        await call.answer("اختيار غير صالح.", show_alert=True)
        return
    if minutes not in LIVE_BIO_INTERVAL_CHOICES:
        await call.answer("اختيار غير صالح.", show_alert=True)
        return
    owner = call.from_user.id
    await _set_bio_data(
        owner,
        interval_minutes=minutes,
        interval_mode=interval_mode,
        last_updated=int(datetime.now(UTC).timestamp()),
    )
    if interval_mode == "religious":
        await _render_religious_bio(call)
    else:
        await _render_live_bio(call)
    await call.answer(f"تم ضبط المدة: {LIVE_BIO_INTERVAL_CHOICES[minutes]}.")


# ========== الإحالة ==========
@router.callback_query(F.data == "my_referral")
async def my_referral(call: CallbackQuery) -> None:
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
        remaining = max(0, settings.referral_target - count)
        text = (
            "<b>🎁 رابط الإحالة الخاص بك</b>\n\n"
            f"<code>{safe_html(link)}</code>\n\n"
            f"عدد إحالاتك: <b>{count}</b>\n"
            f"الهدف: <b>{settings.referral_target}</b>\n"
            f"متبقي: <b>{remaining}</b>\n"
            f"المكافأة: <b>{settings.referral_reward_days} أيام</b>"
        )
        await edit_or_replace(call, text, back_kb("home"))
    except Exception:
        log.exception("my_referral.failed")
        await call.answer("تعذّر تحميل الرابط.", show_alert=True)
    await call.answer()


# ========== تحويل الصوت + يوتيوب ==========
COOKIES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "cookie.txt",
)


async def _tools_owner(message: Message) -> int | None:
    if message.business_connection_id:
        row = await db.fetchrow(
            "SELECT user_id FROM business WHERE connection_id=$1",
            message.business_connection_id,
        )
        return row["user_id"] if row else None
    if message.from_user:
        row = await db.fetchrow(
            "SELECT user_id FROM business "
            "WHERE user_id=$1 AND enabled=TRUE LIMIT 1",
            message.from_user.id,
        )
        if row:
            return message.from_user.id
        return settings.owner_id
    return None


async def _tools_flag(
    owner_id: int, field: str, default: bool = True
) -> bool:
    try:
        val = await db.fetchval(
            f"SELECT {field} FROM settings WHERE owner_id=$1", owner_id
        )
        return default if val is None else bool(val)
    except Exception:
        return default


async def _tools_reply(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    kwargs = {
        "chat_id": message.chat.id,
        "text": text,
        "reply_markup": reply_markup,
        "parse_mode": "HTML",
    }
    if message.business_connection_id:
        kwargs["business_connection_id"] = message.business_connection_id
    try:
        return await message.bot.send_message(**kwargs)
    except Exception:
        log.exception("tools.reply.failed")
        return None


def _sync_transcribe(path: str) -> str:
    import speech_recognition as sr
    from pydub import AudioSegment
    try:
        sound = AudioSegment.from_file(path)
    except Exception as exc:
        return f"❌ تعذّر فتح الملف الصوتي: {exc}"
    sound = sound.set_frame_rate(16000).set_channels(1)
    max_chunk_ms = 50000
    if len(sound) > max_chunk_ms:
        chunks = [
            sound[i:i + max_chunk_ms]
            for i in range(0, len(sound), max_chunk_ms)
        ]
    else:
        chunks = [sound]
    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 300
    recognizer.dynamic_energy_threshold = True
    recognizer.pause_threshold = 0.8
    results = []
    for idx, chunk in enumerate(chunks):
        wav_path = path + f".chunk{idx}.wav"
        chunk.export(wav_path, format="wav")
        try:
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio, language="ar-EG")
                results.append(text)
            except sr.UnknownValueError:
                try:
                    text_en = recognizer.recognize_google(
                        audio, language="en-US"
                    )
                    results.append(f"[EN] {text_en}")
                except Exception:
                    results.append("...")
            except sr.RequestError as exc:
                return f"❌ فشل الاتصال بخدمة التعرف: {exc}"
        finally:
            try:
                os.remove(wav_path)
            except Exception:
                pass
    text = " ".join(t for t in results if t and t != "...")
    if not text.strip():
        return "❌ لم أستطع فهم الكلام. جرّب تسجيلًا أوضح."
    return text.strip()




async def _do_speech(
    message: Message, target: Message, owner_id: int
) -> None:
    file_id = None
    ext = "wav"
    if target.voice:
        file_id = target.voice.file_id
        ext = "ogg"
    elif target.audio:
        file_id = target.audio.file_id
        ext = (target.audio.file_name or "").rsplit(".", 1)[-1] or "mp3"
    elif target.video_note:
        file_id = target.video_note.file_id
        ext = "mp4"
    elif target.video:
        file_id = target.video.file_id
        ext = "mp4"
    if not file_id:
        await _tools_reply(message, fancy_error("لم أجد ملفًا صوتيًا."))
        return
    status = await _tools_reply(message, "🎙 <b>جاري تحميل الصوت...</b>")
    if not status:
        return

    async def _edit(text: str) -> None:
        try:
            await message.bot.edit_message_text(
                chat_id=status.chat.id,
                message_id=status.message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception:
            pass

    ext = re.sub(r"[^A-Za-z0-9]", "", ext.lower())[:10] or "audio"
    fd, tmp_path = tempfile.mkstemp(
        prefix="stt_", suffix=f".{ext}", dir=tempfile.gettempdir()
    )
    os.close(fd)
    try:
        await _edit("🎙 <b>جاري تنزيل الملف...</b>")
        try:
            await message.bot.download(file_id, destination=tmp_path)
        except Exception:
            log.exception("speech.download.failed")
            await _edit(fancy_error("فشل تنزيل الملف الصوتي."))
            return
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            await _edit(fancy_error("الملف الصوتي فارغ."))
            return
        await _edit("🎙 <b>جاري تحليل الصوت...</b>")
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_sync_transcribe, tmp_path), timeout=180,
            )
        except asyncio.TimeoutError:
            result = "❌ انتهت المهلة أثناء تحليل الصوت."
        except Exception as exc:
            log.exception("speech.transcribe.failed")
            result = f"❌ فشل التحويل: {safe_html(str(exc)[:200])}"
        await _edit(
            "<b>📝 النص المُستخرج:</b>\n\n"
            f"<blockquote>{safe_html(result)}</blockquote>"
        )
    finally:
        for p in (tmp_path, tmp_path + ".wav"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


@router.message(F.text.regexp(r"^(?:\.?اكتب|\.?وش يقول)$"))
async def speech_trigger(message: Message, state: FSMContext) -> None:
    if not message.reply_to_message:
        await _tools_reply(
            message,
            fancy_error("قم بالرد على رسالة صوتية أولًا ثم أرسل <code>اكتب</code>."),
        )
        return
    target = message.reply_to_message
    if not (target.voice or target.audio or target.video_note or target.video):
        await _tools_reply(
            message,
            fancy_error(
                "الرسالة المردود عليها ليست صوتًا أو فيديو.\n"
                "المدعوم: رسالة صوتية، ملف صوتي، فيديو نوت، فيديو."
            ),
        )
        return
    owner_id = await _tools_owner(message)
    if not owner_id:
        return
    if not await _tools_flag(owner_id, "speech_enabled", True):
        await _tools_reply(message, fancy_error("ميزة تحويل الصوت معطّلة."))
        return
    await _do_speech(message, target, owner_id)


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _sync_youtube_download(
    query: str, outtmpl: str, tmp_dir: str
) -> tuple[dict, str, str | None]:
    import urllib.request
    from yt_dlp import YoutubeDL
    is_url = query.startswith(("http://", "https://"))
    target = query if is_url else f"ytsearch1:{query}"
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noplaylist": True,
        "no_warnings": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target, download=True)
    except Exception as exc:
        err = str(exc).lower()
        if "sign in to confirm" in err or "bot" in err:
            ydl_opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["web_safari", "android", "ios"],
                }
            }
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=True)
        else:
            raise
    if info.get("entries"):
        info = info["entries"][0]
    video_id = info.get("id") or "audio"
    audio_path = os.path.join(tmp_dir, f"yt_{video_id}.mp3")
    if not os.path.exists(audio_path):
        for ext in ("m4a", "webm", "opus", "wav"):
            candidate = os.path.join(tmp_dir, f"yt_{video_id}.{ext}")
            if os.path.exists(candidate):
                audio_path = candidate
                break
    thumb_path = None
    thumb_url = info.get("thumbnail")
    if thumb_url:
        try:
            thumb_path = os.path.join(tmp_dir, f"yt_{video_id}.jpg")
            urllib.request.urlretrieve(thumb_url, thumb_path)
        except Exception:
            thumb_path = None
    return info, audio_path, thumb_path


async def _do_youtube(
    message: Message, query: str, owner_id: int
) -> None:
    if not query.strip():
        await _tools_reply(message, fancy_error("اكتب اسم المقطع."))
        return
    status = await _tools_reply(
        message,
        f"🔎 <b>جاري البحث عن:</b>\n<code>{safe_html(query)}</code>",
    )
    if not status:
        return

    async def _edit(text: str) -> None:
        try:
            await message.bot.edit_message_text(
                chat_id=status.chat.id,
                message_id=status.message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception:
            pass

    tmp_dir = tempfile.gettempdir()
    outtmpl = os.path.join(tmp_dir, "yt_%(id)s.%(ext)s")
    try:
        info, audio_path, thumb_path = await asyncio.wait_for(
            asyncio.to_thread(
                _sync_youtube_download, query, outtmpl, tmp_dir
            ),
            timeout=600,
        )
    except asyncio.TimeoutError:
        await _edit(fancy_error("انتهت المهلة أثناء التحميل."))
        return
    except Exception as exc:
        log.exception("yt.download.failed")
        err_msg = str(exc)[:300]
        if "sign in to confirm" in str(exc).lower():
            err_msg = (
                "يوتيوب يطلب تسجيل دخول.\n"
                "تأكد من وجود ملف <code>cookies.txt</code> صحيح."
            )
        await _edit(fancy_error(f"فشل التحميل: {safe_html(err_msg)}"))
        return
    if not audio_path or not os.path.exists(audio_path):
        await _edit(fancy_error("لم أستطع تنزيل الملف."))
        return
    await _edit("⬆️ <b>جاري الرفع إلى تيليجرام...</b>")
    caption = (
        f"🎵 <b>{safe_html(info.get('title') or '')}</b>\n"
        f"👤 {safe_html(info.get('uploader') or '')}\n"
        f"⏱ {_fmt_duration(info.get('duration') or 0)}"
    )
    if info.get("webpage_url"):
        caption += f'\n🔗 <a href="{info["webpage_url"]}">المصدر</a>'
    send_kwargs: dict = {
        "chat_id": message.chat.id,
        "audio": FSInputFile(audio_path),
        "duration": int(info.get("duration") or 0),
        "title": str(info.get("title") or "")[:64],
        "performer": str(info.get("uploader") or "")[:64],
        "caption": caption,
        "parse_mode": "HTML",
    }
    if thumb_path and os.path.exists(thumb_path):
        send_kwargs["thumbnail"] = FSInputFile(thumb_path)
    if message.business_connection_id:
        send_kwargs["business_connection_id"] = message.business_connection_id
    if message.reply_to_message:
        send_kwargs["reply_to_message_id"] = (
            message.reply_to_message.message_id
        )
    try:
        await message.bot.send_audio(**send_kwargs)
        try:
            await message.bot.delete_message(
                chat_id=status.chat.id, message_id=status.message_id
            )
        except Exception:
            await _edit(fancy_success("تم الإرسال ✅"))
    except Exception:
        log.exception("yt.send.failed")
        await _edit(fancy_error("فشل إرسال الملف إلى تيليجرام."))
    finally:
        for p in (audio_path, thumb_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


@router.message(F.text.regexp(r"^تحميل(?:\s+.+)?$"))
async def youtube_trigger(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        owner_id = await _tools_owner(message)
        await state.set_state(YouTubeSG.waiting_query)
        await state.update_data(yt_owner=owner_id)
        await _tools_reply(
            message,
            "<b>🎬 تحميل من يوتيوب</b>\n\nأرسل اسم المقطع أو الرابط.",
            cancel_kb(),
        )
        return
    owner_id = await _tools_owner(message)
    if not owner_id:
        return
    if not await _tools_flag(owner_id, "youtube_enabled", True):
        await _tools_reply(message, fancy_error("ميزة التحميل معطّلة."))
        return
    await _do_youtube(message, query, owner_id)


@router.message(YouTubeSG.waiting_query)
async def youtube_query_received(
    message: Message, state: FSMContext
) -> None:
    if not message.text:
        return
    data = await state.get_data()
    owner_id = data.get("yt_owner") or await _tools_owner(message)
    await state.clear()
    if not owner_id:
        return
    if not await _tools_flag(owner_id, "youtube_enabled", True):
        await _tools_reply(message, fancy_error("ميزة التحميل معطّلة."))
        return
    await _do_youtube(message, message.text.strip(), owner_id)


# ========== لوحة الأدوات ==========
def tools_kb(
    speech_enabled: bool, youtube_enabled: bool
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=(
                f"{'تعطيل' if speech_enabled else 'تفعيل'} تحويل الصوت "
                f"({'مفعّل ✅' if speech_enabled else 'معطّل ❌'})"
            ),
            callback_data="tools_toggle_speech",
        )],
        [InlineKeyboardButton(
            text=(
                f"{'تعطيل' if youtube_enabled else 'تفعيل'} تحميل يوتيوب "
                f"({'مفعّل ✅' if youtube_enabled else 'معطّل ❌'})"
            ),
            callback_data="tools_toggle_youtube",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])


@router.callback_query(F.data == "tools_menu")
async def tools_menu(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    await ensure_settings(owner)
    row = await db.fetchrow(
        "SELECT speech_enabled, youtube_enabled FROM settings "
        "WHERE owner_id=$1",
        owner,
    )
    speech_enabled = bool(row["speech_enabled"]) if row else True
    youtube_enabled = bool(row["youtube_enabled"]) if row else True
    cookies_status = "موجود ✅" if os.path.exists(COOKIES_FILE) else "غير موجود ❌"
    await edit_or_replace(
        call,
        "<b>🧰 أدوات إضافية</b>\n\n"
        "<blockquote>"
        "🎙 <b>تحويل الصوت إلى نص:</b> ردّ على رسالة صوتية "
        "ثم اكتب <code>اكتب</code> أو <code>وش يقول</code>.\n\n"
        "🎬 <b>تحميل من يوتيوب:</b> اكتب "
        "<code>تحميل اسم المقطع</code>.\n\n"
        f"🍪 ملف الكوكيز: <b>{cookies_status}</b>"
        "</blockquote>",
        tools_kb(speech_enabled, youtube_enabled),
    )
    await call.answer()


@router.callback_query(F.data.in_({
    "tools_toggle_speech", "tools_toggle_youtube"
}))
async def tools_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    field = (
        "speech_enabled" if call.data.endswith("speech")
        else "youtube_enabled"
    )
    cur = await db.fetchval(
        f"SELECT {field} FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        f"UPDATE settings SET {field}=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await tools_menu(call)
    label = "تحويل الصوت" if field == "speech_enabled" else "تحميل يوتيوب"
    state_text = "مفعّل ✅" if new_val else "معطّل ❌"
    await call.answer(f"{label} الآن: {state_text}")


# ========== أوامر Business: انتحال/مسح/سبام/كول ==========
from plugins.Temporary import (
    _impersonation_cache,
    parse_spam_command,
    parse_delete_command,
    save_original_profile,
    impersonate_user,
    restore_original_profile,
    check_call_permission,
    open_group_call,
    close_group_call,
)


@router.message(F.text.regexp(r"^\.?انتحال$"))
async def impersonate_command(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    row = await db.fetchrow(
        "SELECT connection_id FROM business "
        "WHERE user_id=$1 AND enabled=TRUE LIMIT 1",
        message.from_user.id,
    )
    if not row:
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ قم بالرد على رسالة العضو أولاً.")
        return
    target = message.reply_to_message.from_user
    if target.id == settings.owner_id:
        await message.answer("⚠️ لا يمكن انتحال حساب المالك.")
        return
    try:
        current = await message.bot.get_business_connection(
            business_connection_id=row["connection_id"]
        )
        if not (current.rights and current.rights.can_edit_name):
            await message.answer(
                "⚠️ تحتاج صلاحية «تعديل الاسم» لاستخدام الانتحال."
            )
            return
    except Exception:
        pass
    await save_original_profile(
        message.bot, message.from_user.id, row["connection_id"]
    )
    success, result_msg = await impersonate_user(
        message.bot, message.from_user.id, row["connection_id"],
        target.first_name or "", target.last_name or "",
    )
    await message.answer(result_msg)


@router.message(F.text.regexp(r"^\.?رجوع$"))
async def restore_command(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    success, result_msg = await restore_original_profile(
        message.bot, message.from_user.id
    )
    await message.answer(result_msg)


@router.message(F.text.regexp(r"^\.?مسح\s+\d+$"))
async def delete_messages_command(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user:
        return
    count = parse_delete_command(message.text or "")
    if not count:
        await message.answer("⚠️ الصيغة: مسح <العدد>")
        return
    if not message.reply_to_message:
        await message.answer("⚠️ قم بالرد على رسالة لبدء المسح.")
        return
    base_id = message.reply_to_message.message_id
    ids_to_delete = [base_id - i for i in range(count) if base_id - i > 0]
    ids_to_delete.append(message.message_id)
    try:
        await message.bot.delete_messages(
            chat_id=message.chat.id, message_ids=ids_to_delete,
        )
    except Exception as e:
        await message.answer(f"❌ فشل المسح: {str(e)[:100]}")


@router.message(F.text.regexp(r"^\.?(سبام|تكرار)\s+.+\s+\d+$"))
async def spam_command(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    spam_text, spam_count = parse_spam_command(message.text or "")
    if not spam_text or not spam_count:
        await message.answer("⚠️ الصيغة: تكرار <النص> <العدد>")
        return
    try:
        await message.delete()
    except Exception:
        pass
    for i in range(spam_count):
        try:
            await message.answer(spam_text)
            await asyncio.sleep(0.3)
        except Exception:
            break


@router.message(F.text.regexp(r"^\.?(فتح|قفل)\s+الكول$"))
async def call_control_command(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user:
        return
    row = await db.fetchrow(
        "SELECT connection_id FROM business "
        "WHERE user_id=$1 AND enabled=TRUE LIMIT 1",
        message.from_user.id,
    )
    text = (message.text or "").strip().lstrip(".")
    is_open = text.startswith("فتح")
    if row:
        has_perm, perm_error = await check_call_permission(
            message.bot, row["connection_id"]
        )
        if not has_perm:
            await message.answer(f"⚠️ {perm_error}")
            return
    if is_open:
        success, error_msg = await open_group_call(message.bot, message.chat.id)
    else:
        success, error_msg = await close_group_call(message.bot, message.chat.id)
    if success:
        action = "فتح" if is_open else "قفل"
        await message.answer(f"✅ تم {action} المكالمة.")
    else:
        await message.answer(f"⚠️ {error_msg}")


# ========== قائمة العملاء ==========
@router.callback_query(F.data == "customers_list")
async def customers_list(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    rows = await db.fetch(
        "SELECT DISTINCT c.chat_id, c.first_name, c.username, "
        "c.last_seen FROM customers c "
        "JOIN business b ON b.connection_id = c.connection_id "
        "WHERE b.user_id=$1 AND b.enabled=TRUE "
        "ORDER BY c.last_seen DESC LIMIT 100",
        call.from_user.id,
    )
    if not rows:
        await edit_or_replace(
            call,
            "<b>👥 قائمة العملاء</b>\n\nلا يوجد عملاء بعد.",
            back_kb("home"),
        )
        await call.answer()
        return
    kb_rows = []
    for r in rows:
        name = r["first_name"] or "بدون اسم"
        username = r["username"]
        display = f"@{username}" if username else name
        try:
            ts = r["last_seen"].strftime("%m-%d")
        except Exception:
            ts = ""
        kb_rows.append([InlineKeyboardButton(
            text=f"👤 {display[:28]} • {ts}",
            url=f"tg://user?id={r['chat_id']}",
        )])
    kb_rows.append([InlineKeyboardButton(text="رجوع", callback_data="home")])
    await edit_or_replace(
        call,
        f"<b>👥 قائمة العملاء ({len(rows)})</b>\n\n"
        "<blockquote>آخر 100 عميل نشط. اضغط على الاسم لفتح الملف.</blockquote>",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await call.answer()