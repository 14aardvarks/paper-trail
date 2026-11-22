import csv
import datetime as dt
import glob
import io
import json
import os
import threading
import time

from flask import Flask, Response, redirect, render_template_string, request, url_for

import match_receipts as mr

DATA = os.environ.get("DATA_DIR", "/data")
EXPORTS = os.path.join(DATA, "exports")
RESULTS = os.path.join(DATA, "results.json")
PL_URL = os.environ.get("PAPERLESS_URL", "https://paperless.example.com").rstrip("/")
PL_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
AMOUNT_FIELD = os.environ.get("AMOUNT_FIELD", "amount")
# "tag|account;tag|account"
BUSINESSES = [b.split("|", 1) for b in os.environ.get("BUSINESSES", "").split(";") if "|" in b]

os.makedirs(EXPORTS, exist_ok=True)
if os.environ.get("TZ"):
    time.tzset()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
lock = threading.Lock()


def latest_export():
    files = sorted(glob.glob(os.path.join(EXPORTS, "*.csv")))
    return files[-1] if files else None


def run_all(year=None):
    path = latest_export()
    if not path:
        raise RuntimeError("No Actual export uploaded yet")
    if not PL_TOKEN or not BUSINESSES:
        raise RuntimeError("PAPERLESS_TOKEN and BUSINESSES must be set")
    year = year or dt.date.today().year
    pl = mr.Paperless(PL_URL, PL_TOKEN)
    out = {"ran_at": dt.datetime.now().isoformat(timespec="seconds"), "year": year,
           "export": os.path.basename(path), "businesses": []}
    newest = None
    for tag, acct in BUSINESSES:
        txns = mr.load_transactions(path, year, {acct.lower()}, False, False)
        docs = mr.load_receipts(pl, year, tag, AMOUNT_FIELD)
        res = mr.match(txns, docs, 60, 0)
        used = {v[0] for v in res.values()}
        for t in txns:
            newest = max(newest, t["date"]) if newest else t["date"]
        out["businesses"].append({
            "tag": tag, "account": acct, "matched": len(res), "docs": len(docs),
            "missing": [{"date": str(t["date"]), "amount": "%.2f" % (t["signed"] / 100),
                         "payee": t["payee"], "category": t["category"]}
                        for t in sorted(txns, key=lambda x: x["date"]) if t["id"] not in res],
            "orphans": [{"date": str(d["created"]), "title": d["title"],
                         "correspondent": d["correspondent"],
                         "amount": "" if d["cents"] is None else "%.2f" % (d["cents"] / 100),
                         "url": "%s/documents/%d/details" % (PL_URL, d["id"])}
                        for d in docs if d["id"] not in used],
        })
    out["newest_transaction"] = str(newest) if newest else ""
    with lock, open(RESULTS, "w") as f:
        json.dump(out, f)
    return out


def load_results():
    try:
        with open(RESULTS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper Trail</title>
<style>
:root{--bg:#fff;--fg:#1d2327;--mut:#5d6870;--line:#dde2e6;--card:#f6f8f9;--acc:#2f6f3e;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#121516;--fg:#e6eaec;--mut:#9aa6ad;--line:#2b3236;--card:#1a1f21;--acc:#6fbf80;--bad:#f2867f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
form.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button,input[type=file]{font:inherit}button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}
button.alt{background:transparent;color:var(--fg);border:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.bad{color:var(--bad)}.ok{color:var(--acc)}
.err{background:#f9dedc;color:#410e0b;padding:10px 12px;border-radius:8px;margin:12px 0}a{color:var(--acc)}
.scroll{overflow-x:auto}
</style></head><body><main>
<h1>Paper Trail</h1>
<div class="mut">Paperless receipts matched against your Actual export.</div>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
{% if msg %}<div class="card ok">{{ msg }}</div>{% endif %}
<div class="card">
<form class="row" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
<input type="file" name="file" accept=".csv" required>
<input type="number" name="year" value="{{ year }}" style="width:90px;font:inherit;padding:6px">
<button>Upload and check</button></form>
<form class="row" style="margin-top:8px" method="post" action="{{ url_for('rerun') }}">
<input type="hidden" name="year" value="{{ year }}"><button class="alt">Re-run on latest export</button></form>
</div>
{% if r %}
<div class="mut">Export {{ r.export }}, newest transaction {{ r.newest_transaction or 'unknown' }}. Checked {{ r.ran_at }}.</div>
{% for b in r.businesses %}
<h2>{{ b.tag }}</h2>
<div class="mut">{{ b.matched }} matched, <span class="{{ 'bad' if b.missing else 'ok' }}">{{ b.missing|length }} missing a receipt</span>, account {{ b.account }}</div>
{% if b.missing %}<div class="card scroll"><table><tr><th>Date</th><th>Payee</th><th class="n">Amount</th></tr>
{% for m in b.missing %}<tr><td>{{ m.date }}</td><td>{{ m.payee }}</td><td class="n">{{ m.amount }}</td></tr>{% endfor %}</table>
<div style="margin-top:8px"><a href="{{ url_for('download', i=loop.index0) }}">Download CSV</a></div></div>{% endif %}
{% if b.orphans %}<details class="card"><summary>{{ b.orphans|length }} receipts with no transaction</summary><div class="scroll"><table>
<tr><th>Date</th><th>Receipt</th><th class="n">Amount</th></tr>
{% for o in b.orphans %}<tr><td>{{ o.date }}</td><td><a href="{{ o.url }}">{{ o.title }}</a></td><td class="n">{{ o.amount }}</td></tr>{% endfor %}
</table></div></details>{% endif %}
{% endfor %}
{% else %}<div class="card mut">Nothing yet. Export from Actual (Settings, Export data, or an account's Export) and upload the CSV.</div>{% endif %}
</main></body></html>"""


def page(error=None, msg=None):
    r = load_results()
    return render_template_string(PAGE, r=r, error=error, msg=msg,
                                  year=(r or {}).get("year", dt.date.today().year))


def parse_year():
    try:
        return int(request.form.get("year", ""))
    except ValueError:
        return None


@app.get("/")
def index():
    return page(msg=request.args.get("msg"))


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".csv"):
        return page(error="Choose a .csv file exported from Actual."), 400
    head = f.stream.read(2048).decode("utf-8-sig", "ignore")
    f.stream.seek(0)
    if "Date" not in head.split("\n", 1)[0] or "Amount" not in head.split("\n", 1)[0]:
        return page(error="That doesn't look like an Actual export (missing Date and Amount columns)."), 400
    name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv"
    f.save(os.path.join(EXPORTS, name))
    for old in sorted(glob.glob(os.path.join(EXPORTS, "*.csv")))[:-12]:
        os.remove(old)
    try:
        run_all(parse_year())
    except Exception as e:
        return page(error="Check failed: %s" % e), 500
    return redirect(url_for("index", msg="Checked."))


@app.post("/rerun")
def rerun():
    try:
        run_all(parse_year())
    except Exception as e:
        return page(error="Check failed: %s" % e), 500
    return redirect(url_for("index", msg="Checked."))


@app.get("/download/<int:i>")
def download(i):
    r = load_results()
    if not r or i >= len(r["businesses"]):
        return redirect(url_for("index"))
    b = r["businesses"][i]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "amount", "payee", "category"])
    for m in b["missing"]:
        w.writerow([m["date"], m["amount"], m["payee"], m["category"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=missing-%s-%s.csv" % (b["tag"], r["year"])})


@app.get("/health")
def health():
    return "ok"
