from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

import structlog
import pytz
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from config import settings
from Database import db, init_schema
from plugins import register_all
from plugins.Temporary import (
    ErrorMiddleware,
    MaintenanceMiddleware,
    ReferralMiddleware,
    ThrottlingMiddleware,
    bold_outgoing_text_middleware,
    rich_outgoing_middleware,
    clean_clock_suffix,
    clickable_user_link,
    clock_text,
    safe_html,
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiogram.bot").setLevel(logging.WARNING)
    logging.getLogger("aiogram.client").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger(__name__)
UTC = timezone.utc


async def cleanup_worker() -> None:
    while True:
        try:
            await asyncio.sleep(3600)
            log.info("cleanup.start")
            await db.execute(
                "DELETE FROM tracked_messages "
                "WHERE created_at < NOW() - INTERVAL '30 days'"
            )
            await db.execute(
                "DELETE FROM tracking_logs "
                "WHERE created_at < NOW() - INTERVAL '90 days'"
            )
            await db.execute(
                "DELETE FROM bot_subscriptions "
                "WHERE expires_at < NOW() - INTERVAL '90 days'"
            )
            log.info("cleanup.completed")
        except Exception:
            log.exception("cleanup.failed")


async def stats_worker() -> None:
    while True:
        try:
            await asyncio.sleep(300)
            pool = db.pool
            log.info(
                "pool.stats",
                size=pool.get_size(),
                idle=pool.get_idle_size(),
                max=pool.get_max_size(),
            )
        except Exception:
            log.exception("stats.failed")


async def live_clock_worker(bot: Bot) -> None:
    while True:
        try:
            rows = await db.fetch(
                "SELECT connection_id, user_id, clock_style, "
                "original_name, original_last_name "
                "FROM business WHERE enabled=TRUE AND clock_enabled=TRUE"
            )
            if not rows:
                await asyncio.sleep(60)
                continue

            for row in rows:
                try:
                    current = await bot.get_business_connection(
                        business_connection_id=row["connection_id"]
                    )
                    can_edit = bool(
                        current.rights and current.rights.can_edit_name
                    )
                    if not current.is_enabled or not can_edit:
                        await db.execute(
                            "UPDATE business SET clock_enabled=FALSE "
                            "WHERE connection_id=$1",
                            row["connection_id"],
                        )
                        continue
                    new_last = clock_text(row["clock_style"] or 0)
                    if (current.user.last_name or "") == new_last:
                        continue
                    await bot.set_business_account_name(
                        business_connection_id=row["connection_id"],
                        first_name=(
                            clean_clock_suffix(
                                current.user.first_name or ""
                            ) or " "
                        ),
                        last_name=new_last,
                    )
                except Exception:
                    log.exception(
                        "live_clock.update.failed",
                        cid=row["connection_id"],
                    )
            now = datetime.now(UTC)
            delay = 60 - now.second - (now.microsecond / 1_000_000)
            await asyncio.sleep(max(0.2, delay))
        except Exception:
            log.exception("live_clock.worker.failed")
            await asyncio.sleep(30)


async def live_bio_worker(bot: Bot) -> None:
    from plugins.callbacks import (
        _get_bio_data,
        _bio_items,
        _set_bio_data,
        _set_business_bio,
        _editable_bio_conn,
    )

    while True:
        try:
            await asyncio.sleep(60)
            rows = await db.fetch(
                "SELECT owner_id, live_bio_mode, "
                "live_bio_interval_minutes, "
                "live_bio_custom_interval_minutes, "
                "live_bio_religious_interval_minutes, "
                "live_bio_last_updated "
                "FROM settings WHERE live_bio_enabled=TRUE"
            )
            if not rows:
                continue
            now_ts = int(datetime.now(UTC).timestamp())
            for row in rows:
                try:
                    interval_value = (
                        row["live_bio_religious_interval_minutes"]
                        if row["live_bio_mode"] == "religious"
                        else row["live_bio_custom_interval_minutes"]
                    )
                    interval = max(
                        1,
                        int(
                            interval_value
                            or row["live_bio_interval_minutes"]
                            or 360
                        ),
                    ) * 60
                    last_updated = int(row["live_bio_last_updated"] or 0)
                    if now_ts - last_updated < interval:
                        continue

                    owner_id = row["owner_id"]
                    data = await _get_bio_data(owner_id)
                    items = await _bio_items(data)
                    if not data["enabled"] or not items:
                        continue
                    cid = await _editable_bio_conn(bot, owner_id)
                    if not cid:
                        continue
                    idx = data["index"] % len(items)
                    if await _set_business_bio(bot, cid, items[idx]):
                        await _set_bio_data(
                            owner_id,
                            index=(idx + 1) % len(items),
                            last_updated=now_ts,
                        )
                except Exception:
                    log.exception(
                        "live_bio.update.failed",
                        owner=row["owner_id"],
                    )
        except Exception:
            log.exception("live_bio.worker.failed")
            await asyncio.sleep(30)


async def notifications_worker(bot: Bot) -> None:
    while True:
        try:
            await asyncio.sleep(20)
            rows = await db.fetch_unread_notifications(
                settings.owner_id, limit=50
            )
            for row in rows:
                try:
                    text = _format_notification(
                        row["notif_type"], row["payload"] or {}
                    )
                    if not text:
                        await db.mark_notification_read(row["id"])
                        continue

                    reply_markup = _build_notification_kb(
                        row["notif_type"], row["payload"] or {}
                    )

                    await bot.send_message(
                        settings.owner_id,
                        text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                    await db.mark_notification_read(row["id"])
                except Exception:
                    log.exception(
                        "notification.send.failed",
                        notif_id=row["id"],
                        notif_type=row["notif_type"],
                    )
                    await asyncio.sleep(2)
        except Exception:
            log.exception("notifications.worker.failed")
            await asyncio.sleep(30)


def _format_notification(notif_type: str, payload: dict) -> str | None:
    if not isinstance(payload, dict):
        payload = {}

    if notif_type == "new_user":
        uid = payload.get("user_id", "?")
        number = payload.get("user_number", "?")
        name_raw = payload.get("name") or "بدون اسم"
        username = payload.get("username") or ""

        clickable_name = clickable_user_link(uid, name_raw)
        username_link = (
            f'<a href="https://t.me/{safe_html(username)}">'
            f'@{safe_html(username)}</a>'
            if username else "غير متوفر"
        )
        return (
            f"👤 <b>مستخدم جديد دخل البوت رقم {number}</b>\n\n"
            f"الاسم: {clickable_name}\n"
            f"اليوزر: {username_link}\n"
            f"الآيدي: <code>{uid}</code>"
        )

    if notif_type == "new_connection":
        uid = payload.get("user_id", "?")
        name_raw = payload.get("name") or "بدون اسم"
        username = payload.get("username") or ""
        perms = payload.get("permissions", "غير معروفة")

        clickable_name = clickable_user_link(uid, name_raw)
        username_link = (
            f'<a href="https://t.me/{safe_html(username)}">'
            f'@{safe_html(username)}</a>'
            if username else "غير متوفر"
        )
        return (
            f"✅ <b>تم ربط حساب جديد</b>\n\n"
            f"الاسم: {clickable_name}\n"
            f"اليوزر: {username_link}\n"
            f"الآيدي: <code>{uid}</code>\n"
            f"الصلاحيات: <b>{safe_html(perms)}</b>"
        )

    if notif_type == "unlink":
        uid = payload.get("user_id", "?")
        name_raw = payload.get("name") or "بدون اسم"
        username = payload.get("username") or ""

        clickable_name = clickable_user_link(uid, name_raw)
        username_link = (
            f'<a href="https://t.me/{safe_html(username)}">'
            f'@{safe_html(username)}</a>'
            if username else "غير متوفر"
        )
        return (
            f"⚠️ <b>تم إلغاء ربط حساب</b>\n\n"
            f"الاسم: {clickable_name}\n"
            f"اليوزر: {username_link}\n"
            f"الآيدي: <code>{uid}</code>"
        )

    if notif_type == "banned_word":
        chat_id = payload.get("chat_id", "?")
        word = safe_html(payload.get("word", ""))
        count = payload.get("count", 0)
        return (
            f"🚫 <b>كلمة ممنوعة تم رصدها</b>\n\n"
            f"العميل: <code>{chat_id}</code>\n"
            f"الكلمة: <code>{word}</code>\n"
            f"عدد المخالفات: <b>{count}</b>"
        )

    if notif_type == "subscription_expiring":
        uid = payload.get("user_id", "?")
        remaining = payload.get("remaining", "?")
        return (
            f"⏰ <b>اشتراك على وشك الانتهاء</b>\n\n"
            f"المستخدم: <code>{uid}</code>\n"
            f"المتبقي: <b>{safe_html(remaining)}</b>"
        )

    if notif_type == "pool_warning":
        size = payload.get("size", "?")
        max_size = payload.get("max_size", "?")
        return (
            f"⚠️ <b>تحذير: استهلاك Pool مرتفع</b>\n\n"
            f"الحجم الحالي: <b>{size}</b> / <b>{max_size}</b>"
        )

    return None


def _build_notification_kb(
    notif_type: str, payload: dict
) -> InlineKeyboardMarkup | None:
    if not isinstance(payload, dict):
        return None
    uid = payload.get("user_id")
    name_raw = payload.get("name") or "المستخدم"

    if notif_type in ("new_user", "new_connection", "unlink") and uid:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"👤 {name_raw[:40]}",
                url=f"tg://user?id={uid}",
            )
        ]])
    return None


async def referral_worker(bot: Bot) -> None:
    if not settings.referral_enabled:
        return
    while True:
        try:
            await asyncio.sleep(180)
            rows = await db.get_pending_referral_rewards(
                settings.referral_target
            )
            for row in rows:
                referrer_id = row["referrer_id"]
                try:
                    await _reward_referrer(bot, referrer_id)
                except Exception:
                    log.exception(
                        "referral.reward.failed", referrer=referrer_id
                    )
        except Exception:
            log.exception("referral.worker.failed")
            await asyncio.sleep(60)


async def _reward_referrer(bot: Bot, referrer_id: int) -> None:
    days = settings.referral_reward_days
    await db.execute(
        """
        INSERT INTO bot_subscriptions(user_id, expires_at, trial, created_at)
        VALUES($1, NOW() + ($2 || ' days')::interval, TRUE, NOW())
        ON CONFLICT(user_id) DO UPDATE SET
            expires_at = GREATEST(
                bot_subscriptions.expires_at, NOW()
            ) + ($2 || ' days')::interval,
            trial = TRUE
        """,
        referrer_id, str(days),
    )
    await db.mark_referral_rewarded(referrer_id)
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 <b>مبروك!</b>\n\n"
            f"تمت مكافأتك بـ <b>{days}</b> أيام مجانية "
            f"بفضل إحالاتك الناجحة.\n\n"
            f"شكرًا لدعمك! 🙏",
            parse_mode="HTML",
        )
    except Exception:
        log.exception("referral.reward.notify.failed")


async def healthcheck_worker(bot: Bot) -> None:
    while True:
        try:
            await asyncio.sleep(60)
            await bot.get_me()
            await db.fetchval("SELECT 1")
        except Exception:
            log.exception("healthcheck.failed")


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="بدء استخدام البوت"),
        BotCommand(command="help", description="المساعدة والأوامر"),
        BotCommand(command="cancel", description="إلغاء العملية الحالية"),
        BotCommand(command="stats", description="إحصائيات (للمالك)"),
        BotCommand(command="pool", description="حالة الاتصالات (للمالك)"),
        BotCommand(
            command="maintenance",
            description="تفعيل/تعطيل الصيانة (للمالك)",
        ),
    ])
    log.info("commands.registered", count=6)


async def notify_boot_success(bot: Bot) -> None:
    cairo_tz = pytz.timezone("Africa/Cairo")
    now_cairo = datetime.now(cairo_tz)
    time_str = now_cairo.strftime("%Y-%m-%d %I:%M:%S %p")

    try:
        me = await bot.get_me()
        bot_display_name = me.full_name or settings.bot_name
        username = me.username or ""
    except Exception:
        bot_display_name = settings.bot_name
        username = ""

    mode_line = (
        "<b>Webhook</b>" if settings.use_webhook else "<b>Polling</b>"
    )
    text = (
        "<blockquote><b>✅ تم تشغيل البوت بنجاح</b></blockquote>\n\n"
        f"🤖 اسم البوت: <b>{safe_html(bot_display_name)}</b>\n"
        f"👤 المالك: <code>{settings.owner_id}</code>\n"
        f"🕐 وقت التشغيل: <b>{time_str} (القاهرة)</b>\n"
        f"💾 قاعدة البيانات: <b>PostgreSQL (Supabase)</b>\n"
        f"🌐 وضع التشغيل: {mode_line}\n"
        f"🛠 وضع الصيانة: "
        f"<b>{'مفعّل ⚠️' if settings.maintenance_mode else 'معطّل ✅'}</b>\n"
        f"🎁 نظام الإحالة: "
        f"<b>{'مفعّل ✅' if settings.referral_enabled else 'معطّل ❌'}</b>\n\n"
        "<blockquote>⚙️ البوت جاهز الآن لاستقبال الرسائل والتحديثات.</blockquote>"
    )

    if username:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"🎛 فتح البوت: {bot_display_name}",
                url=f"https://t.me/{username}",
            )
        ]])
    else:
        kb = None

    try:
        await bot.send_message(
            settings.owner_id,
            text,
            reply_markup=kb,
            parse_mode="HTML",
        )
        log.info("boot.notify.sent", owner=settings.owner_id)
    except Exception:
        log.exception("boot.notify.failed", owner=settings.owner_id)


async def notify_boot_failure(bot: Bot, error: str) -> None:
    cairo_tz = pytz.timezone("Africa/Cairo")
    now_cairo = datetime.now(cairo_tz)
    time_str = now_cairo.strftime("%Y-%m-%d %I:%M:%S %p")
    text = (
        "<blockquote><b>⚠️ فشل تشغيل البوت</b></blockquote>\n\n"
        f"<blockquote><b>🕐 الوقت: <code>{time_str} (القاهرة)</code></b></blockquote>\n"
        f"<blockquote><b>❌ الخطأ:</b>\n<code>{safe_html(error[:800])}</code></blockquote>"
    )
    try:
        await bot.send_message(
            settings.owner_id,
            text,
            parse_mode="HTML",
        )
        log.info("boot.failure.notified", owner=settings.owner_id)
    except Exception:
        log.exception("boot.failure.notify.failed")


ALLOWED_UPDATES = [
    "message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "my_chat_member",
]


async def _register_background_tasks(bot: Bot) -> list[asyncio.Task]:
    tasks = [
        asyncio.create_task(cleanup_worker()),
        asyncio.create_task(stats_worker()),
        asyncio.create_task(live_clock_worker(bot)),
        asyncio.create_task(live_bio_worker(bot)),
        asyncio.create_task(notifications_worker(bot)),
        asyncio.create_task(healthcheck_worker(bot)),
    ]
    if settings.referral_enabled:
        tasks.append(asyncio.create_task(referral_worker(bot)))
    log.info("background.tasks.started", count=len(tasks))
    return tasks


async def run_polling(dp: Dispatcher, bot: Bot) -> None:
    log.info("polling.start")
    await dp.start_polling(
        bot,
        allowed_updates=ALLOWED_UPDATES,
        handle_as_tasks=True,
        tasks_concurrency_limit=settings.max_concurrent_updates,
    )


async def run_webhook(dp: Dispatcher, bot: Bot) -> None:
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import (
        SimpleRequestHandler,
        setup_application,
    )

    full_url = (
        settings.webhook_url.rstrip("/") + settings.webhook_path
    )
    await bot.set_webhook(
        url=full_url,
        secret_token=settings.webhook_secret,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=False,
    )
    log.info(
        "webhook.set",
        url=full_url,
        host=settings.webapp_host,
        port=settings.webapp_port,
    )

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner, host=settings.webapp_host, port=settings.webapp_port
    )
    await site.start()
    log.info("webhook.server.started")

    try:
        await asyncio.Event().wait()
    finally:
        try:
            await runner.cleanup()
        except Exception:
            log.exception("webhook.runner.cleanup.failed")


async def main() -> None:
    setup_logging()
    log.info(
        "boot.start",
        bot_name=settings.bot_name,
        owner_id=settings.owner_id,
        log_level=settings.log_level,
        webhook=settings.use_webhook,
        maintenance=settings.maintenance_mode,
        referral=settings.referral_enabled,
    )

    try:
        await db.connect()
        log.info("database.connected")
        await init_schema()
        log.info("database.schema.ready")
    except Exception:
        log.exception("database.init.failed")
        raise

    try:
        session = AiohttpSession(timeout=180)
        bot = Bot(
            token=settings.bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        bot.session.middleware.register(bold_outgoing_text_middleware)
        bot.session.middleware.register(rich_outgoing_middleware)
        me = await bot.get_me()
        log.info("bot.initialized", username=me.username, bot_id=me.id)
    except Exception:
        log.exception("bot.init.failed")
        raise

    storage = (
        RedisStorage.from_url(settings.redis_url)
        if settings.redis_url
        else MemoryStorage()
    )
    dp = Dispatcher(storage=storage)
    dp.update.outer_middleware(ErrorMiddleware())
    dp.update.outer_middleware(MaintenanceMiddleware())
    dp.update.outer_middleware(ReferralMiddleware())
    dp.update.outer_middleware(ThrottlingMiddleware(
        rate=settings.rate_limit_per_user,
        window=settings.rate_limit_window,
    ))
    log.info(
        "dispatcher.ready",
        rate_limit=settings.rate_limit_per_user,
        window=settings.rate_limit_window,
    )

    try:
        register_all(dp)
        log.info("plugins.registered")
    except Exception:
        log.exception("plugins.register.failed")
        raise

    background_tasks = await _register_background_tasks(bot)

    await setup_commands(bot)
    await notify_boot_success(bot)

    try:
        if settings.use_webhook and settings.webhook_url:
            await run_webhook(dp, bot)
        else:
            await run_polling(dp, bot)
    except Exception as exc:
        log.exception("runtime.failed")
        try:
            await notify_boot_failure(bot, str(exc))
        except Exception:
            pass
        raise
    finally:
        log.info("shutdown.start")
        for task in background_tasks:
            task.cancel()
        try:
            await asyncio.gather(
                *background_tasks, return_exceptions=True
            )
        except Exception:
            log.exception("background.cancel.failed")

        if settings.use_webhook:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                log.info("webhook.deleted")
            except Exception:
                log.exception("webhook.delete.failed")

        try:
            await db.close()
            log.info("database.closed")
        except Exception:
            log.exception("database.close.failed")
        try:
            await bot.session.close()
            log.info("bot.session.closed")
        except Exception:
            log.exception("bot.session.close.failed")
        try:
            await dp.storage.close()
            log.info("dispatcher.storage.closed")
        except Exception:
            log.exception("dispatcher.storage.close.failed")
        log.info("shutdown.done")


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        print('{"event": "uvloop.enabled", "level": "info"}')
    except ImportError:
        pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('{"event": "shutdown.keyboard_interrupt", "level": "info"}')
    except Exception as exc:
        print(f'{{"event": "fatal.error", "error": "{exc}", "level": "error"}}')
        sys.exit(1)
