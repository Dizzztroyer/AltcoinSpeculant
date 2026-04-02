# charting.py — Plotly chart: interactive display + PNG export for Telegram

import os
import tempfile
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from liquidity import LiquidityZone, SweepEvent
from structure import find_swings


# ── Internal figure builder (shared by both display and export) ────────────────

def _build_figure(df: pd.DataFrame,
                  symbol: str,
                  timeframe: str,
                  zones: list[LiquidityZone],
                  sweeps: list[SweepEvent],
                  signal: dict | None = None) -> go.Figure:

    df_s = find_swings(df)

    fig = make_subplots(rows=2, cols=1, row_heights=[0.8, 0.2],
                        shared_xaxes=True, vertical_spacing=0.02)

    # ── Candlestick ────────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df_s["timestamp"],
        open=df_s["open"], high=df_s["high"],
        low=df_s["low"],   close=df_s["close"],
        name="Price",
        increasing_line_color="#00b386",
        decreasing_line_color="#e63946",
    ), row=1, col=1)

    # ── Volume bars ───────────────────────────────────────────────────────────
    vol_colors = [
        "#00b386" if c >= o else "#e63946"
        for c, o in zip(df_s["close"], df_s["open"])
    ]
    fig.add_trace(go.Bar(
        x=df_s["timestamp"], y=df_s["volume"],
        marker_color=vol_colors, opacity=0.6, name="Volume",
    ), row=2, col=1)

    # ── Swing highs / lows ────────────────────────────────────────────────────
    sh = df_s[df_s["swing_high"]]
    fig.add_trace(go.Scatter(
        x=sh["timestamp"], y=sh["high"], mode="markers",
        marker=dict(symbol="triangle-down", color="#ff4c4c", size=9),
        name="Swing High",
    ), row=1, col=1)

    sl = df_s[df_s["swing_low"]]
    fig.add_trace(go.Scatter(
        x=sl["timestamp"], y=sl["low"], mode="markers",
        marker=dict(symbol="triangle-up", color="#00e676", size=9),
        name="Swing Low",
    ), row=1, col=1)

    # ── Liquidity zones ───────────────────────────────────────────────────────
    color_map = {
        "high":       "rgba(255,80,80,0.50)",
        "low":        "rgba(80,200,80,0.50)",
        "equal_high": "rgba(255,160,0,0.65)",
        "equal_low":  "rgba(0,210,210,0.65)",
    }
    for zone in zones[-20:]:
        if zone.candle_idx not in df_s.index:
            continue
        t_start = df_s.loc[zone.candle_idx, "timestamp"]
        t_end   = df_s["timestamp"].iloc[-1]
        col     = color_map.get(zone.zone_type, "white")
        dash    = "dot" if "equal" in zone.zone_type else "dash"
        fig.add_shape(
            type="line", x0=t_start, x1=t_end,
            y0=zone.level, y1=zone.level,
            line=dict(color=col, width=1.5, dash=dash),
            row=1, col=1,
        )
        fig.add_annotation(
            x=t_end, y=zone.level, text=zone.label(),
            showarrow=False, font=dict(size=9, color=col),
            xanchor="right", row=1, col=1,
        )

    # ── Sweep candle highlights ───────────────────────────────────────────────
    for sweep in sweeps:
        if sweep.candle_idx not in df_s.index:
            continue
        t = df_s.loc[sweep.candle_idx, "timestamp"]
        fig.add_vrect(
            x0=t, x1=t, line_width=3, line_color="gold",
            annotation_text="SWEEP", annotation_position="top left",
            row=1, col=1,
        )

    # ── Signal levels ─────────────────────────────────────────────────────────
    if signal:
        fig.add_hrect(
            y0=signal["entry_low"], y1=signal["entry_high"],
            fillcolor="rgba(0,180,255,0.18)", line_width=0,
            annotation_text="ENTRY", annotation_position="right",
            row=1, col=1,
        )
        fig.add_hline(
            y=signal["stop"],
            line=dict(color="#ff4c4c", dash="dash", width=2),
            annotation_text=f"SL {signal['stop']:.2f}",
            annotation_position="right", row=1, col=1,
        )
        fig.add_hline(
            y=signal["tp"],
            line=dict(color="#00e676", dash="dash", width=2),
            annotation_text=f"TP {signal['tp']:.2f}",
            annotation_position="right", row=1, col=1,
        )

    # ── Layout ────────────────────────────────────────────────────────────────
    title = f"{symbol}  [{timeframe}]"
    if signal:
        arrow = "🟢" if signal["direction"] == "long" else "🔴"
        title += f"  {arrow} {signal['direction'].upper()}"

    fig.update_layout(
        title=title,
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        showlegend=True,
        height=750,
        font=dict(family="monospace"),
        paper_bgcolor="#131722",
        plot_bgcolor="#131722",
    )
    fig.update_yaxes(title_text="Price",  row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    return fig


# ── Public: interactive browser chart ─────────────────────────────────────────

def draw_chart(df: pd.DataFrame,
               symbol: str,
               timeframe: str,
               zones: list[LiquidityZone],
               sweeps: list[SweepEvent],
               signal: dict | None = None) -> None:
    """Open an interactive Plotly chart in the browser."""
    fig = _build_figure(df, symbol, timeframe, zones, sweeps, signal)
    fig.show()


# ── Public: render PNG for Telegram ───────────────────────────────────────────

def render_chart_image(df: pd.DataFrame,
                       symbol: str,
                       timeframe: str,
                       zones: list[LiquidityZone],
                       sweeps: list[SweepEvent],
                       signal: dict | None = None,
                       width: int = 1400,
                       height: int = 800) -> str | None:
    """
    Render the chart to a PNG file and return the file path.

    Returns None if kaleido is not installed or export fails.

    Requires:  pip install kaleido
    The PNG is saved to a temp file — caller is responsible for deleting it
    after upload (use Path(path).unlink()).
    """
    try:
        import kaleido  # noqa: F401 — just to check it's installed
    except ImportError:
        from utils import log_warn
        log_warn("[CHART] kaleido not installed — pip install kaleido")
        return None

    try:
        fig  = _build_figure(df, symbol, timeframe, zones, sweeps, signal)
        safe = symbol.replace("/", "_")
        tmp  = tempfile.NamedTemporaryFile(
            suffix=".png",
            prefix=f"smc_{safe}_{timeframe}_",
            delete=False,
        )
        tmp.close()
        fig.write_image(tmp.name, width=width, height=height, scale=2)
        return tmp.name
    except Exception as exc:
        from utils import log_warn
        log_warn(f"[CHART] PNG export failed: {exc}")
        return None