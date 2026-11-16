#!/usr/bin/env python3
"""govcon — government contract opportunities for a small software firm.

  govcon.py run              fetch all sources + score + print digest
  govcon.py fetch            recompetes (USAspending), Utah (Bonfire), DoD SBIR, SAM.gov (if key)
  govcon.py score            LLM-score unscored opportunities against profile.md
  govcon.py list [--min 60] [--kind recompete|solicitation|sbir] [--status pursuing]
  govcon.py show <id>
  govcon.py mark <id> <status> [--note ...]    new|pursuing|teaming|submitted|won|lost|skipped

Sources (all public):
  recompete     USAspending awards in your NAICS ending in the next ~18 months. The incumbent and
                rival primes are subcontracting targets; the rebid lands on SAM.gov 6-12 months out.
  solicitation  Utah public procurement (Bonfire portal) + SAM.gov solicitations (needs SAM_API_KEY,
                free from your sam.gov account profile; put it in config.json "govcon.sam_api_key").
  sources-sought / presolicitation
                SAM.gov pre-bid notices. Answer these: a 2-page capability response is how a new small
                business gets on the contracting officer's list and finds teaming primes.
  sbir          Open / pre-release DoD SBIR/STTR topics.
Bidding federally requires an active SAM.gov registration (UEI + CAGE) for your entity.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import hunt
import sources

GOV_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "fit": {"type": "integer"},
            "path": {"type": "string", "enum": ["subcontract", "prime", "sbir", "skip"]},
            "why": {"type": "string"},
            "angle": {"type": "string"},
            "blockers": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "fit", "path", "why", "angle", "blockers"],
    }}},
    "required": ["results"],
}

PROMPT = """You are a capture manager for a very small software consulting firm (the candidate's business in PROFILE).
Judge each government opportunity below. For each, return:
- `id` exactly as given.
- `fit` 0-100: how well the firm's real experience (PROFILE) matches the technical work. Generic IT ops,
  networks, hardware, construction, staffing of non-software roles => under 20.
- `path`: `subcontract` (best pursued under the incumbent or a competing prime — typical for large or
  ops-heavy contracts), `prime` (small enough / narrow enough to bid directly as a small business),
  `sbir` (SBIR/STTR topic worth a proposal), or `skip`.
- `why`: one sentence.
- `angle`: one sentence — the strongest hook from the firm's actual history.
- `blockers`: concrete gating items (e.g. "needs SAM.gov registration", "requires clearance",
  "COTS product purchase, not custom dev", "closes in 3 days", "needs past performance").

# PROFILE
{profile}

# OPPORTUNITIES
{items}
"""


def db():
    c = hunt.db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS gov (
      id TEXT PRIMARY KEY, source TEXT, kind TEXT, title TEXT, agency TEXT, holder TEXT,
      value REAL, deadline TEXT, url TEXT, description TEXT, first_seen TEXT,
      fit INTEGER, path TEXT, why TEXT, angle TEXT, blockers TEXT, scored_at TEXT,
      status TEXT DEFAULT 'new', notes TEXT
    );""")
    return c


def conf():
    return hunt.cfg().get("govcon", {})


def post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": sources.UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


# ---------------------------------------------------------------- sources

def recompetes(g):
    today = dt.date.today()
    horizon = today + dt.timedelta(days=30 * g.get("recompete_months", 18))
    out = {}
    for kw in g["keywords"]:
        body = {"filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "naics_codes": {"require": g["naics"]},
            "time_period": [{"start_date": str(today - dt.timedelta(days=3 * 365)),
                             "end_date": str(today)}],
            "award_amounts": [{"lower_bound": g["min_value"], "upper_bound": g["max_value"]}],
            "keywords": [kw]},
            "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
                       "Awarding Sub Agency", "Description", "End Date", "generated_internal_id"],
            "limit": 100, "sort": "Award Amount", "order": "desc"}
        for r in post_json("https://api.usaspending.gov/api/v2/search/spending_by_award/", body)["results"]:
            end = r.get("End Date") or ""
            desc = r.get("Description") or ""
            # keyword search is fuzzy (".NET" matches "network"): require the term in the description
            if not (str(today) < end <= str(horizon)) or kw.lower().strip(".") not in desc.lower():
                continue
            out[r["Award ID"]] = dict(
                source="usaspending", kind="recompete", ext=r["Award ID"], title=desc[:200],
                agency=r.get("Awarding Sub Agency") or r.get("Awarding Agency"),
                holder=r["Recipient Name"], value=r["Award Amount"], deadline=end,
                url=f"https://www.usaspending.gov/award/{r['generated_internal_id']}", description=desc)
    return list(out.values())


def utah(g):
    d = sources._json("https://utah.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData")
    deps = d["payload"]["departments"]
    kw = re.compile(g["state_title_regex"], re.I)
    out = []
    for p in d["payload"]["projects"].values():
        if not kw.search(p["ProjectName"]):
            continue
        dep = (deps.get(p["DepartmentID"]) or {}).get("DepartmentName", "")
        out.append(dict(source="utah-bonfire", kind="solicitation", ext=p["ProjectID"],
                        title=p["ProjectName"], agency=dep, holder="", value=None,
                        deadline=p["DateClose"][:10],
                        url=f"https://utah.bonfirehub.com/opportunities/{p['ProjectID']}",
                        description=f"{p['ReferenceID']}: {p['ProjectName']} ({dep})"))
    return out


def dod_sbir(g):
    q = urllib.parse.urlencode({"searchParam": json.dumps({"searchText": "", "topicReleaseStatus": [591, 592]}),
                                "size": 100, "page": 0})
    d = sources._json("https://www.dodsbirsttr.mil/topics/api/public/topics/search?" + q)
    kw = re.compile(g["sbir_title_regex"], re.I)
    out = []
    for t in d.get("data", []):
        if not kw.search(t.get("topicTitle", "")):
            continue
        detail = {}
        try:
            detail = sources._json(f"https://www.dodsbirsttr.mil/topics/api/public/topics/{t['topicId']}/details")
        except Exception:
            pass
        end = t.get("topicEndDate")
        out.append(dict(source="dod-sbir", kind="sbir", ext=t["topicCode"],
                        title=t["topicTitle"], agency=f"{t.get('component')} {t.get('program')}",
                        holder="", value=None,
                        deadline=str(dt.datetime.utcfromtimestamp(end / 1000).date()) if end else "",
                        url=f"https://www.dodsbirsttr.mil/topics-app/?topicCode={t['topicCode']}",
                        description="\n".join(sources.strip_html(str(detail.get(k) or ""))
                                              for k in ("objective", "description", "phase1Description"))))
    return out


def sam(g):
    key = os.environ.get("SAM_API_KEY") or g.get("sam_api_key")
    if not key:
        return []
    today = dt.date.today()
    out = []
    for naics in g["naics"]:
        # r = sources sought, p = presolicitation, o = solicitation, k = combined synopsis/solicitation.
        # Sources sought / presolicitations are the ones worth answering: they come 3-9 months before the
        # bid and are how a new small business gets known to the contracting officer and the teaming primes.
        for ptype, kind in (("r", "sources-sought"), ("p", "presolicitation"), ("o", "solicitation"),
                            ("k", "solicitation")):
            q = urllib.parse.urlencode({"api_key": key, "limit": 200, "ncode": naics, "ptype": ptype,
                                        "postedFrom": (today - dt.timedelta(days=14)).strftime("%m/%d/%Y"),
                                        "postedTo": today.strftime("%m/%d/%Y")})
            for o in sources._json("https://api.sam.gov/opportunities/v2/search?" + q).get("opportunitiesData", []):
                poc = ", ".join(f"{c.get('fullName','')} <{c.get('email','')}>" for c in (o.get("pointOfContact") or [])
                                if c.get("email"))
                out.append(dict(source="sam", kind=kind, ext=o["noticeId"], title=o.get("title", ""),
                                agency=o.get("fullParentPathName", ""), holder="", value=None,
                                deadline=(o.get("responseDeadLine") or "")[:10], url=o.get("uiLink", ""),
                                description=f"{o.get('type')} | set-aside: {o.get('typeOfSetAsideDescription')} | "
                                            f"NAICS {naics} | POC: {poc or '-'} | {o.get('title')}"))
    return out


SOURCES = {"recompete": recompetes, "utah": utah, "sbir": dod_sbir, "sam": sam}


# ---------------------------------------------------------------- commands

def cmd_fetch(a):
    c, g = db(), conf()
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    for name in g.get("sources", list(SOURCES)):
        try:
            items = SOURCES[name](g)
        except Exception as e:
            hunt.log(f"govcon source {name} failed: {e}")
            continue
        new = 0
        for it in items:
            gid = hunt.job_id(it["source"], it["ext"])
            if c.execute("SELECT 1 FROM gov WHERE id=?", (gid,)).fetchone():
                continue
            c.execute("""INSERT INTO gov (id, source, kind, title, agency, holder, value, deadline, url,
                         description, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (gid, it["source"], it["kind"], it["title"], it["agency"], it["holder"], it["value"],
                       it["deadline"], it["url"], it["description"], now))
            new += 1
        c.commit()
        hunt.log(f"govcon {name}: {len(items)} relevant, {new} new")


def cmd_score(a):
    c = db()
    rows = c.execute("SELECT * FROM gov WHERE scored_at IS NULL LIMIT 120").fetchall()
    if not rows:
        return hunt.log("govcon score: nothing to score")
    profile = hunt.read("profile.md")
    model = hunt.cfg()["scoring"]["model"]
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    for i in range(0, len(rows), 10):
        batch = rows[i:i + 10]
        items = "\n\n".join(
            f"--- id: {r['id']}\nKind: {r['kind']}\nTitle: {r['title']}\nAgency: {r['agency']}\n"
            f"Incumbent: {r['holder'] or '-'}\nValue: {r['value'] or '-'}\nDeadline/end: {r['deadline']}\n"
            f"{(r['description'] or '')[:2500]}" for r in batch)
        out, _ = hunt.claude(hunt.render(PROMPT, profile=profile, items=items), GOV_SCHEMA, model)
        for res in out["results"]:
            c.execute("""UPDATE gov SET fit=?, path=?, why=?, angle=?, blockers=?, scored_at=? WHERE id=?""",
                      (res["fit"], res["path"], res["why"], res["angle"], json.dumps(res["blockers"]), now,
                       res["id"]))
        c.commit()
    hunt.log(f"govcon score: {len(rows)} scored")


def fmt(r, long=False):
    val = f"${r['value'] / 1e6:.1f}M" if r["value"] else "—"
    s = (f"{r['id']}  {r['fit'] if r['fit'] is not None else '?':>3}  {r['path'] or '?':<11} {r['kind']:<12} "
         f"{r['deadline'] or '':<10} {val:>7}  {r['title'][:60]} — {(r['agency'] or '')[:30]}")
    if long:
        s += (f"\n  incumbent: {r['holder'] or '-'}\n  why: {r['why']}\n  angle: {r['angle']}\n"
              f"  blockers: {'; '.join(json.loads(r['blockers'] or '[]'))}\n  {r['url']}")
    return s


def cmd_list(a):
    q, p = "SELECT * FROM gov WHERE fit>=?", [a.min]
    if getattr(a, "status", None):
        q += " AND status=?"
        p.append(a.status)
    else:
        q += " AND status NOT IN ('skipped')"
    if a.kind:
        q += " AND kind=?"
        p.append(a.kind)
    for r in db().execute(q + " ORDER BY fit DESC, deadline LIMIT 60", p):
        print(fmt(r))


def cmd_show(a):
    r = db().execute("SELECT * FROM gov WHERE id LIKE ?", (a.id + "%",)).fetchone() or sys.exit("not found")
    print(fmt(r, long=True))
    print("\n" + (r["description"] or "")[:3000])


def cmd_mark(a):
    c = db()
    c.execute("UPDATE gov SET status=?, notes=coalesce(notes,'')||? WHERE id LIKE ?",
              (a.status, f"[{dt.date.today()}] {a.note or ''}\n", a.id + "%"))
    c.commit()
    print(a.id, "->", a.status)


def cmd_run(a):
    cmd_fetch(a)
    cmd_score(a)
    a.min, a.kind = 55, None
    cmd_list(a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("run", "fetch", "score"):
        sp.add_parser(n)
    s = sp.add_parser("list")
    s.add_argument("--min", type=int, default=50)
    s.add_argument("--kind")
    s.add_argument("--status", help="new|pursuing|teaming|submitted|won|lost|skipped")
    sp.add_parser("show").add_argument("id")
    s = sp.add_parser("mark")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("--note")
    a = ap.parse_args()
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
