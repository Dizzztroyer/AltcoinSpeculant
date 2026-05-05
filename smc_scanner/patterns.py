# patterns.py — Institutional Price Action patterns (CMS / Mansor Sapari)
#
# Detectors:
#   QM  — Quasimodo (standard, continuation, ignored)
#   FK  — Fakeout (V1 default, V2 SR-flip, V3 Diamond/DM Shadow)
#   SR  — Support/Resistance Flip
#   CP  — Compression (Flag B / accumulation before move)
#   MPL — Multiple Points of Liquidity (stop hunt cluster)
#
# Each detector is standalone. Results feed into confirmation.py as
# score adjustments. Hard blocks fire when fakeout confidence is high.
#
# Config flags:
#   USE_QM_FILTER, USE_FAKEOUT_FILTER, USE_SR_FLIP,
#   USE_COMPRESSION, USE_MPL  (all default True)

from __future__ import annotations
from dataclasses import dataclass
import pandas as pd
import config
from utils import log_info


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class QMResult:
    is_qm: bool; qm_type: str; qm_level: float
    description: str; score_bonus: int

@dataclass
class FakeoutResult:
    is_fakeout: bool; fakeout_type: str; confidence: float
    description: str; score_penalty: int

@dataclass
class SRFlipResult:
    has_flip: bool; flip_level: float | None; flip_type: str
    description: str; score_bonus: int

@dataclass
class CompressionResult:
    has_compression: bool; compression_pct: float; bars: int
    description: str; score_bonus: int

@dataclass
class MPLResult:
    touch_count: int; is_mpl: bool
    description: str; score_bonus: int

@dataclass
class PatternContext:
    qm: QMResult
    fakeout: FakeoutResult
    sr_flip: SRFlipResult
    compression: CompressionResult
    mpl: MPLResult

    @property
    def net_score_adjustment(self) -> int:
        t = 0
        if getattr(config, "USE_QM_FILTER",      True): t += self.qm.score_bonus
        if getattr(config, "USE_FAKEOUT_FILTER",  True): t += self.fakeout.score_penalty
        if getattr(config, "USE_SR_FLIP",         True): t += self.sr_flip.score_bonus
        if getattr(config, "USE_COMPRESSION",     True): t += self.compression.score_bonus
        if getattr(config, "USE_MPL",             True): t += self.mpl.score_bonus
        return t

    @property
    def is_hard_blocked(self) -> bool:
        threshold = getattr(config, "FAKEOUT_BLOCK_THRESHOLD", 0.7)
        return (getattr(config, "USE_FAKEOUT_FILTER", True)
                and self.fakeout.is_fakeout
                and self.fakeout.confidence >= threshold)

    def summary_lines(self) -> list[str]:
        out = []
        if getattr(config, "USE_QM_FILTER",     True) and self.qm.is_qm:
            out.append(f"QM/{self.qm.qm_type} ({self.qm.score_bonus:+d}): {self.qm.description}")
        if getattr(config, "USE_FAKEOUT_FILTER", True) and self.fakeout.is_fakeout:
            out.append(f"FAKEOUT/{self.fakeout.fakeout_type} "
                       f"({self.fakeout.score_penalty:+d} "
                       f"conf={self.fakeout.confidence:.0%}): {self.fakeout.description}")
        if getattr(config, "USE_SR_FLIP",       True) and self.sr_flip.has_flip:
            out.append(f"SR_FLIP/{self.sr_flip.flip_type} ({self.sr_flip.score_bonus:+d}): "
                       f"{self.sr_flip.description}")
        if getattr(config, "USE_COMPRESSION",   True) and self.compression.has_compression:
            out.append(f"COMPRESSION ({self.compression.score_bonus:+d}): "
                       f"{self.compression.description}")
        if getattr(config, "USE_MPL",           True) and self.mpl.is_mpl:
            out.append(f"MPL/{self.mpl.touch_count}x ({self.mpl.score_bonus:+d}): "
                       f"{self.mpl.description}")
        return out


# ── Main entry point ──────────────────────────────────────────────────────────

def analyse_patterns(df: pd.DataFrame,
                     direction: str,
                     sweep_candle_idx: int,
                     sweep_level: float,
                     bos_candle_idx: int,
                     bos_level: float) -> PatternContext:
    """
    Run all pattern detectors.  Call this after BOS is confirmed
    but before scoring so results can adjust the final score.
    """
    qm  = detect_quasimodo(df, direction, sweep_candle_idx, sweep_level, bos_candle_idx)
    fk  = detect_fakeout(df, direction, sweep_candle_idx, sweep_level, bos_candle_idx, bos_level)
    sr  = detect_sr_flip(df, direction, bos_level)
    cp  = detect_compression(df, sweep_candle_idx)
    mpl = detect_mpl(df, sweep_level, sweep_candle_idx)

    ctx = PatternContext(qm=qm, fakeout=fk, sr_flip=sr, compression=cp, mpl=mpl)
    adj = ctx.net_score_adjustment
    if adj != 0 or ctx.is_hard_blocked:
        log_info(f"[PATTERN] adj={adj:+d} blocked={ctx.is_hard_blocked}")
        for ln in ctx.summary_lines():
            log_info(f"[PATTERN]   {ln}")
    return ctx


# ── QM — Quasimodo ────────────────────────────────────────────────────────────

def detect_quasimodo(df, direction, sweep_idx, sweep_level, bos_idx) -> QMResult:
    """
    SHORT QM structure: HH → LL → HHH (sweep) → HL(QML) → BOS below HL
    LONG  QM structure: LL → HH → LLL (sweep) → LH(QML) → BOS above LH

    QML = the last shoulder (HL for short, LH for long) before the failed attempt.
    This level is the key: price should react strongly there.
    """
    if not getattr(config, "USE_QM_FILTER", True):
        return QMResult(False, "none", 0.0, "disabled", 0)

    pre = df[df.index < sweep_idx].tail(40)
    if len(pre) < 15:
        return QMResult(False, "none", 0.0, "insufficient history", 0)

    has_sh = "swing_high" in df.columns
    has_sl = "swing_low"  in df.columns

    if direction == "short":
        return _qm_short(pre, sweep_level, has_sh, has_sl, df, sweep_idx)
    return _qm_long(pre, sweep_level, has_sh, has_sl, df, sweep_idx)


def _qm_short(pre, sweep_level, has_sh, has_sl, df, sweep_idx) -> QMResult:
    if has_sh:
        sh = pre[pre["swing_high"]]["high"].values
        if len(sh) < 2:
            return QMResult(False, "none", 0.0, "need 2+ swing highs", 0)
        first_hh, second_hh = sh[-2], sh[-1]
    else:
        n = len(pre); first_hh = pre["high"].iloc[:n//2].max(); second_hh = pre["high"].iloc[n//2:].max()

    if second_hh < first_hh * 0.998:
        return QMResult(False, "none", 0.0, f"no HH (h1={first_hh:.2f} h2={second_hh:.2f})", 0)
    if abs(sweep_level - second_hh) / second_hh > 0.006:
        return QMResult(False, "none", 0.0, f"sweep not near HHH", 0)

    if has_sl:
        sl = pre[pre["swing_low"]]["low"].values
        qml = sl[-1] if len(sl) else pre["low"].iloc[len(pre)//2:].min()
    else:
        qml = pre["low"].iloc[len(pre)//2:].min()

    if qml <= pre["low"].iloc[:len(pre)//2].min() * 1.001:
        return QMResult(False, "none", 0.0, "HL not above recent LL", 0)

    is_cont = _is_continuation(df, sweep_idx, "short")
    is_ign  = _is_ignored(df, qml, sweep_idx, "short")
    if is_ign:
        return QMResult(True, "ignored", qml,
                        f"Ignored QML {qml:.2f} (previously bypassed)",
                        getattr(config, "QM_IGNORED_BONUS", 0))
    qm_type = "continuation" if is_cont else "standard"
    return QMResult(True, qm_type, qml,
                    f"QM {qm_type}: HHH={second_hh:.2f} QML={qml:.2f}",
                    getattr(config, "QM_STANDARD_BONUS", 10))


def _qm_long(pre, sweep_level, has_sh, has_sl, df, sweep_idx) -> QMResult:
    if has_sl:
        sl = pre[pre["swing_low"]]["low"].values
        if len(sl) < 2:
            return QMResult(False, "none", 0.0, "need 2+ swing lows", 0)
        first_ll, second_ll = sl[-2], sl[-1]
    else:
        n = len(pre); first_ll = pre["low"].iloc[:n//2].min(); second_ll = pre["low"].iloc[n//2:].min()

    if second_ll > first_ll * 1.002:
        return QMResult(False, "none", 0.0, f"no LL structure", 0)
    if abs(sweep_level - second_ll) / max(second_ll, 1e-9) > 0.006:
        return QMResult(False, "none", 0.0, "sweep not near LLL", 0)

    if has_sh:
        sh = pre[pre["swing_high"]]["high"].values
        qml = sh[-1] if len(sh) else pre["high"].iloc[len(pre)//2:].max()
    else:
        qml = pre["high"].iloc[len(pre)//2:].max()

    if qml >= pre["high"].iloc[:len(pre)//2].max() * 0.999:
        return QMResult(False, "none", 0.0, "LH not below recent HH", 0)

    is_cont = _is_continuation(df, sweep_idx, "long")
    is_ign  = _is_ignored(df, qml, sweep_idx, "long")
    if is_ign:
        return QMResult(True, "ignored", qml,
                        f"Ignored QML {qml:.2f}",
                        getattr(config, "QM_IGNORED_BONUS", 0))
    qm_type = "continuation" if is_cont else "standard"
    return QMResult(True, qm_type, qml,
                    f"QM {qm_type}: LLL={second_ll:.2f} QML={qml:.2f}",
                    getattr(config, "QM_STANDARD_BONUS", 10))


def _is_continuation(df, sweep_idx, direction) -> bool:
    pre = df[df.index < sweep_idx].tail(60)
    if len(pre) < 20: return False
    mid = len(pre)//2
    a, b = pre["close"].iloc[:mid].mean(), pre["close"].iloc[mid:].mean()
    return (b < a) if direction == "short" else (b > a)


def _is_ignored(df, qml, sweep_idx, direction) -> bool:
    pre = df[df.index < sweep_idx]
    tol = qml * 0.003
    for _, c in pre.iterrows():
        if direction == "short" and c["close"] < qml - tol: return True
        if direction == "long"  and c["close"] > qml + tol: return True
    return False


# ── Fakeout detection ─────────────────────────────────────────────────────────

def detect_fakeout(df, direction, sweep_idx, sweep_level,
                    bos_idx, bos_level) -> FakeoutResult:
    """
    This is the most critical filter for our 14% SHORT WR problem.

    V1: BOS fires but price immediately returns through the BOS level
        → the BOS was a stop hunt (manipulation), not a real reversal.

    V2: BOS level was a well-tested S/R → bounce likely.

    V3 (Diamond/DM Shadow): same level swept 2x with weakening rejection.
    """
    if not getattr(config, "USE_FAKEOUT_FILTER", True):
        return FakeoutResult(False, "none", 0.0, "disabled", 0)

    for fn in [_fk_v1, _fk_v3, _fk_v2]:
        r = fn(df, direction, sweep_idx, sweep_level, bos_idx, bos_level)
        if r.is_fakeout:
            return r
    return FakeoutResult(False, "none", 0.0, "no fakeout", 0)


def _fk_v1(df, direction, sweep_idx, sweep_level, bos_idx, bos_level) -> FakeoutResult:
    """V1: price returns through BOS level within 3 candles after BOS."""
    post = df[df.index > bos_idx].head(3)
    if post.empty or bos_idx not in df.index:
        return FakeoutResult(False, "none", 0.0, "", 0)
    reversals = sum(
        1 for _, c in post.iterrows()
        if (direction == "short" and c["close"] > bos_level) or
           (direction == "long"  and c["close"] < bos_level)
    )
    if reversals >= 1:
        conf = min(0.5 + reversals * 0.2, 0.90)
        return FakeoutResult(True, "V1", conf,
                             f"BOS {bos_level:.2f} reversed in {reversals} candle(s)",
                             getattr(config, "FAKEOUT_V1_PENALTY", -20))
    return FakeoutResult(False, "none", 0.0, "", 0)


def _fk_v3(df, direction, sweep_idx, sweep_level, bos_idx, bos_level) -> FakeoutResult:
    """V3 Diamond: 2 sweeps of same level, second with weaker rejection."""
    pre  = df[df.index < sweep_idx].tail(50)
    tol  = sweep_level * 0.002
    cnt  = 0; last_rej = 0.0
    for _, c in pre.iterrows():
        cr = c["high"] - c["low"]
        if cr == 0: continue
        if direction == "short" and c["high"] >= sweep_level - tol and c["close"] < sweep_level:
            cnt += 1
            last_rej = (c["high"] - max(c["open"], c["close"])) / cr
        elif direction == "long" and c["low"] <= sweep_level + tol and c["close"] > sweep_level:
            cnt += 1
            last_rej = (min(c["open"], c["close"]) - c["low"]) / cr
    if cnt == 2 and last_rej < 0.30:
        return FakeoutResult(True, "V3", 0.60,
                             f"Diamond: 2 sweeps of {sweep_level:.2f}, rej={last_rej:.0%}",
                             getattr(config, "FAKEOUT_V3_PENALTY", -10))
    return FakeoutResult(False, "none", 0.0, "", 0)


def _fk_v2(df, direction, sweep_idx, sweep_level, bos_idx, bos_level) -> FakeoutResult:
    """V2 SR-flip: BOS level was heavily tested as S/R → bounce likely."""
    pre  = df[df.index < sweep_idx].tail(30)
    tol  = bos_level * 0.003
    res  = sum(1 for _, c in pre.iterrows()
               if abs(c["high"] - bos_level) < tol and c["close"] < bos_level)
    sup  = sum(1 for _, c in pre.iterrows()
               if abs(c["low"]  - bos_level) < tol and c["close"] > bos_level)
    t = max(res, sup)
    if t >= 3:
        return FakeoutResult(True, "V2", 0.45,
                             f"SR level {bos_level:.2f} tested {t}× — sticky",
                             getattr(config, "FAKEOUT_V2_PENALTY", -8))
    return FakeoutResult(False, "none", 0.0, "", 0)


# ── SR Flip ───────────────────────────────────────────────────────────────────

def detect_sr_flip(df, direction, bos_level) -> SRFlipResult:
    """
    Level previously acted as resistance → now support (for longs), or
    support → resistance (for shorts). Makes the OB stronger.
    """
    if not getattr(config, "USE_SR_FLIP", True):
        return SRFlipResult(False, None, "none", "disabled", 0)

    lb  = df.tail(getattr(config, "SR_FLIP_LOOKBACK", 60))
    tol = bos_level * 0.004
    res = sum(1 for _, c in lb.iterrows()
              if abs(c["high"] - bos_level) < tol and c["close"] < bos_level)
    sup = sum(1 for _, c in lb.iterrows()
              if abs(c["low"]  - bos_level) < tol and c["close"] > bos_level)
    bonus = getattr(config, "SR_FLIP_BONUS", 8)

    if direction == "long" and res >= 2:
        return SRFlipResult(True, bos_level, "resistance_to_support",
                            f"{bos_level:.2f} was resistance {res}× → now support", bonus)
    if direction == "short" and sup >= 2:
        return SRFlipResult(True, bos_level, "support_to_resistance",
                            f"{bos_level:.2f} was support {sup}× → now resistance", bonus)
    return SRFlipResult(False, None, "none", f"no flip at {bos_level:.2f}", 0)


# ── Compression ───────────────────────────────────────────────────────────────

def detect_compression(df, sweep_idx) -> CompressionResult:
    """
    Range contraction before the sweep (Flag B / CP pattern).
    Lower highs + higher lows = coiling before the move.
    Institutional accumulation/distribution marker.
    """
    if not getattr(config, "USE_COMPRESSION", True):
        return CompressionResult(False, 0.0, 0, "disabled", 0)

    n   = getattr(config, "COMPRESSION_LOOKBACK", 15)
    pre = df[df.index < sweep_idx].tail(n)
    if len(pre) < 8:
        return CompressionResult(False, 0.0, len(pre), "too few bars", 0)

    half = len(pre)//2
    r1   = pre["high"].iloc[:half].max() - pre["low"].iloc[:half].min()
    r2   = pre["high"].iloc[half:].max() - pre["low"].iloc[half:].min()
    pct  = 1.0 - (r2 / r1) if r1 > 0 else 0.0

    h2 = pre["high"].iloc[half:].values
    l2 = pre["low"].iloc[half:].values
    lh = all(h2[i] <= h2[i-1]*1.001 for i in range(1, len(h2)))
    hl = all(l2[i] >= l2[i-1]*0.999 for i in range(1, len(l2)))

    ok = pct >= getattr(config, "COMPRESSION_MIN_PCT", 0.30) or lh or hl
    if not ok:
        return CompressionResult(False, pct, len(pre),
                                 f"range -{pct:.0%} insufficient", 0)

    desc = ("lower highs" if lh else "higher lows" if hl else f"range -{pct:.0%}")
    return CompressionResult(True, pct, len(pre),
                             f"Compression: {desc} in {len(pre)} bars",
                             getattr(config, "COMPRESSION_BONUS", 6))


# ── MPL — Multiple Points of Liquidity ───────────────────────────────────────

def detect_mpl(df, sweep_level, sweep_idx) -> MPLResult:
    """
    How many times price touched the swept level before the final sweep.
    3+ touches = institutional stop hunt cluster → stronger reaction expected.
    5+ touches = very strong MPL.
    """
    if not getattr(config, "USE_MPL", True):
        return MPLResult(0, False, "disabled", 0)

    pre  = df[df.index < sweep_idx].tail(60)
    tol  = sweep_level * 0.003
    cnt  = sum(
        1 for _, c in pre.iterrows()
        if abs(c["high"] - sweep_level) < tol or abs(c["low"] - sweep_level) < tol
    )
    min_t = getattr(config, "MPL_MIN_TOUCHES", 3)
    if cnt < min_t:
        return MPLResult(cnt, False, f"{cnt} touches (need {min_t})", 0)

    bonus = (getattr(config, "MPL_STRONG_BONUS", 12) if cnt >= 5
             else getattr(config, "MPL_BONUS", 8))
    return MPLResult(cnt, True,
                     f"MPL {cnt}× touches at {sweep_level:.2f} — stop cluster",
                     bonus)