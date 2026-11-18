"""
Test harness: runs the Trade desk through a live session in PAPER mode, acting as a user who
confirms every suggestion, so the desk can be checked on real market days without anyone
watching. Used by .github/workflows/desk-test.yml. Never trades real money: it forces paper.

Two test accounts run side by side (futures and options); each day's state is saved under
docs/data/desk-test-<name>/<date>.json and committed every 30 minutes.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
import traceback

os.environ.pop("LIVE_TRADING", None)          # paper only, whatever the environment says
os.environ.pop("UPSTOX_ACCESS_TOKEN", None)

import desk as D
import risk as R

QUIZ_OK = {q["id"]: q["answer"] for q in R.QUIZ}
ACCOUNTS = {
    "futures": {"trade": True, "capital": 500000, "maxLoss": 10000, "maxTrades": 2, "instrument": "futures",
                "experience": "Over 2 years", "quiz": QUIZ_OK},
    "options": {"trade": True, "capital": 50000, "maxLoss": 2500, "maxTrades": 2, "instrument": "options",
                "experience": "Over 2 years", "quiz": QUIZ_OK},
}
STOP_AT = "15:17"


def commit(msg: str) -> None:
    if os.environ.get("NO_COMMIT"):
        return
    cmds = [["git", "add", "docs/data"], ["git", "commit", "-q", "-m", msg],
            ["git", "pull", "--rebase", "-q"], ["git", "push", "-q"]]
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode and c[1] != "commit":          # "nothing to commit" is fine
            print("git:", " ".join(c), r.stderr.strip()[:200], flush=True)


def main() -> int:
    feed = D.Feed()
    desks = {name: D.Desk(feed=feed, subdir=f"desk-test-{name}") for name in ACCOUNTS}
    for name, d in desks.items():
        st = d.load()
        if not st.get("profile"):
            d.start(ACCOUNTS[name])
        print(name, "profile:", d.state["profile"].get("mode"), d.state["profile"].get("maxLoss"), flush=True)
    last_commit = time.time()
    while True:
        now = dt.datetime.now(D.IST)
        if now.strftime("%H:%M") >= STOP_AT or now.weekday() >= 5:
            break
        for name, d in desks.items():
            try:
                d.tick()
                if not d.state["position"]:
                    r = d.suggest()
                    for m in r.get("markets", []):
                        if m.get("signal") and m.get("size", {}).get("ok"):
                            res = d.confirm(m["id"])
                            print(now.strftime("%H:%M:%S"), name, "confirm", m["id"], res.get("error") or "OK", flush=True)
                            break
            except Exception:
                traceback.print_exc()
        if time.time() - last_commit > 1800:
            commit(f"Desk test {now:%H:%M} IST")
            last_commit = time.time()
        time.sleep(20)
    for name, d in desks.items():
        try:
            d.tick()                                   # 3:15 square-off happens here if still open
        except Exception:
            traceback.print_exc()
        v = d.view()
        print(name, "trades:", [(t["name"], t["why"], t["pnl"]) for t in v["trades"]], "day:", v["realized"], flush=True)
    commit(f"Desk test end of day {dt.datetime.now(D.IST):%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
