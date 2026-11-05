# Nifty Bollinger Band Scanner

Scans the **Nifty 200 (NSE's top 200 listed companies by market cap)** by default, or Nifty 500 / any other list, for Bollinger Band signals. Data from **Dhan** (DhanHQ v2) or **Yahoo Finance**. Use it as a web app or from the command line.

- **Midline:** 20-period EMA
- **Bands:** EMA ± 1 standard deviation (20-period rolling std of close)
- **Timeframes:** `1m`, `5m`, `15m`, `1h`, `1d` (pick one per run)

## Web app

```bash
pip install -r requirements.txt
streamlit run app.py          # or double-click run_app.bat on Windows
```

Pick the data source, scan type, timeframe and signals in the sidebar, then click **Scan**.

### Scan types
| Scan | What it finds |
|---|---|
| **Bollinger Bands** | 20 EMA midline, ±1 SD bands: breakouts / breakdowns / EMA crosses |
| **TTL (RSI / EMA trailing stop)** | Your TradingView "Tiger TTL" script: RSI(9) vs its EMA(5) gives BUY/SELL, with a 4-bar trailing stop. Signals: `buy_entry`, `sell_entry`, `buy_stop_hit`, `sell_stop_hit` |
| **BB + TTL combo** | `combo_buy` = TTL **buy entry** on a candle that closed **above the BB upper band**; `combo_sell` = TTL **sell entry** on a candle that closed **below the BB lower band** |

"Signal within last N candles" (default 1 = the latest closed candle) also catches signals from the last few candles; the `bars_ago` column says how old each one is.

Colours: 🟢 green = BUY, 🔴 red = SELL, 🟠 orange = stop hit, 🟣 purple = both. A strong tint is a fresh signal; in TTL scans with "show all" a light tint marks stocks already in a BUY/SELL trade (see the `position` and `stop_loss` columns). The chart shows the trailing stop (BUY SL red, SELL SL green, as in the Pine script), entry/stop markers and the RSI panel.

The TTL logic is a line-by-line port of `pinescript/tiger_ttl.pine` (`ttl.py`). Position and stops depend on history, so a short data window can differ slightly from the TradingView chart.

### Host it free (no command line)
1. Open https://share.streamlit.io and sign in with GitHub.
2. **Create app** -> choose this repo, branch `main`, main file `app.py` -> **Deploy**.
3. Optional: in the app's **Settings -> Secrets** add your Dhan credentials so you don't retype them:
   ```toml
   DHAN_CLIENT_ID = "your-client-id"
   DHAN_ACCESS_TOKEN = "your-token"
   ```
   Make the app **private** (Share settings) if you store secrets.

### Auto-refresh
Tick **Auto-refresh** in the sidebar (on by default, every 5 min) and the app re-scans itself using your current settings,
with a countdown to the next run. By default it only refreshes during NSE hours (9:15-15:30 IST, Mon-Fri; holidays are not checked).
If a refresh fails (for example your Dhan token expired) it keeps showing the last good scan and tries again next cycle.
Keep the browser tab open - the refresh runs while the page is open.

### Which stocks
The **Stocks to scan** dropdown offers Nifty 200 (default), 500, 100, 50, Midcap 150, Smallcap 250 and **F&O stocks** (every stock with futures & options),
downloaded live from NSE (cached 24 h). Choose **Upload CSV** (needs a `Symbol` column) or **Custom list** for your own set,
for example a list ranked exactly the way you want from a screener.
The F&O list comes from NSE's lot-size file; if NSE blocks it, the app derives the list from the stock futures in Dhan's public instrument list.
For the other lists, if NSE blocks the download, save the CSV as `universes/ind_nifty500list.csv` and it will be used as a fallback.

Scan time: Dhan allows about 5 requests/second, so 200 stocks take roughly 40 seconds (500 stocks about 2 minutes) on the first scan
(a progress bar shows it). Repeat scans within the cache window (1 to 30 min depending on timeframe) are instant.
Yahoo scans in batches of 100 and is usually quicker but can rate limit.

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
python scanner.py -t 15m                          # any signal, 15-minute candles (Nifty 50 file)
python scanner.py -t 15m --universe "Nifty 200"   # top 200 by market cap
python scanner.py -t 15m --scan ttl               # TTL entries (add --lookback 3 for the last 3 candles)
python scanner.py -t 15m --scan combo             # BB upper + buy entry / BB lower + sell entry
python scanner.py -t 15m --universe "F&O"         # all F&O stocks (quote it in the terminal)
python scanner.py -t 1d -s cross_above_upper      # daily breakouts only
python scanner.py -t 5m -s below_lower -s cross_below_lower
python scanner.py -t 1h --all --csv out.csv       # every stock, save CSV
python scanner.py                                 # prompts for timeframe
```

| Option | Meaning |
|---|---|
| `-t, --timeframe` | `1m`, `5m`, `15m`, `1h`, `1d` |
| `--scan` | `bb` (default), `ttl` or `combo` |
| `--lookback N` | ttl/combo: accept signals from the last N candles |
| `-s, --signal` | Filter (repeatable). See signals below |
| `--universe` | `Nifty 500`, `Nifty 200`, `Nifty 100`, `Nifty 50`, `F&O`, ... (downloads the NSE list) |
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

- Dhan: up to 90 days of intraday history per request, about 5 requests/second (50 stocks ~15 s, 500 stocks ~2 min).
- Yahoo history limits: 1m ≈ last 7 days, 5m/15m ≈ 60 days, 1h ≈ 2 years.
- Yahoo data is free and can be delayed or rate limited; it's fine for scanning, not for execution.
- Index lists are reconstituted by NSE twice a year; the app always pulls the current one.
- Educational tool, not investment advice.
