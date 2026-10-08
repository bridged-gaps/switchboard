#!/usr/bin/env python3
"""hunt — find, score, and draft applications for remote eng work.

  hunt.py run                 fetch + score + digest (what the scheduler calls)
  hunt.py fetch               pull from all sources into the DB
  hunt.py score [--limit N]   LLM-score unscored jobs
  hunt.py digest              write today's digest of top unreviewed matches
  hunt.py list [--min 70] [--status new] [--track w2]
  hunt.py show <id>
  hunt.py draft <id>          tailored resume + cover/proposal + ATS answers -> drafts/<id>/
  hunt.py mark <id> <status>  new|shortlisted|drafted|applied|skipped|interviewing|offer|rejected
  hunt.py stats
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

import resolve
import sources

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "hunt.db")
STATUSES = ("new", "shortlisted", "drafted", "applied", "skipped", "interviewing", "offer",
            "rejected")


def local(rel):
    """Personal files (config.json, profile.md, resume/master.md) are gitignored; fall back to the
    committed *.example template so a fresh clone runs out of the box."""
    path = os.path.join(ROOT, rel)
    if os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    return base + ".example" + ext


def cfg():
    with open(local("config.json")) as f:
        return json.load(f)


def read(rel):
    with open(local(rel)) as f:
        return f.read()


def render(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ storage

def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, source TEXT, ext_id TEXT, url TEXT, title TEXT, company TEXT,
      location TEXT, salary TEXT, posted TEXT, description TEXT, kind_hint TEXT,
      dupe_key TEXT, first_seen TEXT,
      score INTEGER, fit INTEGER, oe_compat INTEGER, track TEXT, red_flags TEXT, why TEXT,
      angle TEXT, comp TEXT, scored_at TEXT,
      status TEXT DEFAULT 'new', status_at TEXT, notes TEXT, digested_at TEXT
    );
    CREATE INDEX IF NOT EXISTS jobs_dupe ON jobs(dupe_key);
    CREATE INDEX IF NOT EXISTS jobs_score ON jobs(score);
    CREATE TABLE IF NOT EXISTS runs (
      at TEXT, kind TEXT, n INTEGER, cost_usd REAL, detail TEXT
    );
    """)
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    for col in ("company_size", "apply_url", "apply_method", "enriched_at"):
        if col not in cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
    c.execute("UPDATE jobs SET track='lead' WHERE track='bg_lead'")  # renamed track
    return c


def job_id(source, ext_id):
    return hashlib.sha1(f"{source}:{ext_id}".encode()).hexdigest()[:10]


def dupe_key(company, title):
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(company)}|{norm(title)}"


# ---------------------------------------------------------------- prefilter

def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.isdigit():
            n = int(s)
            return dt.datetime.utcfromtimestamp(n / 1000 if n > 1e11 else n)
        if "T" in s or re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")[:19])
        return email.utils.parsedate_to_datetime(s).replace(tzinfo=None)
    except Exception:
        return None


def has_any(text, markers):
    return any(re.search(r"(?<![a-z])" + re.escape(m) + r"(?![a-z])", text) for m in markers)


def passes(j, pf, from_ats):
    title = f" {j['title'].lower()} "
    if not any(k in title for k in pf["title_include"]):
        return False
    if any(k in title for k in pf["title_exclude"]):
        return False
    loc = j["location"].lower()
    if j["source"] != "hn":  # HN location lives in a free-text header; let the LLM judge
        if any(k in loc for k in pf["location_exclude"]):
            return False
        if from_ats and pf["ats_require_remote"] and not has_any(loc, pf["remote_markers"]):
            return False
        if has_any(loc, pf["foreign_markers"]) and not has_any(loc, pf["us_markers"]):
            return False
    d = parse_date(j["posted"])
    if d and (dt.datetime.utcnow() - d).days > pf["max_age_days"]:
        return False
    return True


# -------------------------------------------------------------------- fetch

def cmd_fetch(args):
    c, conf = db(), cfg()
    pf = conf["prefilter"]
    tasks = [(name, False, (lambda fn: lambda: fn(conf))(sources.AGGREGATORS[name]))
             for name in conf["aggregators"]]
    for ats, slugs in conf["companies"].items():
        for slug in slugs:
            tasks.append((f"{ats}:{slug}", True,
                          (lambda fn, s: lambda: fn(s))(sources.ATS[ats], slug)))

    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    total = kept = new = 0
    errors = []
    with cf.ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(fn): (name, ats) for name, ats, fn in tasks}
        for fut in cf.as_completed(futs):
            name, from_ats = futs[fut]
            try:
                jobs = fut.result()
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            total += len(jobs)
            for j in jobs:
                if not j["title"] or not passes(j, pf, from_ats):
                    continue
                kept += 1
                jid = job_id(j["source"], j["ext_id"])
                dk = dupe_key(j["company"], j["title"])
                if c.execute("SELECT 1 FROM jobs WHERE id=? OR dupe_key=?", (jid, dk)).fetchone():
                    continue
                c.execute("""INSERT INTO jobs (id, source, ext_id, url, title, company, location,
                             salary, posted, description, kind_hint, dupe_key, first_seen, status_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (jid, j["source"], j["ext_id"], j["url"], j["title"], j["company"],
                           j["location"], j["salary"], j["posted"], j["description"],
                           j["kind_hint"], dk, now, now))
                new += 1
    c.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
              (now, "fetch", new, 0, json.dumps({"seen": total, "passed": kept, "errors": errors})))
    c.commit()
    for e in errors:
        log("source error:", e)
    log(f"fetch: {total} seen, {kept} passed prefilter, {new} new")


# -------------------------------------------------------------------- claude

def claude(prompt, schema, model):
    """Headless Claude Code call with structured output. Uses the logged-in subscription."""
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--strict-mcp-config", "--no-session-persistence", "--json-schema", json.dumps(schema)]
    for attempt in range(3):
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600)
        try:
            out = json.loads(p.stdout)
            if not out.get("is_error") and out.get("structured_output"):
                return out["structured_output"], out.get("total_cost_usd") or 0
            err = out.get("result") or p.stderr
        except json.JSONDecodeError:
            err = (p.stderr or p.stdout)[-500:]
        log(f"claude attempt {attempt + 1} failed: {err[:300]}")
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("claude call failed")


SCORE_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "track": {"type": "string", "enum": ["w2", "contract", "freelance", "lead", "reject"]},
            "fit": {"type": "integer"},
            "oe_compat": {"type": "integer"},
            "red_flags": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"},
            "angle": {"type": "string"},
            "comp": {"type": "string"},
            "company_size": {"type": "string", "enum": ["small", "mid", "large", "unknown"]},
        },
        "required": ["id", "track", "fit", "oe_compat", "red_flags", "why", "angle", "comp",
                     "company_size"],
    }}},
    "required": ["results"],
}


PAY_RE = re.compile(r"[^\n]*(\$\s?\d[\d,.]*\s?[kK]?|per hour|/hr|/hour|hourly rate|salary range|pay range|"
                    r"\b(?:c2c|corp[- ]to[- ]corp|1099|w-?2)\b)[^\n]*", re.I)


def pay_lines(desc):
    """Comp/terms often sit at the very end of long postings, past the scorer's truncation."""
    hits = list(dict.fromkeys(m.group(0).strip()[:200] for m in PAY_RE.finditer(desc or "")))[:6]
    return "Pay/terms mentioned in posting: " + " || ".join(hits) + "\n" if hits else ""


def cmd_score(args):
    c, conf = db(), cfg()
    sc = conf["scoring"]
    limit = args.limit or sc["max_per_run"]
    rows = c.execute("SELECT * FROM jobs WHERE scored_at IS NULL ORDER BY first_seen DESC LIMIT ?",
                     (limit,)).fetchall()
    if not rows:
        log("score: nothing to score")
        return
    profile, tpl = read("profile.md"), read("prompts/score.md")
    batches = [rows[i:i + sc["batch_size"]] for i in range(0, len(rows), sc["batch_size"])]

    def run(batch):
        jobs_txt = "\n\n".join(
            f"--- id: {r['id']}\nTitle: {r['title']}\nCompany: {r['company']}\n"
            f"Location: {r['location'][:200]}\nComp: {r['salary']}\nType hint: {r['kind_hint'][:100]}\n"
            f"Source: {r['source']}\n{pay_lines(r['description'])}\n{r['description'][:sc['desc_chars']]}"
            for r in batch)
        return claude(render(tpl, profile=profile, jobs=jobs_txt), SCORE_SCHEMA, sc["model"])

    cost = done = 0
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    with cf.ThreadPoolExecutor(3) as ex:
        for batch, fut in zip(batches, [ex.submit(run, b) for b in batches]):
            try:
                out, usd = fut.result()
            except Exception as e:
                log("score batch failed:", e)
                continue
            cost += usd
            ids = {r["id"] for r in batch}
            for res in out["results"]:
                if res["id"] not in ids:
                    continue
                score = 0 if res["track"] == "reject" else round(
                    sc["fit_weight"] * res["fit"] + sc["oe_weight"] * res["oe_compat"])
                if res["track"] == "w2":
                    score -= sc.get("w2_size_penalty", {}).get(res["company_size"], 0)
                score = max(score, 0)
                c.execute("""UPDATE jobs SET score=?, fit=?, oe_compat=?, track=?, red_flags=?,
                             why=?, angle=?, comp=?, company_size=?, scored_at=? WHERE id=?""",
                          (score, res["fit"], res["oe_compat"], res["track"],
                           json.dumps(res["red_flags"]), res["why"], res["angle"], res["comp"],
                           res["company_size"], now, res["id"]))
                done += 1
            c.commit()
            log(f"score: {done}/{len(rows)}")
    c.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (now, "score", done, cost, ""))
    c.commit()
    log(f"score: {done} scored, ~${cost:.2f} (notional on subscription)")


# ------------------------------------------------------------------- digest

def fmt_row(r):
    flags = json.loads(r["red_flags"] or "[]")
    comp = r["comp"] or r["salary"] or "comp n/a"
    s = (f"### [{r['score']}] {r['title']} — {r['company']}\n"
         f"`{r['id']}` · **{r['track']}** · {r['company_size'] or '?'} co · fit {r['fit']} · OE {r['oe_compat']} · {comp} · {r['source']}\n\n"
         f"{r['why']}\n\n*Angle:* {r['angle']}\n")
    if flags:
        s += f"\n⚠️ {'; '.join(flags)}\n"
    apply = f"\n**Apply:** {r['apply_method']} → {r['apply_url']}" if r["apply_url"] else ""
    if r["source"] == "linkedin" and r["kind_hint"] == "easy_apply":
        apply = "\n**Apply:** LinkedIn Easy Apply" + apply
    return s + f"\n{r['url']}{apply}\n"


def cmd_digest(args):
    c, conf = db(), cfg()
    d = conf["digest"]
    rows = c.execute("""SELECT * FROM jobs WHERE status='new' AND digested_at IS NULL AND score>=?
                        ORDER BY score DESC LIMIT ?""", (d["min_score"], d["max_items"])).fetchall()
    today = dt.date.today().isoformat()
    path = os.path.join(ROOT, "digests", f"{today}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        log("digest: no new matches")
        return
    by_track = {}
    for r in rows:
        by_track.setdefault(r["track"], []).append(r)
    names = {"w2": "W2 (OE)", "contract": "Contracts", "freelance": "Freelance gigs",
             "lead": "Consulting leads"}
    body = [f"# Hunt digest — {today}\n",
            f"{len(rows)} new matches ≥ {d['min_score']}. "
            f"Next: `hunt.py draft <id>` or `hunt.py mark <id> skipped`.\n"]
    for t in ("w2", "contract", "freelance", "lead"):
        if by_track.get(t):
            body.append(f"\n## {names[t]} ({len(by_track[t])})\n")
            body += [fmt_row(r) for r in by_track[t]]
    mode = "a" if os.path.exists(path) else "w"
    with open(path, mode) as f:
        f.write("\n".join(body) + "\n")
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    c.executemany("UPDATE jobs SET digested_at=? WHERE id=?", [(now, r["id"]) for r in rows])
    c.commit()
    log(f"digest: {len(rows)} matches -> {path}")
    if d.get("notify"):
        top = rows[0]
        msg = f"{len(rows)} new matches. Top: {top['title'][:60]} @ {top['company'][:30]} ({top['score']})"
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(msg)} with title "hunt"'],
                       capture_output=True)


# -------------------------------------------------------------------- draft

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "resume_md": {"type": "string"},
        "cover": {"type": "string"},
        "answers": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
            "required": ["question", "answer"]}},
        "notes": {"type": "string"},
    },
    "required": ["resume_md", "cover", "answers", "notes"],
}


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RESUME_CSS = """body{font:10.5pt/1.35 -apple-system,Helvetica,Arial,sans-serif;max-width:7.3in;margin:0 auto;color:#111}
h1{font-size:18pt;margin:0 0 2pt}h2{font-size:11.5pt;border-bottom:1px solid #999;margin:12pt 0 4pt;
text-transform:uppercase;letter-spacing:.04em}h3{font-size:10.5pt;margin:8pt 0 2pt}p{margin:3pt 0}
ul{margin:2pt 0 4pt;padding-left:16pt}li{margin:1pt 0}a{color:#111}"""


def md_to_html(md):
    import html as h
    inline = lambda s: re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>',
                              re.sub(r"\*([^*]+)\*", r"<em>\1</em>",
                                     re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", h.escape(s))))
    out, in_ul, lines = [], False, []
    for line in md.splitlines():  # join soft-wrapped paragraph lines
        plain = line.strip() and not re.match(r"^\s*([-*]\s|#|>|---)", line)
        if plain and lines and lines[-1].strip() and not re.match(r"^\s*(#|>|---)", lines[-1]):
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
    for line in lines:
        s = line.strip()
        m = re.match(r"^(#{1,3})\s+(.*)", s)
        if re.match(r"^[-*]\s+", s):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(s[2:].strip())}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif s and not s.startswith(">") and s != "---":
            out.append(f"<p>{inline(s)}</p>")
    if in_ul:
        out.append("</ul>")
    return f"<!doctype html><meta charset=utf-8><style>{RESUME_CSS}</style>" + "\n".join(out)


def md_to_pdf(md_path, pdf_path):
    html_path = md_path[:-3] + ".html"
    with open(md_path) as f, open(html_path, "w") as g:
        g.write(md_to_html(f.read()))
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf_path}", "file://" + html_path],
                   capture_output=True, timeout=120)
    return os.path.exists(pdf_path)


def resume_pdf_name():
    """'<Your Name>_Resume.pdf', from the first heading/line of resume/master.md."""
    first = next((l for l in read("resume/master.md").splitlines() if l.strip()
                  and not l.startswith(">") and "resume" not in l.lower()), "Resume")
    name = re.split(r"[—|,·-]", first.lstrip("# ").strip())[0].strip()
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return f"{name}_Resume.pdf" if name and "resume" not in name.lower() else "Resume.pdf"


def get_job(c, jid):
    r = c.execute("SELECT * FROM jobs WHERE id LIKE ?", (jid + "%",)).fetchone()
    if not r:
        sys.exit(f"no job {jid}")
    return r


def cmd_draft(args):
    c, conf = db(), cfg()
    r = get_job(c, args.id)
    prompt = render(read("prompts/draft.md"), profile=read("profile.md"),
                    resume=read("resume/master.md"), track=r["track"] or "w2",
                    title=r["title"], company=r["company"], location=r["location"],
                    salary=r["comp"] or r["salary"], url=r["apply_url"] or r["url"],
                    apply_method=r["apply_method"] or "unknown", fit=r["fit"],
                    oe_compat=r["oe_compat"], red_flags=r["red_flags"], angle=r["angle"],
                    description=r["description"][:12000])
    log(f"drafting {r['id']} {r['title']} @ {r['company']} ...")
    out, usd = claude(prompt, DRAFT_SCHEMA, conf["draft"]["model"])
    slug = re.sub(r"[^a-z0-9]+", "-", f"{r['company']}-{r['title']}".lower()).strip("-")[:60]
    d = os.path.join(ROOT, "drafts", f"{r['id']}-{slug}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "resume.md"), "w") as f:
        f.write(out["resume_md"])
    with open(os.path.join(d, "cover.md"), "w") as f:
        f.write(out["cover"])
    with open(os.path.join(d, "answers.md"), "w") as f:
        f.write("\n\n".join(f"**{a['question']}**\n{a['answer']}" for a in out["answers"]))
    with open(os.path.join(d, "job.json"), "w") as f:
        json.dump({k: r[k] for k in r.keys()}, f, indent=2)
    with open(os.path.join(d, "NOTES.md"), "w") as f:
        f.write(f"# {r['title']} — {r['company']}\n\n{r['url']}\n\n{out['notes']}\n")
    if not md_to_pdf(os.path.join(d, "resume.md"), os.path.join(d, resume_pdf_name())):
        log("warning: resume PDF render failed (Chrome headless)")
    _set_status(c, r["id"], "drafted")
    log(f"draft -> {d}  (~${usd:.2f})")
    print(d)


# --------------------------------------------------------------- inspection

def _set_status(c, jid, status):
    c.execute("UPDATE jobs SET status=?, status_at=? WHERE id=?",
              (status, dt.datetime.utcnow().isoformat(timespec="seconds"), jid))
    c.commit()


def cmd_mark(args):
    if args.status not in STATUSES:
        sys.exit(f"status must be one of {STATUSES}")
    c = db()
    r = get_job(c, args.id)
    _set_status(c, r["id"], args.status)
    if args.note:
        c.execute("UPDATE jobs SET notes=coalesce(notes,'')||? WHERE id=?",
                  (f"[{dt.date.today()}] {args.note}\n", r["id"]))
        c.commit()
    print(f"{r['id']} {r['title']} @ {r['company']} -> {args.status}")


def cmd_list(args):
    c = db()
    q, p = "SELECT * FROM jobs WHERE score>=?", [args.min]
    if args.status:
        q += " AND status=?"
        p.append(args.status)
    if args.track:
        q += " AND track=?"
        p.append(args.track)
    for r in c.execute(q + " ORDER BY score DESC LIMIT ?", p + [args.n]):
        ea = "EA" if r["source"] == "linkedin" and r["kind_hint"] == "easy_apply" else "  "
        print(f"{r['id']}  {r['score']:>3} {ea} {r['track']:<9} {r['status']:<11} "
              f"{r['title'][:55]:<55}  {r['company'][:25]}")


def cmd_show(args):
    r = get_job(db(), args.id)
    print(fmt_row(r))
    print(f"status: {r['status']}  location: {r['location'][:150]}\n")
    print(r["description"][:4000])


def cmd_stats(args):
    c = db()
    print("by status:", dict(c.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall()))
    print("by track :", dict(c.execute(
        "SELECT coalesce(track,'unscored'), count(*) FROM jobs GROUP BY 1").fetchall()))
    print("score>=70:", c.execute("SELECT count(*) FROM jobs WHERE score>=70").fetchone()[0])
    for r in c.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 6"):
        print(f"  {r['at']} {r['kind']:<6} n={r['n']} ${r['cost_usd']:.2f}")


def cmd_enrich(args):
    """Resolve promising aggregator jobs to the employer's own posting; rescore if it adds info."""
    c, conf = db(), cfg()
    e = conf["enrich"]
    marks = ",".join("?" * len(e["sources"]))
    rows = c.execute(f"""SELECT * FROM jobs WHERE enriched_at IS NULL AND status IN ('new','shortlisted')
                         AND score>=? AND source IN ({marks}) ORDER BY score DESC LIMIT ?""",
                     [e["min_score"]] + e["sources"] + [e["max_per_run"]]).fetchall()
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    found = rescore = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for r, res in zip(rows, ex.map(lambda r: _safe_resolve(dict(r)), rows)):
            if not res:
                c.execute("UPDATE jobs SET enriched_at=? WHERE id=?", (now, r["id"]))
                continue
            found += 1
            richer = len(res["description"]) > max(1.3 * len(r["description"]), len(r["description"]) + 500)
            c.execute("""UPDATE jobs SET apply_url=?, apply_method=?, enriched_at=?,
                         description=CASE WHEN ? THEN ? ELSE description END,
                         scored_at=CASE WHEN ? THEN NULL ELSE scored_at END,
                         digested_at=CASE WHEN ? THEN NULL ELSE digested_at END WHERE id=?""",
                      (res["apply_url"], res["apply_method"], now, richer, res["description"],
                       richer, richer, r["id"]))
            rescore += richer
    c.commit()
    log(f"enrich: {len(rows)} checked, {found} resolved to employer posting, {rescore} queued to rescore")


def _safe_resolve(r):
    try:
        return resolve.resolve(r)
    except Exception as e:
        log(f"resolve {r['id']} failed: {e}")
        return None


def cmd_run(args):
    cmd_fetch(args)
    cmd_score(args)
    cmd_enrich(args)
    cmd_score(args)  # rescore anything enrichment made richer
    cmd_digest(args)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "score"):
        s = sp.add_parser(name)
        s.add_argument("--limit", type=int)
    sp.add_parser("fetch")
    sp.add_parser("enrich")
    sp.add_parser("digest")
    sp.add_parser("stats")
    s = sp.add_parser("list")
    s.add_argument("--min", type=int, default=70)
    s.add_argument("--status")
    s.add_argument("--track")
    s.add_argument("-n", type=int, default=40)
    for name in ("show", "draft"):
        sp.add_parser(name).add_argument("id")
    s = sp.add_parser("mark")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("--note")
    a = ap.parse_args()
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
