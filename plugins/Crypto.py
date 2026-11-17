# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import urllib.request
from datetime import datetime, timezone

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings
from Database import (
    db, audit, save_crypto_snapshot, get_crypto_history,
)
from plugins.Temporary import (
    back_kb, cancel_kb, edit_or_replace, fancy_error,
    fancy_info, fancy_success, safe_html,
)

log = structlog.get_logger(__name__)
router = Router(name="crypto")
UTC = timezone.utc
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


_live_price_cache: dict | None = None
_live_price_cache_expires_at = 0.0
_live_price_cache_failed_until = 0.0
_live_price_cache_lock = asyncio.Lock()


async def _fetch_uncached() -> dict | None:
    def load(url: str):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"{settings.bot_name}/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return json.loads(resp.read().decode("utf-8"))

    async def try_sources(sources, label):
        errors = []
        for url, extract in sources:
            try:
                data = await asyncio.to_thread(load, url)
                value = float(extract(data))
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid price")
                return value
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        raise RuntimeError(f"failed {label}: " + " | ".join(errors))

    ton_sources = [
        (
            "https://tonapi.io/v2/rates?tokens=ton&currencies=usd",
            lambda d: d["rates"]["TON"]["prices"]["USD"],
        ),
        (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=the-open-network&vs_currencies=usd",
            lambda d: d["the-open-network"]["usd"],
        ),
    ]
    gram_sources = [
        (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=the-open-network&vs_currencies=usd",
            lambda d: d["the-open-network"]["usd"] * 0,
        ),
        (
            "https://tonapi.io/v2/rates?tokens=ton&currencies=usd",
            lambda d: d["rates"]["TON"]["prices"]["USD"] * 1,
        ),
    ]
    fx_sources = [
        (
            "https://open.er-api.com/v6/latest/USD",
            lambda d: (
                d["rates"]["EGP"] if d.get("result") != "error" else None
            ),
        ),
        (
            "https://api.exchangerate-api.com/v4/latest/USD",
            lambda d: d["rates"]["EGP"],
        ),
    ]
    try:
        ton_usd, gram_usd, usd_egp = await asyncio.gather(
            try_sources(ton_sources, "TON"),
            try_sources(gram_sources, "GRAM"),
            try_sources(fx_sources, "EGP"),
        )
        return {
            "ton_usd": ton_usd,
            "usd_egp": usd_egp,
            "ton_egp": ton_usd * usd_egp,
            "gram_usd": gram_usd,
        }
    except Exception:
        log.exception("crypto.fetch.failed")
        return None


async def fetch_live_prices(force: bool = False) -> dict | None:
    global _live_price_cache, _live_price_cache_expires_at
    global _live_price_cache_failed_until

    now = time.monotonic()
    if not force:
        if _live_price_cache is not None and now < _live_price_cache_expires_at:
            return dict(_live_price_cache)
        if now < _live_price_cache_failed_until:
            return None

    async with _live_price_cache_lock:
        now = time.monotonic()
        if not force and _live_price_cache is not None and now < _live_price_cache_expires_at:
            return dict(_live_price_cache)
        try:
            prices = await asyncio.wait_for(_fetch_uncached(), timeout=8)
        except asyncio.TimeoutError:
            prices = None
        if prices is not None:
            _live_price_cache = dict(prices)
            _live_price_cache_expires_at = time.monotonic() + 60
            _live_price_cache_failed_until = 0.0
            await save_crypto_snapshot(
                prices["ton_usd"], prices["usd_egp"],
                prices["ton_egp"], prices["gram_usd"],
            )
        else:
            _live_price_cache_failed_until = time.monotonic() + 5
        return dict(prices) if prices is not None else None


_ALIAS = {
    "ton": ("ton", "t", "تون", "ت"),
    "usd": ("usd", "usdt", "u", "$", "dollar", "دولار", "دولر"),
    "egp": ("egp", "ج", "جنيه", "جنية", "مصري", "مصرى"),
    "gram": ("gram", "g", "جرام", "غرام"),
}


def _currency_of(unit: str) -> str | None:
    u = unit.lower()
    for key, aliases in _ALIAS.items():
        if u in aliases:
            return key
    return None


def parse_amount_query(text: str) -> tuple[float, str] | None:
    if not text:
        return None
    value = text.strip().lower().translate(ARABIC_DIGITS)
    value = value.removeprefix("/").strip()
    value = re.sub(r"^(?:price|سعر|بورصة|بورصه|عملات|عملة)\s+", "", value)

    m = re.fullmatch(
        r"(\d+(?:[.,]\d+)?)\s*([a-zA-Z$آ-ي]+)", value
    )
    if m:
        amount = float(m.group(1).replace(",", "."))
        unit = _currency_of(m.group(2))
        if unit:
            return amount, unit
    m = re.fullmatch(
        r"([a-zA-Zآ-ي]+)\s*(\d+(?:[.,]\d+)?)", value
    )
    if m:
        unit = _currency_of(m.group(1))
        if unit:
            return float(m.group(2).replace(",", ".")), unit
    m = re.fullmatch(r"(ton|usd|usdt|egp|gram|تون|دولار|جنيه|جرام)", value)
    if m:
        unit = _currency_of(m.group(1))
        if unit:
            return 1.0, unit
    return None


_LABELS = {"ton": "TON", "usd": "USD", "egp": "EGP", "gram": "GRAM"}


def _fmt(key: str, value: float) -> str:
    decimals = {"usd": 2, "ton": 4, "egp": 2, "gram": 4}.get(key, 2)
    return f"{value:,.{decimals}f}".rstrip("0").rstrip(".")


def build_values(amount: float, base: str, prices: dict) -> dict:
    usd = prices["ton_usd"] and 0
    if base == "ton":
        return {
            "ton": amount,
            "usd": amount * prices["ton_usd"],
            "egp": amount * prices["ton_egp"],
            "gram": amount * (prices["ton_usd"] / prices["gram_usd"])
                     if prices["gram_usd"] else amount,
        }
    if base == "usd":
        return {
            "ton": amount / prices["ton_usd"],
            "usd": amount,
            "egp": amount * prices["usd_egp"],
            "gram": amount / prices["gram_usd"] if prices["gram_usd"] else 0,
        }
    if base == "egp":
        return {
            "ton": amount / prices["ton_egp"],
            "usd": amount / prices["usd_egp"],
            "egp": amount,
            "gram": (amount / prices["usd_egp"]) / prices["gram_usd"]
                    if prices["gram_usd"] else 0,
        }
    if base == "gram":
        return {
            "ton": (amount * prices["gram_usd"]) / prices["ton_usd"],
            "usd": amount * prices["gram_usd"],
            "egp": amount * prices["gram_usd"] * prices["usd_egp"],
            "gram": amount,
        }
    return {}


def build_keyboard(values: dict, requested: str) -> InlineKeyboardMarkup:
    order = [requested] + [k for k in ("ton", "usd", "egp", "gram") if k != requested]
    rows = []
    for key in order:
        shown = _fmt(key, values[key])
        rows.append([InlineKeyboardButton(
            text=f"{_LABELS[key]}: {shown}",
            copy_text=CopyTextButton(text=shown),
        )])
    rows.append([
        InlineKeyboardButton(text="🔄 تحديث", callback_data="crypto_refresh"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def header(amount: float, base: str, prices: dict) -> str:
    lines = [
        f"<b>💱 سعر {_fmt(base, amount)} {_LABELS[base]}</b>",
        "",
        f"<b>TON</b> = <code>{_fmt('ton', prices['ton_usd'])}</code> USD",
        f"<b>USD</b> = <code>{_fmt('usd', prices['usd_egp'])}</code> EGP",
        f"<b>TON</b> = <code>{_fmt('ton', prices['ton_egp'])}</code> EGP",
    ]
    if prices.get("gram_usd"):
        lines.append(
            f"<b>GRAM</b> = <code>{_fmt('gram', prices['gram_usd'])}</code> USD"
        )
    lines.append("")
    lines.append(f"🕐 <i>{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC</i>")
    return "\n".join(lines)


async def _send_prices(message: Message, amount: float, base: str) -> None:
    prices = await fetch_live_prices()
    if not prices:
        await message.answer(
            fancy_error("تعذّر جلب الأسعار الآن. حاول بعد قليل.")
        )
        return
    values = build_values(amount, base, prices)
    try:
        await message.answer(
            header(amount, base, prices),
            reply_markup=build_keyboard(values, base),
            parse_mode="HTML",
        )
    except Exception:
        log.exception("crypto.send.failed")


@router.message(Command("price"))
async def cmd_price(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if arg:
        parsed = parse_amount_query(arg)
        if parsed:
            await _send_prices(message, *parsed)
            return
    await _send_prices(message, 1.0, "usd")


@router.message(Command("ton"))
async def cmd_ton(message: Message) -> None:
    await _send_prices(message, 1.0, "ton")


@router.message(Command("usd"))
async def cmd_usd(message: Message) -> None:
    await _send_prices(message, 1.0, "usd")


@router.message(Command("egp"))
async def cmd_egp(message: Message) -> None:
    await _send_prices(message, 100.0, "egp")


@router.message(Command("gram"))
async def cmd_gram(message: Message) -> None:
    await _send_prices(message, 1.0, "gram")


@router.business_message(F.text.regexp(r"^\d"))
async def biz_price_auto(message: Message) -> None:
    bc_id = message.business_connection_id
    if not bc_id or not message.text:
        return
    biz = await db.fetchrow(
        "SELECT user_id, enabled FROM business WHERE connection_id=$1",
        bc_id,
    )
    if not biz or not biz["enabled"]:
        return
    owner_id = biz["user_id"]
    enabled = await db.fetchval(
        "SELECT crypto_enabled FROM settings WHERE owner_id=$1",
        owner_id,
    )
    if enabled is False:
        return
    parsed = parse_amount_query(message.text.strip())
    if not parsed:
        return
    amount, base = parsed
    prices = await fetch_live_prices()
    if not prices:
        return
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
        log.exception("crypto.biz_send.failed")


@router.callback_query(F.data == "crypto_refresh")
async def crypto_refresh(call: CallbackQuery) -> None:
    prices = await fetch_live_prices(force=True)
    if not prices:
        await call.answer("تعذّر التحديث، حاول لاحقاً.", show_alert=True)
        return
    values = build_values(1.0, "usd", prices)
    try:
        await call.message.edit_text(
            header(1.0, "usd", prices),
            reply_markup=build_keyboard(values, "usd"),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await call.answer("تم التحديث ✅")


class CryptoAlertSG(StatesGroup):
    ton_target = State()
    usd_target = State()


async def crypto_alert_worker(bot: Bot) -> None:
    while True:
        try:
            await asyncio.sleep(300)
            rows = await db.fetch(
                "SELECT owner_id, crypto_alert_ton_usd, crypto_alert_usd_egp "
                "FROM settings "
                "WHERE crypto_enabled=TRUE "
                "AND (crypto_alert_ton_usd > 0 OR crypto_alert_usd_egp > 0)"
            )
            if not rows:
                continue
            prices = await fetch_live_prices()
            if not prices:
                continue
            for r in rows:
                owner = r["owner_id"]
                ton_target = float(r["crypto_alert_ton_usd"] or 0)
                egp_target = float(r["crypto_alert_usd_egp"] or 0)
                msgs = []
                if ton_target > 0 and prices["ton_usd"] >= ton_target:
                    msgs.append(
                        f"🚀 TON وصل <b>{_fmt('ton', prices['ton_usd'])}</b> USD "
                        f"(الهدف: {_fmt('ton', ton_target)})"
                    )
                if egp_target > 0 and prices["usd_egp"] >= egp_target:
                    msgs.append(
                        f"📈 USD/EGP وصل <b>{_fmt('usd', prices['usd_egp'])}</b> "
                        f"(الهدف: {_fmt('usd', egp_target)})"
                    )
                if not msgs:
                    continue
                try:
                    await bot.send_message(
                        owner,
                        "<b>🔔 تنبيه سعر</b>\n\n" + "\n\n".join(msgs),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                await db.execute(
                    "UPDATE settings SET crypto_alert_ton_usd=0, "
                    "crypto_alert_usd_egp=0 WHERE owner_id=$1",
                    owner,
                )
        except Exception:
            log.exception("crypto_alert_worker.failed")


@router.callback_query(F.data == "crypto_alert_menu")
async def crypto_alert_menu(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    row = await db.fetchrow(
        "SELECT crypto_enabled, crypto_alert_ton_usd, crypto_alert_usd_egp "
        "FROM settings WHERE owner_id=$1",
        call.from_user.id,
    )
    enabled = bool(row and row["crypto_enabled"])
    ton_t = float(row["crypto_alert_ton_usd"] or 0) if row else 0
    egp_t = float(row["crypto_alert_usd_egp"] or 0) if row else 0
    text = (
        "<b>🔔 تنبيهات الأسعار</b>\n\n"
        f"الحالة: <b>{'مفعّل ✅' if enabled else 'معطّل ❌'}</b>\n"
        f"حد تنبيه TON/USD: <code>{_fmt('ton', ton_t) if ton_t else 'غير محدد'}</code>\n"
        f"حد تنبيه USD/EGP: <code>{_fmt('usd', egp_t) if egp_t else 'غير محدد'}</code>\n\n"
        "<blockquote>سيتم تنبيهك عند تجاوز السعر للحد.</blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'تعطيل' if enabled else 'تفعيل'} التنبيهات",
            callback_data="crypto_alert_toggle",
        )],
        [InlineKeyboardButton(
            text="تحديد حد TON/USD", callback_data="crypto_alert_set_ton",
        )],
        [InlineKeyboardButton(
            text="تحديد حد USD/EGP", callback_data="crypto_alert_set_usd",
        )],
        [InlineKeyboardButton(text="رجوع", callback_data="home")],
    ])
    await edit_or_replace(call, text, kb)
    await call.answer()


@router.callback_query(F.data == "crypto_alert_toggle")
async def crypto_alert_toggle(call: CallbackQuery) -> None:
    if not call.from_user:
        return
    cur = await db.fetchval(
        "SELECT crypto_enabled FROM settings WHERE owner_id=$1",
        call.from_user.id,
    )
    new = not bool(cur)
    await db.execute(
        "UPDATE settings SET crypto_enabled=$1 WHERE owner_id=$2",
        new, call.from_user.id,
    )
    await crypto_alert_menu(call)


@router.callback_query(F.data == "crypto_alert_set_ton")
async def crypto_alert_set_ton(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user:
        return
    await state.set_state(CryptoAlertSG.ton_target)
    await edit_or_replace(
        call,
        "<b>أرسل سعر TON بالدولار الذي تريد التنبيه عنده</b>\n"
        "مثال: <code>5.50</code> — أرسل 0 للإلغاء.",
        cancel_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "crypto_alert_set_usd")
async def crypto_alert_set_usd(
    call: CallbackQuery, state: FSMContext
) -> None:
    if not call.from_user:
        return
    await state.set_state(CryptoAlertSG.usd_target)
    await edit_or_replace(
        call,
        "<b>أرسل سعر الدولار بالجنيه الذي تريد التنبيه عنده</b>\n"
        "مثال: <code>50.00</code> — أرسل 0 للإلغاء.",
        cancel_kb(),
    )
    await call.answer()


@router.message(CryptoAlertSG.ton_target)
async def crypto_alert_ton_save(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not message.text:
        return
    try:
        value = float(message.text.replace(",", ".").translate(ARABIC_DIGITS))
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("أرسل رقماً صحيحاً.")
        return
    await db.execute(
        "UPDATE settings SET crypto_alert_ton_usd=$1 WHERE owner_id=$2",
        value, message.from_user.id,
    )
    await state.clear()
    await audit(
        message.from_user.id, "crypto_alert_set", "ton",
        value=value,
    )
    await message.answer(
        fancy_success(f"تم تحديد حد TON/USD عند {_fmt('ton', value) or 'إلغاء'}."),
        reply_markup=back_kb("crypto_alert_menu"),
        parse_mode="HTML",
    )


@router.message(CryptoAlertSG.usd_target)
async def crypto_alert_usd_save(
    message: Message, state: FSMContext
) -> None:
    if not message.from_user or not message.text:
        return
    try:
        value = float(message.text.replace(",", ".").translate(ARABIC_DIGITS))
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("أرسل رقماً صحيحاً.")
        return
    await db.execute(
        "UPDATE settings SET crypto_alert_usd_egp=$1 WHERE owner_id=$2",
        value, message.from_user.id,
    )
    await state.clear()
    await audit(
        message.from_user.id, "crypto_alert_set", "usd_egp",
        value=value,
    )
    await message.answer(
        fancy_success(f"تم تحديد حد USD/EGP عند {_fmt('usd', value) or 'إلغاء'}."),
        reply_markup=back_kb("crypto_alert_menu"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "crypto_bourse")
async def crypto_bourse(call: CallbackQuery) -> None:
    prices = await fetch_live_prices()
    if not prices:
        await call.answer("تعذّر جلب الأسعار.", show_alert=True)
        return
    values = build_values(1.0, "usd", prices)
    text = (
        "<b>💹 بورصة العملات الحية</b>\n\n"
        + header(1.0, "usd", prices).split("\n", 1)[1]
    )
    kb = build_keyboard(values, "usd")
    kb.inline_keyboard.append([InlineKeyboardButton(
        text="🔔 تنبيهات", callback_data="crypto_alert_menu",
    )])
    await edit_or_replace(call, text, kb)
    await call.answer()


@router.message(F.text.regexp(
    r"^(?:سعر|price)\s+.+$|^بورصة$|^بورصه$"
))
async def text_price_command(message: Message) -> None:
    txt = (message.text or "").strip()
    if txt in ("بورصة", "بورصه"):
        prices = await fetch_live_prices()
        if not prices:
            await message.answer(fancy_error("تعذّر جلب الأسعار."))
            return
        values = build_values(1.0, "usd", prices)
        await message.answer(
            header(1.0, "usd", prices),
            reply_markup=build_keyboard(values, "usd"),
            parse_mode="HTML",
        )
        return
    parsed = parse_amount_query(txt)
    if parsed:
        await _send_prices(message, *parsed)