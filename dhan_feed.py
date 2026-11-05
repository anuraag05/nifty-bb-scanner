"""
Dhan (DhanHQ v2) data feed.

Returns DataFrames shaped like yfinance output (Open/High/Low/Close/Volume,
tz-aware IST DatetimeIndex) so the scanner can use either source.

Credentials are NEVER stored in the repo. Provide them via:
    - environment variables  DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN, or
    - the web app's sidebar / Streamlit secrets.
Dhan access tokens expire (24 h for tokens generated from the web console),
so generate a fresh one when requests start failing with 401/invalid token.
"""

import io
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_CACHE = Path(__file__).with_name(".dhan_scrip_master.csv")  # git-ignored
IST = "Asia/Kolkata"

# timeframe -> (Dhan interval in minutes or None for daily, days of history to request)
# Dhan allows at most 90 days per intraday request.
DHAN_TF = {
    "1m": (1, 5),
    "5m": (5, 30),
    "15m": (15, 60),
    "1h": (60, 90),
    "1d": (None, 365),
}

MIN_GAP = 0.25  # seconds between requests (Dhan data API allows ~5 req/s)


class DhanError(Exception):
    pass


def creds_from_env() -> tuple[str, str]:
    return os.environ.get("DHAN_CLIENT_ID", "").strip(), os.environ.get("DHAN_ACCESS_TOKEN", "").strip()


def _headers(token: str, client_id: str) -> dict:
    return {
        "access-token": token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def load_security_ids(symbols: list[str]) -> dict[str, str]:
    """Map NSE trading symbols (no .NS) -> Dhan securityId using Dhan's public instrument master."""
    fresh = SCRIP_CACHE.exists() and (time.time() - SCRIP_CACHE.stat().st_mtime) < 24 * 3600
    if fresh:
        text = SCRIP_CACHE.read_text()
    else:
        r = requests.get(SCRIP_MASTER_URL, timeout=60)
        r.raise_for_status()
        text = r.text
        try:
            SCRIP_CACHE.write_text(text)
        except OSError:
            pass  # read-only filesystem (some hosts) - just don't cache

    df = pd.read_csv(io.StringIO(text), low_memory=False)
    id_col = next((c for c in df.columns if "SECURITY_ID" in c.upper()), None)
    if id_col is None:
        raise DhanError("Could not find the security-id column in Dhan's instrument master.")
    df = df[
        (df["SEM_EXM_EXCH_ID"] == "NSE")
        & (df["SEM_SEGMENT"] == "E")
        & (df["SEM_INSTRUMENT_NAME"] == "EQUITY")
    ]
    if "SEM_SERIES" in df.columns:
        eq = df[df["SEM_SERIES"] == "EQ"]
        df = eq if not eq.empty else df
    df = df.drop_duplicates("SEM_TRADING_SYMBOL")
    table = dict(zip(df["SEM_TRADING_SYMBOL"].astype(str).str.upper(), df[id_col].astype(int).astype(str)))
    return {s: table[s] for s in symbols if s in table}


def _post(path: str, body: dict, token: str, client_id: str, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        r = requests.post(f"{BASE}{path}", json=body, headers=_headers(token, client_id), timeout=30)
        if r.status_code == 200:
            return r.json()
        last = r
        if r.status_code in (401, 403):
            raise DhanError(f"Dhan rejected the credentials ({r.status_code}). Token expired or wrong client id? {r.text[:200]}")
        if r.status_code == 429 or "DH-904" in r.text:  # rate limited -> back off
            time.sleep(1.5 * (attempt + 1))
            continue
        time.sleep(0.5)
    raise DhanError(f"Dhan error {last.status_code}: {last.text[:200]}")


def _to_frame(payload: dict) -> pd.DataFrame:
    ts = payload.get("timestamp") or []
    if not ts:
        return pd.DataFrame()
    idx = pd.to_datetime(pd.Series(ts), unit="s", utc=True).dt.tz_convert(IST)
    df = pd.DataFrame(
        {
            "Open": payload["open"],
            "High": payload["high"],
            "Low": payload["low"],
            "Close": payload["close"],
            "Volume": payload.get("volume", [0] * len(ts)),
        }
    )
    df.index = pd.DatetimeIndex(idx)
    return df[~df.index.duplicated()].sort_index()


def fetch_one(security_id: str, tf: str, token: str, client_id: str) -> pd.DataFrame:
    interval, days = DHAN_TF[tf]
    now = datetime.now()
    common = {"securityId": security_id, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "oi": False}
    if interval is None:
        body = {
            **common,
            "expiryCode": 0,
            "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
            "toDate": (now + timedelta(days=1)).strftime("%Y-%m-%d"),  # toDate is non-inclusive
        }
        return _to_frame(_post("/charts/historical", body, token, client_id))
    body = {
        **common,
        "interval": str(interval),
        "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:00:00"),
        "toDate": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return _to_frame(_post("/charts/intraday", body, token, client_id))


def download(symbols: list[str], tf: str, client_id: str = "", token: str = "", progress=None) -> dict[str, pd.DataFrame]:
    """symbols are Yahoo-style ('RELIANCE.NS'); returns {same_symbol: DataFrame}."""
    if not client_id or not token:
        client_id, token = creds_from_env()
    if not client_id or not token:
        raise DhanError("Dhan credentials missing. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN.")

    bare = [s.replace(".NS", "").upper() for s in symbols]
    ids = load_security_ids(bare)
    out = {}
    last_call = 0.0
    total = len(symbols)
    for n, (sym, bare_sym) in enumerate(zip(symbols, bare), 1):
        if progress:
            progress(n, total)
        sid = ids.get(bare_sym)
        if sid is None:
            continue
        wait = MIN_GAP - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        try:
            df = fetch_one(sid, tf, token, client_id)
        except DhanError as e:
            if "credentials" in str(e):
                raise
            continue  # skip a single failing symbol, reported as "no data" by the caller
        if not df.empty:
            out[sym] = df
    return out
