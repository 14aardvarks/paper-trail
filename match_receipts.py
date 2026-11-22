#!/usr/bin/env python3
"""Match Paperless receipts to Actual Budget transactions.

Reads an Actual "Export data" zip (or db.sqlite) and the Paperless API.
Writes matched.csv, unmatched_transactions.csv and unmatched_receipts.csv.
Standard library only.

Example
  export PAPERLESS_TOKEN=xxxx
  python3 match_receipts.py --actual budget.zip --year 2026 \
      --tag arthur-property-management --account "Business Checking" --outdir out
"""
import argparse
import csv
import datetime as dt
import difflib
import os
import re
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from email.message import Message
import json


# ---------- Actual ----------

def open_actual(path):
    if path.lower().endswith(".zip"):
        tmp = tempfile.mkdtemp()
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith("db.sqlite")]
            if not names:
                sys.exit("No db.sqlite found in the export zip")
            z.extract(names[0], tmp)
            path = os.path.join(tmp, names[0])
    return sqlite3.connect(path)


TRANSFER_RE = re.compile(r"transfer|credit card payment|starting balance|checking account", re.I)


def load_transactions_csv(path, year, accounts, include_income, keep_transfers):
    """Actual's CSV export. It has no transaction ids, so it cannot feed push_notes.js."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, r in enumerate(csv.DictReader(f)):
            try:
                date = dt.date.fromisoformat(r["Date"][:10])
                amt = int(round(float(r["Amount"]) * 100))
            except (KeyError, ValueError):
                continue
            if year and date.year != year:
                continue
            if accounts and r.get("Account", "").lower() not in accounts:
                continue
            blob = " ".join([r.get("Payee", ""), r.get("Category", ""), r.get("Notes", "")])
            if not keep_transfers and TRANSFER_RE.search(blob):
                continue
            if amt >= 0 and not include_income:
                continue
            out.append({
                "id": "csv-%d" % i, "date": date, "cents": abs(amt), "signed": amt,
                "payee": r.get("Payee", ""), "category": r.get("Category", ""),
                "account": r.get("Account", ""), "notes": r.get("Notes", ""),
            })
    return out


def load_transactions(path, year, accounts, include_income, keep_transfers=False):
    if path.lower().endswith(".csv"):
        return load_transactions_csv(path, year, accounts, include_income, keep_transfers)
    con = open_actual(path)
    base = """
        SELECT t.id, t.date, t.amount, t.notes, p.name, c.name, a.name{extra}
        FROM transactions t
        LEFT JOIN payees p ON p.id = t.description
        LEFT JOIN categories c ON c.id = t.category
        LEFT JOIN accounts a ON a.id = t.acct
        WHERE COALESCE(t.tombstone, 0) = 0
          AND COALESCE(t.isChild, 0) = 0
          AND COALESCE(a.tombstone, 0) = 0
    """
    try:
        rows = con.execute(base.format(extra=", p.transfer_acct")).fetchall()
    except sqlite3.OperationalError:
        rows = [r + (None,) for r in con.execute(base.format(extra="")).fetchall()]
    out = []
    for tid, d, amt, notes, payee, cat, acct, transfer in rows:
        if d is None or amt is None:
            continue
        s = str(d)
        date = dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if year and date.year != year:
            continue
        if accounts and (acct or "").lower() not in accounts:
            continue
        if transfer:
            continue
        if amt >= 0 and not include_income:
            continue
        out.append({
            "id": tid, "date": date, "cents": abs(amt), "signed": amt,
            "payee": payee or "", "category": cat or "", "account": acct or "",
            "notes": notes or "",
        })
    return out


# ---------- Paperless ----------

class Paperless:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, url):
        req = urllib.request.Request(url, headers={
            "Authorization": "Token " + self.token,
            "Accept": "application/json",
        })
        return urllib.request.urlopen(req, timeout=60)

    def get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        with self._req(url) as r:
            return json.load(r)

    def paged(self, path, params):
        data = self.get(path, params)
        results = list(data.get("results", []))
        while data.get("next"):
            p = urllib.parse.urlparse(data["next"])
            url = self.base + p.path + ("?" + p.query if p.query else "")
            with self._req(url) as r:
                data = json.load(r)
            results.extend(data.get("results", []))
        return results

    def download(self, doc_id):
        url = "%s/api/documents/%d/download/?original=true" % (self.base, doc_id)
        with self._req(url) as r:
            body = r.read()
            cd = r.headers.get("Content-Disposition", "")
        m = Message()
        m["content-disposition"] = cd
        name = m.get_filename() or ("%d.pdf" % doc_id)
        return body, name


def money_to_cents(v):
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return int(round(float(m.group()) * 100)) if m else None


MONEY_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?!\d)")


def amounts_in_text(text):
    found = set()
    for whole, cents in MONEY_RE.findall(text or ""):
        found.add(int(whole.replace(",", "")) * 100 + int(cents))
    return found


def norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def load_receipts(pl, year, tag, amount_field):
    params = {"page_size": 100, "ordering": "created",
              "fields": "id,title,correspondent,created,custom_fields,content,original_file_name"}
    if year:
        params["created__date__gte"] = "%d-01-01" % year
        params["created__date__lte"] = "%d-12-31" % year
    if tag:
        t = pl.get("/api/tags/", {"name__iexact": tag}).get("results", [])
        if not t:
            sys.exit("Tag not found in Paperless: " + tag)
        params["tags__id__all"] = t[0]["id"]
    fid = None
    cf = pl.get("/api/custom_fields/", {"name__iexact": amount_field}).get("results", [])
    if cf:
        fid = cf[0]["id"]
    corr = {c["id"]: c["name"] for c in pl.paged("/api/correspondents/", {"page_size": 1000})}
    docs = []
    for d in pl.paged("/api/documents/", params):
        created = dt.date.fromisoformat(str(d["created"])[:10])
        cents = None
        for f in d.get("custom_fields") or []:
            if f.get("field") == fid:
                cents = money_to_cents(f.get("value"))
        docs.append({
            "id": d["id"], "title": d.get("title") or "", "created": created,
            "correspondent": corr.get(d.get("correspondent"), ""),
            "cents": cents, "content": d.get("content") or "",
            "amounts": amounts_in_text(d.get("content")),
        })
    return docs


# ---------- Matching ----------

def score(txn, doc, tolerance):
    # amount
    if doc["cents"] is not None and abs(doc["cents"] - txn["cents"]) <= tolerance:
        a = 50
    elif txn["cents"] in doc["amounts"]:
        a = 40
    else:
        return 0
    # date, bank posting can lag a few days
    days = abs((txn["date"] - doc["created"]).days)
    d = 30 * max(0.0, 1 - days / 7.0)
    # name
    payee = norm(txn["payee"])
    n = 0.0
    if payee:
        names = norm(doc["correspondent"] + " " + doc["title"])
        n = difflib.SequenceMatcher(None, payee, norm(doc["correspondent"])).ratio()
        toks = [w for w in payee.split() if len(w) >= 4]
        hay = names + " " + norm(doc["content"][:3000])
        if toks:
            n = max(n, sum(1 for w in toks if w in hay) / len(toks))
    return a + d + 20 * n


def match(txns, docs, threshold, tolerance):
    pairs = []
    for t in txns:
        for d in docs:
            s = score(t, d, tolerance)
            if s >= threshold:
                pairs.append((s, t["id"], d["id"]))
    pairs.sort(reverse=True)
    used_t, used_d, result = set(), set(), {}
    for s, tid, did in pairs:
        if tid in used_t or did in used_d:
            continue
        used_t.add(tid)
        used_d.add(did)
        result[tid] = (did, round(s, 1))
    return result


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "file"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--actual", required=True, help="Actual export zip, db.sqlite, or a CSV export")
    ap.add_argument("--paperless-url", default=os.environ.get("PAPERLESS_URL", "https://paperless.example.com"))
    ap.add_argument("--year", type=int)
    ap.add_argument("--tag", help="Only receipts with this Paperless tag")
    ap.add_argument("--account", action="append", help="Only this Actual account (repeatable)")
    ap.add_argument("--amount-field", default="amount")
    ap.add_argument("--threshold", type=float, default=60)
    ap.add_argument("--tolerance", type=int, default=0, help="Amount tolerance in cents")
    ap.add_argument("--include-income", action="store_true")
    ap.add_argument("--keep-transfers", action="store_true", help="CSV only: do not skip transfers")
    ap.add_argument("--export", action="store_true", help="Copy matched originals into folders by Actual category")
    ap.add_argument("--outdir", default="out")
    a = ap.parse_args()

    token = os.environ.get("PAPERLESS_TOKEN")
    if not token:
        sys.exit("Set PAPERLESS_TOKEN first")
    accounts = {x.lower() for x in a.account} if a.account else None

    txns = load_transactions(a.actual, a.year, accounts, a.include_income, a.keep_transfers)
    pl = Paperless(a.paperless_url, token)
    docs = load_receipts(pl, a.year, a.tag, a.amount_field)
    res = match(txns, docs, a.threshold, a.tolerance)
    by_doc = {d["id"]: d for d in docs}
    link = a.paperless_url.rstrip("/") + "/documents/%d/details"

    os.makedirs(a.outdir, exist_ok=True)
    with open(os.path.join(a.outdir, "matched.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["transaction_id", "date", "amount", "payee", "category", "account",
                    "document_id", "document_title", "document_url", "score", "existing_notes"])
        for t in txns:
            if t["id"] in res:
                did, s = res[t["id"]]
                w.writerow([t["id"], t["date"], "%.2f" % (t["signed"] / 100), t["payee"], t["category"],
                            t["account"], did, by_doc[did]["title"], link % did, s, t["notes"]])
    un_t = [t for t in txns if t["id"] not in res]
    with open(os.path.join(a.outdir, "unmatched_transactions.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "amount", "payee", "category", "account", "notes"])
        for t in sorted(un_t, key=lambda x: x["date"]):
            w.writerow([t["date"], "%.2f" % (t["signed"] / 100), t["payee"], t["category"], t["account"], t["notes"]])
    matched_docs = {v[0] for v in res.values()}
    un_d = [d for d in docs if d["id"] not in matched_docs]
    with open(os.path.join(a.outdir, "unmatched_receipts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["document_id", "created", "correspondent", "title", "amount", "url"])
        for d in un_d:
            w.writerow([d["id"], d["created"], d["correspondent"], d["title"],
                        "" if d["cents"] is None else "%.2f" % (d["cents"] / 100), link % d["id"]])

    if a.export:
        for t in txns:
            if t["id"] in res:
                did = res[t["id"]][0]
                body, name = pl.download(did)
                folder = os.path.join(a.outdir, "receipts", slug(t["category"] or "Uncategorized"))
                os.makedirs(folder, exist_ok=True)
                fn = "%s_%s_%.2f_%s" % (t["date"], slug(t["payee"] or "unknown"), t["cents"] / 100, slug(name))
                with open(os.path.join(folder, fn), "wb") as out:
                    out.write(body)

    print("Transactions checked : %d" % len(txns))
    print("Receipts checked     : %d" % len(docs))
    print("Matched              : %d" % len(res))
    print("Transactions with no receipt : %d  (unmatched_transactions.csv)" % len(un_t))
    print("Receipts with no transaction : %d  (unmatched_receipts.csv)" % len(un_d))
    print("Output in %s/" % a.outdir)


if __name__ == "__main__":
    main()
