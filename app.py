import csv
import datetime as dt
import glob
import io
import json
import os
import re
import threading
import time

from flask import Flask, Response, redirect, render_template_string, request, url_for

import match_receipts as mr

DATA = os.environ.get("DATA_DIR", "/data")
EXPORTS = os.path.join(DATA, "exports")
RESULTS = os.path.join(DATA, "results.json")
CONFIG = os.path.join(DATA, "config.json")
PL_URL = os.environ.get("PAPERLESS_URL", "https://paperless.example.com").rstrip("/")
PL_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
AMOUNT_FIELD = os.environ.get("AMOUNT_FIELD", "amount")

os.makedirs(EXPORTS, exist_ok=True)
if os.environ.get("TZ"):
    time.tzset()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
lock = threading.Lock()


# ---------- config and data helpers ----------

def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f).get("businesses", [])
    except (OSError, ValueError):
        return []


def save_config(items):
    with lock, open(CONFIG, "w") as f:
        json.dump({"businesses": items}, f, indent=2)


def latest_export():
    files = sorted(glob.glob(os.path.join(EXPORTS, "*.csv")))
    return files[-1] if files else None


def export_accounts():
    path = latest_export()
    if not path:
        return []
    names = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("Account"):
                names.add(r["Account"])
    return sorted(names)


def paperless_tags():
    try:
        pl = mr.Paperless(PL_URL, PL_TOKEN)
        return sorted(t["name"] for t in pl.paged("/api/tags/", {"page_size": 200}))
    except Exception:
        return []


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "business"


def run_all(year=None):
    path = latest_export()
    businesses = load_config()
    if not path:
        raise RuntimeError("No Actual export uploaded yet")
    if not businesses:
        raise RuntimeError("Add a business in Settings first")
    if not PL_TOKEN:
        raise RuntimeError("PAPERLESS_TOKEN is not set")
    year = year or dt.date.today().year
    pl = mr.Paperless(PL_URL, PL_TOKEN)
    out = {"ran_at": dt.datetime.now().isoformat(timespec="seconds"), "year": year,
           "export": os.path.basename(path), "businesses": []}
    newest = None
    for b in businesses:
        txns = mr.load_transactions(path, year, {b["account"].lower()}, False, False)
        docs = mr.load_receipts(pl, year, b["tag"], AMOUNT_FIELD)
        res = mr.match(txns, docs, 60, 0)
        used = {v[0] for v in res.values()}
        paid_personal = []
        extra = {a.strip().lower() for a in b.get("personal", "").split(";") if a.strip()}
        if extra:
            left = [d for d in docs if d["id"] not in used]
            ptx = mr.load_transactions(path, year, extra, False, False)
            pres = mr.match(ptx, left, 60, 0)
            by_doc = {d["id"]: d for d in left}
            for t in ptx:
                if t["id"] in pres:
                    d = by_doc[pres[t["id"]][0]]
                    used.add(d["id"])
                    paid_personal.append({"date": str(t["date"]), "amount": "%.2f" % (t["signed"] / 100),
                                          "payee": t["payee"], "title": d["title"],
                                          "url": "%s/documents/%d/details" % (PL_URL, d["id"])})
        for t in txns:
            newest = max(newest, t["date"]) if newest else t["date"]
        out["businesses"].append({
            "name": b["name"], "tag": b["tag"], "account": b["account"],
            "matched": len(res), "docs": len(docs), "personal": b.get("personal", ""),
            "paid_personal": paid_personal,
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


# ---------- pages ----------

CSS = """
:root{--bg:#fff;--fg:#1d2327;--mut:#5d6870;--line:#dde2e6;--card:#f6f8f9;--acc:#2f6f3e;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#121516;--fg:#e6eaec;--mut:#9aa6ad;--line:#2b3236;--card:#1a1f21;--acc:#6fbf80;--bad:#f2867f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}nav{display:flex;gap:14px;margin:6px 0 12px}
button,input,select{font:inherit}input,select{padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}
button.alt{background:transparent;color:var(--fg);border:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.bad{color:var(--bad)}.ok{color:var(--acc)}
.err{background:#f9dedc;color:#410e0b;padding:10px 12px;border-radius:8px;margin:12px 0}a{color:var(--acc)}
.scroll{overflow-x:auto}label{display:block;font-size:13px;color:var(--mut);margin:8px 0 2px}
"""

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper Trail</title>
<style>{{ css|safe }}</style></head><body><main>
<h1>Paper Trail</h1>
<nav><a href="{{ url_for('index') }}">Check</a><a href="{{ url_for('settings') }}">Settings</a></nav>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
{% if msg %}<div class="card ok">{{ msg }}</div>{% endif %}
"""

INDEX = HEAD + """
<div class="mut">Paperless receipts matched against your Actual export.</div>
<div class="card">
<form class="row" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
<input type="file" name="file" accept=".csv" required>
<input type="number" name="year" value="{{ year }}" style="width:90px">
<button>Upload and check</button></form>
<form class="row" style="margin-top:8px" method="post" action="{{ url_for('rerun') }}">
<input type="hidden" name="year" value="{{ year }}"><button class="alt">Re-run on latest export</button></form>
</div>
{% if not businesses %}<div class="card">No businesses yet. <a href="{{ url_for('settings') }}">Add one in Settings.</a></div>{% endif %}
{% if r %}
<div class="mut">Export {{ r.export }}, newest transaction {{ r.newest_transaction or 'unknown' }}. Checked {{ r.ran_at }}.</div>
{% for b in r.businesses %}
<h2>{{ b.name }}</h2>
<div class="mut">{{ b.matched }} matched, <span class="{{ 'bad' if b.missing else 'ok' }}">{{ b.missing|length }} missing a receipt</span>, tag {{ b.tag }}, account {{ b.account }}</div>
{% if b.missing %}<div class="card scroll"><table><tr><th>Date</th><th>Payee</th><th class="n">Amount</th></tr>
{% for m in b.missing %}<tr><td>{{ m.date }}</td><td>{{ m.payee }}</td><td class="n">{{ m.amount }}</td></tr>{% endfor %}</table>
<div style="margin-top:8px"><a href="{{ url_for('download', i=loop.index0) }}">Download CSV</a></div></div>{% endif %}
{% if b.paid_personal %}<div class="card scroll"><b>{{ b.paid_personal|length }} paid from a personal account</b> (reimburse or log as owner contribution)<table>
<tr><th>Date</th><th>Receipt</th><th class="n">Amount</th></tr>
{% for p in b.paid_personal %}<tr><td>{{ p.date }}</td><td><a href="{{ p.url }}">{{ p.title }}</a></td><td class="n">{{ p.amount }}</td></tr>{% endfor %}
</table></div>{% endif %}
{% if b.orphans %}<details class="card"><summary>{{ b.orphans|length }} receipts with no transaction</summary><div class="scroll"><table>
<tr><th>Date</th><th>Receipt</th><th class="n">Amount</th></tr>
{% for o in b.orphans %}<tr><td>{{ o.date }}</td><td><a href="{{ o.url }}">{{ o.title }}</a></td><td class="n">{{ o.amount }}</td></tr>{% endfor %}
</table></div></details>{% endif %}
{% endfor %}
{% elif businesses %}<div class="card mut">Nothing yet. Export from Actual and upload the CSV.</div>{% endif %}
</main></body></html>"""

SETTINGS = HEAD + """
<h2>Businesses</h2>
<div class="mut">Each business is a Paperless tag plus the Actual account that pays for it. Add as many as you need.</div>
{% if businesses %}<div class="card scroll"><table><tr><th>Name</th><th>Paperless tag</th><th>Actual account</th><th>Personal accounts</th><th></th></tr>
{% for b in businesses %}<tr><td>{{ b.name }}</td><td>{{ b.tag }}</td><td>{{ b.account }}</td><td>{{ b.personal }}</td>
<td><form method="post" action="{{ url_for('delete', i=loop.index0) }}"><button class="alt">Remove</button></form></td></tr>{% endfor %}
</table></div>{% endif %}
<form class="card" method="post" action="{{ url_for('add') }}">
<label>Name</label><input name="name" required placeholder="Rental property">
<label>Paperless tag</label><input name="tag" list="tags" required>
<datalist id="tags">{% for t in tags %}<option value="{{ t }}">{% endfor %}</datalist>
<label>Actual account</label><input name="account" list="accts" required>
<datalist id="accts">{% for a in accounts %}<option value="{{ a }}">{% endfor %}</datalist>
<label>Personal accounts that sometimes pay for it (optional, separate with ;)</label><input name="personal" list="accts">
<div style="margin-top:12px"><button>Add business</button></div>
{% if not accounts %}<div class="mut" style="margin-top:8px">Upload an Actual export first and your account names will show up as suggestions.</div>{% endif %}
</form>
</main></body></html>"""


def page(template, error=None, msg=None, **kw):
    r = load_results()
    return render_template_string(template, css=CSS, r=r, error=error, msg=msg,
                                  businesses=load_config(),
                                  year=(r or {}).get("year", dt.date.today().year), **kw)


def parse_year():
    try:
        return int(request.form.get("year", ""))
    except ValueError:
        return None


@app.get("/")
def index():
    return page(INDEX, msg=request.args.get("msg"))


@app.get("/settings")
def settings():
    return page(SETTINGS, msg=request.args.get("msg"), tags=paperless_tags(), accounts=export_accounts())


@app.post("/settings/add")
def add():
    name = request.form.get("name", "").strip()
    tag = request.form.get("tag", "").strip()
    account = request.form.get("account", "").strip()
    if not (name and tag and account):
        return page(SETTINGS, error="Fill in all three fields.", tags=paperless_tags(), accounts=export_accounts()), 400
    items = load_config()
    items.append({"name": name, "tag": tag, "account": account,
                  "personal": request.form.get("personal", "").strip()})
    save_config(items)
    return redirect(url_for("settings", msg="Added %s." % name))


@app.post("/settings/delete/<int:i>")
def delete(i):
    items = load_config()
    if 0 <= i < len(items):
        items.pop(i)
        save_config(items)
    return redirect(url_for("settings", msg="Removed."))


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".csv"):
        return page(INDEX, error="Choose a .csv file exported from Actual."), 400
    first = f.stream.read(2048).decode("utf-8-sig", "ignore").split("\n", 1)[0]
    f.stream.seek(0)
    if "Date" not in first or "Amount" not in first:
        return page(INDEX, error="That doesn't look like an Actual export (missing Date and Amount columns)."), 400
    name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv"
    f.save(os.path.join(EXPORTS, name))
    for old in sorted(glob.glob(os.path.join(EXPORTS, "*.csv")))[:-12]:
        os.remove(old)
    if not load_config():
        return redirect(url_for("settings", msg="Export saved. Add a business to run the check."))
    try:
        run_all(parse_year())
    except Exception as e:
        return page(INDEX, error="Check failed: %s" % e), 500
    return redirect(url_for("index", msg="Checked."))


@app.post("/rerun")
def rerun():
    try:
        run_all(parse_year())
    except Exception as e:
        return page(INDEX, error="Check failed: %s" % e), 500
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
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=missing-%s-%s.csv" % (slug(b["name"]), r["year"])})


@app.get("/health")
def health():
    return "ok"
