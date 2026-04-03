# dashboard.py — Performance report: ASCII console + HTML file
#
# Usage:
#   python dashboard.py              # print ASCII report to console
#   python dashboard.py --html       # generate dashboard.html and open it
#   python dashboard.py --html --no-open   # generate without opening browser

import argparse
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import portfolio
import journal
from utils import log_info


# ── ASCII console report ───────────────────────────────────────────────────────

def print_report() -> None:
    s   = portfolio.get_stats()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bal_change = s["current_balance"] - s["starting_balance"]
    bal_sign   = "+" if bal_change >= 0 else ""
    pnl_sign   = "+" if s["total_pnl_usd"] >= 0 else ""

    sep  = "═" * 58
    sep2 = "─" * 58

    print(f"\n{sep}")
    print(f"  📊  SMC SCANNER — VIRTUAL PORTFOLIO REPORT")
    print(f"  {now}")
    print(sep)
    print(f"  Starting balance : ${s['starting_balance']:.2f}")
    print(f"  Current balance  : ${s['current_balance']:.2f}  "
          f"({bal_sign}${bal_change:.2f}  {pnl_sign}{s['total_pnl_pct']:.2f}%)")
    print(sep2)
    print(f"  Total signals    : {s['total_trades']}")
    print(f"  Triggered        : {s['triggered']}")
    print(f"  Won              : {s['won']}  ✅")
    print(f"  Lost             : {s['lost']}  ❌")
    print(f"  Expired (entry)  : {s['expired_after_entry']}  ⏱")
    print(sep2)
    print(f"  Win rate         : {s['win_rate']:.1f}%")
    print(f"  Avg win          : +${s['avg_win_usd']:.2f}")
    print(f"  Avg loss         : ${s['avg_loss_usd']:.2f}")
    print(f"  Avg RR (actual)  : {s['avg_rr_actual']:.2f}")
    print(f"  Best trade       : +${s['best_trade_usd']:.2f}")
    print(f"  Worst trade      : ${s['worst_trade_usd']:.2f}")
    print(f"  Profit factor    : {s['profit_factor']:.2f}")
    print(f"  Expectancy/trade : ${s['expectancy_usd']:.2f}")
    print(sep2)

    # Daily PnL table
    if s["daily_pnl"]:
        print(f"  {'Date':<12} {'Daily PnL':>10}  {'Balance EOD':>12}")
        print(f"  {'-'*38}")
        for day in s["daily_pnl"]:
            sign = "+" if day["pnl_usd"] >= 0 else ""
            bar  = _sparkbar(day["pnl_usd"], max_abs=max(
                abs(d["pnl_usd"]) for d in s["daily_pnl"]) or 1)
            print(f"  {day['date']:<12} {sign}${day['pnl_usd']:>8.2f}  "
                  f"${day['balance_eod']:>10.2f}  {bar}")

    print(sep)

    # Recent closed trades
    _print_recent_trades()


def _print_recent_trades(n: int = 10) -> None:
    with portfolio._connect() as con:
        rows = con.execute("""
            SELECT ps.*, s.score, s.ob_label
            FROM portfolio_snapshots ps
            LEFT JOIN signals s ON ps.signal_id = s.id
            WHERE ps.status IN ('won','lost','expired')
            ORDER BY ps.id DESC LIMIT ?
        """, (n,)).fetchall()

    if not rows:
        return

    print(f"\n  Recent closed trades (last {n}):")
    print(f"  {'#':<4} {'Symbol':<12} {'TF':<6} {'Dir':<6} "
          f"{'PnL $':>8} {'RR':>5} {'Status':<8} Score")
    print(f"  {'-'*62}")
    for r in rows:
        pnl   = r["pnl_usd"] or 0.0
        rr    = r["rr_actual"] or 0.0
        sign  = "+" if pnl >= 0 else ""
        emoji = "✅" if r["status"] == "won" else "❌" if r["status"] == "lost" else "⏱"
        score = r["score"] or 0
        print(f"  {r['signal_id']:<4} {r['symbol']:<12} {r['timeframe']:<6} "
              f"{r['direction'].upper():<6} "
              f"{sign}${pnl:>6.2f} {rr:>5.2f} {emoji} {r['status']:<6}  {score}")
    print()


def _sparkbar(value: float, max_abs: float, width: int = 10) -> str:
    """Simple text bar for daily PnL."""
    if max_abs == 0:
        return " " * width
    frac = abs(value) / max_abs
    filled = int(frac * width)
    char = "█" if value >= 0 else "▓"
    return (char * filled).ljust(width)


# ── HTML dashboard ─────────────────────────────────────────────────────────────

def generate_html(output_path: str = "dashboard.html") -> str:
    s   = portfolio.get_stats()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    bal_change = s["current_balance"] - s["starting_balance"]
    bal_color  = "#00e676" if bal_change >= 0 else "#ff4c4c"
    pnl_sign   = "+" if s["total_pnl_usd"] >= 0 else ""

    # Recent trades table rows
    with portfolio._connect() as con:
        trade_rows = con.execute("""
            SELECT ps.*, s.score, s.sweep_side, s.bos_type
            FROM portfolio_snapshots ps
            LEFT JOIN signals s ON ps.signal_id = s.id
            ORDER BY ps.id DESC LIMIT 30
        """).fetchall()

    trade_html = ""
    for r in trade_rows:
        pnl   = r["pnl_usd"] or 0.0
        rr    = r["rr_actual"] or 0.0
        sign  = "+" if pnl >= 0 else ""
        color = "#00e676" if pnl >= 0 else "#ff4c4c"
        emoji = "✅" if r["status"] == "won" else "❌" if r["status"] == "lost" else "⏱"
        score = r["score"] or 0
        closed = (r["closed_at"] or "")[:16].replace("T", " ")
        trade_html += f"""
        <tr>
            <td>#{r['signal_id']}</td>
            <td>{r['symbol']}</td>
            <td>{r['timeframe']}</td>
            <td class="{'long' if r['direction']=='long' else 'short'}">{r['direction'].upper()}</td>
            <td>${r['entry_price']:.2f}</td>
            <td>${r['exit_price']:.2f if r['exit_price'] else '—'}</td>
            <td style="color:{color};font-weight:bold">{sign}${pnl:.2f}</td>
            <td>{rr:.2f}R</td>
            <td>{emoji} {r['status']}</td>
            <td>{score}</td>
            <td>{closed}</td>
        </tr>"""

    # Daily PnL chart data for Chart.js
    labels  = [d["date"] for d in s["daily_pnl"]]
    pnl_pts = [d["pnl_usd"] for d in s["daily_pnl"]]
    bal_pts = [d["balance_eod"] for d in s["daily_pnl"]]
    bar_colors = ['"#00e676"' if v >= 0 else '"#ff4c4c"' for v in pnl_pts]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SMC Scanner — Portfolio Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; color: #e6edf3; font-family: 'Courier New', monospace;
         font-size: 14px; padding: 24px; }}
  h1   {{ font-size: 22px; color: #58a6ff; margin-bottom: 4px; }}
  .sub {{ color: #8b949e; font-size: 12px; margin-bottom: 24px; }}

  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 16px; margin-bottom: 28px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 16px; }}
  .card .label {{ color: #8b949e; font-size: 11px; text-transform: uppercase;
                  letter-spacing: 0.05em; margin-bottom: 6px; }}
  .card .value {{ font-size: 22px; font-weight: bold; }}
  .card .value.green  {{ color: #00e676; }}
  .card .value.red    {{ color: #ff4c4c; }}
  .card .value.blue   {{ color: #58a6ff; }}
  .card .value.yellow {{ color: #ffd700; }}

  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
             margin-bottom: 28px; }}
  .chart-box {{ background: #161b22; border: 1px solid #30363d;
                border-radius: 8px; padding: 16px; }}
  .chart-box h3 {{ color: #8b949e; font-size: 12px; margin-bottom: 12px;
                   text-transform: uppercase; }}

  table {{ width: 100%; border-collapse: collapse; background: #161b22;
           border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }}
  th    {{ background: #21262d; color: #8b949e; font-size: 11px; padding: 10px 12px;
           text-align: left; text-transform: uppercase; letter-spacing: 0.05em; }}
  td    {{ padding: 9px 12px; border-top: 1px solid #21262d; }}
  tr:hover td {{ background: #1c2128; }}
  .long  {{ color: #00e676; }}
  .short {{ color: #ff4c4c; }}
  h2    {{ color: #58a6ff; margin-bottom: 14px; font-size: 15px; }}
</style>
</head>
<body>

<h1>📊 SMC Scanner — Virtual Portfolio</h1>
<div class="sub">Risk per trade: {config_risk()}% &nbsp;|&nbsp; Starting balance: ${s['starting_balance']:.2f} &nbsp;|&nbsp; Updated: {now}</div>

<div class="grid">
  <div class="card">
    <div class="label">Current Balance</div>
    <div class="value {'green' if bal_change >= 0 else 'red'}">${s['current_balance']:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Total P&amp;L</div>
    <div class="value {'green' if s['total_pnl_usd'] >= 0 else 'red'}">{pnl_sign}${s['total_pnl_usd']:.2f} ({pnl_sign}{s['total_pnl_pct']:.2f}%)</div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value blue">{s['win_rate']:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">Profit Factor</div>
    <div class="value {'green' if s['profit_factor'] >= 1 else 'red'}">{s['profit_factor']:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Expectancy</div>
    <div class="value {'green' if s['expectancy_usd'] >= 0 else 'red'}">{'+' if s['expectancy_usd'] >= 0 else ''}${s['expectancy_usd']:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Avg RR (actual)</div>
    <div class="value yellow">{s['avg_rr_actual']:.2f}R</div>
  </div>
  <div class="card">
    <div class="label">Won / Lost</div>
    <div class="value">&nbsp;✅ {s['won']} &nbsp; ❌ {s['lost']}</div>
  </div>
  <div class="card">
    <div class="label">Total Signals</div>
    <div class="value blue">{s['total_trades']}</div>
  </div>
</div>

<div class="charts">
  <div class="chart-box">
    <h3>Daily P&amp;L ($)</h3>
    <canvas id="dailyPnl" height="180"></canvas>
  </div>
  <div class="chart-box">
    <h3>Balance Curve ($)</h3>
    <canvas id="balanceCurve" height="180"></canvas>
  </div>
</div>

<h2>Trade Log (last 30)</h2>
<table>
  <thead>
    <tr>
      <th>#</th><th>Symbol</th><th>TF</th><th>Dir</th>
      <th>Entry</th><th>Exit</th>
      <th>P&amp;L</th><th>RR</th><th>Status</th><th>Score</th><th>Closed</th>
    </tr>
  </thead>
  <tbody>{trade_html}</tbody>
</table>

<script>
const labels   = {labels};
const pnlData  = {pnl_pts};
const balData  = {bal_pts};
const barColors = [{','.join(bar_colors)}];

new Chart(document.getElementById('dailyPnl'), {{
  type: 'bar',
  data: {{ labels, datasets: [{{
    data: pnlData,
    backgroundColor: barColors,
    borderRadius: 3,
  }}] }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
      y: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
    }}
  }}
}});

new Chart(document.getElementById('balanceCurve'), {{
  type: 'line',
  data: {{ labels, datasets: [{{
    data: balData,
    borderColor: '#58a6ff',
    backgroundColor: 'rgba(88,166,255,0.08)',
    fill: true,
    tension: 0.3,
    pointRadius: 4,
    pointBackgroundColor: '#58a6ff',
  }}] }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
      y: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
    }}
  }}
}});
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    log_info(f"[DASHBOARD] HTML saved → {output_path}")
    return output_path


def config_risk() -> str:
    try:
        import config as cfg
        return f"{cfg.RISK_PER_TRADE_PCT * 100:.1f}"
    except Exception:
        return "1.0"


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    import journal as j
    j.init_db()
    portfolio.init_portfolio_db()

    p = argparse.ArgumentParser(description="SMC Portfolio Dashboard")
    p.add_argument("--html",     action="store_true", help="Generate HTML dashboard")
    p.add_argument("--no-open",  action="store_true", help="Don't open browser")
    p.add_argument("--out",      default="dashboard.html")
    args = p.parse_args()

    print_report()

    if args.html:
        path = generate_html(args.out)
        if not args.no_open:
            webbrowser.open(f"file://{Path(path).resolve()}")


if __name__ == "__main__":
    main()