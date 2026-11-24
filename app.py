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

from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for

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

def _read():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(d):
    with lock, open(CONFIG, "w") as f:
        json.dump(d, f, indent=2)


def load_config():
    return _read().get("businesses", [])


def save_config(items):
    d = _read()
    d["businesses"] = items
    _write(d)


def ignored_keys():
    return set(_read().get("ignored", []))


def save_ignored(keys):
    d = _read()
    d["ignored"] = sorted(keys)
    _write(d)


def row_key(date, payee, amount):
    return "%s|%s|%s" % (date, payee, amount)


def saved_banks():
    return _read().get("bank_accounts", [])


def save_banks(items):
    d = _read()
    d["bank_accounts"] = sorted(set(items), key=str.lower)
    _write(d)


def latest_export():
    files = sorted(glob.glob(os.path.join(EXPORTS, "*.csv")))
    return files[-1] if files else None


def export_accounts():
    names = set(saved_banks())
    for b in load_config():
        for k in ("account", "personal"):
            names.update(x.strip() for x in b.get(k, "").split(";") if x.strip())
    path = latest_export()
    if not path:
        return sorted(names, key=str.lower)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("Account"):
                names.add(r["Account"])
    return sorted(names, key=str.lower)


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
    skip = ignored_keys()
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
                                          "payee": t["payee"], "category": t["category"], "title": d["title"],
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
            rows.append({"date": p["date"], "payee": p["payee"], "amount": p["amount"], "category": p.get("category", ""),
                         "status": "personal", "title": p["title"], "url": p["url"]})
        for r in rows:
            if row_key(r["date"], r["payee"], r["amount"]) in skip:
                r["was"] = r["status"]
                r["status"] = "ignored"
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

LOGO = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#6b46c1"/>'
        '<path d="M18 10h28v44l-5-3-4.5 3-4.5-3-4.5 3-4.5-3-5 3z" fill="#fff"/>'
        '<path d="M24 22h16M24 30h16M24 38h9" stroke="#6b46c1" stroke-width="3" stroke-linecap="round"/>'
        '<circle cx="46" cy="44" r="11" fill="#a78bfa" stroke="#6b46c1" stroke-width="3"/>'
        '<path d="M40.5 44l4 4 7-8" fill="none" stroke="#fff" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/></svg>')

CSS = """
:root{--bg:#fff;--fg:#1d2327;--mut:#5d6870;--line:#dde2e6;--card:#f6f8f9;--acc:#6b46c1;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#121516;--fg:#e6eaec;--mut:#9aa6ad;--line:#2b3236;--card:#1a1f21;--acc:#a78bfa;--bad:#f2867f}}
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
.scroll{overflow-x:auto}details.ms{position:relative}details.ms summary{list-style:none;cursor:pointer;padding:6px 12px;border:1px solid var(--line);border-radius:8px}
details.ms[data-on] summary{background:var(--acc);color:#fff;border-color:var(--acc)}
.msb{position:absolute;z-index:5;top:110%;left:0;min-width:240px;max-height:280px;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;box-shadow:0 4px 14px rgba(0,0,0,.25)}
.msb label{display:block;margin:2px 0;color:var(--fg);font-size:14px;white-space:nowrap}.msb a{font-size:13px}
button.on{background:var(--acc);color:#fff;border-color:var(--acc)}label{display:block;font-size:13px;color:var(--mut);margin:8px 0 2px}
"""

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper Trail</title>
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<style>{{ css|safe }}</style></head><body><main>
<h1 style="display:flex;align-items:center;gap:10px"><img src="/logo.svg" alt="" width="34" height="34">Paper Trail</h1>
<nav><a href="{{ url_for('index') }}">Check</a><a href="{{ url_for('settings') }}">Settings</a></nav>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
{% if msg %}<div class="card ok">{{ msg }}</div>{% endif %}
"""

INDEX = HEAD + """
<div class="mut">Paperless receipts matched against your bank export.</div>
<div class="card">
<form data-busy class="row" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
<input type="file" name="file" accept=".csv" required>
<input type="number" name="year" value="{{ year }}" style="width:90px">
<button>Upload and check</button></form>
<form data-busy class="row" style="margin-top:8px" method="post" action="{{ url_for('rerun') }}">
<input type="hidden" name="year" value="{{ year }}"><button class="alt">Re-run on latest export</button></form>
</div>
{% if not businesses %}<div class="card">No accounts yet. <a href="{{ url_for('settings') }}">Add one in Settings.</a></div>{% endif %}
{% if r %}
{% if stale is not none and stale > 14 %}<div class="card" style="border-color:#d9a400">Your newest transaction is {{ stale }} days old. Export from your budget app again for current data.</div>{% endif %}
<div class="mut">Export {{ r.export }}, newest transaction {{ r.newest_transaction or 'unknown' }}. Checked {{ r.ran_at }}.</div>
<div class="row" style="margin:14px 0 0"><label style="margin:0">Viewing</label>
<select id="biz">{% for b in r.businesses %}<option value="{{ loop.index0 }}">{{ b.name }}</option>{% endfor %}</select></div>
{% for b in r.businesses %}{% set bi = loop.index0 %}
<section data-sec="{{ bi }}">
<h2>{{ b.name }}</h2>
<div class="mut sum">{{ b.rows|selectattr('status','equalto','ok')|list|length }} with receipt, <span class="{{ 'bad' if b.missing else 'ok' }}">{{ b.missing|length }} missing</span>, {{ b.paid_personal|length }} paid personally{% set ig = b.rows|selectattr('status','equalto','ignored')|list|length %}{% if ig %}, {{ ig }} not needed{% endif %}</div>
<div class="card scroll" data-biz>
<div class="row" style="margin-bottom:8px">
<button type="button" class="alt f on" data-f="all">All</button>
<button type="button" class="alt f" data-f="missing">Missing</button>
<button type="button" class="alt f" data-f="ok">Has receipt</button>
<button type="button" class="alt f" data-f="personal">Paid personally</button>
<button type="button" class="alt f" data-f="ignored">Not needed</button>
{% set cats = b.rows|map(attribute='category')|select|unique|sort|list %}
{% set payees = b.rows|map(attribute='payee')|select|unique|sort|list %}
{% if cats %}<details class="ms" data-k="cat"><summary class="alt">Category</summary><div class="msb">{% for c in cats %}<label><input type="checkbox" value="{{ c }}"> {{ c }}</label>{% endfor %}<a href="#" class="clr">Clear</a></div></details>{% endif %}
{% if payees %}<details class="ms" data-k="payee"><summary class="alt">Payee</summary><div class="msb">{% for p in payees %}<label><input type="checkbox" value="{{ p }}"> {{ p }}</label>{% endfor %}<a href="#" class="clr">Clear</a></div></details>{% endif %}
<input class="q" placeholder="Search" style="margin-left:auto;width:150px">
<a href="{{ url_for('download', i=bi) }}">CSV</a></div>
<form data-busy class="row" method="get" action="{{ url_for('export_docs') }}" style="margin-bottom:8px">
<input type="hidden" name="i" value="{{ bi }}">
<span class="mut">Download list and documents</span>
<input type="date" name="start" value="{{ r.year }}-01-01" required>
<input type="date" name="end" value="{{ r.year }}-12-31" required>
<button class="alt">Zip and download</button></form>
<table><tr><th style="width:36px"></th><th>Date</th><th>Payee</th><th>Category</th><th>Receipt</th><th class="n">Amount</th><th></th></tr>
{% for m in b.rows %}<tr data-s="{{ m.status }}" data-cat="{{ m.category }}" data-payee="{{ m.payee }}"><td>{% if m.status == 'ok' %}<span class="ok" title="Has receipt">&#10003;</span>{% elif m.status == 'personal' %}<span title="Paid from a personal account" style="color:#d9a400">&#9888;</span>{% elif m.status == 'ignored' %}<span class="mut" title="Marked not needed">&ndash;</span>{% else %}<span class="bad" title="Missing receipt">&#10007;</span>{% endif %}</td>
<td>{{ m.date }}</td><td>{{ m.payee }}</td><td>{{ m.category }}</td><td>{% if m.url %}<a href="{{ m.url }}" target="_blank" rel="noopener">{{ m.title }}</a>{% endif %}</td><td class="n">{{ m.amount }}</td>
<td><form method="post" action="{{ url_for('ignore') }}" style="margin:0"><input type="hidden" name="i" value="{{ bi }}"><input type="hidden" name="r" value="{{ loop.index0 }}"><button class="alt" style="padding:2px 8px;font-size:12px">{{ 'Undo' if m.status == 'ignored' else 'Not needed' }}</button></form></td></tr>{% endfor %}
</table></div>
{% if b.orphans %}<details class="card"><summary>{{ b.orphans|length }} receipts with no transaction</summary><div class="scroll"><table>
<tr><th>Date</th><th>Receipt</th><th class="n">Amount</th></tr>
{% for o in b.orphans %}<tr><td>{{ o.date }}</td><td><a href="{{ o.url }}" target="_blank" rel="noopener">{{ o.title }}</a></td><td class="n">{{ o.amount }}</td></tr>{% endfor %}
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
  var f='all', q='', sel={cat:[],payee:[]};
  function apply(){box.querySelectorAll('tr[data-s]').forEach(function(tr){
    var ok=(f==='all'||tr.dataset.s===f)&&(!q||tr.textContent.toLowerCase().indexOf(q)>-1)
      &&(!sel.cat.length||sel.cat.indexOf(tr.dataset.cat)>-1)&&(!sel.payee.length||sel.payee.indexOf(tr.dataset.payee)>-1);
    tr.style.display=ok?'':'none';});}
  box.querySelectorAll('details.ms').forEach(function(d){
    function upd(){sel[d.dataset.k]=[].slice.call(d.querySelectorAll('input:checked')).map(function(x){return x.value;});
      var n=sel[d.dataset.k].length;d.querySelector('summary').textContent=d.dataset.k==='cat'?'Category':'Payee';
      if(n){d.querySelector('summary').textContent+=' ('+n+')';d.setAttribute('data-on','1');}else d.removeAttribute('data-on');apply();}
    d.querySelectorAll('input').forEach(function(i){i.onchange=upd;});
    d.querySelector('.clr').onclick=function(e){e.preventDefault();d.querySelectorAll('input').forEach(function(i){i.checked=false;});upd();};
    document.addEventListener('click',function(e){if(!d.contains(e.target))d.removeAttribute('open');});
  });
  box.querySelectorAll('.f').forEach(function(b){b.onclick=function(){
    f=b.dataset.f;box.querySelectorAll('.f').forEach(function(x){x.classList.toggle('on',x===b);});apply();};});
  box.querySelectorAll('.f').forEach(function(b){b.dataset.l=b.textContent;});
  function recount(){var cnt={},tot=0;box.querySelectorAll('tr[data-s]').forEach(function(tr){cnt[tr.dataset.s]=(cnt[tr.dataset.s]||0)+1;tot++;});
    box.querySelectorAll('.f').forEach(function(b){b.textContent=b.dataset.l+' ('+(b.dataset.f==='all'?tot:(cnt[b.dataset.f]||0))+')';});
    var sm=box.closest('section').querySelector('.sum');
    if(sm){var m=cnt.missing||0,t=(cnt.ok||0)+' with receipt, '+m+' missing, '+(cnt.personal||0)+' paid personally';
      if(cnt.ignored)t+=', '+cnt.ignored+' not needed';sm.textContent=t;sm.className='mut sum';}}
  recount();
  var tb=box.querySelector('table'),dir={},cur=null;
  function sortBy(c,asc){var rows=[].slice.call(tb.querySelectorAll('tr[data-s]'));
    rows.sort(function(a,b){var x=a.cells[c].textContent.trim(),y=b.cells[c].textContent.trim();
      if(c===5)return (parseFloat(x)-parseFloat(y))*(asc?1:-1);return x.localeCompare(y)*(asc?1:-1);});
    var par=rows[0]&&rows[0].parentNode;rows.forEach(function(r){par.appendChild(r);});
    tb.querySelectorAll('th').forEach(function(h,i){h.textContent=h.textContent.replace(/ [\u25B2\u25BC]$/,'');
      if(i===c)h.textContent+=asc?' \u25B2':' \u25BC';});}
  [1,2,3,4,5].forEach(function(c){var h=tb.querySelectorAll('th')[c];h.style.cursor='pointer';h.title='Click to sort';
    h.onclick=function(){dir[c]=cur===c?!dir[c]:(c===1||c===5?false:true);cur=c;sortBy(c,dir[c]);};});
  dir[1]=false;cur=1;sortBy(1,false);
  box.querySelectorAll('form[action$="/ignore"]').forEach(function(fm){fm.addEventListener('submit',function(e){
    e.preventDefault();var btn=fm.querySelector('button');btn.disabled=true;
    fetch(fm.action,{method:'POST',body:new FormData(fm),headers:{'X-Requested-With':'fetch'}}).then(function(r){
      if(!r.ok)throw 0;return r.json();}).then(function(j){var tr=fm.closest('tr'),to=j.status;tr.dataset.s=to;
      var ic={ok:'<span class="ok" title="Has receipt">&#10003;</span>',personal:'<span title="Paid from a personal account" style="color:#d9a400">&#9888;</span>',
        ignored:'<span class="mut" title="Marked not needed">&ndash;</span>',missing:'<span class="bad" title="Missing receipt">&#10007;</span>'};
      tr.cells[0].innerHTML=ic[to]||'';btn.textContent=to==='ignored'?'Undo':'Not needed';btn.disabled=false;recount();apply();
    }).catch(function(){btn.disabled=false;fm.submit();});});});
  box.querySelector('.q').oninput=function(e){q=e.target.value.toLowerCase();apply();};
});
</script>
<script>
document.querySelectorAll('form[data-busy]').forEach(function(f){f.addEventListener('submit',function(){
  var b=f.querySelector('button');if(!b)return;var t=b.textContent;
  setTimeout(function(){b.disabled=true;b.textContent='Working...';},0);
  if(f.method==='get')setTimeout(function(){b.disabled=false;b.textContent=t;},20000);});});
</script>
</main></body></html>"""

SETTINGS = HEAD + """
<h2>Bank accounts</h2>
<div class="mut">Names must match the Account column in your bank export. Accounts found in an uploaded export are added automatically; add any others here.</div>
<div class="card">
{% for a in banks %}<form method="post" action="{{ url_for('bank_delete') }}" class="row" style="margin-bottom:6px"><span style="min-width:240px">{{ a }}</span><input type="hidden" name="name" value="{{ a }}"><button class="alt">Remove</button></form>{% endfor %}
{% if not banks %}<div class="mut" style="margin-bottom:8px">None saved yet.</div>{% endif %}
<form method="post" action="{{ url_for('bank_add') }}" class="row"><input name="name" required placeholder="Bank account name" style="min-width:240px"><button>Add bank account</button></form>
</div>
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
                                  businesses=load_config(), banks=saved_banks(),
                                  year=(r or {}).get("year", dt.date.today().year), **kw)


def parse_year():
    try:
        return int(request.form.get("year", ""))
    except ValueError:
        return None


@app.get("/")
def index():
    r = load_results()
    stale = None
    try:
        stale = (dt.date.today() - dt.date.fromisoformat(r["newest_transaction"])).days
    except (TypeError, KeyError, ValueError):
        pass
    return page(INDEX, msg=request.args.get("msg"), stale=stale)


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


@app.post("/settings/bank/add")
def bank_add():
    name = request.form.get("name", "").strip()
    if name:
        save_banks(saved_banks() + [name])
    return redirect(url_for("settings", msg="Added %s." % name if name else None))


@app.post("/settings/bank/delete")
def bank_delete():
    name = request.form.get("name", "")
    save_banks([x for x in saved_banks() if x != name])
    return redirect(url_for("settings", msg="Removed %s." % name))


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
    w.writerow(["status", "date", "payee", "category", "amount", "receipt", "link"])
    for m in b["rows"]:
        w.writerow([m["status"], m["date"], m["payee"], m["category"], m["amount"], m["title"], m["url"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=receipts-%s-%s.csv" % (slug(b["name"]), r["year"])})


def cpa_html(name, start, end, entries):
    from html import escape
    labels = {"ok": "Receipt", "personal": "Paid personally", "missing": "No receipt", "none": "No transaction", "ignored": "Not needed"}
    tr = []
    for st, d, p, c, a, path in entries:
        link = '<a href="%s" target="_blank">open</a>' % escape(path, quote=True) if path else ""
        tr.append('<tr data-s="%s"><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="n">%s</td><td>%s</td></tr>'
                  % (st, labels.get(st, st), escape(d), escape(p), escape(c), escape(a), link))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(n)s receipts</title><style>
body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:20px;color:#1d2327}h1{font-size:20px;margin:0}
.m{color:#5d6870;margin:2px 0 14px}.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
button,input{font:inherit;padding:6px 10px;border:1px solid #c9d0d5;border-radius:8px;background:#fff}
button.on{background:#6b46c1;color:#fff;border-color:#6b46c1}table{border-collapse:collapse;width:100%%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e3e7ea}td.n,th.n{text-align:right}
th{cursor:pointer;position:sticky;top:0;background:#f6f8f9}tr[data-s=missing] td:first-child{color:#b3261e}
tr[data-s=ok] td:first-child{color:#2f6f3e}a{color:#6b46c1}</style></head><body>
<h1>%(n)s</h1><div class="m">%(s)s to %(e)s. %(c)d items. Click a column heading to sort.</div>
<div class="bar"><button class="on" data-f="all">All</button><button data-f="ok">Receipt</button>
<button data-f="missing">No receipt</button><button data-f="personal">Paid personally</button><button data-f="none">No transaction</button><button data-f="ignored">Not needed</button>
<input id="q" placeholder="Search" style="margin-left:auto"></div>
<table id="t"><thead><tr><th>Status</th><th>Date</th><th>Payee</th><th>Category</th><th class="n">Amount</th><th>File</th></tr></thead>
<tbody>%(rows)s</tbody></table>
<script>
var f='all',q='',tb=document.querySelector('#t tbody');
function ap(){tb.querySelectorAll('tr').forEach(function(r){r.style.display=((f==='all'||r.dataset.s===f)&&(!q||r.textContent.toLowerCase().indexOf(q)>-1))?'':'none'})}
document.querySelectorAll('.bar button').forEach(function(b){b.onclick=function(){f=b.dataset.f;document.querySelectorAll('.bar button').forEach(function(x){x.classList.toggle('on',x===b)});ap()}});
document.getElementById('q').oninput=function(e){q=e.target.value.toLowerCase();ap()};
document.querySelectorAll('th').forEach(function(h,i){var asc=1;h.onclick=function(){asc=-asc;
 var rs=[].slice.call(tb.querySelectorAll('tr'));rs.sort(function(a,b){var x=a.cells[i].textContent,y=b.cells[i].textContent;
 var nx=parseFloat(x),ny=parseFloat(y);if(i===4)return (nx-ny)*asc;return x.localeCompare(y)*asc});rs.forEach(function(r){tb.appendChild(r)})}});
</script></body></html>""" % {"n": escape(name), "s": start, "e": end, "c": len(entries), "rows": "".join(tr)}


def doc_id(url):
    m = re.search(r"/documents/(\d+)/", url or "")
    return int(m.group(1)) if m else None


@app.get("/export")
def export_docs():
    r = load_results()
    try:
        b = r["businesses"][int(request.args.get("i", ""))]
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        dt.date.fromisoformat(start)
        dt.date.fromisoformat(end)
    except (TypeError, KeyError, ValueError, IndexError):
        return redirect(url_for("index", msg="Run a check first, then pick valid dates."))
    rows = [x for x in b["rows"] if start <= x["date"] <= end]
    orphans = [o for o in b["orphans"] if start <= o["date"] <= end]
    if not rows and not orphans:
        return redirect(url_for("index", msg="Nothing in that period."))
    try:
        pl = mr.Paperless(PL_URL, PL_TOKEN)
        tmp = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
        index = io.StringIO()
        w = csv.writer(index)
        w.writerow(["status", "date", "payee", "category", "amount", "receipt file"])
        entries = []
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for x in rows:
                path = ""
                did = doc_id(x["url"])
                if did:
                    body, name = pl.download(did)
                    ext = os.path.splitext(name)[1] or ".pdf"
                    path = "receipts/%s/%s_%s_%s%s" % (slug(x["category"] or "uncategorized").lower(), x["date"],
                                                      slug(x["payee"]).lower(), x["amount"].replace("-", ""), ext)
                    z.writestr(path, body)
                w.writerow([x["status"], x["date"], x["payee"], x["category"], x["amount"], path])
                entries.append((x["status"], x["date"], x["payee"], x["category"], x["amount"], path))
            for o in orphans:
                did = doc_id(o["url"])
                if not did:
                    continue
                body, name = pl.download(did)
                path = "no-transaction/%s_%d_%s" % (o["date"], did, slug(name))
                z.writestr(path, body)
                w.writerow(["no transaction", o["date"], o["correspondent"], "", o["amount"], path])
                entries.append(("none", o["date"], o["correspondent"], "", o["amount"], path))
            z.writestr("list.csv", index.getvalue())
            z.writestr("index.html", cpa_html(b["name"], start, end, entries))
    except Exception as e:
        return redirect(url_for("index", msg="Export failed: %s" % e))
    tmp.seek(0)
    fn = "%s_%s_to_%s.zip" % (slug(b["name"]).lower(), start, end)
    return Response(tmp, mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=" + fn})


@app.post("/ignore")
def ignore():
    r = load_results()
    try:
        b = r["businesses"][int(request.form.get("i", ""))]
        row = b["rows"][int(request.form.get("r", ""))]
    except (TypeError, KeyError, ValueError, IndexError):
        return redirect(url_for("index"))
    key = row_key(row["date"], row["payee"], row["amount"])
    keys = ignored_keys()
    if row["status"] != "ignored":
        keys.add(key)
        row["was"] = row["status"]
        row["status"] = "ignored"
    else:
        keys.discard(key)
        row["status"] = row.get("was", "missing")
    save_ignored(keys)
    b["missing"] = [x for x in b["rows"] if x["status"] == "missing"]
    with lock, open(RESULTS, "w") as f:
        json.dump(r, f)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(status=row["status"])
    return redirect(url_for("index"))


@app.get("/logo.svg")
def logo():
    return Response(LOGO, mimetype="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/health")
def health():
    return "ok"
