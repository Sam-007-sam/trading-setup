"""
Risk profile and position sizing for the Trade desk.

The morning questions turn into a profile: whether the user may trade today, in paper or
live mode, how much they may lose today, per trade, and on how many trades. Sizing then
turns a setup (entry and stop) into a quantity whose loss at the stop stays inside that
budget. If even one lot would exceed it, the answer is "don't trade", never a smaller stop.

A stop-loss limits a loss; it cannot prevent one. Gaps and fast markets can fill a stop
worse than its price, which is why sizing leaves a slippage allowance.
"""
from __future__ import annotations

import math

# NSE lot sizes since 30 Dec 2025. Live mode reads the current size from the broker instead.
LOT_SIZE = {"nifty50": 65, "banknifty": 30}
FUTURES_MARGIN_PCT = 15.0     # rough SPAN + exposure margin as % of contract value; the broker's figure rules
SLIPPAGE_ALLOWANCE = 0.15     # size as if the stop fills 15% worse than planned
MAX_DAILY_LOSS_PCT = 5.0      # hard cap on the daily loss limit, whatever the user enters

QUIZ = [
    {"id": "stop", "q": "Your stop-loss is 50 points away and you hold 1 lot of 65. Roughly how much do you lose if it is hit?",
     "options": ["₹650", "₹3,250", "₹32,500"], "answer": 1,
     "why": "Loss = points × quantity: 50 × 65 = ₹3,250, before charges and slippage."},
    {"id": "option", "q": "You buy a Nifty call option and Nifty falls sharply. What happens to the option's price?",
     "options": ["It rises", "It falls, and can go close to zero by expiry", "It stays the same"], "answer": 1,
     "why": "A call gains when the index rises and loses value when it falls; time decay also eats it every day."},
    {"id": "limit", "q": "You have hit today's loss limit and see a great-looking setup. What should you do?",
     "options": ["Take it to win the loss back", "Double the size", "Stop for the day"], "answer": 2,
     "why": "The daily limit only protects you if it is final. Chasing losses is how small losses become large ones."},
]

QUESTIONS = [
    {"id": "trade", "q": "Do you want to trade today?", "type": "yesno"},
    {"id": "capital", "q": "How much money is available for trading today (₹)?", "type": "number", "min": 1000},
    {"id": "maxLoss", "q": "What is the most you are willing to lose today (₹)?", "type": "number", "min": 100},
    {"id": "maxTrades", "q": "At most how many trades today?", "type": "choice", "options": [1, 2, 3]},
    {"id": "instrument", "q": "Trade futures or buy options?", "type": "choice", "options": ["options", "futures"]},
    {"id": "experience", "q": "How long have you traded F&O?", "type": "choice",
     "options": ["Never or under 6 months", "6 months to 2 years", "Over 2 years"]},
]


def evaluate(answers: dict, live_enabled: bool) -> dict:
    """Turn the morning answers into today's rules."""
    notes = []
    if not answers.get("trade"):
        return {"allowed": False, "reason": "You chose not to trade today. Good days to sit out are part of trading."}
    capital = float(answers.get("capital") or 0)
    max_loss = float(answers.get("maxLoss") or 0)
    if capital < 1000 or max_loss <= 0:
        return {"allowed": False, "reason": "Enter the money available and a loss limit above zero."}
    cap = capital * MAX_DAILY_LOSS_PCT / 100
    if max_loss > cap:
        notes.append(f"Your loss limit was capped at {MAX_DAILY_LOSS_PCT:g}% of capital: ₹{cap:,.0f} instead of ₹{max_loss:,.0f}.")
        max_loss = cap
    trades = int(answers.get("maxTrades") or 1)
    quiz = answers.get("quiz") or {}
    wrong = [q for q in QUIZ if quiz.get(q["id"]) != q["answer"]]
    new = str(answers.get("experience", "")).startswith("Never")
    mode = "live" if live_enabled else "paper"
    if wrong:
        mode = "paper"
        notes.append("Paper mode today: " + " ".join(q["why"] for q in wrong))
    if new and mode == "live":
        mode = "paper"
        notes.append("Paper mode while you are new to F&O: build a record first; switch to live once you have one.")
    if not live_enabled:
        notes.append("Live trading is not switched on in this app, so trades are simulated with real prices.")
    instrument = answers.get("instrument") if answers.get("instrument") in ("options", "futures") else "options"
    if instrument == "futures" and new:
        instrument = "options"
        notes.append("Futures need large margin and have open-ended risk, so options buying is used while you are new.")
    return {"allowed": True, "mode": mode, "capital": round(capital), "maxLoss": round(max_loss),
            "maxTrades": trades, "perTradeRisk": round(max_loss / trades), "instrument": instrument, "notes": notes,
            "quizWrong": [q["id"] for q in wrong]}


# ---------------------------------------------------------------- option premium (paper mode)
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_premium(spot: float, strike: float, days: float, iv_pct: float, kind: str, rate: float = 0.065) -> float:
    """Black-Scholes price; paper mode uses India VIX as the volatility."""
    t = max(days, 0.02) / 365
    v = max(iv_pct, 1) / 100
    d1 = (math.log(spot / strike) + (rate + v * v / 2) * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    if kind == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-rate * t) * _ncdf(d2)
    return strike * math.exp(-rate * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot, strike, days, iv_pct, kind, rate=0.065) -> float:
    t = max(days, 0.02) / 365
    v = max(iv_pct, 1) / 100
    d1 = (math.log(spot / strike) + (rate + v * v / 2) * t) / (v * math.sqrt(t))
    return _ncdf(d1) if kind == "CE" else _ncdf(d1) - 1


# ---------------------------------------------------------------- sizing
def size_futures(market: str, entry: float, stop: float, risk_rupees: float, capital: float,
                 lot_size: int | None = None) -> dict:
    lot = lot_size or LOT_SIZE[market]
    pts = abs(entry - stop)
    loss_per_lot = pts * lot * (1 + SLIPPAGE_ALLOWANCE)
    margin_per_lot = entry * lot * FUTURES_MARGIN_PCT / 100
    lots = min(math.floor(risk_rupees / loss_per_lot) if loss_per_lot else 0,
               math.floor(capital / margin_per_lot) if margin_per_lot else 0)
    if lots < 1:
        why = (f"one lot would risk about ₹{loss_per_lot:,.0f} at the stop, above your ₹{risk_rupees:,.0f} per-trade limit"
               if risk_rupees < loss_per_lot else
               f"one lot needs about ₹{margin_per_lot:,.0f} margin, more than your ₹{capital:,.0f}")
        return {"ok": False, "reason": f"Can't size this safely: {why}."}
    return {"ok": True, "instrument": "futures", "lots": lots, "qty": lots * lot, "lotSize": lot,
            "riskAtStop": round(pts * lots * lot), "marginEstimate": round(margin_per_lot * lots),
            "stopPoints": round(pts, 2)}


def size_option(market: str, spot: float, stop: float, side: int, risk_rupees: float, capital: float,
                days_to_expiry: float, vix: float, premium: float | None = None, strike: float | None = None,
                lot_size: int | None = None) -> dict:
    """Buy the at-the-money call (side +1) or put (side -1). The option's stop is set where the
    index stop would put it (by delta), and never more than 35% below the entry premium."""
    lot = lot_size or LOT_SIZE[market]
    step = 50 if market == "nifty50" else 100
    k = strike or round(spot / step) * step
    kind = "CE" if side > 0 else "PE"
    prem = premium or bs_premium(spot, k, days_to_expiry, vix, kind)
    delta = abs(bs_delta(spot, k, days_to_expiry, vix, kind))
    prem_stop = max(prem * 0.65, prem - delta * abs(spot - stop))
    loss_per_lot = (prem - prem_stop) * lot * (1 + SLIPPAGE_ALLOWANCE)
    cost_per_lot = prem * lot
    lots = min(math.floor(risk_rupees / loss_per_lot) if loss_per_lot > 0 else 0, math.floor(capital / cost_per_lot))
    if lots < 1:
        why = (f"one lot would risk about ₹{loss_per_lot:,.0f} at the stop, above your ₹{risk_rupees:,.0f} per-trade limit"
               if risk_rupees < loss_per_lot else f"one lot costs about ₹{cost_per_lot:,.0f}, more than your ₹{capital:,.0f}")
        return {"ok": False, "reason": f"Can't size this safely: {why}."}
    return {"ok": True, "instrument": "option", "kind": kind, "strike": k, "lots": lots, "qty": lots * lot,
            "lotSize": lot, "premium": round(prem, 2), "premiumStop": round(prem_stop, 2), "delta": round(delta, 2),
            "riskAtStop": round((prem - prem_stop) * lots * lot), "cost": round(prem * lots * lot),
            "premiumEstimated": premium is None}
