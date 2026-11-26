"""BAC share vesting tracker."""
from __future__ import annotations

import os
import tempfile
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from share_vesting import calc, db, market
from share_vesting.importer import apply_rows, rows_from_parsed
from share_vesting.parser import file_hash, parse_pdf

st.set_page_config(page_title="Share Vesting", page_icon="📈", layout="wide")


# ------------------------------------------------------------------ login
def login() -> bool:
    if st.session_state.get("authed"):
        return True
    pw = st.secrets.get("app_password")
    if not pw:
        st.error("Set `app_password` in the app's secrets.")
        return False
    st.title("Share Vesting")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", width="stretch"):
            if entered == pw:
                st.session_state.authed = True
                st.rerun()
            st.error("Wrong password.")
    return False


if not login():
    st.stop()


# ------------------------------------------------------------------ data
def d(table):
    return db.load(table)


def usd(x):
    return "—" if x is None or pd.isna(x) else f"${x:,.2f}"


def eur(x):
    return "—" if x is None or pd.isna(x) else f"€{x:,.2f}"


@st.cache_data(ttl=300, show_spinner=False)
def live_quotes():
    return market.latest_bac(), market.latest_eurusd()


def last_known(col):
    p = d("shares_prices")
    if p.empty or col not in p:
        return None
    p = p.dropna(subset=[col]).sort_values("price_date")
    return None if p.empty else float(p[col].iloc[-1])


grants, schedule_t, vests = d("shares_grants"), d("shares_vest_schedule"), d("shares_vests")
sales, sale_lots, lot_costs = d("shares_sales"), d("shares_sale_lots"), d("shares_lot_costs")
divs, cash, stmts, prices = d("shares_dividends"), d("shares_cash"), d("shares_statements"), d("shares_prices")
net_rate = float(db.setting("net_rate", "0.48"))

bac, fx = live_quotes()
price_note = "live"
if bac is None:
    bac, price_note = last_known("bac_close"), "last stored"
if fx is None:
    fx = last_known("eurusd")

lot_df = calc.lots(vests, sale_lots, lot_costs, grants)
held = float(lot_df["remaining"].sum()) if not lot_df.empty else 0.0
if not sales.empty:
    # sales not yet broken into lots on a statement (e.g. from a trade confirmation)
    covered = set(pd.to_datetime(sale_lots["trade_date"]).dt.date) if not sale_lots.empty else set()
    s = sales.copy()
    s["trade_date"] = pd.to_datetime(s["trade_date"]).dt.date
    pending_sales = s[~s["trade_date"].isin(covered)]
else:
    pending_sales = pd.DataFrame()
pending_qty = float(pending_sales["quantity"].sum()) if not pending_sales.empty else 0.0
held_now = held - pending_qty
cost_held = float(lot_df["remaining_cost"].sum()) if not lot_df.empty else 0.0
if pending_qty and held:
    cost_held *= held_now / held   # approximate until the statement assigns lots (FIFO)
sched = calc.schedule(grants, schedule_t, vests, net_rate)

# ------------------------------------------------------------------ pages
with st.sidebar:
    st.markdown("### 📈 Share Vesting")
    page = st.radio("Page", ["Dashboard", "Vesting", "Holdings", "Sales & gains",
                             "Dividends & cash", "Grants", "Import", "Reconcile"],
                    label_visibility="collapsed")
    st.caption(f"BAC {usd(bac)} ({price_note})  \nEUR/USD {fx:.4f}" if bac and fx else "Prices unavailable")
    if st.button("Refresh data", width="stretch"):
        db.clear_cache()
        live_quotes.clear()
        st.rerun()


def metric_row(items):
    cols = st.columns(len(items))
    for c, (label, val, help_) in zip(cols, items):
        c.metric(label, val, help=help_)


if page == "Dashboard":
    st.header("Dashboard")
    value = held_now * (bac or 0)
    unreal = value - cost_held
    upcoming = sched[sched["status"] == "upcoming"] if not sched.empty else pd.DataFrame()
    unvested_net = float(upcoming["net_estimate"].sum()) if not upcoming.empty else 0.0
    metric_row([
        ("Shares held", f"{held_now:,.0f}", "Vested shares in the Merrill account"),
        ("Value", usd(value), f"{eur(calc.to_eur(value, fx))}"),
        ("Unrealised gain", usd(unreal), "Value less Merrill cost basis of shares held"),
        ("Unvested (net est.)", f"{unvested_net:,.0f}",
         f"Gross upcoming shares × {net_rate:.0%}; worth {usd(unvested_net * (bac or 0))}"),
    ])
    st.caption(f"Value in EUR: {eur(calc.to_eur(value, fx))} · Unrealised in EUR: {eur(calc.to_eur(unreal, fx))}")

    real = float(sale_lots["gain"].sum()) if not sale_lots.empty else 0.0
    proceeds = float(sales["net_amount"].sum()) if not sales.empty else 0.0
    div_total = float(divs["amount"].sum()) if not divs.empty else 0.0
    nra = float(-cash.loc[cash["type"].str.contains("Non-Resident", na=False), "amount"].sum()) if not cash.empty else 0.0
    last_cash = None
    if not stmts.empty:
        last = stmts.sort_values("period_end").iloc[-1]
        last_cash = last["cash"]
    metric_row([
        ("Total sold (net)", usd(proceeds), f"{sales['quantity'].sum():,.0f} shares" if not sales.empty else None),
        ("Realised gain", usd(real), "From statement lot detail"),
        ("Dividends", usd(div_total), f"US tax withheld {usd(nra)}"),
        ("Cash in account", usd(last_cash), "At last statement"),
    ])
    if not pending_sales.empty:
        st.info(f"{pending_qty:,.0f} shares sold on "
                f"{', '.join(str(x) for x in pending_sales['trade_date'])} are waiting for a quarterly "
                "statement to show which lots were sold.")

    if not upcoming.empty:
        nxt = upcoming.iloc[0]
        st.subheader("Next vests")
        nv = upcoming.groupby("vest_date", as_index=False).agg(gross=("gross_shares", "sum"),
                                                                 net_est=("net_estimate", "sum")).head(6)
        nv["value_est"] = nv["net_est"] * (bac or 0)
        st.dataframe(nv, hide_index=True, width="stretch",
                     column_config={"value_est": st.column_config.NumberColumn("Est. value ($)", format="$%.0f")})

    if not stmts.empty:
        st.subheader("Account value by quarter")
        ch = stmts.copy()
        ch["period_end"] = pd.to_datetime(ch["period_end"])
        for c in ("closing_value", "market_value", "cash", "shares_held"):
            ch[c] = pd.to_numeric(ch[c], errors="coerce")
        ch["closing_value"] = ch["closing_value"].fillna(ch["market_value"].fillna(0) + ch["cash"].fillna(0))
        chart = alt.Chart(ch).mark_area(opacity=0.5).encode(
            x=alt.X("period_end:T", title=None),
            y=alt.Y("closing_value:Q", title="Closing value ($)"),
            tooltip=["period_end:T", alt.Tooltip("closing_value:Q", format="$,.0f"),
                     alt.Tooltip("shares_held:Q", format=",.0f")])
        st.altair_chart(chart, width="stretch")

elif page == "Vesting":
    st.header("Vesting schedule")
    if sched.empty:
        st.info("No grants yet. Add them on the Grants page.")
    else:
        years = sched.copy()
        years["year"] = [x.year for x in years["vest_date"]]
        piv = years.pivot_table(index="year", columns="status", values="net_estimate", aggfunc="sum").fillna(0)
        piv["gross"] = years.groupby("year")["gross_shares"].sum()
        piv["value_now"] = piv.get("upcoming", 0) * (bac or 0)
        st.subheader("By year (net shares)")
        st.dataframe(piv.reset_index(), hide_index=True, width="stretch",
                     column_config={"value_now": st.column_config.NumberColumn(
                         "Upcoming value at today's price", format="$%.0f")})
        bar = alt.Chart(years).mark_bar().encode(
            x=alt.X("year:O", title=None), y=alt.Y("sum(net_estimate):Q", title="Net shares"),
            color=alt.Color("status:N", legend=alt.Legend(orient="bottom")),
            tooltip=["year", "status", "sum(net_estimate)"])
        st.altair_chart(bar, width="stretch")
        st.subheader("Every tranche")
        show = sched[["vest_date", "grant_date", "award_type", "gross_shares", "delivered",
                      "withheld", "released", "delayed", "net_estimate", "status"]]
        st.dataframe(show, hide_index=True, width="stretch")
        st.caption(f"Upcoming net shares assume {net_rate:.0%} is delivered after tax. "
                   "Vested rows use what was actually delivered.")

elif page == "Holdings":
    st.header("Holdings by lot")
    if lot_df.empty:
        st.info("Nothing imported yet.")
    else:
        h = lot_df[lot_df["remaining"] > 0].copy()
        h["value"] = h["remaining"] * (bac or 0)
        h["gain"] = h["value"] - h["remaining_cost"]
        st.dataframe(h[["lapse_date", "grant_date", "award_type", "net_shares", "remaining",
                        "unit_cost", "remaining_cost", "value", "gain"]],
                     hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="$%.2f")
                                    for c in ["unit_cost", "remaining_cost", "value", "gain"]})
        if not pending_sales.empty:
            st.caption(f"Lot detail does not yet include {pending_qty:,.0f} shares sold since the last statement.")
        with st.expander("All vest deliveries"):
            st.dataframe(lot_df, hide_index=True, width="stretch")

elif page == "Sales & gains":
    st.header("Sales & gains")
    if sales.empty:
        st.info("No sales yet.")
    else:
        s = sales.copy().sort_values("trade_date", ascending=False)
        if not sale_lots.empty:
            g = sale_lots.groupby("trade_date", as_index=False).agg(cost_basis=("cost_basis", "sum"),
                                                                   gain=("gain", "sum"))
            s = s.merge(g, on="trade_date", how="left")
        s["fx"] = [calc.rate_on(prices, pd.to_datetime(x).date()) if pd.notna(x) else None for x in s["trade_date"]]
        s["net_eur"] = s["net_amount"] / s["fx"]
        st.dataframe(s[["trade_date", "settle_date", "quantity", "unit_price", "fees", "net_amount",
                        "cost_basis", "gain", "fx", "net_eur"]],
                     hide_index=True, width="stretch")
        detail, yearly = calc.cgt(sale_lots, prices)
        if not yearly.empty:
            st.subheader("Irish CGT view (for information)")
            st.caption("Cost converted at the ECB rate on the vest date, proceeds at the rate on the "
                       "sale date; €1,270 annual exemption; 33%. Not tax advice.")
            st.dataframe(yearly, hide_index=True, width="stretch",
                         column_config={c: st.column_config.NumberColumn(format="%.2f") for c in yearly.columns if c != "tax_year"})
            if yearly["gain_eur"].isna().any():
                st.warning("Some FX rates are missing. Use Import → Refresh prices.")
            with st.expander("Lot detail"):
                st.dataframe(detail, hide_index=True, width="stretch")

elif page == "Dividends & cash":
    st.header("Dividends & cash")
    if not divs.empty:
        dv = divs.copy()
        dv["year"] = pd.to_datetime(dv["pay_date"]).dt.year
        tax = pd.DataFrame()
        if not cash.empty:
            t = cash[cash["type"].str.contains("Non-Resident", na=False)].copy()
            t["year"] = pd.to_datetime(t["txn_date"]).dt.year
            tax = t.groupby("year", as_index=False)["amount"].sum().rename(columns={"amount": "us_tax"})
        y = dv.groupby("year", as_index=False)["amount"].sum().rename(columns={"amount": "dividends"})
        if not tax.empty:
            y = y.merge(tax, on="year", how="left").fillna(0)
            y["net"] = y["dividends"] + y["us_tax"]
        st.subheader("Dividends by year")
        st.dataframe(y, hide_index=True, width="stretch")
        with st.expander("Every dividend"):
            st.dataframe(dv.sort_values("pay_date", ascending=False)[["pay_date", "holding", "amount", "kind"]],
                         hide_index=True, width="stretch")
    if not cash.empty:
        st.subheader("Cash movements")
        c = cash.sort_values("txn_date", ascending=False)
        st.dataframe(c[["txn_date", "type", "description", "detail", "amount"]],
                     hide_index=True, width="stretch")
        out = cash[cash["amount"] < 0].groupby("type")["amount"].sum()
        st.caption("Totals out: " + " · ".join(f"{k}: {usd(-v)}" for k, v in out.items()))

elif page == "Grants":
    st.header("Grants")
    st.caption("Awards are entered by hand (statements only show the net shares delivered). "
               "Each grant's yearly gross tranches drive the vesting schedule.")
    if not grants.empty:
        gv = grants.sort_values("grant_date")[["id", "grant_date", "award_type", "total_shares",
                                               "issue_price", "vesting_years", "confirmed", "notes"]]
        st.dataframe(gv, hide_index=True, width="stretch")
    with st.form("new_grant"):
        st.subheader("Add a grant")
        c1, c2, c3 = st.columns(3)
        gd = c1.date_input("Grant date", value=date(date.today().year, 3, 15))
        at = c2.selectbox("Type", ["profit_share", "bonus"])
        tot = c3.number_input("Total shares", min_value=0, step=1)
        c4, c5 = st.columns(2)
        ip = c4.number_input("Issue price ($, bonus only)", min_value=0.0, step=0.01)
        yrs = c5.number_input("Vesting years", min_value=1, max_value=10, value=4)
        notes = st.text_input("Notes")
        if st.form_submit_button("Add grant") and tot:
            res = db.client().table("shares_grants").insert(dict(
                grant_date=gd.isoformat(), award_type=at, total_shares=tot,
                issue_price=ip or None, vesting_years=int(yrs), notes=notes or None,
                confirmed=False)).execute()
            gid = res.data[0]["id"]
            base, extra = divmod(int(tot), int(yrs))
            rows = []
            for i in range(int(yrs)):
                vd = date(gd.year + i + 1, gd.month, min(gd.day, 28) if gd.month == 2 else gd.day)
                rows.append(dict(grant_id=gid, vest_date=vd.isoformat(),
                                 gross_shares=base + (1 if i >= int(yrs) - extra else 0)))
            db.client().table("shares_vest_schedule").insert(rows).execute()
            db.clear_cache()
            st.success("Grant added with an even vesting schedule. Adjust tranches below if needed.")
            st.rerun()
    if not schedule_t.empty:
        st.subheader("Edit tranches")
        ed = schedule_t.sort_values(["vest_date", "grant_id"])[["id", "grant_id", "vest_date", "gross_shares"]]
        new = st.data_editor(ed, hide_index=True, disabled=["id", "grant_id"], width="stretch")
        if st.button("Save tranches"):
            changed = new.merge(ed, on="id", suffixes=("", "_old"))
            changed = changed[(changed["gross_shares"] != changed["gross_shares_old"]) |
                              (changed["vest_date"].astype(str) != changed["vest_date_old"].astype(str))]
            for r in changed.to_dict("records"):
                db.client().table("shares_vest_schedule").update(
                    {"gross_shares": r["gross_shares"], "vest_date": str(r["vest_date"])}).eq("id", r["id"]).execute()
            db.clear_cache()
            st.success(f"Saved {len(changed)} change(s).")
            st.rerun()
    st.subheader("Settings")
    nr = st.number_input("Share of a vest delivered after tax", 0.0, 1.0, net_rate, 0.01)
    if nr != net_rate and st.button("Save rate"):
        db.set_setting("net_rate", str(nr))
        st.rerun()

elif page == "Import":
    st.header("Import statements")
    st.caption("Drop Merrill quarterly or annual statements and trade confirmations. "
               "Re-importing the same period replaces it, so duplicates are safe. "
               "The PDFs are read here and not stored.")
    files = st.file_uploader("PDFs", type="pdf", accept_multiple_files=True)
    if files and st.button(f"Import {len(files)} file(s)", type="primary"):
        docs = d("shares_documents")
        seen = set(docs["file_hash"]) if not docs.empty else set()
        log = []
        # statements first so that trade confirmations add to them
        parsed = []
        for f in files:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(f.getbuffer())
                path = tmp.name
            try:
                h = file_hash(path)
                p = parse_pdf(path)
                p.source = f.name
                parsed.append((f.name, h, p))
            except Exception as e:
                log.append((f.name, f"could not read: {e}"))
            finally:
                os.unlink(path)
        parsed.sort(key=lambda x: (x[2].kind != "statement",
                                   x[2].periods[0]["start"] if x[2].periods else date.max))
        for name, h, p in parsed:
            if h in seen:
                log.append((name, "already imported, re-applied"))
            rows = rows_from_parsed(p)
            counts = apply_rows(db.client(), rows)
            per = p.periods
            db.client().table("shares_documents").upsert(dict(
                file_hash=h, filename=name, kind=p.kind,
                period_start=per[0]["start"].isoformat() if per else None,
                period_end=per[-1]["end"].isoformat() if per else None)).execute()
            summary = ", ".join(f"{k.replace('shares_', '')} {v}" for k, v in counts.items() if v)
            log.append((name, f"{p.kind}: {summary or 'nothing found'}"
                              + (f" · warnings: {'; '.join(p.warnings)}" if p.warnings else "")))
        db.clear_cache()
        st.dataframe(pd.DataFrame(log, columns=["file", "result"]), hide_index=True, width="stretch")
        st.success("Done. Check the Reconcile page.")
    st.divider()
    st.subheader("Prices")
    first = date(2019, 1, 1)
    if st.button("Refresh price & FX history"):
        with st.spinner("Downloading..."):
            st.info(db.refresh_prices(first))
    if not prices.empty:
        st.caption(f"{len(prices)} days stored, latest {prices['price_date'].max()}.")
    docs = d("shares_documents")
    if not docs.empty:
        with st.expander("Imported documents"):
            st.dataframe(docs.sort_values("period_end")[["filename", "kind", "period_start", "period_end", "imported_at"]],
                         hide_index=True, width="stretch")

elif page == "Reconcile":
    st.header("Reconcile")
    st.subheader("Shares")
    st.caption("Shares calculated from imported vests and sales, compared with the share count "
               "printed on each statement.")
    rec = calc.reconcile(stmts, vests, sales)
    if rec.empty:
        st.info("Nothing imported yet.")
    else:
        bad = rec[rec["difference"].abs() > 1e-6]
        if bad.empty:
            st.success(f"All {len(rec)} statement periods match.")
        else:
            st.error(f"{len(bad)} period(s) don't match. A statement is probably missing.")
        st.dataframe(rec, hide_index=True, width="stretch")
    st.subheader("Cash")
    crec = calc.cash_reconcile(stmts, divs, sales, cash)
    if not crec.empty:
        off = crec[crec["difference"].abs() > 0.01]
        if off.empty:
            st.success("Cash balance adds up in every period.")
        else:
            st.warning(f"Cash doesn't add up in {len(off)} period(s).")
        st.dataframe(crec, hide_index=True, width="stretch")
    over = calc.oversold(vests, sale_lots)
    if not over.empty:
        st.warning("More shares sold than vested for these acquisition dates. A vest is missing:")
        st.dataframe(over, width="stretch")
    if not stmts.empty:
        st_dates = pd.to_datetime(stmts["period_end"]).dt.to_period("Q")
        full = pd.period_range(st_dates.min(), st_dates.max(), freq="Q")
        missing = [str(q) for q in full if q not in set(st_dates)]
        if missing:
            st.warning("No statement imported for: " + ", ".join(missing))
