# portfolio.py — Virtual portfolio: position sizing, PnL, statistics
#
# Risk model: fixed fractional — 1% of current balance per trade.
# Balance compounds trade-by-trade.

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from utils import log_info


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id      INTEGER NOT NULL UNIQUE,
    created_at     TEXT    NOT NULL,
    closed_at      TEXT,
    symbol         TEXT    NOT NULL,
    timeframe      TEXT    NOT NULL,
    direction      TEXT    NOT NULL,
    entry_price    REAL    NOT NULL,
    exit_price     REAL,
    stop_loss      REAL    NOT NULL,
    take_profit    REAL    NOT NULL,
    position_size  REAL    NOT NULL,
    risk_usd       REAL    NOT NULL,
    pnl_usd        REAL,
    pnl_pct        REAL,
    balance_before REAL    NOT NULL,
    balance_after  REAL,
    status         TEXT    DEFAULT 'open',
    rr_actual      REAL
);
"""

_MIGRATE_SIGNALS = {
    "position_size": "REAL",
    "pnl_usd":       "REAL",
    "pnl_pct":       "REAL",
}


def init_portfolio_db() -> None:
    with _connect() as con:
        con.execute(CREATE_SNAPSHOTS)
        sig_cols = {row[1] for row in con.execute("PRAGMA table_info(signals)")}
        for col, typ in _MIGRATE_SIGNALS.items():
            if col not in sig_cols:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
                log_info(f"[PORTFOLIO] migrated signals: added {col}")


def get_current_balance() -> float:
    with _connect() as con:
        row = con.execute(
            "SELECT SUM(pnl_usd) as total FROM portfolio_snapshots "
            "WHERE status IN ('won','lost')"
        ).fetchone()
    return config.VIRTUAL_BALANCE + (row["total"] or 0.0)


def calc_position(entry_mid: float, stop_loss: float,
                  balance: float) -> tuple[float, float]:
    risk_usd   = balance * config.RISK_PER_TRADE_PCT
    price_risk = abs(entry_mid - stop_loss)
    if price_risk == 0:
        return 0.0, 0.0
    return round(risk_usd / price_risk, 8), round(risk_usd, 4)


def open_trade(signal_id: int, symbol: str, timeframe: str,
               direction: str, entry_mid: float,
               stop_loss: float, take_profit: float) -> Optional[float]:
    balance   = get_current_balance()
    pos, risk = calc_position(entry_mid, stop_loss, balance)
    if pos <= 0:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as con:
        con.execute("""
            INSERT OR IGNORE INTO portfolio_snapshots
                (signal_id, created_at, symbol, timeframe, direction,
                 entry_price, stop_loss, take_profit,
                 position_size, risk_usd, balance_before, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')
        """, (signal_id, now, symbol, timeframe, direction,
              entry_mid, stop_loss, take_profit, pos, risk, balance))
        con.execute("UPDATE signals SET position_size=? WHERE id=?", (pos, signal_id))
    log_info(f"[PORTFOLIO] #{signal_id} opened  size={pos:.6f}  "
             f"risk=${risk:.2f}  balance=${balance:.2f}")
    return pos


def close_trade(signal_id: int, exit_price: float,
                outcome: str) -> Optional[dict]:
    with _connect() as con:
        snap = con.execute(
            "SELECT * FROM portfolio_snapshots WHERE signal_id=? AND status='open'",
            (signal_id,)
        ).fetchone()
    if snap is None:
        return None

    entry      = snap["entry_price"]
    stop       = snap["stop_loss"]
    pos        = snap["position_size"]
    risk_usd   = snap["risk_usd"]
    bal_before = snap["balance_before"]
    direction  = snap["direction"]

    if outcome == "won":
        pnl_usd = abs(risk_usd * _snap_rr(snap))
    elif outcome == "lost":
        pnl_usd = -abs(risk_usd)
    else:
        pnl_usd = _calc_pnl(direction, entry, exit_price, pos)

    price_risk = abs(entry - stop)
    rr_actual  = (abs(exit_price - entry) / price_risk if price_risk > 0 else 0.0)
    if outcome == "lost":
        rr_actual = -rr_actual

    bal_after = bal_before + pnl_usd
    pnl_pct   = (pnl_usd / bal_before * 100) if bal_before else 0.0
    now       = datetime.now(timezone.utc).isoformat()

    with _connect() as con:
        con.execute("""
            UPDATE portfolio_snapshots
            SET closed_at=?, exit_price=?, pnl_usd=?, pnl_pct=?,
                balance_after=?, status=?, rr_actual=?
            WHERE signal_id=?
        """, (now, exit_price, round(pnl_usd, 4), round(pnl_pct, 4),
              round(bal_after, 4), outcome, round(rr_actual, 4), signal_id))
        con.execute("UPDATE signals SET pnl_usd=?, pnl_pct=? WHERE id=?",
                    (round(pnl_usd, 4), round(pnl_pct, 4), signal_id))

    sign = "+" if pnl_usd >= 0 else ""
    log_info(f"[PORTFOLIO] #{signal_id} closed  {outcome.upper()}  "
             f"PnL={sign}${pnl_usd:.2f} ({sign}{pnl_pct:.2f}%)  "
             f"balance=${bal_after:.2f}")

    return dict(pnl_usd=round(pnl_usd, 4), pnl_pct=round(pnl_pct, 4),
                balance_after=round(bal_after, 4), rr_actual=round(rr_actual, 4))


def _snap_rr(snap) -> float:
    risk = abs(snap["entry_price"] - snap["stop_loss"])
    if risk == 0:
        return 0.0
    return abs(snap["take_profit"] - snap["entry_price"]) / risk


def _calc_pnl(direction: str, entry: float, exit_p: float, size: float) -> float:
    return size * (exit_p - entry) if direction == "long" else size * (entry - exit_p)


# ── Statistics ─────────────────────────────────────────────────────────────────

def get_stats(days_back: int | None = None) -> dict:
    """
    Full performance stats. If days_back is set, only include trades
    closed within the last N days.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id"
        ).fetchall()

    if days_back is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        rows = [r for r in rows if (r["closed_at"] or "") >= cutoff]

    s = _empty_stats()

    closed  = [r for r in rows if r["status"] in ("won", "lost", "expired")]
    wins    = [r for r in closed if r["status"] == "won"]
    losses  = [r for r in closed if r["status"] == "lost"]
    decisive = wins + losses

    s["total_trades"]        = len(rows)
    s["triggered"]           = len(closed)
    s["won"]                 = len(wins)
    s["lost"]                = len(losses)
    s["expired_after_entry"] = sum(1 for r in closed if r["status"] == "expired")

    if closed:
        pnls = [r["pnl_usd"] for r in closed if r["pnl_usd"] is not None]
        s["total_pnl_usd"] = round(sum(pnls), 2)
        s["total_pnl_pct"] = round(s["total_pnl_usd"] / config.VIRTUAL_BALANCE * 100, 2)

    s["current_balance"] = get_current_balance()

    if decisive:
        s["win_rate"] = round(len(wins) / len(decisive) * 100, 1)

    if wins:
        wp = [r["pnl_usd"] for r in wins if r["pnl_usd"]]
        s["avg_win_usd"]    = round(sum(wp) / len(wp), 2)
        s["best_trade_usd"] = round(max(wp), 2)

    if losses:
        lp = [r["pnl_usd"] for r in losses if r["pnl_usd"]]
        s["avg_loss_usd"]    = round(sum(lp) / len(lp), 2)
        s["worst_trade_usd"] = round(min(lp), 2)

    rr_vals = [r["rr_actual"] for r in closed if r["rr_actual"] is not None]
    if rr_vals:
        s["avg_rr_actual"] = round(sum(rr_vals) / len(rr_vals), 2)

    gross_profit = sum(r["pnl_usd"] for r in wins   if r["pnl_usd"])
    gross_loss   = abs(sum(r["pnl_usd"] for r in losses if r["pnl_usd"]))
    if gross_loss > 0:
        s["profit_factor"] = round(gross_profit / gross_loss, 2)
    if decisive:
        s["expectancy_usd"] = round(s["total_pnl_usd"] / len(decisive), 2)

    # Daily PnL buckets
    daily: dict[str, float]  = defaultdict(float)
    daily_bal: dict[str, float] = {}
    daily_won: dict[str, int]   = defaultdict(int)
    daily_lost: dict[str, int]  = defaultdict(int)

    for r in closed:
        if r["closed_at"] and r["pnl_usd"] is not None:
            day = r["closed_at"][:10]
            daily[day]     += r["pnl_usd"]
            daily_bal[day]  = r["balance_after"] or 0.0
            if r["status"] == "won":
                daily_won[day] += 1
            elif r["status"] == "lost":
                daily_lost[day] += 1

    s["daily_pnl"] = [
        {"date": d, "pnl_usd": round(daily[d], 2),
         "balance_eod": round(daily_bal.get(d, 0.0), 2),
         "won": daily_won[d], "lost": daily_lost[d]}
        for d in sorted(daily)
    ]

    return s


def _empty_stats() -> dict:
    return dict(
        starting_balance=config.VIRTUAL_BALANCE,
        current_balance=config.VIRTUAL_BALANCE,
        total_pnl_usd=0.0, total_pnl_pct=0.0,
        total_trades=0, triggered=0, won=0, lost=0,
        expired_after_entry=0, win_rate=0.0,
        avg_win_usd=0.0, avg_loss_usd=0.0, avg_rr_actual=0.0,
        best_trade_usd=0.0, worst_trade_usd=0.0,
        profit_factor=0.0, expectancy_usd=0.0, daily_pnl=[],
    )


def get_period_stats(days: int) -> dict:
    """Stats for last N days only."""
    return get_stats(days_back=days)