#!/usr/bin/env python3
"""
BB + TTL trading bot (intraday, NSE cash equities, Dhan).

STRATEGY (same signals as the scanner's "BB + TTL combo"):
    BUY   when a TTL buy entry happens on a candle that closes ABOVE the Bollinger upper band
    SELL  when a TTL sell entry happens on a candle that closes BELOW the Bollinger lower band
    EXIT  when the TTL trailing stop is hit (close beyond the 4-bar trailing low/high), or at square-off time

SAFETY
    * PAPER mode is the default: it simulates fills and never sends an order.
    * LIVE mode needs  --live  AND a typed confirmation (or BOT_CONFIRM_LIVE=I_UNDERSTAND for unattended runs).
    * Risk limits: risk per trade, capital per trade, max open positions, max daily loss, entry window,
      automatic square-off. Create a file named STOP next to this script to flatten everything and exit.
    * Live orders need your machine's STATIC IP whitelisted in Dhan (Dhan requires it for order APIs).
    * Credentials come only from the environment: DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN.

Run:  python bot.py            (paper)        python bot.py --live        (real orders)
"""

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import dhan_feed
import scanner
import ttl
import universe

IST = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).parent
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
MIN_BARS = 50  # indicator warm-up before a signal is trusted


# ------------------------------------------------------------------ config
@dataclass
class Config:
    timeframe: str = "15m"            # 1m / 5m / 15m / 1h  (200 stocks on Dhan = ~40 s per fetch: use 5m or slower)
    universe: str = "Nifty 200"       # name from the app's list ("Nifty 200", "F&O", ...) or a path to a symbols file
    source: str = "dhan"              # price data: dhan or yahoo
    allow_long: bool = True
    allow_short: bool = True          # intraday short selling of equities
    risk_per_trade: float = 500.0     # INR you are willing to lose if the stop is hit (sizes the quantity)
    capital_per_trade: float = 20000.0  # INR cap on position value per trade
    max_stop_pct: float = 3.0         # skip trades whose stop is further than this % from entry
    max_open_positions: int = 5
    max_daily_loss: float = 2000.0    # INR; reached -> flatten everything and stop for the day
    first_entry_time: str = "09:30"
    no_new_entries_after: str = "14:45"
    square_off_time: str = "15:15"
    max_signal_age_sec: int = 180     # only trade a signal if its candle closed this recently (no chasing)
    data_delay_sec: int = 10          # wait after candle close before fetching
    slippage_pct: float = 0.05        # paper trading only
    product_type: str = "INTRADAY"

    @staticmethod
    def load(path: Path | None) -> "Config":
        cfg = Config()
        if path and path.exists():
            raw = json.loads(path.read_text())
            known = {f.name for f in fields(Config)}
            bad = set(raw) - known
            if bad:
                sys.exit(f"Unknown setting(s) in {path.name}: {', '.join(sorted(bad))}")
            for k, v in raw.items():
                setattr(cfg, k, v)
        if cfg.timeframe not in TF_MIN:
            sys.exit(f"timeframe must be one of {list(TF_MIN)}")
        if cfg.source not in ("dhan", "yahoo"):
            sys.exit("source must be dhan or yahoo")
        return cfg


def parse_hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


# ------------------------------------------------------------------ time helpers
def bar_end(start: pd.Timestamp, bar_len: timedelta) -> pd.Timestamp:
    """When a candle that starts at `start` finishes (the last hourly candle ends at 15:30, not 16:15)."""
    end = start + bar_len
    close = start.replace(hour=15, minute=30, second=0, microsecond=0)
    return min(end, close) if start < close else end


def closed(df: pd.DataFrame | None, now: datetime, bar_len: timedelta):
    """Drop the still-forming last candle."""
    if df is None or df.empty:
        return df
    return df.iloc[:-1] if bar_end(df.index[-1], bar_len) > now else df


def check_times(day: date, cfg: Config) -> list[datetime]:
    """Candle-close times at which the bot wakes up (plus the square-off time)."""
    step = timedelta(minutes=TF_MIN[cfg.timeframe])
    t = datetime.combine(day, MARKET_OPEN, IST) + step
    out = []
    while t.time() <= MARKET_CLOSE:
        out.append(t)
        t += step
    for extra in (datetime.combine(day, MARKET_CLOSE, IST), datetime.combine(day, parse_hm(cfg.square_off_time), IST)):
        if extra not in out:
            out.append(extra)
    return sorted(out)


# ------------------------------------------------------------------ brokers
class OrderRejected(Exception):
    """The exchange/broker definitely did not take the order."""


class OrderUnknown(Exception):
    """We cannot tell whether the order went through (network error / 5xx). Never retry blindly."""


@dataclass
class Fill:
    qty: int
    price: float
    order_id: str = ""


class PaperBroker:
    name = "paper"

    def __init__(self, slippage_pct: float = 0.05):
        self.slip = slippage_pct / 100.0

    def place(self, symbol: str, side: str, qty: int, ref_price: float, cid: str) -> Fill:
        px = ref_price * (1 + self.slip) if side == "BUY" else ref_price * (1 - self.slip)
        return Fill(qty, round(px, 2), f"paper-{cid}")

    def lookup(self, cid: str):
        return None


def _first(js):
    """Dhan answers are sometimes an object, a list or wrapped in 'data'."""
    if isinstance(js, dict) and "data" in js:
        js = js["data"]
    if isinstance(js, list):
        js = js[0] if js else {}
    return js if isinstance(js, dict) else {}


class DhanBroker:
    """Real orders via DhanHQ v2. NOT testable from the build sandbox - verify in paper first, then go small."""
    name = "live"

    def __init__(self, client_id: str, token: str, symbols: list[str], product: str = "INTRADAY",
                 fill_timeout: float = 10.0):
        self.client_id, self.token, self.product, self.fill_timeout = client_id, token, product, fill_timeout
        self.ids = dhan_feed.load_security_ids(symbols)
        self.headers = dhan_feed._headers(token, client_id)

    def _req(self, method: str, path: str, **kw):
        return requests.request(method, dhan_feed.BASE + path, headers=self.headers, timeout=15, **kw)

    def _info(self, order_id: str) -> dict:
        r = self._req("GET", f"/orders/{order_id}")
        r.raise_for_status()
        return _first(r.json())

    def place(self, symbol: str, side: str, qty: int, ref_price: float, cid: str) -> Fill:
        symbol = symbol.replace(".NS", "")      # the bot works with Yahoo-style names; Dhan's table uses bare symbols
        sid = self.ids.get(symbol)
        if not sid:
            raise OrderRejected(f"{symbol}: no Dhan securityId")
        body = {
            "dhanClientId": self.client_id, "correlationId": cid, "transactionType": side,
            "exchangeSegment": "NSE_EQ", "productType": self.product, "orderType": "MARKET",
            "validity": "DAY", "securityId": sid, "quantity": int(qty), "disclosedQuantity": 0,
            "price": 0, "triggerPrice": 0, "afterMarketOrder": False,
        }
        try:
            r = self._req("POST", "/orders", json=body)
        except requests.RequestException as e:
            raise OrderUnknown(f"network error placing {side} {qty} {symbol}: {e}")
        if r.status_code >= 500:
            raise OrderUnknown(f"Dhan server error {r.status_code} placing {side} {qty} {symbol}: {r.text[:200]}")
        if r.status_code >= 400:
            hint = "  (is this machine's static IP whitelisted in Dhan?)" if "ip" in r.text.lower() else ""
            raise OrderRejected(f"HTTP {r.status_code}: {r.text[:300]}{hint}")
        oid = _first(r.json()).get("orderId")
        if not oid:
            raise OrderRejected(f"no orderId in response: {r.text[:300]}")
        return self._await_fill(str(oid), qty)

    def _fill_from(self, info: dict, oid: str) -> Fill | None:
        filled = int(info.get("filledQty") or 0)
        if filled > 0:
            return Fill(filled, float(info.get("averageTradedPrice") or 0), oid)
        return None

    def _await_fill(self, oid: str, qty: int) -> Fill:
        deadline = time.time() + self.fill_timeout
        info: dict = {}
        while time.time() < deadline:
            try:
                info = self._info(oid)
            except requests.RequestException:
                time.sleep(0.5)
                continue
            status = str(info.get("orderStatus", "")).upper()
            if status == "TRADED":
                return self._fill_from(info, oid) or Fill(qty, 0.0, oid)
            if status in ("REJECTED", "CANCELLED", "EXPIRED"):
                f = self._fill_from(info, oid)
                if f:
                    return f
                raise OrderRejected(f"{status}: {info.get('omsErrorDescription', '')}")
            time.sleep(0.5)
        # not filled in time: cancel, then see what actually happened
        try:
            self._req("DELETE", f"/orders/{oid}")
            time.sleep(0.5)
            info = self._info(oid)
        except requests.RequestException as e:
            raise OrderUnknown(f"order {oid} state unknown after cancel attempt: {e}")
        f = self._fill_from(info, oid)
        if f:
            return f
        raise OrderRejected(f"order {oid} not filled within {self.fill_timeout}s and was cancelled")

    def lookup(self, cid: str):
        """Find an order we may have placed before a crash, via its correlation id."""
        r = self._req("GET", f"/orders/external/{cid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        info = _first(r.json())
        return self._fill_from(info, str(info.get("orderId", ""))) if info else None

    def preflight(self, log=print):
        ks = self._req("GET", "/killswitch")
        status = _first(ks.json()).get("killSwitchStatus") if ks.ok else None
        log(f"kill switch: {status or 'unknown (HTTP %s)' % ks.status_code}")
        if status == "ACTIVATE":
            raise SystemExit("Dhan kill switch is ACTIVE - orders will be blocked. Aborting.")
        fl = self._req("GET", "/fundlimit")
        if fl.ok:
            f = _first(fl.json())
            log(f"available balance: {f.get('availabelBalance', f.get('availableBalance'))}")
        po = self._req("GET", "/positions")
        if po.ok:
            open_ = [p for p in (po.json() if isinstance(po.json(), list) else []) if int(p.get("netQty") or 0) != 0]
            if open_:
                log(f"WARNING: {len(open_)} open position(s) already in your Dhan account: "
                    + ", ".join(f"{p.get('tradingSymbol')} {p.get('netQty')}" for p in open_)
                    + ". The bot does not manage positions it did not open.")


# ------------------------------------------------------------------ the bot
class Bot:
    def __init__(self, cfg: Config, broker, state_path: Path, trades_path: Path, log=None, today: date | None = None):
        self.cfg, self.broker = cfg, broker
        self.state_path, self.trades_path = state_path, trades_path
        self.say = log or (lambda m: print(f"{datetime.now(IST):%H:%M:%S} {m}", flush=True))
        self.seq = 0
        self.state = self._load(today or datetime.now(IST).date())

    # ---- state
    def _fresh(self, day: date) -> dict:
        return {"date": str(day), "realized": 0.0, "halted": False, "halt_reason": "", "positions": {},
                "seen": [], "pending": {}, "n_trades": 0}

    def _load(self, day: date) -> dict:
        if self.state_path.exists():
            st = json.loads(self.state_path.read_text())
            if st.get("date") == str(day):
                return st
            if st.get("positions") or st.get("pending"):
                self.say(f"WARNING: discarding positions/pending orders from {st.get('date')} - intraday positions "
                         f"should have been squared off. Check your broker account.")
        return self._fresh(day)

    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=str))
        tmp.replace(self.state_path)

    def _log_trade(self, now, action, sym, side, qty, price, reason, pnl=0.0, order_id=""):
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "mode", "action", "symbol", "side", "qty", "price", "pnl", "reason", "order_id"])
            w.writerow([now.isoformat(timespec="seconds"), self.broker.name, action, sym, side, qty,
                        round(price, 2), round(pnl, 2), reason, order_id])
        self.state["n_trades"] += 1

    # ---- order plumbing (state is saved BEFORE an order is sent, so a crash can be reconciled)
    def _send(self, sym, side, qty, ref, meta) -> Fill | None:
        self.seq += 1
        cid = f"bb{int(time.time() * 1000) % 10**11}{self.seq}"
        self.state["pending"][cid] = {"symbol": sym, "side": side, "qty": qty, **meta}
        self.save()
        try:
            fill = self.broker.place(sym, side, qty, ref, cid)
        except OrderRejected as e:
            self.state["pending"].pop(cid, None)
            self.save()
            self.say(f"ORDER REJECTED {side} {qty} {sym}: {e}")
            return None
        except OrderUnknown as e:
            self.state["halted"], self.state["halt_reason"] = True, f"order state unknown: {e}"
            self.save()
            self.say(f"!!! {e}\n!!! New entries halted. Check your Dhan order book, then restart the bot (it will reconcile).")
            return None
        self.state["pending"].pop(cid, None)
        return fill

    def _apply_entry(self, now, sym, side, fill: Fill, stop, last_bar, reason="BB+TTL signal"):
        self.state["positions"][sym] = {"side": side, "qty": fill.qty, "entry": fill.price, "stop": float(stop),
                                        "last_bar": str(last_bar), "entry_time": now.isoformat(timespec="seconds"),
                                        "order_id": fill.order_id}
        self._log_trade(now, "ENTRY", sym, side, fill.qty, fill.price, reason, order_id=fill.order_id)
        self.say(f"{side} {fill.qty} {sym} @ {fill.price:.2f}  stop {float(stop):.2f}")
        self.save()

    def _apply_exit(self, now, sym, fill: Fill, reason):
        pos = self.state["positions"][sym]
        d = 1 if pos["side"] == "BUY" else -1
        qty = min(fill.qty, pos["qty"])
        pnl = (fill.price - pos["entry"]) * d * qty
        self.state["realized"] += pnl
        self._log_trade(now, "EXIT", sym, "SELL" if d == 1 else "BUY", qty, fill.price, reason, pnl, fill.order_id)
        self.say(f"EXIT {sym} {qty} @ {fill.price:.2f}  pnl {pnl:+.2f}  ({reason})")
        if qty >= pos["qty"]:
            del self.state["positions"][sym]
        else:
            pos["qty"] -= qty
            self.say(f"  partial exit: {pos['qty']} {sym} still open - will retry")
        self.save()

    def reconcile(self, now):
        """After a crash/restart: find orders that were sent but not recorded."""
        for cid, p in list(self.state["pending"].items()):
            try:
                fill = self.broker.lookup(cid)
            except requests.RequestException as e:
                self.say(f"cannot reconcile order {cid} ({p['symbol']}): {e}")
                self.state["halted"], self.state["halt_reason"] = True, "unreconciled order"
                continue
            if fill is None:
                self.say(f"pending order {cid} ({p['side']} {p['qty']} {p['symbol']}) not found at broker - treating as not placed")
            elif p["kind"] == "entry":
                self.say(f"recovered filled entry {p['symbol']}")
                self._apply_entry(now, p["symbol"], p["side"], fill, p["stop"], p["last_bar"], "recovered after restart")
            elif p["symbol"] in self.state["positions"]:
                self.say(f"recovered filled exit {p['symbol']}")
                self._apply_exit(now, p["symbol"], fill, "recovered after restart")
            self.state["pending"].pop(cid, None)
        self.save()

    # ---- exits
    def _exit(self, now, sym, d: pd.DataFrame, reason):
        pos = self.state["positions"][sym]
        side = "SELL" if pos["side"] == "BUY" else "BUY"
        fill = self._send(sym, side, pos["qty"], float(d["Close"].iloc[-1]), {"kind": "exit", "reason": reason})
        if fill:
            self._apply_exit(now, sym, fill, reason)
        return fill is not None

    def _manage(self, now, sym, d: pd.DataFrame, force: str | None):
        """Replay candles since the last one we processed: ratchet the trailing stop, detect a stop hit."""
        pos = self.state["positions"][sym]
        lows = d["Low"].rolling(ttl.STOP_LEN).min().to_numpy()
        highs = d["High"].rolling(ttl.STOP_LEN).max().to_numpy()
        closes = d["Close"].to_numpy()
        hit = None
        for i in np.flatnonzero(d.index > pd.Timestamp(pos["last_bar"])):
            if pos["side"] == "BUY":
                if not np.isnan(lows[i]):
                    pos["stop"] = max(pos["stop"], float(lows[i]))
                if closes[i] < pos["stop"]:
                    hit = d.index[i]
                    break
            else:
                if not np.isnan(highs[i]):
                    pos["stop"] = min(pos["stop"], float(highs[i]))
                if closes[i] > pos["stop"]:
                    hit = d.index[i]
                    break
            pos["last_bar"] = str(d.index[i])
        if hit is not None:
            self._exit(now, sym, d, f"TTL stop hit ({hit:%H:%M}, stop {pos['stop']:.2f})")
        elif force:
            self._exit(now, sym, d, force)
        else:
            self.save()

    def total_pnl(self, frames) -> float:
        u = 0.0
        for sym, pos in self.state["positions"].items():
            d = frames.get(sym)
            if d is not None:
                u += (float(d["Close"].iloc[-1]) - pos["entry"]) * (1 if pos["side"] == "BUY" else -1) * pos["qty"]
        return self.state["realized"] + u

    # ---- one decision cycle
    def cycle(self, data: dict, now: datetime, square_off: bool = False, ignore_age: bool = False) -> dict:
        cfg, st = self.cfg, self.state
        bar_len = timedelta(minutes=TF_MIN[cfg.timeframe])
        frames = {}
        for sym, df in data.items():
            d = closed(df.dropna(subset=["Close"]), now, bar_len)
            if d is not None and len(d) >= MIN_BARS:
                frames[sym] = d
        n_trades_before = st["n_trades"]
        entered = []

        # 1) manage open positions (stops / square-off)
        force = "square-off time" if square_off else None
        for sym in list(st["positions"]):
            d = frames.get(sym)
            if d is None:
                self.say(f"WARNING {sym}: no data - cannot manage its stop this cycle")
                continue
            self._manage(now, sym, d, force)

        # 2) daily loss limit
        pnl = self.total_pnl(frames)
        if not st["halted"] and pnl <= -cfg.max_daily_loss:
            st["halted"], st["halt_reason"] = True, f"daily loss limit reached ({pnl:.0f})"
            self.say(f"!!! {st['halt_reason']} - flattening and halting for the day")
        if st["halted"]:
            for sym in list(st["positions"]):
                if sym in frames:
                    self._exit(now, sym, frames[sym], st["halt_reason"])

        # 3) new entries
        t = now.time()
        window = parse_hm(cfg.first_entry_time) <= t < parse_hm(cfg.no_new_entries_after)
        if window and not square_off and not st["halted"]:
            cands = []
            for sym, d in frames.items():
                if sym in st["positions"]:
                    continue
                last_bar = d.index[-1]
                key = f"{sym}|{last_bar}"
                if key in st["seen"]:
                    continue
                if not ignore_age and (now - bar_end(last_bar, bar_len)).total_seconds() > cfg.max_signal_age_sec:
                    continue
                tt = ttl.compute(d)
                last = scanner.bollinger(d["Close"]).iloc[-1]
                side = None
                if cfg.allow_long and tt["buy_entry"].iloc[-1] and last["close"] > last["upper"]:
                    side, stop = "BUY", float(tt["buy_sl"].iloc[-1])
                elif cfg.allow_short and tt["sell_entry"].iloc[-1] and last["close"] < last["lower"]:
                    side, stop = "SELL", float(tt["sell_sl"].iloc[-1])
                if side:
                    st["seen"].append(key)
                    strength = abs(float(tt["rsi"].iloc[-1]) - float(tt["rsi_ema"].iloc[-1]))
                    cands.append((strength, sym, side, stop, float(last["close"]), last_bar))
            for strength, sym, side, stop, px, last_bar in sorted(cands, reverse=True):
                if len(st["positions"]) >= cfg.max_open_positions:
                    self.say(f"skip {side} {sym}: max open positions ({cfg.max_open_positions}) reached")
                    continue
                risk = abs(px - stop)
                if not np.isfinite(risk) or risk <= 0:
                    self.say(f"skip {side} {sym}: no valid stop distance")
                    continue
                if risk / px * 100 > cfg.max_stop_pct:
                    self.say(f"skip {side} {sym}: stop {risk / px * 100:.1f}% away (max {cfg.max_stop_pct}%)")
                    continue
                qty = int(min(math.floor(cfg.risk_per_trade / risk), math.floor(cfg.capital_per_trade / px)))
                if qty < 1:
                    self.say(f"skip {side} {sym}: quantity rounds to 0 (price {px:.2f}, stop distance {risk:.2f})")
                    continue
                fill = self._send(sym, side, qty, px, {"kind": "entry", "stop": stop, "last_bar": str(last_bar)})
                if fill:
                    self._apply_entry(now, sym, side, fill, stop, last_bar)
                    entered.append(sym)
                if st["halted"]:
                    break
        st["seen"] = st["seen"][-5000:]
        self.save()
        return {"entered": entered, "open": list(st["positions"]), "pnl": self.total_pnl(frames),
                "trades": st["n_trades"] - n_trades_before}

    def summary(self) -> str:
        st = self.state
        return (f"Day summary ({self.broker.name}): realized P&L {st['realized']:+.2f}, "
                f"{st['n_trades']} order(s), still open: {list(st['positions']) or 'none'}"
                + (f", HALTED: {st['halt_reason']}" if st["halted"] else ""))


# ------------------------------------------------------------------ runner
def resolve_symbols(name: str) -> list[str]:
    key = universe.find(name)
    if key:
        names, note = universe.load(key)
        print(f"Universe: {key} - {len(names)} stocks ({note})")
        return names
    p = Path(name)
    if p.exists():
        return universe.parse_symbols(p.read_text())
    sys.exit(f"Unknown universe '{name}'. Use one of {list(universe.UNIVERSES)} or a path to a symbols file.")


def main():
    ap = argparse.ArgumentParser(description="BB + TTL trading bot (paper by default)")
    ap.add_argument("--live", action="store_true", help="send REAL orders to Dhan (default: paper trading)")
    ap.add_argument("--config", type=Path, default=HERE / "bot_config.json")
    ap.add_argument("--once", action="store_true", help="run one cycle now and exit")
    ap.add_argument("--ignore-age", action="store_true", help="paper only: also act on the latest closed candle even if it closed a while ago")
    args = ap.parse_args()
    if args.ignore_age and args.live:
        sys.exit("--ignore-age is not allowed with --live")

    cfg = Config.load(args.config)
    cid, token = dhan_feed.creds_from_env()
    if (cfg.source == "dhan" or args.live) and not (cid and token):
        sys.exit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in the environment first.")
    names = resolve_symbols(cfg.universe)
    symbols = [n + ".NS" for n in names]
    mode = "live" if args.live else "paper"

    if args.live:
        print("\n*** LIVE TRADING: this will place REAL orders with real money. ***")
        print(f"    {cfg.timeframe} candles, {len(names)} stocks, risk/trade Rs {cfg.risk_per_trade:g}, "
              f"max {cfg.max_open_positions} positions, daily loss limit Rs {cfg.max_daily_loss:g}")
        if os.environ.get("BOT_CONFIRM_LIVE") != "I_UNDERSTAND":
            if not sys.stdin.isatty() or input("Type LIVE to continue: ").strip() != "LIVE":
                sys.exit("Not confirmed - exiting. (Unattended runs: set BOT_CONFIRM_LIVE=I_UNDERSTAND)")
        broker = DhanBroker(cid, token, names, cfg.product_type)
        broker.preflight()
    else:
        print("PAPER mode - no orders will be sent.")
        broker = PaperBroker(cfg.slippage_pct)

    bot = Bot(cfg, broker, HERE / f"bot_state_{mode}.json", HERE / f"trades_{mode}.csv")
    bot.reconcile(datetime.now(IST))
    stop_file = HERE / "STOP"
    sq_time = parse_hm(cfg.square_off_time)

    def fetch():
        return scanner.download(symbols, cfg.timeframe, cfg.source, cid, token)

    if args.once:
        now = datetime.now(IST)
        res = bot.cycle(fetch(), datetime.now(IST), square_off=now.time() >= sq_time, ignore_age=args.ignore_age)
        print(res)
        print(bot.summary())
        return

    looked_after_open_positions = False
    while True:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            sys.exit("Weekend - market closed.")
        upcoming = [t + timedelta(seconds=cfg.data_delay_sec) for t in check_times(now.date(), cfg)]
        upcoming = [t for t in upcoming if t > now]
        if bot.state["positions"] and not looked_after_open_positions:
            looked_after_open_positions = True  # restart with open positions: look after them straight away
            bot.cycle(fetch(), datetime.now(IST), square_off=datetime.now(IST).time() >= sq_time)
            continue
        if not upcoming:
            print(bot.summary())
            return
        target = upcoming[0]
        while datetime.now(IST) < target:
            if stop_file.exists():
                break
            time.sleep(min(5, max(0.1, (target - datetime.now(IST)).total_seconds())))
        stop_now = stop_file.exists()
        try:
            data = fetch()
        except Exception as e:
            bot.say(f"data fetch failed: {e}")
            continue
        now = datetime.now(IST)
        sq = now.time() >= sq_time or stop_now
        res = bot.cycle(data, now, square_off=sq)
        bot.say(f"cycle done: open {res['open']}  P&L {res['pnl']:+.0f}")
        if stop_now:
            bot.say("STOP file found - positions flattened, exiting.")
            print(bot.summary())
            return
        if sq and not bot.state["positions"]:
            print(bot.summary())
            return


if __name__ == "__main__":
    main()
