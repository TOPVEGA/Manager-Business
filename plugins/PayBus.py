
# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from Database import db, audit
from plugins.Temporary import (
    BOT_DANGER_EMOJI,
    BOT_HOME_EMOJI,
    BOT_OK_EMOJI,
    back_kb,
    cancel_kb,
    edit_or_replace,
    fancy_success,
    safe_html,
)

log = structlog.get_logger(__name__)
router = Router(name="botpay")
UTC = timezone.utc


PAYMENT_METHODS: dict[str, dict] = {
    "bybit": {
        "name": "باي بيت", "emoji": "5199662703332836840", "fallback": "💳",
        "prompt": "أرسل البيانات (الأيدي/الرقم/العنوان) لـ باي بيت الآن:",
        "aliases": ("باي بيت", "بايبيت", "Bybit", "Paybit"),
    },
    "binance": {
        "name": "Binance", "emoji": "5454205576512818013", "fallback": "🟡",
        "prompt": "أرسل البيانات (الأيدي/الرقم/العنوان) لـ Binance الآن:",
        "aliases": ("Binance", "باينانس", "بينانس"),
    },
    "shamcash": {
        "name": "شام كاش", "emoji": "5974562373619424507", "fallback": "💸",
        "prompt": "أرسل عنوان شام كاش الآن (الحروف والأرقام فقط):",
        "aliases": ("شام كاش", "شامكاش", "Sham Cash", "ShamCash"),
        "alnum_only": True,
    },
    "iraqi_master": {
        "name": "ماستر عراقي", "emoji": "5190642511802022990", "fallback": "💳",
        "prompt": "أرسل البيانات لـ ماستر عراقي:",
        "aliases": ("ماستر عراقي", "ماسترعراقي", "Master Iraqi", "Iraqi Master"),
    },
    "zaincash": {
        "name": "زين كاش", "emoji": "5226459773091731013", "fallback": "📱",
        "prompt": "أرسل البيانات لـ زين كاش:",
        "aliases": ("زين كاش", "زينكاش", "Zain Cash", "ZainCash"),
    },
    "asiasel": {
        "name": "آسياسيل", "emoji": "5388668704902895822", "fallback": "📱",
        "prompt": "أرسل البيانات لـ آسياسيل:",
        "aliases": ("آسياسيل", "اسيا سيل", "اساسيل", "Asiacell"),
    },
    "orange": {
        "name": "اورنج كاش", "emoji": "5836714629455157161", "fallback": "🟠",
        "prompt": "أرسل البيانات لـ اورنج كاش:",
        "aliases": ("اورنج كاش", "اورنج", "أورنج كاش", "أورنج",
                    "Orange Cash", "Orange"),
    },
    "we": {
        "name": "وي كاش", "emoji": "5837128389424585193", "fallback": "🟣",
        "prompt": "أرسل البيانات لـ وي كاش:",
        "aliases": ("وي كاش", "وي", "WE Cash", "WE", "We Cash", "We"),
    },
    "etisalat": {
        "name": "اتصالات كاش", "emoji": "5836811137370297987", "fallback": "📶",
        "prompt": "أرسل البيانات لـ اتصالات كاش:",
        "aliases": ("اتصالات كاش", "اتصالات", "اتصلات كاش", "اتصلات",
                    "Etisalat Cash", "Etisalat", "EG"),
    },
    "vodafone": {
        "name": "فودافون كاش", "emoji": "5834628314731387616", "fallback": "📱",
        "prompt": "أرسل البيانات لـ فودافون كاش:",
        "aliases": ("فودافون", "فودافون كاش", "فودا",
                    "Vodafone", "Vodafone Cash", "Voda"),
    },
    "tonkeeper": {
        "name": "تون كيبر", "emoji": "5424905554432631390", "fallback": "💎",
        "prompt": "أرسل البيانات لـ Tonkeeper:",
        "aliases": ("تون كيبر", "تونكيبر", "Tonkeeper", "Ton Keeper"),
    },
    "gram": {
        "name": "GRAM (TON)", "emoji": "5240164592826469263", "fallback": "🪙",
        "prompt": "أرسل البيانات لـ Gram (TON):",
        "network_required": True,
        "network_prompt": "أرسل اسم الشبكة لـ Gram (TON):\nTRC20/ERC20/BEP20/TON",
        "aliases": ("GRAM", "GRAM TON", "GRAM (TON)", "جرام",
                    "جرام تون", "جرام (تون)"),
    },
    "usdt": {
        "name": "USDT", "emoji": "5080137014775383220", "fallback": "💵",
        "prompt": "أرسل البيانات لـ USDT:",
        "network_required": True,
        "network_prompt": "أرسل اسم الشبكة لـ USDT:\nTRC20/ERC20/BEP20/TON",
        "aliases": ("USDT", "تيثر", "Tether"),
    },
}

EGYPT_GROUP_KEYS = ("orange", "we", "etisalat", "vodafone")
ENTRY_ORDER_KEYS = (
    "bybit", "binance", "shamcash", "iraqi_master", "zaincash",
    "asiasel", "tonkeeper", "gram", "usdt",
)

CUSTOMER_NAMES = {
    "orange": "اورنج كاش", "we": "وي كاش",
    "etisalat": "اتصالات كاش", "vodafone": "فودافون كاش",
}

EGYPT_GROUP_INFO = {
    "name": "شبكات مصرية", "emoji": "5976310790381115423", "fallback": "📱",
}

_PAYMENT_KB_CACHE_PREFIX = "payment_kb:"


class PaymentSG(StatesGroup):
    network = State()
    value = State()


def normalize_phrase(value: str) -> str:
    v = (value or "").strip().casefold().translate(
        str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"})
    )
    return re.sub(r"[^\w]", "", v, flags=re.UNICODE)


TRIGGER_MAP = {
    normalize_phrase(alias): key
    for key, info in PAYMENT_METHODS.items()
    for alias in info["aliases"]
}


def match_payment_method(value: str) -> Optional[str]:
    return TRIGGER_MAP.get(normalize_phrase(value))


def classify_value(value: str) -> str:
    v = (value or "").strip()
    digit_map = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    normalized = v.translate(digit_map)
    compact = re.sub(r"[\s().-]", "", normalized)
    if re.fullmatch(r"\+?\d{7,15}", compact):
        if (compact.startswith(("+", "00"))
                or (compact.startswith("0") and len(compact.lstrip("+")) >= 10)):
            return "phone"
    lowered = normalized.casefold()
    if "@" in lowered or lowered.startswith(("http://", "https://", "www.")):
        return "address"
    if re.fullmatch(r"0x[a-f0-9]{20,}", normalized, flags=re.IGNORECASE):
        return "address"
    if re.fullmatch(r"(?:EQ|UQ)[A-Za-z0-9_-]{30,}", normalized):
        return "address"
    if re.fullmatch(r"T[A-Za-z0-9]{30,}", normalized):
        return "address"
    if re.fullmatch(r"bc1[a-z0-9]{20,}", normalized, flags=re.IGNORECASE):
        return "address"
    if (len(normalized) >= 25
            and re.fullmatch(r"[A-Za-z0-9_-]+", normalized)
            and re.search(r"[A-Za-z]", normalized)
            and re.search(r"[0-9]", normalized)):
        return "address"
    return "id"


DATA_LABEL = {"phone": "رقم الهاتف", "address": "العنوان", "id": "ID"}


async def get_wallet(owner_id: int, key: str) -> Optional[dict]:
    row = await db.fetchrow(
        "SELECT wallet_value, network, enabled FROM payment_wallets "
        "WHERE owner_id=$1 AND wallet_key=$2",
        owner_id, key,
    )
    return dict(row) if row else None


async def save_wallet(
    owner_id: int, key: str, value: str, network: str = ""
) -> None:
    await db.execute(
        """INSERT INTO payment_wallets
             (owner_id, wallet_key, wallet_value, network, enabled, updated_at)
           VALUES($1, $2, $3, $4, TRUE, NOW())
           ON CONFLICT(owner_id, wallet_key) DO UPDATE SET
             wallet_value=EXCLUDED.wallet_value,
             network=EXCLUDED.network,
             enabled=TRUE,
             updated_at=NOW()""",
        owner_id, key, value, network,
    )
    db.cache_invalidate(f"{_PAYMENT_KB_CACHE_PREFIX}{owner_id}")


async def toggle_wallet(owner_id: int, key: str) -> bool:
    row = await db.fetchrow(
        """
        INSERT INTO payment_wallets(owner_id, wallet_key, enabled, updated_at)
        VALUES($1, $2, TRUE, NOW())
        ON CONFLICT(owner_id, wallet_key) DO UPDATE SET
            enabled = NOT payment_wallets.enabled,
            updated_at = NOW()
        RETURNING enabled
        """,
        owner_id, key,
    )
    db.cache_invalidate(f"{_PAYMENT_KB_CACHE_PREFIX}{owner_id}")
    return bool(row["enabled"]) if row else False


def payment_home_kb(enabled: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{'تعطيل' if enabled else 'تفعيل'} قسم طرق الدفع",
        callback_data="section_toggle_payment",
    )]]
    btns = []
    for key in ENTRY_ORDER_KEYS:
        info = PAYMENT_METHODS[key]
        btns.append(InlineKeyboardButton(
            text=info["name"], callback_data=f"payment_wallet_{key}",
        ))
    for i in range(0, len(btns), 2):
        rows.append(btns[i:i + 2])
    rows.append([InlineKeyboardButton(
        text=EGYPT_GROUP_INFO["name"], callback_data="payment_egypt",
    )])
    rows.append([InlineKeyboardButton(text="رجوع", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_egypt_kb() -> InlineKeyboardMarkup:
    btns = []
    for key in EGYPT_GROUP_KEYS:
        info = PAYMENT_METHODS[key]
        btns.append(InlineKeyboardButton(
            text=info["name"], callback_data=f"payment_wallet_{key}",
        ))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(
        text="رجوع", callback_data="payment_home",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_wallet_manage_kb(key: str, enabled: bool) -> InlineKeyboardMarkup:
    back_cb = "payment_egypt" if key in EGYPT_GROUP_KEYS else "payment_home"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"الحالة: {'مفعّل' if enabled else 'معطّل'}",
            callback_data=f"payment_toggle_{key}",
        )],
        [InlineKeyboardButton(
            text="تعديل البيانات", callback_data=f"payment_edit_{key}",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data=back_cb)],
    ])


async def wallet_panel_text(
    owner_id: int, key: str
) -> tuple[str, InlineKeyboardMarkup]:
    wallet = await get_wallet(owner_id, key)
    info = PAYMENT_METHODS[key]
    name = safe_html(info["name"])
    value = safe_html(wallet["wallet_value"]) if wallet else ""
    network = (
        safe_html(wallet["network"])
        if wallet and wallet.get("network") else ""
    )
    enabled = bool(wallet and wallet.get("enabled"))
    lines = [f"<blockquote><b>╕═══ المحفظة {name} ══╒</b></blockquote>"]
    if network:
        lines.append(
            f"<blockquote><b>الشبكة: <code>{network}</code></b></blockquote>"
        )
    lines.append(
        f"<blockquote><b>"
        f"{f'<code>{value}</code>' if value else 'لم يتم التعيين بعد'}"
        f"</b></blockquote>"
    )
    if value:
        kind = classify_value(value)
        lines.append(
            f"<blockquote><b>نوع البيانات تلقائيًا: "
            f"{DATA_LABEL.get(kind, kind)}</b></blockquote>"
        )
    lines.append(
        "<blockquote><b>اختر من الأسفل للتعديل أو التعطيل:</b></blockquote>"
    )
    lines.append("<blockquote><b>╘═════════════════════════╛</b></blockquote>")
    return "\n".join(lines), payment_wallet_manage_kb(key, enabled)


def customer_text(key: str, wallet: dict) -> str:
    info = PAYMENT_METHODS[key]
    name = safe_html(CUSTOMER_NAMES.get(key, info["name"]))
    raw = wallet["wallet_value"] or ""
    value = safe_html(raw)
    label = DATA_LABEL.get(classify_value(raw), "ID")
    network = safe_html(wallet.get("network") or "")
    separator = "━━━━━━━━━━━━━━━━━━━━"
    sections = [f"<blockquote><b>{name}</b></blockquote>"]
    if network:
        sections.append(
            f"<blockquote><b>الشبكة: <code>{network}</code></b></blockquote>"
        )
    sections.extend([
        f"<blockquote><b>{label}: <code>{value}</code></b></blockquote>",
        f"<blockquote><b>{separator}</b></blockquote>",
        "<blockquote><b>أرسل لقطة شاشة للدفع من فضلك</b></blockquote>",
    ])
    return "\n".join(sections)


def customer_kb(wallet_value: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Click to copy",
            copy_text=CopyTextButton(text=wallet_value),
        )
    ]])


async def send_payment_details(
    message: Message, key: str, wallet: dict,
    business_connection_id: str | None = None,
) -> None:
    kwargs = {
        "chat_id": message.chat.id,
        "text": customer_text(key, wallet),
        "reply_markup": customer_kb(wallet["wallet_value"]),
        "parse_mode": "HTML",
    }
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id
    await message.bot.send_message(**kwargs)


@router.callback_query(F.data == "payment_home")
async def payment_home(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.clear()
    enabled_val = await db.fetchval(
        "SELECT payment_enabled FROM settings WHERE owner_id=$1",
        call.from_user.id,
    )
    enabled = bool(enabled_val) if enabled_val is not None else True
    await edit_or_replace(
        call,
        "<b>💳 طرق الدفع</b>\n\n"
        "<blockquote>أضف بيانات وسائل الدفع الخاصة بك، وسيتم إرسالها "
        "تلقائيًا للعملاء عند طلب الدفع.</blockquote>",
        payment_home_kb(enabled),
    )
    await call.answer()


@router.callback_query(F.data == "section_toggle_payment")
async def section_toggle_payment(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    owner = call.from_user.id
    cur = await db.fetchval(
        "SELECT payment_enabled FROM settings WHERE owner_id=$1", owner
    )
    new_val = not bool(cur)
    await db.execute(
        "UPDATE settings SET payment_enabled=$1 WHERE owner_id=$2",
        new_val, owner,
    )
    db.cache_invalidate(f"setting:{owner}:")
    await payment_home(call, None)


@router.callback_query(F.data == "payment_egypt")
async def payment_egypt(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    await state.clear()
    await edit_or_replace(
        call, "<b>الشبكات المصرية</b>\n\nاختر الشبكة:",
        payment_egypt_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("payment_wallet_"))
async def payment_wallet_open(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user:
        return
    key = call.data.removeprefix("payment_wallet_")
    if key not in PAYMENT_METHODS:
        await call.answer("طريقة غير معروفة.", show_alert=True)
        return
    await state.clear()
    text, kb = await wallet_panel_text(call.from_user.id, key)
    await edit_or_replace(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("payment_toggle_"))
async def payment_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    key = call.data.removeprefix("payment_toggle_")
    if key not in PAYMENT_METHODS:
        await call.answer()
        return
    await toggle_wallet(call.from_user.id, key)
    text, kb = await wallet_panel_text(call.from_user.id, key)
    await edit_or_replace(call, text, kb)
    await call.answer("تم تحديث الحالة.")


@router.callback_query(F.data.startswith("payment_edit_"))
async def payment_edit(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user:
        return
    key = call.data.removeprefix("payment_edit_")
    if key not in PAYMENT_METHODS:
        await call.answer()
        return
    info = PAYMENT_METHODS[key]
    await state.clear()
    await state.update_data(payment_owner=call.from_user.id, payment_key=key)
    if info.get("network_required"):
        await state.set_state(PaymentSG.network)
        prompt = info["network_prompt"]
    else:
        await state.update_data(payment_network="")
        await state.set_state(PaymentSG.value)
        prompt = info["prompt"]
    await edit_or_replace(call, f"{safe_html(prompt)}", cancel_kb())
    await call.answer()


@router.message(PaymentSG.network)
async def payment_network_receive(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    key = data.get("payment_key")
    if key not in PAYMENT_METHODS:
        await state.clear()
        return
    network = (message.text or "").strip()
    if (not network or len(network) > 32
            or not all(ch.isalnum() or ch in " ._-" for ch in network)):
        await message.answer("أرسل اسم شبكة صحيحًا (مثل TRC20).")
        return
    await state.update_data(payment_network=network)
    await state.set_state(PaymentSG.value)
    await message.answer("تم حفظ الشبكة. أرسل البيانات الآن:",
                         parse_mode="HTML")


@router.message(PaymentSG.value)
async def payment_value_receive(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user:
        return
    value = (message.text or "").strip()
    data = await state.get_data()
    key = data.get("payment_key")
    owner = data.get("payment_owner")
    if key not in PAYMENT_METHODS or not owner:
        await state.clear()
        await message.answer("انتهت الجلسة. افتح طرق الدفع مجددًا.")
        return
    if not value or len(value) > 256:
        await message.answer("أرسل قيمة من 1 إلى 256 حرفًا.")
        return
    if PAYMENT_METHODS[key].get("alnum_only") and not value.isalnum():
        await message.answer(
            "يقبل الحروف والأرقام فقط، بدون مسافات أو رموز."
        )
        return
    network = (
        data.get("payment_network", "")
        if PAYMENT_METHODS[key].get("network_required") else ""
    )
    await save_wallet(owner, key, value, network)
    await state.clear()
    await audit(owner, "wallet_save", key, has_network=bool(network))
    await message.answer(
        fancy_success("تم حفظ بيانات المحفظة."),
        reply_markup=back_kb(f"payment_wallet_{key}"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("bpay_"))
async def business_payment_choice(call: CallbackQuery) -> None:
    key = call.data.removeprefix("bpay_")
    bc_id = getattr(call.message, "business_connection_id", None)
    if key not in PAYMENT_METHODS or not bc_id:
        await call.answer("تعذر تحديد طريقة الدفع.", show_alert=True)
        return
    row = await db.fetchrow(
        "SELECT user_id FROM business WHERE connection_id=$1", bc_id
    )
    if not row:
        await call.answer()
        return
    if not call.from_user or call.from_user.id != row["user_id"]:
        await call.answer()
        return
    wallet = await get_wallet(row["user_id"], key)
    if (not wallet or not wallet.get("enabled")
            or not wallet.get("wallet_value")):
        await call.answer("الطريقة غير متاحة حاليًا.", show_alert=True)
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_payment_details(
        call.message, key, wallet, business_connection_id=bc_id
    )
    await call.answer()


async def _build_payment_keyboard_uncached(
    owner_id: int,
) -> InlineKeyboardMarkup | None:
    wallets = []
    for key in PAYMENT_METHODS:
        wallet = await get_wallet(owner_id, key)
        if wallet and wallet.get("enabled") and wallet.get("wallet_value"):
            wallets.append(key)
    if not wallets:
        return None
    buttons = []
    for key in wallets:
        info = PAYMENT_METHODS[key]
        buttons.append([InlineKeyboardButton(
            text=info["name"], callback_data=f"bpay_{key}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_payment_keyboard(owner_id: int) -> InlineKeyboardMarkup | None:
    cache_key = f"{_PAYMENT_KB_CACHE_PREFIX}{owner_id}"
    cached = db.cache_get(cache_key)
    if cached is not None:
        if cached == "__empty__":
            return None
        return cached
    result = await _build_payment_keyboard_uncached(owner_id)
    if result is None:
        db.cache_set(cache_key, "__empty__")
        return None
    db.cache_set(cache_key, result)
    return result


async def try_send_payment_details(
    message: Message, owner_id: int, text_raw: str,
    business_connection_id: str,
) -> bool:
    if not text_raw:
        return False
    enabled = await db.fetchval(
        "SELECT payment_enabled FROM settings WHERE owner_id=$1", owner_id
    )
    if enabled is False:
        return False
    key = match_payment_method(text_raw)
    if not key:
        return False
    wallet = await get_wallet(owner_id, key)
    if (not wallet or not wallet.get("enabled")
            or not wallet.get("wallet_value")):
        return False
    try:
        await send_payment_details(
            message, key, wallet,
            business_connection_id=business_connection_id,
        )
        return True
    except Exception:
        log.exception("send_payment_details.failed")
        return False
