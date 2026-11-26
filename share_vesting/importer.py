"""Turn parsed documents into table rows and write them to Supabase.

Statement data is replaced per statement period: re-importing a period
deletes what that period wrote before and inserts it again, so importing
the same PDF twice (or a yearly PDF that overlaps a quarterly one) is safe.
Sales are keyed on Merrill's order reference, so a trade confirmation and
the later statement line for the same order end up as one row.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

from .parser import Parsed

PERIOD_TABLES = ["shares_vests", "shares_sale_lots", "shares_dividends", "shares_cash"]


def _iso(v):
    return v.isoformat() if isinstance(v, date) else v


def _clean(row: dict) -> dict:
    return {k: _iso(v) for k, v in row.items()}


def _period_of(d: date, periods: list) -> date | None:
    for p in periods:
        if p["start"] <= d <= p["end"]:
            return p["end"]
    return None


def rows_from_parsed(p: Parsed) -> dict:
    """Return {table: [rows]} plus the statement periods covered."""
    src = os.path.basename(p.source)
    periods = p.periods
    out: dict = {t: [] for t in PERIOD_TABLES}
    out.update(shares_sales=[], shares_statements=[], shares_lot_costs=[])

    for v in p.vests:
        out["shares_vests"].append(_clean(dict(
            period_end=_period_of(v["posted"], periods), posted_date=v["posted"],
            grant_date=v["grant_date"], lapse_date=v["lapse_date"],
            net_shares=v["quantity"], posted_value=v["value"], source=src)))

    # realized lots carry the trade date; statement sale lines carry the
    # settlement date. Link them on quantity within a week.
    by_trade: dict = {}
    for r in p.realized:
        by_trade.setdefault(r["sold"], 0.0)
        by_trade[r["sold"]] += r["quantity"]
        period_end = None
        for s in p.sales:
            sd = s.get("settle_date")
            if sd and r["sold"] <= sd <= r["sold"] + timedelta(days=7):
                period_end = _period_of(sd, periods)
        out["shares_sale_lots"].append(_clean(dict(
            period_end=period_end or _period_of(r["sold"], periods),
            trade_date=r["sold"], acquired_date=r["acquired"], quantity=r["quantity"],
            proceeds=r["proceeds"], cost_basis=r["cost_basis"], gain=r["gain"], source=src)))

    for s in p.sales:
        trade = s.get("trade_date")
        if trade is None and s.get("settle_date"):
            for td, qty in by_trade.items():
                if td <= s["settle_date"] <= td + timedelta(days=7) and abs(qty - s["quantity"]) < 1e-6:
                    trade = td
        out["shares_sales"].append(_clean(dict(
            period_end=_period_of(s["settle_date"], periods) if periods and s.get("settle_date") else None,
            order_ref=s.get("order_ref"), trade_date=trade, settle_date=s.get("settle_date"),
            quantity=s["quantity"], unit_price=s.get("unit_price"), gross_amount=s.get("amount"),
            fees=s.get("fees") or 0, net_amount=s.get("net"), source=src)))

    for d in p.dividends:
        out["shares_dividends"].append(_clean(dict(
            period_end=_period_of(d["date"], periods), pay_date=d["date"], kind=d["kind"],
            description=d["description"], holding=d["holding"], amount=d["amount"], source=src)))

    for c in p.cash:
        out["shares_cash"].append(_clean(dict(
            period_end=_period_of(c["date"], periods), txn_date=c["date"], type=c["type"],
            description=c["description"], detail=c["detail"], amount=c["amount"], source=src)))

    snaps = {s["as_of"]: s for s in p.snapshots}
    for per in periods:
        s = snaps.get(per["end"], {})
        out["shares_statements"].append(_clean(dict(
            period_end=per["end"], period_start=per["start"],
            opening_value=per.get("opening_value"), closing_value=per.get("closing_value"),
            shares_held=s.get("quantity", 0.0), cost_basis=s.get("cost_basis"),
            market_price=s.get("market_price"), market_value=s.get("market_value"),
            cash=s.get("cash"), source=src)))

    costs: dict = {}
    for l in p.lot_snapshots:
        costs[l["acquired"]] = l["unit_cost"]
    for r in p.realized:
        costs.setdefault(r["acquired"], round(r["cost_basis"] / r["quantity"], 4))
    out["shares_lot_costs"] = [dict(acquired_date=_iso(k), unit_cost=v) for k, v in sorted(costs.items())]
    out["_periods"] = [_iso(per["end"]) for per in periods]
    return out


def apply_rows(sb, rows: dict) -> dict:
    """Write rows to Supabase. Returns counts per table."""
    periods = rows.get("_periods") or []
    counts = {}
    if periods:
        for t in PERIOD_TABLES:
            sb.table(t).delete().in_("period_end", periods).execute()
    for t in PERIOD_TABLES:
        data = [r for r in rows[t] if r.get("period_end") in periods] if periods else rows[t]
        if data:
            sb.table(t).insert(data).execute()
        counts[t] = len(data)
    with_ref = [r for r in rows["shares_sales"] if r.get("order_ref")]
    if with_ref:
        # a statement line should not wipe a trade date we already know
        existing = {r["order_ref"]: r for r in sb.table("shares_sales").select("*")
                    .in_("order_ref", [r["order_ref"] for r in with_ref]).execute().data}
        for r in with_ref:
            old = existing.get(r["order_ref"])
            if old:
                for k, v in r.items():
                    if v is None and old.get(k) is not None:
                        r[k] = old[k]
        sb.table("shares_sales").upsert(with_ref, on_conflict="order_ref").execute()
    counts["shares_sales"] = len(with_ref)
    if rows["shares_statements"]:
        sb.table("shares_statements").upsert(rows["shares_statements"], on_conflict="period_end").execute()
    counts["shares_statements"] = len(rows["shares_statements"])
    if rows["shares_lot_costs"]:
        sb.table("shares_lot_costs").upsert(rows["shares_lot_costs"], on_conflict="acquired_date").execute()
    counts["shares_lot_costs"] = len(rows["shares_lot_costs"])
    return counts
