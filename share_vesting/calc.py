"""Pure calculations on the tables (as pandas DataFrames). No I/O here."""
from __future__ import annotations

from datetime import date

import pandas as pd

IRISH_CGT_RATE = 0.33
IRISH_CGT_EXEMPTION = 1270.0


def _dt(df: pd.DataFrame, *cols) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        if c in df:
            df[c] = pd.to_datetime(df[c]).dt.date
    return df


def lots(vests: pd.DataFrame, sale_lots: pd.DataFrame, lot_costs: pd.DataFrame,
         grants: pd.DataFrame | None = None) -> pd.DataFrame:
    """Every vest delivery with how many of its shares are still held.

    Merrill reports sales per acquisition (lapse) date, not per grant, so
    sold shares are taken from the vests sharing that date in posting order.
    """
    if vests.empty:
        return pd.DataFrame(columns=["lapse_date", "grant_date", "award_type", "net_shares",
                                     "sold", "remaining", "unit_cost", "remaining_cost"])
    v = _dt(vests, "lapse_date", "grant_date", "posted_date").sort_values(["lapse_date", "posted_date", "id"])
    sold = {}
    if not sale_lots.empty:
        sl = _dt(sale_lots, "acquired_date")
        sold = sl.groupby("acquired_date")["quantity"].sum().to_dict()
    costs = {}
    if not lot_costs.empty:
        lc = _dt(lot_costs, "acquired_date")
        costs = dict(zip(lc["acquired_date"], lc["unit_cost"].astype(float)))
    rows = []
    for _, r in v.iterrows():
        left = sold.get(r["lapse_date"], 0.0)
        take = min(left, float(r["net_shares"]))
        sold[r["lapse_date"]] = left - take
        uc = costs.get(r["lapse_date"])
        if uc is None and r.get("posted_value") and r["net_shares"]:
            uc = float(r["posted_value"]) / float(r["net_shares"])
        rows.append(dict(id=r["id"], lapse_date=r["lapse_date"], posted_date=r["posted_date"],
                         grant_date=r["grant_date"], net_shares=float(r["net_shares"]),
                         sold=take, remaining=float(r["net_shares"]) - take, unit_cost=uc))
    out = pd.DataFrame(rows)
    out["remaining_cost"] = out["remaining"] * out["unit_cost"].fillna(0)
    if grants is not None and not grants.empty:
        g = _dt(grants, "grant_date")[["grant_date", "award_type"]]
        out = out.merge(g, on="grant_date", how="left")
    else:
        out["award_type"] = None
    return out


def oversold(vests: pd.DataFrame, sale_lots: pd.DataFrame) -> pd.DataFrame:
    """Acquisition dates where more shares were sold than were vested."""
    if sale_lots.empty:
        return pd.DataFrame()
    v = _dt(vests, "lapse_date").groupby("lapse_date")["net_shares"].sum()
    s = _dt(sale_lots, "acquired_date").groupby("acquired_date")["quantity"].sum()
    df = pd.DataFrame({"vested": v, "sold": s}).fillna(0)
    return df[df["sold"] > df["vested"] + 1e-6]


def shares_held_on(d: date, vests: pd.DataFrame, sales: pd.DataFrame) -> float:
    v = _dt(vests, "posted_date")
    s = _dt(sales, "settle_date", "trade_date")
    held = v.loc[v["posted_date"] <= d, "net_shares"].astype(float).sum()
    if not s.empty:
        sd = s["settle_date"].fillna(s["trade_date"])
        held -= s.loc[sd <= d, "quantity"].astype(float).sum()
    return held


def reconcile(statements: pd.DataFrame, vests: pd.DataFrame, sales: pd.DataFrame) -> pd.DataFrame:
    if statements.empty:
        return statements
    st = _dt(statements, "period_end", "period_start").sort_values("period_end")
    st["calculated"] = [shares_held_on(d, vests, sales) for d in st["period_end"]]
    st["difference"] = st["shares_held"].astype(float) - st["calculated"]
    return st[["period_start", "period_end", "shares_held", "calculated", "difference",
               "market_price", "market_value", "cash", "closing_value"]]


def schedule(grants: pd.DataFrame, vest_schedule: pd.DataFrame, vests: pd.DataFrame,
             net_rate: float) -> pd.DataFrame:
    """Planned gross vests joined to what was actually delivered."""
    if vest_schedule.empty:
        return pd.DataFrame()
    g = _dt(grants, "grant_date")
    vs = _dt(vest_schedule, "vest_date").merge(
        g[["id", "grant_date", "award_type", "total_shares"]].rename(columns={"id": "grant_id"}),
        on="grant_id")
    actual = pd.DataFrame(columns=["grant_date", "vest_year", "delivered"])
    if not vests.empty:
        v = _dt(vests, "grant_date", "lapse_date")
        v["vest_year"] = [d.year for d in v["lapse_date"]]
        actual = v.groupby(["grant_date", "vest_year"], as_index=False)["net_shares"].sum() \
            .rename(columns={"net_shares": "delivered"})
    vs["vest_year"] = [d.year for d in vs["vest_date"]]
    out = vs.merge(actual, on=["grant_date", "vest_year"], how="left")
    out["gross_shares"] = out["gross_shares"].astype(float)
    out["delivered"] = out["delivered"].astype(float)
    today = date.today()
    out["status"] = ["vested" if pd.notna(dl) else ("not delivered" if vd <= today else "upcoming")
                     for dl, vd in zip(out["delivered"], out["vest_date"])]
    out["withheld"] = out["gross_shares"] - out["delivered"]
    est = (out["gross_shares"] * net_rate).round()
    out["net_estimate"] = out["delivered"].where(out["status"] == "vested",
                                                 est.where(out["status"] == "upcoming", 0.0))
    return out.sort_values(["vest_date", "grant_date"])


def to_eur(usd: float, eurusd: float | None) -> float | None:
    return None if not eurusd else usd / eurusd


def rate_on(prices: pd.DataFrame, d: date) -> float | None:
    """Most recent EUR/USD rate on or before d."""
    if prices.empty:
        return None
    p = _dt(prices, "price_date").dropna(subset=["eurusd"]).sort_values("price_date")
    p = p[p["price_date"] <= d]
    return None if p.empty else float(p["eurusd"].iloc[-1])


def cgt(sale_lots: pd.DataFrame, prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Irish CGT view: cost converted at the acquisition date's rate, proceeds
    at the sale date's rate. For information only."""
    if sale_lots.empty:
        return pd.DataFrame(), pd.DataFrame()
    sl = _dt(sale_lots, "trade_date", "acquired_date")
    sl["fx_acq"] = [rate_on(prices, d) for d in sl["acquired_date"]]
    sl["fx_sale"] = [rate_on(prices, d) for d in sl["trade_date"]]
    sl["cost_eur"] = sl["cost_basis"].astype(float) / sl["fx_acq"]
    sl["proceeds_eur"] = sl["proceeds"].astype(float) / sl["fx_sale"]
    sl["gain_eur"] = sl["proceeds_eur"] - sl["cost_eur"]
    sl["tax_year"] = [d.year for d in sl["trade_date"]]
    yearly = sl.groupby("tax_year", as_index=False).agg(
        proceeds_usd=("proceeds", "sum"), gain_usd=("gain", "sum"),
        proceeds_eur=("proceeds_eur", "sum"), gain_eur=("gain_eur", "sum"))
    yearly["exemption"] = yearly["gain_eur"].clip(lower=0).clip(upper=IRISH_CGT_EXEMPTION)
    yearly["taxable_eur"] = (yearly["gain_eur"] - IRISH_CGT_EXEMPTION).clip(lower=0)
    yearly["cgt_eur"] = yearly["taxable_eur"] * IRISH_CGT_RATE
    return sl, yearly


def cash_reconcile(statements: pd.DataFrame, dividends: pd.DataFrame, sales: pd.DataFrame,
                   cash: pd.DataFrame) -> pd.DataFrame:
    """Previous cash + dividends + sale proceeds + other cash lines = closing cash?"""
    if statements.empty:
        return statements
    st = _dt(statements, "period_start", "period_end").sort_values("period_end").reset_index(drop=True)
    dv = _dt(dividends, "pay_date") if not dividends.empty else pd.DataFrame(columns=["pay_date", "amount"])
    sa = _dt(sales, "settle_date") if not sales.empty else pd.DataFrame(columns=["settle_date", "net_amount"])
    ca = _dt(cash, "txn_date") if not cash.empty else pd.DataFrame(columns=["txn_date", "amount"])

    def within(df, col, val, a, b):
        return float(df.loc[(df[col] >= a) & (df[col] <= b), val].astype(float).sum()) if not df.empty else 0.0

    rows, prev = [], None
    for _, r in st.iterrows():
        a, b = r["period_start"], r["period_end"]
        flows = within(dv, "pay_date", "amount", a, b) + within(sa, "settle_date", "net_amount", a, b) \
            + within(ca, "txn_date", "amount", a, b)
        closing = float(r["cash"]) if pd.notna(r["cash"]) else 0.0
        expected = None if prev is None else round(prev + flows, 2)
        rows.append(dict(period_end=b, opening_cash=prev, flows=round(flows, 2), expected=expected,
                         statement_cash=closing,
                         difference=None if expected is None else round(closing - expected, 2)))
        prev = closing
    return pd.DataFrame(rows)
