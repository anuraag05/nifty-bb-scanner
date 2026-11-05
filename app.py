"""
Nifty Bollinger Band Scanner - web app (Streamlit)

Run locally:   streamlit run app.py
Host free:     https://share.streamlit.io  (point it at this repo, main file: app.py)
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import scanner
import universe

st.set_page_config(page_title="Nifty BB Scanner", page_icon="📈", layout="wide")

# cache lifetime per timeframe (seconds) so repeated clicks don't hammer Yahoo
CACHE_TTL = {"1m": 60, "5m": 120, "15m": 300, "1h": 600, "1d": 1800}


@st.cache_resource
def _store() -> dict:
    return {}


@st.cache_data(show_spinner=False, ttl=6 * 3600)
def load_universe(name: str):
    return universe.load(name)


def get_data(symbols: list[str], tf: str, source: str, client_id: str, token: str, progress=None):
    """Fetch (or reuse recently fetched) candles. Cache lifetime depends on the timeframe."""
    import hashlib
    import time
    bucket = int(time.time() // CACHE_TTL[tf])
    key = (hashlib.md5(",".join(symbols).encode()).hexdigest(), tf, bucket, source,
           hashlib.md5(token.encode()).hexdigest() if token else "")
    store = _store()
    if key in store:
        return store[key]
    data = scanner.download(symbols, tf, source, client_id, token, progress)
    for old in [k for k in store if k[2] != bucket or k[1] != tf][:]:
        store.pop(old, None)  # drop stale entries to keep memory small
    store[key] = data
    return data


def secret(name: str) -> str:
    """Read a credential from Streamlit secrets or environment (never from the repo)."""
    import os
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name, "")


IST = ZoneInfo("Asia/Kolkata")


def market_open() -> bool:
    """NSE cash market hours: Mon-Fri 09:15-15:30 IST (holidays are not checked)."""
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)


@st.fragment(run_every="10s")
def refresh_timer(enabled: bool, minutes: int, only_market_hours: bool):
    """Counts down in the sidebar and triggers a full re-scan when the interval has passed."""
    if not enabled or "last_scan" not in st.session_state:
        return
    if st.session_state.get("auto_due"):
        return  # a refresh is already queued; the main script will run it (avoids a rerun loop)
    if only_market_hours and not market_open():
        st.caption("⏸ Market closed - auto-refresh paused")
        return
    left = st.session_state["last_scan"] + minutes * 60 - time.time()
    if left <= 0:
        st.session_state["auto_due"] = True
        st.rerun()  # reruns the whole app, which performs the scan
    st.caption(f"⏱ Next auto-refresh in {int(left // 60)}:{int(left % 60):02d}")


# ------------------------------------------------------------------ sidebar
st.title("📈 Nifty Bollinger Band Scanner")
st.caption(f"Bollinger Bands: {scanner.EMA_LEN} EMA midline, ±{scanner.STD_MULT:g} standard deviation · data: Dhan or Yahoo Finance")

with st.sidebar:
    st.header("Settings")
    source = st.selectbox("Data source", ["dhan", "yahoo"], format_func=str.capitalize)
    client_id = token = ""
    if source == "dhan":
        client_id = st.text_input("Dhan client ID", value=secret("DHAN_CLIENT_ID"))
        token = st.text_input("Dhan access token", value=secret("DHAN_ACCESS_TOKEN"), type="password",
                              help="Tokens expire (24 h for console-generated ones). Paste a fresh one when needed. "
                                   "It is only used for this session and is not saved.")
    tf = st.selectbox("Timeframe", list(scanner.TIMEFRAMES), index=2)
    signals = st.multiselect(
        "Signals to show",
        scanner.SIGNALS,
        default=["cross_above_upper", "cross_below_lower"],
        help="Leave empty to show every stock that has any signal.",
    )
    show_all = st.checkbox("Show all stocks (ignore signal filter)", value=False)
    include_forming = st.checkbox("Include forming (unclosed) candle", value=False)
    uni_names = list(universe.UNIVERSES) + ["Upload CSV", "Custom list"]
    uni = st.selectbox("Stocks to scan", uni_names, index=0,
                       help="Nifty 200 / Nifty 500 = NSE's official top 200 / top 500 listed companies by market cap.")
    custom_syms: list[str] = []
    if uni == "Upload CSV":
        up = st.file_uploader("CSV with a 'Symbol' column (or one symbol per line)", type=["csv", "txt"])
        if up is not None:
            custom_syms = universe.parse_symbols(up.getvalue().decode("utf-8", errors="ignore"))
    elif uni == "Custom list":
        txt = st.text_area("Symbols (one per line)", value="RELIANCE\nTCS\nINFY", height=160)
        custom_syms = universe.parse_symbols(txt)
    clicked = st.button("🔍 Scan", type="primary", width="stretch")
    st.divider()
    auto = st.checkbox("Auto-refresh", value=True, help="Re-scans automatically using the settings above.")
    every = st.selectbox("Refresh every", [1, 2, 5, 10, 15], index=2, format_func=lambda m: f"{m} min",
                         disabled=not auto)
    mkt_only = st.checkbox("Only during market hours (9:15-15:30 IST, Mon-Fri)", value=True, disabled=not auto)
    refresh_timer(auto, every, mkt_only)

# an auto-refresh tick only counts if a scan has already been done
run = clicked or st.session_state.pop("auto_due", False)

if "result" not in st.session_state and not run:
    st.info("Choose a timeframe in the sidebar and click **Scan**.")
    st.stop()

# ------------------------------------------------------------------ scan
if run:
    if uni in universe.UNIVERSES:
        try:
            names, note = load_universe(uni)
        except Exception as e:
            st.error(str(e))
            st.stop()
        st.session_state["universe_note"] = f"{uni}: {len(names)} stocks ({note})"
    else:
        names = custom_syms
        st.session_state["universe_note"] = f"{uni}: {len(names)} stocks"
    symbols = [n + ".NS" for n in names]
    if not symbols:
        st.error("No symbols to scan. Upload a CSV or enter some symbols.")
        st.stop()
    if source == "dhan" and not (client_id and token):
        st.error("Enter your Dhan client ID and access token in the sidebar (or choose Yahoo).")
        st.stop()
    bar = st.progress(0.0, text=f"Fetching {len(symbols)} symbols on {tf} from {source.capitalize()}...")

    def on_progress(done: int, total: int):
        bar.progress(min(done / total, 1.0), text=f"Fetching {source.capitalize()} data: {done}/{total}")

    failed = False
    try:
        data = get_data(symbols, tf, source, client_id, token, on_progress)
    except Exception as e:  # bad/expired token, network problems, etc.
        bar.empty()
        if "result" in st.session_state:  # keep showing the last good scan
            st.warning(f"Refresh failed ({e}). Showing the previous scan; will retry.")
            failed = True
        else:
            st.error(f"Data fetch failed: {e}")
            st.stop()
    bar.empty()
    st.session_state["last_scan"] = time.time()  # also on failure, so retries are spaced out
    if not failed:
        res = scanner.scan(symbols, tf, include_forming, data=data)
        st.session_state["result"] = (res, data, tf, signals, show_all, symbols)
        st.session_state["scan_time"] = datetime.now(IST).strftime("%d %b %H:%M:%S IST")

res, data, tf_used, signals_used, show_all_used, symbols_used = st.session_state["result"]

if res.empty:
    st.error("No data returned. The market may be closed for this timeframe, Yahoo may be rate limiting, "
             "or the symbols are invalid. Try again in a minute or use the 1d timeframe.")
    st.stop()

st.caption(f"{st.session_state.get('universe_note', '')} · last scan {st.session_state.get('scan_time', '-')}")
missing = [s.replace(".NS", "") for s in symbols_used if s not in data]
if missing:
    with st.expander(f"⚠️ No data for {len(missing)} symbols"):
        st.write(", ".join(missing))

view = res
if not show_all_used:
    if signals_used:
        mask = view["signals"].apply(lambda s: any(x in s.split(",") for x in signals_used))
    else:
        mask = view["signals"] != ""
    view = view[mask]
view = view.sort_values("symbol").reset_index(drop=True)

c1, c2, c3 = st.columns(3)
c1.metric("Timeframe", tf_used)
c2.metric("Stocks scanned", len(res))
c3.metric("Matches", len(view))

if view.empty:
    st.success("No stocks match the selected signals right now.")
else:
    def colour(row):
        sig = row["signals"]
        if "cross_above_upper" in sig or "above_upper" in sig:
            return ["background-color: rgba(46,160,67,0.18)"] * len(row)
        if "cross_below_lower" in sig or "below_lower" in sig:
            return ["background-color: rgba(218,54,51,0.18)"] * len(row)
        return [""] * len(row)

    st.dataframe(view.style.apply(colour, axis=1), width="stretch", hide_index=True)
    st.download_button("⬇️ Download CSV", view.to_csv(index=False).encode(),
                       file_name=f"nifty_bb_{tf_used}.csv", mime="text/csv")

# ------------------------------------------------------------------ chart
st.subheader("Chart")
choices = list(view["symbol"]) if not view.empty else list(res["symbol"])
pick = st.selectbox("Stock", choices)
df = data.get(pick + ".NS")
if df is not None and "Close" in df.columns:
    bar_len = scanner.TIMEFRAMES[tf_used][2]
    if not include_forming:
        df = scanner.drop_forming_bar(df, bar_len, tf_used)
    df = df.dropna(subset=["Close"])
    bb = scanner.bollinger(df["Close"])
    tail = df.tail(120)
    bbt = bb.loc[tail.index]
    x = [str(i) for i in tail.index]  # category axis: no gaps for market-closed hours
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=x, open=tail["Open"], high=tail["High"], low=tail["Low"],
                                 close=tail["Close"], name="Price"))
    fig.add_trace(go.Scatter(x=x, y=bbt["upper"], name="Upper (+1 SD)", line=dict(width=1.3, color="#2ea043")))
    fig.add_trace(go.Scatter(x=x, y=bbt["mid"], name="EMA 20", line=dict(width=1.3, color="#d29922")))
    fig.add_trace(go.Scatter(x=x, y=bbt["lower"], name="Lower (-1 SD)", line=dict(width=1.3, color="#da3633")))
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis=dict(type="category", nticks=10), legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")

st.caption("Data may be delayed or rate limited. Educational tool, not investment advice.")
