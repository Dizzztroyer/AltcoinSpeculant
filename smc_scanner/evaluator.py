# evaluator.py — Signal lifecycle + portfolio PnL recording

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

import config
import journal
import portfolio
from datafeed import fetch_ohlcv
from utils import log_info, log_warn


def evaluate_open_signals() -> None:
    open_signals = journal.get_open_signals()
    if not open_signals:
        log_info("[EVAL] no open signals to evaluate")
        return
    log_info(f"[EVAL] evaluating {len(open_signals)} open signal(s) ...")
    for row in open_signals:
        _evaluate_one(row)


def _evaluate_one(row) -> None:
    signal_id   = row["id"]
    symbol      = row["symbol"]
    timeframe   = row["timeframe"]
    direction   = row["direction"]
    entry_low   = row["entry_low"]
    entry_high  = row["entry_high"]
    stop_loss   = row["stop_loss"]
    take_profit = row["take_profit"]
    created_at  = row["created_at"]
    expires_at  = row["expires_at"]
    status      = row["status"]
    mid_entry   = (entry_low + entry_high) / 2

    now = datetime.now(timezone.utc).isoformat()

    # ── Immediate expiry ──────────────────────────────────────────────────────
    if expires_at and now >= expires_at and status == "pending":
        journal.update_signal_status(signal_id, "expired",
                                     closed_at=now,
                                     exit_reason="expired before entry")
        log_info(f"[EVAL] #{signal_id} {symbol} → expired (no entry)")
        _try_send_outcome_reply(signal_id)
        return

    # ── Fetch candles ─────────────────────────────────────────────────────────
    df = fetch_ohlcv(symbol, timeframe,
                     limit=config.EVALUATION_LOOKAHEAD_BARS + 50)
    if df.empty:
        log_warn(f"[EVAL] no data for #{signal_id} {symbol} {timeframe}")
        return

    created_dt = pd.to_datetime(created_at, utc=True)
    df = df[df["timestamp"] > created_dt].reset_index(drop=True)
    if df.empty:
        log_info(f"[EVAL] #{signal_id}: no new candles yet")
        return
    df = df.head(config.EVALUATION_LOOKAHEAD_BARS)

    entry_hit    = row["entry_hit"] == 1
    entry_hit_at = row["entry_hit_at"]
    mfe = row["mfe"]
    mae = row["mae"]
    best_price  = mid_entry
    worst_price = mid_entry

    for _, candle in df.iterrows():
        h  = candle["high"]
        l  = candle["low"]
        c  = candle["close"]
        ts = candle["timestamp"].isoformat()

        # ── Entry trigger ─────────────────────────────────────────────────────
        if not entry_hit:
            triggered = ((direction == "long"  and l <= entry_high) or
                         (direction == "short" and h >= entry_low))
            if triggered:
                entry_hit    = True
                entry_hit_at = ts
                journal.update_signal_status(signal_id, "triggered",
                                             entry_hit=True, entry_hit_at=ts)
                log_info(f"[EVAL] #{signal_id} {symbol} TRIGGERED @ {ts}")

                # ── Open virtual trade ────────────────────────────────────────
                portfolio.open_trade(
                    signal_id=signal_id,
                    symbol=symbol, timeframe=timeframe, direction=direction,
                    entry_mid=mid_entry, stop_loss=stop_loss,
                    take_profit=take_profit,
                )
            else:
                if expires_at and ts >= expires_at:
                    journal.update_signal_status(signal_id, "expired",
                                                 closed_at=ts,
                                                 exit_reason="expired before entry")
                    log_info(f"[EVAL] #{signal_id} → expired (no entry)")
                    _try_send_outcome_reply(signal_id)
                    return
                continue

        # ── MFE / MAE ─────────────────────────────────────────────────────────
        if direction == "long":
            best_price  = max(best_price, h)
            worst_price = min(worst_price, l)
            mfe = round(best_price  - mid_entry, 6)
            mae = round(worst_price - mid_entry, 6)
        else:
            best_price  = min(best_price, l)
            worst_price = max(worst_price, h)
            mfe = round(mid_entry - best_price,  6)
            mae = round(mid_entry - worst_price, 6)

        # ── SL / TP check ─────────────────────────────────────────────────────
        tp_hit = ((direction == "long"  and h >= take_profit) or
                  (direction == "short" and l <= take_profit))
        sl_hit = ((direction == "long"  and l <= stop_loss) or
                  (direction == "short" and h >= stop_loss))

        if tp_hit and sl_hit:
            outcome = ("won" if ((direction == "long"  and c > mid_entry) or
                                 (direction == "short" and c < mid_entry))
                       else "lost")
            exit_p = take_profit if outcome == "won" else stop_loss
            exit_r = ("TP (ambiguous)" if outcome == "won"
                      else "SL (ambiguous)")
        elif tp_hit:
            outcome, exit_p, exit_r = "won",  take_profit, "TP hit"
        elif sl_hit:
            outcome, exit_p, exit_r = "lost", stop_loss,   "SL hit"
        else:
            if expires_at and ts >= expires_at:
                journal.update_signal_status(signal_id, "expired",
                                             mfe=mfe, mae=mae,
                                             closed_at=ts,
                                             exit_reason="expired after entry",
                                             exit_price=c)
                # Close virtual trade at market
                portfolio.close_trade(signal_id, exit_price=c, outcome="expired")
                log_info(f"[EVAL] #{signal_id} → expired after entry")
                _try_send_outcome_reply(signal_id)
                return
            continue

        # ── Record outcome ────────────────────────────────────────────────────
        journal.update_signal_status(
            signal_id, outcome,
            entry_hit=True, entry_hit_at=entry_hit_at,
            exit_price=exit_p, exit_reason=exit_r,
            closed_at=ts, mfe=mfe, mae=mae,
        )

        # ── Close virtual trade ───────────────────────────────────────────────
        pnl_result = portfolio.close_trade(signal_id,
                                           exit_price=exit_p,
                                           outcome=outcome)

        log_info(f"[EVAL] #{signal_id} {symbol} {timeframe} "
                 f"{direction.upper()} → {outcome.upper()} ({exit_r})"
                 + (f"  PnL=${pnl_result['pnl_usd']:+.2f}" if pnl_result else ""))

        _try_send_outcome_reply(signal_id)
        return

    # End of lookahead — still open
    if entry_hit and mfe is not None:
        journal.update_signal_status(signal_id, "triggered", mfe=mfe, mae=mae)


def _try_send_outcome_reply(signal_id: int) -> None:
    try:
        from alerts import send_outcome_reply
        send_outcome_reply(signal_id)
    except Exception as exc:
        log_warn(f"[EVAL] reply error for #{signal_id}: {exc}")