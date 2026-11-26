"""Parser tests on synthetic statement text (same layout as Merrill's PDFs, no real data)."""
from datetime import date

from share_vesting.importer import rows_from_parsed
from share_vesting.parser import parse_statement_text, parse_trade_confirm_text

HEADER = "YOUR INDIVIDUAL INVESTOR ACCOUNT TRANSACTIONS                       January 01, 2030 - March 31, 2030\n"

STATEMENT = HEADER + """
SECURITIES YOU TRANSFERRED IN/OUT
Date        Description                                   Transaction Type                       Quantity    Value of Securities    Year To Date
02/20       BANK OF AMERICA CORP                          Transfer / Adjustment                  48.0000              1,630.08
            RSU ACTIVITY N/O A Person SHARES GRANT DATE 02/15/2028 LAPSE DATE 02/15/2030 AS OF 02/18/30
            NET TOTAL                                                                                                 1,630.08         1,630.08
DIVIDENDS/INTEREST INCOME TRANSACTIONS                                                                               Income
Date   Description                       Transaction Type                                        Quantity           Income           Year To Date
Taxable Dividends
03/27 BANK OF AMERICA CORP             * Dividend                                                                     125.44
       HOLDING 448.0000 PAY DATE 03/27/2030
SECURITY TRANSACTIONS
Settlement                                                                                                    Transaction       Commissions/                    (Debit)/
Date       Description                        Transaction Type                             Quantity               Amount         Trading Fees                     Credit
Sales
03/10     BANK OF AMERICA CORP        Sale                                               -30.0000              1,500.00                    (.22)            1,499.78
          SSO100 CUS NO 060505104 UNIT PRICE             50.0000
REALIZED GAINS/(LOSSES)
                                                                          Acquired Liquidation                                                                Gains/(Losses)
Description                                                       Quantity       Date        Date          Sale Amount         Cost Basis             This Statement          Year to Date
        BANK OF AMERICA CORP                                      30.0000    02/15/29   03/06/30              1,499.78          1,000.00                        499.78
CASH/OTHER TRANSACTIONS
Date        Description                                  Transaction Type                                          Quantity                       Debit                          Credit
Electronic Transfers
03/12 WIRE TRF OUT123                                    Wire Transfer Out                                                                     1,400.00
    .
Other Debits/Credits
03/27 BANK OF AMERICA CORP                               Non-Resident Alien Tax                                                                   18.82
         TAX ON NRA DIVIDEND PAY DATE 03/27/2030
\f""" + HEADER.replace("TRANSACTIONS", "ASSETS") + """
EQUITIES                                                                                            Total        Estimated               Estimated
Description                             Symbol                         Quantity                Cost Basis      Market Price            Market Value
BANK OF AMERICA CORP                       BAC                       448.0000                  21,348.35           56.9800                  25,527.04
CASH/MONEY ACCOUNTS
CASH                                                   106.62                 106.62                                                  106.62
"""


def test_statement_sections():
    p = parse_statement_text(STATEMENT, "s.pdf")
    assert p.periods[0]["start"] == date(2030, 1, 1)
    v = p.vests[0]
    assert (v["quantity"], v["grant_date"], v["lapse_date"]) == (48.0, date(2028, 2, 15), date(2030, 2, 15))
    s = p.sales[0]
    assert (s["quantity"], s["fees"], s["net"], s["unit_price"], s["order_ref"]) == (30.0, 0.22, 1499.78, 50.0, "SSO100")
    assert p.realized[0]["acquired"] == date(2029, 2, 15) and p.realized[0]["gain"] == 499.78
    assert p.dividends[0]["amount"] == 125.44 and p.dividends[0]["holding"] == 448.0
    amounts = sorted(c["amount"] for c in p.cash)
    assert amounts == [-1400.0, -18.82]
    assert p.snapshots[-1]["quantity"] == 448.0 and p.snapshots[-1]["cash"] == 106.62
    assert not p.warnings


def test_rows_link_sale_to_trade_date():
    rows = rows_from_parsed(parse_statement_text(STATEMENT, "s.pdf"))
    assert rows["shares_sales"][0]["trade_date"] == "2030-03-06"
    assert rows["shares_lot_costs"] == [{"acquired_date": "2029-02-15", "unit_cost": 33.3333}]


def test_year_rollover():
    text = "XX   December 31, 2029 - March 31, 2030\n" + STATEMENT.split("\n", 1)[1].replace(
        "02/20       BANK", "12/31       BANK")
    p = parse_statement_text(text)
    assert p.vests[0]["posted"] == date(2029, 12, 31)


def test_trade_confirm():
    text = """TRADE CONFIRMATION                                                         Date: November 03, 2030
SOLD        BANK OF AMERICA CORP
          Quantity             71            Price      49.500000     Amount                3514.50     Trade Date    11/03/30
          Processing Fee                                                                                Settle Date   11/04/30
          Transaction Fee                                                                       0.07    Symbol        BAC
          NET AMOUNT                                                                       3514.43      FA #          1
          SSO999 ORDERS MAY BE ROUTED"""
    s = parse_trade_confirm_text(text).sales[0]
    assert (s["trade_date"], s["quantity"], s["fees"], s["net"], s["order_ref"]) == (
        date(2030, 11, 3), 71.0, 0.07, 3514.43, "SSO999")


def test_schedule_matches_late_release_in_order():
    import pandas as pd
    from share_vesting.calc import schedule
    grants = pd.DataFrame([dict(id=1, grant_date="2021-03-01", award_type="profit_share", total_shares=375)])
    sched = pd.DataFrame([dict(grant_id=1, vest_date=f"{y}-03-01", gross_shares=g)
                          for y, g in [(2022, 93), (2023, 94), (2024, 94), (2025, 94)]])
    vests = pd.DataFrame([
        dict(id=1, grant_date="2021-03-01", lapse_date="2023-03-01", posted_date="2023-03-20", net_shares=44),
        dict(id=2, grant_date="2021-03-01", lapse_date="2023-03-01", posted_date="2023-08-14", net_shares=45),
        dict(id=3, grant_date="2021-03-01", lapse_date="2024-03-01", posted_date="2024-03-18", net_shares=45),
        dict(id=4, grant_date="2021-03-01", lapse_date="2025-03-01", posted_date="2025-03-18", net_shares=45)])
    out = schedule(grants, sched, vests, 0.48)
    assert list(out["status"]) == ["vested"] * 4
    assert list(out["delivered"]) == [44, 45, 45, 45]
    assert list(out["delayed"]) == [True, True, False, False]
