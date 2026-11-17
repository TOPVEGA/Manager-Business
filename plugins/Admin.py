# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings
from Database import db, init_schema, audit
from plugins.Temporary import (
    back_kb,
    cancel_kb,
    edit_or_replace,
    fancy_error,
    fancy_success,
    force_add_error_kb,
    safe_html,
    verify_bot_is_admin,
    parse_channel_input,
)

log = structlog.get_logger(__name__)
router = Router(name="admin")
UTC = timezone.utc
MAX_BACKUP_SIZE = 50 * 1024 * 1024


# ========== الحالات ==========
class SubscriptionSG(StatesGroup):
    user_id = State()
    duration = State()


class AdminSG(StatesGroup):
    ban_user_id = State()
    unban_user_id = State()
    edit_bot_message = State()
    restore_backup = State()
    edit_user_id = State()
    edit_user_name = State()
    bot_welcome_image = State()


class BotBroadcastSG(StatesGroup):
    add_text = State()
    schedule_at = State()


class ChannelBroadcastSG(StatesGroup):
    add_text = State()
    schedule_at = State()


class CustomerBroadcastSG(StatesGroup):
    add_text = State()
    schedule_at = State()


class ForceSG(StatesGroup):
    add_channel = State()


class StationSG(StatesGroup):
    user_id = State()
    confirm = State()


class MediaSG(StatesGroup):
    dashboard_video = State()


class MaintenanceSG(StatesGroup):
    new_message = State()


# ========== دوال مساعدة ==========
def _is_owner(user_id: int) -> bool:
    return user_id == settings.owner_id


def admin_panel_kb() -> InlineKeyboardMarkup:
    buttons = [
        ("قسم الاشتراكات", "admin_subscription_section"),
        ("قسم الاشتراك الإجباري", "admin_force_section"),
        ("قسم المستخدمين والحظر", "admin_users_section"),
        ("قسم النسخ الاحتياطية", "admin_backup_section"),
        ("قسم الإذاعة", "admin_broadcast_section"),
        ("قسم إعدادات البوت", "admin_settings_section"),
        ("قسم الإحالات", "admin_referral_section"),
        ("قسم الإشعارات", "admin_notifications_section"),
        ("🧾 سجل التدقيق", "admin_audit_section"),
        ("⏰ إذاعات مجدولة", "admin_scheduled_section"),
        (
            f"وضع الصيانة ({'⚠️' if settings.maintenance_mode else '✅'})",
            "admin_maintenance_section",
        ),
        ("تركي ستيشن", "turkey_station"),
        ("رجوع", "home"),
    ]
    rows = []
    for i in range(0, len(buttons), 2):
        rows.append([
            InlineKeyboardButton(text=t, callback_data=c)
            for t, c in buttons[i:i + 2]
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_subsection_kb(buttons: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(buttons), 2):
        rows.append([
            InlineKeyboardButton(text=t, callback_data=c)
            for t, c in buttons[i:i + 2]
        ])
    rows.append([InlineKeyboardButton(
        text="رجوع للوحة الأدمن", callback_data="admin_panel",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_admin_kb(mode: str) -> InlineKeyboardMarkup:
    toggle_text = "تفعيل الوضع المدفوع" if mode == "free" else "تفعيل الوضع المجاني"
    toggle_cb = "access_paid" if mode == "free" else "access_free"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)],
        [InlineKeyboardButton(text="إضافة اشتراك مدفوع", callback_data="sub_paid_add"),
         InlineKeyboardButton(text="حذف اشتراك مدفوع", callback_data="sub_paid_delete")],
        [InlineKeyboardButton(text="عرض الحسابات المدفوعة", callback_data="sub_paid_list"),
         InlineKeyboardButton(text="إضافة فترة تجريبية", callback_data="sub_trial")],
        [InlineKeyboardButton(text="رجوع للوحة الأدمن", callback_data="admin_panel")],
    ])


def parse_duration(value: str) -> tuple[timedelta, str] | None:
    raw = (value or "").strip().lower().translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    )
    if raw.isdigit():
        days = int(raw)
        return (timedelta(days=days), f"{days} يوم") if days > 0 else None
    import re as _re
    m = _re.fullmatch(r"(\d+)\s*([hwdm])", raw)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        return None
    labels = {
        "h": ("ساعة", timedelta(hours=n)),
        "w": ("أسبوع", timedelta(weeks=n)),
        "d": ("يوم", timedelta(days=n)),
        "m": ("شهر", timedelta(days=30 * n)),
    }
    label, span = labels[unit]
    return span, f"{n} {label}"


def subscription_notice(label: str, expires: datetime, trial: bool = False) -> str:
    title = (
        "تم تفعيل النسخة التجريبية تلقائيًا لك"
        if trial else "تم تفعيل النسخة المدفوعة في البوت لك"
    )
    return (
        f"<blockquote><b>🎉 {title}</b>\n\n"
        f"🕐 <b>المدة : {safe_html(label)}</b>\n\n"
        f"📅 <b>تاريخ الانتهاء : "
        f"{safe_html(expires.strftime('%Y-%m-%d %H:%M'))}</b></blockquote>"
    )


# ========== لوحة الأدمن ==========
@router.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        await call.answer("هذه اللوحة للمالك فقط.", show_alert=True)
        return
    await state.clear()
    await edit_or_replace(
        call,
        "<b>لوحة الأدمن</b>\n\nإعدادات البوت وإدارته فقط:",
        admin_panel_kb(),
    )
    await call.answer()


# ========== قسم الاشتراكات ==========
@router.callback_query(F.data == "admin_subscription_section")
async def admin_subscription_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    mode = await db.fetchval(
        "SELECT bot_access_mode FROM settings WHERE owner_id=$1",
        settings.owner_id,
    ) or "free"
    mode_text = "مدفوع" if mode == "paid" else "مجاني"
    await edit_or_replace(
        call,
        f"<blockquote><b>قسم الاشتراكات</b>\n\n"
        f"وضع البوت الحالي: <b>{mode_text}</b>\n"
        f"اختار العملية المطلوبة.</blockquote>",
        subscription_admin_kb(mode),
    )
    await call.answer()


@router.callback_query(F.data.in_({"access_paid", "access_free"}))
async def set_access_mode(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    mode = "paid" if call.data == "access_paid" else "free"
    await db.execute(
        "UPDATE settings SET bot_access_mode=$1 WHERE owner_id=$2",
        mode, settings.owner_id,
    )
    db.cache_invalidate(f"setting:{settings.owner_id}:")
    await audit(call.from_user.id, "access_mode_set", mode, mode=mode)
    await admin_subscription_section(call)


@router.callback_query(F.data.in_({"sub_paid_add", "sub_paid_delete", "sub_trial"}))
async def subscription_start(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    action = call.data.removeprefix("sub_")
    await state.update_data(subscription_action=action)
    await state.set_state(SubscriptionSG.user_id)
    prompts = {
        "paid_add": "أرسل ID المستخدم لإضافة الاشتراك المدفوع.",
        "paid_delete": "أرسل ID المستخدم لحذف الاشتراك المدفوع.",
        "trial": "أرسل ID المستخدم لإضافة فترة تجريبية.",
    }
    await edit_or_replace(
        call,
        f"<blockquote><b>{prompts.get(action, '')}</b></blockquote>",
        cancel_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "sub_paid_list")
async def subscription_paid_list(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    rows = await db.fetch(
        "SELECT user_id, expires_at FROM bot_subscriptions "
        "WHERE trial=FALSE ORDER BY expires_at DESC LIMIT 100"
    )
    if not rows:
        body = "لا توجد حسابات مدفوعة حالياً."
    else:
        lines = []
        for row in rows:
            try:
                expiry = row["expires_at"].strftime("%Y-%m-%d %H:%M")
            except Exception:
                expiry = str(row["expires_at"])
            lines.append(f"<b>• <code>{row['user_id']}</code> — ينتهي: {expiry}</b>")
        body = "\n".join(lines)
    await edit_or_replace(
        call,
        f"<blockquote><b>الحسابات المدفوعة</b>\n\n{body}</blockquote>",
        back_kb("admin_subscription_section"),
    )
    await call.answer()


@router.message(SubscriptionSG.user_id)
async def subscription_user_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("أرسل ID رقمي صحيح.")
        return
    data = await state.get_data()
    action = data.get("subscription_action")

    if action == "paid_delete":
        await db.execute(
            "DELETE FROM bot_subscriptions WHERE user_id=$1 AND trial=FALSE", uid
        )
        await state.clear()
        await audit(message.from_user.id, "sub_paid_delete", str(uid))
        await message.answer(
            f"<blockquote><b>تم حذف الاشتراك المدفوع للمستخدم "
            f"<code>{uid}</code>.</b></blockquote>",
            reply_markup=back_kb("admin_subscription_section"),
            parse_mode="HTML",
        )
        return

    if action == "trial":
        span, label = timedelta(hours=24), "24 ساعة"
        expires = datetime.now(UTC) + span
        await db.execute(
            """INSERT INTO bot_subscriptions(user_id, expires_at, trial, created_at)
               VALUES($1, $2, TRUE, NOW())
               ON CONFLICT(user_id) DO UPDATE SET
                 expires_at=EXCLUDED.expires_at, trial=TRUE""",
            uid, expires,
        )
        await state.clear()
        await audit(message.from_user.id, "sub_trial", str(uid))
        try:
            await message.bot.send_message(
                uid, subscription_notice(label, expires, True),
                parse_mode="HTML",
            )
        except Exception:
            pass
        await message.answer(
            f"<blockquote><b>تم تفعيل الفترة التجريبية 24 ساعة "
            f"للمستخدم <code>{uid}</code>.</b></blockquote>",
            reply_markup=back_kb("admin_subscription_section"),
            parse_mode="HTML",
        )
        return

    await state.update_data(subscription_user_id=uid)
    await state.set_state(SubscriptionSG.duration)
    await message.answer(
        "<blockquote><b>أرسل المدة، مثال: <code>7</code> أيام "
        "أو <code>1h</code> ساعة أو <code>1w</code> أسبوع "
        "أو <code>1m</code> شهر.</b></blockquote>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(SubscriptionSG.duration)
async def subscription_duration_save(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parsed = parse_duration(message.text or "")
    if not parsed:
        await message.answer("المدة غير صحيحة.")
        return
    span, label = parsed
    data = await state.get_data()
    uid = data.get("subscription_user_id")
    expires = datetime.now(UTC) + span
    await db.execute(
        """INSERT INTO bot_subscriptions(user_id, expires_at, trial, created_at)
           VALUES($1, $2, FALSE, NOW())
           ON CONFLICT(user_id) DO UPDATE SET
             expires_at=EXCLUDED.expires_at, trial=FALSE""",
        uid, expires,
    )
    await state.clear()
    await audit(
        message.from_user.id, "sub_paid_add", str(uid),
        duration=label,
    )
    try:
        await message.bot.send_message(
            uid, subscription_notice(label, expires, False),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.answer(
        f"<blockquote><b>تم تفعيل الاشتراك المدفوع للمستخدم "
        f"<code>{uid}</code>.</b></blockquote>",
        reply_markup=back_kb("admin_subscription_section"),
        parse_mode="HTML",
    )


# ========== قسم المستخدمين والحظر ==========
@router.callback_query(F.data == "admin_users_section")
async def admin_users_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await edit_or_replace(
        call,
        "<b>قسم المستخدمين والحظر</b>\n\nاختر العملية:",
        admin_subsection_kb([
            ("حظر مستخدم", "user_ban_start"),
            ("فك حظر مستخدم", "user_unban_start"),
            ("تغيير اسم حساب فعليًا", "user_name_edit"),
            ("عرض الحسابات المرتبطة", "admin_linked_accounts"),
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "user_ban_start")
async def user_ban_start(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(AdminSG.ban_user_id)
    await edit_or_replace(
        call,
        "<b>حظر مستخدم من البوت</b>\n\nأرسل ID المستخدم.",
        cancel_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "user_unban_start")
async def user_unban_start(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(AdminSG.unban_user_id)
    await edit_or_replace(
        call,
        "<b>إلغاء حظر مستخدم</b>\n\nأرسل ID المستخدم.",
        cancel_kb(),
    )
    await call.answer()


@router.message(AdminSG.ban_user_id)
async def ban_user_by_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("أرسل ID رقمي صحيح.")
        return
    if uid <= 0 or uid == settings.owner_id:
        await message.answer("ID غير صالح.")
        return
    await db.execute(
        """INSERT INTO bot_users(user_id, first_name, username, first_seen, blocked)
           VALUES($1, '', '', NOW(), TRUE)
           ON CONFLICT(user_id) DO UPDATE SET blocked=TRUE""",
        uid,
    )
    await state.clear()
    await audit(message.from_user.id, "user_ban", str(uid))
    await message.answer(
        f"تم حظر المستخدم <code>{uid}</code> من استخدام البوت.",
        reply_markup=back_kb("admin_users_section"),
        parse_mode="HTML",
    )


@router.message(AdminSG.unban_user_id)
async def unban_user_by_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("أرسل ID رقمي صحيح.")
        return
    result = await db.execute(
        "UPDATE bot_users SET blocked=FALSE WHERE user_id=$1", uid
    )
    await state.clear()
    await audit(message.from_user.id, "user_unban", str(uid))
    text = (
        f"تم إلغاء حظر <code>{uid}</code>."
        if result != "UPDATE 0" else "المستخدم غير موجود."
    )
    await message.answer(
        text, reply_markup=back_kb("admin_users_section"), parse_mode="HTML"
    )


# ========== تغيير اسم حساب Telegram ==========
@router.callback_query(F.data == "user_name_edit")
async def user_name_edit(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        await call.answer("هذه الخاصية للمالك فقط.", show_alert=True)
        return
    await state.set_state(AdminSG.edit_user_id)
    await edit_or_replace(
        call,
        "<b>تغيير اسم حساب Telegram فعليًا</b>\n\n"
        "أرسل ID صاحب حساب Telegram Business المرتبط بالبوت.",
        cancel_kb(),
    )
    await call.answer()


@router.message(AdminSG.edit_user_id)
async def user_name_edit_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    try:
        uid = int((message.text or "").strip())
    except (TypeError, ValueError):
        await message.answer("أرسل ID رقمي صحيح.")
        return
    if uid <= 0 or uid == settings.owner_id:
        await message.answer("أرسل ID مستخدم صحيح، ولا يمكن تغيير اسم مالك البوت.")
        return
    await state.update_data(edit_user_id=uid)
    await state.set_state(AdminSG.edit_user_name)
    await message.answer(
        f"تم استلام ID: <code>{uid}</code>\n\n"
        "أرسل الاسم الجديد الذي سيظهر فعليًا في حساب Telegram "
        "(من 1 إلى 64 حرفًا).",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AdminSG.edit_user_name)
async def user_name_edit_save(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("أرسل الاسم الجديد كنص.")
        return
    if len(new_name) > 64:
        await message.answer("الاسم طويل جدًا. الحد الأقصى 64 حرفًا.")
        return
    data = await state.get_data()
    uid = data.get("edit_user_id")
    if not uid:
        await state.clear()
        await message.answer(
            "انتهت العملية، ابدأ من لوحة الأدمن مرة أخرى.",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        return
    row = await db.fetchrow(
        "SELECT connection_id FROM business WHERE user_id=$1 AND enabled=TRUE "
        "ORDER BY connection_id DESC LIMIT 1",
        uid,
    )
    if not row:
        await state.clear()
        await message.answer(
            "لم أجد اتصال Telegram Business مفعّل لهذا الـID.\n"
            "يجب أن يكون الحساب مرتبطًا بالبوت أولًا.",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        return
    try:
        current = await message.bot.get_business_connection(
            business_connection_id=row["connection_id"]
        )
    except Exception:
        log.exception("refresh before name update failed")
        current = None
    if (not current or not current.is_enabled
            or not (current.rights and current.rights.can_edit_name)):
        await state.clear()
        await message.answer(
            "لا يمكن تغيير الاسم فعليًا. الحساب غير متصل أو البوت لا يملك "
            "صلاحية تعديل اسم الحساب.",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        return
    last_name = current.user.last_name or ""
    try:
        await message.bot.set_business_account_name(
            business_connection_id=row["connection_id"],
            first_name=new_name, last_name=last_name,
        )
        changed = True
    except Exception:
        log.exception("set_business_account_name failed")
        changed = False
    await state.clear()
    if not changed:
        await message.answer(
            "فشل تغيير الاسم من Telegram. تأكد من صلاحيات Business وحاول مرة أخرى.",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        return
    await audit(message.from_user.id, "user_name_edit", str(uid),
                new_name=new_name)
    await message.answer(
        f"تم تغيير اسم حساب Telegram فعليًا للـID <code>{uid}</code> إلى: "
        f"<b>{safe_html(new_name)}</b>",
        reply_markup=back_kb("admin_users_section"),
        parse_mode="HTML",
    )


# ========== عرض الحسابات المرتبطة ==========
@router.callback_query(F.data == "admin_linked_accounts")
async def admin_linked_accounts(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    rows = await db.fetch(
        "SELECT connection_id, user_id, original_name AS name, "
        "original_last_name AS last_name, enabled, can_reply, can_edit_name "
        "FROM business ORDER BY user_id, connection_id LIMIT 50"
    )
    if not rows:
        await edit_or_replace(
            call,
            f"<b>الحسابات المرتبطة بالبوت</b>\n\n"
            f"لا توجد حسابات مرتبطة محفوظة.",
            admin_subsection_kb([
                ("تحديث القائمة", "admin_linked_accounts"),
                ("إرسال ملف TXT", "admin_linked_accounts_txt"),
            ]),
        )
        await call.answer()
        return
    kb_rows = []
    for row in rows:
        full_name = " ".join(
            part for part in (row["name"], row["last_name"]) if part
        ).strip() or "بدون اسم"
        status_icon = "✅" if row["enabled"] else "❌"
        kb_rows.append([InlineKeyboardButton(
            text=f"{status_icon} {full_name[:35]} | {row['user_id']}",
            url=f"tg://user?id={row['user_id']}",
        )])
    kb_rows.append([InlineKeyboardButton(
        text="تحديث القائمة", callback_data="admin_linked_accounts",
    )])
    kb_rows.append([InlineKeyboardButton(
        text="إرسال ملف TXT", callback_data="admin_linked_accounts_txt",
    )])
    kb_rows.append([InlineKeyboardButton(
        text="رجوع للوحة الأدمن", callback_data="admin_panel",
    )])
    await edit_or_replace(
        call,
        f"<b>الحسابات المرتبطة بالبوت</b>\n\n"
        f"عدد الاتصالات المحفوظة: <b>{len(rows)}</b>\n\n"
        f"<blockquote>اضغط على الاسم لفتح بروفايل المستخدم.</blockquote>",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await call.answer("تم عرض الحسابات المحفوظة.")


@router.callback_query(F.data == "admin_linked_accounts_txt")
async def admin_linked_accounts_txt(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        await call.answer("هذه الخاصية للمالك فقط.", show_alert=True)
        return
    rows = await db.fetch(
        "SELECT user_id, MAX(original_name) AS first_name, "
        "MAX(original_last_name) AS last_name, MAX(enabled::int) AS enabled, "
        "MAX(can_reply::int) AS can_reply, MAX(can_edit_name::int) AS can_edit_name, "
        "COUNT(*) AS connections "
        "FROM business GROUP BY user_id ORDER BY user_id LIMIT 500"
    )
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "تقرير الحسابات المرتبطة بالبوت",
        f"تاريخ الإنشاء: {now}",
        "=" * 60, "",
    ]
    if not rows:
        lines.append("لا توجد حسابات مرتبطة حاليًا.")
    else:
        lines.extend([f"إجمالي الحسابات: {len(rows)}", ""])
        for index, row in enumerate(rows, 1):
            full_name = " ".join(
                part for part in (row["first_name"], row["last_name"]) if part
            ).strip() or "بدون اسم"
            lines.extend([
                f"الحساب رقم {index}",
                f"الاسم: {full_name}",
                f"المعرّف الرقمي: {row['user_id']}",
                f"الحالة: {'مفعّل' if row['enabled'] else 'غير مفعّل'}",
                f"صلاحية الرد: {'نعم' if row['can_reply'] else 'لا'}",
                f"صلاحية تعديل الاسم: {'نعم' if row['can_edit_name'] else 'لا'}",
                f"عدد الاتصالات: {row['connections']}",
                "-" * 40,
            ])
    document = BufferedInputFile(
        "\n".join(lines).encode("utf-8"), filename="linked_accounts.txt",
    )
    try:
        await call.bot.send_document(
            chat_id=settings.owner_id, document=document,
            caption="تقرير الحسابات المرتبطة بالبوت (بيانات غير حساسة).",
        )
        await call.answer("تم إرسال ملف الحسابات.")
    except Exception:
        log.exception("send linked accounts TXT failed")
        await call.answer("تعذر إرسال الملف، حاول مرة أخرى.", show_alert=True)


# ========== تركي ستيشن ==========
@router.callback_query(F.data == "turkey_station")
async def turkey_station(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(StationSG.user_id)
    await edit_or_replace(
        call,
        "<b>تركي ستيشن</b>\n\n"
        "أرسل ID صاحب الحساب المرتبط.\n"
        "سيتم تنظيف بيانات البوت وتعطيل الاتصالات.",
        cancel_kb(),
    )
    await call.answer()


@router.message(StationSG.user_id)
async def turkey_station_preview(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    try:
        uid = int((message.text or "").strip())
    except Exception:
        await message.answer("أرسل ID رقمي صحيح.")
        return
    row = await db.fetchrow(
        "SELECT COUNT(*) AS c FROM business WHERE user_id=$1 AND enabled=TRUE",
        uid,
    )
    if not row or not row["c"]:
        await message.answer(
            "لا يوجد حساب Business مرتبط بهذا الـ ID.",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        await state.clear()
        return
    await state.update_data(station_user_id=uid)
    await state.set_state(StationSG.confirm)
    await message.answer(
        f"سيتم تنظيف بيانات البوت للحساب <code>{uid}</code> وتعطيل اتصالاته. "
        f"هل تؤكد؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="نعم، تنفيذ", callback_data=f"station_execute_{uid}",
            )],
            [InlineKeyboardButton(text="إلغاء", callback_data="admin_panel")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("station_execute_"))
async def turkey_station_execute(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    uid = int(call.data.split("_")[-1])
    async with db.transaction() as conn:
        rows = await conn.fetch(
            "SELECT connection_id FROM business WHERE user_id=$1", uid
        )
        conn_ids = [r["connection_id"] for r in rows]
        if conn_ids:
            for table in (
                "customers", "tracked_messages", "tracking_logs",
                "muted", "violations",
            ):
                await conn.execute(
                    f"DELETE FROM {table} "
                    "WHERE connection_id = ANY($1::text[])", conn_ids,
                )
            await conn.execute(
                "UPDATE business SET enabled=FALSE, clock_enabled=FALSE "
                "WHERE connection_id = ANY($1::text[])", conn_ids,
            )
    await state.clear()
    await audit(call.from_user.id, "turkey_station", str(uid))
    await edit_or_replace(
        call,
        "تم تنظيف بيانات البوت وتعطيل اتصال الحساب.",
        back_kb("admin_users_section"),
    )
    await call.answer("تم التنفيذ")


# ========== قسم الإذاعة ============
@router.callback_query(F.data == "admin_broadcast_section")
async def admin_broadcast_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await edit_or_replace(
        call,
        "<b>قسم الإذاعة</b>\n\nاختر نوع الإذاعة:",
        admin_subsection_kb([
            ("إذاعة مستخدمي البوت", "admin_bot_broadcast"),
            ("إذاعة القنوات", "admin_channel_broadcast"),
            ("إذاعة العملاء", "admin_customer_broadcast"),
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "admin_bot_broadcast")
async def admin_bot_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(BotBroadcastSG.add_text)
    count = await db.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE user_id!=$1", settings.owner_id
    )
    await edit_or_replace(
        call,
        f"<b>إذاعة مستخدمي البوت</b>\n\nعدد: <b>{count}</b>\n\n"
        "أرسل النص أو الميديا.",
        cancel_kb(),
    )
    await call.answer()


@router.message(BotBroadcastSG.add_text)
async def admin_bot_broadcast_do(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    text = (message.html_text or message.caption or "").strip()
    if not text and not any((
        message.photo, message.video, message.voice,
        message.audio, message.document, message.animation,
    )):
        await message.answer("أرسل نصًا أو ميديا.")
        return
    rows = await db.fetch(
        "SELECT user_id FROM bot_users WHERE user_id!=$1 AND blocked=FALSE",
        settings.owner_id,
    )
    sem = asyncio.Semaphore(30)

    async def send_one(uid: int) -> bool:
        async with sem:
            try:
                if message.photo:
                    await message.bot.send_photo(
                        uid, message.photo[-1].file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.video:
                    await message.bot.send_video(
                        uid, message.video.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.voice:
                    await message.bot.send_voice(
                        uid, message.voice.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.audio:
                    await message.bot.send_audio(
                        uid, message.audio.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.document:
                    await message.bot.send_document(
                        uid, message.document.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.animation:
                    await message.bot.send_animation(
                        uid, message.animation.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                else:
                    await message.bot.send_message(uid, text, parse_mode="HTML")
                return True
            except Exception:
                return False

    results = await asyncio.gather(*(send_one(r["user_id"]) for r in rows))
    sent = sum(results)
    await state.clear()
    await audit(message.from_user.id, "broadcast_bot", sent=sent, total=len(rows))
    await message.answer(
        f"تم الإرسال إلى <b>{sent}</b> من <b>{len(rows)}</b> مستخدم.",
        reply_markup=back_kb("admin_broadcast_section"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_channel_broadcast")
async def admin_channel_broadcast(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(ChannelBroadcastSG.add_text)
    count = await db.fetchval("SELECT COUNT(*) FROM bot_channels")
    await edit_or_replace(
        call,
        f"<b>إذاعة القنوات</b>\n\nعدد: <b>{count}</b>\n\n"
        "أرسل النص أو الميديا.",
        cancel_kb(),
    )
    await call.answer()


@router.message(ChannelBroadcastSG.add_text)
async def admin_channel_broadcast_do(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    text = (message.html_text or message.caption or "").strip()
    if not text and not any((
        message.photo, message.video, message.voice,
        message.audio, message.document, message.animation,
    )):
        await message.answer("أرسل نصًا أو ميديا.")
        return
    rows = await db.fetch("SELECT chat_id FROM bot_channels ORDER BY added_at")
    sem = asyncio.Semaphore(20)

    async def send_one(chat_id: int) -> bool:
        async with sem:
            try:
                if message.photo:
                    await message.bot.send_photo(
                        chat_id, message.photo[-1].file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.video:
                    await message.bot.send_video(
                        chat_id, message.video.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.voice:
                    await message.bot.send_voice(
                        chat_id, message.voice.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.audio:
                    await message.bot.send_audio(
                        chat_id, message.audio.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.document:
                    await message.bot.send_document(
                        chat_id, message.document.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                elif message.animation:
                    await message.bot.send_animation(
                        chat_id, message.animation.file_id,
                        caption=text or None, parse_mode="HTML",
                    )
                else:
                    await message.bot.send_message(chat_id, text, parse_mode="HTML")
                return True
            except Exception:
                return False

    results = await asyncio.gather(*(send_one(r["chat_id"]) for r in rows))
    sent = sum(results)
    await state.clear()
    await audit(message.from_user.id, "broadcast_channel",
                sent=sent, total=len(rows))
    await message.answer(
        f"تم الإرسال إلى <b>{sent}</b> من <b>{len(rows)}</b> قناة.",
        reply_markup=back_kb("admin_broadcast_section"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_customer_broadcast")
async def admin_customer_broadcast(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(CustomerBroadcastSG.add_text)
    count = await db.fetchval("SELECT COUNT(DISTINCT chat_id) FROM customers")
    await edit_or_replace(
        call,
        f"<b>إذاعة العملاء</b>\n\nعدد: <b>{count}</b>\n\n"
        "أرسل النص أو الميديا.",
        cancel_kb(),
    )
    await call.answer()


@router.message(CustomerBroadcastSG.add_text)
async def admin_customer_broadcast_do(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    text = (message.html_text or message.caption or "").strip()
    if not text and not any((message.photo, message.video)):
        await message.answer("أرسل نصًا أو ميديا.")
        return
    rows = await db.fetch("SELECT DISTINCT connection_id, chat_id FROM customers")
    sem = asyncio.Semaphore(20)

    async def send_one(conn_id: str, chat_id: int) -> bool:
        async with sem:
            try:
                if message.photo:
                    await message.bot.send_photo(
                        chat_id, message.photo[-1].file_id,
                        caption=text or None,
                        business_connection_id=conn_id, parse_mode="HTML",
                    )
                elif message.video:
                    await message.bot.send_video(
                        chat_id, message.video.file_id,
                        caption=text or None,
                        business_connection_id=conn_id, parse_mode="HTML",
                    )
                else:
                    await message.bot.send_message(
                        chat_id, text,
                        business_connection_id=conn_id, parse_mode="HTML",
                    )
                return True
            except Exception:
                return False

    results = await asyncio.gather(
        *(send_one(r["connection_id"], r["chat_id"]) for r in rows)
    )
    sent = sum(results)
    await state.clear()
    await audit(message.from_user.id, "broadcast_customer",
                sent=sent, total=len(rows))
    await message.answer(
        f"تم الإرسال إلى <b>{sent}</b> من <b>{len(rows)}</b> عميل.",
        reply_markup=back_kb("admin_broadcast_section"),
        parse_mode="HTML",
    )


# ========== إذاعات مجدولة ============
@router.callback_query(F.data == "admin_scheduled_section")
async def admin_scheduled_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    rows = await db.fetch(
        "SELECT id, scope, run_at, status FROM scheduled_broadcasts "
        "WHERE status='pending' ORDER BY run_at LIMIT 30"
    )
    if not rows:
        body = "لا توجد إذاعات مجدولة."
    else:
        lines = []
        for r in rows:
            try:
                ts = r["run_at"].strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts = str(r["run_at"])
            scope_name = {"bot": "مستخدمون", "channel": "قنوات",
                          "customer": "عملاء"}.get(r["scope"], r["scope"])
            lines.append(
                f"<b>#{r['id']}</b> — {scope_name} — <code>{ts}</code>"
            )
        body = "\n".join(lines)
    kb = admin_subsection_kb([
        ("تحديث", "admin_scheduled_section"),
        ("إلغاء كل المجدولة", "admin_scheduled_clear"),
    ])
    await edit_or_replace(
        call,
        f"<b>⏰ الإذاعات المجدولة</b>\n\n{body}\n\n"
        f"<blockquote>لإنشاء إذاعة مجدولة، أضفها من لوحة الإذاعة "
        f"مع اختيار وقت التنفيذ (قريباً).</blockquote>",
        kb,
    )
    await call.answer()


@router.callback_query(F.data == "admin_scheduled_clear")
async def admin_scheduled_clear(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    result = await db.execute(
        "UPDATE scheduled_broadcasts SET status='cancelled' "
        "WHERE status='pending'"
    )
    await audit(call.from_user.id, "scheduled_clear", result)
    await call.answer("تم إلغاء كل الإذاعات المجدولة.")
    await admin_scheduled_section(call)


# ========== قسم النسخ الاحتياطية ==========
@router.callback_query(F.data == "admin_backup_section")
async def admin_backup_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await edit_or_replace(
        call,
        "<b>قسم النسخ الاحتياطية</b>\n\nاختر:",
        admin_subsection_kb([
            ("إنشاء نسخة (JSON)", "admin_backup_json"),
            ("استعادة نسخة", "admin_restore"),
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "admin_backup_json")
async def admin_backup_json(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    msg = await call.message.answer("⏳ جاري إنشاء النسخة...")
    try:
        tables = [
            "settings", "business", "payment_wallets", "customers",
            "bot_users", "bot_subscriptions", "bot_channels", "replies",
            "reply_buttons", "banned_words", "muted", "violations",
            "force_channels", "admins", "bot_messages", "account_messages",
            "tracking_logs", "tracked_messages",
        ]
        payload = {}
        for t in tables:
            rows = await db.fetch(f"SELECT * FROM {t} LIMIT 5000")
            payload[t] = [dict(r) for r in rows]
        data = json.dumps(
            payload, default=str, ensure_ascii=False, indent=2
        ).encode()
        if len(data) > MAX_BACKUP_SIZE:
            raise ValueError(
                "حجم النسخة يتجاوز الحد المدعوم لإرسالها عبر Telegram."
            )
        filename = (
            f"backup_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        )
        await msg.delete()
        await call.message.answer_document(
            BufferedInputFile(data, filename=filename),
            caption=fancy_success("تم إنشاء النسخة الاحتياطية."),
            parse_mode="HTML",
        )
        await audit(call.from_user.id, "backup_created",
                    filename=filename, bytes=len(data))
    except Exception:
        log.exception("backup.failed")
        await msg.edit_text(
            fancy_error("فشل إنشاء النسخة."), parse_mode="HTML"
        )


@router.callback_query(F.data == "admin_restore")
async def admin_restore_start(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(AdminSG.restore_backup)
    await edit_or_replace(
        call,
        "<b>استعادة نسخة احتياطية</b>\n\n"
        "أرسل ملف JSON المُنشأ بواسطة البوت.",
        cancel_kb(),
    )
    await call.answer()


@router.message(AdminSG.restore_backup)
async def admin_restore_save(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    if not message.document:
        await message.answer("أرسل ملف JSON.")
        return
    if (message.document.file_size or 0) > MAX_BACKUP_SIZE:
        await message.answer("الملف كبير جدًا (الحد 50 ميجا).")
        return
    temp_path = f"/tmp/restore_{message.message_id}.json"
    try:
        await message.bot.download(message.document.file_id, destination=temp_path)
        with open(temp_path, "rb") as f:
            payload = json.load(f)
        order = [
            "settings", "business", "payment_wallets", "bot_users",
            "bot_subscriptions", "bot_channels", "customers", "replies",
            "reply_buttons", "banned_words", "muted", "violations",
            "force_channels", "admins", "bot_messages", "account_messages",
            "tracking_logs", "tracked_messages",
        ]
        if not isinstance(payload, dict):
            raise ValueError("تنسيق ملف النسخة غير صحيح.")
        unexpected_tables = set(payload) - set(order)
        if unexpected_tables:
            raise ValueError("ملف النسخة يحتوي على جداول غير معروفة.")
        async with db.transaction() as conn:
            for table in order:
                if table not in payload:
                    continue
                rows = payload[table]
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) for row in rows
                ):
                    raise ValueError(f"بيانات الجدول {table} غير صحيحة.")
                await conn.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
                if not rows:
                    continue
                schema_rows = await conn.fetch(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=$1",
                    table,
                )
                column_types = {r["column_name"]: r["data_type"] for r in schema_rows}
                cols = list(rows[0].keys())
                if not cols or any(
                    not isinstance(c, str) or c not in column_types for c in cols
                ):
                    raise ValueError(f"أعمدة النسخة للجدول {table} غير مطابقة.")
                if any(set(row) != set(cols) for row in rows):
                    raise ValueError(
                        f"صفوف الجدول {table} لا تحتوي على الأعمدة نفسها."
                    )
                placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
                col_names = ", ".join(f'"{col}"' for col in cols)
                query = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'
                records = []
                for row in rows:
                    values = []
                    for col in cols:
                        value = row[col]
                        dt = column_types[col]
                        if value is not None and isinstance(value, str):
                            if dt in (
                                "timestamp with time zone",
                                "timestamp without time zone",
                            ):
                                value = datetime.fromisoformat(
                                    value.replace("Z", "+00:00")
                                )
                            elif dt == "date":
                                value = datetime.fromisoformat(value).date()
                        values.append(value)
                    records.append(tuple(values))
                await conn.executemany(query, records)
        db.cache_invalidate()
        await state.clear()
        await audit(message.from_user.id, "backup_restored",
                    filename=message.document.file_name or "")
        await message.answer(
            fancy_success("تمت استعادة النسخة بنجاح."),
            reply_markup=back_kb("admin_backup_section"),
            parse_mode="HTML",
        )
    except Exception as exc:
        log.exception("restore.failed")
        await message.answer(
            fancy_error(f"فشل الاستعادة: {safe_html(exc)}"),
            parse_mode="HTML",
        )
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


# ========== سجل التدقيق ============
@router.callback_query(F.data == "admin_audit_section")
async def admin_audit_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    rows = await db.fetch(
        "SELECT actor_id, action, target, created_at "
        "FROM audit_logs ORDER BY id DESC LIMIT 30"
    )
    if not rows:
        body = "لا توجد سجلات."
    else:
        lines = []
        for r in rows:
            try:
                ts = r["created_at"].strftime("%m-%d %H:%M")
            except Exception:
                ts = str(r["created_at"])
            lines.append(
                f"• <code>{ts}</code> — <b>{safe_html(r['action'])}</b> "
                f"— <code>{safe_html(r['target'])}</code> "
                f"(actor: <code>{r['actor_id']}</code>)"
            )
        body = "\n".join(lines)
    await edit_or_replace(
        call,
        f"<b>🧾 سجل التدقيق (آخر 30)</b>\n\n{body}",
        admin_subsection_kb([
            ("تحديث", "admin_audit_section"),
        ]),
    )
    await call.answer()


# ========== قسم إعدادات البوت ==========
@router.callback_query(F.data == "admin_settings_section")
async def admin_settings_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await edit_or_replace(
        call,
        "<b>قسم إعدادات البوت</b>\n\nاختر الإعداد المطلوب:",
        admin_subsection_kb([
            ("الإحصائيات", "admin_stats"),
            ("حالة البوت", "admin_status"),
            ("فيديو لوحة التحكم", "admin_dashboard_video"),
            ("صورة ترحيب البوت", "admin_bot_welcome_image"),
            ("تعديل رسائل البوت", "admin_messages_section"),
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "admin_status")
async def admin_status(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    enabled = await db.fetchval(
        "SELECT enabled FROM settings WHERE owner_id=$1", settings.owner_id
    )
    biz_count = await db.fetchval(
        "SELECT COUNT(*) FROM business WHERE enabled=TRUE"
    )
    pool = db.pool
    text = (
        f"<b>حالة البوت</b>\n\n"
        f"الحالة: <b>{'مفعّل' if enabled else 'متوقف'}</b>\n"
        f"الاتصالات: <b>{biz_count}</b>\n"
        f"Pool size: <b>{pool.get_size()}</b>\n"
        f"Pool idle: <b>{pool.get_idle_size()}</b>\n"
        f"Pool max: <b>{pool.get_max_size()}</b>\n"
        f"وضع الصيانة: "
        f"<b>{'مفعّل ⚠️' if settings.maintenance_mode else 'معطّل ✅'}</b>\n"
        f"وضع التشغيل: "
        f"<b>{'Webhook' if settings.use_webhook else 'Polling'}</b>\n"
        f"نظام الإحالة: "
        f"<b>{'مفعّل ✅' if settings.referral_enabled else 'معطّل ❌'}</b>"
    )
    await edit_or_replace(
        call, text, back_kb("admin_settings_section"),
    )
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    row = await db.fetchrow("""
        SELECT
            (SELECT COUNT(*) FROM bot_users) AS users,
            (SELECT COUNT(*) FROM business WHERE enabled=TRUE) AS linked,
            (SELECT COUNT(*) FROM customers) AS customers,
            (SELECT COUNT(*) FROM bot_subscriptions
             WHERE expires_at > NOW()) AS active_subs,
            (SELECT COUNT(*) FROM bot_channels) AS channels,
            (SELECT COUNT(*) FROM replies) AS replies,
            (SELECT COUNT(*) FROM admins) AS admins,
            (SELECT COUNT(*) FROM referrals) AS referrals,
            (SELECT COUNT(*) FROM admin_notifications WHERE read=FALSE) AS pending
    """)
    text = (
        f"<b>إحصائيات {safe_html(settings.bot_name)}</b>\n\n"
        f"👥 مستخدمون: <b>{row['users']}</b>\n"
        f"🔗 حسابات مرتبطة: <b>{row['linked']}</b>\n"
        f"💬 عملاء: <b>{row['customers']}</b>\n"
        f"💎 اشتراكات نشطة: <b>{row['active_subs']}</b>\n"
        f"📢 قنوات: <b>{row['channels']}</b>\n"
        f"💬 ردود: <b>{row['replies']}</b>\n"
        f"👑 أدمنية: <b>{row['admins']}</b>\n"
        f"🎁 إحالات: <b>{row['referrals']}</b>\n"
        f"🔔 إشعارات غير مقروءة: <b>{row['pending']}</b>"
    )
    await edit_or_replace(
        call, text, back_kb("admin_settings_section"),
    )
    await call.answer()


# ========== فيديو لوحة التحكم ==========
@router.callback_query(F.data == "admin_dashboard_video")
async def admin_dashboard_video(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        await call.answer("هذه الخاصية للمالك فقط.", show_alert=True)
        return
    await state.set_state(MediaSG.dashboard_video)
    await edit_or_replace(
        call,
        "<b>فيديو لوحة التحكم</b>\n\n"
        "أرسل الفيديو الذي يظهر للعميل عند الضغط على «لوحة التحكم» بدون ربط حساب.",
        cancel_kb(),
    )
    await call.answer()


@router.message(MediaSG.dashboard_video)
async def admin_dashboard_video_save(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
        
    file_id = None
    file_type = None
    
    if message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.animation:
        file_id = message.animation.file_id
        file_type = "animation"
        
    if not file_id:
        await message.answer("أرسل فيديو (Video) أو صورة متحركة (GIF/Animation) فقط.")
        return
        
    await db.execute(
        "UPDATE settings SET dashboard_video_file_id=$1, "
        "dashboard_video_type=$2 WHERE owner_id=$3",
        file_id, file_type, settings.owner_id,
    )
    await state.clear()
    await message.answer(
        fancy_success(f"تم تحديث فيديو لوحة التحكم ({file_type})."),
        reply_markup=back_kb("admin_settings_section"),
        parse_mode="HTML",
    )


# ========== صورة ترحيب البوت ==========
@router.callback_query(F.data == "admin_bot_welcome_image")
async def admin_bot_welcome_image(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        await call.answer("هذه الخاصية للمالك فقط.", show_alert=True)
        return
    await state.set_state(AdminSG.bot_welcome_image)
    await edit_or_replace(
        call,
        "<b>صورة ترحيب البوت</b>\n\n"
        "أرسل الصورة الآن، وستظهر في رسالة /start العامة للبوت.",
        cancel_kb(),
    )
    await call.answer()


@router.message(AdminSG.bot_welcome_image)
async def admin_bot_welcome_image_save(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    if not message.photo:
        await message.answer("من فضلك أرسل صورة فقط.")
        return
    await db.execute(
        "UPDATE settings SET bot_welcome_image_file_id=$1, "
        "bot_welcome_image_type='photo' WHERE owner_id=$2",
        message.photo[-1].file_id, settings.owner_id,
    )
    await state.clear()
    await message.answer(
        fancy_success("تم حفظ صورة ترحيب البوت."),
        reply_markup=back_kb("admin_settings_section"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_bot_welcome_image_delete")
async def admin_bot_welcome_image_delete(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        await call.answer("هذه الخاصية للمالك فقط.", show_alert=True)
        return
    await db.execute(
        "UPDATE settings SET bot_welcome_image_file_id='', "
        "bot_welcome_image_type='' WHERE owner_id=$1",
        settings.owner_id,
    )
    await edit_or_replace(
        call,
        "تم حذف صورة ترحيب البوت بنجاح.",
        back_kb("admin_messages_section"),
    )
    await call.answer("تم الحذف.", show_alert=True)


# ========== قسم رسائل البوت ==========
@router.callback_query(F.data == "admin_messages_section")
async def admin_messages_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await edit_or_replace(
        call,
        "<b>قسم رسائل البوت</b>\n\nاختر الرسالة:",
        admin_subsection_kb([
            ("تعديل ترحيب البوت", "editmsg_welcome"),
            ("تعديل رسالة الاشتراك", "editmsg_bot_force_sub"),
            ("تعديل اشتراك الحساب", "editmsg_account_force_sub"),
            ("تعديل رسالة الربط", "editmsg_link_help"),
            ("حذف صورة الترحيب", "admin_bot_welcome_image_delete"),
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("editmsg_"))
async def edit_bot_message_start(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    key = call.data[len("editmsg_"):]
    await state.update_data(bot_message_key=key)
    await state.set_state(AdminSG.edit_bot_message)
    current = await db.fetchval(
        "SELECT text FROM bot_messages WHERE message_key=$1", key
    ) or "(لم يتم التعديل بعد)"
    await edit_or_replace(
        call,
        f"<b>تعديل الرسالة</b>\n\nالحالية:\n{current}\n\n"
        "أرسل النص الجديد.",
        cancel_kb(),
    )
    await call.answer()


@router.message(AdminSG.edit_bot_message)
async def edit_bot_message_save(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("أرسل نصًا صحيحًا.")
        return
    data = await state.get_data()
    key = data.get("bot_message_key")
    await db.execute(
        """INSERT INTO bot_messages(message_key, text) VALUES($1, $2)
           ON CONFLICT(message_key) DO UPDATE SET text=EXCLUDED.text""",
        key, text,
    )
    await state.clear()
    await message.answer(
        fancy_success("تم حفظ الرسالة."),
        reply_markup=back_kb("admin_messages_section"),
        parse_mode="HTML",
    )


# ========== الاشتراك الإجباري (بوت) ==========
@router.callback_query(F.data == "admin_force_section")
async def admin_force_section(
    call: CallbackQuery, state: FSMContext | None = None
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    if state is not None:
        await state.clear()
    count = await db.fetchval(
        "SELECT COUNT(*) FROM force_channels WHERE owner_id=$1 AND scope='bot'",
        settings.owner_id,
    )
    enabled = await db.fetchval(
        "SELECT bot_force_enabled FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    await edit_or_replace(
        call,
        f"<b>قسم الاشتراك الإجباري (البوت)</b>\n\n"
        f"عدد القنوات: <b>{count}</b>\n"
        f"الحالة: <b>{'مفعّل' if enabled else 'معطّل'}</b>",
        admin_subsection_kb([
            (f"{'تعطيل' if enabled else 'تفعيل'} الاشتراك", "admin_force_toggle"),
            ("إضافة قناة", "admin_force_add"),
            ("عرض القنوات", "admin_force_list"),
            ("حذف قناة", "admin_force_del_list"),
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "admin_force_toggle")
async def admin_force_toggle(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    cur = await db.fetchval(
        "SELECT bot_force_enabled FROM settings WHERE owner_id=$1",
        settings.owner_id,
    )
    await db.execute(
        "UPDATE settings SET bot_force_enabled=$1 WHERE owner_id=$2",
        not bool(cur), settings.owner_id,
    )
    db.cache_invalidate(f"setting:{settings.owner_id}:")
    await admin_force_section(call, None)  # type: ignore


@router.callback_query(F.data == "admin_force_add")
async def admin_force_add(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.update_data(force_owner=settings.owner_id, force_scope="bot")
    await state.set_state(ForceSG.add_channel)
    await edit_or_replace(
        call,
        "<b>إضافة قناة للاشتراك الإجباري</b>\n\n"
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
            reply_markup=back_kb("admin_force_section"),
            parse_mode="HTML",
        )
        return
    data = await state.get_data()
    scope = data.get("force_scope", "bot")
    owner = data.get("force_owner") or settings.owner_id

    kind, value = parse_channel_input(text)
    if not value:
        await state.clear()
        await message.answer(
            "❌ رابط غير صالح",
            reply_markup=back_kb("admin_force_section"),
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
            reply_markup=back_kb("admin_force_section"),
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
                bot_username, "admin_force_add", "admin_force_section",
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
    from plugins import inform as _inform
    _inform._force_channels_cache.clear()

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
            [InlineKeyboardButton(text="📋 عرض القنوات", callback_data="admin_force_list")],
            [InlineKeyboardButton(text="رجوع", callback_data="admin_force_section")],
        ]),
        parse_mode="HTML",
    )
    await audit(owner, "force_channel_add", stored_key, scope=scope)


@router.callback_query(F.data == "admin_force_list")
async def admin_force_channels_list(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    rows = await db.fetch(
        "SELECT id, channel, chat_id, title FROM force_channels "
        "WHERE owner_id=$1 AND scope='bot' ORDER BY id",
        settings.owner_id,
    )
    if not rows:
        await edit_or_replace(
            call,
            "<b>📋 قنوات الاشتراك الإجباري</b>\n\n"
            "<blockquote>لا توجد قنوات مضافة حالياً.</blockquote>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ إضافة قناة", callback_data="admin_force_add")],
                [InlineKeyboardButton(text="رجوع", callback_data="admin_force_section")],
            ]),
        )
        await call.answer()
        return
    lines = [f"<b>📋 قنوات الاشتراك الإجباري ({len(rows)})</b>\n"]
    for i, r in enumerate(rows, 1):
        title = safe_html(r["title"] or "بدون اسم")
        lines.append(
            f"<b>#{i}</b> — {title}\n"
            f"    🆔 <code>{safe_html(r['channel'])}</code>\n"
        )
    kb_rows = []
    for r in rows:
        title_short = (r["title"] or "قناة")[:28]
        ch = str(r["channel"] or "")
        if ch.startswith("@"):
            url = f"https://t.me/{ch[1:]}"
        elif r["chat_id"]:
            url = f"https://t.me/c/{str(r['chat_id']).replace('-100','',1)}"
        else:
            url = None
        if url:
            kb_rows.append([InlineKeyboardButton(
                text=f"📢 {title_short}", url=url,
            )])
    kb_rows.append([InlineKeyboardButton(
        text="➕ إضافة قناة أخرى", callback_data="admin_force_add",
    )])
    kb_rows.append([InlineKeyboardButton(
        text="🗑 حذف قناة", callback_data="admin_force_del_list",
    )])
    kb_rows.append([InlineKeyboardButton(
        text="🔄 تحديث", callback_data="admin_force_list",
    )])
    kb_rows.append([InlineKeyboardButton(
        text="رجوع", callback_data="admin_force_section",
    )])
    await edit_or_replace(
        call, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await call.answer(f"عدد القنوات: {len(rows)}")


@router.callback_query(F.data == "admin_force_del_list")
async def admin_force_del_list(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    rows = await db.fetch(
        "SELECT id, title FROM force_channels "
        "WHERE owner_id=$1 AND scope='bot' ORDER BY id",
        settings.owner_id,
    )
    if not rows:
        await edit_or_replace(
            call, "لا توجد قنوات.", back_kb("admin_force_section")
        )
        return
    kb = [
        [InlineKeyboardButton(
            text=f"#{r['id']} - {r['title'][:30]}",
            callback_data=f"fdel_{r['id']}",
        )] for r in rows
    ]
    kb.append([InlineKeyboardButton(text="رجوع", callback_data="admin_force_section")])
    await edit_or_replace(
        call, "<b>اختر القناة للحذف:</b>",
        InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fdel_"))
async def force_del_confirm(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    fid = int(call.data.split("_")[-1])
    row = await db.fetchrow(
        "SELECT title FROM force_channels "
        "WHERE id=$1 AND owner_id=$2 AND scope='bot'",
        fid, settings.owner_id,
    )
    if not row:
        await call.answer("غير موجودة.", show_alert=True)
        return
    from plugins.Temporary import confirm_kb
    await edit_or_replace(
        call,
        f"<b>تأكيد الحذف</b>\n\n{safe_html(row['title'])}",
        confirm_kb("fdel", str(fid)),
    )
    await call.answer()


# ========== قسم الإحالات ============
@router.callback_query(F.data == "admin_referral_section")
async def admin_referral_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    total = await db.fetchval("SELECT COUNT(*) FROM referrals") or 0
    rewarded = await db.fetchval(
        "SELECT COUNT(*) FROM referrals WHERE rewarded=TRUE"
    ) or 0
    pending = await db.fetchval(
        "SELECT COUNT(*) FROM referrals WHERE rewarded=FALSE"
    ) or 0
    top_rows = await db.fetch(
        "SELECT referrer_id, COUNT(*) AS cnt FROM referrals "
        "GROUP BY referrer_id ORDER BY cnt DESC LIMIT 10"
    )
    top_text = "\n".join(
        f"<b>• <code>{r['referrer_id']}</code> — {r['cnt']} إحالة</b>"
        for r in top_rows
    ) or "لا توجد إحالات بعد."
    status_text = "مفعّل ✅" if settings.referral_enabled else "معطّل ❌"
    text = (
        f"<b>🎁 قسم الإحالات</b>\n\n"
        f"الحالة: <b>{status_text}</b>\n"
        f"الهدف: <b>{settings.referral_target}</b> إحالات\n"
        f"المكافأة: <b>{settings.referral_reward_days}</b> أيام\n\n"
        f"إجمالي الإحالات: <b>{total}</b>\n"
        f"تمت مكافأتها: <b>{rewarded}</b>\n"
        f"في الانتظار: <b>{pending}</b>\n\n"
        f"<b>أعلى 10 مُحيلين:</b>\n{top_text}"
    )
    await edit_or_replace(call, text, back_kb("admin_panel"))
    await call.answer()


# ========== قسم الإشعارات ============
@router.callback_query(F.data == "admin_notifications_section")
async def admin_notifications_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    rows = await db.fetch(
        "SELECT id, notif_type, created_at, read FROM admin_notifications "
        "WHERE owner_id=$1 ORDER BY id DESC LIMIT 20",
        settings.owner_id,
    )
    if not rows:
        body = "لا توجد إشعارات."
    else:
        type_labels = {
            "new_user": "👤 مستخدم جديد",
            "new_connection": "✅ ربط حساب",
            "unlink": "⚠️ إلغاء ربط",
            "banned_word": "🚫 كلمة ممنوعة",
            "subscription_expiring": "⏰ اشتراك ينتهي",
            "pool_warning": "⚠️ Pool",
        }
        lines = []
        for r in rows:
            label = type_labels.get(r["notif_type"], r["notif_type"])
            read_mark = "✅" if r["read"] else "🔵"
            try:
                ts = r["created_at"].strftime("%m-%d %H:%M")
            except Exception:
                ts = str(r["created_at"])
            lines.append(f"{read_mark} {label} — <code>{ts}</code>")
        body = "\n".join(lines)
    unread = await db.fetchval(
        "SELECT COUNT(*) FROM admin_notifications "
        "WHERE owner_id=$1 AND read=FALSE", settings.owner_id,
    )
    text = (
        f"<b>🔔 قسم الإشعارات</b>\n\n"
        f"غير مقروء: <b>{unread}</b>\n\n"
        f"<blockquote>{body}</blockquote>"
    )
    await edit_or_replace(
        call, text,
        admin_subsection_kb([
            ("تحديث", "admin_notifications_section"),
            ("مسح المقروءة", "admin_notifications_clear"),
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "admin_notifications_clear")
async def admin_notifications_clear(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    result = await db.execute(
        "DELETE FROM admin_notifications WHERE owner_id=$1 AND read=TRUE",
        settings.owner_id,
    )
    count = int(result.split()[-1]) if result.startswith("DELETE") else 0
    await call.answer(f"تم مسح {count} إشعار.", show_alert=True)
    await admin_notifications_section(call)


# ========== وضع الصيانة ==========
@router.callback_query(F.data == "admin_maintenance_section")
async def admin_maintenance_section(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    status = "مفعّل ⚠️" if settings.maintenance_mode else "معطّل ✅"
    current_msg = safe_html(settings.maintenance_message or "غير محدد")
    text = (
        f"<b>🛠 قسم وضع الصيانة</b>\n\n"
        f"الحالة الحالية: <b>{status}</b>\n\n"
        f"<b>الرسالة الحالية:</b>\n"
        f"<blockquote>{current_msg}</blockquote>"
    )
    kb = admin_subsection_kb([
        (f"{'تعطيل' if settings.maintenance_mode else 'تفعيل'} الصيانة",
         "admin_maintenance_toggle"),
        ("تعديل الرسالة", "admin_maintenance_edit"),
    ])
    await edit_or_replace(call, text, kb)
    await call.answer()


@router.callback_query(F.data == "admin_maintenance_toggle")
async def admin_maintenance_toggle(call: CallbackQuery) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    new_value = not settings.maintenance_mode
    settings.maintenance_mode = new_value
    await db.execute(
        "UPDATE settings SET maintenance_enabled=$1, "
        "maintenance_message=$2 WHERE owner_id=$3",
        new_value, settings.maintenance_message, settings.owner_id,
    )
    db.cache_invalidate(f"setting:{settings.owner_id}:")
    await audit(call.from_user.id, "maintenance_toggle", value=new_value)
    status = "مفعّل ⚠️" if new_value else "معطّل ✅"
    await call.answer(f"وضع الصيانة: {status}", show_alert=True)
    await admin_maintenance_section(call)


@router.callback_query(F.data == "admin_maintenance_edit")
async def admin_maintenance_edit(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user or not _is_owner(call.from_user.id):
        return
    await state.set_state(MaintenanceSG.new_message)
    await edit_or_replace(
        call,
        "<b>✏️ تعديل رسالة الصيانة</b>\n\n"
        "أرسل الرسالة الجديدة (HTML مدعوم).",
        cancel_kb(),
    )
    await call.answer()


@router.message(MaintenanceSG.new_message)
async def admin_maintenance_edit_save(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    new_msg = (message.html_text or message.text or "").strip()
    if not new_msg:
        await message.answer("أرسل رسالة صحيحة.")
        return
    if len(new_msg) > 1000:
        await message.answer("الرسالة طويلة جدًا (الحد 1000 حرف).")
        return
    settings.maintenance_message = new_msg
    await db.execute(
        "UPDATE settings SET maintenance_message=$1 WHERE owner_id=$2",
        new_msg, settings.owner_id,
    )
    db.cache_invalidate(f"setting:{settings.owner_id}:")
    await state.clear()
    await message.answer(
        fancy_success("تم حفظ رسالة الصيانة."),
        reply_markup=back_kb("admin_maintenance_section"),
        parse_mode="HTML",
    )


# ========== أوامر سريعة ============
@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    row = await db.fetchrow("""
        SELECT
            (SELECT COUNT(*) FROM bot_users) AS users,
            (SELECT COUNT(*) FROM business WHERE enabled=TRUE) AS linked,
            (SELECT COUNT(*) FROM bot_subscriptions
             WHERE expires_at > NOW()) AS subs,
            (SELECT COUNT(*) FROM referrals) AS referrals
    """)
    await message.answer(
        f"<b>إحصائيات</b>\n\n"
        f"مستخدمون: {row['users']}\n"
        f"حسابات: {row['linked']}\n"
        f"اشتراكات: {row['subs']}\n"
        f"إحالات: {row['referrals']}",
        parse_mode="HTML",
    )


@router.message(Command("pool"))
async def cmd_pool(message: Message) -> None:
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    pool = db.pool
    await message.answer(
        f"<b>Pool</b>\nmin={pool.get_min_size()} max={pool.get_max_size()}\n"
        f"size={pool.get_size()} idle={pool.get_idle_size()}",
        parse_mode="HTML",
    )