"""Neon (Postgres) storage for scanned cards.

Kya karta hai:
  - Har card ki finished row (jo Excel me jayegi) database me save hoti hai.
  - Server restart / session timeout / phone band hone par wahi cards wapas load ho jate hain.
  - Export Excel dabane ke baad (app.py) wo cards database se delete ho jate hain.

Setup: Render > Environment me DATABASE_URL daalo (Neon ki connection string, postgresql://...).
Table apne aap ban jata hai (CREATE TABLE IF NOT EXISTS).

Agar DATABASE_URL nahi hai ya DB down hai, app band nahi hota: wo purane tareeke se memory me chalta rehta hai
aur sidebar me error dikhata hai (status["error"]).
"""
import os
import threading
from datetime import timedelta, timezone

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

status = {"error": ""}      # last DB problem, shown in the sidebar ("" = all fine)

_pool = None
_ready = False
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS scanned_cards (
    token      text        NOT NULL,
    card_id    integer     NOT NULL,
    status     text        NOT NULL DEFAULT 'done',
    data       jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (token, card_id)
)
"""
UPSERT = """
INSERT INTO scanned_cards (token, card_id, status, data, updated_at)
VALUES (%s, %s, %s, %s, now())
ON CONFLICT (token, card_id)
DO UPDATE SET status = EXCLUDED.status, data = EXCLUDED.data, updated_at = now()
"""


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


def _get_pool():
    """One small connection pool for the whole server process (this module is imported only once)."""
    global _pool, _ready
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(
                os.environ["DATABASE_URL"].strip(),
                min_size=1, max_size=4, open=False,
                kwargs={"autocommit": True, "connect_timeout": 15},
                check=ConnectionPool.check_connection,   # Neon suspends idle compute -> drop dead connections
                max_idle=240,
            )
            _pool.open()
        if not _ready:
            with _pool.connection() as conn:
                conn.execute(SCHEMA)
            _ready = True
        return _pool


def _run(fn, default=None):
    """Run fn(connection). A database problem must never break card reading, so errors are only recorded."""
    if not enabled():
        return default
    try:
        with _get_pool().connection() as conn:
            result = fn(conn)
        status["error"] = ""
        return result
    except Exception as e:
        status["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return default


def save_many(token: str, items: list):
    """items = [(card_id, status, row_dict), ...] -> insert or update."""
    if not items:
        return

    def go(conn):
        with conn.cursor() as cur:
            cur.executemany(UPSERT, [(token, int(i), s, Jsonb(r)) for i, s, r in items])
    _run(go)


def save(token: str, card_id: int, card_status: str, row: dict):
    save_many(token, [(card_id, card_status, row)])


def delete_many(token: str, ids: list):
    ids = [int(i) for i in ids]
    if not ids:
        return
    _run(lambda conn: conn.execute("DELETE FROM scanned_cards WHERE token = %s AND card_id = ANY(%s)", (token, ids)))


def delete_token(token: str):
    _run(lambda conn: conn.execute("DELETE FROM scanned_cards WHERE token = %s", (token,)))


def load(token: str) -> list:
    """[(card_id, status, row_dict)] of this session, in card order."""
    def go(conn):
        cur = conn.execute("SELECT card_id, status, data FROM scanned_cards WHERE token = %s ORDER BY card_id", (token,))
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    return _run(go, []) or []


def pending_sessions(limit: int = 10) -> list:
    """Sessions that still have un-exported cards: [(token, card_count, 'dd Mon HH:MM' in IST)]."""
    def go(conn):
        cur = conn.execute("SELECT token, count(*), max(updated_at) FROM scanned_cards "
                           "GROUP BY token ORDER BY max(updated_at) DESC LIMIT %s", (limit,))
        out = []
        for token, n, ts in cur.fetchall():
            ist = ts.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
            out.append((token, n, ist.strftime("%d %b %H:%M")))
        return out
    return _run(go, []) or []