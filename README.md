# Nifty Bollinger Band Scanner

Scans Nifty 50 stocks for Bollinger Band signals. Data from **Dhan** (DhanHQ v2) or **Yahoo Finance**. Use it as a web app or from the command line.

- **Midline:** 20-period EMA
- **Bands:** EMA ± 1 standard deviation (20-period rolling std of close)
- **Timeframes:** `1m`, `5m`, `15m`, `1h`, `1d` (pick one per run)

## Web app

```bash
pip install -r requirements.txt
streamlit run app.py          # or double-click run_app.bat on Windows
```

Pick the data source, timeframe and signals in the sidebar, then click **Scan**.

### Host it free (no command line)
1. Open https://share.streamlit.io and sign in with GitHub.
2. **Create app** -> choose this repo, branch `main`, main file `app.py` -> **Deploy**.
3. Optional: in the app's **Settings -> Secrets** add your Dhan credentials so you don't retype them:
   ```toml
   DHAN_CLIENT_ID = "your-client-id"
   DHAN_ACCESS_TOKEN = "your-token"
   ```
   Make the app **private** (Share settings) if you store secrets.

### Dhan credentials
Never commit them. Use the sidebar fields, Streamlit secrets, or environment variables
`DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` (CLI: `python scanner.py -t 5m --source dhan`).
Console-generated Dhan tokens expire after 24 hours, so paste a fresh one when the app reports an auth error.

## Command line

Setup:

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
| `--source` | `yahoo` (default) or `dhan` |
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

- Dhan: up to 90 days of intraday history per request, about 5 requests/second (a full scan takes ~15 s).
- Yahoo history limits: 1m ≈ last 7 days, 5m/15m ≈ 60 days, 1h ≈ 2 years.
- Yahoo data is free and can be delayed or rate limited; it's fine for scanning, not for execution.
- `nifty50.txt` is editable; update it when NSE reconstitutes the index.
- Educational tool, not investment advice.
