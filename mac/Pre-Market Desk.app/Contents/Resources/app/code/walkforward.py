"""
Walk-forward research: does tuning a strategy on the past help in the future?

For each strategy family we try a grid of settings, with and without a volatility filter.
Then, rolling forward one year at a time:
  1. TRAIN: on the previous TRAIN_YEARS of data, pick the setting with the best Sharpe
     above cash. If no setting beats cash (Sharpe <= 0), the choice is to hold cash.
  2. TEST: run that setting on the next TEST_YEARS, which it has never seen.
The test years are stitched together into one out-of-sample record and compared with
buy-and-hold and the liquid fund over exactly the same days.

Execution and costs are identical to backtest.py. Each test year starts flat, so a
position held across a year boundary is closed and re-opened (paying costs twice).

Writes docs/data/walkforward.json.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import sys

import backtest as B
import indicators as I

log = logging.getLogger("walkforward")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

TRAIN_YEARS, TEST_YEARS, HISTORY_YEARS = 3, 1, 11
SESSIONS_PER_YEAR = 248


# ---------------------------------------------------------------- parameterised strategies
def vol_ok(bars: list[dict]) -> list[bool]:
    """True when ATR as % of price is at or below its own 1-year median (calmer than usual)."""
    h, l, c = [b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars]
    a = I.atr(h, l, c, 14)
    pct = [None if x is None else x / p for x, p in zip(a, c)]
    out = []
    for i in range(len(bars)):
        win = [x for x in pct[max(0, i - 251):i + 1] if x is not None]
        out.append(bool(win) and pct[i] is not None and pct[i] <= sorted(win)[len(win) // 2])
    return out


def ema_cross(bars, fast, slow):
    c = [b["c"] for b in bars]
    f, s = I.ema(c, fast), I.ema(c, slow)
    return [0 if a is None or z is None else (1 if a > z else 0) for a, z in zip(f, s)]


def rsi_pullback(bars, entry, exit_):
    c = [b["c"] for b in bars]
    r, e = I.rsi(c, 14), I.ema(c, 200)
    pos, out = 0, []
    for i in range(len(c)):
        if r[i] is None or e[i] is None:
            out.append(0)
            continue
        if pos == 0 and c[i] > e[i] and r[i] < entry:
            pos = 1
        elif pos == 1 and (r[i] > exit_ or c[i] < e[i]):
            pos = 0
        out.append(pos)
    return out


def supertrend_long(bars, n, mult):
    _, d = I.supertrend([b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars], n, mult)
    return [1 if v == 1 else 0 for v in d]


FAMILIES = {
    "ema": ("EMA crossover", ema_cross,
            [(f, s) for f, s in itertools.product((10, 20, 30), (50, 100, 200)) if f < s],
            lambda p: f"EMA {p[0]}/{p[1]}"),
    "rsi": ("RSI pullback", rsi_pullback,
            list(itertools.product((30, 35, 40), (55, 60, 70))),
            lambda p: f"RSI buy < {p[0]}, sell > {p[1]}"),
    "supertrend": ("Supertrend, long only", supertrend_long,
                   list(itertools.product((7, 10, 14), (2, 3, 4))),
                   lambda p: f"Supertrend ({p[0]}, {p[1]})"),
}


def candidates(bars):
    """Every (family, label, target list) on the full history. Targets only use past bars."""
    vf = vol_ok(bars)
    out = []
    for fam, (_, fn, grid, label) in FAMILIES.items():
        for p in grid:
            base = fn(bars, *p)
            out.append((fam, label(p), base))
            out.append((fam, label(p) + " + calm-market filter", [t if ok else 0 for t, ok in zip(base, vf)]))
    return out


# ---------------------------------------------------------------- walk-forward engine
def segment(bars, target, start, end):
    """Run the engine on bars[start:end] (start >= 1), flat at the start."""
    return B.run(bars[:end], target[:end], start)


def sharpe(res):
    return res["stats"]["sharpe"] if res["stats"]["sharpe"] is not None else 0.0


def walk_forward(bars: list[dict]) -> dict:
    train_n, test_n = TRAIN_YEARS * SESSIONS_PER_YEAR, TEST_YEARS * SESSIONS_PER_YEAR
    first = B.WARMUP + 252                     # indicators and the volatility filter need history
    cands = candidates(bars)
    folds, oos = [], {fam: [] for fam in list(FAMILIES) + ["any"]}
    t0 = first + train_n
    while t0 + 20 < len(bars):
        t1 = min(len(bars), t0 + test_n)
        fold = {"train": [bars[t0 - train_n]["d"], bars[t0 - 1]["d"]], "test": [bars[t0]["d"], bars[t1 - 1]["d"]],
                "picks": {}}
        scored = [(fam, lab, tgt, sharpe(segment(bars, tgt, t0 - train_n, t0))) for fam, lab, tgt in cands]
        for fam in list(FAMILIES) + ["any"]:
            pool = [x for x in scored if fam == "any" or x[0] == fam]
            best = max(pool, key=lambda x: x[3])
            if best[3] <= 0:                    # nothing beat cash in training: hold cash
                lab, tgt, tr = "Hold cash (nothing beat cash in training)", [0] * len(bars), 0.0
            else:
                _, lab, tgt, tr = best
            res = segment(bars, tgt, t0, t1)
            oos[fam].append(res)
            fold["picks"][fam] = {"setting": lab, "trainSharpe": round(tr, 2),
                                  "testSharpe": res["stats"]["sharpe"], "testReturnPct": res["stats"]["totalPct"]}
        folds.append(fold)
        t0 = t1
    start = next(i for i, b in enumerate(bars) if b["d"] == folds[0]["test"][0])
    hold = B.run(bars, [1] * len(bars), start, cost_pct=0)
    cash = B.run(bars, [0] * len(bars), start)
    families = []
    for fam in list(FAMILIES) + ["any"]:
        curve, trades = stitch(oos[fam])
        st = B.stats(curve, trades, 0, None, [bars[start - 1]["d"]] + [d for d, _ in curve])
        tr_avg = sum(f["picks"][fam]["trainSharpe"] for f in folds) / len(folds)
        families.append({"id": fam, "name": "Best of all families" if fam == "any" else FAMILIES[fam][0],
                         "stats": st, "avgTrainSharpe": round(tr_avg, 2), "curve": B.thin(curve),
                         "cashYears": sum(1 for f in folds if f["picks"][fam]["setting"].startswith("Hold cash"))})
    return {"from": bars[start]["d"], "to": bars[-1]["d"], "folds": folds, "families": families,
            "buyHold": {"stats": hold["stats"], "curve": B.thin(hold["curve"])}, "cash": {"stats": cash["stats"]},
            "trainYears": TRAIN_YEARS, "testYears": TEST_YEARS}


def stitch(results):
    curve, trades, level = [], [], 1.0
    for r in results:
        for d, e in r["curve"]:
            curve.append((d, level * e))
        level = curve[-1][1]
        trades += [t for t in r["trades"] if not t.get("open")]
    return curve, trades


def main() -> int:
    import fetch
    now = dt.datetime.now(fetch.IST)
    out = {"generated": now.isoformat(timespec="minutes"), "costPct": B.COST_PCT,
           "cashRatePct": B.CASH_RATE_PCT, "markets": []}
    for mid, name, ticker in B.MARKETS:
        try:
            bars = fetch.completed_bars(fetch.yf_ohlc(ticker, period=f"{HISTORY_YEARS}y"), now.date(), now)
            bars = [b for b in bars if b["o"] > 0 and b["h"] >= b["l"]]
            out["markets"].append({"id": mid, "name": name, **walk_forward(bars)})
            log.info("%s: %d folds", name, len(out["markets"][-1]["folds"]))
        except Exception as e:
            log.warning("%s failed: %s", name, e)
    if not out["markets"]:
        return 1
    (B.DATA / "walkforward.json").write_text(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
