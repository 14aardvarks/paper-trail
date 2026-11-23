import csv
import datetime as dt
import glob
import io
import json
import os
import re
import tempfile
import threading
import zipfile
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
        raise RuntimeError("No bank export uploaded yet")
    if not businesses:
        raise RuntimeError("Add an account in Settings first")
    if not PL_TOKEN:
        raise RuntimeError("PAPERLESS_TOKEN is not set")
    year = year or dt.date.today().year
    pl = mr.Paperless(PL_URL, PL_TOKEN)
    out = {"ran_at": dt.datetime.now().isoformat(timespec="seconds"), "year": year,
           "export": os.path.basename(path), "businesses": []}
    newest = None
    for b in businesses:
        accts = {a.strip().lower() for a in b["account"].split(";") if a.strip()}
        txns = mr.load_transactions(path, year, accts, False, False)
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
        byid = {d["id"]: d for d in docs}
        rows = []
        for t in txns:
            r = {"date": str(t["date"]), "payee": t["payee"], "amount": "%.2f" % (t["signed"] / 100),
                 "category": t["category"], "status": "missing", "title": "", "url": ""}
            if t["id"] in res:
                d = byid[res[t["id"]][0]]
                r.update(status="ok", title=d["title"], url="%s/documents/%d/details" % (PL_URL, d["id"]))
            rows.append(r)
        for p in paid_personal:
            rows.append({"date": p["date"], "payee": p["payee"], "amount": p["amount"], "category": "",
                         "status": "personal", "title": p["title"], "url": p["url"]})
        rows.sort(key=lambda x: x["date"])
        out["businesses"].append({
            "name": b["name"], "tag": b["tag"], "account": b["account"],
            "matched": len(res), "docs": len(docs), "personal": b.get("personal", ""),
            "paid_personal": paid_personal, "rows": rows,
            "missing": [r for r in rows if r["status"] == "missing"],
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
.scroll{overflow-x:auto}button.on{background:var(--acc);color:#fff;border-color:var(--acc)}label{display:block;font-size:13px;color:var(--mut);margin:8px 0 2px}
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
<div class="mut">Paperless receipts matched against your bank export.</div>
<div class="card">
<form class="row" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
<input type="file" name="file" accept=".csv" required>
<input type="number" name="year" value="{{ year }}" style="width:90px">
<button>Upload and check</button></form>
<form class="row" style="margin-top:8px" method="post" action="{{ url_for('rerun') }}">
<input type="hidden" name="year" value="{{ year }}"><button class="alt">Re-run on latest export</button></form>
</div>
{% if not businesses %}<div class="card">No accounts yet. <a href="{{ url_for('settings') }}">Add one in Settings.</a></div>{% endif %}
{% if r %}
<div class="mut">Export {{ r.export }}, newest transaction {{ r.newest_transaction or 'unknown' }}. Checked {{ r.ran_at }}.</div>
<div class="row" style="margin:14px 0 0"><label style="margin:0">Viewing</label>
<select id="biz">{% for b in r.businesses %}<option value="{{ loop.index0 }}">{{ b.name }}</option>{% endfor %}</select></div>
{% for b in r.businesses %}{% set bi = loop.index0 %}
<section data-sec="{{ bi }}">
<h2>{{ b.name }}</h2>
<div class="mut">{{ b.rows|selectattr('status','equalto','ok')|list|length }} with receipt, <span class="{{ 'bad' if b.missing else 'ok' }}">{{ b.missing|length }} missing</span>, {{ b.paid_personal|length }} paid personally</div>
<div class="card scroll" data-biz>
<div class="row" style="margin-bottom:8px">
<button type="button" class="alt f on" data-f="all">All</button>
<button type="button" class="alt f" data-f="missing">Missing</button>
<button type="button" class="alt f" data-f="ok">Has receipt</button>
<button type="button" class="alt f" data-f="personal">Paid personally</button>
<input class="q" placeholder="Search" style="margin-left:auto;width:150px">
<a href="{{ url_for('download', i=bi) }}">CSV</a></div>
<form class="row" method="get" action="{{ url_for('export_docs') }}" style="margin-bottom:8px">
<input type="hidden" name="i" value="{{ bi }}">
<span class="mut">Download documents</span>
<input type="date" name="start" value="{{ r.year }}-01-01" required>
<input type="date" name="end" value="{{ r.year }}-12-31" required>
<button class="alt">Zip</button></form>
<table><tr><th style="width:36px"></th><th>Date</th><th>Payee</th><th>Receipt</th><th class="n">Amount</th></tr>
{% for m in b.rows %}<tr data-s="{{ m.status }}"><td>{% if m.status == 'ok' %}<span class="ok" title="Has receipt">&#10003;</span>{% elif m.status == 'personal' %}<span title="Paid from a personal account" style="color:#d9a400">&#9888;</span>{% else %}<span class="bad" title="Missing receipt">&#10007;</span>{% endif %}</td>
<td>{{ m.date }}</td><td>{{ m.payee }}</td><td>{% if m.url %}<a href="{{ m.url }}">{{ m.title }}</a>{% endif %}</td><td class="n">{{ m.amount }}</td></tr>{% endfor %}
</table></div>
{% if b.orphans %}<details class="card"><summary>{{ b.orphans|length }} receipts with no transaction</summary><div class="scroll"><table>
<tr><th>Date</th><th>Receipt</th><th class="n">Amount</th></tr>
{% for o in b.orphans %}<tr><td>{{ o.date }}</td><td><a href="{{ o.url }}">{{ o.title }}</a></td><td class="n">{{ o.amount }}</td></tr>{% endfor %}
</table></div></details>{% endif %}
</section>
{% endfor %}
{% elif businesses %}<div class="card mut">Nothing yet. Export your transactions and upload the CSV.</div>{% endif %}
<script>
(function(){var sel=document.getElementById('biz');if(!sel)return;
  function show(){document.querySelectorAll('[data-sec]').forEach(function(x){x.style.display=x.dataset.sec===sel.value?'':'none';});
    try{localStorage.setItem('pt-biz',sel.value);}catch(e){}}
  try{var v=localStorage.getItem('pt-biz');if(v&&sel.querySelector('option[value="'+v+'"]'))sel.value=v;}catch(e){}
  sel.onchange=show;show();})();
document.querySelectorAll('[data-biz]').forEach(function(box){
  var f='all', q='';
  function apply(){box.querySelectorAll('tr[data-s]').forEach(function(tr){
    var ok=(f==='all'||tr.dataset.s===f)&&(!q||tr.textContent.toLowerCase().indexOf(q)>-1);
    tr.style.display=ok?'':'none';});}
  box.querySelectorAll('.f').forEach(function(b){b.onclick=function(){
    f=b.dataset.f;box.querySelectorAll('.f').forEach(function(x){x.classList.toggle('on',x===b);});apply();};});
  box.querySelector('.q').oninput=function(e){q=e.target.value.toLowerCase();apply();};
});
</script>
</main></body></html>"""

SETTINGS = HEAD + """
<h2>Accounts</h2>
<div class="mut">Each account here is a group you review on the Check page: a name, the Paperless tag its receipts carry, and the bank accounts whose transactions belong to it.</div>
{% if businesses %}<div class="card scroll"><table><tr><th>Name</th><th>Paperless tag</th><th>Bank accounts</th><th>Also checked</th><th></th></tr>
{% for b in businesses %}<tr><td>{{ b.name }}</td><td>{{ b.tag }}</td><td>{{ b.account.replace(';', ', ') }}</td><td>{{ b.personal.replace(';', ', ') }}</td>
<td style="white-space:nowrap"><a href="{{ url_for('settings', edit=loop.index0) }}">Edit</a>
<form method="post" action="{{ url_for('delete', i=loop.index0) }}" style="display:inline" onsubmit="return confirm('Remove this account?')"><button class="alt">Remove</button></form></td></tr>{% endfor %}
</table></div>{% endif %}
<h2>{{ 'Edit account' if e else 'Add account' }}</h2>
<form class="card" method="post" action="{{ url_for('save', i=edit) if e else url_for('add') }}">
<label>Name</label><input name="name" required value="{{ e.name if e else '' }}" placeholder="Rental property">
<label>Paperless tag on its receipts</label><input name="tag" list="tags" required value="{{ e.tag if e else '' }}">
<datalist id="tags">{% for t in tags %}<option value="{{ t }}">{% endfor %}</datalist>
{% if accounts %}
<label>Bank accounts for this one (tick every one whose transactions should have receipts, such as checking and savings)</label>
{% for a in accounts %}<div><label style="display:inline;color:var(--fg)"><input type="checkbox" name="account" value="{{ a }}" {{ 'checked' if e and a in e.account.split(';') }}> {{ a }}</label></div>{% endfor %}
<label>Also look for receipts in these accounts (optional, for things you paid from a personal account)</label>
{% for a in accounts %}<div><label style="display:inline;color:var(--fg)"><input type="checkbox" name="personal" value="{{ a }}" {{ 'checked' if e and a in e.personal.split(';') }}> {{ a }}</label></div>{% endfor %}
{% else %}
<label>Bank accounts (separate with ;)</label><input name="account" required value="{{ e.account if e else '' }}" placeholder="Checking;Savings">
<label>Also look for receipts in these accounts (optional, separate with ;)</label><input name="personal" value="{{ e.personal if e else '' }}">
<div class="mut" style="margin-top:8px">Upload your bank export on the Check page and your bank accounts will show up as tickboxes.</div>
{% endif %}
<div style="margin-top:12px" class="row"><button>{{ 'Save' if e else 'Add account' }}</button>
{% if e %}<a href="{{ url_for('settings') }}">Cancel</a>{% endif %}</div>
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


def form_entry():
    return {"name": request.form.get("name", "").strip(), "tag": request.form.get("tag", "").strip(),
            "account": ";".join(x.strip() for x in request.form.getlist("account") if x.strip()),
            "personal": ";".join(x.strip() for x in request.form.getlist("personal") if x.strip())}


@app.get("/settings")
def settings():
    items = load_config()
    edit = request.args.get("edit", type=int)
    e = items[edit] if edit is not None and 0 <= edit < len(items) else None
    return page(SETTINGS, msg=request.args.get("msg"), tags=paperless_tags(), accounts=export_accounts(),
                e=e, edit=edit if e else None)


@app.post("/settings/add")
def add():
    e = form_entry()
    if not (e["name"] and e["tag"] and e["account"]):
        return page(SETTINGS, error="Fill in the name, tag and at least one bank account.",
                    tags=paperless_tags(), accounts=export_accounts(), e=None, edit=None), 400
    items = load_config()
    items.append(e)
    save_config(items)
    return redirect(url_for("settings", msg="Added %s." % e["name"]))


@app.post("/settings/save/<int:i>")
def save(i):
    e = form_entry()
    items = load_config()
    if not (0 <= i < len(items)):
        return redirect(url_for("settings"))
    if not (e["name"] and e["tag"] and e["account"]):
        return page(SETTINGS, error="Fill in the name, tag and at least one bank account.",
                    tags=paperless_tags(), accounts=export_accounts(), e=items[i], edit=i), 400
    items[i] = e
    save_config(items)
    return redirect(url_for("settings", msg="Saved %s." % e["name"]))


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
        return page(INDEX, error="Choose a .csv file exported from your budget app."), 400
    first = f.stream.read(2048).decode("utf-8-sig", "ignore").split("\n", 1)[0]
    f.stream.seek(0)
    if "Date" not in first or "Amount" not in first:
        return page(INDEX, error="That doesn't look like an bank export (missing Date and Amount columns)."), 400
    name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv"
    f.save(os.path.join(EXPORTS, name))
    for old in sorted(glob.glob(os.path.join(EXPORTS, "*.csv")))[:-12]:
        os.remove(old)
    if not load_config():
        return redirect(url_for("settings", msg="Export saved. Add an account to run the check."))
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
    w.writerow(["status", "date", "payee", "amount", "receipt", "link"])
    for m in b["rows"]:
        w.writerow([m["status"], m["date"], m["payee"], m["amount"], m["title"], m["url"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=receipts-%s-%s.csv" % (slug(b["name"]), r["year"])})


@app.get("/export")
def export_docs():
    items = load_config()
    try:
        b = items[int(request.args.get("i", ""))]
        start = dt.date.fromisoformat(request.args.get("start", ""))
        end = dt.date.fromisoformat(request.args.get("end", ""))
    except (ValueError, IndexError):
        return redirect(url_for("index", msg="Pick a business and valid dates."))
    try:
        pl = mr.Paperless(PL_URL, PL_TOKEN)
        docs = [d for d in mr.load_receipts(pl, None, b["tag"], AMOUNT_FIELD) if start <= d["created"] <= end]
        tmp = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
        index = io.StringIO()
        w = csv.writer(index)
        w.writerow(["file", "date", "title", "correspondent", "amount"])
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for d in sorted(docs, key=lambda x: x["created"]):
                body, name = pl.download(d["id"])
                name = "%s_%d_%s" % (d["created"], d["id"], re.sub(r"[^A-Za-z0-9._-]+", "-", name))
                z.writestr(name, body)
                w.writerow([name, d["created"], d["title"], d["correspondent"],
                            "" if d["cents"] is None else "%.2f" % (d["cents"] / 100)])
            z.writestr("index.csv", index.getvalue())
    except Exception as e:
        return redirect(url_for("index", msg="Export failed: %s" % e))
    if not docs:
        return redirect(url_for("index", msg="No documents with that tag in that period."))
    tmp.seek(0)
    fn = "%s_%s_to_%s.zip" % (slug(b["name"]).lower(), start, end)
    return Response(tmp, mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=" + fn})


@app.get("/health")
def health():
    return "ok"
