"""
Stock universes (which stocks to scan).

"Nifty 200" / "Nifty 500" are NSE's official lists of the top 200 / top 500 listed
companies ranked by full market capitalisation (averaged over six months, from NSE's
eligible stocks), so they are the standard "top N by market cap" lists. They are
reconstituted twice a year.

Lists are downloaded live from NSE's public archive, cached for 24 h, and fall back
to the last cached copy or a file you place in ./universes/<name>.csv.
You can also upload your own CSV (needs a 'Symbol' column) in the web app.
"""

import io
import time
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).parent
CACHE_DIR = HERE / ".universe_cache"
BUNDLED_DIR = HERE / "universes"
BASE = "https://nsearchives.nseindia.com/content/indices/"

UNIVERSES = {
    "Nifty 200 (top 200 by market cap)": "ind_nifty200list.csv",
    "Nifty 500 (top 500 by market cap)": "ind_nifty500list.csv",
    "Nifty 100": "ind_nifty100list.csv",
    "Nifty 50": "ind_nifty50list.csv",
    "Nifty Midcap 150": "ind_niftymidcap150list.csv",
    "Nifty Smallcap 250": "ind_niftysmallcap250list.csv",
    "F&O stocks (all stocks with futures & options)": "fo_mktlots.csv",
}
FNO = "F&O stocks (all stocks with futures & options)"
URLS = {FNO: "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"}  # everything else lives under BASE
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
DEFAULT = "Nifty 200 (top 200 by market cap)"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}


def parse_symbols(text: str) -> list[str]:
    """Extract NSE symbols from NSE's CSV (column 'Symbol'), any CSV with a Symbol column,
    or a plain one-symbol-per-line list."""
    try:
        df = pd.read_csv(io.StringIO(text))
        col = next((c for c in df.columns if str(c).strip().lower() == "symbol"), None)
        if col is not None:
            vals = df[col]
        elif df.shape[1] == 1 or text.lstrip().splitlines()[0].count(",") == 0:
            raise ValueError("plain list")
        else:
            vals = df.iloc[:, 0]
    except Exception:
        vals = pd.Series([ln.split("#")[0] for ln in text.splitlines()])
    out, seen = [], set()
    for v in vals.dropna().astype(str):
        s = v.strip().upper().replace(".NS", "")
        if s and s not in seen and s != "SYMBOL":
            seen.add(s)
            out.append(s)
    return out


def parse_fo_lots(text: str) -> list[str]:
    """NSE's F&O lot-size file: 'UNDERLYING, SYMBOL, <month>, <month>, ...' with an index section first and an
    'Individual Securities' (stocks) section after it. Returns only the stock symbols."""
    import csv
    rows = list(csv.reader(io.StringIO(text)))
    has_marker = any("individual securities" in ",".join(r).lower() for r in rows)
    started = not has_marker
    out, seen = [], set()
    for r in rows:
        line = ",".join(r).lower()
        if "individual securities" in line:
            started = True
            continue
        if has_marker and "derivatives on" in line and "individual" not in line:
            started = False
            continue
        if not started or len(r) < 3:
            continue
        sym = r[1].strip().upper()
        if not sym or sym == "SYMBOL" or sym in INDEX_UNDERLYINGS or sym in seen:
            continue
        if not any(c.strip().replace(".", "").isdigit() for c in r[2:]):  # needs a lot size
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _parse(name: str, text: str) -> list[str]:
    return parse_fo_lots(text) if name == FNO else parse_symbols(text)


def load(name: str) -> tuple[list[str], str]:
    """Return (symbols without .NS, note about where they came from)."""
    fname = UNIVERSES[name]
    cache = CACHE_DIR / fname
    bundled = BUNDLED_DIR / fname

    min_size = 20 if name != FNO else 100
    fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < 24 * 3600
    if fresh:
        return _parse(name, cache.read_text()), "cached NSE list (<24 h old)"

    err = None
    try:
        r = requests.get(URLS.get(name, BASE + fname), headers=HEADERS, timeout=25)
        r.raise_for_status()
        syms = _parse(name, r.text)
        if len(syms) >= min_size:
            try:
                CACHE_DIR.mkdir(exist_ok=True)
                cache.write_text(r.text)
            except OSError:
                pass
            return syms, "live NSE list"
    except Exception as e:  # network blocked, NSE down, etc.
        err = e

    if cache.exists():
        return _parse(name, cache.read_text()), "older cached NSE list (live download failed)"
    if name == FNO:  # NSE blocked -> derive the F&O list from Dhan's public instrument master
        try:
            import dhan_feed
            syms = dhan_feed.fno_underlyings()
            if len(syms) >= min_size:
                return syms, "derived from Dhan's stock-futures list (NSE download failed)"
        except Exception as e2:
            err = f"{err}; Dhan fallback: {e2}"
    if bundled.exists():
        return _parse(name, bundled.read_text()), "bundled list in ./universes"
    if name == "Nifty 50":
        return parse_symbols((HERE / "nifty50.txt").read_text()), "bundled nifty50.txt"
    raise RuntimeError(
        f"Could not download '{name}' from NSE ({err}). Upload the CSV in the sidebar, or save it as "
        f"universes/{fname} (download it from https://www.nseindia.com/products-content/equities/indices/)."
    )
