# Pre-Market Desk

A morning dashboard for Indian index traders. Every weekday at about 8:15 AM IST a GitHub
Action collects overnight global cues, India's previous close, FII/DII flows, Nifty and Bank
Nifty option positioning, news, and 1-month day-wise history for Nifty 50, Bank Nifty,
FinNifty, Midcap Select, Sensex and India VIX. It commits the results as JSON, and GitHub
Pages serves the dashboard.

## How it fits together

- `backtest.py` tests simple strategies (Supertrend, EMA crossover, RSI pullback, 200-day trend filter) on 5 years of Nifty and Bank Nifty data, with next-day-open execution and 0.05% cost per side, and writes `docs/data/backtest.json` for the Backtest tab.
- `intraday.py` backtests intraday rules (opening range breakout, VWAP cross, CPR open) on the last 60 days of 15-minute candles, squared off by 3:15 PM, and writes `docs/data/intraday.json`.
- `paper.py` paper-trades the daily strategies with ₹10 lakh of fake money each, starting from its first run, and writes `docs/data/paper.json`. No real orders are placed.
- `server.py` is the local server the Mac app uses to refresh data on launch.
- `mac/` holds the Mac app bundle (`Pre-Market Desk.app`) and its install notes.

- `fetch.py` pulls the data and writes `docs/data/`.
- `indicators.py` computes daily technical indicators (EMA 20/50/200, RSI, MACD, Supertrend, Bollinger, ATR, pivots and CPR) and a trend verdict per index. It has no dependencies, so the trading code can reuse it later.
- `docs/index.html` is the dashboard. It reads `docs/data/` (the same page also runs inside Claude).
- `.github/workflows/premarket.yml` runs `fetch.py` on schedule and commits the new data.

## Setup (about 10 minutes)

1. Create a new GitHub repository and push this folder to it.
2. **Settings → Pages**: set Source to "Deploy from a branch", branch `main`, folder `/docs`.
   Your dashboard will be at `https://<username>.github.io/<repo>/`.
   (Pages on a private repo needs a paid GitHub plan. On a free plan the repo and site are public.
   The data is only market data, but keep that in mind.)
3. **Settings → Secrets and variables → Actions → New repository secret**:
   `ANTHROPIC_API_KEY` = your key from console.anthropic.com. Optional but recommended: it writes
   the bias summary, news, stocks in focus, levels, GIFT Nifty and watchlist notes. Without it you
   get the numbers, a rule-based bias and raw RSS headlines.
4. **Actions tab → Pre-market brief → Run workflow** to test it once.

## Your watchlist

Edit `docs/data/watchlist.json`:

```json
{"items": [{"sym": "HDFCBANK", "note": "swing long"}, {"sym": "TATAMOTORS", "note": ""}]}
```

The morning run checks news for each symbol.

## Run it on your own computer

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...                     # optional
python fetch.py
python -m http.server -d docs 8000                  # open http://localhost:8000
```

## Known limits

- **GitHub's scheduler can start jobs 5 to 30 minutes late** at busy times, which is why the run is
  set for 8:15 IST. If you need it exact, run `fetch.py` from your own machine with cron or
  Windows Task Scheduler instead.
- **NSE often blocks cloud servers**, so the option chain and FII/DII calls may fail from GitHub.
  The brief still builds: FII/DII then comes from the AI news search, and the options panel is
  hidden. Running `fetch.py` from your home internet connection usually works.
- **GIFT Nifty** has no free, reliable API; it comes from the AI news search.
- **Yahoo Finance tickers** occasionally change. They're listed at the top of `fetch.py`.
  If an index fails, the page keeps the previous day's data and the footer says what failed.
- The model name is set by the `ANTHROPIC_MODEL` environment variable (default in `fetch.py`).

Information only, not investment advice.
