"""
Nifty Bollinger Band Scanner - web app (Streamlit)

Run locally:   streamlit run app.py
Host free:     https://share.streamlit.io  (point it at this repo, main file: app.py)
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import scanner

st.set_page_config(page_title="Nifty BB Scanner", page_icon="📈", layout="wide")

# cache lifetime per timeframe (seconds) so repeated clicks don't hammer Yahoo
CACHE_TTL = {"1m": 60, "5m": 120, "15m": 300, "1h": 600, "1d": 1800}


@st.cache_data(show_spinner=False, ttl=3600)
def fetch(symbols: tuple, tf: str, bucket: int, source: str, client_id: str, token: str):
    # `bucket` changes with time so the cache expires per-timeframe
    return scanner.download(list(symbols), tf, source, client_id, token)


def get_data(symbols: list[str], tf: str, source: str, client_id: str, token: str):
    import time
    bucket = int(time.time() // CACHE_TTL[tf])
    return fetch(tuple(symbols), tf, bucket, source, client_id, token)


def secret(name: str) -> str:
    """Read a credential from Streamlit secrets or environment (never from the repo)."""
    import os
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name, "")


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
    default_syms = "\n".join(scanner.load_symbols(scanner.DEFAULT_SYMBOL_FILE)).replace(".NS", "")
    sym_text = st.text_area("Symbols (one per line)", value=default_syms, height=220,
                            help="NSE symbols; .NS is added automatically.")
    run = st.button("🔍 Scan", type="primary", width="stretch")

if "result" not in st.session_state and not run:
    st.info("Choose a timeframe in the sidebar and click **Scan**.")
    st.stop()

# ------------------------------------------------------------------ scan
if run:
    symbols = []
    for line in sym_text.splitlines():
        s = line.strip().upper()
        if s:
            symbols.append(s if s.endswith(".NS") or s.startswith("^") else s + ".NS")
    if not symbols:
        st.error("Add at least one symbol.")
        st.stop()
    if source == "dhan" and not (client_id and token):
        st.error("Enter your Dhan client ID and access token in the sidebar (or choose Yahoo).")
        st.stop()
    try:
        with st.spinner(f"Fetching {len(symbols)} symbols on {tf} from {source.capitalize()}..."):
            data = get_data(symbols, tf, source, client_id, token)
    except Exception as e:  # bad/expired token, network problems, etc.
        st.error(f"Data fetch failed: {e}")
        st.stop()
    res = scanner.scan(symbols, tf, include_forming, data=data)
    st.session_state["result"] = (res, data, tf, signals, show_all, symbols)

res, data, tf_used, signals_used, show_all_used, symbols_used = st.session_state["result"]

if res.empty:
    st.error("No data returned. The market may be closed for this timeframe, Yahoo may be rate limiting, "
             "or the symbols are invalid. Try again in a minute or use the 1d timeframe.")
    st.stop()

missing = [s.replace(".NS", "") for s in symbols_used if s not in data]
if missing:
    st.warning("No data for: " + ", ".join(missing))

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
