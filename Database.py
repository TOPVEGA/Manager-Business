# الترخيص: @TopVeGa
# العلامة التجارية: VEGA & Logic
# حقوق الطبع والنشر: © 2015 - 2025


from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, AsyncIterator, Optional

import structlog
from cachetools import TTLCache

from config import settings

log = structlog.get_logger(__name__)

TS_FORMAT = "%Y-%m-%d %H:%M:%f+00:00"
NOW_SQL = "strftime('" + TS_FORMAT + "','now')"

_TX_ACTIVE: ContextVar[bool] = ContextVar("db_tx_active", default=False)


def _adapt_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(
        sep=" ", timespec="milliseconds"
    )


def _adapt_date(value: date) -> str:
    return value.isoformat()


def _adapt_decimal(value: Decimal) -> float:
    return float(value)


def _convert_timestamp(raw: bytes) -> Any:
    text = raw.decode("utf-8")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return text
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _convert_boolean(raw: bytes) -> Any:
    text = raw.decode("utf-8").strip().lower()
    if text in ("1", "true", "t"):
        return True
    if text in ("0", "false", "f", ""):
        return False
    return bool(text)


def _convert_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw.decode("utf-8")


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_adapter(date, _adapt_date)
sqlite3.register_adapter(Decimal, _adapt_decimal)
sqlite3.register_converter("TIMESTAMPTZ", _convert_timestamp)
sqlite3.register_converter("BOOLEAN", _convert_boolean)
sqlite3.register_converter("JSONB", _convert_json)


def _bind(args: Any) -> dict:
    return {f"p{i}": _adapt_arg(v) for i, v in enumerate(args, 1)}


def _adapt_arg(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset)):
        return json.dumps(list(value), ensure_ascii=False, default=str)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


_RE_GREATEST_INTERVAL = re.compile(
    r"GREATEST\(\s*(.+?)\s*,\s*NOW\(\)\s*\)\s*\+\s*"
    r"\(\s*\$(\d+)\s*\|\|\s*'\s*(\w+)\s*'\s*\)\s*::\s*interval",
    re.IGNORECASE | re.DOTALL,
)
_RE_NOW_PLUS_PARAM = re.compile(
    r"NOW\(\)\s*\+\s*\(\s*\$(\d+)\s*\|\|\s*'\s*(\w+)\s*'\s*\)"
    r"\s*::\s*interval",
    re.IGNORECASE,
)
_RE_NOW_INTERVAL = re.compile(
    r"NOW\(\)\s*([+-])\s*INTERVAL\s*'\s*(\d+)\s*(\w+)\s*'",
    re.IGNORECASE,
)
_RE_NOW = re.compile(r"\bNOW\(\)", re.IGNORECASE)
_RE_ANY = re.compile(
    r"=\s*ANY\(\s*\$(\d+)\s*(?:::\s*\w+(?:\[\])?)?\s*\)",
    re.IGNORECASE,
)
_RE_CAST = re.compile(
    r"::\s*(?:text|int|integer|bigint|smallint|numeric|float|double|jsonb|json|"
    r"boolean|bool|timestamptz|timestamp|date|interval|varchar)(?:\[\])?",
    re.IGNORECASE,
)
_RE_PARAM = re.compile(r"\$(\d+)")
_RE_TRUNCATE = re.compile(
    r"^\s*TRUNCATE\s+TABLE\s+\"?(\w+)\"?.*$", re.IGNORECASE | re.DOTALL
)
_RE_INFO_SCHEMA = re.compile(r"information_schema\.columns", re.IGNORECASE)

_INFO_SCHEMA_SQL = (
    "SELECT name AS column_name, CASE UPPER(type) "
    "WHEN 'TIMESTAMPTZ' THEN 'timestamp with time zone' "
    "WHEN 'DATE' THEN 'date' ELSE LOWER(type) END AS data_type "
    "FROM pragma_table_info(:p1)"
)


@lru_cache(maxsize=4096)
def _translate(query: str) -> str:
    if _RE_INFO_SCHEMA.search(query):
        return _INFO_SCHEMA_SQL
    sql = _RE_GREATEST_INTERVAL.sub(
        lambda m: (
            f"strftime('{TS_FORMAT}', MAX({m.group(1)}, {NOW_SQL}), "
            f"(${m.group(2)} || ' {m.group(3)}'))"
        ),
        query,
    )
    sql = _RE_NOW_PLUS_PARAM.sub(
        lambda m: (
            f"strftime('{TS_FORMAT}','now',(${m.group(1)} || ' {m.group(2)}'))"
        ),
        sql,
    )
    sql = _RE_NOW_INTERVAL.sub(
        lambda m: (
            f"strftime('{TS_FORMAT}','now','{m.group(1)}{m.group(2)} "
            f"{m.group(3)}')"
        ),
        sql,
    )
    sql = _RE_NOW.sub(NOW_SQL, sql)
    sql = _RE_ANY.sub(
        lambda m: f"IN (SELECT value FROM json_each(${m.group(1)}))", sql
    )
    sql = _RE_CAST.sub("", sql)
    return _RE_PARAM.sub(lambda m: ":p" + m.group(1), sql)


class Record:
    __slots__ = ("_keys", "_index", "_values")

    def __init__(
        self, keys: tuple, index: dict, values: tuple
    ) -> None:
        self._keys = keys
        self._index = index
        self._values = values

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def keys(self):
        return iter(self._keys)

    def values(self):
        return iter(self._values)

    def items(self):
        return iter(zip(self._keys, self._values))

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: Any) -> bool:
        return key in self._index

    def __repr__(self) -> str:
        body = ", ".join(f"{k}={v!r}" for k, v in self.items())
        return f"<Record {body}>"


class _Conn:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

    def _run_execute(self, query: str, args: tuple) -> str:
        match = _RE_TRUNCATE.match(query)
        if match:
            table = match.group(1)
            self._raw.execute(f'DELETE FROM "{table}"')
            try:
                self._raw.execute(
                    "DELETE FROM sqlite_sequence WHERE name=?", (table,)
                )
            except sqlite3.OperationalError:
                pass
            return "TRUNCATE TABLE"
        sql = _translate(query)
        cur = self._raw.execute(sql, _bind(args))
        if cur.description is not None:
            count = len(cur.fetchall())
        else:
            count = cur.rowcount
        verb = sql.lstrip().split(None, 1)[0].upper()
        if verb == "INSERT":
            return f"INSERT 0 {count}"
        if verb in ("UPDATE", "DELETE"):
            return f"{verb} {count}"
        return verb

    def _run_fetch(self, query: str, args: tuple) -> list:
        sql = _translate(query)
        cur = self._raw.execute(sql, _bind(args))
        rows = cur.fetchall()
        if cur.description is None:
            return []
        keys = tuple(d[0] for d in cur.description)
        index = {k: i for i, k in enumerate(keys)}
        return [Record(keys, index, tuple(row)) for row in rows]

    def _run_many(self, query: str, records: list) -> None:
        sql = _translate(query)
        prepared = [_bind(rec) for rec in records]
        self._raw.executemany(sql, prepared)

    def _run_script(self, script: str) -> None:
        self._raw.executescript(script)

    def _run_control(self, statement: str) -> None:
        self._raw.execute(statement)

    async def execute(self, query: str, *args: Any) -> str:
        return await asyncio.to_thread(self._run_execute, query, args)

    async def fetch(self, query: str, *args: Any) -> list[Record]:
        return await asyncio.to_thread(self._run_fetch, query, args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[Record]:
        rows = await asyncio.to_thread(self._run_fetch, query, args)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        rows = await asyncio.to_thread(self._run_fetch, query, args)
        if not rows or len(rows[0]) == 0:
            return None
        return rows[0][0]

    async def executemany(self, query: str, records: Any) -> None:
        await asyncio.to_thread(self._run_many, query, list(records))

    async def executescript(self, script: str) -> None:
        await asyncio.to_thread(self._run_script, script)

    async def begin(self) -> None:
        await asyncio.to_thread(self._run_control, "BEGIN IMMEDIATE")

    async def commit(self) -> None:
        await asyncio.to_thread(self._run_control, "COMMIT")

    async def rollback(self) -> None:
        try:
            await asyncio.to_thread(self._run_control, "ROLLBACK")
        except sqlite3.OperationalError:
            pass

    async def close(self) -> None:
        await asyncio.to_thread(self._raw.close)


class _Pool:
    def __init__(self, conn: _Conn, lock: asyncio.Lock) -> None:
        self.conn = conn
        self._lock = lock

    def get_size(self) -> int:
        return 1

    def get_idle_size(self) -> int:
        return 0 if self._lock.locked() else 1

    def get_max_size(self) -> int:
        return 1

    def get_min_size(self) -> int:
        return 1

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        if _TX_ACTIVE.get():
            yield self.conn
            return
        async with self._lock:
            yield self.conn

    async def close(self) -> None:
        await self.conn.close()


def _database_path() -> str:
    url = (settings.database_url or "").strip()
    for prefix in ("sqlite:///", "sqlite://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    if not url or url.startswith(("postgres://", "postgresql://")):
        url = "manager.db"
    if url == ":memory:" or os.path.isabs(url):
        return url
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, url)


def _open_connection(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
    raw = sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
        check_same_thread=False,
        timeout=float(settings.db_command_timeout),
    )
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA synchronous=NORMAL")
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute(f"PRAGMA busy_timeout={int(settings.db_command_timeout) * 1000}")
    return raw


class Database:
    def __init__(self) -> None:
        self._pool: Optional[_Pool] = None
        self._lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()
        self._settings_cache: TTLCache = TTLCache(
            maxsize=5000, ttl=settings.settings_cache_ttl
        )
        self._perm_cache: TTLCache = TTLCache(maxsize=10000, ttl=300)

    async def connect(self) -> None:
        async with self._lock:
            if self._pool is not None:
                return
            path = _database_path()
            raw = await asyncio.to_thread(_open_connection, path)
            self._pool = _Pool(_Conn(raw), self._io_lock)
            log.info("database.sqlite.opened", path=path)

    async def close(self) -> None:
        async with self._lock:
            if self._pool:
                async with self._io_lock:
                    await self._pool.close()
                self._pool = None
                log.info("database.sqlite.closed")

    @property
    def pool(self) -> _Pool:
        if self._pool is None:
            raise RuntimeError("Database is not initialized.")
        return self._pool

    async def _call(self, name: str, query: str, args: tuple) -> Any:
        conn = self.pool.conn
        if _TX_ACTIVE.get():
            return await getattr(conn, name)(query, *args)
        async with self._io_lock:
            return await getattr(conn, name)(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        return await self._call("execute", query, args)

    async def fetch(self, query: str, *args: Any) -> list[Record]:
        return await self._call("fetch", query, args)

    async def fetchrow(
        self, query: str, *args: Any
    ) -> Optional[Record]:
        return await self._call("fetchrow", query, args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._call("fetchval", query, args)

    async def executescript(self, script: str) -> None:
        conn = self.pool.conn
        if _TX_ACTIVE.get():
            await conn.executescript(script)
            return
        async with self._io_lock:
            await conn.executescript(script)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_Conn]:
        conn = self.pool.conn
        if _TX_ACTIVE.get():
            yield conn
            return
        async with self._io_lock:
            token = _TX_ACTIVE.set(True)
            await conn.begin()
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
            finally:
                _TX_ACTIVE.reset(token)

    def cache_get(self, key: str) -> Any:
        return self._settings_cache.get(key)

    def cache_set(self, key: str, value: Any) -> None:
        self._settings_cache[key] = value

    def cache_invalidate(self, prefix: str = "") -> None:
        if not prefix:
            self._settings_cache.clear()
            return
        for k in list(self._settings_cache.keys()):
            if k.startswith(prefix):
                self._settings_cache.pop(k, None)

    def perm_cache_get(self, key: str) -> Any:
        return self._perm_cache.get(key)

    def perm_cache_set(self, key: str, value: Any) -> None:
        self._perm_cache[key] = value

    def perm_cache_invalidate(self, user_id: int) -> None:
        self._perm_cache.pop(f"perm:{user_id}", None)

    async def push_notification(
        self, owner_id: int, notif_type: str, payload: dict
    ) -> None:
        try:
            await self.execute(
                """
                INSERT INTO admin_notifications(owner_id, notif_type, payload)
                VALUES($1, $2, $3)
                """,
                owner_id, notif_type,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            log.exception("push_notification.failed", notif_type=notif_type)

    async def fetch_unread_notifications(
        self, owner_id: int, limit: int = 50
    ) -> list[Record]:
        return await self.fetch(
            """
            SELECT id, notif_type, payload, created_at
            FROM admin_notifications
            WHERE owner_id=$1 AND read=FALSE
            ORDER BY id
            LIMIT $2
            """,
            owner_id, limit,
        )

    async def mark_notification_read(self, notif_id: int) -> None:
        await self.execute(
            "UPDATE admin_notifications SET read=TRUE WHERE id=$1",
            notif_id,
        )


db = Database()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    owner_id BIGINT PRIMARY KEY,
    enabled BOOLEAN DEFAULT TRUE,
    welcome TEXT DEFAULT '',
    welcome_enabled BOOLEAN DEFAULT TRUE,
    welcome_mode TEXT DEFAULT 'always',
    welcome_sticker_file_id TEXT DEFAULT '',
    welcome_image_file_id TEXT DEFAULT '',
    welcome_image_type TEXT DEFAULT '',
    media_file_id TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    dashboard_video_file_id TEXT DEFAULT '',
    dashboard_video_type TEXT DEFAULT '',
    bot_welcome_image_file_id TEXT DEFAULT '',
    bot_welcome_image_type TEXT DEFAULT '',
    social_enabled BOOLEAN DEFAULT TRUE,
    social_tiktok TEXT DEFAULT '',
    social_instagram TEXT DEFAULT '',
    social_telegram TEXT DEFAULT '',
    social_facebook TEXT DEFAULT '',
    social_youtube TEXT DEFAULT '',
    media_enabled BOOLEAN DEFAULT TRUE,
    media_delete_types JSONB DEFAULT '[]',
    track_deleted_enabled BOOLEAN DEFAULT TRUE,
    track_edited_enabled BOOLEAN DEFAULT TRUE,
    transfer_enabled BOOLEAN DEFAULT TRUE,
    calculator_enabled BOOLEAN DEFAULT TRUE,
    currency_enabled BOOLEAN DEFAULT TRUE,
    crypto_enabled BOOLEAN DEFAULT TRUE,
    crypto_alert_ton_usd NUMERIC DEFAULT 0,
    crypto_alert_usd_egp NUMERIC DEFAULT 0,
    live_clock_enabled BOOLEAN DEFAULT FALSE,
    live_clock_style INT DEFAULT 0,
    live_bio_enabled BOOLEAN DEFAULT FALSE,
    live_bio_religion TEXT DEFAULT '',
    live_bio_verses JSONB DEFAULT '[]',
    live_bio_custom_verses JSONB DEFAULT '{}',
    live_bio_custom_texts JSONB DEFAULT '[]',
    live_bio_mode TEXT DEFAULT 'custom',
    live_bio_mode_migrated BOOLEAN DEFAULT FALSE,
    live_bio_interval_minutes INT DEFAULT 360,
    live_bio_custom_interval_minutes INT DEFAULT 360,
    live_bio_religious_interval_minutes INT DEFAULT 360,
    live_bio_last_updated BIGINT DEFAULT 0,
    live_bio_index INT DEFAULT 0,
    bot_force_enabled BOOLEAN DEFAULT TRUE,
    bot_access_mode TEXT DEFAULT 'free',
    bot_trial_hours INT DEFAULT 24,
    account_force_enabled BOOLEAN DEFAULT TRUE,
    replies_enabled BOOLEAN DEFAULT TRUE,
    words_enabled BOOLEAN DEFAULT TRUE,
    muted_enabled BOOLEAN DEFAULT TRUE,
    payment_enabled BOOLEAN DEFAULT TRUE,
    broadcast_enabled BOOLEAN DEFAULT TRUE,
    maintenance_enabled BOOLEAN DEFAULT FALSE,
    maintenance_message TEXT DEFAULT '',
    speech_enabled BOOLEAN DEFAULT TRUE,
    youtube_enabled BOOLEAN DEFAULT TRUE,
    font_style INT DEFAULT 0,
    bot_log_channel BIGINT DEFAULT 0,
    pv_log_enabled BOOLEAN DEFAULT TRUE,
    pv_log_target BIGINT DEFAULT 0,
    swear_filter_enabled BOOLEAN DEFAULT TRUE,
    swear_auto_mute BOOLEAN DEFAULT TRUE,
    swear_mute_after INT DEFAULT 3,
    swear_notice_text TEXT DEFAULT '🚫 ممنوع الشتيمة والسب في هذا الحساب.',
    created_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    updated_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);

CREATE TABLE IF NOT EXISTS business (
    connection_id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    can_reply BOOLEAN DEFAULT FALSE,
    can_edit_name BOOLEAN DEFAULT FALSE,
    can_edit_bio BOOLEAN DEFAULT FALSE,
    original_name TEXT DEFAULT '',
    original_last_name TEXT,
    clock_enabled BOOLEAN DEFAULT FALSE,
    clock_style INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_business_user ON business(user_id) WHERE enabled=TRUE;

CREATE TABLE IF NOT EXISTS business_profile_snapshots (
    connection_id TEXT PRIMARY KEY
        REFERENCES business(connection_id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    original_first_name TEXT NOT NULL DEFAULT '',
    original_last_name TEXT NOT NULL DEFAULT '',
    original_bio TEXT,
    bio_captured BOOLEAN NOT NULL DEFAULT FALSE,
    original_photo_file_id TEXT,
    photo_captured BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_business_profile_snapshots_owner
    ON business_profile_snapshots(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS payment_wallets (
    owner_id BIGINT NOT NULL,
    wallet_key TEXT NOT NULL,
    wallet_value TEXT NOT NULL DEFAULT '',
    network TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    PRIMARY KEY (owner_id, wallet_key)
);

CREATE TABLE IF NOT EXISTS customers (
    connection_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    first_name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    last_seen TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    last_welcome TIMESTAMPTZ,
    last_subscription_prompt_id BIGINT DEFAULT 0,
    PRIMARY KEY (connection_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_customers_last_seen ON customers(last_seen DESC);

CREATE TABLE IF NOT EXISTS bot_users (
    user_number INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id BIGINT UNIQUE NOT NULL,
    first_name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    first_seen TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    blocked BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_bot_users_blocked ON bot_users(blocked) WHERE blocked=TRUE;

CREATE TABLE IF NOT EXISTS bot_subscriptions (
    user_id BIGINT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    trial BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_subs_expires ON bot_subscriptions(expires_at);

CREATE TABLE IF NOT EXISTS bot_channels (
    chat_id BIGINT PRIMARY KEY,
    title TEXT DEFAULT '',
    username TEXT DEFAULT '',
    added_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);

CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id BIGINT NOT NULL,
    trigger TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_replies_owner ON replies(owner_id);
CREATE INDEX IF NOT EXISTS idx_replies_trigger ON replies(owner_id, LOWER(trigger));

CREATE TABLE IF NOT EXISTS reply_buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reply_id BIGINT NOT NULL REFERENCES replies(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    btn_type TEXT NOT NULL,
    btn_color TEXT DEFAULT 'primary',
    icon_custom_emoji_id TEXT DEFAULT '',
    btn_value TEXT NOT NULL,
    position INT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reply_buttons_reply ON reply_buttons(reply_id, position);

CREATE TABLE IF NOT EXISTS banned_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id BIGINT NOT NULL,
    word TEXT NOT NULL,
    pattern TEXT DEFAULT '',
    is_regex BOOLEAN DEFAULT FALSE,
    kind TEXT NOT NULL DEFAULT 'banned'
);
CREATE INDEX IF NOT EXISTS idx_banned_owner ON banned_words(owner_id);

CREATE TABLE IF NOT EXISTS muted (
    connection_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    muted_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    PRIMARY KEY (connection_id, chat_id)
);

CREATE TABLE IF NOT EXISTS violations (
    connection_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    owner_id BIGINT NOT NULL,
    count INT DEFAULT 0,
    last_word TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    PRIMARY KEY (connection_id, chat_id)
);

CREATE TABLE IF NOT EXISTS force_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id BIGINT NOT NULL,
    channel TEXT NOT NULL,
    chat_id BIGINT DEFAULT 0,
    title TEXT DEFAULT '',
    scope TEXT DEFAULT 'account'
);
CREATE INDEX IF NOT EXISTS idx_force_owner_scope ON force_channels(owner_id, scope);

CREATE TABLE IF NOT EXISTS admins (
    user_id BIGINT PRIMARY KEY,
    added_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);

CREATE TABLE IF NOT EXISTS bot_messages (
    message_key TEXT PRIMARY KEY,
    text TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS account_messages (
    owner_id BIGINT NOT NULL,
    message_key TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner_id, message_key)
);

CREATE TABLE IF NOT EXISTS tracking_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id BIGINT NOT NULL,
    connection_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    old_text TEXT DEFAULT '',
    new_text TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    media_file_id TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_tracking_owner ON tracking_logs(owner_id, event_type, id DESC);

CREATE TABLE IF NOT EXISTS tracked_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    sender_id BIGINT,
    sender_name TEXT DEFAULT '',
    sender_username TEXT DEFAULT '',
    text_content TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    media_file_id TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    UNIQUE (connection_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_lookup ON tracked_messages(connection_id, chat_id, message_id);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id BIGINT NOT NULL,
    referred_id BIGINT NOT NULL UNIQUE,
    rewarded BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);

CREATE TABLE IF NOT EXISTS admin_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id BIGINT NOT NULL,
    notif_type TEXT NOT NULL,
    payload JSONB DEFAULT '{}',
    read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON admin_notifications(owner_id, id DESC) WHERE read=FALSE;

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    meta JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor_id, id DESC);

CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id BIGINT NOT NULL,
    scope TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    run_at TIMESTAMPTZ NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_sched_pending ON scheduled_broadcasts(status, run_at);

CREATE TABLE IF NOT EXISTS crypto_price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ton_usd NUMERIC NOT NULL,
    usd_egp NUMERIC NOT NULL,
    ton_egp NUMERIC NOT NULL,
    gram_usd NUMERIC DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS idx_crypto_snap ON crypto_price_snapshots(created_at DESC);
"""

_SETTINGS_SCHEMA_SQL = SCHEMA_SQL.split(
    "CREATE TABLE IF NOT EXISTS business", 1
)[0]
SETTINGS_COLUMNS = frozenset(
    re.findall(
        r"^\s+([a-z][a-z0-9_]*)\s+(?:BIGINT|BOOLEAN|TEXT|INT|JSONB|TIMESTAMPTZ|NUMERIC)\b",
        _SETTINGS_SCHEMA_SQL,
        flags=re.MULTILINE,
    )
)


MIGRATION_SQL = """
ALTER TABLE settings ADD COLUMN IF NOT EXISTS media_file_id TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS media_type TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS dashboard_video_file_id TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS dashboard_video_type TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS bot_welcome_image_file_id TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS bot_welcome_image_type TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS welcome_image_type TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS welcome_mode TEXT DEFAULT 'always';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_verses JSONB DEFAULT '[]';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_mode_migrated BOOLEAN DEFAULT FALSE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_religion TEXT DEFAULT '';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_custom_verses JSONB DEFAULT '{}';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_custom_texts JSONB DEFAULT '[]';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_mode TEXT DEFAULT 'custom';
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_interval_minutes INT DEFAULT 360;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_custom_interval_minutes INT DEFAULT 360;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_religious_interval_minutes INT DEFAULT 360;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_last_updated BIGINT DEFAULT 0;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS live_bio_index INT DEFAULT 0;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS maintenance_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS maintenance_message TEXT DEFAULT '';
ALTER TABLE business ADD COLUMN IF NOT EXISTS can_edit_bio BOOLEAN DEFAULT FALSE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS speech_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS youtube_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS font_style INT DEFAULT 0;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS bot_log_channel BIGINT DEFAULT 0;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS pv_log_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS pv_log_target BIGINT DEFAULT 0;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS crypto_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS crypto_alert_ton_usd NUMERIC DEFAULT 0;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS crypto_alert_usd_egp NUMERIC DEFAULT 0;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS swear_filter_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS swear_auto_mute BOOLEAN DEFAULT TRUE;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS swear_mute_after INT DEFAULT 3;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS swear_notice_text TEXT DEFAULT '🚫 ممنوع الشتيمة والسب في هذا الحساب.';
ALTER TABLE business_profile_snapshots ADD COLUMN IF NOT EXISTS original_photo_file_id TEXT;
ALTER TABLE business_profile_snapshots ADD COLUMN IF NOT EXISTS photo_captured BOOLEAN DEFAULT FALSE;
ALTER TABLE force_channels ADD COLUMN IF NOT EXISTS chat_id BIGINT DEFAULT 0;
ALTER TABLE banned_words ADD COLUMN IF NOT EXISTS pattern TEXT DEFAULT '';
ALTER TABLE banned_words ADD COLUMN IF NOT EXISTS is_regex BOOLEAN DEFAULT FALSE;
ALTER TABLE banned_words ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'banned';
"""


_MIGRATION_RE = re.compile(
    r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) ([^;]+);"
)


async def init_schema() -> None:
    await db.execute("DROP INDEX IF EXISTS idx_banned_kind")
    await db.executescript(SCHEMA_SQL)
    for table, column, definition in _MIGRATION_RE.findall(MIGRATION_SQL):
        existing = {
            r["name"]
            for r in await db.fetch(
                "SELECT name FROM pragma_table_info($1)", table
            )
        }
        if column not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition.strip()}"
            )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_banned_kind "
        "ON banned_words(owner_id, kind)"
    )
    log.info("database.schema.initialized")


async def force_schema_migration() -> None:
    await init_schema()
    log.info("database.schema.migration_forced")


async def get_setting(key: str, owner_id: int) -> Any:
    if key not in SETTINGS_COLUMNS:
        raise ValueError(f"Unknown settings column: {key!r}")
    cache_key = f"setting:{owner_id}:{key}"
    cached = db.cache_get(cache_key)
    if cached is not None:
        return cached
    row = await db.fetchrow(
        f"SELECT {key} FROM settings WHERE owner_id = $1", owner_id
    )
    value = row[key] if row else None
    if value is not None:
        db.cache_set(cache_key, value)
    return value


async def set_setting(key: str, value: Any, owner_id: int) -> None:
    if key not in SETTINGS_COLUMNS:
        raise ValueError(f"Unknown settings column: {key!r}")
    await db.execute(
        f"UPDATE settings SET {key} = $1 WHERE owner_id = $2",
        value, owner_id,
    )
    db.cache_invalidate(f"setting:{owner_id}:")


async def ensure_settings(owner_id: int) -> None:
    await db.execute(
        "INSERT INTO settings(owner_id) VALUES($1) "
        "ON CONFLICT(owner_id) DO NOTHING",
        owner_id,
    )


async def ensure_account_settings(owner_id: int) -> None:
    await ensure_settings(owner_id)


async def is_admin(user_id: int) -> bool:
    cached = db.perm_cache_get(f"perm:{user_id}")
    if cached is not None:
        return cached
    row = await db.fetchrow(
        """
        SELECT 1 FROM business WHERE user_id=$1 AND enabled=TRUE
        UNION ALL
        SELECT 1 FROM admins WHERE user_id=$2
        LIMIT 1
        """,
        user_id, user_id,
    )
    result = bool(row)
    if not result:
        result = user_id == settings.owner_id
    db.perm_cache_set(f"perm:{user_id}", result)
    return result


async def is_account_linked(user_id: int) -> bool:
    row = await db.fetchrow(
        "SELECT 1 FROM business WHERE user_id=$1 AND enabled=TRUE LIMIT 1",
        user_id,
    )
    return bool(row)


async def settings_owner(user_id: int) -> int:
    if await is_account_linked(user_id):
        return user_id
    return settings.owner_id


async def get_account_setting(owner_id: int, key: str) -> Any:
    return await get_setting(key, owner_id)


async def set_account_setting(owner_id: int, key: str, value: Any) -> None:
    await ensure_account_settings(owner_id)
    await set_setting(key, value, owner_id)


async def get_account_message(owner_id: int, key: str) -> str:
    row = await db.fetchrow(
        "SELECT text FROM account_messages "
        "WHERE owner_id=$1 AND message_key=$2",
        owner_id, key,
    )
    if row and row["text"]:
        return row["text"]
    row = await db.fetchrow(
        "SELECT text FROM bot_messages WHERE message_key=$1", key
    )
    return row["text"] if row else ""


async def set_account_message(owner_id: int, key: str, text: str) -> None:
    await db.execute(
        """INSERT INTO account_messages(owner_id, message_key, text)
           VALUES($1, $2, $3)
           ON CONFLICT(owner_id, message_key) DO UPDATE SET text=EXCLUDED.text""",
        owner_id, key, text,
    )


async def get_bot_message(key: str) -> str:
    row = await db.fetchrow(
        "SELECT text FROM bot_messages WHERE message_key=$1", key
    )
    return row["text"] if row and row["text"] else ""


async def set_bot_message(key: str, text: str) -> None:
    await db.execute(
        """INSERT INTO bot_messages(message_key, text) VALUES($1, $2)
           ON CONFLICT(message_key) DO UPDATE SET text=EXCLUDED.text""",
        key, text,
    )


async def is_bot_user_blocked(user_id: int) -> bool:
    row = await db.fetchrow(
        "SELECT blocked FROM bot_users WHERE user_id=$1", user_id
    )
    return bool(row and row["blocked"])


async def audit(
    actor_id: int, action: str, target: str = "", **meta
) -> None:
    try:
        await db.execute(
            "INSERT INTO audit_logs(actor_id, action, target, meta) "
            "VALUES($1,$2,$3,$4)",
            actor_id, action, target,
            json.dumps(meta, ensure_ascii=False),
        )
    except Exception:
        log.exception("audit.failed", action=action)


async def save_crypto_snapshot(
    ton_usd: float, usd_egp: float, ton_egp: float, gram_usd: float = 0,
) -> None:
    try:
        await db.execute(
            "INSERT INTO crypto_price_snapshots"
            "(ton_usd, usd_egp, ton_egp, gram_usd) "
            "VALUES($1,$2,$3,$4)",
            ton_usd, usd_egp, ton_egp, gram_usd,
        )
    except Exception:
        log.exception("crypto.snapshot.failed")


async def get_crypto_history(limit: int = 20) -> list[Record]:
    return await db.fetch(
        "SELECT * FROM crypto_price_snapshots "
        "ORDER BY id DESC LIMIT $1",
        limit,
    )


async def get_live_bio_data(owner_id: int) -> dict:
    await ensure_settings(owner_id)
    row = await db.fetchrow(
        "SELECT live_bio_enabled, live_bio_religion, live_bio_verses, "
        "live_bio_custom_verses, live_bio_custom_texts, live_bio_mode, "
        "live_bio_interval_minutes, live_bio_custom_interval_minutes, "
        "live_bio_religious_interval_minutes, live_bio_last_updated, "
        "live_bio_index FROM settings WHERE owner_id=$1",
        owner_id,
    )
    if not row:
        return {
            "enabled": False, "religion": "", "verses": [],
            "religious_verses": [], "custom_verses": [],
            "custom_by_religion": {}, "custom_texts": [],
            "mode": "custom",
            "interval_minutes": 360,
            "custom_interval_minutes": 360,
            "religious_interval_minutes": 360,
            "last_updated": 0, "index": 0,
        }
    custom_map = row["live_bio_custom_verses"] or {}
    if not isinstance(custom_map, dict):
        custom_map = {}
    custom_map = {
        k: [str(v).strip() for v in custom_map.get(k, []) if str(v).strip()]
        for k in ("islam", "christian")
        if isinstance(custom_map.get(k, []), list)
    }
    custom_texts = row["live_bio_custom_texts"] or []
    if not isinstance(custom_texts, list):
        custom_texts = []
    custom_texts = [str(v).strip() for v in custom_texts if str(v).strip()]
    religion = (row["live_bio_religion"] or "").strip()
    return {
        "enabled": bool(row["live_bio_enabled"]),
        "religion": religion,
        "verses": row["live_bio_verses"] or [],
        "religious_verses": row["live_bio_verses"] or [],
        "custom_verses": custom_map.get(religion, []),
        "custom_by_religion": custom_map,
        "custom_texts": custom_texts,
        "mode": row["live_bio_mode"] or "custom",
        "interval_minutes": int(row["live_bio_interval_minutes"] or 360),
        "custom_interval_minutes": int(
            row["live_bio_custom_interval_minutes"] or 360
        ),
        "religious_interval_minutes": int(
            row["live_bio_religious_interval_minutes"] or 360
        ),
        "last_updated": int(row["live_bio_last_updated"] or 0),
        "index": int(row["live_bio_index"] or 0),
    }


async def set_live_bio_data(owner_id: int, **fields) -> None:
    await ensure_settings(owner_id)
    assignments: list[str] = []
    values: list = []

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
        add("live_bio_mode_migrated", True)
    if fields.get("interval_minutes") is not None:
        col = (
            "live_bio_religious_interval_minutes"
            if fields.get("interval_mode") == "religious"
            else "live_bio_custom_interval_minutes"
        )
        add(col, int(fields["interval_minutes"]))
        add("live_bio_interval_minutes", int(fields["interval_minutes"]))
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


async def get_customer_stats(
    connection_id: str, chat_id: int
) -> Optional[dict]:
    row = await db.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM tracked_messages
             WHERE connection_id=$1 AND chat_id=$2) AS total_messages,
            (SELECT COUNT(*) FROM muted
             WHERE connection_id=$1 AND chat_id=$2) AS mute_count,
            (SELECT last_word FROM violations
             WHERE connection_id=$1 AND chat_id=$2) AS last_banned_word,
            (SELECT count FROM violations
             WHERE connection_id=$1 AND chat_id=$2) AS violations_count,
            (SELECT first_seen FROM customers
             WHERE connection_id=$1 AND chat_id=$2) AS first_seen,
            (SELECT last_seen FROM customers
             WHERE connection_id=$1 AND chat_id=$2) AS last_seen
        """,
        connection_id, chat_id,
    )
    if not row:
        return None
    return dict(row)


async def register_referral(referrer_id: int, referred_id: int) -> bool:
    if referrer_id == referred_id:
        return False
    exists = await db.fetchval(
        "SELECT 1 FROM bot_users WHERE user_id=$1", referrer_id
    )
    if not exists:
        return False
    result = await db.execute(
        """
        INSERT INTO referrals(referrer_id, referred_id)
        VALUES($1, $2)
        ON CONFLICT(referred_id) DO NOTHING
        """,
        referrer_id, referred_id,
    )
    return result == "INSERT 0 1"


async def count_referrals(referrer_id: int) -> int:
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1",
            referrer_id,
        ) or 0
    )


async def get_pending_referral_rewards(target: int) -> list[Record]:
    return await db.fetch(
        """
        SELECT referrer_id, COUNT(*) AS cnt
        FROM referrals
        WHERE rewarded = FALSE
        GROUP BY referrer_id
        HAVING COUNT(*) >= $1
        """,
        target,
    )


async def mark_referral_rewarded(referrer_id: int) -> None:
    await db.execute(
        "UPDATE referrals SET rewarded=TRUE WHERE referrer_id=$1",
        referrer_id,
    )


DEFAULT_SWEAR_WORDS = [
    "كلب", "كلبة", "كلاب",
    "حمار", "حمارة", "حمير",
    "خنزير", "خنزيرة", "خنازير",
    "غبي", "غبية", "أغبياء", "أغبي",
    "حثالة",
    "يابن اللبوة", 
    "انيك",
    "حقير", "حقيرة",
    "تافه", "تافهة",
    "سافل", "سافلة",
    "قذر", "قذرة",
    "وسخ", "وسخة",
    "زبالة", "زبل", "قمامة",
    "نصاب", "نصابة",
    "حرامي", "حرامية",
    "كذاب", "كذابة",
    "منافق", "منافقة",
    "خائن", "خائنة",
    "بهيمة", "بهائم",
    "شرموط", "شرموطة",
    "عرص", "معرص",
    "قواد", "قوادة",
    "زاني", "زانية",
    "عاهر", "عاهرة",
    "احا", "خخخ", "كسك", "كسمك", "عرص", "خول", "يبن", "كلب", "علق", "كسم", "سسس", "كسمكم",
    "يابن متناكه", "يمتناكه", "يمتناك", "عرث", "الخول", "سكس", "كسي", "كسم فيجا", "طيز",
    "متيظز", "طيزو", "طيزي", "تيز", "تيزك", "كسس", "نيك", "هنيكك", "نيكو", "نكت", "ناكك",
    "معيرص", "كسماك", "كوسك", "يبن القحبه", "القحبه", "مك متناكه", "امك صحبتي", "يابن الفاجره",
    "الفاجره", "شرموطه", "شرمود", "شرموط", "صايع", "كلبي", "بت متناكه", "بت المتناكه", "العاهره",
    "بت فجره", "بت الفجره", "يولاد متناكه", "كسم اللي فجروب", "يولاد القحابي", "هلف", "حلوف",
    "مجلخ", "حمار", "تيث", "تيوس", "تيس", "جبان", "شخ", "خخخخ", "خهخهخهههه", "خخخخخخخه", "طري",
    "زبي", "تع مصمص", "تعالي مص", "مصمصلي فيزبي", "دخلو", "ادخلو", "انتشك", "زاني", "يا ابن الزنيه",
    "يا بت الزنيه", "يبن زانيه", "يبن زنيه", "يبت زانيه", "العبي فكسك", "ظيزك كبيره", "بزازك", "بز",
    "كسو", "كسمو", "كسمه", "كساس", "عره", "كس مجلخ", "كلابي", "كسم كيمي", "بضاني", "تع لحوص",
    "هفشخك", "هفشخو", "فشختو", "هنيك امك", "نكت امك", "امك الشرموطه", "امك متنكه", "امك متناكه",
    "طيزها", "معتوه", "كس", "زب", "يا ابن متناك",
    "fuck", "fucking", "fucker",
    "shit", "bitch", "bastard",
    "asshole", "dickhead", "dickface",
    "pussy", "cunt", "whore", "slut",
    "motherfucker",
    "retard", "retarded",
]


DEFAULT_BANNED_WORDS = DEFAULT_SWEAR_WORDS


async def ensure_banned_seeded(owner_id: int) -> None:
    await ensure_settings(owner_id)
    already = await db.fetchval(
        "SELECT 1 FROM banned_words "
        "WHERE owner_id=$1 AND kind='banned' LIMIT 1",
        owner_id,
    )
    if already:
        return
    for w in DEFAULT_BANNED_WORDS:
        try:
            await db.execute(
                "INSERT INTO banned_words(owner_id, word, kind) "
                "VALUES($1, $2, 'banned')",
                owner_id, w.lower(),
            )
        except Exception:
            log.exception("ensure_banned_seeded.failed", word=w)
