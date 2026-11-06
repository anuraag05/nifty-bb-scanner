"""
Offline tests for bot.py (no network, no real orders). Run:  python tests/test_bot_sim.py
"""
import sys
import tempfile
import traceback
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot as B  # noqa: E402
import scanner  # noqa: E402
import ttl  # noqa: E402

IST = B.IST
BAR = timedelta(minutes=15)
TMP = Path(tempfile.mkdtemp())
_n = [0]


def new_bot(broker=None, **cfg_kw):
    _n[0] += 1
    cfg = B.Config(**{"slippage_pct": 0.0, "max_stop_pct": 10.0, **cfg_kw})
    msgs = []
    bot = B.Bot(cfg, broker or B.PaperBroker(0.0), TMP / f"s{_n[0]}.json", TMP / f"t{_n[0]}.csv",
                log=msgs.append, today=datetime(2026, 11, 4).date())
    bot.msgs = msgs
    return bot


def at(h, m, s=15, day=4):
    return datetime(2026, 11, day, h, m, s, tzinfo=IST)


# ------------------------------------------------------------------ synthetic market
def make_df(seed, days=6):
    r = np.random.default_rng(seed)
    idx = []
    for d in pd.bdate_range("2026-10-26", periods=days):
        idx += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=25, freq="15min", tz=IST))
    idx = pd.DatetimeIndex(idx)
    c = 100 * np.exp(np.cumsum(r.normal(0, .004, len(idx)) + r.choice([-1, 1]) * .0008))
    o = np.r_[c[0], c[:-1]]
    sp = np.abs(r.normal(0, .002, len(idx))) * c
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) + sp, "Low": np.minimum(o, c) - sp, "Close": c, "Volume": 1}, index=idx)


def find_signal(side):
    """First (df, bar index) where a combo signal fires between 09:30 and 14:45 with room afterwards."""
    for seed in range(600):
        df = make_df(seed)
        t = ttl.compute(df)
        bb = scanner.bollinger(df["Close"])
        mask = (t.buy_entry & (bb.close > bb.upper)) if side == "BUY" else (t.sell_entry & (bb.close < bb.lower))
        for e in np.flatnonzero(mask.to_numpy()):
            end = B.bar_end(df.index[e], BAR)
            if e >= 80 and dtime(9, 30) <= end.time() < dtime(14, 45) and e + 12 < len(df):
                return df, int(e), t
    raise AssertionError("no signal found in synthetic data")


# ------------------------------------------------------------------ tests
def test_replay_matches_independent_ttl(side):
    df, e, t = find_signal(side)
    bot = new_bot(risk_per_trade=1000, capital_per_trade=50000)
    sym = "TEST.NS"
    exit_col = "buy_exit" if side == "BUY" else "sell_exit"
    # independent expectation from the Pine-port state machine
    day_end_idx = max(i for i in range(e, len(df)) if df.index[i].date() == df.index[e].date())
    x = next((i for i in range(e + 1, day_end_idx + 1) if t[exit_col].iloc[i]), None)
    sq_idx = next(i for i in range(e, len(df)) if B.bar_end(df.index[i], BAR).time() >= dtime(15, 15))  # last candle before square-off
    expected_exit_bar = min(x, sq_idx) if x is not None else sq_idx
    entered_at = exited_at = None
    for j in range(e + 1, day_end_idx + 2):               # j candles are closed at this cycle
        now = B.bar_end(df.index[j - 1], BAR) + timedelta(seconds=15)
        data = {sym: df.iloc[:min(j + 1, len(df))]}       # includes the still-forming candle j
        res = bot.cycle(data, now, square_off=now.time() >= dtime(15, 15))
        if j == e + 1:
            assert res["entered"] == [sym], f"expected entry at bar {e}, got {res}"
            pos = bot.state["positions"][sym]
            assert pos["side"] == side
            entered_at = j - 1
        if entered_at is not None and sym not in bot.state["positions"] and exited_at is None:
            exited_at = j - 1
            break
    assert exited_at == expected_exit_bar, f"bot exited at bar {exited_at}, independent TTL says {expected_exit_bar}"
    csv = pd.read_csv(bot.trades_path)
    assert list(csv.action) == ["ENTRY", "EXIT"], csv
    d = 1 if side == "BUY" else -1
    assert abs(csv.pnl.iloc[1] - (csv.price.iloc[1] - csv.price.iloc[0]) * d * csv.qty.iloc[0]) < 0.02
    why = "TTL stop" if x is not None and x <= sq_idx else "square-off"
    return f"{side}: entry bar {e} -> exit bar {exited_at} via {why}; qty {int(csv.qty.iloc[0])}; pnl {csv.pnl.iloc[1]:+.2f}"


class Patch:
    """Force signals: every symbol gets a TTL entry on its last bar and a close beyond the band."""
    def __init__(self, signals):  # {sym: (side, stop, strength)}
        self.signals = signals

    def __enter__(self):
        self.old = (ttl.compute, scanner.bollinger)
        sig = self.signals

        def fake_compute(d):
            n = len(d)
            sym = d.attrs["sym"]
            side, stop, strength = sig[sym]
            z = np.zeros(n, bool)
            be, se = z.copy(), z.copy()
            (be if side == "BUY" else se)[-1] = True
            nan = np.full(n, np.nan)
            bsl, ssl = nan.copy(), nan.copy()
            (bsl if side == "BUY" else ssl)[-1] = stop
            return pd.DataFrame({"buy_entry": be, "sell_entry": se, "buy_sl": bsl, "sell_sl": ssl,
                                 "rsi": np.full(n, 50 + strength), "rsi_ema": np.full(n, 50.0)}, index=d.index)

        def fake_bb(close):
            return pd.DataFrame({"close": close, "mid": close, "upper": close - 1.0, "lower": close + 1.0}, index=close.index)

        ttl.compute, scanner.bollinger = fake_compute, fake_bb
        return self

    def __exit__(self, *a):
        ttl.compute, scanner.bollinger = self.old


def flat_frame(sym, price=100.0, last_start="10:15", n=80):
    idx = pd.date_range(end=pd.Timestamp(f"2026-11-04 {last_start}", tz=IST), periods=n, freq="15min")
    df = pd.DataFrame({"Open": price, "High": price + .2, "Low": price - .2, "Close": price, "Volume": 1}, index=idx)
    df.attrs["sym"] = sym
    return df


def test_sizing_and_filters():
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(); b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        assert b.state["positions"]["A.NS"]["qty"] == 200, b.state["positions"]       # min(500/1 risk, 20000/100 capital)
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(capital_per_trade=1000); b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        assert b.state["positions"]["A.NS"]["qty"] == 10
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(risk_per_trade=250); b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        assert b.state["positions"]["A.NS"]["qty"] == 200   # risk cap 250 vs capital cap 200
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(risk_per_trade=100); b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        assert b.state["positions"]["A.NS"]["qty"] == 100   # risk-limited
    with Patch({"A.NS": ("BUY", 90.0, 5)}):
        b = new_bot(max_stop_pct=3.0); r = b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        assert r["entered"] == [] and any("stop" in m and "away" in m for m in b.msgs)
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(); r = b.cycle({"A.NS": flat_frame("A.NS", price=30000.0)}, at(10, 30))
        assert r["entered"] == [] and any("rounds to 0" in m or "away" in m for m in b.msgs)
    with Patch({"A.NS": ("SELL", 101.0, 5)}):
        b = new_bot(allow_short=False); assert b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))["entered"] == []
        b = new_bot(); b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        assert b.state["positions"]["A.NS"]["side"] == "SELL"
    return "quantity = min(risk cap, capital cap); wide stops, zero-qty and disabled-shorts are skipped"


def test_limits_and_timing():
    sig = {f"S{i}.NS": ("BUY", 99.0, i) for i in range(4)}
    with Patch(sig):
        data = {s: flat_frame(s) for s in sig}
        b = new_bot(max_open_positions=2); r = b.cycle(data, at(10, 30))
        assert sorted(r["entered"]) == ["S2.NS", "S3.NS"], r      # strongest momentum first
        assert b.cycle(data, at(10, 30))["entered"] == []         # same candle again: no duplicates
        b2 = new_bot(); assert b2.cycle(data, at(10, 30, 0) + timedelta(seconds=600))["entered"] == []   # stale signal
        assert len(b2.cycle(data, at(10, 30) + timedelta(seconds=600), ignore_age=True)["entered"]) == 4
        d2 = {"S0.NS": flat_frame("S0.NS", last_start="14:45")}
        assert new_bot().cycle(d2, at(15, 0))["entered"] == []       # after 14:45 no new entries
        d3 = {"S0.NS": flat_frame("S0.NS", last_start="09:00")}
        assert new_bot().cycle(d3, at(9, 15))["entered"] == []       # before 09:30
        f = flat_frame("S0.NS")                                      # forming candle is ignored
        forming = f.copy(); forming.loc[f.index[-1] + BAR] = f.iloc[-1]; forming.attrs["sym"] = "S0.NS"
        b3 = new_bot(); r3 = b3.cycle({"S0.NS": forming}, at(10, 30))
        assert r3["entered"] == ["S0.NS"]                            # uses candle ending 10:30, not the forming 10:45 one
    return "max positions (ranked), dedupe, stale-signal, entry window and forming-candle handling OK"


def test_daily_loss_and_squareoff():
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(max_daily_loss=400)
        b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        qty = b.state["positions"]["A.NS"]["qty"]
        crash = flat_frame("A.NS", price=100.0, last_start="10:30")
        crash.iloc[-1, crash.columns.get_loc("Close")] = 100.0 - 400.0 / qty - 0.5    # unrealized loss beyond limit
        crash.iloc[-1, crash.columns.get_loc("Low")] = crash.iloc[-1]["Close"] - .1
        b.cycle({"A.NS": crash}, at(10, 45))
        assert b.state["halted"] and "A.NS" not in b.state["positions"], b.state
        assert b.cycle({"A.NS": flat_frame("A.NS", last_start="10:45")}, at(11, 0))["entered"] == []   # halted: no new entries
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(); b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        r = b.cycle({"A.NS": flat_frame("A.NS", last_start="15:00")}, at(15, 15), square_off=True)
        assert r["open"] == [] and "square-off" in pd.read_csv(b.trades_path).reason.iloc[-1]
    return "daily loss limit flattens and halts; square-off closes everything"


def test_state_survives_restart():
    with Patch({"A.NS": ("BUY", 99.0, 5)}):
        b = new_bot(); b.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))
        b2 = B.Bot(b.cfg, b.broker, b.state_path, b.trades_path, log=lambda m: None, today=datetime(2026, 11, 4).date())
        assert "A.NS" in b2.state["positions"]
        assert b2.cycle({"A.NS": flat_frame("A.NS")}, at(10, 30))["entered"] == []       # signal not re-traded after restart
        b3 = B.Bot(b.cfg, b.broker, b.state_path, b.trades_path, log=lambda m: None, today=datetime(2026, 11, 5).date())
        assert b3.state["positions"] == {}                                                # new day -> fresh state
    return "positions persist across a restart, no duplicate trades, new day resets"


# ------------------------------------------------------------------ Dhan order handling (mocked HTTP)
class Resp:
    def __init__(self, code=200, js=None, text=""):
        self.status_code, self._js, self.text = code, js, text or str(js)
        self.ok = code < 400

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise B.requests.HTTPError(str(self.status_code))


class FakeHTTP:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __call__(self, method, url, **kw):
        path = url.split("/v2")[1]
        self.calls.append((method, path, kw.get("json")))
        return self.handler(method, path, kw.get("json"))


def dhan_broker(handler, fill_timeout=0.4):
    fake = FakeHTTP(handler)
    old = (B.requests.request, B.dhan_feed.load_security_ids)
    B.requests.request = fake
    B.dhan_feed.load_security_ids = lambda syms: {"TEST": "1333"}
    br = B.DhanBroker("1104000000", "TOKEN", ["TEST"], fill_timeout=fill_timeout)
    br._restore = lambda: (setattr(B.requests, "request", old[0]), setattr(B.dhan_feed, "load_security_ids", old[1]))
    return br, fake


def test_dhan_orders():
    # normal fill
    def ok(m, p, body):
        if m == "POST":
            return Resp(200, {"orderId": "77", "orderStatus": "TRANSIT"})
        return Resp(200, {"orderId": "77", "orderStatus": "TRADED", "filledQty": 40, "averageTradedPrice": 101.5})
    br, fake = dhan_broker(ok)
    try:
        f = br.place("TEST", "BUY", 40, 101.0, "cid1")
        body = fake.calls[0][2]
        assert (f.qty, f.price, f.order_id) == (40, 101.5, "77")
        assert body["transactionType"] == "BUY" and body["orderType"] == "MARKET" and body["productType"] == "INTRADAY"
        assert body["exchangeSegment"] == "NSE_EQ" and body["securityId"] == "1333" and body["quantity"] == 40
        assert body["correlationId"] == "cid1" and body["dhanClientId"] == "1104000000"
    finally:
        br._restore()

    # rejected (e.g. IP not whitelisted): no position, pending cleared
    br, _ = dhan_broker(lambda m, p, b: Resp(403, {"errorMessage": "Invalid IP"}, "Invalid IP address"))
    try:
        bot = new_bot(broker=br)
        with Patch({"TEST.NS": ("BUY", 99.0, 5)}):
            bot.cycle({"TEST.NS": flat_frame("TEST.NS")}, at(10, 30))
        assert bot.state["positions"] == {} and bot.state["pending"] == {} and not bot.state["halted"]
        assert any("static IP" in m for m in bot.msgs), bot.msgs
    finally:
        br._restore()

    # not filled in time -> cancelled, partial fill recorded
    state = {"cancelled": False}

    def slow(m, p, body):
        if m == "POST":
            return Resp(200, {"orderId": "9", "orderStatus": "TRANSIT"})
        if m == "DELETE":
            state["cancelled"] = True
            return Resp(202, {})
        if state["cancelled"]:
            return Resp(200, {"orderId": "9", "orderStatus": "CANCELLED", "filledQty": 3, "averageTradedPrice": 50.0})
        return Resp(200, {"orderId": "9", "orderStatus": "PENDING", "filledQty": 0})
    br, _ = dhan_broker(slow, fill_timeout=0.3)
    try:
        f = br.place("TEST", "BUY", 10, 50.0, "cid2")
        assert state["cancelled"] and (f.qty, f.price) == (3, 50.0)
    finally:
        br._restore()

    # unknown outcome (HTTP 500): bot halts, keeps the pending order, restart reconciles it
    br, _ = dhan_broker(lambda m, p, b: Resp(500, {}, "gateway error"))
    try:
        bot = new_bot(broker=br)
        with Patch({"TEST.NS": ("BUY", 99.0, 5)}):
            bot.cycle({"TEST.NS": flat_frame("TEST.NS")}, at(10, 30))
        assert bot.state["halted"] and len(bot.state["pending"]) == 1 and bot.state["positions"] == {}
        cid = next(iter(bot.state["pending"]))
    finally:
        br._restore()

    def found(m, p, body):
        assert p == f"/orders/external/{cid}", p
        return Resp(200, {"orderId": "55", "orderStatus": "TRADED", "filledQty": 200, "averageTradedPrice": 100.1})
    br2, _ = dhan_broker(found)
    try:
        bot2 = B.Bot(bot.cfg, br2, bot.state_path, bot.trades_path, log=lambda m: None, today=datetime(2026, 11, 4).date())
        bot2.reconcile(at(10, 40))
        assert bot2.state["positions"]["TEST.NS"]["qty"] == 200 and bot2.state["pending"] == {}
    finally:
        br2._restore()
    return "market INTRADAY order body, rejection/IP hint, cancel+partial fill, unknown-outcome halt, crash reconcile"


def test_schedule_and_universe():
    cfg = B.Config(timeframe="15m")
    ts = B.check_times(datetime(2026, 11, 4).date(), cfg)
    assert ts[0].strftime("%H:%M") == "09:30" and ts[-1].strftime("%H:%M") == "15:30" and len(ts) == 25
    assert datetime(2026, 11, 4, 15, 15, tzinfo=IST) in ts
    h = B.check_times(datetime(2026, 11, 4).date(), B.Config(timeframe="1h"))
    assert [t.strftime("%H:%M") for t in h] == ["10:15", "11:15", "12:15", "13:15", "14:15", "15:15", "15:30"]
    last_hour_bar = pd.Timestamp("2026-11-04 15:15", tz=IST)
    assert B.bar_end(last_hour_bar, timedelta(hours=1)).strftime("%H:%M") == "15:30"   # last hourly candle ends at the close
    assert B.bar_end(pd.Timestamp("2026-11-04 14:15", tz=IST), timedelta(hours=1)).strftime("%H:%M") == "15:15"
    return "check times: 15m -> 09:30..15:30 (25), 1h -> 10:15..15:15 + 15:30"


if __name__ == "__main__":
    tests = [("replay BUY vs independent TTL", lambda: test_replay_matches_independent_ttl("BUY")),
             ("replay SELL vs independent TTL", lambda: test_replay_matches_independent_ttl("SELL")),
             ("sizing & filters", test_sizing_and_filters), ("limits & timing", test_limits_and_timing),
             ("daily loss & square-off", test_daily_loss_and_squareoff), ("restart & state", test_state_survives_restart),
             ("Dhan order handling", test_dhan_orders), ("schedule", test_schedule_and_universe)]
    failed = 0
    for name, fn in tests:
        try:
            print(f"PASS  {name}: {fn()}")
        except Exception:
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
