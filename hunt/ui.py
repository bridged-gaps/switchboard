#!/usr/bin/env python3
"""hunt ui — local web app to review matches, draft, and apply.

  python3 ui.py [--port 8765]     then open http://localhost:8765

Binds to 127.0.0.1 only. Drafting shells out to `hunt.py draft` in the background; "Apply with
Claude" opens a Terminal running `claude "/apply <id>"` from the repo root. Set HUNT_CLAUDE_CONFIG_DIR
to pin which Claude login both of those use.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import hunt

ROOT = hunt.ROOT
REPO = os.path.dirname(ROOT)
ID_RE = re.compile(r"^[0-9a-f]{10}$")
DRAFT_FILES = ("cover.md", "answers.md", "NOTES.md", "resume.md")
VIEWS = {
    "inbox": ("new", "shortlisted"),
    "shortlisted": ("shortlisted",),
    "drafted": ("drafted",),
    "applied": ("applied", "interviewing", "offer"),
    "skipped": ("skipped", "rejected"),
}
DRAFTING = {}  # id -> Popen


def claude_env():
    env = dict(os.environ)
    if os.environ.get("HUNT_CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["HUNT_CLAUDE_CONFIG_DIR"]
    return env


def draft_dir(jid):
    hits = sorted(glob.glob(os.path.join(ROOT, "drafts", f"{jid}-*")))
    return hits[-1] if hits else None


def list_jobs(q):
    view = q.get("view", "inbox")
    min_score = int(q.get("min", 60))
    sql = ("SELECT id, score, fit, oe_compat, track, status, title, company, company_size, comp, "
           "salary, apply_method, first_seen, source FROM jobs WHERE score>=?")
    p = [min_score]
    if view in VIEWS:
        sql += f" AND status IN ({','.join('?' * len(VIEWS[view]))})"
        p += VIEWS[view]
    if q.get("track"):
        sql += " AND track=?"
        p.append(q["track"])
    if q.get("q"):
        sql += " AND (title LIKE ? OR company LIKE ? OR description LIKE ?)"
        p += [f"%{q['q']}%"] * 3
    sql += " ORDER BY score DESC, first_seen DESC LIMIT 400"
    c = hunt.db()
    rows = [dict(r) for r in c.execute(sql, p)]
    counts = dict(c.execute("SELECT status, count(*) FROM jobs WHERE score>=? GROUP BY status",
                            (min_score,)).fetchall())
    for r in rows:
        r["has_draft"] = bool(draft_dir(r["id"]))
        r["drafting"] = _drafting(r["id"])
    return {"jobs": rows, "counts": {v: sum(counts.get(s, 0) for s in ss) for v, ss in VIEWS.items()}}


def _drafting(jid):
    p = DRAFTING.get(jid)
    return bool(p and p.poll() is None)


def job_detail(jid):
    r = hunt.db().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return None
    out = dict(r)
    out["red_flags"] = json.loads(r["red_flags"] or "[]")
    out["drafting"] = _drafting(jid)
    p = DRAFTING.get(jid)
    if p and p.poll() not in (None, 0):
        out["draft_error"] = _tail(os.path.join(ROOT, "logs", f"ui-draft-{jid}.log"))
    d = draft_dir(jid)
    out["draft"] = None
    if d:
        files = {}
        for name in DRAFT_FILES:
            path = os.path.join(d, name)
            if os.path.exists(path):
                with open(path) as f:
                    files[name] = f.read()
        pdfs = [os.path.basename(x) for x in glob.glob(os.path.join(d, "*.pdf"))]
        out["draft"] = {"dir": d, "files": files, "pdf": pdfs[0] if pdfs else None}
    return out


def _tail(path, n=15):
    try:
        with open(path) as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def start_draft(jid):
    if _drafting(jid):
        return
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    log = open(os.path.join(ROOT, "logs", f"ui-draft-{jid}.log"), "w")
    DRAFTING[jid] = subprocess.Popen([sys.executable, "hunt.py", "draft", jid], cwd=ROOT,
                                     env=claude_env(), stdout=log, stderr=subprocess.STDOUT)


def set_status(jid, status, note):
    if status not in hunt.STATUSES:
        raise ValueError(f"bad status {status}")
    c = hunt.db()
    hunt._set_status(c, jid, status)
    if note:
        c.execute("UPDATE jobs SET notes=coalesce(notes,'')||? WHERE id=?",
                  (f"[{dt.date.today()}] {note}\n", jid))
        c.commit()


def claude_apply(jid):
    prefix = ""
    if os.environ.get("HUNT_CLAUDE_CONFIG_DIR"):
        prefix = f"CLAUDE_CONFIG_DIR='{os.environ['HUNT_CLAUDE_CONFIG_DIR']}' "
    cmd = f"cd '{REPO}' && {prefix}claude '/apply {jid}'"
    script = f'tell application "Terminal" to do script "{cmd}"\ntell application "Terminal" to activate'
    subprocess.Popen(["osascript", "-e", script])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local_host(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        if not self._local_host():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/jobs":
            return self._send(200, list_jobs(q))
        if len(parts) == 3 and parts[:2] == ["api", "job"] and ID_RE.match(parts[2]):
            d = job_detail(parts[2])
            return self._send(200 if d else 404, d or {"error": "not found"})
        if len(parts) == 3 and parts[0] == "pdf" and ID_RE.match(parts[1]):
            d = draft_dir(parts[1])
            path = os.path.join(d, os.path.basename(parts[2])) if d else ""
            if path.endswith(".pdf") and os.path.exists(path):
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "application/pdf")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # The custom header forces a CORS preflight, so other sites can't post here.
        if not self._local_host() or self.headers.get("X-Hunt") != "1":
            return self._send(403, {"error": "forbidden"})
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "job"] or not ID_RE.match(parts[2]):
            return self._send(404, {"error": "not found"})
        jid, action = parts[2], parts[3]
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if action == "status":
                set_status(jid, body.get("status"), (body.get("note") or "").strip())
            elif action == "draft":
                start_draft(jid)
            elif action == "claude-apply":
                claude_apply(jid)
            elif action == "open-folder":
                d = draft_dir(jid)
                if d:
                    subprocess.Popen(["open", d])
            else:
                return self._send(404, {"error": "unknown action"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self._send(200, job_detail(jid))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hunt Review</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1c1a;--muted:#6b6862;--line:#e4e1da;--accent:#2f5bd3;
  --accent-ink:#fff;--good:#1d7a46;--warn:#a35b00;--bad:#b3261e;--chip:#efede8;--sel:#e9eefc}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151514;--panel:#1d1d1b;--ink:#ecebe7;
  --muted:#9d9a93;--line:#2e2d2a;--accent:#7a9bff;--accent-ink:#0d1020;--good:#5cc98a;--warn:#e5a54b;
  --bad:#f08a80;--chip:#2a2926;--sel:#232b44}}
:root[data-theme="dark"]{--bg:#151514;--panel:#1d1d1b;--ink:#ecebe7;--muted:#9d9a93;--line:#2e2d2a;
  --accent:#7a9bff;--accent-ink:#0d1020;--good:#5cc98a;--warn:#e5a54b;--bad:#f08a80;--chip:#2a2926;--sel:#232b44}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:15px;margin:0 8px 0 0}
.tabs{display:flex;gap:4px;flex-wrap:wrap}
.tabs button{border:1px solid var(--line);background:none;color:var(--ink);padding:4px 10px;border-radius:999px;cursor:pointer;font:inherit}
.tabs button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.tabs .n{color:var(--muted);margin-left:4px}.tabs .on .n{color:inherit;opacity:.7}
input,select,textarea{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
.grow{flex:1}
main{display:grid;grid-template-columns:minmax(300px,420px) 1fr;height:calc(100vh - 53px)}
#list{overflow:auto;border-right:1px solid var(--line)}
.row{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer;display:grid;grid-template-columns:38px 1fr;gap:10px}
.row:hover{background:var(--chip)}.row.sel{background:var(--sel)}
.score{font-weight:700;font-size:16px;text-align:center;padding-top:2px}
.s-hi{color:var(--good)}.s-mid{color:var(--warn)}.s-lo{color:var(--muted)}
.t{font-weight:600}.sub{color:var(--muted);font-size:12.5px}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 6px;font-size:12px;margin-right:4px;color:var(--muted)}
.chip.st{color:var(--ink)}
#detail{overflow:auto;padding:20px 28px 60px}
#detail>*{max-width:880px}
#detail h2{margin:0 0 4px;font-size:20px}
.meta{color:var(--muted);margin-bottom:14px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:6px 12px;border-radius:6px;cursor:pointer;font:inherit;text-decoration:none}
.btn:hover{border-color:var(--muted)}
.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.btn.danger{color:var(--bad)}
.btn:disabled{opacity:.5;cursor:default}
kbd{font-size:11px;color:var(--muted);margin-left:4px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
.card h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.flags li{color:var(--warn)}
.pre{white-space:pre-wrap}
.desc{white-space:pre-wrap;color:var(--ink);max-height:420px;overflow:auto;font-size:13px}
.qa{border-top:1px solid var(--line);padding:10px 0}.qa:first-child{border-top:0;padding-top:0}
.qa .q{font-weight:600;display:flex;justify-content:space-between;gap:8px}
.confirm{background:color-mix(in srgb,var(--warn) 18%,transparent);border-radius:3px;padding:0 2px}
.copy{font-size:12px;padding:2px 8px}
.empty{color:var(--muted);padding:40px;text-align:center}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--muted);border-top-color:transparent;border-radius:50%;animation:s 1s linear infinite;vertical-align:-1px}
@keyframes s{to{transform:rotate(360deg)}}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:6px;opacity:0;transition:.2s;pointer-events:none}
.toast.on{opacity:1}
@media (max-width:800px){main{grid-template-columns:1fr;height:auto}#list{max-height:45vh;border-right:0;border-bottom:1px solid var(--line)}#detail{padding:16px}}
</style></head><body>
<header>
  <h1>Hunt</h1>
  <div class="tabs" id="tabs"></div>
  <span class="grow"></span>
  <input id="q" placeholder="Search" size="14">
  <select id="track"><option value="">All tracks</option><option>w2</option><option>contract</option><option>freelance</option><option>lead</option></select>
  <label class="sub">min <input id="min" type="number" value="60" min="0" max="100" style="width:58px"></label>
</header>
<main><div id="list"></div><div id="detail"><div class="empty">Pick a job. Keys: j/k move, s shortlist, x skip, d draft, a apply.</div></div></main>
<div class="toast" id="toast"></div>
<script>
const VIEWS=[["inbox","Inbox"],["shortlisted","Shortlist"],["drafted","Drafted"],["applied","Applied"],["skipped","Skipped"],["all","All"]];
let state={view:"inbox",jobs:[],sel:null,detail:null,poll:null};
try{const v=localStorage.getItem("hunt.view");if(v)state.view=v}catch(e){}
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const sc=n=>n>=72?"s-hi":n>=65?"s-mid":"s-lo";
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");setTimeout(()=>t.classList.remove("on"),1800)}
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json","X-Hunt":"1"},body:JSON.stringify(body)}:{});return r.json()}

async function load(keepSel){
  const p=new URLSearchParams({view:state.view,min:$("#min").value,track:$("#track").value,q:$("#q").value});
  const d=await api("/api/jobs?"+p);state.jobs=d.jobs;
  $("#tabs").innerHTML=VIEWS.map(([k,l])=>`<button data-v="${k}" class="${k==state.view?"on":""}">${l}${d.counts[k]!=null?`<span class="n">${d.counts[k]}</span>`:""}</button>`).join("");
  renderList();
  if(!keepSel||!state.jobs.find(j=>j.id==state.sel)){state.jobs.length?select(state.jobs[0].id):($("#detail").innerHTML='<div class="empty">Nothing here.</div>')}
}
function renderList(){
  $("#list").innerHTML=state.jobs.map(j=>`<div class="row ${j.id==state.sel?"sel":""}" data-id="${j.id}">
    <div class="score ${sc(j.score)}">${j.score}</div>
    <div><div class="t">${esc(j.title)}</div><div class="sub">${esc(j.company)} · ${esc(j.comp||j.salary||"comp n/a")}</div>
    <div style="margin-top:3px"><span class="chip">${esc(j.track)}</span>${j.company_size?`<span class="chip">${esc(j.company_size)}</span>`:""}${state.view=="all"||state.view=="applied"?`<span class="chip st">${j.status}</span>`:""}${j.drafting?'<span class="chip"><span class="spin"></span> drafting</span>':j.has_draft?'<span class="chip">draft ready</span>':""}</div></div></div>`).join("")||'<div class="empty">No matches.</div>';
}
async function select(id){
  state.sel=id;renderList();
  const d=await api("/api/job/"+id);if(state.sel!=id)return;state.detail=d;renderDetail();
  clearInterval(state.poll);if(d.drafting)state.poll=setInterval(async()=>{const n=await api("/api/job/"+id);if(!n.drafting){clearInterval(state.poll);if(state.sel==id){state.detail=n;renderDetail()}load(true);toast(n.draft_error?"Draft failed":"Draft ready")}},4000);
}
function confirmMarks(s){return esc(s).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/\[CONFIRM[^\]]*\]/g,m=>`<span class="confirm">${m}</span>`)}
function parseAnswers(md){return md.split(/\n\s*\n(?=\*\*)/).map(b=>{const m=b.match(/^\*\*(.+?)\*\*\s*\n?([\s\S]*)$/);return m?{q:m[1],a:m[2].trim()}:{q:"",a:b.trim()}})}
function renderDetail(){
  const j=state.detail,dr=j.draft,applyUrl=j.apply_url||j.url;
  const method=(j.apply_method||"").replace(/^ats:/,"");
  let h=`<h2>${esc(j.title)}</h2>
  <div class="meta">${esc(j.company)}${j.company_size?" · "+esc(j.company_size)+" co":""} · ${esc(j.location||"")} · <b>${esc(j.comp||j.salary||"comp n/a")}</b><br>
  <span class="chip">score ${j.score}</span><span class="chip">fit ${j.fit}</span><span class="chip">work-style ${j.oe_compat}</span><span class="chip">${esc(j.track)}</span><span class="chip st">${j.status}</span><span class="chip">${esc(j.source)}</span>${method?`<span class="chip">apply: ${esc(method)}</span>`:""}</div>
  <div class="bar">
    ${j.status!="shortlisted"?`<button class="btn" data-a="shortlisted">Shortlist<kbd>s</kbd></button>`:""}
    <button class="btn" data-a="draft" ${j.drafting?"disabled":""}>${j.drafting?'<span class="spin"></span> Drafting…':dr?"Redraft":"Draft"}<kbd>d</kbd></button>
    <a class="btn primary" href="${esc(applyUrl)}" target="_blank" rel="noopener" data-open>Open application<kbd>a</kbd></a>
    <button class="btn" data-a="claude-apply" title="Opens Terminal running claude /apply ${j.id}">Apply with Claude</button>
    <button class="btn" data-a="applied">Mark applied</button>
    <button class="btn danger" data-a="skipped">Skip<kbd>x</kbd></button>
  </div>
  <input id="note" placeholder="Note saved with the next status change (e.g. why you skipped)" style="width:100%">
  ${j.draft_error?`<div class="card"><h3>Draft failed</h3><div class="pre sub">${esc(j.draft_error)}</div></div>`:""}
  <div class="card"><h3>Why it scored</h3><div>${esc(j.why)}</div>${j.angle?`<p><b>Angle:</b> ${esc(j.angle)}</p>`:""}
  ${j.red_flags.length?`<ul class="flags">${j.red_flags.map(f=>`<li>${esc(f)}</li>`).join("")}</ul>`:""}</div>`;
  if(dr){
    const f=dr.files;
    h+=`<div class="card"><h3>Draft</h3><div class="bar" style="margin:0">
      ${dr.pdf?`<a class="btn" href="/pdf/${j.id}/${encodeURIComponent(dr.pdf)}" target="_blank">Resume PDF</a>`:""}
      ${f["cover.md"]?`<button class="btn" data-copy="cover">Copy cover note</button>`:""}
      <button class="btn" data-a="open-folder">Open folder</button></div></div>`;
    if(f["NOTES.md"])h+=`<div class="card"><h3>Check before you apply</h3><div class="pre">${confirmMarks(f["NOTES.md"].replace(/^#.*\n+.*\n+/,""))}</div></div>`;
    if(f["cover.md"])h+=`<div class="card"><h3>Cover note</h3><div class="pre">${confirmMarks(f["cover.md"])}</div></div>`;
    if(f["answers.md"])h+=`<div class="card"><h3>Form answers</h3>${parseAnswers(f["answers.md"]).map((x,i)=>`<div class="qa"><div class="q"><span>${esc(x.q)}</span><button class="btn copy" data-copy="a${i}">Copy</button></div><div class="pre">${confirmMarks(x.a)}</div></div>`).join("")}</div>`;
  }
  if(j.notes)h+=`<div class="card"><h3>Your notes</h3><div class="pre">${esc(j.notes)}</div></div>`;
  h+=`<div class="card"><h3>Posting</h3><p><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.url)}</a></p><div class="desc">${esc((j.description||"").replace(/(^|\n)[-•*]\s*\n+/g,"$1• ").replace(/\n{3,}/g,"\n\n"))}</div></div>`;
  $("#detail").innerHTML=h;$("#detail").scrollTop=0;
}
async function act(a){
  const j=state.detail;if(!j)return;
  if(a=="draft"){state.detail=await api(`/api/job/${j.id}/draft`,{});toast("Drafting… about a minute");renderDetail();select(j.id);load(true);return}
  if(a=="claude-apply"||a=="open-folder"){await api(`/api/job/${j.id}/${a}`,{});toast(a=="claude-apply"?"Opened Terminal":"Opened folder");return}
  const note=$("#note")?.value||"";
  await api(`/api/job/${j.id}/status`,{status:a,note});
  toast({skipped:"Skipped",shortlisted:"Shortlisted",applied:"Marked applied"}[a]||a);
  const i=state.jobs.findIndex(x=>x.id==j.id);await load(true);
  if(!state.jobs.find(x=>x.id==j.id)){const n=state.jobs[Math.min(i,state.jobs.length-1)];n?select(n.id):$("#detail").innerHTML='<div class="empty">Inbox zero.</div>'}else select(j.id);
}
async function copy(k){
  const f=state.detail.draft.files;let t=k=="cover"?f["cover.md"]:parseAnswers(f["answers.md"])[+k.slice(1)].a;
  t=t.replace(/\s*\[CONFIRM[^\]]*\]/g,"");try{await navigator.clipboard.writeText(t);toast("Copied")}catch(e){toast("Copy failed")}
}
document.addEventListener("click",e=>{
  const t=e.target.closest("[data-v],[data-id],[data-a],[data-copy]");if(!t)return;
  if(t.dataset.v){state.view=t.dataset.v;try{localStorage.setItem("hunt.view",state.view)}catch(e){}load()}
  else if(t.dataset.id)select(t.dataset.id);
  else if(t.dataset.a)act(t.dataset.a);
  else if(t.dataset.copy)copy(t.dataset.copy);
});
let deb;["#q","#min"].forEach(s=>$(s).addEventListener("input",()=>{clearTimeout(deb);deb=setTimeout(()=>load(),250)}));
$("#track").addEventListener("change",()=>load());
document.addEventListener("keydown",e=>{
  if(e.target.matches("input,textarea,select")||e.metaKey||e.ctrlKey)return;
  const i=state.jobs.findIndex(x=>x.id==state.sel);
  if(e.key=="j"&&i<state.jobs.length-1)select(state.jobs[i+1].id);
  else if(e.key=="k"&&i>0)select(state.jobs[i-1].id);
  else if(e.key=="s")act("shortlisted");
  else if(e.key=="x")act("skipped");
  else if(e.key=="d"&&!state.detail?.drafting)act("draft");
  else if(e.key=="a")document.querySelector("[data-open]")?.click();
  else return;
  document.querySelector(".row.sel")?.scrollIntoView({block:"nearest"});
});
load();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"hunt ui on http://localhost:{a.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
