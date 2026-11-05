# Nifty Bollinger Band Scanner

Scans Nifty 50 stocks using Yahoo Finance data for Bollinger Band signals.

- **Midline:** 20-period EMA
- **Bands:** EMA ± 1 standard deviation (20-period rolling std of close)
- **Timeframes:** `1m`, `5m`, `15m`, `1h`, `1d` (pick one per run)

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python scanner.py -t 15m                          # any signal, 15-minute candles
python scanner.py -t 1d -s cross_above_upper      # daily breakouts only
python scanner.py -t 5m -s below_lower -s cross_below_lower
python scanner.py -t 1h --all --csv out.csv       # every stock, save CSV
python scanner.py                                 # prompts for timeframe
```

| Option | Meaning |
|---|---|
| `-t, --timeframe` | `1m`, `5m`, `15m`, `1h`, `1d` |
| `-s, --signal` | Filter (repeatable). See signals below |
| `--symbols FILE` | Custom symbol list (one per line; `.NS` added automatically) |
| `--include-forming` | Include the still-open candle (default: closed candles only) |
| `--all` | Show all stocks, not just those with a signal |
| `--csv FILE` | Save results |

## Signals

| Signal | Condition on last closed candle |
|---|---|
| `cross_above_upper` | Previous close ≤ upper band, last close > upper band |
| `cross_below_lower` | Previous close ≥ lower band, last close < lower band |
| `above_upper` / `below_lower` | Close is currently outside the band |
| `cross_above_mid` / `cross_below_mid` | Close crossed the 20 EMA |

## Notes

- Yahoo history limits: 1m ≈ last 7 days, 5m/15m ≈ 60 days, 1h ≈ 2 years.
- Yahoo data is free and can be delayed or rate limited; it's fine for scanning, not for execution.
- `nifty50.txt` is editable; update it when NSE reconstitutes the index.
- Educational tool, not investment advice.
