# evaluator.py — Signal lifecycle evaluation
#
# For every open signal stored in the DB, fetch fresh OHLCV data and
# determine whether the trade played out (won / lost / expired / triggered).
#
# ── Ambiguity rule (documented for README) ────────────────────────────────────
#  When both SL and TP are inside the same candle (high >= TP and low <= SL):
#  • For LONG  signals: if close > mid_entry → TP assumed first (won)
#                       else                 → SL assumed first (lost)
#  • For SHORT signals: if close < mid_entry → TP assumed first (won)
#                       else                 → SL assumed first (lost)
#  This conservative rule avoids inflating win-rate.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

import config
import journal
from datafeed import fetch_ohlcv
from utils import log_info, log_warn


def evaluate_open_signals() -> None:
    """
    Main entry point called each scan cycle.
    Loads all open signals and checks their outcome against fresh candle data.
    """
    open_signals = journal.get_open_signals()
    if not open_signals:
        log_info("[EVAL] no open signals to evaluate")
        return

    log_info(f"[EVAL] evaluating {len(open_signals)} open signal(s) ...")

    for row in open_signals:
        _evaluate_one(row)


def _evaluate_one(row) -> None:
    signal_id  = row["id"]
    symbol     = row["symbol"]
    timeframe  = row["timeframe"]
    direction  = row["direction"]
    entry_low  = row["entry_low"]
    entry_high = row["entry_high"]
    stop_loss  = row["stop_loss"]
    take_profit = row["take_profit"]
    created_at = row["created_at"]
    expires_at = row["expires_at"]
    status     = row["status"]

    now = datetime.now(timezone.utc).isoformat()

    # ── 1. Check expiry ───────────────────────────────────────────────────────
    if expires_at and now >= expires_at and status == "pending":
        journal.update_signal_status(signal_id, "expired",
                                     closed_at=now,
                                     exit_reason="expired before entry")
        log_info(f"[EVAL] signal #{signal_id} {symbol} {timeframe} → expired")
        return

    # ── 2. Fetch candles after signal creation ────────────────────────────────
    df = fetch_ohlcv(symbol, timeframe, limit=config.EVALUATION_LOOKAHEAD_BARS + 50)
    if df.empty:
        log_warn(f"[EVAL] no data for #{signal_id} {symbol} {timeframe}")
        return

    # Keep only candles that formed AFTER the signal was created
    created_dt = pd.to_datetime(created_at, utc=True)
    df = df[df["timestamp"] > created_dt].reset_index(drop=True)

    if df.empty:
        log_info(f"[EVAL] #{signal_id}: no new candles since creation")
        return

    # ── 3. Limit lookahead ────────────────────────────────────────────────────
    df = df.head(config.EVALUATION_LOOKAHEAD_BARS)

    mid_entry = (entry_low + entry_high) / 2

    entry_hit    = row["entry_hit"] == 1
    entry_hit_at = row["entry_hit_at"]

    mfe = row["mfe"]   # max favorable excursion (price moved in our favour)
    mae = row["mae"]   # max adverse excursion  (price moved against us)

    # MFE / MAE accumulators (start from prior values if already triggered)
    best_price  = mid_entry   # best price seen in our favour
    worst_price = mid_entry   # worst price seen against us

    for _, candle in df.iterrows():
        h = candle["high"]
        l = candle["low"]
        c = candle["close"]
        ts = candle["timestamp"].isoformat()

        # ── Mark entry as triggered ───────────────────────────────────────────
        if not entry_hit:
            if direction == "long"  and l <= entry_high:
                entry_hit    = True
                entry_hit_at = ts
                journal.update_signal_status(signal_id, "triggered",
                                             entry_hit=True, entry_hit_at=ts)
                log_info(f"[EVAL] #{signal_id} {symbol} TRIGGERED @ {ts}")
            elif direction == "short" and h >= entry_low:
                entry_hit    = True
                entry_hit_at = ts
                journal.update_signal_status(signal_id, "triggered",
                                             entry_hit=True, entry_hit_at=ts)
                log_info(f"[EVAL] #{signal_id} {symbol} TRIGGERED @ {ts}")

        if not entry_hit:
            # Check expiry while waiting for entry
            if expires_at and ts >= expires_at:
                journal.update_signal_status(signal_id, "expired",
                                             closed_at=ts,
                                             exit_reason="expired before entry")
                log_info(f"[EVAL] #{signal_id} → expired (no entry)")
                return
            continue

        # ── Update MFE / MAE ──────────────────────────────────────────────────
        if direction == "long":
            best_price  = max(best_price,  h)
            worst_price = min(worst_price, l)
            mfe = round(best_price  - mid_entry, 6)
            mae = round(worst_price - mid_entry, 6)   # negative = adverse
        else:
            best_price  = min(best_price,  l)
            worst_price = max(worst_price, h)
            mfe = round(mid_entry - best_price,  6)
            mae = round(mid_entry - worst_price, 6)   # negative = adverse

        # ── Check SL / TP hits ────────────────────────────────────────────────
        tp_hit = (direction == "long"  and h >= take_profit) or \
                 (direction == "short" and l <= take_profit)
        sl_hit = (direction == "long"  and l <= stop_loss) or \
                 (direction == "short" and h >= stop_loss)

        if tp_hit and sl_hit:
            # Ambiguity: both inside same candle — use close to decide
            if direction == "long":
                outcome = "won" if c > mid_entry else "lost"
            else:
                outcome = "won" if c < mid_entry else "lost"
            exit_p   = take_profit if outcome == "won" else stop_loss
            exit_r   = "TP (ambiguous candle)" if outcome == "won" else "SL (ambiguous candle)"
        elif tp_hit:
            outcome = "won"
            exit_p  = take_profit
            exit_r  = "TP hit"
        elif sl_hit:
            outcome = "lost"
            exit_p  = stop_loss
            exit_r  = "SL hit"
        else:
            # Check expiry
            if expires_at and ts >= expires_at:
                journal.update_signal_status(signal_id, "expired",
                                             mfe=mfe, mae=mae,
                                             closed_at=ts,
                                             exit_reason="expired after entry",
                                             exit_price=c)
                log_info(f"[EVAL] #{signal_id} → expired after entry")
                return
            continue

        # ── Record final outcome ──────────────────────────────────────────────
        journal.update_signal_status(
            signal_id, outcome,
            entry_hit=True, entry_hit_at=entry_hit_at,
            exit_price=exit_p, exit_reason=exit_r,
            closed_at=ts, mfe=mfe, mae=mae,
        )
        log_info(f"[EVAL] signal #{signal_id} {symbol} {timeframe} "
                 f"{direction.upper()} → {outcome.upper()}  ({exit_r})")
        return

    # Reached end of lookahead without conclusion — update MFE/MAE and leave open
    if entry_hit and (mfe is not None or mae is not None):
        journal.update_signal_status(signal_id, "triggered", mfe=mfe, mae=mae)