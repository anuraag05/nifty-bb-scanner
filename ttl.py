"""
Python port of the "Tiger TTL" Pine Script (see pinescript/tiger_ttl.pine).

Logic, bar by bar (identical to the Pine state machine):
    lr   = RSI(close, 9)                      (Wilder / RMA, like ta.rsi)
    lm   = EMA(lr, 5)
    buy  = lr > lm          sell = lr < lm
    buystop  = lowest(low, 4)   sellstop = highest(high, 4)   (current bar included)

    position  0 flat / 1 BUY / -1 SELL
    BUY open : stop ratchets up  (max of old stop and buystop); close < stop  -> BUY STOP HIT, flat
    SELL open: stop ratchets down(min of old stop and sellstop); close > stop -> SELL STOP HIT, flat
    same-bar reversal: BUY stop hit and sell  -> SELL ENTRY ; SELL stop hit and buy -> BUY ENTRY
    flat: buy -> BUY ENTRY, elif sell -> SELL ENTRY

Because position/stop depend on the whole history, results can differ slightly from TradingView
if the data window is shorter than the chart you are comparing with.
"""

import math

import numpy as np
import pandas as pd

RSI_LEN = 9
RSI_EMA_LEN = 5
STOP_LEN = 4


def _rma(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.rma: SMA seed over the first n valid values, then Wilder smoothing."""
    out = np.full(len(x), np.nan)
    idx = np.flatnonzero(~np.isnan(x))
    if len(idx) < n:
        return out
    start = idx[0]
    seed = start + n - 1
    out[seed] = np.mean(x[start:seed + 1])
    a = 1.0 / n
    for i in range(seed + 1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.ema: seeded with the first valid value."""
    out = np.full(len(x), np.nan)
    idx = np.flatnonzero(~np.isnan(x))
    if len(idx) == 0:
        return out
    a = 2.0 / (n + 1)
    out[idx[0]] = x[idx[0]]
    for i in range(idx[0] + 1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1] if not np.isnan(x[i]) else out[i - 1]
    return out


def rsi(close: np.ndarray, n: int = RSI_LEN) -> np.ndarray:
    change = np.diff(close, prepend=np.nan)
    up = np.where(np.isnan(change), np.nan, np.maximum(change, 0.0))
    dn = np.where(np.isnan(change), np.nan, np.maximum(-change, 0.0))
    u, d = _rma(up, n), _rma(dn, n)
    out = np.full(len(close), np.nan)
    ok = ~np.isnan(u) & ~np.isnan(d)
    with np.errstate(divide="ignore", invalid="ignore"):
        val = 100.0 - 100.0 / (1.0 + u / d)
    out[ok] = val[ok]
    out[ok & (d == 0)] = 100.0
    out[ok & (u == 0) & (d != 0)] = 0.0
    return out


def _nan_max(a: float, b: float) -> float:  # Pine math.max returns na if either is na
    return math.nan if math.isnan(a) or math.isnan(b) else max(a, b)


def _nan_min(a: float, b: float) -> float:
    return math.nan if math.isnan(a) or math.isnan(b) else min(a, b)


def compute(df: pd.DataFrame, rsi_len: int = RSI_LEN, ema_len: int = RSI_EMA_LEN,
            stop_len: int = STOP_LEN) -> pd.DataFrame:
    """df needs High, Low, Close columns. One row per bar: rsi, rsi_ema, position (1/-1/0), buy_sl / sell_sl
    (the live trailing stop, NaN when not in that trade) and the boolean event columns buy_entry, sell_entry,
    buy_exit (BUY STOP HIT), sell_exit (SELL STOP HIT)."""
    high = df["High"].to_numpy(float)
    low = df["Low"].to_numpy(float)
    close = df["Close"].to_numpy(float)
    n = len(close)

    lr = rsi(close, rsi_len)
    lm = _ema(lr, ema_len)
    lowest = pd.Series(low).rolling(stop_len).min().to_numpy()
    highest = pd.Series(high).rolling(stop_len).max().to_numpy()

    pos = 0
    buy_sl = sell_sl = math.nan
    names = ("buy", "sell", "buy_entry", "sell_entry", "buy_exit", "sell_exit")
    cols = {k: np.zeros(n, dtype=bool) for k in names}
    position = np.zeros(n, dtype=int)
    buy_stop = np.full(n, np.nan)
    sell_stop = np.full(n, np.nan)

    for i in range(n):
        buy = bool(lr[i] > lm[i])   # NaN compares False, like na in Pine
        sell = bool(lr[i] < lm[i])
        buy_entry = sell_entry = buy_exit = sell_exit = False

        if pos == 1:
            buy_sl = _nan_max(buy_sl, lowest[i])
            if close[i] < buy_sl:
                buy_exit, pos, buy_sl = True, 0, math.nan
        if pos == -1:
            sell_sl = _nan_min(sell_sl, highest[i])
            if close[i] > sell_sl:
                sell_exit, pos, sell_sl = True, 0, math.nan

        if buy_exit and sell:          # BUY stop hit -> reverse to SELL
            sell_entry, pos, sell_sl = True, -1, highest[i]
        if sell_exit and buy:          # SELL stop hit -> reverse to BUY
            buy_entry, pos, buy_sl = True, 1, lowest[i]

        if pos == 0:
            if buy:
                buy_entry, pos, buy_sl = True, 1, lowest[i]
            elif sell:
                sell_entry, pos, sell_sl = True, -1, highest[i]

        cols["buy"][i], cols["sell"][i] = buy, sell
        cols["buy_entry"][i], cols["sell_entry"][i] = buy_entry, sell_entry
        cols["buy_exit"][i], cols["sell_exit"][i] = buy_exit, sell_exit
        position[i] = pos
        if pos == 1:
            buy_stop[i] = buy_sl
        elif pos == -1:
            sell_stop[i] = sell_sl

    return pd.DataFrame({"rsi": lr, "rsi_ema": lm, "position": position,
                         "buy_sl": buy_stop, "sell_sl": sell_stop, **cols}, index=df.index)
