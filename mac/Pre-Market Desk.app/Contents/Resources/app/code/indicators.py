"""
Technical indicators computed from daily OHLC bars.

Pure Python (no pandas needed) so it is easy to test and to reuse later in the
trading side. Every function takes plain lists ordered oldest -> newest and
returns a list of the same length, with None where there isn't enough data yet.

The main entry point is `analyze(bars)`, which returns a JSON-ready summary:
latest values, a plain-language reading for each indicator, pivot levels for
the next session, and an overall trend score.
"""
from __future__ import annotations

from typing import Optional

Num = Optional[float]


# ---------------------------------------------------------------- building blocks
def sma(values: list[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(values: list[float], n: int) -> list[Num]:
    """Exponential moving average, seeded with the simple average of the first n values."""
    out: list[Num] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: list[float], n: int, start: int = 0) -> list[Num]:
    """Wilder's smoothing (used by RSI and ATR). `start` skips leading undefined values."""
    out: list[Num] = [None] * len(values)
    if len(values) - start < n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(closes: list[float], n: int = 14) -> list[Num]:
    out: list[Num] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    for i in range(len(closes)):
        if ag[i] is None:
            continue
        out[i] = 100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
    return out


def true_range(high: list[float], low: list[float], close: list[float]) -> list[float]:
    tr = [high[0] - low[0]]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return tr


def atr(high: list[float], low: list[float], close: list[float], n: int = 14) -> list[Num]:
    return _wilder(true_range(high, low, close), n)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line: list[Num] = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[Num] = [None] * len(closes)
    if first is not None:
        tail = ema([v for v in line[first:]], signal)  # type: ignore[arg-type]
        sig[first:] = tail
    hist: list[Num] = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: list[float], n: int = 20, k: float = 2.0):
    mid = sma(closes, n)
    up: list[Num] = [None] * len(closes)
    lo: list[Num] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = (sum((x - m) ** 2 for x in window) / n) ** 0.5  # population SD, as most charting tools use
        up[i], lo[i] = m + k * sd, m - k * sd
    return up, mid, lo


def supertrend(high: list[float], low: list[float], close: list[float], n: int = 10, mult: float = 3.0):
    """Returns (line, direction) where direction is +1 (uptrend) or -1 (downtrend)."""
    a = atr(high, low, close, n)
    line: list[Num] = [None] * len(close)
    dirn: list[Optional[int]] = [None] * len(close)
    fu = fl = None
    for i in range(len(close)):
        if a[i] is None:
            continue
        hl2 = (high[i] + low[i]) / 2
        bu, bl = hl2 + mult * a[i], hl2 - mult * a[i]
        if fu is None:
            fu, fl, d = bu, bl, 1 if close[i] >= hl2 else -1
        else:
            fu = bu if (bu < fu or close[i - 1] > fu) else fu
            fl = bl if (bl > fl or close[i - 1] < fl) else fl
            d = dirn[i - 1] or 1
            if d == -1 and close[i] > fu:
                d = 1
            elif d == 1 and close[i] < fl:
                d = -1
        dirn[i] = d
        line[i] = fl if d == 1 else fu
    return line, dirn


def pivots(high: float, low: float, close: float) -> dict:
    """Classic floor pivots and the Central Pivot Range (CPR) for the next session."""
    p = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * p - bc
    rng = high - low
    return {
        "P": p, "R1": 2 * p - low, "S1": 2 * p - high,
        "R2": p + rng, "S2": p - rng, "R3": high + 2 * (p - low), "S3": low - 2 * (high - p),
        "cprTop": max(tc, bc), "cprBottom": min(tc, bc), "cprWidthPct": abs(tc - bc) / p * 100,
    }


# ---------------------------------------------------------------- summary for the dashboard
def _r(x: Num, d: int = 2) -> Num:
    return None if x is None else round(x, d)


def analyze(bars: list[dict], kind: str = "index") -> dict | None:
    """bars: [{"d": "YYYY-MM-DD", "o":..., "h":..., "l":..., "c":...}] oldest -> newest,
    completed sessions only. Needs ~60 bars; 200+ for the 200-day average."""
    if len(bars) < 35:
        return None
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    last = c[-1]
    e20, e50, e200 = ema(c, 20)[-1], ema(c, 50)[-1], ema(c, 200)[-1]
    r = rsi(c)
    rv, rprev = r[-1], r[-2]
    ml, ms, mh = macd(c)
    st_line, st_dir = supertrend(h, l, c)
    bu, bm, bl = bollinger(c)
    a14 = atr(h, l, c)[-1]
    year = bars[-252:]
    hi52, lo52 = max(b["h"] for b in year), min(b["l"] for b in year)

    rows, score = [], 0

    def add(name, value, reading, signal, weight=1):
        nonlocal score
        rows.append({"name": name, "value": value, "reading": reading, "signal": signal})
        score += weight * {"bull": 1, "bear": -1}.get(signal, 0)

    if kind == "vol":
        # For India VIX, report levels only; "bullish" VIX means more fear, so no trend score.
        add("20-day average", _r(sma(c, 20)[-1]), f"VIX is {'above' if last > (sma(c, 20)[-1] or last) else 'below'} its 20-day average", "neutral")
        add("RSI (14)", _r(rv, 1), "Fear rising fast" if (rv or 0) > 70 else "Fear unusually low" if (rv or 100) < 30 else "Normal range", "neutral")
        if bu[-1] is not None:
            add("Bollinger band (20, 2)", f"{bl[-1]:.2f} to {bu[-1]:.2f}",
                "Above the upper band: volatility spike" if last > bu[-1] else
                "Below the lower band: unusually calm" if last < bl[-1] else "Inside the bands", "neutral")
        return {"asOf": bars[-1]["d"], "close": _r(last), "rows": rows, "score": 0, "verdict": "Volatility gauge",
                "atr": _r(a14), "hi52": _r(hi52), "lo52": _r(lo52)}

    # Moving averages
    for n, v, w in ((20, e20, 1), (50, e50, 1), (200, e200, 2)):
        if v is None:
            add(f"{n}-day EMA", None, "Not enough history yet", "neutral", 0)
            continue
        gap = (last / v - 1) * 100
        add(f"{n}-day EMA", _r(v), f"Price {abs(gap):.1f}% {'above' if gap >= 0 else 'below'}",
            "bull" if gap > 0 else "bear", w)
    if e50 is not None and e200 is not None:
        add("50 vs 200 EMA", None, "50-day above 200-day (long-term uptrend)" if e50 > e200
            else "50-day below 200-day (long-term downtrend)", "bull" if e50 > e200 else "bear")

    # Momentum
    if rv is not None:
        if rv >= 70:
            reading, sig = "Overbought: strong, but stretched", "neutral"
        elif rv <= 30:
            reading, sig = "Oversold: weak, but stretched", "neutral"
        elif rv >= 55:
            reading, sig = "Bullish momentum", "bull"
        elif rv <= 45:
            reading, sig = "Bearish momentum", "bear"
        else:
            reading, sig = "Neutral momentum", "neutral"
        reading += f" ({'rising' if rprev is not None and rv > rprev else 'falling'})"
        add("RSI (14)", _r(rv, 1), reading, sig)

    if ml[-1] is not None and ms[-1] is not None:
        cross = ""
        if mh[-2] is not None and (mh[-1] > 0) != (mh[-2] > 0):
            cross = " Fresh crossover today."
        add("MACD (12, 26, 9)", _r(mh[-1]),
            ("MACD above signal line" if mh[-1] > 0 else "MACD below signal line")
            + (", histogram growing" if mh[-2] is not None and abs(mh[-1]) > abs(mh[-2]) else ", histogram shrinking")
            + "." + cross, "bull" if mh[-1] > 0 else "bear")

    # Trend-following
    if st_dir[-1] is not None:
        flipped = st_dir[-2] is not None and st_dir[-2] != st_dir[-1]
        add("Supertrend (10, 3)", _r(st_line[-1]),
            ("Uptrend" if st_dir[-1] == 1 else "Downtrend") + (", flipped today" if flipped else "")
            + f"; trailing level {st_line[-1]:,.0f}", "bull" if st_dir[-1] == 1 else "bear", 2)

    # Volatility / range
    if bu[-1] is not None:
        width = (bu[-1] - bl[-1]) / bm[-1] * 100
        pos = (last - bl[-1]) / (bu[-1] - bl[-1]) if bu[-1] != bl[-1] else 0.5
        add("Bollinger band (20, 2)", f"{bl[-1]:,.0f} to {bu[-1]:,.0f}",
            f"Price at {pos * 100:.0f}% of the band; band width {width:.1f}%"
            + (" (squeeze: a big move often follows)" if width < 3 else ""), "neutral")
    if a14 is not None:
        add("ATR (14)", _r(a14), f"Average daily range about {a14 / last * 100:.2f}% ({a14:,.0f} points)", "neutral")

    add("52-week range", f"{lo52:,.0f} to {hi52:,.0f}",
        f"{(1 - last / hi52) * 100:.1f}% below the high, {(last / lo52 - 1) * 100:.1f}% above the low", "neutral")

    max_score = sum({"20-day EMA": 1, "50-day EMA": 1, "200-day EMA": 2, "50 vs 200 EMA": 1, "RSI (14)": 1,
                     "MACD (12, 26, 9)": 1, "Supertrend (10, 3)": 2}.get(x["name"], 0) for x in rows)
    norm = score / max_score if max_score else 0
    verdict = ("Strong uptrend" if norm >= 0.6 else "Uptrend" if norm >= 0.25 else
               "Strong downtrend" if norm <= -0.6 else "Downtrend" if norm <= -0.25 else "Sideways / mixed")
    pv = pivots(h[-1], l[-1], last)
    return {
        "asOf": bars[-1]["d"], "close": _r(last), "rows": rows,
        "score": round(norm, 2), "verdict": verdict,
        "bull": sum(1 for x in rows if x["signal"] == "bull"),
        "bear": sum(1 for x in rows if x["signal"] == "bear"),
        "pivots": {k: _r(v) for k, v in pv.items()},
        "atr": _r(a14), "hi52": _r(hi52), "lo52": _r(lo52),
    }
