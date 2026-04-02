# alerts.py — Telegram alert delivery
#
# Sends a formatted message to a Telegram chat via the Bot API.
# Alert is suppressed if:
#   • TELEGRAM_ENABLED is False
#   • score is below ALERT_SCORE_THRESHOLD
#   • alert_sent flag is already set in the DB
#   • a duplicate signal was sent within DEDUP_LOOKBACK_HOURS

import requests

import config
import journal
from utils import log_info, log_warn


# ── Public entry point ────────────────────────────────────────────────────────

def maybe_send_alert(signal_id: int, sig: dict, score: int,
                     htf_bias: str, vol_confirmed: bool) -> None:
    """
    Decide whether to send a Telegram alert and, if so, send it.

    Parameters
    ----------
    signal_id     : DB row id of the saved signal
    sig           : signal dict from signals.py
    score         : scoring.score_signal() result
    htf_bias      : 'bullish' | 'bearish' | 'range' | ''
    vol_confirmed : whether volume spike was detected
    """
    if not config.TELEGRAM_ENABLED:
        return

    if score < config.ALERT_SCORE_THRESHOLD:
        log_info(f"[ALERT] #{signal_id} score {score} < threshold "
                 f"{config.ALERT_SCORE_THRESHOLD} — skipped")
        return

    # Check if already sent (e.g. after restart)
    row = journal.get_signal_by_id(signal_id)
    if row and row["alert_sent"]:
        log_info(f"[SKIP] duplicate alert suppressed for #{signal_id}")
        return

    # Dedup: same symbol/tf/direction sent recently?
    recent_sent = _recently_alerted(sig["symbol"], sig["timeframe"], sig["direction"])
    if recent_sent:
        log_info(f"[SKIP] duplicate alert suppressed — "
                 f"similar signal already sent within {config.DEDUP_LOOKBACK_HOURS}h")
        return

    text = _format_message(sig, score, htf_bias, vol_confirmed)
    ok   = _send(text)

    if ok:
        journal.mark_alert_sent(signal_id)
        log_info(f"[ALERT] Telegram sent for {sig['symbol']} {sig['timeframe']} "
                 f"{sig['direction'].upper()}  (score {score})")
    else:
        log_warn(f"[ALERT] Telegram delivery failed for #{signal_id}")


# ── Message formatter ─────────────────────────────────────────────────────────

def _format_message(sig: dict, score: int,
                    htf_bias: str, vol_confirmed: bool) -> str:
    direction  = sig["direction"].upper()
    emoji      = "🟢" if direction == "LONG" else "🔴"
    htf_line   = f"✅ HTF aligned ({htf_bias})" if htf_bias in ("bullish", "bearish") \
                 else f"⚪ HTF neutral ({htf_bias})" if htf_bias == "range" \
                 else "❓ HTF unavailable"
    vol_line   = "✅ Volume spike confirmed" if vol_confirmed else "⚪ No volume spike"

    mid_entry  = (sig["entry_low"] + sig["entry_high"]) / 2
    risk       = abs(mid_entry - sig["stop"])
    rr         = round(abs(sig["tp"] - mid_entry) / risk, 2) if risk else 0.0

    msg = (
        f"🚨 <b>SMC Signal</b>\n"
        f"\n"
        f"<b>Symbol</b>:    {sig['symbol']}\n"
        f"<b>Timeframe</b>: {sig['timeframe']}\n"
        f"<b>Direction</b>: {emoji} {direction}\n"
        f"<b>Context</b>:   {sig.get('context','')}\n"
        f"<b>Score</b>:     {score}/100\n"
        f"\n"
        f"<b>Entry</b>:  {sig['entry_low']:.2f} – {sig['entry_high']:.2f}\n"
        f"<b>Stop</b>:   {sig['stop']:.2f}\n"
        f"<b>TP</b>:     {sig['tp']:.2f}\n"
        f"<b>RR</b>:     {rr:.2f}\n"
        f"\n"
        f"{htf_line}\n"
        f"{vol_line}\n"
        f"\n"
        f"<i>{sig.get('reason','')}</i>"
    )
    return msg


# ── Telegram API call ─────────────────────────────────────────────────────────

def _send(text: str) -> bool:
    """
    POST the message to Telegram.
    Returns True on success, False on any error.
    """
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    config.TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        log_warn(f"[ALERT] Telegram API error {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.RequestException as e:
        log_warn(f"[ALERT] Telegram request failed: {e}")
        return False


# ── Dedup check ───────────────────────────────────────────────────────────────

def _recently_alerted(symbol: str, timeframe: str, direction: str) -> bool:
    """
    Return True if an alert was already sent for the same symbol/tf/direction
    within DEDUP_LOOKBACK_HOURS.
    """
    rows = journal.get_recent_signals(
        symbol, timeframe, direction, config.DEDUP_LOOKBACK_HOURS
    )
    return any(r["alert_sent"] == 1 for r in rows)