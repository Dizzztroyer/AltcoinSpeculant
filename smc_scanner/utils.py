# utils.py — Shared helpers: logging, formatting, EMA

import datetime
import math
import os

import numpy as np
import pandas as pd
from colorama import Fore, Style, init

import config

init(autoreset=True)   # colorama cross-platform


# ── Logging ────────────────────────────────────────────────────────────────────

def ts() -> str:
    """Return current UTC timestamp string."""
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def log_info(msg: str) -> None:
    print(f"{Fore.CYAN}[{ts()}] INFO  {msg}{Style.RESET_ALL}")


def log_warn(msg: str) -> None:
    print(f"{Fore.YELLOW}[{ts()}] WARN  {msg}{Style.RESET_ALL}")


def log_signal(text: str) -> None:
    """Print a signal in green and optionally append to log file."""
    print(f"{Fore.GREEN}{text}{Style.RESET_ALL}")
    if config.LOG_FILE:
        with open(config.LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")


def log_error(msg: str) -> None:
    print(f"{Fore.RED}[{ts()}] ERROR {msg}{Style.RESET_ALL}")


# ── Math / indicator helpers ───────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"]
    low  = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def pct_diff(a: float, b: float) -> float:
    """Absolute percentage difference between two prices."""
    if b == 0:
        return math.inf
    return abs(a - b) / b


def round_price(price: float, decimals: int = 2) -> float:
    return round(price, decimals)


# ── Signal formatting ──────────────────────────────────────────────────────────

def format_signal(sig: dict) -> str:
    """
    Convert a signal dict into a human-readable block.

    Expected keys:
        symbol, timeframe, context, sweep_desc, bos_desc,
        direction, entry_low, entry_high, stop, tp, reason
    """
    direction_str = "LONG 🟢" if sig["direction"] == "long" else "SHORT 🔴"
    lines = [
        "=" * 56,
        f"  SMC SIGNAL — {ts()}",
        "=" * 56,
        f"  Symbol     : {sig['symbol']}",
        f"  Timeframe  : {sig['timeframe']}",
        f"  Context    : {sig['context']}",
        f"  Liq sweep  : {sig['sweep_desc']}",
        f"  Structure  : {sig['bos_desc']}",
        f"  Signal     : {direction_str}",
        f"  Entry zone : {round_price(sig['entry_low'])} – {round_price(sig['entry_high'])}",
        f"  Stop Loss  : {round_price(sig['stop'])}",
        f"  Take Profit: {round_price(sig['tp'])}",
        f"  Reason     : {sig['reason']}",
        "=" * 56,
    ]
    return "\n".join(lines)