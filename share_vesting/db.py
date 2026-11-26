"""Supabase access. Uses the service-role key from Streamlit secrets."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st
from supabase import Client, create_client

from . import market

TABLES = ["shares_grants", "shares_vest_schedule", "shares_vests", "shares_sales",
          "shares_sale_lots", "shares_dividends", "shares_cash", "shares_statements",
          "shares_prices", "shares_lot_costs", "shares_documents", "shares_settings"]


@st.cache_resource
def client() -> Client:
    return create_client(st.secrets["supabase"]["url"], st.secrets["supabase"]["key"])


def _fetch_all(table: str) -> list:
    rows, start, page = [], 0, 1000
    while True:
        chunk = client().table(table).select("*").range(start, start + page - 1).execute().data
        rows += chunk
        if len(chunk) < page:
            return rows
        start += page


@st.cache_data(ttl=600, show_spinner=False)
def load(table: str) -> pd.DataFrame:
    return pd.DataFrame(_fetch_all(table))


def clear_cache():
    load.clear()


def setting(key: str, default: str) -> str:
    s = load("shares_settings")
    if s.empty or key not in set(s["key"]):
        return default
    return s.loc[s["key"] == key, "value"].iloc[0]


def set_setting(key: str, value: str):
    client().table("shares_settings").upsert({"key": key, "value": value}).execute()
    clear_cache()


def refresh_prices(first_date: date) -> str:
    """Backfill daily BAC closes and EUR/USD rates up to today."""
    prices = load("shares_prices")
    start = first_date
    if not prices.empty:
        p = prices.copy()
        p["price_date"] = pd.to_datetime(p["price_date"]).dt.date
        have = p.dropna(subset=["eurusd", "bac_close"], how="any")
        if not have.empty:
            start = max(first_date, have["price_date"].max() - timedelta(days=5))
    msgs = []
    try:
        fx = market.eurusd_history(start)
    except Exception as e:
        fx = pd.DataFrame(columns=["price_date", "eurusd"])
        msgs.append(f"EUR/USD history unavailable: {e}")
    try:
        bac = market.bac_history(start)
    except Exception as e:
        bac = pd.DataFrame(columns=["price_date", "bac_close"])
        msgs.append(f"BAC history unavailable: {e}")
    merged = pd.merge(fx, bac, on="price_date", how="outer")
    if not merged.empty:
        # keep any value already stored when a source has no quote that day
        recs = []
        for r in merged.to_dict("records"):
            recs.append({k: (None if pd.isna(v) else v) for k, v in r.items()})
        old = {}
        if not prices.empty:
            old = {str(r["price_date"]): r for r in prices.to_dict("records")}
        for r in recs:
            o = old.get(r["price_date"], {})
            for k in ("eurusd", "bac_close"):
                if r.get(k) is None and o.get(k) is not None and not pd.isna(o.get(k)):
                    r[k] = o[k]
        for i in range(0, len(recs), 500):
            client().table("shares_prices").upsert(recs[i:i + 500], on_conflict="price_date").execute()
        msgs.append(f"Stored {len(recs)} days of prices from {start}.")
    clear_cache()
    return " ".join(msgs) or "Prices already up to date."
