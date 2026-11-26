"""Parse Merrill (benefits.ml.com) BAC statements and trade confirmations.

Works on the text produced by ``pdftotext -layout``. A single PDF may hold
one quarterly statement or a whole year of them concatenated, plus the
"Equity Cost Basis" lot pages. Every page carries its statement period in
its header, so each line is dated against the period of its own page.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import date, datetime

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

PERIOD_RE = re.compile(r"([A-Z][a-z]+) (\d{1,2}), (\d{4}) - ([A-Z][a-z]+) (\d{1,2}), (\d{4})")
NUM = r"\(?-?[\d,]*\.\d+\)?"


def num(s: str | None) -> float | None:
    if s is None or s.strip() in ("", "-"):
        return None
    s = s.strip()
    neg = s.startswith("(") or s.startswith("-")
    v = float(s.strip("()-").replace(",", ""))
    return -v if neg else v


def mdy(s: str) -> date:
    fmt = "%m/%d/%Y" if len(s.split("/")[-1]) == 4 else "%m/%d/%y"
    return datetime.strptime(s, fmt).date()


@dataclass
class Period:
    start: date
    end: date

    def resolve(self, mmdd: str) -> date:
        m, d = (int(x) for x in mmdd.split("/"))
        year = self.end.year if m <= self.end.month else self.start.year
        return date(year, m, d)


@dataclass
class Parsed:
    source: str
    kind: str = "unknown"            # statement | trade_confirm
    periods: list = field(default_factory=list)
    vests: list = field(default_factory=list)
    sales: list = field(default_factory=list)
    realized: list = field(default_factory=list)
    dividends: list = field(default_factory=list)
    cash: list = field(default_factory=list)
    snapshots: list = field(default_factory=list)
    lot_snapshots: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


def pdf_text(path: str) -> str:
    return subprocess.run(["pdftotext", "-layout", path, "-"], check=True,
                          capture_output=True, text=True).stdout


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------- statements
SECTIONS = {
    "SECURITIES YOU TRANSFERRED IN/OUT": "transfers",
    "SECURITY TRANSACTIONS": "security",
    "REALIZED GAINS/(LOSSES)": "realized",
    "DIVIDENDS/INTEREST INCOME TRANSACTIONS": "dividends",
    "CASH/OTHER TRANSACTIONS": "cash",
    "EQUITIES": "equities",
    "CASH/MONEY ACCOUNTS": "cashacct",
    "LONG PORTFOLIO": None,
    "Customer Service": None,
    "This page intentionally left blank": None,
}

VEST_RE = re.compile(rf"^\s*(\d\d/\d\d)\s+BANK OF AMERICA CORP\s+Transfer / Adjustment\s+(-?[\d,]+\.\d+)\s+({NUM})")
GRANT_RE = re.compile(r"GRANT DATE (\d\d/\d\d/\d{4}) LAPSE DATE (\d\d/\d\d/\d{4})")
SALE_RE = re.compile(rf"^\s*(\d\d/\d\d)\s+BANK OF AMERICA CORP\s+Sale\s+(-?[\d,]+\.\d+)\s+({NUM})(?:\s+({NUM}))?\s+({NUM})\s*$")
UNIT_RE = re.compile(r"^\s*(\S+)?.*UNIT PRICE\s+([\d.]+)")
REAL_RE = re.compile(rf"BANK OF AMERICA CORP\s+([\d,]+\.\d+)\s+(\d\d/\d\d/\d\d)\s+(\d\d/\d\d/\d\d)\s+({NUM})\s+({NUM})\s+({NUM})")
DIV_RE = re.compile(rf"^\s*(\d\d/\d\d)\s+(.+?)\s{{2,}}\*?\s*(Dividend|Interest|[A-Za-z ]*Interest[A-Za-z ]*)\s+({NUM})\s*$")
HOLDING_RE = re.compile(r"HOLDING ([\d,]+\.\d+) PAY DATE (\d\d/\d\d/\d{4})")
CASHLINE_RE = re.compile(rf"^\s*(\d\d/\d\d)\s+(.+?)\s{{2,}}([A-Z][A-Za-z /-]+?)\s{{2,}}({NUM})\s*$")
EQUITY_RE = re.compile(rf"BANK OF AMERICA CORP\s+BAC\s+([\d,]+\.\d+)\s+({NUM})\s+([\d.]+)\s+({NUM})")
CASHBAL_RE = re.compile(rf"^\s*CASH\s+({NUM})")
OPEN_RE = re.compile(rf"Opening Value \((\d\d/\d\d)\)\s+\$?({NUM})")
CLOSE_RE = re.compile(rf"Closing Value \((\d\d/\d\d)\)\s+\$?({NUM})")
LOT_RE = re.compile(rf"(?:^|\s)(\d\d/\d\d/\d\d)\s+([\d,]+(?:\.\d+)?)\s+([\d.]+)\s+({NUM})\s+([\d.]+)\s+({NUM})")


FOOTER_RE = re.compile(r"^\d{3}\s{5,}")
DEBIT_TYPES = ("Wire Transfer Out", "Non-Resident Alien Tax", "Withdrawal", "Fee", "Check")
CREDIT_TYPES = ("Wire Transfer In", "Funds Received", "Deposit")


def _cash_is_debit(description: str, txn_type: str, next_line: str) -> bool | None:
    """Merrill's debit/credit columns move between page layouts, so decide by type."""
    if txn_type.startswith(DEBIT_TYPES) or description.startswith("WIRE TRF OUT"):
        return True
    if txn_type.startswith(CREDIT_TYPES):
        return False
    if description.startswith("CSH CONV"):          # sale proceeds converted and paid out
        return True
    if "RSU ACTIVITY" in next_line and " CASH " in next_line:   # cash paid with a vest
        return False
    return None


def parse_statement_text(text: str, source: str = "") -> Parsed:
    out = Parsed(source=source, kind="statement")
    pages = text.split("\f")
    seen_periods: dict = {}
    for page in pages:
        lines = page.splitlines()
        period = None
        for ln in lines[:12]:
            m = PERIOD_RE.search(ln)
            if m:
                period = Period(date(int(m[3]), MONTHS[m[1]], int(m[2])),
                                date(int(m[6]), MONTHS[m[4]], int(m[5])))
                break
        if period is None:
            continue
        seen_periods.setdefault((period.start, period.end), {"start": period.start, "end": period.end})
        cost_basis_page = any("EQUITY COST BASIS" in ln for ln in lines[:12])
        section = None
        debit_col = credit_col = None
        pending = None   # last record awaiting its detail line
        for i, ln in enumerate(lines):
            s = ln.strip()
            for head, name in SECTIONS.items():
                if s.startswith(head):
                    section = name
                    pending = None
                    break
            if section == "cash" and "Debit" in ln and "Credit" in ln:
                debit_col = ln.index("Debit") + len("Debit")
                credit_col = ln.index("Credit") + len("Credit")
                continue

            m = OPEN_RE.search(ln)
            if m:
                seen_periods[(period.start, period.end)]["opening_value"] = num(m[2])
            m = CLOSE_RE.search(ln)
            if m:
                seen_periods[(period.start, period.end)]["closing_value"] = num(m[2])

            if cost_basis_page:
                m = LOT_RE.search(ln)
                if m:
                    out.lot_snapshots.append(dict(
                        as_of=period.end, acquired=mdy(m[1]), quantity=num(m[2]),
                        unit_cost=num(m[3]), cost_basis=num(m[4]),
                        market_price=num(m[5]), market_value=num(m[6]), origin="cost_basis"))
                continue

            if section == "transfers":
                m = VEST_RE.match(ln)
                if m:
                    pending = dict(posted=period.resolve(m[1]), quantity=num(m[2]),
                                   value=num(m[3]), grant_date=None, lapse_date=None,
                                   description="")
                    out.vests.append(pending)
                    continue
                if pending is not None:
                    g = GRANT_RE.search(ln)
                    if g:
                        pending["grant_date"] = mdy(g[1])
                        pending["lapse_date"] = mdy(g[2])
                        pending["description"] = s
                        pending = None
            elif section == "security":
                m = SALE_RE.match(ln)
                if m:
                    amount = num(m[3])
                    fees = abs(num(m[4])) if m[4] else 0.0
                    pending = dict(settle_date=period.resolve(m[1]), quantity=abs(num(m[2])),
                                   amount=amount, fees=fees, net=num(m[5]),
                                   unit_price=None, order_ref=None)
                    out.sales.append(pending)
                    continue
                if pending is not None and "UNIT PRICE" in ln:
                    u = UNIT_RE.match(ln)
                    pending["unit_price"] = float(u[2])
                    pending["order_ref"] = u[1]
                    pending = None
            elif section == "realized":
                m = REAL_RE.search(ln)
                if m:
                    out.realized.append(dict(
                        quantity=num(m[1]), acquired=mdy(m[2]), sold=mdy(m[3]),
                        proceeds=num(m[4]), cost_basis=num(m[5]), gain=num(m[6])))
            elif section == "dividends":
                m = DIV_RE.match(ln)
                if m:
                    pending = dict(date=period.resolve(m[1]), description=m[2].strip(),
                                   kind=m[3].strip(), amount=num(m[4]), holding=None)
                    out.dividends.append(pending)
                    continue
                if pending is not None:
                    h = HOLDING_RE.search(ln)
                    if h:
                        pending["holding"] = num(h[1])
                        pending = None
            elif section == "cash":
                m = CASHLINE_RE.match(ln)
                if m and "Subtotal" not in ln and "TOTAL" not in ln:
                    is_debit = _cash_is_debit(m[2].strip(), m[3].strip(), lines[i + 1] if i + 1 < len(lines) else "")
                    if is_debit is None:
                        end = m.end(4)
                        if debit_col and credit_col:
                            is_debit = abs(end - debit_col) < abs(end - credit_col)
                        else:
                            is_debit = True
                        out.warnings.append(f"cash direction guessed ({'debit' if is_debit else 'credit'}): {s}")
                    amt = abs(num(m[4]))
                    pending = dict(date=period.resolve(m[1]), description=m[2].strip(),
                                   type=m[3].strip(), amount=-amt if is_debit else amt,
                                   detail="")
                    out.cash.append(pending)
                    continue
                if pending is not None and s and s != "." and not s.startswith("+") \
                        and not FOOTER_RE.match(s):
                    pending["detail"] = s
                    pending = None
            elif section == "equities":
                m = LOT_RE.search(ln)
                if m:
                    out.lot_snapshots.append(dict(
                        as_of=period.end, acquired=mdy(m[1]), quantity=num(m[2]),
                        unit_cost=num(m[3]), cost_basis=num(m[4]),
                        market_price=num(m[5]), market_value=num(m[6]), origin="holdings"))
                    continue
                m = EQUITY_RE.search(ln)
                if m:
                    out.snapshots.append(dict(
                        as_of=period.end, quantity=num(m[1]), cost_basis=num(m[2]),
                        market_price=num(m[3]), market_value=num(m[4])))
            elif section == "cashacct":
                m = CASHBAL_RE.match(ln)
                if m:
                    out.snapshots.append(dict(as_of=period.end, cash=num(m[1])))

    out.periods = sorted(seen_periods.values(), key=lambda p: p["start"])
    # merge equity + cash snapshots per date
    merged: dict = {}
    for snap in out.snapshots:
        merged.setdefault(snap["as_of"], {"as_of": snap["as_of"]}).update(snap)
    # older statements list lots in the holdings section instead of a total
    cb_dates = {l["as_of"] for l in out.lot_snapshots if l["origin"] == "cost_basis"}
    out.lot_snapshots = [l for l in out.lot_snapshots
                         if l["origin"] == "cost_basis" or l["as_of"] not in cb_dates]
    for l in out.lot_snapshots:
        snap = merged.setdefault(l["as_of"], {"as_of": l["as_of"]})
        if "quantity" not in snap or snap.get("_from_lots"):
            snap["_from_lots"] = True
            snap["quantity"] = snap.get("quantity", 0) + l["quantity"]
            snap["cost_basis"] = round(snap.get("cost_basis", 0) + l["cost_basis"], 2)
            snap["market_price"] = l["market_price"]
            snap["market_value"] = round(snap.get("market_value", 0) + l["market_value"], 2)
    for snap in merged.values():
        snap.pop("_from_lots", None)
    out.snapshots = sorted(merged.values(), key=lambda s: s["as_of"])
    for v in out.vests:
        if v["grant_date"] is None:
            out.warnings.append(f"vest without grant line: {v}")
    for s in out.sales:
        if s["unit_price"] is None:
            out.warnings.append(f"sale without unit price: {s}")
    return out


# --------------------------------------------------------- trade confirmation
TC_RE = re.compile(r"(SOLD|BOUGHT)\s+BANK OF AMERICA CORP")
TC_LINE = re.compile(r"Quantity\s+([\d,]+)\s+Price\s+([\d.]+)\s+Amount\s+([\d,]+\.\d+)\s+Trade Date\s+(\d\d/\d\d/\d\d)")
TC_SETTLE = re.compile(r"Settle Date\s+(\d\d/\d\d/\d\d)")
TC_FEE = re.compile(r"(Transaction Fee|Processing Fee|Commission)\s+([\d,]+\.\d+)")
TC_NET = re.compile(r"NET AMOUNT\s+([\d,]+\.\d+)")
TC_REF = re.compile(r"\b(SSO\d+)")


def parse_trade_confirm_text(text: str, source: str = "") -> Parsed:
    out = Parsed(source=source, kind="trade_confirm")
    first = text.split("\f")[0]
    m = TC_LINE.search(first)
    if not m or not TC_RE.search(first):
        out.warnings.append("no trade found")
        return out
    fees = sum(num(f[2]) for f in TC_FEE.finditer(first))
    settle = TC_SETTLE.search(first)
    net = TC_NET.search(first)
    ref = TC_REF.search(first)
    out.sales.append(dict(
        trade_date=mdy(m[4]), settle_date=mdy(settle[1]) if settle else None,
        quantity=num(m[1] + ".0"), unit_price=float(m[2]), amount=num(m[3]),
        fees=round(fees, 2), net=num(net[1]) if net else None,
        order_ref=ref[1] if ref else None,
        side="sell" if TC_RE.search(first)[1] == "SOLD" else "buy"))
    return out


def parse_text(text: str, source: str = "") -> Parsed:
    if "TRADE CONFIRMATION" in text[:3000]:
        return parse_trade_confirm_text(text, source)
    return parse_statement_text(text, source)


def parse_pdf(path: str) -> Parsed:
    return parse_text(pdf_text(path), source=path)
