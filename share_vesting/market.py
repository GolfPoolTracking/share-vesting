"""Live and historic BAC prices (Yahoo) and EUR/USD rates (ECB via Frankfurter)."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import requests

FRANKFURTER = "https://api.frankfurter.app"


def latest_bac() -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker("BAC")
        price = t.fast_info.get("last_price")
        if price:
            return float(price)
        h = t.history(period="5d")
        return float(h["Close"].iloc[-1]) if not h.empty else None
    except Exception:
        return None


def latest_eurusd() -> float | None:
    try:
        r = requests.get(f"{FRANKFURTER}/latest", params={"from": "EUR", "to": "USD"}, timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["USD"])
    except Exception:
        try:
            import yfinance as yf
            h = yf.Ticker("EURUSD=X").history(period="5d")
            return float(h["Close"].iloc[-1]) if not h.empty else None
        except Exception:
            return None


def eurusd_history(start: date, end: date | None = None) -> pd.DataFrame:
    end = end or date.today()
    r = requests.get(f"{FRANKFURTER}/{start.isoformat()}..{end.isoformat()}",
                     params={"from": "EUR", "to": "USD"}, timeout=30)
    r.raise_for_status()
    rates = r.json()["rates"]
    return pd.DataFrame([{"price_date": d, "eurusd": v["USD"]} for d, v in sorted(rates.items())])


def bac_history(start: date, end: date | None = None) -> pd.DataFrame:
    import yfinance as yf
    end = end or date.today()
    h = yf.Ticker("BAC").history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat())
    if h.empty:
        return pd.DataFrame(columns=["price_date", "bac_close"])
    return pd.DataFrame({"price_date": [d.date().isoformat() for d in h.index],
                         "bac_close": h["Close"].round(4).tolist()})
