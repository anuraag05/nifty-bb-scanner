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
    python scanner.py -t 15m --scan ttl      (TTL entries / stops)
    python scanner.py -t 15m --scan combo    (BB upper + buy entry, BB lower + sell entry)
    python scanner.py -t 15m --universe "Nifty 200"   (top 200 by market cap)
    python scanner.py -t 5m --source dhan      (needs DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN env vars)
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
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

TTL_SIGNALS = [
    "buy_entry",       # TTL BUY entry on the bar
    "sell_entry",      # TTL SELL entry on the bar
    "buy_stop_hit",    # long trailing stop hit
    "sell_stop_hit",   # short trailing stop hit
]
COMBO_SIGNALS = [
    "combo_buy",       # TTL buy entry AND close above the upper Bollinger Band
    "combo_sell",      # TTL sell entry AND close below the lower Bollinger Band
]
MODES = {
    "bb": ("Bollinger Bands", SIGNALS, ["cross_above_upper", "cross_below_lower"]),
    "ttl": ("TTL (RSI / EMA trailing stop)", TTL_SIGNALS, ["buy_entry", "sell_entry"]),
    "combo": ("BB + TTL combo", COMBO_SIGNALS, ["combo_buy", "combo_sell"]),
}

_BUY_SIGNALS = {"buy_entry", "combo_buy", "cross_above_upper", "above_upper", "cross_above_mid"}
_SELL_SIGNALS = {"sell_entry", "combo_sell", "cross_below_lower", "below_lower", "cross_below_mid"}


def side_of(signals: str) -> str:
    """BUY / SELL / EXIT (a stop was hit, nothing new) / '' for a comma separated signal string."""
    sig = set(filter(None, signals.split(",")))
    b, s_ = bool(sig & _BUY_SIGNALS), bool(sig & _SELL_SIGNALS)
    if b and not s_:
        return "BUY"
    if s_ and not b:
        return "SELL"
    if b and s_:
        return "BUY/SELL"
    if sig & {"buy_stop_hit", "sell_stop_hit"}:
        return "EXIT"
    return ""


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


def download(symbols: list[str], tf: str, source: str = "yahoo",
             client_id: str = "", token: str = "", progress=None) -> dict[str, pd.DataFrame]:
    """Fetch OHLC data from the chosen source ('yahoo' or 'dhan').
    `progress(done, total)` is called as symbols complete (optional)."""
    if source == "dhan":
        import dhan_feed
        return dhan_feed.download(symbols, tf, client_id=client_id, token=token, progress=progress)
    return download_yahoo(symbols, tf, progress=progress)


YAHOO_BATCH = 100  # keep batches modest so Yahoo doesn't rate-limit big universes


def download_yahoo(symbols: list[str], tf: str, progress=None) -> dict[str, pd.DataFrame]:
    import time
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), YAHOO_BATCH):
        batch = symbols[i:i + YAHOO_BATCH]
        for attempt in range(2):
            try:
                out.update(_yahoo_batch(batch, tf))
                break
            except Exception:
                time.sleep(2)
        if progress:
            progress(min(i + YAHOO_BATCH, len(symbols)), len(symbols))
        if i + YAHOO_BATCH < len(symbols):
            time.sleep(1)
    return out


def _yahoo_batch(symbols: list[str], tf: str) -> dict[str, pd.DataFrame]:
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


def _recent(series: pd.Series, lookback: int) -> int | None:
    """Bars ago (0 = last bar) of the most recent True in the last `lookback` bars, else None."""
    tail = series.iloc[-lookback:].to_numpy()
    hits = [i for i, v in enumerate(tail) if v]
    return (len(tail) - 1 - hits[-1]) if hits else None


def scan(symbols: list[str], tf: str, include_forming: bool, data: dict | None = None,
         source: str = "yahoo", client_id: str = "", token: str = "", progress=None,
         mode: str = "bb", lookback: int = 1) -> pd.DataFrame:
    """mode: 'bb' Bollinger signals, 'ttl' TTL entries/stops, 'combo' TTL entry that is also outside the BB.
    lookback: for ttl/combo, how many of the latest closed bars may contain the signal (1 = last bar only)."""
    import ttl
    _, _, bar_len = TIMEFRAMES[tf]
    lookback = max(1, int(lookback))
    if data is None:
        data = download(symbols, tf, source, client_id, token, progress)
    rows = []
    for sym, df in data.items():
        if "Close" not in df.columns:
            continue
        if not include_forming:
            df = drop_forming_bar(df, bar_len, tf)
        df = df.dropna(subset=["Close"])
        close = df["Close"]
        if len(close) < max(EMA_LEN + 2, 30 if mode != "bb" else 0):
            continue
        bb = bollinger(close)
        last = bb.iloc[-1]
        row = {"symbol": sym.replace(".NS", ""), "time": close.index[-1], "close": round(last.close, 2)}

        if mode == "bb":
            width_pct = (last.upper - last.lower) / last.mid * 100
            row.update({
                "ema20": round(last.mid, 2), "upper": round(last.upper, 2), "lower": round(last.lower, 2),
                "band_width_%": round(width_pct, 2), "signals": ",".join(classify(bb)),
            })
        else:
            t = ttl.compute(df)
            lt = t.iloc[-1]
            pos = {1: "BUY", -1: "SELL", 0: "-"}[int(lt.position)]
            sl = lt.buy_sl if lt.position == 1 else lt.sell_sl if lt.position == -1 else np.nan
            fired, ago = [], []
            if mode == "ttl":
                for name, col in (("buy_entry", "buy_entry"), ("sell_entry", "sell_entry"),
                                  ("buy_stop_hit", "buy_exit"), ("sell_stop_hit", "sell_exit")):
                    k = _recent(t[col], lookback)
                    if k is not None:
                        fired.append(name)
                        ago.append(k)
            else:  # combo: the entry bar itself must be outside the band
                above = t["buy_entry"] & (bb["close"] > bb["upper"])
                below = t["sell_entry"] & (bb["close"] < bb["lower"])
                for name, series in (("combo_buy", above), ("combo_sell", below)):
                    k = _recent(series, lookback)
                    if k is not None:
                        fired.append(name)
                        ago.append(k)
                row.update({"upper": round(last.upper, 2), "lower": round(last.lower, 2)})
            row.update({
                "rsi": round(float(lt.rsi), 1) if pd.notna(lt.rsi) else np.nan,
                "rsi_ema": round(float(lt.rsi_ema), 1) if pd.notna(lt.rsi_ema) else np.nan,
                "position": pos,
                "stop_loss": round(float(sl), 2) if pd.notna(sl) else np.nan,
                "signals": ",".join(fired),
                "bars_ago": min(ago) if ago else np.nan,
            })
        row["side"] = side_of(row["signals"])
        rows.append(row)
    missing = sorted(s.replace(".NS", "") for s in set(symbols) - set(data))
    if missing:
        shown = ", ".join(missing[:15]) + (f" ... (+{len(missing) - 15} more)" if len(missing) > 15 else "")
        print(f"[warn] no data for {len(missing)} symbols: {shown}", file=sys.stderr)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description="Nifty Bollinger Band (20 EMA, 1 SD) scanner")
    p.add_argument("-t", "--timeframe", choices=TIMEFRAMES, default=None,
                   help="1m, 5m, 15m, 1h or 1d (prompted if omitted)")
    p.add_argument("--scan", choices=list(MODES), default="bb",
                   help="bb = Bollinger Bands (default), ttl = RSI/EMA trailing-stop entries and stops, "
                        "combo = TTL buy entry above upper band / TTL sell entry below lower band")
    p.add_argument("--lookback", type=int, default=1,
                   help="ttl/combo: accept a signal from any of the last N closed bars (default 1 = last bar only)")
    p.add_argument("-s", "--signal", choices=SIGNALS + TTL_SIGNALS + COMBO_SIGNALS, action="append",
                   help="only show these signals (repeatable). Default: show any signal of the chosen scan")
    p.add_argument("--universe", default=None,
                   help="NSE index list to scan, e.g. 'Nifty 500', 'Nifty 200', 'Nifty 100', 'Nifty 50', 'F&O' "
                        "(default: nifty50.txt). 'Nifty 500' = top 500 by market cap")
    p.add_argument("--symbols", type=Path, default=DEFAULT_SYMBOL_FILE,
                   help="text file with one symbol per line (used when --universe is not given)")
    p.add_argument("--source", choices=["yahoo", "dhan"], default="yahoo",
                   help="data feed. 'dhan' reads DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN from environment variables")
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

    if args.universe:
        import universe
        match = [k for k in universe.UNIVERSES if k.lower().startswith(args.universe.lower())]
        if not match:
            sys.exit(f"Unknown universe. Choose from: {', '.join(universe.UNIVERSES)}")
        names, note = universe.load(match[0])
        print(f"Universe: {match[0]} ({len(names)} stocks, {note})")
        symbols = [n + ".NS" for n in names]
    else:
        symbols = load_symbols(args.symbols)
    bad = [x for x in (args.signal or []) if x not in MODES[args.scan][1]]
    if bad:
        sys.exit(f"Signal(s) {', '.join(bad)} do not belong to --scan {args.scan}. Options: {', '.join(MODES[args.scan][1])}")
    print(f"[{args.source}] {MODES[args.scan][0]} scan of {len(symbols)} symbols on {tf} "
          f"| BB(EMA {EMA_LEN}, {STD_MULT} SD) ...")
    res = scan(symbols, tf, args.include_forming, source=args.source, mode=args.scan, lookback=args.lookback)
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
    colours = {"BUY": "\033[92m", "SELL": "\033[91m", "EXIT": "\033[93m", "BUY/SELL": "\033[95m"}
    with pd.option_context("display.width", 220, "display.max_columns", None):
        lines = res.to_string(index=False).splitlines()
        print(lines[0])
        for line, side in zip(lines[1:], res["side"]):
            c = colours.get(side, "")
            print(f"{c}{line}\033[0m" if c and sys.stdout.isatty() else line)
    print(f"\n{len(res)} result(s).")
    if args.csv:
        res.to_csv(args.csv, index=False)
        print(f"Saved to {args.csv}")


if __name__ == "__main__":
    main()
