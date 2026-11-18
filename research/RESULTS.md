# Intraday rule research

512 variants of the opening-range breakout. Chosen on data up to 2024-12-31, tested once on later data. Costs 0.012% per side. A rule must be profitable (profit factor > 1) on both Nifty and Bank Nifty.

- nifty50: 2022-01-04 to 2026-11-17, 739 training and 464 test sessions
- banknifty: 2022-01-04 to 2026-11-17, 739 training and 464 test sessions

Share of ALL variants profitable on both indices: training 4%, test 0%.

## Top variants chosen on training data

| Variant | Train PF (Nifty / Bank) | Test PF (Nifty / Bank) | Test return (Nifty / Bank) | Test trades |
|---|---|---|---|---|
| ORB 30m, dir=cpr, target=noneR, trail=no, entries<13:00, stop=range, gap-skip=0.5 | 1.174 / 1.17 | 0.87 / 0.73 | -3.95% / -6.96% | 159 / 125 |
| ORB 30m, dir=cpr, target=2R, trail=no, entries<13:00, stop=range, gap-skip=0.5 | 1.135 / 1.141 | 0.89 / 0.727 | -3.37% / -7.04% | 159 / 125 |
| ORB 30m, dir=trend, target=noneR, trail=no, entries<13:00, stop=range, gap-skip=0.5 | 1.178 / 1.111 | 0.884 / 0.967 | -4.15% / -1.15% | 178 / 144 |
| ORB 30m, dir=trend, target=noneR, trail=no, entries<11:00, stop=range, gap-skip=0.5 | 1.171 / 1.105 | 0.863 / 1.108 | -3.96% / 2.18% | 134 / 108 |
| ORB 15m, dir=cpr, target=noneR, trail=no, entries<13:00, stop=range, gap-skip=0.5 | 1.129 / 1.086 | 0.952 / 0.682 | -1.64% / -11.69% | 178 / 158 |

**Survivors (profitable in training and test, both indices): 0**

