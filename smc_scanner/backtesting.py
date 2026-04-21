# backtesting.py — Multi-model SMC backtest framework
#
# Architecture:
#   ModelConfig   — dataclass defining a strategy variant
#   MODEL_REGISTRY — dict of all registered models
#   BacktestEngine — walks candles, runs pipeline per model, collects trades
#   BacktestResult — per-model stats + trade log
#   run_all()     — loops over models, merges results
#   HTML report   — comparison table + equity curves + per-model detail
#
# Add a new model: add a ModelConfig entry to MODEL_REGISTRY. Done.
#
# Usage:
#   python backtesting.py --days 180 --html
#   python backtesting.py --models A,B,C --days 90 --html
#   python backtesting.py --symbols BTC/USDT,ETH/USDT --timeframes 1h,4h

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time as time_mod
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import config
from datafeed import fetch_ohlcv
from utils import log_info, log_warn, log_error


# ══════════════════════════════════════════════════════════════════════════════
# MODEL CONFIG
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ModelConfig:
    """
    Defines one strategy variant.
    The engine passes this to the signal + confirmation pipeline
    instead of reading from config.py — allowing multiple models
    to run on the same data without restarting.
    """
    id:                    str
    name:                  str
    description:           str

    # ── Entry requirements ────────────────────────────────────────────────────
    allow_fvg_only:        bool  = False   # allow FVG without OB as entry
    require_ob:            bool  = True    # require valid OB
    require_fresh_ob:      bool  = False   # OB must not be previously mitigated
    require_first_retest:  bool  = False   # only first touch of OB

    # ── Structural requirements ───────────────────────────────────────────────
    require_displacement:  bool  = False   # BOS candle must be strong impulse
    require_bos_quality:   bool  = False   # BOS must close beyond structure
    max_bars_after_bos:    int   = 999     # max candles between BOS and current

    # ── Zone requirements ─────────────────────────────────────────────────────
    allow_equilibrium:     bool  = True    # allow equilibrium zone entries
    premium_discount_only: bool  = False   # strict: short=premium, long=discount only
    pd_discount_level:     float = 0.40
    pd_premium_level:      float = 0.60

    # ── Confirmation thresholds ───────────────────────────────────────────────
    min_score:             int   = 75
    min_rr_to_liq:         float = 0.0    # min RR to liquidity target (0=disabled)

    # ── HTF ───────────────────────────────────────────────────────────────────
    use_htf_bias:          bool  = True
    block_htf_range:       bool  = True   # block when HTF is range
    block_htf_opposing:    bool  = True   # block when HTF opposes

    # ── Mandatory flags (mirrors confirmation.py) ─────────────────────────────
    htf_mandatory:         bool  = True
    sweep_mandatory:       bool  = True
    bos_mandatory:         bool  = True
    ob_mandatory:          bool  = True
    pd_mandatory:          bool  = True

    # ── Sweep quality ─────────────────────────────────────────────────────────
    sweep_wick_dominance:  float = 0.55

    # ── BOS quality ───────────────────────────────────────────────────────────
    bos_min_body_atr:      float = 0.8

    # ── Expired treatment ─────────────────────────────────────────────────────
    # "zero"   = expired always counts as 0R (conservative, recommended)
    # "market" = mark-to-market at last close price
    expired_treatment:     str   = "zero"


# ══════════════════════════════════════════════════════════════════════════════
# MODEL REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

MODEL_REGISTRY: dict[str, ModelConfig] = {

    "A": ModelConfig(
        id="A", name="Baseline",
        description="Current live logic with corrected expired accounting. "
                    "FVG-only blocked, P/D mandatory, HTF range blocked.",
        allow_fvg_only=False,
        require_ob=True,
        allow_equilibrium=True,
        premium_discount_only=False,
        min_score=75,
        block_htf_range=True,
        pd_mandatory=True,
        expired_treatment="zero",
    ),

    "B": ModelConfig(
        id="B", name="Strict OB",
        description="Only fresh unmitigated OBs, first retest only, "
                    "no equilibrium, higher score bar.",
        allow_fvg_only=False,
        require_ob=True,
        require_fresh_ob=True,
        require_first_retest=True,
        allow_equilibrium=False,
        premium_discount_only=True,
        min_score=82,
        block_htf_range=True,
        pd_mandatory=True,
        expired_treatment="zero",
    ),

    "C": ModelConfig(
        id="C", name="MTF Classic",
        description="4h HTF bias, 1h context, entry on 1h/4h. "
                    "Classic sweep→BOS→OB retest. FVG only as confluence inside OB.",
        allow_fvg_only=False,
        require_ob=True,
        require_displacement=True,
        allow_equilibrium=True,
        premium_discount_only=False,
        min_score=78,
        block_htf_range=True,
        block_htf_opposing=True,
        bos_min_body_atr=1.0,     # stronger displacement required
        expired_treatment="zero",
    ),

    "D": ModelConfig(
        id="D", name="P/D Array Strict",
        description="Long only in discount, short only in premium. "
                    "Equilibrium disabled. Min RR 2.5 to liquidity target.",
        allow_fvg_only=False,
        require_ob=True,
        allow_equilibrium=False,
        premium_discount_only=True,
        pd_discount_level=0.35,
        pd_premium_level=0.65,
        min_score=80,
        min_rr_to_liq=2.5,
        block_htf_range=True,
        pd_mandatory=True,
        expired_treatment="zero",
    ),

    "E": ModelConfig(
        id="E", name="Ultra Selective",
        description="Everything from D plus strong displacement, "
                    "BOS quality filter, max 10 bars after BOS. "
                    "Very few signals, maximum quality.",
        allow_fvg_only=False,
        require_ob=True,
        require_fresh_ob=True,
        require_first_retest=True,
        require_displacement=True,
        require_bos_quality=True,
        max_bars_after_bos=10,
        allow_equilibrium=False,
        premium_discount_only=True,
        pd_discount_level=0.33,
        pd_premium_level=0.67,
        min_score=85,
        min_rr_to_liq=2.0,
        bos_min_body_atr=1.2,
        sweep_wick_dominance=0.65,
        block_htf_range=True,
        pd_mandatory=True,
        expired_treatment="zero",
    ),

    "F": ModelConfig(
        id="F", name="Balanced",
        description="Middle ground between C and E. Strong but not extreme. "
                    "Fresh OB preferred, displacement required, equilibrium allowed.",
        allow_fvg_only=False,
        require_ob=True,
        require_fresh_ob=True,
        require_displacement=True,
        max_bars_after_bos=20,
        allow_equilibrium=True,
        premium_discount_only=False,
        min_score=80,
        min_rr_to_liq=0.0,
        bos_min_body_atr=1.0,
        sweep_wick_dominance=0.60,
        block_htf_range=True,
        pd_mandatory=True,
        expired_treatment="zero",
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# TRADE + RESULT DATACLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BacktestTrade:
    model_id:     str
    symbol:       str
    timeframe:    str
    direction:    str
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    signal_bar:   int
    entry_bar:    int | None
    exit_bar:     int | None
    exit_price:   float | None
    outcome:      str          # won | lost | expired | pending
    rr_planned:   float
    rr_actual:    float
    pnl_r:        float
    score:        int
    ob_label:     str
    pd_zone:      str
    htf_bias:     str
    mfe:          float
    mae:          float
    signal_time:  str
    reject_reason: str = ""


@dataclass
class BacktestResult:
    model:  ModelConfig
    trades: list[BacktestTrade] = field(default_factory=list)

    # ── Derived stats ─────────────────────────────────────────────────────────

    @property
    def decisive(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome in ("won", "lost")]

    @property
    def wins(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome == "won"]

    @property
    def losses(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome == "lost"]

    @property
    def expired(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.outcome == "expired"]

    @property
    def win_rate(self) -> float:
        d = len(self.decisive)
        return len(self.wins) / d * 100 if d else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.pnl_r for t in self.trades)

    @property
    def gross_win(self) -> float:
        return sum(t.pnl_r for t in self.wins)

    @property
    def gross_loss(self) -> float:
        return abs(sum(t.pnl_r for t in self.losses))

    @property
    def profit_factor(self) -> float:
        return self.gross_win / self.gross_loss if self.gross_loss > 0 else 0.0

    @property
    def expectancy(self) -> float:
        d = len(self.decisive)
        if not d:
            return 0.0
        return sum(t.pnl_r for t in self.decisive) / d

    @property
    def avg_r(self) -> float:
        pnls = [t.pnl_r for t in self.decisive]
        return sum(pnls) / len(pnls) if pnls else 0.0

    @property
    def median_r(self) -> float:
        pnls = sorted(t.pnl_r for t in self.decisive)
        return statistics.median(pnls) if pnls else 0.0

    @property
    def avg_rr_planned(self) -> float:
        vals = [t.rr_planned for t in self.decisive if t.rr_planned > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def avg_rr_actual(self) -> float:
        vals = [t.rr_actual for t in self.decisive]
        return sum(vals) / len(vals) if vals else 0.0

    def equity_curve(self, start: float = 100.0, risk: float = 0.01
                     ) -> list[float]:
        """Simulated balance curve at fixed fractional risk."""
        bal = start
        curve = [bal]
        for t in self.trades:
            bal += bal * risk * t.pnl_r
            curve.append(round(bal, 4))
        return curve

    def max_drawdown(self, start: float = 100.0, risk: float = 0.01) -> float:
        curve = self.equity_curve(start, risk)
        if not curve:
            return 0.0
        peak = curve[0]
        max_dd = 0.0
        for val in curve:
            peak = max(peak, val)
            dd = (peak - val) / peak * 100
            max_dd = max(max_dd, dd)
        return round(max_dd, 2)

    def ending_balance(self, start: float = 100.0, risk: float = 0.01) -> float:
        curve = self.equity_curve(start, risk)
        return curve[-1] if curve else start


# ══════════════════════════════════════════════════════════════════════════════
# MODEL-AWARE SIGNAL SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def scan_with_model(symbol: str, timeframe: str,
                    df: pd.DataFrame,
                    model: ModelConfig) -> list[dict]:
    """
    Run signal pipeline with model-specific parameters.
    Temporarily overrides config values so the existing pipeline
    respects model constraints without code duplication.
    """
    import journal as _journal

    # Patch config with model params
    _orig = {}
    patches = {
        "CONFIRMATION_MIN_SCORE":          model.min_score,
        "CONFIRMATION_HTF_MANDATORY":      model.htf_mandatory,
        "CONFIRMATION_SWEEP_MANDATORY":    model.sweep_mandatory,
        "CONFIRMATION_BOS_MANDATORY":      model.bos_mandatory,
        "CONFIRMATION_OB_MANDATORY":       model.ob_mandatory,
        "CONFIRMATION_PD_MANDATORY":       model.pd_mandatory,
        "SWEEP_WICK_DOMINANCE":            model.sweep_wick_dominance,
        "BOS_MIN_BODY_ATR_RATIO":          model.bos_min_body_atr,
        "PD_DISCOUNT_LEVEL":               model.pd_discount_level,
        "PD_PREMIUM_LEVEL":                model.pd_premium_level,
    }
    for k, v in patches.items():
        _orig[k] = getattr(config, k, None)
        setattr(config, k, v)

    # Bypass DB dedup
    orig_recent = _journal.get_recent_signals
    _journal.get_recent_signals = lambda *a, **kw: []

    # Reload confirmation module so it picks up patched config
    import confirmation as _conf
    importlib.reload(_conf)

    try:
        from signals import scan_for_signals
        signals = scan_for_signals(symbol, timeframe, df)
    except Exception as exc:
        log_warn(f"[BT/{model.id}] scan error {symbol} {timeframe}: {exc}")
        signals = []
    finally:
        # Restore config
        for k, v in _orig.items():
            setattr(config, k, v)
        _journal.get_recent_signals = orig_recent
        importlib.reload(_conf)

    # Post-filter: model-specific checks not in confirmation.py
    filtered = []
    for sig in signals:
        reason = _model_post_filter(sig, model, df)
        if reason:
            log_info(f"[BT/{model.id}] post-filter blocked: {reason}")
            continue
        filtered.append(sig)

    return filtered


def _model_post_filter(sig: dict, model: ModelConfig,
                       df: pd.DataFrame) -> str:
    """
    Additional model-specific filters that run after confirmation.
    Returns rejection reason string, or "" if allowed.
    """
    # Block HTF range
    htf_bias = sig.get("htf_bias", "")
    if model.block_htf_range and htf_bias == "range":
        return f"HTF=range blocked by model {model.id}"

    # Block FVG-only entries
    ob_label = sig.get("ob_label", "")
    if not model.allow_fvg_only:
        if ob_label.startswith("FVG [") and "OB" not in ob_label:
            return "FVG-only entry not allowed"

    # Strict P/D: no equilibrium
    pd_zone = sig.get("pd_zone", "equilibrium")
    if not model.allow_equilibrium and pd_zone == "equilibrium":
        return "equilibrium zone disabled in this model"

    # Premium/discount strict
    if model.premium_discount_only:
        direction = sig.get("direction", "")
        if direction == "long" and pd_zone != "discount":
            return f"long not in discount (zone={pd_zone})"
        if direction == "short" and pd_zone != "premium":
            return f"short not in premium (zone={pd_zone})"

    # Min RR to liquidity target
    if model.min_rr_to_liq > 0:
        liq_target = sig.get("liq_target")
        if liq_target is None:
            return f"no liquidity target (min_rr_to_liq={model.min_rr_to_liq})"
        mid = (sig["entry_low"] + sig["entry_high"]) / 2
        risk = abs(mid - sig["stop"])
        if risk > 0:
            rr = abs(liq_target - mid) / risk
            if rr < model.min_rr_to_liq:
                return f"RR to liq {rr:.2f} < min {model.min_rr_to_liq}"

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# TRADE EVALUATOR
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_trade(sig: dict, future: pd.DataFrame,
                   signal_bar: int, model: ModelConfig) -> BacktestTrade | None:
    """Walk future candles to determine trade outcome."""
    direction   = sig["direction"]
    entry_low   = sig["entry_low"]
    entry_high  = sig["entry_high"]
    stop_loss   = sig["stop"]
    take_profit = sig["tp"]
    mid_entry   = (entry_low + entry_high) / 2
    price_risk  = abs(mid_entry - stop_loss)

    if price_risk <= 0:
        return None

    rr_planned = round(abs(take_profit - mid_entry) / price_risk, 2)

    entry_bar  = None
    exit_bar   = None
    exit_price = None
    outcome    = "expired"
    mfe = mae  = 0.0
    best = worst = mid_entry

    for offset, (_, candle) in enumerate(future.iterrows()):
        o = candle.get("open", candle["close"])
        h = candle["high"]
        l = candle["low"]
        c = candle["close"]
        bar_abs = signal_bar + offset

        # Entry trigger
        if entry_bar is None:
            triggered = ((direction == "long"  and l <= entry_high) or
                         (direction == "short" and h >= entry_low))
            if not triggered:
                continue
            entry_bar = bar_abs
            best = worst = mid_entry   # reset from entry point

        # MFE / MAE (only after entry)
        if direction == "long":
            best  = max(best, h)
            worst = min(worst, l)
        else:
            best  = min(best, l)
            worst = max(worst, h)

        if price_risk > 0:
            mfe = abs(best  - mid_entry) / price_risk
            mae = abs(worst - mid_entry) / price_risk
            if direction == "long":
                mae = -mae if worst < mid_entry else mae
            else:
                mae = -mae if worst > mid_entry else mae

        # Trailing stop (breakeven at 1R)
        if getattr(config, "TRAILING_STOP_ENABLED", False):
            trigger_r = getattr(config, "TRAILING_STOP_TRIGGER_R", 1.0)
            lock_r    = getattr(config, "TRAILING_STOP_LOCK_R", 0.0)
            if direction == "long":
                moved_r = (h - mid_entry) / price_risk
                if moved_r >= trigger_r:
                    new_sl = mid_entry + lock_r * price_risk
                    if new_sl > stop_loss:
                        stop_loss = new_sl
            else:
                moved_r = (mid_entry - l) / price_risk
                if moved_r >= trigger_r:
                    new_sl = mid_entry - lock_r * price_risk
                    if new_sl < stop_loss:
                        stop_loss = new_sl

        # TP / SL check
        tp_hit = (direction == "long"  and h >= take_profit) or \
                 (direction == "short" and l <= take_profit)
        sl_hit = (direction == "long"  and l <= stop_loss) or \
                 (direction == "short" and h >= stop_loss)

        if tp_hit and sl_hit:
            outcome = _resolve_intrabar_collision(
                direction=direction,
                candle_open=o,
                candle_close=c,
                entry_price=mid_entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            exit_price = take_profit if outcome == "won" else stop_loss
            exit_bar   = bar_abs
            break
        elif tp_hit:
            outcome, exit_price, exit_bar = "won",  take_profit, bar_abs
            break
        elif sl_hit:
            outcome, exit_price, exit_bar = "lost", stop_loss,   bar_abs
            break

    # Handle expired
    if outcome == "expired":
        if model.expired_treatment == "zero":
            pnl_r = 0.0
            exit_price = exit_price or mid_entry
        else:  # mark-to-market
            ep = future["close"].iloc[-1] if not future.empty else mid_entry
            exit_price = ep
            pnl_r = ((ep - mid_entry) / price_risk if direction == "long"
                     else (mid_entry - ep) / price_risk)
    elif outcome == "won":
        pnl_r = abs((exit_price or mid_entry) - mid_entry) / price_risk
    else:
        pnl_r = -1.0

    rr_actual = (abs((exit_price or mid_entry) - mid_entry) / price_risk
                 if price_risk > 0 else 0.0)
    if outcome == "lost":
        rr_actual = -rr_actual

    return BacktestTrade(
        model_id    = sig.get("_model_id", "?"),
        symbol      = sig["symbol"],
        timeframe   = sig["timeframe"],
        direction   = direction,
        entry_price = mid_entry,
        stop_loss   = stop_loss,
        take_profit = take_profit,
        signal_bar  = signal_bar,
        entry_bar   = entry_bar,
        exit_bar    = exit_bar,
        exit_price  = round(exit_price or mid_entry, 4),
        outcome     = outcome,
        rr_planned  = rr_planned,
        rr_actual   = round(rr_actual, 3),
        pnl_r       = round(pnl_r, 3),
        score       = sig.get("conf_score", sig.get("score", 0)),
        ob_label    = sig.get("ob_label", ""),
        pd_zone     = sig.get("pd_zone", ""),
        htf_bias    = sig.get("htf_bias", ""),
        mfe         = round(mfe, 3),
        mae         = round(mae, 3),
        signal_time = str(sig.get("_bar_time", "")),
    )


def _resolve_intrabar_collision(direction: str,
                                candle_open: float,
                                candle_close: float,
                                entry_price: float,
                                stop_loss: float,
                                take_profit: float) -> str:
    """
    Resolve candles where both TP and SL are inside the same bar.

    Default to conservative handling because OHLC data does not provide
    the true intrabar path.
    """
    policy = getattr(config, "BACKTEST_INTRABAR_POLICY", "conservative")

    if policy == "optimistic":
        return "won"
    if policy == "close_bias":
        if (direction == "long" and candle_close > entry_price) or (
            direction == "short" and candle_close < entry_price
        ):
            return "won"
        return "lost"
    if policy == "open_distance":
        tp_distance = abs(take_profit - candle_open)
        sl_distance = abs(candle_open - stop_loss)
        return "won" if tp_distance < sl_distance else "lost"
    return "lost"


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_model(model: ModelConfig,
              symbol: str, timeframe: str,
              df: pd.DataFrame,
              walk_step: int = 2) -> BacktestResult:
    """Walk-forward backtest for one model on one symbol/timeframe."""
    result   = BacktestResult(model=model)
    warmup   = 100
    seen:    set[str] = set()

    i = warmup
    while i < len(df) - 10:
        window = df.iloc[:i].copy()

        try:
            sigs = scan_with_model(symbol, timeframe, window, model)
        except Exception as exc:
            log_warn(f"[BT/{model.id}] {symbol} {timeframe} bar={i}: {exc}")
            i += walk_step
            continue

        for sig in sigs:
            sig["_model_id"]  = model.id
            sig["_bar_time"]  = str(df.iloc[i]["timestamp"]) if "timestamp" in df.columns else ""
            h = _sig_hash(sig)
            if h in seen:
                continue
            seen.add(h)

            future = df.iloc[i: i + config.EVALUATION_LOOKAHEAD_BARS + 10]
            trade  = evaluate_trade(sig, future, i, model)
            if trade:
                result.trades.append(trade)

        i += walk_step

    return result


def run_all(models:     list[ModelConfig],
            symbols:    list[str],
            timeframes: list[str],
            days:       int = 180,
            walk_step:  int = 2) -> list[BacktestResult]:
    """Run all models on all symbol/timeframe combinations."""
    from datafeed import fetch_ohlcv

    # Fetch data once, reuse across models
    data_cache: dict[tuple, pd.DataFrame] = {}
    bars_per_day = {"1m":1440,"5m":288,"15m":96,"30m":48,"1h":24,"4h":6,"1d":1}

    for sym in symbols:
        for tf in timeframes:
            needed = min(days * bars_per_day.get(tf, 24) + 150, 1000)
            log_info(f"[BT] fetching {sym} [{tf}] {needed} candles ...")
            df = fetch_ohlcv(sym, tf, limit=needed)
            if not df.empty:
                data_cache[(sym, tf)] = df
            time_mod.sleep(0.3)

    all_results: list[BacktestResult] = []

    for model in models:
        log_info(f"\n[BT] ══ Model {model.id}: {model.name} ══")
        model_result = BacktestResult(model=model)

        for (sym, tf), df in data_cache.items():
            log_info(f"[BT/{model.id}]   {sym} [{tf}] {len(df)} candles ...")
            r = run_model(model, sym, tf, df, walk_step=walk_step)
            model_result.trades.extend(r.trades)
            w = len(r.wins)
            l = len(r.losses)
            e = len(r.expired)
            log_info(f"[BT/{model.id}]   → {len(r.trades)} trades "
                     f"W:{w} L:{l} E:{e} "
                     f"WR:{r.win_rate:.0f}% "
                     f"R:{r.total_r:+.2f}")

        all_results.append(model_result)
        log_info(f"[BT/{model.id}] TOTAL  "
                 f"trades={len(model_result.trades)} "
                 f"WR={model_result.win_rate:.1f}% "
                 f"PF={model_result.profit_factor:.2f} "
                 f"R={model_result.total_r:+.2f} "
                 f"DD={model_result.max_drawdown():.1f}%")

    return all_results


# ══════════════════════════════════════════════════════════════════════════════
# CONSOLE REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_comparison(results: list[BacktestResult]) -> None:
    SEP = "═" * 90
    print(f"\n{SEP}")
    print(f"  SMC MULTI-MODEL BACKTEST — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(SEP)
    print(f"  {'Model':<22} {'Trades':>6} {'W':>4} {'L':>4} {'E':>4} "
          f"{'WR%':>6} {'PF':>5} {'Exp':>6} {'TotR':>8} "
          f"{'DD%':>6} {'$End':>8}")
    print(f"  {'-'*86}")

    best_pf  = max(results, key=lambda r: r.profit_factor)
    best_exp = max(results, key=lambda r: r.expectancy)
    best_r   = max(results, key=lambda r: r.total_r)
    best_dd  = min(results, key=lambda r: r.max_drawdown())

    for r in results:
        flags = ""
        if r.model.id == best_pf.model.id:  flags += "★PF "
        if r.model.id == best_exp.model.id: flags += "★Exp"
        name = f"{r.model.id}: {r.model.name}"
        sign = "+" if r.total_r >= 0 else ""
        wr_color = "✅" if r.win_rate >= 40 else "❌"
        print(f"  {name:<22} {len(r.trades):>6} "
              f"{len(r.wins):>4} {len(r.losses):>4} {len(r.expired):>4} "
              f"{r.win_rate:>5.1f}% {r.profit_factor:>5.2f} "
              f"{r.expectancy:>+6.2f} {sign}{r.total_r:>7.2f} "
              f"{r.max_drawdown():>5.1f}% ${r.ending_balance():>7.2f}  {flags}")

    print(SEP)
    print(f"  ★ Best PF: Model {best_pf.model.id} ({best_pf.model.name})")
    print(f"  ★ Best Expectancy: Model {best_exp.model.id} ({best_exp.model.name})")
    print(f"  ★ Best Total R: Model {best_r.model.id} ({best_r.model.name})")
    print(f"  ★ Lowest Drawdown: Model {best_dd.model.id} ({best_dd.model.name})")
    print(SEP)


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

_COLORS = ["#58a6ff","#00e676","#ffd700","#ff7b54","#c084fc","#4dd9e0","#ff4c4c"]

def generate_html(results: list[BacktestResult],
                  output: str = "backtest_comparison.html") -> str:

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    best_pf  = max(results, key=lambda r: r.profit_factor).model.id
    best_exp = max(results, key=lambda r: r.expectancy).model.id
    best_r   = max(results, key=lambda r: r.total_r).model.id
    best_dd  = min(results, key=lambda r: r.max_drawdown()).model.id

    # ── Comparison table ───────────────────────────────────────────────────────
    rows_html = ""
    for r in results:
        badges = ""
        if r.model.id == best_pf:  badges += '<span style="background:#1D9E75;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;margin-left:4px">★PF</span>'
        if r.model.id == best_exp: badges += '<span style="background:#185FA5;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;margin-left:4px">★Exp</span>'
        if r.model.id == best_r:   badges += '<span style="background:#BA7517;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;margin-left:4px">★R</span>'
        if r.model.id == best_dd:  badges += '<span style="background:#533AB7;color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;margin-left:4px">★DD</span>'

        sign = "+" if r.total_r >= 0 else ""
        wr_col = "#00e676" if r.win_rate >= 40 else "#ff4c4c"
        pf_col = "#00e676" if r.profit_factor >= 1 else "#ff4c4c"
        rows_html += f"""<tr>
            <td><b>{r.model.id}</b>: {r.model.name}{badges}</td>
            <td>{len(r.trades)}</td><td>{len(r.wins)}</td>
            <td>{len(r.losses)}</td><td>{len(r.expired)}</td>
            <td style="color:{wr_col};font-weight:bold">{r.win_rate:.1f}%</td>
            <td style="color:{pf_col};font-weight:bold">{r.profit_factor:.2f}</td>
            <td>{r.expectancy:+.2f}R</td>
            <td>{sign}{r.total_r:.2f}R</td>
            <td>{r.median_r:+.2f}R</td>
            <td>{r.max_drawdown():.1f}%</td>
            <td>${r.ending_balance():.2f}</td>
        </tr>\n"""

    # ── Equity curves data ────────────────────────────────────────────────────
    max_len = max(len(r.equity_curve()) for r in results) if results else 1
    eq_datasets = []
    for i, r in enumerate(results):
        curve = r.equity_curve()
        color = _COLORS[i % len(_COLORS)]
        eq_datasets.append({
            "label": f"{r.model.id}: {r.model.name}",
            "data":  curve,
            "borderColor": color,
            "backgroundColor": "transparent",
            "tension": 0.3,
            "pointRadius": 1,
            "borderWidth": 2,
        })

    eq_labels = list(range(max_len))

    # ── Per-model detail sections ─────────────────────────────────────────────
    detail_html = ""
    for i, r in enumerate(results):
        color = _COLORS[i % len(_COLORS)]
        detail_html += _model_detail_html(r, color)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>SMC Multi-Model Backtest</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;font-size:13px;padding:20px}}
h1{{font-size:20px;color:#58a6ff;margin-bottom:4px}}
h2{{font-size:15px;color:#58a6ff;margin:20px 0 10px}}
h3{{font-size:13px;color:#8b949e;margin:14px 0 8px;text-transform:uppercase}}
.sub{{color:#8b949e;font-size:11px;margin-bottom:20px}}
.card-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}}
.card .label{{color:#8b949e;font-size:10px;text-transform:uppercase;margin-bottom:4px}}
.card .value{{font-size:18px;font-weight:bold}}
.green{{color:#00e676}}.red{{color:#ff4c4c}}.blue{{color:#58a6ff}}.yellow{{color:#ffd700}}
.chart-box{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:16px}}
th{{background:#21262d;color:#8b949e;font-size:10px;padding:8px 10px;text-align:left;text-transform:uppercase}}
td{{padding:6px 10px;border-top:1px solid #21262d;font-size:12px}}
tr:hover td{{background:#1c2128}}
.long{{color:#00e676}}.short{{color:#ff4c4c}}
.section{{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:16px;margin-bottom:24px}}
.badge{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;margin-left:4px}}
</style></head><body>
<h1>📊 SMC Multi-Model Backtest Comparison</h1>
<div class="sub">Generated {now} | {len(results)} models | expired treated as 0R</div>

<h2>Model Comparison</h2>
<table>
<thead><tr>
  <th>Model</th><th>Trades</th><th>W</th><th>L</th><th>E</th>
  <th>Win Rate</th><th>PF</th><th>Expectancy</th>
  <th>Total R</th><th>Median R</th><th>Max DD</th><th>Balance</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<div class="chart-box">
<h3>Equity Curves — all models ($100 start, 1% risk)</h3>
<div style="position:relative;height:300px"><canvas id="eqAll"></canvas></div>
</div>

{detail_html}

<script>
const eqDatasets = {json.dumps(eq_datasets)};
const eqLabels = {json.dumps(eq_labels)};
new Chart(document.getElementById('eqAll'), {{
  type:'line',
  data:{{labels:eqLabels, datasets:eqDatasets}},
  options:{{
    responsive:true, maintainAspectRatio:false,
    plugins:{{legend:{{labels:{{color:'#8b949e',font:{{size:11}}}}}}}},
    scales:{{
      x:{{ticks:{{color:'#8b949e',maxTicksLimit:12}},grid:{{color:'#21262d'}}}},
      y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}
    }}
  }}
}});
</script>
</body></html>"""

    with open(output, "w", encoding="utf-8") as f:
        f.write(html)
    log_info(f"[BT] HTML saved → {output}")
    return output


def _model_detail_html(r: BacktestResult, color: str) -> str:
    from collections import defaultdict

    sign = "+" if r.total_r >= 0 else ""
    wr_col = "#00e676" if r.win_rate >= 40 else "#ff4c4c"
    pf_col = "#00e676" if r.profit_factor >= 1 else "#ff4c4c"

    # Summary cards
    cards = f"""
    <div class="card-row">
      <div class="card"><div class="label">Trades</div><div class="value blue">{len(r.trades)}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value" style="color:{wr_col}">{r.win_rate:.1f}%</div></div>
      <div class="card"><div class="label">PF</div><div class="value" style="color:{pf_col}">{r.profit_factor:.2f}</div></div>
      <div class="card"><div class="label">Expectancy</div><div class="value {'green' if r.expectancy>=0 else 'red'}">{r.expectancy:+.2f}R</div></div>
      <div class="card"><div class="label">Total R</div><div class="value {'green' if r.total_r>=0 else 'red'}">{sign}{r.total_r:.2f}R</div></div>
      <div class="card"><div class="label">Median R</div><div class="value">{r.median_r:+.2f}R</div></div>
      <div class="card"><div class="label">Max DD</div><div class="value {'green' if r.max_drawdown()<15 else 'red'}">{r.max_drawdown():.1f}%</div></div>
      <div class="card"><div class="label">Balance</div><div class="value {'green' if r.ending_balance()>=100 else 'red'}">${r.ending_balance():.2f}</div></div>
    </div>"""

    # Breakdown by symbol
    by_sym: dict = defaultdict(list)
    for t in r.trades: by_sym[t.symbol].append(t)
    sym_rows = ""
    for sym, ts in sorted(by_sym.items()):
        w = sum(1 for t in ts if t.outcome=="won")
        l = sum(1 for t in ts if t.outcome=="lost")
        e = sum(1 for t in ts if t.outcome=="expired")
        rv = sum(t.pnl_r for t in ts)
        d  = w+l
        wr = w/d*100 if d else 0
        sym_rows += f"<tr><td>{sym}</td><td>{len(ts)}</td><td>{w}</td><td>{l}</td><td>{e}</td><td style='color:{'#00e676' if wr>=40 else '#ff4c4c'}'>{wr:.0f}%</td><td>{rv:+.2f}R</td></tr>"

    # Breakdown by score bucket
    score_rows = ""
    buckets: dict = defaultdict(list)
    for t in r.trades:
        b = (t.score // 5) * 5
        buckets[b].append(t)
    for b in sorted(buckets):
        ts = buckets[b]
        w = sum(1 for t in ts if t.outcome=="won")
        l = sum(1 for t in ts if t.outcome=="lost")
        rv = sum(t.pnl_r for t in ts)
        d  = w+l
        wr = w/d*100 if d else 0
        score_rows += f"<tr><td>{b}–{b+4}</td><td>{len(ts)}</td><td>{w}</td><td>{l}</td><td style='color:{'#00e676' if wr>=40 else '#ff4c4c'}'>{wr:.0f}%</td><td>{rv:+.2f}R</td></tr>"

    # Breakdown by zone
    by_zone: dict = defaultdict(list)
    for t in r.trades: by_zone[t.pd_zone or "?"].append(t)
    zone_rows = ""
    for z, ts in sorted(by_zone.items()):
        w = sum(1 for t in ts if t.outcome=="won")
        l = sum(1 for t in ts if t.outcome=="lost")
        rv = sum(t.pnl_r for t in ts)
        d  = w+l
        wr = w/d*100 if d else 0
        zone_rows += f"<tr><td>{z}</td><td>{len(ts)}</td><td>{w}</td><td>{l}</td><td style='color:{'#00e676' if wr>=40 else '#ff4c4c'}'>{wr:.0f}%</td><td>{rv:+.2f}R</td></tr>"

    # Breakdown by direction
    dir_rows = ""
    by_dir: dict = defaultdict(list)
    for t in r.trades: by_dir[t.direction].append(t)
    for d, ts in sorted(by_dir.items()):
        w = sum(1 for t in ts if t.outcome=="won")
        l = sum(1 for t in ts if t.outcome=="lost")
        rv = sum(t.pnl_r for t in ts)
        dd = w+l
        wr = w/dd*100 if dd else 0
        dir_rows += f"<tr><td class='{'long' if d=='long' else 'short'}'>{d.upper()}</td><td>{len(ts)}</td><td>{w}</td><td>{l}</td><td style='color:{'#00e676' if wr>=40 else '#ff4c4c'}'>{wr:.0f}%</td><td>{rv:+.2f}R</td></tr>"

    # Trade log (last 30)
    trade_rows = ""
    for t in r.trades[-30:]:
        col  = "#00e676" if t.outcome=="won" else "#ff4c4c" if t.outcome=="lost" else "#888"
        emoji = "✅" if t.outcome=="won" else "❌" if t.outcome=="lost" else "⏱"
        sign2 = "+" if t.pnl_r >= 0 else ""
        trade_rows += (f"<tr><td>{t.symbol}</td><td>{t.timeframe}</td>"
                       f"<td class='{'long' if t.direction=='long' else 'short'}'>{t.direction.upper()}</td>"
                       f"<td>{t.score}</td>"
                       f"<td style='color:{col}'>{emoji} {t.outcome}</td>"
                       f"<td style='color:{col};font-weight:bold'>{sign2}{t.pnl_r:.2f}R</td>"
                       f"<td>{t.rr_planned:.2f}</td>"
                       f"<td>{t.mfe:.2f}R</td><td>{t.mae:.2f}R</td>"
                       f"<td>{t.htf_bias}</td><td>{t.pd_zone}</td>"
                       f"<td style='font-size:11px'>{t.ob_label[:28]}</td></tr>\n")

    table_header = "<tr><th>Symbol</th><th>n</th><th>W</th><th>L</th><th>E</th><th>WR</th><th>R</th></tr>"
    score_header = "<tr><th>Score</th><th>n</th><th>W</th><th>L</th><th>WR</th><th>R</th></tr>"
    dir_header   = "<tr><th>Dir</th><th>n</th><th>W</th><th>L</th><th>WR</th><th>R</th></tr>"

    return f"""
<div class="section">
<h2 style="color:{color}">Model {r.model.id}: {r.model.name}</h2>
<p style="color:#8b949e;font-size:12px;margin-bottom:12px">{r.model.description}</p>
{cards}
<div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px">
  <div>
    <h3>By Symbol</h3>
    <table><thead>{table_header}</thead><tbody>{sym_rows}</tbody></table>
  </div>
  <div>
    <h3>By Score</h3>
    <table><thead>{score_header}</thead><tbody>{score_rows}</tbody></table>
  </div>
  <div>
    <h3>By Zone</h3>
    <table><thead>{score_header}</thead><tbody>{zone_rows}</tbody></table>
  </div>
  <div>
    <h3>By Direction</h3>
    <table><thead>{dir_header}</thead><tbody>{dir_rows}</tbody></table>
  </div>
</div>
<h3>Trade Log (last 30)</h3>
<table><thead><tr>
  <th>Symbol</th><th>TF</th><th>Dir</th><th>Score</th><th>Outcome</th>
  <th>PnL</th><th>RR</th><th>MFE</th><th>MAE</th><th>HTF</th><th>Zone</th><th>OB</th>
</tr></thead><tbody>{trade_rows}</tbody></table>
</div>"""


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _sig_hash(sig: dict) -> str:
    import hashlib
    key = (
        f"{sig['symbol']}|{sig['timeframe']}|{sig['direction']}|"
        f"{round(sig['entry_low'], 2)}|{round(sig['entry_high'], 2)}|"
        f"{sig.get('_bar_time', '')}"
    )
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser(description="SMC Multi-Model Backtester")
    p.add_argument("--days",       type=int,   default=180)
    p.add_argument("--step",       type=int,   default=2,
                   help="Walk step bars (lower=more thorough, slower)")
    p.add_argument("--models",     default="all",
                   help="all  or  A,B,C")
    p.add_argument("--symbols",    default=None,
                   help="BTC/USDT,ETH/USDT  (default: config.SYMBOLS)")
    p.add_argument("--timeframes", default=None,
                   help="1h,4h  (default: config.TIMEFRAMES)")
    p.add_argument("--html",       action="store_true")
    p.add_argument("--no-open",    action="store_true")
    p.add_argument("--out",        default="backtest_comparison.html")
    args = p.parse_args()

    # Resolve models
    if args.models.lower() == "all":
        models = list(MODEL_REGISTRY.values())
    else:
        ids = [m.strip().upper() for m in args.models.split(",")]
        models = [MODEL_REGISTRY[i] for i in ids if i in MODEL_REGISTRY]
        if not models:
            print(f"Unknown models: {args.models}. Available: {', '.join(MODEL_REGISTRY)}")
            sys.exit(1)

    symbols    = args.symbols.split(",")    if args.symbols    else config.SYMBOLS
    timeframes = args.timeframes.split(",") if args.timeframes else config.TIMEFRAMES

    log_info(f"[BT] Models: {[m.id for m in models]}")
    log_info(f"[BT] Symbols: {symbols}")
    log_info(f"[BT] Timeframes: {timeframes}")
    log_info(f"[BT] Days: {args.days}  Step: {args.step}")

    results = run_all(models, symbols, timeframes,
                      days=args.days, walk_step=args.step)

    print_comparison(results)

    if args.html:
        import webbrowser
        from pathlib import Path
        path = generate_html(results, output=args.out)
        if not args.no_open:
            webbrowser.open(f"file://{Path(path).resolve()}")


if __name__ == "__main__":
    main()
