"""
Intraday rule research with a proper out-of-sample test.

  python research.py            (needs network: Upstox public candles)

Protocol, fixed BEFORE looking at any results:
  * Data: Upstox 15-minute candles for Nifty 50 and Nifty Bank, Jan 2022 to the last close.
  * TRAIN = 2022-01-01 .. 2024-12-31. Every variant is scored here only.
  * TEST  = 2025-01-01 .. latest. The top variants by TRAIN score are judged here once.
  * Score = profit factor after costs, needing at least 150 trades per index in TRAIN.
    A variant must be profitable on BOTH indices to count.
  * Luck check: the share of ALL variants that are profitable in TEST on both indices. If that
    share is high, the market simply favoured the family in the test years and a "winner"
    proves little; if it is low and the TRAIN-chosen variants still pass, that means more.
  * Costs and fills: identical to intraday.py (next-candle fills, stops before targets,
    3:15 PM square-off, intraday.COST_PCT per side).

Writes docs/data/research.json and research/RESULTS.md.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import pathlib
import sys

import indicators as I
import intraday as X

ROOT = pathlib.Path(__file__).resolve().parent
TRAIN_END = "2024-12-31"
MIN_TRADES = 150
TOP_N = 5


# ---------------------------------------------------------------- a family of rules
def make_orb(range_bars, direction, target_r, trail, last_entry, stop_mode, skip_gap):
    """Opening-range breakout with options.
    direction: none | trend | cpr | both (trend and CPR agree)
    stop_mode: range (other side of the range) | mid (middle of the range)
    skip_gap: skip days opening more than this % away from the previous close (None = never)."""
    def rule(i, bars, state, ctx):
        if i < range_bars or state["n"] >= 1 or state["pos"] or bars[i]["t"] >= last_entry:
            return None
        hi = max(b["h"] for b in bars[:range_bars])
        lo = min(b["l"] for b in bars[:range_bars])
        c = bars[i]["c"]
        if (hi - lo) / c * 100 > X.ORB_SKIP_RANGE_PCT:
            return None
        if skip_gap is not None and ctx.get("prevClose") and \
                abs(bars[0]["o"] / ctx["prevClose"] - 1) * 100 > skip_gap:
            return None
        cpr = ctx.get("cpr") or {}
        cpr_side = 1 if bars[0]["o"] > cpr.get("cprTop", 9e9) else -1 if bars[0]["o"] < cpr.get("cprBottom", -1) else 0
        allowed = {"none": {1, -1}, "trend": {ctx.get("trend", 0)}, "cpr": {cpr_side},
                   "both": {ctx.get("trend", 0)} & {cpr_side}}[direction] - {0}
        side = 1 if c > hi else -1 if c < lo else 0
        if not side or side not in allowed:
            return None
        stop = (lo if side > 0 else hi) if stop_mode == "range" else (hi + lo) / 2
        return {"side": side, "stop": stop, "targetR": target_r, "trail": trail}
    return rule


GRID = {
    "range_bars": [1, 2],                     # 15 or 30-minute opening range
    "direction": ["none", "trend", "cpr", "both"],
    "target_r": [1, 1.5, 2, None],            # None = hold to the stop or 3:15
    "trail": [False, True],
    "last_entry": ["11:00", "13:00"],
    "stop_mode": ["range", "mid"],
    "skip_gap": [None, 0.5],
}
# Round 2 (added after round 1 found no survivors) tests mean reversion. Both rounds share
# the same test period, so they are reported together and research stops after round 2.


def make_gap_fade(min_gap, confirm, fill, stop_mult):
    """Big opening gap: trade back toward the previous close after the first candle.
    confirm: the first candle must already move toward the previous close.
    fill: target = this fraction of the gap closed. Stop: beyond the first candle's extreme,
    by stop_mult x that candle's range."""
    def rule(i, bars, state, ctx):
        if i != 0 or state["n"] >= 1 or not ctx.get("prevClose"):
            return None
        pc, o, b0 = ctx["prevClose"], bars[0]["o"], bars[0]
        gap = (o / pc - 1) * 100
        if abs(gap) < min_gap:
            return None
        side = -1 if gap > 0 else 1
        if confirm and side * (b0["c"] - o) <= 0:
            return None
        rng = max(b0["h"] - b0["l"], o * 0.001)
        stop = (b0["h"] + (stop_mult - 1) * rng) if side < 0 else (b0["l"] - (stop_mult - 1) * rng)
        entry = b0["c"]
        target = entry + side * abs(entry - pc) * fill
        risk = abs(entry - stop)
        if risk <= 0 or side * (target - entry) <= 0:
            return None
        return {"side": side, "stop": stop, "targetR": abs(target - entry) / risk, "trail": False}
    return rule


def make_range_fade(range_bars, last_entry, target, direction):
    """Failed breakout: a candle closes outside the opening range, the next closes back inside;
    trade back into the range. Stop at the breakout candle's extreme; target the range middle
    or its far side. direction: none | trend (only fade breakouts against the daily trend)."""
    def rule(i, bars, state, ctx):
        if i < range_bars + 1 or state["n"] >= 1 or state["pos"] or bars[i]["t"] >= last_entry:
            return None
        hi = max(b["h"] for b in bars[:range_bars])
        lo = min(b["l"] for b in bars[:range_bars])
        prev, c = bars[i - 1], bars[i]["c"]
        if prev["c"] > hi and lo < c < hi:
            side, stop = -1, max(prev["h"], bars[i]["h"])
        elif prev["c"] < lo and lo < c < hi:
            side, stop = 1, min(prev["l"], bars[i]["l"])
        else:
            return None
        if direction == "trend" and side != ctx.get("trend", 0):
            return None
        tgt = (hi + lo) / 2 if target == "mid" else (lo if side < 0 else hi)
        risk = abs(c - stop)
        if risk <= 0 or side * (tgt - c) <= 0:
            return None
        return {"side": side, "stop": stop, "targetR": abs(tgt - c) / risk, "trail": False}
    return rule


GAP_GRID = {"min_gap": [0.3, 0.5, 0.8], "confirm": [False, True], "fill": [0.5, 1.0], "stop_mult": [1.0, 1.5]}
FADE_GRID = {"range_bars": [1, 2], "last_entry": ["11:00", "13:00"], "target": ["mid", "far"],
             "direction": ["none", "trend"]}


def _grid(g):
    keys = list(g)
    for combo in itertools.product(*(g[k] for k in keys)):
        yield dict(zip(keys, combo))


def variants():
    for p in _grid(GRID):
        yield {"family": "orb", **p}, make_orb(**p)
    for p in _grid(GAP_GRID):
        yield {"family": "gapfade", **p}, make_gap_fade(**p)
    for p in _grid(FADE_GRID):
        yield {"family": "rangefade", **p}, make_range_fade(**p)


def label(p):
    if p["family"] == "gapfade":
        return (f"Gap fade: gap>{p['min_gap']}%, confirm={'yes' if p['confirm'] else 'no'}, "
                f"target {int(p['fill'] * 100)}% fill, stop {p['stop_mult']}x first candle")
    if p["family"] == "rangefade":
        return (f"Failed-breakout fade {15 * p['range_bars']}m, entries<{p['last_entry']}, target={p['target']}, "
                f"dir={p['direction']}")
    return (f"ORB {15 * p['range_bars']}m, dir={p['direction']}, target={p['target_r'] or 'none'}R, "
            f"trail={'BE' if p['trail'] else 'no'}, entries<{p['last_entry']}, stop={p['stop_mode']}, "
            f"gap-skip={p['skip_gap'] or 'no'}")


# ---------------------------------------------------------------- evaluation
def contexts(sessions):
    """Per-session context from earlier sessions only: trend, CPR, previous close."""
    out = []
    for k in range(len(sessions)):
        if k == 0:
            out.append(None)
            continue
        prev = sessions[k - 1][1]
        out.append({"cpr": I.pivots(max(b["h"] for b in prev), min(b["l"] for b in prev), prev[-1]["c"]),
                    "trend": X.daily_trend([s[1][-1]["c"] for s in sessions[max(0, k - 20):k]]),
                    "prevClose": prev[-1]["c"]})
    return out


def score(sessions, ctxs, fn, period):
    pnl, wins, losses, n, eq, peak, mdd = [], 0.0, 0.0, 0, 1.0, 1.0, 0.0
    for (d, bars), ctx in zip(sessions, ctxs):
        if ctx is None or (period == "train" and d > TRAIN_END) or (period == "test" and d <= TRAIN_END):
            continue
        for t in X.simulate_day(bars, fn, ctx):
            r = t["pct"]
            n += 1
            wins += max(r, 0)
            losses += -min(r, 0)
            eq *= 1 + r / 100
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
    return {"trades": n, "pf": round(wins / losses, 3) if losses else None,
            "totalPct": round((eq - 1) * 100, 2), "maxDdPct": round(mdd * 100, 2)}


def research(data: dict) -> dict:
    """data: {market: candles}. Returns every variant's TRAIN and TEST scores per market."""
    prepared = {}
    for m, candles in data.items():
        ss = X.group_sessions(candles)
        prepared[m] = (ss, contexts(ss))
    rows = []
    for p, fn in variants():
        row = {"params": p, "label": label(p), "train": {}, "test": {}}
        for m, (ss, cx) in prepared.items():
            row["train"][m] = score(ss, cx, fn, "train")
            row["test"][m] = score(ss, cx, fn, "test")
        rows.append(row)

    def eligible(r):
        return all(r["train"][m]["trades"] >= MIN_TRADES and (r["train"][m]["pf"] or 0) > 0 for m in prepared)

    def train_key(r):          # the weaker index decides: a rule must work on both
        return min(r["train"][m]["pf"] or 0 for m in prepared)

    def passes(r, period):
        return all((r[period][m]["pf"] or 0) > 1 for m in prepared)

    chosen = sorted([r for r in rows if eligible(r)], key=train_key, reverse=True)[:TOP_N]
    luck = sum(passes(r, "test") for r in rows) / len(rows)
    survivors = [r for r in chosen if passes(r, "train") and passes(r, "test")]
    spans = {m: {"from": ss[1][0], "to": ss[-1][0], "sessions": len(ss) - 1,
                 "trainSessions": sum(1 for d, _ in ss[1:] if d <= TRAIN_END),
                 "testSessions": sum(1 for d, _ in ss[1:] if d > TRAIN_END)} for m, (ss, _) in prepared.items()}
    return {"variants": len(rows), "trainEnd": TRAIN_END, "minTrades": MIN_TRADES, "spans": spans,
            "costPct": X.COST_PCT, "chosen": chosen, "survivors": survivors,
            "shareAllPassingTest": round(luck, 3),
            "shareAllPassingTrain": round(sum(passes(r, "train") for r in rows) / len(rows), 3),
            "families": {f: {"n": sum(1 for r in rows if r["params"]["family"] == f),
                             "passTest": sum(1 for r in rows if r["params"]["family"] == f and passes(r, "test")),
                             "best": max((r for r in rows if r["params"]["family"] == f and eligible(r)),
                                         key=train_key, default=None)}
                         for f in ("orb", "gapfade", "rangefade")},
            "testPfAll": sorted(min(r["test"][m]["pf"] or 0 for m in prepared) for r in rows)}


def write_report(res: dict, path: pathlib.Path) -> None:
    fams = {}
    for f in res.get("families", {}):
        fams[f] = res["families"][f]
    lines = ["# Intraday rule research", "",
             f"{res['variants']} variants: opening-range breakouts (round 1), gap fades and failed-breakout fades (round 2). Chosen on data up to {res['trainEnd']}, "
             f"tested once on later data. Costs {res['costPct']}% per side. A rule must be profitable "
             f"(profit factor > 1) on both Nifty and Bank Nifty.", ""]
    for m, s in res["spans"].items():
        lines.append(f"- {m}: {s['from']} to {s['to']}, {s['trainSessions']} training and {s['testSessions']} test sessions")
    for f, v in fams.items():
        lines.append(f"- {f}: {v['n']} variants, {v['passTest']} profitable on both indices in the test period")
    lines += ["", f"Share of ALL variants profitable on both indices: training {res['shareAllPassingTrain']:.0%}, "
                  f"test {res['shareAllPassingTest']:.0%}.", "", "## Top variants chosen on training data", "",
              "| Variant | Train PF (Nifty / Bank) | Test PF (Nifty / Bank) | Test return (Nifty / Bank) | Test trades |",
              "|---|---|---|---|---|"]
    for r in res["chosen"]:
        ms = list(r["train"])
        lines.append(f"| {r['label']} | " + " / ".join(str(r["train"][m]["pf"]) for m in ms) + " | "
                     + " / ".join(str(r["test"][m]["pf"]) for m in ms) + " | "
                     + " / ".join(f"{r['test'][m]['totalPct']}%" for m in ms) + " | "
                     + " / ".join(str(r["test"][m]["trades"]) for m in ms) + " |")
    lines += ["", f"**Survivors (profitable in training and test, both indices): {len(res['survivors'])}**", ""]
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    import upstox_data
    end = dt.date.today()
    data = {}
    for m, key in (("nifty50", "NSE_INDEX|Nifty 50"), ("banknifty", "NSE_INDEX|Nifty Bank")):
        data[m] = upstox_data.candles(key, "minutes", 15, dt.date(2022, 1, 3), end)
        print(m, len(data[m]), "candles", flush=True)
    res = research(data)
    (ROOT / "docs" / "data" / "research.json").write_text(json.dumps(res))
    write_report(res, ROOT / "research" / "RESULTS.md")
    print((ROOT / "research" / "RESULTS.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
