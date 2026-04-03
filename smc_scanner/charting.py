# charting.py — Plotly chart: candles + OB boxes + FVG bands + signal levels

import tempfile
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from liquidity import LiquidityZone, SweepEvent
from structure import find_swings


def _build_figure(df: pd.DataFrame,
                  symbol: str,
                  timeframe: str,
                  zones: list[LiquidityZone],
                  sweeps: list[SweepEvent],
                  signal: dict | None = None,
                  obs=None,
                  fvgs=None) -> go.Figure:

    df_s = find_swings(df)
    fig  = make_subplots(rows=2, cols=1, row_heights=[0.8, 0.2],
                         shared_xaxes=True, vertical_spacing=0.02)

    # ── Candlestick ───────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df_s["timestamp"],
        open=df_s["open"], high=df_s["high"],
        low=df_s["low"],   close=df_s["close"],
        name="Price",
        increasing_line_color="#00b386",
        decreasing_line_color="#e63946",
    ), row=1, col=1)

    # ── Volume ────────────────────────────────────────────────────────────────
    vol_colors = ["#00b386" if c >= o else "#e63946"
                  for c, o in zip(df_s["close"], df_s["open"])]
    fig.add_trace(go.Bar(
        x=df_s["timestamp"], y=df_s["volume"],
        marker_color=vol_colors, opacity=0.5, name="Volume",
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

    # ── Liquidity zone lines ──────────────────────────────────────────────────
    liq_colors = {
        "high":       "rgba(255,80,80,0.50)",
        "low":        "rgba(80,200,80,0.50)",
        "equal_high": "rgba(255,160,0,0.65)",
        "equal_low":  "rgba(0,210,210,0.65)",
    }
    for zone in zones[-15:]:
        if zone.candle_idx not in df_s.index:
            continue
        t0  = df_s.loc[zone.candle_idx, "timestamp"]
        t1  = df_s["timestamp"].iloc[-1]
        col = liq_colors.get(zone.zone_type, "white")
        fig.add_shape(type="line", x0=t0, x1=t1,
                      y0=zone.level, y1=zone.level,
                      line=dict(color=col, width=1.2,
                                dash="dot" if "equal" in zone.zone_type else "dash"),
                      row=1, col=1)
        fig.add_annotation(x=t1, y=zone.level, text=zone.label(),
                           showarrow=False, font=dict(size=8, color=col),
                           xanchor="right", row=1, col=1)

    # ── Sweep highlights ──────────────────────────────────────────────────────
    for sweep in sweeps:
        if sweep.candle_idx not in df_s.index:
            continue
        t = df_s.loc[sweep.candle_idx, "timestamp"]
        fig.add_vrect(x0=t, x1=t, line_width=3, line_color="gold",
                      annotation_text="SWEEP", annotation_position="top left",
                      row=1, col=1)

    # ── Order Block boxes ─────────────────────────────────────────────────────
    if obs:
        t_end = df_s["timestamp"].iloc[-1]
        for ob in obs[-10:]:   # show last 10 OBs
            if ob.candle_idx not in df_s.index:
                continue
            t_start = df_s.loc[ob.candle_idx, "timestamp"]

            if ob.ob_type == "bullish":
                fill = ("rgba(0,230,118,0.12)" if not ob.mitigated
                        else "rgba(0,230,118,0.04)")
                border = ("rgba(0,230,118,0.7)" if not ob.mitigated
                          else "rgba(0,230,118,0.25)")
            else:
                fill = ("rgba(255,76,76,0.12)" if not ob.mitigated
                        else "rgba(255,76,76,0.04)")
                border = ("rgba(255,76,76,0.7)" if not ob.mitigated
                          else "rgba(255,76,76,0.25)")

            # OB box
            fig.add_shape(
                type="rect", x0=t_start, x1=t_end,
                y0=ob.low, y1=ob.high,
                fillcolor=fill,
                line=dict(color=border, width=1.5),
                row=1, col=1,
            )
            mit_tag = " [MIT]" if ob.mitigated else ""
            fvg_tag = " +FVG"  if ob.has_fvg   else ""
            fig.add_annotation(
                x=t_start, y=ob.high,
                text=f"{ob.ob_type[:4].upper()} OB{fvg_tag}{mit_tag}",
                showarrow=False,
                font=dict(size=8, color=border),
                xanchor="left",
                row=1, col=1,
            )

            # FVG band inside OB
            if ob.has_fvg:
                fvg_fill = ("rgba(255,214,0,0.18)" if not ob.mitigated
                            else "rgba(255,214,0,0.06)")
                fig.add_shape(
                    type="rect", x0=t_start, x1=t_end,
                    y0=ob.fvg_low, y1=ob.fvg_high,
                    fillcolor=fvg_fill,
                    line=dict(color="rgba(255,214,0,0.6)", width=1, dash="dot"),
                    row=1, col=1,
                )

    # ── Standalone FVGs (not attached to OBs) ────────────────────────────────
    if fvgs:
        shown = 0
        t_end = df_s["timestamp"].iloc[-1]
        for fvg in reversed(fvgs):
            if fvg.filled or shown >= 5:
                continue
            if fvg.candle_idx not in df_s.index:
                continue
            t_start = df_s.loc[fvg.candle_idx, "timestamp"]
            col     = ("rgba(0,255,150,0.08)" if fvg.fvg_type == "bullish"
                       else "rgba(255,100,100,0.08)")
            fig.add_shape(
                type="rect", x0=t_start, x1=t_end,
                y0=fvg.gap_low, y1=fvg.gap_high,
                fillcolor=col,
                line=dict(color=col.replace("0.08", "0.4"), width=1, dash="dash"),
                row=1, col=1,
            )
            shown += 1

    # ── Signal entry / SL / TP ────────────────────────────────────────────────
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
        ob_note = f"  |  OB+FVG" if signal.get("ob_has_fvg") else \
                  f"  |  OB"     if signal.get("ob_label")   else ""
        title += f"  {arrow} {signal['direction'].upper()}{ob_note}"

    fig.update_layout(
        title=title,
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        showlegend=True,
        height=780,
        font=dict(family="monospace"),
        paper_bgcolor="#131722",
        plot_bgcolor="#131722",
    )
    fig.update_yaxes(title_text="Price",  row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    return fig


# ── Public: interactive ────────────────────────────────────────────────────────

def draw_chart(df, symbol, timeframe, zones, sweeps,
               signal=None, obs=None, fvgs=None):
    fig = _build_figure(df, symbol, timeframe, zones, sweeps, signal, obs, fvgs)
    fig.show()


# ── Public: PNG for Telegram ───────────────────────────────────────────────────

def render_chart_image(df, symbol, timeframe, zones, sweeps,
                       signal=None, obs=None, fvgs=None,
                       width=1400, height=800) -> str | None:
    """Render chart to PNG. Returns temp file path, or None on failure."""
    try:
        import kaleido  # noqa
    except ImportError:
        from utils import log_warn
        log_warn("[CHART] kaleido not installed — pip install kaleido")
        return None
    try:
        fig  = _build_figure(df, symbol, timeframe, zones, sweeps, signal, obs, fvgs)
        safe = symbol.replace("/", "_")
        tmp  = tempfile.NamedTemporaryFile(
            suffix=".png", prefix=f"smc_{safe}_{timeframe}_", delete=False)
        tmp.close()
        fig.write_image(tmp.name, width=width, height=height, scale=2)
        return tmp.name
    except Exception as exc:
        from utils import log_warn
        log_warn(f"[CHART] PNG export failed: {exc}")
        return None