# journal.py — SQLite signal persistence layer (v2 + telegram_message_id)

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import config


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,
    timeframe            TEXT    NOT NULL,
    direction            TEXT    NOT NULL,
    context              TEXT,
    entry_low            REAL    NOT NULL,
    entry_high           REAL    NOT NULL,
    stop_loss            REAL    NOT NULL,
    take_profit          REAL    NOT NULL,
    rr                   REAL,
    score                INTEGER DEFAULT 0,
    alert_sent           INTEGER DEFAULT 0,
    telegram_message_id  INTEGER DEFAULT NULL,
    status               TEXT    DEFAULT 'pending',
    reason               TEXT,
    sweep_side           TEXT,
    bos_type             TEXT,
    higher_tf_bias       TEXT,
    entry_hit            INTEGER DEFAULT 0,
    entry_hit_at         TEXT,
    exit_price           REAL,
    exit_reason          TEXT,
    closed_at            TEXT,
    mfe                  REAL,
    mae                  REAL,
    expires_at           TEXT,
    signal_hash          TEXT    UNIQUE
);
"""

MIGRATE_ADD_TELEGRAM_ID = """
ALTER TABLE signals ADD COLUMN telegram_message_id INTEGER DEFAULT NULL;
"""


def init_db() -> None:
    """Create table if missing; run any pending migrations."""
    with _connect() as con:
        con.execute(CREATE_TABLE)
        con.execute("CREATE INDEX IF NOT EXISTS idx_status ON signals(status)")

        # Migration: add telegram_message_id if old DB exists without it
        cols = {row[1] for row in con.execute("PRAGMA table_info(signals)")}
        if "telegram_message_id" not in cols:
            con.execute(MIGRATE_ADD_TELEGRAM_ID)
            print("[DB] migrated: added telegram_message_id column")

    print(f"[DB] initialized → {config.DB_PATH}")


def make_signal_hash(symbol: str, timeframe: str, direction: str,
                     entry_low: float, entry_high: float) -> str:
    key = f"{symbol}|{timeframe}|{direction}|{round(entry_low,2)}|{round(entry_high,2)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def save_signal(sig: dict, score: int, higher_tf_bias: str = "") -> Optional[int]:
    """Persist a new signal. Returns new row id, or None if duplicate."""
    now        = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=config.SIGNAL_EXPIRY_HOURS)

    mid_entry = (sig["entry_low"] + sig["entry_high"]) / 2
    risk      = abs(mid_entry - sig["stop"])
    rr        = round(abs(sig["tp"] - mid_entry) / risk, 2) if risk else 0.0

    sig_hash = make_signal_hash(
        sig["symbol"], sig["timeframe"], sig["direction"],
        sig["entry_low"], sig["entry_high"]
    )

    row = {
        "created_at":          now.isoformat(),
        "symbol":              sig["symbol"],
        "timeframe":           sig["timeframe"],
        "direction":           sig["direction"],
        "context":             sig.get("context", ""),
        "entry_low":           sig["entry_low"],
        "entry_high":          sig["entry_high"],
        "stop_loss":           sig["stop"],
        "take_profit":         sig["tp"],
        "rr":                  rr,
        "score":               score,
        "alert_sent":          0,
        "telegram_message_id": None,
        "status":              "pending",
        "reason":              sig.get("reason", ""),
        "sweep_side":          sig.get("sweep_desc", ""),
        "bos_type":            sig.get("bos_desc", ""),
        "higher_tf_bias":      higher_tf_bias,
        "entry_hit":           0,
        "entry_hit_at":        None,
        "exit_price":          None,
        "exit_reason":         None,
        "closed_at":           None,
        "mfe":                 None,
        "mae":                 None,
        "expires_at":          expires_at.isoformat(),
        "signal_hash":         sig_hash,
    }

    sql = """
        INSERT OR IGNORE INTO signals
            (created_at, symbol, timeframe, direction, context,
             entry_low, entry_high, stop_loss, take_profit, rr,
             score, alert_sent, telegram_message_id, status, reason,
             sweep_side, bos_type, higher_tf_bias,
             entry_hit, entry_hit_at, exit_price, exit_reason,
             closed_at, mfe, mae, expires_at, signal_hash)
        VALUES
            (:created_at, :symbol, :timeframe, :direction, :context,
             :entry_low, :entry_high, :stop_loss, :take_profit, :rr,
             :score, :alert_sent, :telegram_message_id, :status, :reason,
             :sweep_side, :bos_type, :higher_tf_bias,
             :entry_hit, :entry_hit_at, :exit_price, :exit_reason,
             :closed_at, :mfe, :mae, :expires_at, :signal_hash)
    """
    with _connect() as con:
        cur = con.execute(sql, row)
        return cur.lastrowid if cur.rowcount > 0 else None


def get_open_signals() -> list[sqlite3.Row]:
    sql = "SELECT * FROM signals WHERE status IN ('pending','triggered') ORDER BY id"
    with _connect() as con:
        return con.execute(sql).fetchall()


def get_signal_by_id(signal_id: int) -> Optional[sqlite3.Row]:
    with _connect() as con:
        return con.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()


def get_recent_signals(symbol: str, timeframe: str,
                       direction: str, hours: int) -> list[sqlite3.Row]:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    sql = """
        SELECT * FROM signals
        WHERE symbol=? AND timeframe=? AND direction=? AND created_at >= ?
        ORDER BY id DESC
    """
    with _connect() as con:
        return con.execute(sql, (symbol, timeframe, direction, cutoff)).fetchall()


def mark_alert_sent(signal_id: int, telegram_message_id: int | None = None) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE signals SET alert_sent=1, telegram_message_id=? WHERE id=?",
            (telegram_message_id, signal_id),
        )


def update_signal_status(signal_id: int, status: str,
                         entry_hit: bool = False,
                         entry_hit_at: Optional[str] = None,
                         exit_price: Optional[float] = None,
                         exit_reason: Optional[str] = None,
                         closed_at: Optional[str] = None,
                         mfe: Optional[float] = None,
                         mae: Optional[float] = None) -> None:
    fields: dict = {"status": status}
    if entry_hit:
        fields["entry_hit"]    = 1
        fields["entry_hit_at"] = entry_hit_at
    if exit_price is not None:
        fields["exit_price"]  = exit_price
        fields["exit_reason"] = exit_reason
        fields["closed_at"]   = closed_at
    if mfe is not None:
        fields["mfe"] = mfe
    if mae is not None:
        fields["mae"] = mae

    set_clause = ", ".join(f"{k}=?" for k in fields)
    values     = list(fields.values()) + [signal_id]
    with _connect() as con:
        con.execute(f"UPDATE signals SET {set_clause} WHERE id=?", values)


def print_summary() -> None:
    sql = "SELECT status, COUNT(*) as n FROM signals GROUP BY status"
    with _connect() as con:
        rows = con.execute(sql).fetchall()
    if not rows:
        print("[DB] no signals stored yet")
        return
    print("[DB] Signal summary:")
    for r in rows:
        print(f"       {r['status']:12s} {r['n']:4d}")