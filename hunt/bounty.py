#!/usr/bin/env python3
"""bounty — web3 bug bounty programs worth a researcher's time, filtered the way Immunefi filters.

  bounty.py run                 fetch + score + list
  bounty.py watch               light Immunefi-only poll for the scheduler; desktop-notify new programs /
                                scope changes / new competitions (no LLM, cheap enough to run hourly)
  bounty.py fetch               pull Immunefi's public program feed, apply config filters, detect scope changes
  bounty.py score               LLM-rank unscored / changed programs against profile.md (fit, why, angle, blockers)
  bounty.py list [--min 60] [--status ...] [--track crypto|fintech|c-cpp|web|ai] [--source ...] [--changed]
  bounty.py show <id>
  bounty.py mark <id> <status> [--note ...]

Sources:
  immunefi   https://immunefi.com/public-api/bounties.json (every program: scope assets, reward table, KYC /
             PoC rules, tags). Filters in config.json "bounty" mirror the site's panel: ecosystem, language,
             productType, programType, projectType, kyc, plus min max-bounty and paused / invite-only exclusions.
  hackerone  public directory via hackerone.com/graphql (no account): bounty stats, industry, structured
             scopes. Filters: industries, asset types, min top bounty, min response efficiency.
  curated    off-platform programs listed by hand in config "bounty.curated" (Chrome VRP, Google OSS VRP,
             Mozilla, Microsoft, Apple, ZDI ...). Static rows, scored like the rest.
Every row carries a `track`: crypto | fintech | c-cpp | web | ai, so `list --track fintech` is one queue.

What the feed is for: picking programs to study and where to spend review time, in the order the
program's own rules and the researcher's strengths suggest. Everything downstream (reading scope, running
analysis, writing a report) happens inside that program's rules: in-scope assets only, PoCs on a local
fork, disclosure through the platform. Nothing here touches a live deployment.

Signals used by the local `signal` score (0-100, no LLM). It estimates the chance of a valid paid finding,
not the headline payout:
  payout        critical max reward on a log scale, flat above ~$250k
  crowd         negative above $1M (saturated programs)
  fresh         assets added or program updated in the last 30 days (new scope is under-reviewed)
  new_program   launched in the last 60 / 180 days
  audit_gap     newest listed audit older than 12 months, or none listed
  breadth       number of in-scope smart-contract / client assets
  payer         Vault feature (rewards escrowed, pays reliably once triaged)
  low_sev_paid  mediums and lows accepted without a PoC
  friction      negative: KYC, pay-to-submit, invite-only, paused, Attackathon-only
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
import sys
import urllib.request

import hunt
import sources

FEED = "https://immunefi.com/public-api/bounties.json"
STATUSES = ("new", "watching", "researching", "submitted", "paid", "skipped")

SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "fit": {"type": "integer"},
            "why": {"type": "string"},
            "angle": {"type": "string"},
            "blockers": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "fit", "why", "angle", "blockers"],
    }}},
    "required": ["results"],
}

PROMPT = """You are helping a security researcher choose which public bug bounty programs to study. The
researcher's background is in PROFILE. Every program below is an Immunefi program that has published its
scope, rules and reward table; the researcher will work strictly inside those rules.

Programs come from several sources and carry a `track`:
- crypto: Immunefi smart-contract / client programs (reward table, scope assets, KYC/PoC rules given).
- fintech: payment, banking, exchange and wallet programs, mostly web/API scope. The researcher's edge here is
  business-logic in money flows (checkout, refunds, ledgers, reconciliation, webhooks, dispute handling).
- c-cpp: open-source or product programs whose scope is C/C++ code. The researcher is returning to C++
  after years away; the realistic play is fuzzing-driven discovery with clean root-cause reports, not
  exploit chains. Score these on how approachable the code scope is and whether crash reports pay.
- web / ai: general web, mobile and AI product programs; score lower unless something specific fits.
HackerOne rows include resolved-report counts and response efficiency: thousands of resolved reports means a
crowded program where duplicates dominate; low efficiency means slow or unreliable payment.

For each program return:
- `id` exactly as given.
- `fit` 0-100: the researcher's chance of producing a valid, paid finding on this program in the next few
  months. Weigh: technology match (languages, chain/client type, protocol category); how crowded the
  program is (headline bounties above $1M attract every hunter, small and new programs do not); whether
  scope changed recently or audits are stale; scope size; whether mediums and lows pay without a PoC;
  friction (KYC, pay-to-submit, invite-only). A $30k program the researcher can realistically find a medium
  on beats a $5M program they cannot. Prefer programs where the researcher's niche (payments-style value
  flows, Go-based clients, bridges, lending accounting) is rare among hunters.
- `why`: one sentence.
- `angle`: one sentence on where in the program's scope the researcher's experience applies best
  (e.g. "the Go consensus client", "the bridge message verification contracts", "the vault accounting").
  Name scope areas, not vulnerability techniques.
- `blockers`: concrete gating items (e.g. "KYC required", "PoC required for all severities",
  "pay-to-submit $75", "invite only", "scope is mostly Rust", "no audits listed, expect heavy triage").

# PROFILE
{profile}

# RESEARCHER PREFERENCES
{prefs}

# PROGRAMS
{items}
"""


def db():
    c = hunt.db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS bounty (
      id TEXT PRIMARY KEY, source TEXT, slug TEXT, project TEXT, url TEXT, github TEXT,
      max_bounty REAL, sc_critical_max REAL, sc_high_max REAL, dlt_critical_max REAL,
      languages TEXT, ecosystems TEXT, product_types TEXT, program_types TEXT, project_type TEXT,
      kyc INTEGER, poc TEXT, invite_only INTEGER, paused INTEGER, features TEXT,
      launch_date TEXT, updated_date TEXT, latest_audit TEXT, n_audits INTEGER,
      n_sc INTEGER, n_dlt INTEGER, n_web INTEGER, latest_asset TEXT, assets_hash TEXT,
      overview TEXT, signal INTEGER, signal_parts TEXT,
      first_seen TEXT, last_seen TEXT, changed_at TEXT, change_note TEXT,
      fit INTEGER, why TEXT, angle TEXT, blockers TEXT, scored_at TEXT,
      status TEXT DEFAULT 'new', notes TEXT
    );""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(bounty)")}
    if "track" not in cols:
        c.execute("ALTER TABLE bounty ADD COLUMN track TEXT DEFAULT 'crypto'")
        c.commit()
    if "notified_at" not in cols:
        c.execute("ALTER TABLE bounty ADD COLUMN notified_at TEXT")   # last desktop-notification sent for this row
        c.commit()
    return c


def conf():
    return hunt.cfg().get("bounty", {})


# ---------------------------------------------------------------- feed + filters

def fetch_feed():
    req = urllib.request.Request(FEED, headers={"User-Agent": sources.UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _any(wanted, have):
    """Filter semantics like the site: empty wanted list = no filter; otherwise any overlap passes."""
    return not wanted or bool(set(wanted) & set(have or []))


def passes(p, g):
    if g.get("exclude_paused", True) and p.get("isPaused"):
        return False
    if g.get("exclude_invite_only", True) and p.get("inviteOnly"):
        return False
    if g.get("kyc") == "not_required" and p.get("kyc"):
        return False
    if g.get("kyc") == "required" and not p.get("kyc"):
        return False
    if (p.get("maxBounty") or 0) < g.get("min_max_bounty", 0):
        return False
    end = (p.get("endDate") or "")[:10]
    if end and end < dt.date.today().isoformat():      # finished audit competition
        return False
    return (_any(g.get("ecosystem"), p.get("ecosystem")) and _any(g.get("language"), p.get("language"))
            and _any(g.get("product_type"), p.get("productType")) and _any(g.get("program_type"), p.get("programType"))
            and _any(g.get("project_type"), p.get("projectType")))


def reward_max(p, asset_type, severity):
    best = 0
    for r in p.get("rewards") or []:
        if r.get("assetType") == asset_type and r.get("severity") == severity:
            best = max(best, r.get("maxReward") or 0)
    return best


def signal_score(row, today):
    """Chance of landing a valid, paid finding, not headline payout. Big-name programs are saturated;
    small, new or recently re-scoped programs with old audits and low friction are where first wins come from."""
    parts = {}
    pay = row["sc_critical_max"] or row["dlt_critical_max"] or row["max_bounty"] or 0
    # payout: enough to matter, flat above $250k, and a crowd penalty above $1M (everyone is already there)
    parts["payout"] = int(min(100, max(0, (math.log10(max(pay, 1)) - 4) / 1.4 * 100)))
    parts["crowd"] = -40 if pay >= 2_000_000 else -20 if pay >= 1_000_000 else 0
    days_asset = _age_days(row["latest_asset"], today)
    days_upd = _age_days(row["updated_date"], today)
    fresh = min(days_asset, days_upd)
    parts["fresh"] = 100 if fresh <= 14 else 75 if fresh <= 30 else 40 if fresh <= 90 else 0
    launched = _age_days(row["launch_date"], today)
    parts["new_program"] = 100 if launched <= 60 else 60 if launched <= 180 else 0
    audit_age = _age_days(row["latest_audit"], today)
    parts["audit_gap"] = 90 if row["n_audits"] == 0 else 70 if audit_age > 365 else 35 if audit_age > 180 else 10
    n = row["n_sc"] + row["n_dlt"]
    parts["breadth"] = 20 if n <= 3 else 55 if n <= 10 else 85 if n <= 40 else 100
    feats = json.loads(row["features"] or "[]")
    poc = json.loads(row["poc"] or "[]")
    parts["payer"] = 30 if "Vault" in feats else 0                      # funds escrowed: pays when triaged
    parts["low_sev_paid"] = 20 if not any("low" in x or "medium" in x for x in poc) else 0  # mediums/lows without PoC
    friction = 0
    if row["kyc"]:
        friction += 10
    if "Pay to Submit" in feats:
        friction += 25
    if row["invite_only"]:
        friction += 40
    if row["paused"]:
        friction += 60
    if "Attackathon" in feats and len(feats) == 1:
        friction += 20
    parts["friction"] = -friction
    score = (0.15 * parts["payout"] + 0.25 * parts["fresh"] + 0.15 * parts["new_program"] + 0.20 * parts["audit_gap"]
             + 0.15 * parts["breadth"] + 0.10 * parts["payer"] + 0.05 * parts["low_sev_paid"]
             + parts["crowd"] + parts["friction"])
    return max(0, min(100, int(round(score)))), parts


def _age_days(iso, today):
    if not iso:
        return 10 ** 4
    try:
        return (today - dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).date()).days
    except ValueError:
        return 10 ** 4


def row_from(p):
    assets = p.get("assets") or []
    sc = [a for a in assets if a.get("type") == "smart_contract"]
    dlt = [a for a in assets if a.get("type") == "blockchain_dlt"]
    web = [a for a in assets if a.get("type") == "websites_and_applications"]
    latest_asset = max((a.get("addedAt") or "" for a in assets), default="")
    audits = p.get("audits") or []
    latest_audit = max((a.get("date") or "" for a in audits), default="")
    poc = p.get("pocPerTypeAndSeverity") or []
    assets_hash = hashlib.sha1(json.dumps(sorted(a.get("id", "") for a in assets)).encode()).hexdigest()[:12]
    return dict(
        source="immunefi", slug=p.get("slug", ""), project=p.get("project", ""),
        url=f"https://immunefi.com/bug-bounty/{p.get('slug', '')}/", github=p.get("githubUrl") or "",
        max_bounty=p.get("maxBounty") or 0,
        sc_critical_max=reward_max(p, "smart_contract", "critical"), sc_high_max=reward_max(p, "smart_contract", "high"),
        dlt_critical_max=reward_max(p, "blockchain_dlt", "critical"),
        languages=json.dumps(p.get("language") or []), ecosystems=json.dumps(p.get("ecosystem") or []),
        product_types=json.dumps(p.get("productType") or []), program_types=json.dumps(p.get("programType") or []),
        project_type=json.dumps(p.get("projectType") or []),
        kyc=1 if p.get("kyc") else 0, poc=json.dumps(poc), invite_only=1 if p.get("inviteOnly") else 0,
        paused=1 if p.get("isPaused") else 0, features=json.dumps(p.get("features") or []),
        launch_date=(p.get("launchDate") or "")[:10], updated_date=(p.get("updatedDate") or "")[:10],
        latest_audit=latest_audit[:10], n_audits=len(audits),
        n_sc=len(sc), n_dlt=len(dlt), n_web=len(web), latest_asset=latest_asset[:10], assets_hash=assets_hash,
        overview=(p.get("programOverview") or "")[:3000],
    )


# ---------------------------------------------------------------- hackerone

H1_GQL = "https://hackerone.com/graphql"
H1_QUERY = """query($after: String) { teams(first: 100, after: $after, where: {submission_state:{_eq: open}, offers_bounties:{_eq:true}}) {
  pageInfo { hasNextPage endCursor }
  edges { node { handle name industry triage_active resolved_report_count response_efficiency_percentage
    average_bounty_lower_amount average_bounty_upper_amount top_bounty_lower_amount top_bounty_upper_amount
    base_bounty currency launched_at
    structured_scopes(first: 100, archived: false) { edges { node { asset_identifier asset_type eligible_for_bounty eligible_for_submission max_severity } } } } } } }"""
FINTECH_WORDS = re.compile(r"\b(pay|payment|bank|card|wallet|fintech|lending|loan|credit|exchange|broker|treasury|remit|invoice|billing|checkout|money|crypto|coin)\w*", re.I)
CODE_ASSETS = ("SOURCE_CODE", "DOWNLOADABLE_EXECUTABLES", "HARDWARE", "OTHER_APK", "OTHER_IPA")
WEB_ASSETS = ("URL", "WILDCARD", "API", "CIDR")


def fetch_hackerone():
    out, after = [], None
    for _ in range(10):
        body = json.dumps({"query": H1_QUERY, "variables": {"after": after}}).encode()
        req = urllib.request.Request(H1_GQL, data=body, headers={"Content-Type": "application/json", "User-Agent": sources.UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.load(r)
        if d.get("errors"):
            raise RuntimeError(d["errors"][0].get("message"))
        t = d["data"]["teams"]
        out += [e["node"] for e in t["edges"]]
        if not t["pageInfo"]["hasNextPage"]:
            break
        after = t["pageInfo"]["endCursor"]
    return out


def h1_track(n, g):
    scopes = [e["node"] for e in (n.get("structured_scopes") or {}).get("edges", [])]
    types = {sc["asset_type"] for sc in scopes}
    if n["handle"] in ((g.get("hackerone") or {}).get("c_cpp_handles") or []):
        return "c-cpp"
    if "SMART_CONTRACT" in types:
        return "crypto"
    if n.get("industry") == "Financial Services & Insurance" or FINTECH_WORDS.search(n.get("name") or ""):
        return "fintech"
    if "AI_MODEL" in types or re.search(r"\b(ai|openai|anthropic|llm)\b", n.get("name") or "", re.I):
        return "ai"
    return "web"


def h1_passes(n, g, track):
    h = g.get("hackerone") or {}
    if n["handle"] in (h.get("exclude_handles") or []):
        return False
    if track in (h.get("exclude_tracks") or []):
        return False
    if (n.get("top_bounty_upper_amount") or 0) < h.get("min_top_bounty", 0) and track != "c-cpp":
        return False
    if (n.get("response_efficiency_percentage") or 0) < h.get("min_response_efficiency", 0) and track != "c-cpp":
        return False
    inds = h.get("industries") or []
    if inds and track == "web" and (n.get("industry") or "") not in inds:
        return False
    return True


def c_cpp_scope(scopes, g):
    pats = [re.compile(x, re.I) for x in (g.get("hackerone") or {}).get("c_cpp_repos") or []]
    return sorted({sc["asset_identifier"] for sc in scopes if sc["asset_type"] in CODE_ASSETS
                   and any(p.search(sc["asset_identifier"]) for p in pats)})


def h1_row(n, track, g=None):
    g = g or conf()
    scopes = [e["node"] for e in (n.get("structured_scopes") or {}).get("edges", [])]
    cpp = c_cpp_scope(scopes, g)
    types = sorted({sc["asset_type"] for sc in scopes})
    code = [sc for sc in scopes if sc["asset_type"] in CODE_ASSETS]
    web = [sc for sc in scopes if sc["asset_type"] in WEB_ASSETS]
    top = n.get("top_bounty_upper_amount") or n.get("top_bounty_lower_amount") or 0
    overview = (f"HackerOne public program. Industry: {n.get('industry') or '-'}. Resolved reports: {n.get('resolved_report_count')}. "
                f"Response efficiency: {n.get('response_efficiency_percentage')}%. Average bounty: ${n.get('average_bounty_lower_amount') or 0}"
                f"-${n.get('average_bounty_upper_amount') or 0}. Top bounty: ${n.get('top_bounty_lower_amount') or 0}-${top}. "
                f"Triage by HackerOne: {bool(n.get('triage_active'))}.\nIn-scope assets ({len(scopes)}):\n" +
                "\n".join(f"- [{sc['asset_type']}] {sc['asset_identifier']} (max {sc.get('max_severity') or '?'}{', no bounty' if not sc.get('eligible_for_bounty') else ''})"
                           for sc in scopes[:60]))
    assets_hash = hashlib.sha1(json.dumps(sorted(sc["asset_identifier"] for sc in scopes)).encode()).hexdigest()[:12]
    return dict(
        source="hackerone", slug=n["handle"], project=n["name"], url=f"https://hackerone.com/{n['handle']}", github="",
        max_bounty=top, sc_critical_max=0, sc_high_max=0, dlt_critical_max=0,
        ecosystems=json.dumps([]), product_types=json.dumps([n.get("industry") or "Unknown"]),
        program_types=json.dumps(types), project_type=json.dumps([track]),
        kyc=0, poc=json.dumps([]), invite_only=0, paused=0,
        features=json.dumps([x for x, ok in (("Triaged by HackerOne", n.get("triage_active")),
                                               (f"{n.get('resolved_report_count') or 0} resolved reports", True),
                                               (f"{n.get('response_efficiency_percentage') or 0}% response efficiency", True),
                                               (f"C/C++ code in scope: {', '.join(cpp)[:200]}", bool(cpp))) if ok]),
        languages=json.dumps(["C/C++"] if (cpp or track == "c-cpp") else []),
        launch_date=(n.get("launched_at") or "")[:10], updated_date="", latest_audit="", n_audits=0,
        n_sc=len(code), n_dlt=0, n_web=len(web), latest_asset="", assets_hash=assets_hash,
        overview=overview[:3000], track=track,
    )


def h1_signal(n, row, today):
    """Win-probability analogue for HackerOne: enough money, responsive program, not a dupe farm."""
    parts = {}
    top = row["max_bounty"] or 0
    parts["payout"] = int(min(100, max(0, (math.log10(max(top, 1)) - 3) / 1.7 * 100)))     # $1k→0, $50k→100
    rr = n.get("resolved_report_count") or 0
    parts["crowd"] = -35 if rr > 2000 else -20 if rr > 800 else 0 if rr > 50 else 10
    eff = n.get("response_efficiency_percentage") or 0
    parts["responsive"] = 100 if eff >= 90 else 60 if eff >= 70 else 20 if eff >= 50 else -20
    avg = n.get("average_bounty_upper_amount") or n.get("average_bounty_lower_amount") or 0
    parts["avg_bounty"] = 100 if avg >= 2000 else 70 if avg >= 1000 else 40 if avg >= 500 else 10
    launched = _age_days((n.get("launched_at") or "")[:10], today)
    parts["new_program"] = 100 if launched <= 90 else 60 if launched <= 365 else 0
    parts["code_scope"] = 60 if "C/C++" in (row.get("languages") or "") else 40 if row["n_sc"] else 0
    parts["edge"] = 40 if row["track"] in ("fintech", "c-cpp") else 0
    score = (0.25 * parts["payout"] + 0.20 * parts["responsive"] + 0.15 * parts["avg_bounty"] + 0.10 * parts["new_program"]
             + 0.15 * parts["code_scope"] + 0.15 * parts["edge"] + parts["crowd"])
    return max(0, min(100, int(round(score)))), parts


def curated_rows(g):
    rows = []
    for it in g.get("curated") or []:
        slug = re.sub(r"[^a-z0-9]+", "-", it["name"].lower()).strip("-")
        rows.append(dict(
            source="curated", slug=slug, project=it["name"], url=it["url"], github="",
            max_bounty=it.get("max_bounty") or 0, sc_critical_max=0, sc_high_max=0, dlt_critical_max=0,
            languages=json.dumps(it.get("languages") or []), ecosystems=json.dumps([]),
            product_types=json.dumps(it.get("categories") or []), program_types=json.dumps(it.get("asset_types") or []),
            project_type=json.dumps([it.get("track") or "web"]), kyc=0, poc=json.dumps([]), invite_only=0, paused=0,
            features=json.dumps(it.get("features") or []), launch_date="", updated_date="", latest_audit="", n_audits=0,
            n_sc=1 if (it.get("track") == "c-cpp") else 0, n_dlt=0, n_web=0, latest_asset="",
            assets_hash=hashlib.sha1(json.dumps(it, sort_keys=True).encode()).hexdigest()[:12],
            overview=(it.get("notes") or "")[:3000], track=it.get("track") or "web"))
    return rows


def upsert(c, row, now):
    """Insert or update one program row; returns ('new'|'changed'|'same')."""
    old = c.execute("SELECT assets_hash, max_bounty, n_sc, n_dlt FROM bounty WHERE id=?", (row["id"],)).fetchone()
    if old is None:
        row.update(first_seen=now, last_seen=now)
        c.execute(f"INSERT INTO bounty ({', '.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        return "new"
    notes = []
    if old["assets_hash"] != row["assets_hash"]:
        notes.append(f"scope changed ({old['n_sc'] + old['n_dlt']} -> {row['n_sc'] + row['n_dlt']} code assets)")
    if (old["max_bounty"] or 0) != (row["max_bounty"] or 0):
        notes.append(f"max bounty {old['max_bounty'] or 0:.0f} -> {row['max_bounty']:.0f}")
    sets = ", ".join(f"{k}=?" for k in row if k != "id")
    c.execute(f"UPDATE bounty SET {sets}, last_seen=? WHERE id=?", [v for k, v in row.items() if k != "id"] + [now, row["id"]])
    if notes:
        c.execute("UPDATE bounty SET changed_at=?, change_note=?, scored_at=NULL WHERE id=?", (now, "; ".join(notes), row["id"]))
        return "changed"
    return "same"


# ---------------------------------------------------------------- notifications

NOTIFY_MIN_SIGNAL = 55          # standard programs below this don't pop; competitions always pop


def _is_competition(p):
    """Immunefi audit competition / boost: time-boxed, split-pool, first-mover matters."""
    return bool(p.get("endDate")) or str(p.get("project") or "").lower().startswith("audit competition") \
        or any(p.get(k) for k in ("primaryPool", "allStarsPool", "podiumPool", "rewardsPool", "boostedLeaderboard"))


def notify_mac(title, msg, sound="Glass"):
    """Fire a native macOS notification (same idiom as hunt.py digest)."""
    try:
        subprocess.run(["osascript", "-e",
                        f"display notification {json.dumps(msg)} with title {json.dumps(title)} "
                        f"sound name {json.dumps(sound)}"],
                       capture_output=True, timeout=10)
    except Exception as e:
        hunt.log(f"bounty notify: osascript failed: {e}")


def _event_line(state, row, comp, p):
    pay = row["sc_critical_max"] or row["dlt_critical_max"] or row["max_bounty"] or 0
    tag = "COMP" if comp else "bounty"
    live = ""
    if comp:
        launch = (p.get("launchDate") or "")[:16].replace("T", " ")
        end = (p.get("endDate") or "")[:10]
        live = f"  launch {launch or '?'} UTC -> {end or '?'}"
    note = f"  [{row['change_note']}]" if state == "changed" and row.get("change_note") else ""
    return (f"{state.upper():7} {tag}  {row['project'][:40]:<40} ${pay/1e3:>6,.0f}k  sig {row['signal']:>3}"
            f"{live}{note}\n    {row['url']}")


def notify_events(c, events, now):
    """events = list of (state, row, p). Pop one desktop notification summarizing the un-notified ones,
    newest competitions first, and append the full detail to logs/bounty-alerts.log. Returns count popped."""
    fresh = []
    for state, row, p in events:
        comp = _is_competition(p)
        if not comp and (row["signal"] or 0) < NOTIFY_MIN_SIGNAL:
            continue
        r = c.execute("SELECT notified_at, changed_at FROM bounty WHERE id=?", (row["id"],)).fetchone()
        na = r["notified_at"] if r else None
        if state == "new" and na:                                   # already announced this program
            continue
        if state == "changed" and na and na >= (r["changed_at"] or ""):  # already announced this change
            continue
        fresh.append((state, row, comp, p))
    if not fresh:
        return 0
    c.executemany("UPDATE bounty SET notified_at=? WHERE id=?", [(now, row["id"]) for _, row, _, _ in fresh])
    c.commit()
    fresh.sort(key=lambda t: (not t[2], -(t[1]["signal"] or 0)))     # competitions first, then signal
    lines = [_event_line(s, row, comp, p) for s, row, comp, p in fresh]
    with open(hunt.local("logs/bounty-alerts.log"), "a") as f:
        f.write(f"\n=== {now} ===\n" + "\n".join(lines) + "\n")
    state0, row0, comp0, _ = fresh[0]
    ncomp = sum(1 for _, _, comp, _ in fresh if comp)
    if len(fresh) == 1:
        title = "🎯 Audit competition" if comp0 else f"💰 {state0} bounty"
        pay = row0["sc_critical_max"] or row0["dlt_critical_max"] or row0["max_bounty"] or 0
        msg = f"{row0['project'][:60]} — ${pay/1e3:,.0f}k (signal {row0['signal']})"
    else:
        title = f"🎯 {len(fresh)} bounty alerts" + (f" ({ncomp} comp)" if ncomp else "")
        msg = "; ".join(row["project"][:28] for _, row, _, _ in fresh[:3]) + ("…" if len(fresh) > 3 else "")
    notify_mac(title, msg, sound="Glass" if ncomp else "Pop")
    hunt.log(f"bounty notify: {len(fresh)} popped ({ncomp} competitions) -> logs/bounty-alerts.log")
    return len(fresh)


# ---------------------------------------------------------------- commands

def cmd_fetch(a):
    c, g = db(), conf()
    today = dt.date.today()
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    srcs = g.get("sources") or ["immunefi", "hackerone", "curated"]

    if "immunefi" in srcs:
        feed = fetch_feed()
        json.dump(feed, open(hunt.local("data/immunefi-feed.json"), "w"))   # recon.py reads scope from this
        kept = [p for p in feed if passes(p, g)]
        counts = {"new": 0, "changed": 0, "same": 0}
        keep_ids, events = [], []
        for p in kept:
            row = row_from(p)
            row["id"] = hunt.job_id("immunefi", row["slug"])
            row["track"] = "crypto"
            row["signal"], parts = signal_score(row, today)
            row["signal_parts"] = json.dumps(parts)
            keep_ids.append(row["id"])
            state = upsert(c, row, now)
            counts[state] += 1
            if state in ("new", "changed"):
                row["change_note"] = c.execute("SELECT change_note FROM bounty WHERE id=?", (row["id"],)).fetchone()[0]
                events.append((state, row, p))
        dropped = _drop_unfiltered(c, "immunefi", keep_ids)
        c.commit()
        notify_events(c, events, now)
        hunt.log(f"bounty immunefi: {len(feed)} programs, {len(kept)} pass filters, {counts['new']} new, "
                 f"{counts['changed']} changed scope, {dropped} dropped")

    if "hackerone" in srcs:
        try:
            teams = fetch_hackerone()
        except Exception as e:
            hunt.log(f"bounty hackerone: fetch failed: {e}")
            teams = []
        counts = {"new": 0, "changed": 0, "same": 0}
        keep_ids, kept = [], 0
        for n in teams:
            track = h1_track(n, g)
            if not h1_passes(n, g, track):
                continue
            kept += 1
            row = h1_row(n, track, g)
            row["id"] = hunt.job_id("hackerone", row["slug"])
            row["signal"], parts = h1_signal(n, row, today)
            row["signal_parts"] = json.dumps(parts)
            keep_ids.append(row["id"])
            counts[upsert(c, row, now)] += 1
        dropped = _drop_unfiltered(c, "hackerone", keep_ids) if teams else 0
        c.commit()
        if teams:
            hunt.log(f"bounty hackerone: {len(teams)} public bounty programs, {kept} pass filters, {counts['new']} new, "
                     f"{counts['changed']} changed scope, {dropped} dropped")

    if "curated" in srcs:
        counts = {"new": 0, "changed": 0, "same": 0}
        keep_ids = []
        for row in curated_rows(g):
            row["id"] = hunt.job_id("curated", row["slug"])
            row["signal"] = 50
            row["signal_parts"] = json.dumps({"curated": True})
            keep_ids.append(row["id"])
            counts[upsert(c, row, now)] += 1
        _drop_unfiltered(c, "curated", keep_ids)
        c.commit()
        hunt.log(f"bounty curated: {len(keep_ids)} programs, {counts['new']} new")


def _drop_unfiltered(c, source, keep_ids):
    """Untouched rows from this source that no longer pass the filters go away; anything marked stays."""
    q, p = "DELETE FROM bounty WHERE source=? AND status='new' AND notes IS NULL", [source]
    if keep_ids:
        q += f" AND id NOT IN ({','.join('?' * len(keep_ids))})"
        p += keep_ids
    return c.execute(q, p).rowcount


def describe(r):
    return (f"--- id: {r['id']}\nProject: {r['project']}  ({r['url']})  source: {r['source']}  track: {r['track']}\n"
            f"Languages: {r['languages']}  Ecosystems: {r['ecosystems']}\n"
            f"Product: {r['product_types']}  Program types: {r['program_types']}  Project: {r['project_type']}\n"
            f"Max bounty: ${r['max_bounty']:,.0f}  SC critical max: ${r['sc_critical_max']:,.0f}  "
            f"SC high max: ${r['sc_high_max']:,.0f}  DLT critical max: ${r['dlt_critical_max']:,.0f}\n"
            f"KYC: {'yes' if r['kyc'] else 'no'}  PoC required: {r['poc']}  Features: {r['features']}\n"
            f"Scope: {r['n_sc']} contracts, {r['n_dlt']} client/DLT assets, {r['n_web']} web; latest asset added {r['latest_asset'] or '-'}\n"
            f"Launched {r['launch_date']}, updated {r['updated_date']}; audits: {r['n_audits']} (latest {r['latest_audit'] or '-'})\n"
            f"Local signal: {r['signal']} {r['signal_parts']}\n"
            f"{'Recent change: ' + r['change_note'] if r['change_note'] else ''}\n"
            f"{(r['overview'] or '')[:1200]}")


def cmd_score(a):
    c, g = db(), conf()
    rows = c.execute("SELECT * FROM bounty WHERE scored_at IS NULL AND status NOT IN ('skipped') "
                     "ORDER BY signal DESC LIMIT 80").fetchall()
    if not rows:
        return hunt.log("bounty score: nothing to score")
    profile = hunt.read("profile.md")
    prefs = g.get("preferences", "No extra preferences.")
    model = hunt.cfg()["scoring"]["model"]
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    for i in range(0, len(rows), 8):
        batch = rows[i:i + 8]
        out, _ = hunt.claude(hunt.render(PROMPT, profile=profile, prefs=prefs,
                                         items="\n\n".join(describe(r) for r in batch)), SCHEMA, model)
        for res in out["results"]:
            c.execute("UPDATE bounty SET fit=?, why=?, angle=?, blockers=?, scored_at=? WHERE id=?",
                      (res["fit"], res["why"], res["angle"], json.dumps(res["blockers"]), now, res["id"]))
        c.commit()
    hunt.log(f"bounty score: {len(rows)} scored")


def fmt(r, long=False):
    langs = ",".join(json.loads(r["languages"] or "[]"))[:18]
    kinds = ",".join(json.loads(r["product_types"] or "[]"))[:22]
    flag = "*" if r["change_note"] and (r["changed_at"] or "") >= (dt.datetime.utcnow() - dt.timedelta(days=14)).isoformat() else " "
    s = (f"{r['id']}  fit {r['fit'] if r['fit'] is not None else '?':>3}  sig {r['signal']:>3} {flag} "
         f"${(r['sc_critical_max'] or r['dlt_critical_max'] or r['max_bounty'] or 0) / 1e3:>6,.0f}k "
         f"{'kyc' if r['kyc'] else '   '} {(r['track'] or '')[:7]:<7} {r['status']:<11} {r['project'][:28]:<28} {langs:<18} {kinds}")
    if long:
        s += (f"\n  {r['url']}\n  github: {r['github'] or '-'}\n  why: {r['why']}\n  angle: {r['angle']}\n"
              f"  blockers: {'; '.join(json.loads(r['blockers'] or '[]'))}\n"
              f"  signal parts: {r['signal_parts']}\n  change: {r['change_note'] or '-'} ({r['changed_at'] or '-'})\n"
              f"  notes: {r['notes'] or '-'}")
    return s


def cmd_list(a):
    q, p = "SELECT * FROM bounty WHERE coalesce(fit, signal) >= ?", [a.min]
    if getattr(a, "track", None):
        q += " AND track=?"
        p.append(a.track)
    if getattr(a, "source", None):
        q += " AND source=?"
        p.append(a.source)
    if a.status:
        q += " AND status=?"
        p.append(a.status)
    else:
        q += " AND status != 'skipped'"
    if a.changed:
        q += " AND changed_at IS NOT NULL"
    for r in db().execute(q + " ORDER BY coalesce(fit, 0) DESC, signal DESC LIMIT 60", p):
        print(fmt(r))


def cmd_show(a):
    r = db().execute("SELECT * FROM bounty WHERE id LIKE ?", (a.id + "%",)).fetchone() or sys.exit("not found")
    print(fmt(r, long=True))
    print("\n" + (r["overview"] or ""))


def cmd_mark(a):
    if a.status not in STATUSES:
        sys.exit(f"status must be one of {STATUSES}")
    c = db()
    # the id is a prefix match; an empty or ambiguous prefix would rewrite many rows
    n = c.execute("SELECT count(*) FROM bounty WHERE id LIKE ?", (a.id + "%",)).fetchone()[0]
    if not a.id or n != 1:
        sys.exit(f"id {a.id!r} matches {n} programs; nothing changed")
    c.execute("UPDATE bounty SET status=?, notes=coalesce(notes,'')||? WHERE id LIKE ?",
              (a.status, f"[{dt.date.today()}] {a.note or ''}\n", a.id + "%"))
    c.commit()
    print(a.id, "->", a.status)


def cmd_run(a):
    cmd_fetch(a)
    cmd_score(a)
    a.min, a.status, a.changed, a.track, a.source = 60, None, False, None, None
    cmd_list(a)


def cmd_watch(a):
    """Lightweight poll for the scheduler: Immunefi feed only (one GET, no LLM, no HackerOne), detect
    new programs / scope changes / newly-listed competitions, and pop a desktop notification. Cheap enough
    to run hourly. The twice-daily `run` still does full scoring + digest."""
    c, g = db(), conf()
    today = dt.date.today()
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    feed = fetch_feed()
    json.dump(feed, open(hunt.local("data/immunefi-feed.json"), "w"))
    kept = [p for p in feed if passes(p, g)]
    counts = {"new": 0, "changed": 0, "same": 0}
    events = []
    for p in kept:
        row = row_from(p)
        row["id"] = hunt.job_id("immunefi", row["slug"])
        row["track"] = "crypto"
        row["signal"], parts = signal_score(row, today)
        row["signal_parts"] = json.dumps(parts)
        state = upsert(c, row, now)
        counts[state] += 1
        if state in ("new", "changed"):
            row["change_note"] = c.execute("SELECT change_note FROM bounty WHERE id=?", (row["id"],)).fetchone()[0]
            events.append((state, row, p))
    c.commit()
    popped = notify_events(c, events, now)
    hunt.log(f"bounty watch: {len(kept)} in scope, {counts['new']} new, {counts['changed']} changed, {popped} alerts")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("run", "fetch", "score", "watch"):
        sp.add_parser(n)
    s = sp.add_parser("list")
    s.add_argument("--min", type=int, default=50)
    s.add_argument("--status")
    s.add_argument("--track", choices=["crypto", "fintech", "c-cpp", "web", "ai"])
    s.add_argument("--source", choices=["immunefi", "hackerone", "curated"])
    s.add_argument("--changed", action="store_true")
    sp.add_parser("show").add_argument("id")
    s = sp.add_parser("mark")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("--note")
    a = ap.parse_args()
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
