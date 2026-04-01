# charting.py — Plotly chart with candles, swing points, liquidity zones, sweeps

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from liquidity import LiquidityZone, SweepEvent
from structure import find_swings


def draw_chart(df: pd.DataFrame,
               symbol: str,
               timeframe: str,
               zones: list[LiquidityZone],
               sweeps: list[SweepEvent],
               signal: dict | None = None) -> None:
    """
    Build and show an interactive Plotly candlestick chart annotated with:
    • Swing highs / lows
    • Liquidity zones (horizontal lines)
    • Sweep candles (markers)
    • Signal entry / SL / TP bands (if provided)
    """
    df_s = find_swings(df)

    fig = make_subplots(rows=2, cols=1, row_heights=[0.8, 0.2],
                        shared_xaxes=True, vertical_spacing=0.02)

    # ── Candlestick ────────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df_s["timestamp"],
        open=df_s["open"],
        high=df_s["high"],
        low=df_s["low"],
        close=df_s["close"],
        name="Price",
        increasing_line_color="#00b386",
        decreasing_line_color="#e63946",
    ), row=1, col=1)

    # ── Volume bars ───────────────────────────────────────────────────────────
    colors = [
        "#00b386" if c >= o else "#e63946"
        for c, o in zip(df_s["close"], df_s["open"])
    ]
    fig.add_trace(go.Bar(
        x=df_s["timestamp"],
        y=df_s["volume"],
        marker_color=colors,
        opacity=0.6,
        name="Volume",
    ), row=2, col=1)

    # ── Swing highs ───────────────────────────────────────────────────────────
    sh = df_s[df_s["swing_high"]]
    fig.add_trace(go.Scatter(
        x=sh["timestamp"], y=sh["high"],
        mode="markers",
        marker=dict(symbol="triangle-down", color="red", size=8),
        name="Swing High",
    ), row=1, col=1)

    # ── Swing lows ────────────────────────────────────────────────────────────
    sl = df_s[df_s["swing_low"]]
    fig.add_trace(go.Scatter(
        x=sl["timestamp"], y=sl["low"],
        mode="markers",
        marker=dict(symbol="triangle-up", color="lime", size=8),
        name="Swing Low",
    ), row=1, col=1)

    # ── Liquidity zone lines ───────────────────────────────────────────────────
    color_map = {
        "high":       "rgba(255,80,80,0.45)",
        "low":        "rgba(80,200,80,0.45)",
        "equal_high": "rgba(255,140,0,0.6)",
        "equal_low":  "rgba(0,200,200,0.6)",
    }
    for zone in zones[-20:]:    # show last 20 to keep chart readable
        if zone.candle_idx >= len(df_s):
            continue
        t_start = df_s.loc[zone.candle_idx, "timestamp"] if zone.candle_idx in df_s.index else df_s["timestamp"].iloc[0]
        t_end   = df_s["timestamp"].iloc[-1]
        col     = color_map.get(zone.zone_type, "white")
        dash    = "dot" if "equal" in zone.zone_type else "dash"

        fig.add_shape(
            type="line",
            x0=t_start, x1=t_end,
            y0=zone.level, y1=zone.level,
            line=dict(color=col, width=1.5, dash=dash),
            row=1, col=1,
        )
        fig.add_annotation(
            x=t_end, y=zone.level,
            text=zone.label(),
            showarrow=False,
            font=dict(size=9, color=col),
            xanchor="right",
            row=1, col=1,
        )

    # ── Sweep candle highlights ────────────────────────────────────────────────
    for sweep in sweeps:
        if sweep.candle_idx not in df_s.index:
            continue
        t = df_s.loc[sweep.candle_idx, "timestamp"]
        fig.add_vrect(
            x0=t, x1=t,
            line_width=3,
            line_color="gold",
            annotation_text="SWEEP",
            annotation_position="top left",
            row=1, col=1,
        )

    # ── Signal entry / SL / TP ────────────────────────────────────────────────
    if signal:
        t_start = df_s["timestamp"].iloc[-20]
        t_end   = df_s["timestamp"].iloc[-1]

        entry_color = "rgba(0,180,255,0.20)"
        sl_color    = "rgba(255,60,60,0.20)"
        tp_color    = "rgba(0,230,115,0.20)"

        # Entry band
        fig.add_hrect(
            y0=signal["entry_low"], y1=signal["entry_high"],
            fillcolor=entry_color, line_width=0,
            annotation_text="ENTRY", annotation_position="right",
            row=1, col=1,
        )
        # SL line
        fig.add_hline(
            y=signal["stop"],
            line=dict(color="red", dash="dash", width=2),
            annotation_text=f"SL {signal['stop']:.2f}",
            annotation_position="right",
            row=1, col=1,
        )
        # TP line
        fig.add_hline(
            y=signal["tp"],
            line=dict(color="lime", dash="dash", width=2),
            annotation_text=f"TP {signal['tp']:.2f}",
            annotation_position="right",
            row=1, col=1,
        )

    # ── Layout ────────────────────────────────────────────────────────────────
    title = f"{symbol}  [{timeframe}]"
    if signal:
        title += f"  ▶ {signal['direction'].upper()} SIGNAL"

    fig.update_layout(
        title=title,
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        showlegend=True,
        height=750,
        font=dict(family="monospace"),
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    fig.show()