"""Tiny in-memory stand-in for the supabase-py query builder used by the app."""
from __future__ import annotations

import copy
import itertools


class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload = db, table, "select", None
        self.filters, self.conflict, self.rng = [], None, None

    def select(self, *_):
        self.op = "select"; return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows if isinstance(rows, list) else [rows]; return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.payload = "upsert", rows if isinstance(rows, list) else [rows]
        self.conflict = on_conflict; return self

    def update(self, values):
        self.op, self.payload = "update", values; return self

    def delete(self):
        self.op = "delete"; return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val); return self

    def in_(self, col, vals):
        vals = set(vals); self.filters.append(lambda r: r.get(col) in vals); return self

    def range(self, a, b):
        self.rng = (a, b); return self

    def limit(self, n):
        self.rng = (0, n - 1); return self

    def _match(self, r):
        return all(f(r) for f in self.filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        pk = self.db.pk.get(self.table, "id")
        if self.op == "select":
            out = [copy.deepcopy(r) for r in rows if self._match(r)]
            if self.rng:
                out = out[self.rng[0]:self.rng[1] + 1]
            return _Res(out)
        if self.op == "insert":
            new = []
            for r in self.payload:
                r = dict(r)
                if pk == "id":
                    r.setdefault("id", next(self.db.ids))
                rows.append(r); new.append(r)
            return _Res(copy.deepcopy(new))
        if self.op == "upsert":
            key = self.conflict or pk
            for r in self.payload:
                hit = next((x for x in rows if x.get(key) == r.get(key)), None)
                if hit:
                    hit.update(r)
                else:
                    r = dict(r)
                    if pk == "id":
                        r.setdefault("id", next(self.db.ids))
                    rows.append(r)
            return _Res(self.payload)
        if self.op == "update":
            for r in rows:
                if self._match(r):
                    r.update(self.payload)
            return _Res([])
        if self.op == "delete":
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            return _Res([])


class FakeClient:
    def __init__(self):
        self.tables = {}
        self.ids = itertools.count(1)
        self.pk = {"shares_statements": "period_end", "shares_prices": "price_date",
                   "shares_lot_costs": "acquired_date", "shares_documents": "file_hash",
                   "shares_settings": "key"}

    def table(self, name):
        return _Query(self, name)
