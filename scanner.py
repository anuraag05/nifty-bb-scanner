#!/usr/bin/env python3
"""
Nifty Bollinger Band Scanner
----------------------------
Scans Nifty stocks (Yahoo Finance data) for Bollinger Band signals where the
middle band is a 20-period EMA and the bands are +/- 1 standard deviation
(of the close, 20-period rolling, population-free / sample std as in pandas).

Usage examples:
    python scanner.py -t 15m
    python scanner.py -t 1d --signal cross_above_upper
    python scanner.py -t 5m --symbols my_list.txt --csv out.csv
    python scanner.py -t 1h --include-forming
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------- settings
EMA_LEN = 20
STD_MULT = 1.0

# timeframe -> (yfinance interval, period to download, bar length)
# Yahoo limits: 1m = last 7 days, 5m/15m = last 60 days, 1h = last 730 days
TIMEFRAMES = {
    "1m": ("1m", "5d", timedelta(minutes=1)),
    "5m": ("5m", "30d", timedelta(minutes=5)),
    "15m": ("15m", "60d", timedelta(minutes=15)),
    "1h": ("60m", "180d", timedelta(hours=1)),
    "1d": ("1d", "1y", timedelta(days=1)),
}

SIGNALS = [
    "cross_above_upper",  # prev close <= upper, last close > upper (breakout)
    "cross_below_lower",  # prev close >= lower, last close < lower (breakdown)
    "above_upper",        # last close > upper (any time)
    "below_lower",        # last close < lower (any time)
    "cross_above_mid",    # crossed above the 20 EMA
    "cross_below_mid",    # crossed below the 20 EMA
]

DEFAULT_SYMBOL_FILE = Path(__file__).with_name("nifty50.txt")


# ---------------------------------------------------------------- helpers
def load_symbols(path: Path) -> list[str]:
    symbols = []
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip().upper()
        if not line:
            continue
        if not line.endswith(".NS") and not line.startswith("^"):
            line += ".NS"
        symbols.append(line)
    return symbols


def bollinger(close: pd.Series, ema_len: int = EMA_LEN, mult: float = STD_MULT) -> pd.DataFrame:
    """Bollinger Bands with an EMA midline."""
    mid = close.ewm(span=ema_len, adjust=False).mean()
    std = close.rolling(ema_len).std()
    return pd.DataFrame(
        {
            "close": close,
            "mid": mid,
            "upper": mid + mult * std,
            "lower": mid - mult * std,
        }
    )


def classify(bb: pd.DataFrame) -> list[str]:
    """Return the list of signals that fired on the last bar."""
    if len(bb) < 2:
        return []
    prev, last = bb.iloc[-2], bb.iloc[-1]
    if last[["upper", "lower", "mid"]].isna().any() or prev[["upper", "lower", "mid"]].isna().any():
        return []

    fired = []
    if prev.close <= prev.upper and last.close > last.upper:
        fired.append("cross_above_upper")
    if prev.close >= prev.lower and last.close < last.lower:
        fired.append("cross_below_lower")
    if last.close > last.upper:
        fired.append("above_upper")
    if last.close < last.lower:
        fired.append("below_lower")
    if prev.close <= prev.mid and last.close > last.mid:
        fired.append("cross_above_mid")
    if prev.close >= prev.mid and last.close < last.mid:
        fired.append("cross_below_mid")
    return fired


def drop_forming_bar(df: pd.DataFrame, bar_len: timedelta, tf: str) -> pd.DataFrame:
    """Remove the last candle if it has not closed yet (intraday only)."""
    if tf == "1d" or df.empty:
        return df
    last_start = df.index[-1]
    now = datetime.now(last_start.tzinfo) if last_start.tzinfo else datetime.now()
    # an hourly bar on NSE starting 15:15 only lasts 15 min, so use a safe rule:
    # if "now" is before bar start + bar length, the bar is still forming.
    if now < last_start + bar_len:
        return df.iloc[:-1]
    return df


def download(symbols: list[str], tf: str) -> dict[str, pd.DataFrame]:
    interval, period, _ = TIMEFRAMES[tf]
    raw = yf.download(
        tickers=symbols,
        interval=interval,
        period=period,
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
    )
    out = {}
    if raw is None or raw.empty:
        return out
    if len(symbols) == 1:
        out[symbols[0]] = raw.dropna(how="all")
        return out
    for sym in symbols:
        if sym in raw.columns.get_level_values(0):
            df = raw[sym].dropna(how="all")
            if not df.empty:
                out[sym] = df
    return out


def scan(symbols: list[str], tf: str, include_forming: bool) -> pd.DataFrame:
    _, _, bar_len = TIMEFRAMES[tf]
    data = download(symbols, tf)
    rows = []
    for sym, df in data.items():
        if "Close" not in df.columns:
            continue
        if not include_forming:
            df = drop_forming_bar(df, bar_len, tf)
        close = df["Close"].dropna()
        if len(close) < EMA_LEN + 2:
            continue
        bb = bollinger(close)
        last = bb.iloc[-1]
        width_pct = (last.upper - last.lower) / last.mid * 100
        rows.append(
            {
                "symbol": sym.replace(".NS", ""),
                "time": close.index[-1],
                "close": round(last.close, 2),
                "ema20": round(last.mid, 2),
                "upper": round(last.upper, 2),
                "lower": round(last.lower, 2),
                "band_width_%": round(width_pct, 2),
                "signals": ",".join(classify(bb)),
            }
        )
    missing = set(symbols) - set(data)
    if missing:
        print(f"[warn] no data for: {', '.join(sorted(s.replace('.NS', '') for s in missing))}", file=sys.stderr)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description="Nifty Bollinger Band (20 EMA, 1 SD) scanner")
    p.add_argument("-t", "--timeframe", choices=TIMEFRAMES, default=None,
                   help="1m, 5m, 15m, 1h or 1d (prompted if omitted)")
    p.add_argument("-s", "--signal", choices=SIGNALS, action="append",
                   help="only show these signals (repeatable). Default: show any signal")
    p.add_argument("--symbols", type=Path, default=DEFAULT_SYMBOL_FILE,
                   help="text file with one symbol per line (default: nifty50.txt)")
    p.add_argument("--include-forming", action="store_true",
                   help="include the currently forming (unclosed) candle")
    p.add_argument("--all", action="store_true", help="show every stock, even without a signal")
    p.add_argument("--csv", type=Path, help="also save results to this CSV file")
    args = p.parse_args()

    tf = args.timeframe
    if tf is None:
        tf = input(f"Timeframe {list(TIMEFRAMES)}: ").strip().lower()
        if tf not in TIMEFRAMES:
            sys.exit(f"Invalid timeframe: {tf}")

    symbols = load_symbols(args.symbols)
    print(f"Scanning {len(symbols)} symbols on {tf} | BB(EMA {EMA_LEN}, {STD_MULT} SD) ...")
    res = scan(symbols, tf, args.include_forming)
    if res.empty:
        sys.exit("No data returned (market closed / rate limited / bad symbols?).")

    if not args.all:
        if args.signal:
            mask = res["signals"].apply(lambda s: any(x in s.split(",") for x in args.signal))
        else:
            mask = res["signals"] != ""
        res = res[mask]

    if res.empty:
        print("No stocks match.")
        return

    res = res.sort_values("symbol").reset_index(drop=True)
    with pd.option_context("display.width", 200, "display.max_columns", None):
        print(res.to_string(index=False))
    print(f"\n{len(res)} result(s).")
    if args.csv:
        res.to_csv(args.csv, index=False)
        print(f"Saved to {args.csv}")


if __name__ == "__main__":
    main()
