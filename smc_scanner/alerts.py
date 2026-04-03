# alerts.py — Telegram alerts with chart image + outcome replies
#
# Flow for new signals:
#   1. render PNG chart via charting.render_chart_image()
#   2. send photo with caption  (sendPhoto)
#   3. save returned message_id to DB
#
# Flow for closed signals (won / lost / expired):
#   1. load original telegram_message_id from DB
#   2. send reply_to_message_id pointing at the original signal message
#   3. include outcome emoji, MFE/MAE, exit price

import os
from pathlib import Path
from typing import Optional

import requests

import config
import journal
from utils import log_info, log_warn


# ── New signal alert ──────────────────────────────────────────────────────────

def maybe_send_alert(signal_id: int, sig: dict, score: int,
                     htf_bias: str, vol_confirmed: bool,
                     df=None, zones=None, sweeps=None,
                     obs=None, fvgs=None) -> None:
    """
    Send a Telegram alert for a fresh signal.

    Parameters
    ----------
    signal_id     : DB row id
    sig           : signal dict
    score         : 0-100 quality score
    htf_bias      : 'bullish' | 'bearish' | 'range' | ''
    vol_confirmed : whether volume spike detected
    df            : OHLCV DataFrame for chart rendering (optional)
    zones         : liquidity zones list (optional)
    sweeps        : sweep events list (optional)
    """
    if not config.TELEGRAM_ENABLED:
        return

    if score < config.ALERT_SCORE_THRESHOLD:
        log_info(f"[ALERT] #{signal_id} score {score} < threshold "
                 f"{config.ALERT_SCORE_THRESHOLD} — skipped")
        return

    row = journal.get_signal_by_id(signal_id)
    if row and row["alert_sent"]:
        log_info(f"[SKIP] #{signal_id} alert already sent")
        return

    if _recently_alerted(sig["symbol"], sig["timeframe"], sig["direction"]):
        log_info(f"[SKIP] duplicate suppressed for {sig['symbol']} "
                 f"{sig['timeframe']} {sig['direction'].upper()}")
        return

    caption   = _format_signal_caption(sig, score, htf_bias, vol_confirmed)
    image_path = None

    # Try to render chart PNG
    if df is not None and zones is not None and sweeps is not None:
        try:
            from charting import render_chart_image
            image_path = render_chart_image(df, sig["symbol"], sig["timeframe"],
                                            zones, sweeps, sig,
                                            obs=obs, fvgs=fvgs)
        except Exception as exc:
            log_warn(f"[CHART] render failed: {exc}")

    # Send photo or text fallback
    if image_path:
        msg_id = _send_photo(image_path, caption)
        try:
            Path(image_path).unlink()   # clean up temp file
        except OSError:
            pass
    else:
        msg_id = _send_text(caption)

    if msg_id is not None:
        journal.mark_alert_sent(signal_id, telegram_message_id=msg_id)
        log_info(f"[ALERT] Telegram sent for {sig['symbol']} {sig['timeframe']} "
                 f"{sig['direction'].upper()}  score={score}  msg_id={msg_id}")
    else:
        log_warn(f"[ALERT] delivery failed for #{signal_id}")


# ── Outcome reply ─────────────────────────────────────────────────────────────

def send_outcome_reply(signal_id: int) -> None:
    """
    Called by evaluator after a signal is closed (won / lost / expired).
    Replies to the original Telegram signal message with the outcome.
    """
    if not config.TELEGRAM_ENABLED:
        return

    row = journal.get_signal_by_id(signal_id)
    if row is None:
        return

    # Only reply if we originally sent an alert for this signal
    msg_id = row["telegram_message_id"]
    if not msg_id:
        return

    status     = row["status"]
    symbol     = row["symbol"]
    timeframe  = row["timeframe"]
    direction  = row["direction"]
    exit_price = row["exit_price"]
    exit_reason= row["exit_reason"] or ""
    mfe        = row["mfe"]
    mae        = row["mae"]
    rr         = row["rr"] or 0.0

    # Outcome emoji
    if status == "won":
        headline = "✅ <b>TRADE WON</b>"
    elif status == "lost":
        headline = "❌ <b>TRADE LOST</b>"
    elif status == "expired":
        headline = "⏱ <b>SIGNAL EXPIRED</b>"
    else:
        return   # not a terminal state — don't reply yet

    direction_emoji = "🟢" if direction == "long" else "🔴"

    lines = [
        headline,
        "",
        f"<b>{symbol}</b>  [{timeframe}]  {direction_emoji} {direction.upper()}",
        "",
        f"<b>Result</b>:  {status.upper()}",
        f"<b>Reason</b>:  {exit_reason}",
    ]
    if exit_price is not None:
        lines.append(f"<b>Exit price</b>: {exit_price:.2f}")
    if mfe is not None:
        lines.append(f"<b>MFE</b>: {mfe:+.4f}  (max in your favour)")
    if mae is not None:
        lines.append(f"<b>MAE</b>: {mae:+.4f}  (max against you)")
    lines.append(f"<b>RR planned</b>: {rr:.2f}")

    text = "\n".join(lines)

    ok = _send_reply(text, reply_to_message_id=msg_id)
    if ok:
        log_info(f"[ALERT] outcome reply sent for #{signal_id} → {status.upper()}")
    else:
        log_warn(f"[ALERT] outcome reply failed for #{signal_id}")


# ── Caption formatters ────────────────────────────────────────────────────────

def _format_signal_caption(sig: dict, score: int,
                            htf_bias: str, vol_confirmed: bool) -> str:
    direction = sig["direction"].upper()
    emoji     = "🟢" if direction == "LONG" else "🔴"

    htf_line = (
        f"✅ HTF aligned ({htf_bias})"   if htf_bias in ("bullish", "bearish") and sig.get("htf_aligned") else
        f"⚪ HTF neutral ({htf_bias})"   if htf_bias == "range" else
        f"❌ HTF opposing ({htf_bias})"  if sig.get("htf_opposing") else
        "❓ HTF unavailable"
    )
    vol_line = "✅ Volume spike" if vol_confirmed else "⚪ No volume spike"

    mid_entry = (sig["entry_low"] + sig["entry_high"]) / 2
    risk      = abs(mid_entry - sig["stop"])
    rr        = round(abs(sig["tp"] - mid_entry) / risk, 2) if risk else 0.0

    return (
        f"🚨 <b>SMC Signal</b>\n"
        f"\n"
        f"<b>Symbol</b>:    {sig['symbol']}\n"
        f"<b>Timeframe</b>: {sig['timeframe']}\n"
        f"<b>Direction</b>: {emoji} {direction}\n"
        f"<b>Context</b>:   {sig.get('context','')}\n"
        f"<b>Score</b>:     {score}/100\n"
        f"\n"
        f"<b>Entry</b>: {sig['entry_low']:.2f} – {sig['entry_high']:.2f}\n"
        f"<b>Stop</b>:  {sig['stop']:.2f}\n"
        f"<b>TP</b>:    {sig['tp']:.2f}\n"
        f"<b>RR</b>:    {rr:.2f}\n"
        f"\n"
        f"{htf_line}\n"
        f"{vol_line}\n"
        f"\n"
        f"<i>{sig.get('reason','')}</i>"
    )


# ── Telegram API helpers ──────────────────────────────────────────────────────

def _send_photo(image_path: str, caption: str) -> Optional[int]:
    """Send a photo with caption. Returns message_id or None."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "chat_id":    config.TELEGRAM_CHAT_ID,
                    "caption":    caption,
                    "parse_mode": "HTML",
                },
                files={"photo": f},
                timeout=30,
            )
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        log_warn(f"[ALERT] sendPhoto error {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as exc:
        log_warn(f"[ALERT] sendPhoto exception: {exc}")
        return None


def _send_text(text: str) -> Optional[int]:
    """Send a plain text message. Returns message_id or None."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        log_warn(f"[ALERT] sendMessage error {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as exc:
        log_warn(f"[ALERT] sendMessage exception: {exc}")
        return None


def _send_reply(text: str, reply_to_message_id: int) -> bool:
    """Reply to a specific message. Returns True on success."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":             config.TELEGRAM_CHAT_ID,
            "text":                text,
            "parse_mode":          "HTML",
            "reply_to_message_id": reply_to_message_id,
            "disable_web_page_preview": True,
        }, timeout=10)
        return resp.status_code == 200
    except Exception as exc:
        log_warn(f"[ALERT] reply exception: {exc}")
        return False


# ── Dedup check ───────────────────────────────────────────────────────────────

def _recently_alerted(symbol: str, timeframe: str, direction: str) -> bool:
    rows = journal.get_recent_signals(
        symbol, timeframe, direction, config.DEDUP_LOOKBACK_HOURS
    )
    return any(r["alert_sent"] == 1 for r in rows)