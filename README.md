# Share Vesting

A Streamlit app that tracks BAC share awards: grants, vesting, sales, cost basis,
dividends and cash. It replaces the `Share_Vesting_Modelling.xlsx` workbook.

## How it works

- **Grants** are entered on the Grants page (statements only show net shares delivered).
  Each grant has yearly gross tranches, so you can see the full vesting schedule.
- **Statements**: upload Merrill quarterly or annual PDFs on the Import page. The parser reads
  - vests (grant date, lapse date, net shares, value)
  - sales and the lots each sale used, with cost basis
  - dividends, US tax withheld and other cash lines
  - quarter-end share count and cash balance.
  Re-importing a period replaces it, so overlapping or duplicate uploads are safe. PDFs are not stored.
- **Trade confirmations** add a sale straight away. When the quarterly statement arrives it updates
  that same sale (matched on Merrill's order reference) and adds the lot detail.
- **Reconcile** checks the calculated share count and cash balance against every statement.
- **Prices**: the live BAC price comes from Yahoo Finance and EUR/USD from the ECB (via frankfurter.app).
  Use Import → *Refresh price & FX history* to store daily history (needed for the EUR and CGT views).

## Setup

1. Tables live in the Supabase project as `public.shares_*` with row-level security on and no
   public policies, so only the service-role key can read them.
2. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` (or paste into the
   Streamlit Cloud secrets box) and fill in the values.
3. `pip install -r requirements.txt` and install `poppler-utils` (`pdftotext`). On Streamlit Cloud
   `packages.txt` does this.
4. `streamlit run app.py`

## Tests

```
pip install -r requirements-dev.txt
pytest
```
