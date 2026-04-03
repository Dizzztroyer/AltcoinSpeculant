# daily_report.py — Midnight daily/weekly/monthly performance report
#
# Called automatically by scheduler at local midnight.
# Prints ASCII summary + sends Telegram message if enabled.
#
# Report sections:
#   1. Today's closed trades
#   2. Last 7 days
#   3. Last 30 days
#   4. All-time
#   5. Per-symbol breakdown (today)

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import portfolio
from utils import log_info, log_warn


# ── Public entry point ─────────────────────────────────────────────────────────

def run_daily_report() -> None:
    """Generate and send the midnight performance report."""
    local_now = _local_now()
    log_info(f"[REPORT] generating midnight report — {local_now.strftime('%Y-%m-%d %H:%M %Z')}")

    report_text = _build_report(local_now)

    # Print to console
    print(report_text)

    # Send to Telegram
    if config.TELEGRAM_ENABLED:
        _send_telegram(report_text)

    log_info("[REPORT] done")


# ── Report builder ─────────────────────────────────────────────────────────────

def _build_report(local_now: datetime) -> str:
    today_str = local_now.strftime("%Y-%m-%d")
    yesterday = (local_now.replace(hour=0, minute=0, second=0) -
                 __import__("datetime").timedelta(seconds=1)).strftime("%Y-%m-%d")

    # Stats for different periods
    s_today  = portfolio.get_period_stats(days=1)
    s_week   = portfolio.get_period_stats(days=7)
    s_month  = portfolio.get_period_stats(days=30)
    s_all    = portfolio.get_stats()

    lines = []
    SEP   = "═" * 52
    sep2  = "─" * 52

    lines.append(SEP)
    lines.append(f"  📊 DAILY REPORT  —  {yesterday}")
    lines.append(f"  Virtual portfolio  |  Risk: {config.RISK_PER_TRADE_PCT*100:.0f}% / trade")
    lines.append(SEP)

    # ── Today ──────────────────────────────────────────────────────────────────
    lines.append(_period_block("TODAY", s_today, prefix="  "))
    lines.append(sep2)

    # ── Week ───────────────────────────────────────────────────────────────────
    lines.append(_period_block("LAST 7 DAYS", s_week, prefix="  "))
    lines.append(sep2)

    # ── Month ──────────────────────────────────────────────────────────────────
    lines.append(_period_block("LAST 30 DAYS", s_month, prefix="  "))
    lines.append(sep2)

    # ── All time ───────────────────────────────────────────────────────────────
    lines.append(_period_block("ALL TIME", s_all, prefix="  ", show_balance=True))
    lines.append(sep2)

    # ── Best / worst day of the week ───────────────────────────────────────────
    if s_week["daily_pnl"]:
        best  = max(s_week["daily_pnl"], key=lambda d: d["pnl_usd"])
        worst = min(s_week["daily_pnl"], key=lambda d: d["pnl_usd"])
        lines.append(f"  Best day  : {best['date']}  "
                     f"+${best['pnl_usd']:.2f}  "
                     f"({best['won']}W/{best['lost']}L)")
        lines.append(f"  Worst day : {worst['date']}  "
                     f"${worst['pnl_usd']:.2f}  "
                     f"({worst['won']}W/{worst['lost']}L)")
        lines.append(sep2)

    # ── Per-symbol today ───────────────────────────────────────────────────────
    sym_block = _symbol_breakdown(days=1)
    if sym_block:
        lines.append("  BY SYMBOL (today):")
        lines += sym_block
        lines.append(sep2)

    # ── Daily PnL bar (last 7) ─────────────────────────────────────────────────
    if s_week["daily_pnl"]:
        lines.append("  DAILY P&L (last 7 days):")
        max_abs = max(abs(d["pnl_usd"]) for d in s_week["daily_pnl"]) or 1
        for day in s_week["daily_pnl"]:
            sign = "+" if day["pnl_usd"] >= 0 else ""
            bar  = _bar(day["pnl_usd"], max_abs)
            lines.append(f"  {day['date']}  {sign}${day['pnl_usd']:>7.2f}  {bar}  "
                         f"✅{day['won']} ❌{day['lost']}")

    lines.append(SEP)
    return "\n".join(lines)


def _period_block(label: str, s: dict,
                  prefix: str = "",
                  show_balance: bool = False) -> str:
    pnl   = s["total_pnl_usd"]
    sign  = "+" if pnl >= 0 else ""
    emoji = "🟢" if pnl >= 0 else "🔴"
    out   = [f"{prefix}{label}"]

    if show_balance:
        bal_change = s["current_balance"] - s["starting_balance"]
        bal_sign   = "+" if bal_change >= 0 else ""
        out.append(f"{prefix}  Balance : ${s['current_balance']:.2f}  "
                   f"({bal_sign}${bal_change:.2f})")

    out.append(f"{prefix}  P&L     : {emoji} {sign}${pnl:.2f}  ({sign}{s['total_pnl_pct']:.2f}%)")
    out.append(f"{prefix}  Trades  : {s['triggered']} closed  "
               f"✅{s['won']}W  ❌{s['lost']}L  ⏱{s['expired_after_entry']}exp")

    if s["triggered"] > 0:
        out.append(f"{prefix}  Win rate: {s['win_rate']:.1f}%  "
                   f"PF: {s['profit_factor']:.2f}  "
                   f"Expect: {sign}${s['expectancy_usd']:.2f}")
        out.append(f"{prefix}  Avg RR  : {s['avg_rr_actual']:.2f}  "
                   f"Best: +${s['best_trade_usd']:.2f}  "
                   f"Worst: ${s['worst_trade_usd']:.2f}")

    return "\n".join(out)


def _symbol_breakdown(days: int = 1) -> list[str]:
    """Per-symbol win/loss/PnL for the period."""
    import sqlite3
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        con = portfolio._connect()
        rows = con.execute("""
            SELECT symbol, status, pnl_usd
            FROM portfolio_snapshots
            WHERE status IN ('won','lost','expired')
              AND closed_at >= ?
            ORDER BY symbol
        """, (cutoff,)).fetchall()
        con.close()
    except Exception:
        return []

    if not rows:
        return ["  (no closed trades)"]

    by_sym: dict[str, dict] = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in by_sym:
            by_sym[sym] = {"won": 0, "lost": 0, "pnl": 0.0}
        by_sym[sym]["pnl"] += r["pnl_usd"] or 0.0
        if r["status"] == "won":
            by_sym[sym]["won"] += 1
        elif r["status"] == "lost":
            by_sym[sym]["lost"] += 1

    lines = []
    for sym, d in sorted(by_sym.items()):
        sign = "+" if d["pnl"] >= 0 else ""
        lines.append(f"    {sym:<14}  {sign}${d['pnl']:>7.2f}  "
                     f"✅{d['won']} ❌{d['lost']}")
    return lines


def _bar(value: float, max_abs: float, width: int = 12) -> str:
    frac   = abs(value) / max_abs if max_abs else 0
    filled = int(frac * width)
    char   = "█" if value >= 0 else "▓"
    return (char * filled).ljust(width)


def _local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(config.LOCAL_TIMEZONE))
    except Exception:
        return datetime.now(timezone.utc)


# ── Telegram delivery ─────────────────────────────────────────────────────────

def _send_telegram(text: str) -> None:
    """Send report as HTML-escaped Telegram message, split if too long."""
    try:
        import requests
        # Telegram max message length = 4096 chars
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        url    = (f"https://api.telegram.org/bot"
                  f"{config.TELEGRAM_BOT_TOKEN}/sendMessage")
        for chunk in chunks:
            resp = requests.post(url, json={
                "chat_id":    config.TELEGRAM_CHAT_ID,
                "text":       f"<pre>{chunk}</pre>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=15)
            if resp.status_code != 200:
                log_warn(f"[REPORT] Telegram error {resp.status_code}")
    except Exception as exc:
        log_warn(f"[REPORT] Telegram send failed: {exc}")


# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import journal
    journal.init_db()
    portfolio.init_portfolio_db()
    run_daily_report()