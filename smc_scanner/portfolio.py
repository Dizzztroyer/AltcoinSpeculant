# portfolio.py — Virtual portfolio: position sizing, PnL calculation
#
# Logic:
#   - Virtual balance starts at VIRTUAL_BALANCE (default $100)
#   - Each trade risks exactly RISK_PER_TRADE_PCT of the CURRENT balance
#   - Position size = risk_usd / (entry_mid - stop_loss)  [in base units]
#   - PnL is calculated when a signal closes (won / lost / expired)
#   - Balance compounds trade by trade (running total)
#   - Expired signals that never triggered = $0 PnL (no capital at risk)
#
# All state is stored in SQLite alongside signals (portfolio_snapshots table).

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import config
from utils import log_info


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    import config as cfg
    con = sqlite3.connect(cfg.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL UNIQUE,
    created_at    TEXT    NOT NULL,
    closed_at     TEXT,
    symbol        TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    entry_price   REAL    NOT NULL,   -- mid of entry zone
    exit_price    REAL,
    stop_loss     REAL    NOT NULL,
    take_profit   REAL    NOT NULL,
    position_size REAL    NOT NULL,   -- base-asset units
    risk_usd      REAL    NOT NULL,   -- $ risked on this trade
    pnl_usd       REAL,               -- realised P&L in $
    pnl_pct       REAL,               -- P&L as % of balance at trade open
    balance_before REAL   NOT NULL,   -- portfolio balance before this trade
    balance_after  REAL,              -- portfolio balance after close
    status        TEXT    DEFAULT 'open',   -- open | won | lost | expired
    rr_actual     REAL                -- actual RR achieved (exit vs entry vs sl)
);
"""

MIGRATE_COLS = {
    "position_size": "REAL",
    "pnl_usd":       "REAL",
    "pnl_pct":       "REAL",
}


def init_portfolio_db() -> None:
    """Create portfolio_snapshots table and migrate signals table if needed."""
    with _connect() as con:
        con.execute(CREATE_SNAPSHOTS)

        # Add PnL columns to signals table if missing (migration)
        sig_cols = {row[1] for row in con.execute("PRAGMA table_info(signals)")}
        for col, col_type in MIGRATE_COLS.items():
            if col not in sig_cols:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_type}")
                log_info(f"[PORTFOLIO] migrated signals: added {col}")


# ── Position sizing ────────────────────────────────────────────────────────────

def get_current_balance() -> float:
    """
    Return the current virtual balance.
    = VIRTUAL_BALANCE + sum of all closed PnL so far.
    """
    with _connect() as con:
        row = con.execute(
            "SELECT SUM(pnl_usd) as total FROM portfolio_snapshots "
            "WHERE status IN ('won','lost')"
        ).fetchone()
    realised = row["total"] or 0.0
    return config.VIRTUAL_BALANCE + realised


def calc_position(entry_mid: float,
                  stop_loss: float,
                  balance: float) -> tuple[float, float]:
    """
    Calculate position size and risk amount.

    Parameters
    ----------
    entry_mid : mid of entry zone (average fill assumption)
    stop_loss : stop-loss price
    balance   : current virtual balance

    Returns
    -------
    (position_size_units, risk_usd)

    position_size_units = risk_usd / abs(entry_mid - stop_loss)
    This is the number of base-asset units (e.g. BTC) to buy/sell.
    """
    risk_usd = balance * config.RISK_PER_TRADE_PCT
    price_risk = abs(entry_mid - stop_loss)
    if price_risk == 0:
        return 0.0, 0.0
    position_size = risk_usd / price_risk
    return round(position_size, 8), round(risk_usd, 4)


# ── Open a virtual trade ───────────────────────────────────────────────────────

def open_trade(signal_id: int,
               symbol: str,
               timeframe: str,
               direction: str,
               entry_mid: float,
               stop_loss: float,
               take_profit: float) -> Optional[float]:
    """
    Record a new virtual trade when a signal is triggered.

    Returns the position size (units), or None if sizing failed.
    Called by evaluator when entry_hit becomes True.
    """
    balance = get_current_balance()
    pos_size, risk_usd = calc_position(entry_mid, stop_loss, balance)

    if pos_size <= 0:
        log_info(f"[PORTFOLIO] #{signal_id} position sizing failed (zero risk)")
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
              entry_mid, stop_loss, take_profit,
              pos_size, risk_usd, balance))

        # Store position_size on the signal row too for quick reference
        con.execute("UPDATE signals SET position_size=? WHERE id=?",
                    (pos_size, signal_id))

    log_info(f"[PORTFOLIO] #{signal_id} trade opened  "
             f"size={pos_size:.6f} {symbol.split('/')[0]}  "
             f"risk=${risk_usd:.2f}  balance=${balance:.2f}")
    return pos_size


# ── Close a virtual trade ──────────────────────────────────────────────────────

def close_trade(signal_id: int,
                exit_price: float,
                outcome: str) -> Optional[dict]:
    """
    Close a virtual trade and calculate PnL.

    Parameters
    ----------
    signal_id  : DB signal id
    exit_price : actual exit price (TP, SL, or last price for expired)
    outcome    : 'won' | 'lost' | 'expired'

    Returns
    -------
    dict with pnl_usd, pnl_pct, balance_after, rr_actual
    or None if no open trade found.
    """
    with _connect() as con:
        snap = con.execute(
            "SELECT * FROM portfolio_snapshots WHERE signal_id=? AND status='open'",
            (signal_id,)
        ).fetchone()

    if snap is None:
        return None   # trade was never opened (entry never hit)

    entry_price   = snap["entry_price"]
    stop_loss     = snap["stop_loss"]
    take_profit   = snap["take_profit"]
    position_size = snap["position_size"]
    risk_usd      = snap["risk_usd"]
    balance_before = snap["balance_before"]
    direction     = snap["direction"]

    # ── PnL calculation ────────────────────────────────────────────────────────
    # For expired after entry — use exit_price (last close) as exit
    if outcome == "expired":
        # Assume we close at market (exit_price = last candle close)
        pnl_usd = _calc_pnl(direction, entry_price, exit_price, position_size)
    elif outcome == "won":
        pnl_usd = abs(risk_usd * snap_rr(snap))
    elif outcome == "lost":
        pnl_usd = -abs(risk_usd)
    else:
        pnl_usd = _calc_pnl(direction, entry_price, exit_price, position_size)

    # Actual RR achieved
    price_risk = abs(entry_price - stop_loss)
    rr_actual  = (abs(exit_price - entry_price) / price_risk
                  if price_risk > 0 else 0.0)
    if outcome == "lost":
        rr_actual = -rr_actual

    balance_after = balance_before + pnl_usd
    pnl_pct       = (pnl_usd / balance_before * 100) if balance_before else 0.0
    now           = datetime.now(timezone.utc).isoformat()

    with _connect() as con:
        con.execute("""
            UPDATE portfolio_snapshots
            SET closed_at=?, exit_price=?, pnl_usd=?, pnl_pct=?,
                balance_after=?, status=?, rr_actual=?
            WHERE signal_id=?
        """, (now, exit_price, round(pnl_usd, 4), round(pnl_pct, 4),
              round(balance_after, 4), outcome, round(rr_actual, 4),
              signal_id))

        con.execute("""
            UPDATE signals SET pnl_usd=?, pnl_pct=? WHERE id=?
        """, (round(pnl_usd, 4), round(pnl_pct, 4), signal_id))

    result = dict(
        pnl_usd=round(pnl_usd, 4),
        pnl_pct=round(pnl_pct, 4),
        balance_after=round(balance_after, 4),
        rr_actual=round(rr_actual, 4),
    )

    sign = "+" if pnl_usd >= 0 else ""
    log_info(f"[PORTFOLIO] #{signal_id} closed  "
             f"outcome={outcome.upper()}  "
             f"PnL={sign}${pnl_usd:.2f} ({sign}{pnl_pct:.2f}%)  "
             f"balance=${balance_after:.2f}")
    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def snap_rr(snap) -> float:
    """Planned RR from a portfolio_snapshots row."""
    price_risk = abs(snap["entry_price"] - snap["stop_loss"])
    if price_risk == 0:
        return 0.0
    return abs(snap["take_profit"] - snap["entry_price"]) / price_risk


def _calc_pnl(direction: str, entry: float,
              exit_p: float, size: float) -> float:
    """Raw PnL = position_size × price_difference (signed)."""
    if direction == "long":
        return size * (exit_p - entry)
    else:
        return size * (entry - exit_p)


# ── Statistics ─────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """
    Return a complete performance statistics dict.

    Keys:
        starting_balance, current_balance, total_pnl_usd, total_pnl_pct
        total_trades, triggered, won, lost, expired_after_entry
        win_rate, avg_win_usd, avg_loss_usd, avg_rr_actual
        best_trade_usd, worst_trade_usd
        profit_factor, expectancy_usd
        daily_pnl  : list of (date_str, pnl_usd, balance_eod)
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id"
        ).fetchall()

    stats: dict = {
        "starting_balance": config.VIRTUAL_BALANCE,
        "current_balance":  get_current_balance(),
        "total_trades":     0,
        "triggered":        0,
        "won":              0,
        "lost":             0,
        "expired_after_entry": 0,
        "win_rate":         0.0,
        "avg_win_usd":      0.0,
        "avg_loss_usd":     0.0,
        "avg_rr_actual":    0.0,
        "best_trade_usd":   0.0,
        "worst_trade_usd":  0.0,
        "profit_factor":    0.0,
        "expectancy_usd":   0.0,
        "total_pnl_usd":    0.0,
        "total_pnl_pct":    0.0,
        "daily_pnl":        [],
    }

    closed = [r for r in rows if r["status"] in ("won", "lost", "expired")]
    wins   = [r for r in closed if r["status"] == "won"]
    losses = [r for r in closed if r["status"] == "lost"]

    stats["total_trades"] = len(rows)
    stats["triggered"]    = len(closed)
    stats["won"]          = len(wins)
    stats["lost"]         = len(losses)
    stats["expired_after_entry"] = sum(
        1 for r in closed if r["status"] == "expired")

    if closed:
        pnl_values = [r["pnl_usd"] for r in closed if r["pnl_usd"] is not None]
        stats["total_pnl_usd"] = round(sum(pnl_values), 2)
        stats["total_pnl_pct"] = round(
            stats["total_pnl_usd"] / config.VIRTUAL_BALANCE * 100, 2)

    decisive = wins + losses
    if decisive:
        stats["win_rate"] = round(len(wins) / len(decisive) * 100, 1)

    if wins:
        win_pnls = [r["pnl_usd"] for r in wins if r["pnl_usd"] is not None]
        stats["avg_win_usd"]  = round(sum(win_pnls) / len(win_pnls), 2)
        stats["best_trade_usd"] = round(max(win_pnls), 2)

    if losses:
        loss_pnls = [r["pnl_usd"] for r in losses if r["pnl_usd"] is not None]
        stats["avg_loss_usd"]   = round(sum(loss_pnls) / len(loss_pnls), 2)
        stats["worst_trade_usd"] = round(min(loss_pnls), 2)

    rr_vals = [r["rr_actual"] for r in closed
               if r["rr_actual"] is not None]
    if rr_vals:
        stats["avg_rr_actual"] = round(sum(rr_vals) / len(rr_vals), 2)

    gross_profit = sum(r["pnl_usd"] for r in wins  if r["pnl_usd"])
    gross_loss   = abs(sum(r["pnl_usd"] for r in losses if r["pnl_usd"]))
    if gross_loss > 0:
        stats["profit_factor"] = round(gross_profit / gross_loss, 2)

    if decisive:
        stats["expectancy_usd"] = round(
            stats["total_pnl_usd"] / len(decisive), 2)

    # Daily PnL
    from collections import defaultdict
    daily: dict[str, float] = defaultdict(float)
    daily_bal: dict[str, float] = {}
    for r in closed:
        if r["closed_at"] and r["pnl_usd"] is not None:
            day = r["closed_at"][:10]
            daily[day] += r["pnl_usd"]
            daily_bal[day] = r["balance_after"] or 0.0

    stats["daily_pnl"] = [
        {"date": d, "pnl_usd": round(daily[d], 2),
         "balance_eod": round(daily_bal.get(d, 0.0), 2)}
        for d in sorted(daily)
    ]

    return stats