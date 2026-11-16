#!/usr/bin/env python3
"""recon — pull a bounty program's in-scope code and run the pattern library over it.

  recon.py pull <bounty-id|slug>        fetch in-scope source (GitHub at the pinned commit, or verified
                                        explorer source) into data/recon/<slug>/, plus the program's rules
  recon.py run  <bounty-id|slug>        run the detectors (slither / aderyn / semgrep) over what was pulled
                                        and file normalised leads; re-runs are skipped unless scope changed
  recon.py leads [--program X] [--min 0] [--status new|reading|confirmed|rejected|known|reported]
  recon.py lead <lead-id>               full detail for one lead with the card's manual checklist
  recon.py mark <lead-id> <status> [--note ...]
  recon.py cards                        list the pattern cards and which detectors back them
  recon.py tools                        which detectors are installed

Pattern cards live in patterns/*.json. A card is one vulnerability class: the Immunefi impacts it maps to,
the protocol categories it applies to, the detector ids (per tool) that hint at it, a weight, and a
manual checklist. Detector hits are mapped to cards; hits no card claims land in `uncarded` at low weight.

Lead rank = card weight x tool confidence x program fit (from bounty.py). It orders reading time across
every program; it is not a finding. Everything after `leads` is manual: read, threat-model, then a test
on a local fork and a report through the platform.

Scope rules enforced here:
  - only assets the program lists as smart_contract / blockchain_dlt are pulled; web assets are ignored
  - nothing talks to a chain RPC; explorer calls fetch verified source only
  - the program's known issues, custom out-of-scope text and prior audits are saved next to the code and
    shown with every lead so duplicates are caught before any time is spent
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

import hunt
import sources

ROOT = hunt.ROOT
FEED_CACHE = os.path.join(ROOT, "data", "immunefi-feed.json")
RECON = os.path.join(ROOT, "data", "recon")
PATTERNS = os.path.join(ROOT, "patterns")
LEAD_STATUSES = ("new", "reading", "confirmed", "rejected", "known", "reported")

# Etherscan V2 covers these with one key (ETHERSCAN_API_KEY). Anything else is tried as Blockscout (no key).
ETHERSCAN_CHAINS = {
    "etherscan.io": 1, "sepolia.etherscan.io": 11155111, "hoodi.etherscan.io": 560048,
    "bscscan.com": 56, "polygonscan.com": 137, "arbiscan.io": 42161, "optimistic.etherscan.io": 10,
    "basescan.org": 8453, "snowtrace.io": 43114, "snowscan.xyz": 43114, "gnosisscan.io": 100,
    "scrollscan.com": 534352, "lineascan.build": 59144, "sonicscan.org": 146, "celoscan.io": 42220,
    "moonscan.io": 1284, "moonbeam.moonscan.io": 1284, "blastscan.io": 81457, "mantlescan.xyz": 5000,
    "era.zksync.network": 324, "fraxscan.com": 252, "katanascan.com": 747474, "hyperevmscan.io": 999,
    "uniscan.xyz": 130, "berascan.com": 80094, "worldscan.org": 480, "explorer.zksync.io": 324,
    "ftmscan.com": 250, "cronoscan.com": 25, "kavascan.com": 2222, "modescan.io": 34443,
}
TOOLS = ("slither", "aderyn", "semgrep", "solc-select")


def db():
    c = hunt.db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
      id TEXT PRIMARY KEY, program_id TEXT, slug TEXT, unit TEXT, tool TEXT, check_id TEXT, card TEXT,
      severity TEXT, confidence TEXT, file TEXT, line INTEGER, text TEXT, rank REAL,
      first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'new', notes TEXT
    );
    CREATE TABLE IF NOT EXISTS recon_runs (slug TEXT, unit TEXT, tool TEXT, at TEXT, ok INTEGER, n INTEGER, detail TEXT);
    CREATE INDEX IF NOT EXISTS leads_prog ON leads(program_id, status);
    """)
    return c


def log(m):
    hunt.log(m)


# ---------------------------------------------------------------- program lookup

def feed():
    if not os.path.exists(FEED_CACHE):
        sys.exit("no feed cache; run `python3 bounty.py fetch` first")
    return json.load(open(FEED_CACHE))


def program(ref):
    """Resolve a bounty id prefix or slug to (bounty row, feed record)."""
    c = db()
    row = c.execute("SELECT * FROM bounty WHERE id LIKE ? OR slug=?", (ref + "%", ref)).fetchone()
    if not row:
        sys.exit(f"no bounty program matching {ref!r} (see bounty.py list)")
    rec = next((p for p in feed() if p.get("slug") == row["slug"]), None)
    if not rec:
        sys.exit(f"{row['slug']} not in the cached feed; run bounty.py fetch")
    return row, rec


def pdir(slug):
    d = os.path.join(RECON, slug)
    os.makedirs(os.path.join(d, "src"), exist_ok=True)
    return d


# ---------------------------------------------------------------- pull

def write_rules(d, rec):
    rules = {k: rec.get(k) for k in (
        "project", "slug", "programOverview", "knownIssues", "customOutOfScopeInformation",
        "customProhibitedActivities", "defaultOutOfScopeSmartContract", "defaultOutOfScopeBlockchain",
        "defaultProhibitedActivities", "defaultFeasibilityLimitations", "eligibilityCriteria",
        "prioritizedVulnerabilities", "impacts", "rewards", "pocPerTypeAndSeverity", "kyc", "audits",
        "responsiblePublicationCategory", "tenPercentEconomicRule")}
    json.dump(rules, open(os.path.join(d, "rules.json"), "w"), indent=1)
    md = [f"# {rec.get('project')} — program rules (from Immunefi, {dt.date.today()})\n",
          "## In-scope impacts", *[f"- [{i.get('severity')}] {i.get('title')}" for i in rec.get("impacts") or []],
          "\n## Known issues (not paid)", rec.get("knownIssues") or "(none listed)",
          "\n## Custom out of scope", rec.get("customOutOfScopeInformation") or "(none)",
          "\n## Prohibited activities", rec.get("customProhibitedActivities") or "", rec.get("defaultProhibitedActivities") or "",
          "\n## Audits", *[f"- {a.get('date', '')[:10]} {a.get('auditor')} {a.get('url')}" for a in rec.get("audits") or []],
          "\n## PoC required for", json.dumps(rec.get("pocPerTypeAndSeverity")),
          "\n## Rewards", *[f"- {r.get('assetType')} {r.get('severity')}: {r.get('minReward')}–{r.get('maxReward')} ({r.get('rewardModel')})"
                            for r in rec.get("rewards") or []]]
    # newer programs send some of these as structured lists (knownIssues: [{link, description}])
    md = [x if isinstance(x, str) else "\n".join(
        f"- {i.get('description', '')} {i.get('link', '')}".rstrip() if isinstance(i, dict) else f"- {i}" for i in x)
        for x in md]
    open(os.path.join(d, "RULES.md"), "w").write("\n".join(md) + "\n")


def gh_parts(url):
    """github.com/<owner>/<repo>[/(blob|tree)/<ref>/<path>] -> (owner, repo, ref, path)"""
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)(?:/(?:blob|tree)/([^/]+)(?:/([^#?]*))?)?", url)
    if not m:
        return None
    owner, repo, ref, path = m.groups()
    return owner, repo.removesuffix(".git"), ref or "", path or ""


def clone_at(owner, repo, ref, dest):
    """Shallow clone at a commit/branch/tag. Returns the resolved sha or None."""
    if os.path.exists(os.path.join(dest, ".git")):
        sha = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        return sha or None
    os.makedirs(dest, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    cmds = [["git", "init", "-q"], ["git", "remote", "add", "origin", url]]
    for cmd in cmds:
        subprocess.run(cmd, cwd=dest, capture_output=True)
    r = subprocess.run(["git", "fetch", "-q", "--depth", "1", "origin", ref or "HEAD"], cwd=dest,
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return None
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest, capture_output=True)
    subprocess.run(["git", "submodule", "update", "--init", "--depth", "1", "-q"], cwd=dest, capture_output=True, timeout=600)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()


def explorer_source(host, address):
    """Verified source for one address. Returns (name, files{path: content}, meta) or raises."""
    chain = ETHERSCAN_CHAINS.get(host)
    key = os.environ.get("ETHERSCAN_API_KEY") or hunt.cfg().get("bounty", {}).get("etherscan_api_key", "")
    if chain:
        if not key:
            raise RuntimeError("needs ETHERSCAN_API_KEY (free at etherscan.io/myapikey; one key covers all chains)")
        q = urllib.parse.urlencode({"chainid": chain, "module": "contract", "action": "getsourcecode",
                                    "address": address, "apikey": key})
        url = "https://api.etherscan.io/v2/api?" + q
    else:
        q = urllib.parse.urlencode({"module": "contract", "action": "getsourcecode", "address": address})
        url = f"https://{host}/api?" + q
    req = urllib.request.Request(url, headers={"User-Agent": sources.UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    res = (d.get("result") or [{}])[0] if isinstance(d.get("result"), list) else {}
    src = res.get("SourceCode") or ""
    if not src:
        raise RuntimeError("not verified on the explorer" if d.get("status") in ("1", 1) else f"explorer: {d.get('message') or d.get('result')}")
    name = res.get("ContractName") or address[:10]
    files = {}
    body = src.strip()
    if body.startswith("{{"):
        body = body[1:-1]
    if body.startswith("{"):
        try:
            j = json.loads(body)
            srcs = j.get("sources", j)
            for path, v in srcs.items():
                files[path.lstrip("./")] = v.get("content", "") if isinstance(v, dict) else str(v)
            if isinstance(j.get("settings"), dict):
                files["__settings.json"] = json.dumps(j["settings"], indent=1)
        except ValueError:
            files[f"{name}.sol"] = src
    else:
        files[f"{name}.sol"] = src
    meta = {"compiler": res.get("CompilerVersion"), "proxy": res.get("Proxy"), "implementation": res.get("Implementation"),
            "optimization": res.get("OptimizationUsed"), "runs": res.get("Runs"), "evm": res.get("EVMVersion"),
            "license": res.get("LicenseType")}
    return name, files, meta


def write_foundry_toml(unit_dir, meta, settings_json):
    """Explorer dumps are flat standard-json trees. A foundry.toml with the verified remappings, solc
    version and optimizer lets `forge build` (and so slither / aderyn) compile them in place."""
    settings = {}
    if settings_json:
        try:
            settings = json.loads(settings_json)
        except ValueError:
            settings = {}
    ver = (meta.get("compiler") or "").lstrip("v").split("+")[0]
    opt = settings.get("optimizer") or {}
    lines = ["[profile.default]", 'src = "."', 'out = "out"', "libs = []", 'test = "__no_tests__"', 'script = "__no_scripts__"']
    if re.match(r"^0\.\d+\.\d+$", ver):
        lines.append(f'solc = "{ver}"')
    if "enabled" in opt:
        lines.append(f"optimizer = {str(bool(opt.get('enabled'))).lower()}")
        lines.append(f"optimizer_runs = {int(opt.get('runs') or 200)}")
    if settings.get("evmVersion"):
        lines.append(f'evm_version = "{settings["evmVersion"]}"')
    if settings.get("viaIR"):
        lines.append("via_ir = true")
    remaps = settings.get("remappings") or []
    lines.append("remappings = [" + ", ".join(json.dumps(r) for r in remaps) + "]")
    open(os.path.join(unit_dir, "foundry.toml"), "w").write("\n".join(lines) + "\n")


def cmd_pull(a):
    row, rec = program(a.ref)
    slug = row["slug"]
    d = pdir(slug)
    write_rules(d, rec)
    manifest = {"slug": slug, "program_id": row["id"], "pulled_at": dt.datetime.utcnow().isoformat(timespec="seconds"),
                "assets_hash": row["assets_hash"], "units": [], "skipped": []}
    seen_units = {}
    for asset in rec.get("assets") or []:
        url, kind, desc = asset.get("url") or "", asset.get("type"), asset.get("description") or ""
        if kind not in ("smart_contract", "blockchain_dlt"):
            manifest["skipped"].append({"url": url, "why": f"asset type {kind} is outside this tool"})
            continue
        gp = gh_parts(url)
        if gp:
            owner, repo, ref, path = gp
            key = f"{owner}/{repo}@{ref or 'HEAD'}"
            unit = seen_units.get(key)
            if not unit:
                dest = os.path.join(d, "src", f"{owner}__{repo}@{(ref or 'HEAD')[:12]}")
                sha = clone_at(owner, repo, ref, dest)
                unit = {"unit": os.path.basename(dest), "kind": "github", "repo": f"{owner}/{repo}", "ref": ref,
                        "sha": sha, "ok": bool(sha), "paths": [], "descriptions": []}
                seen_units[key] = unit
                manifest["units"].append(unit)
                log(f"[{slug}] github {key}: {'ok ' + (sha or '')[:10] if sha else 'FAILED'}")
            if path:
                unit["paths"].append(path)
            unit["descriptions"].append(desc)
            continue
        m = re.match(r"https?://([^/]+)/(?:address|token|contract)/(0x[0-9a-fA-F]{40})", url)
        if m:
            host, addr = m.group(1), m.group(2).lower()
            key = f"{host}:{addr}"
            if key in seen_units:
                continue
            name = os.path.join(d, "src", f"{host.split('.')[0]}__{addr[:10]}")
            unit = {"unit": os.path.basename(name), "kind": "explorer", "host": host, "address": addr,
                    "description": desc, "ok": False}
            seen_units[key] = unit
            manifest["units"].append(unit)
            try:
                mpath_u = os.path.join(name, "meta.json")
                if os.path.exists(mpath_u):
                    meta = json.load(open(mpath_u))
                    cname = meta.get("name", "")
                    unit.update(ok=True, note="cached", name=cname, compiler=meta.get("compiler"), proxy=meta.get("proxy"),
                                implementation=meta.get("implementation"))
                else:
                    cname, files, meta = explorer_source(host, addr)
                    os.makedirs(name, exist_ok=True)
                    for p, content in files.items():
                        fp = os.path.join(name, p)
                        os.makedirs(os.path.dirname(fp), exist_ok=True)
                        open(fp, "w").write(content)
                    meta = {"name": cname, **meta, "address": addr, "host": host, "description": desc}
                    json.dump(meta, open(mpath_u, "w"), indent=1)
                    write_foundry_toml(name, meta, files.get("__settings.json"))
                    unit.update(ok=True, name=cname, compiler=meta.get("compiler"), proxy=meta.get("proxy"),
                                implementation=meta.get("implementation"), files=len(files))
                    log(f"[{slug}] {host} {addr[:10]} {cname}: {len(files)} files")
                impl = (meta.get("implementation") or "").lower()
                if impl.startswith("0x") and len(impl) == 42 and not os.path.exists(os.path.join(name, "impl", "meta.json")):
                    iname, ifiles, imeta = explorer_source(host, impl)
                    idir = os.path.join(name, "impl")
                    for p, content in ifiles.items():
                        fp = os.path.join(idir, p)
                        os.makedirs(os.path.dirname(fp), exist_ok=True)
                        open(fp, "w").write(content)
                    json.dump({"name": iname, **imeta, "address": impl, "host": host, "proxy_of": addr},
                              open(os.path.join(idir, "meta.json"), "w"), indent=1)
                    write_foundry_toml(idir, imeta, ifiles.get("__settings.json"))
                    unit.update(impl_name=iname, impl_files=len(ifiles), impl_compiler=imeta.get("compiler"))
                    log(f"[{slug}]   -> implementation {impl[:10]} {iname}: {len(ifiles)} files")
                elif impl and os.path.exists(os.path.join(name, "impl", "meta.json")):
                    unit.update(impl_name=json.load(open(os.path.join(name, "impl", "meta.json"))).get("name"))
            except Exception as e:
                unit["error"] = str(e)
                log(f"[{slug}] {host} {addr[:10]}: {e}")
            continue
        manifest["skipped"].append({"url": url, "why": "not a GitHub or explorer address URL (docs page or unknown host)"})
    json.dump(manifest, open(os.path.join(d, "manifest.json"), "w"), indent=1)
    ok = sum(1 for u in manifest["units"] if u.get("ok"))
    log(f"[{slug}] pulled {ok}/{len(manifest['units'])} code units; {len(manifest['skipped'])} assets skipped; rules in {d}/RULES.md")


# ---------------------------------------------------------------- cards

def cards():
    out = {}
    for p in sorted(glob.glob(os.path.join(PATTERNS, "*.json"))):
        c = json.load(open(p))
        out[c["id"]] = c
    return out


def detector_index(cs):
    idx = {}
    for cid, c in cs.items():
        for tool, ids in (c.get("detectors") or {}).items():
            for did in ids:
                idx.setdefault((tool, did.lower()), []).append(cid)
    return idx


def applicable(card, product_types):
    cats = card.get("categories") or []
    return not cats or bool(set(cats) & set(product_types or [])) or "*" in cats


# ---------------------------------------------------------------- run

def have(tool):
    return shutil.which(tool) is not None


def solc_pragma(unit_dir):
    vers = []
    for p in glob.glob(os.path.join(unit_dir, "**", "*.sol"), recursive=True)[:400]:
        try:
            head = open(p, errors="replace").read(4000)
        except OSError:
            continue
        for m in re.finditer(r"pragma\s+solidity\s+([^;]+);", head):
            vers.append(m.group(1).strip())
    exact = [re.sub(r"[\^>=~ ]", "", v) for v in vers if re.match(r"^\s*=?\s*0\.\d+\.\d+\s*$", v)]
    caret = [re.sub(r"[\^>=~ ]", "", v.split()[0]) for v in vers if v.startswith("^") or v.startswith(">=")]
    pick = max(exact or caret or ["0.8.20"], key=lambda v: [int(x) for x in re.findall(r"\d+", v)[:3]])
    return pick if re.match(r"^0\.\d+\.\d+$", pick) else "0.8.20"


def ensure_solc(version):
    if not have("solc-select"):
        return False
    r = subprocess.run(["solc-select", "versions"], capture_output=True, text=True)
    if version not in r.stdout:
        subprocess.run(["solc-select", "install", version], capture_output=True, text=True, timeout=600)
    subprocess.run(["solc-select", "use", version], capture_output=True, text=True)
    return True


def run_slither(unit_dir, out):
    is_foundry = os.path.exists(os.path.join(unit_dir, "foundry.toml"))
    is_hh = any(os.path.exists(os.path.join(unit_dir, f)) for f in ("hardhat.config.js", "hardhat.config.ts"))
    target = "." if (is_foundry or is_hh) else "."
    env = dict(os.environ)
    cmd = ["slither", target, "--json", out, "--exclude-informational", "--exclude-optimization"]
    if not (is_foundry or is_hh):
        # explorer dump: flat tree of .sol files, compile with solc directly and remap nothing
        cmd += ["--solc-disable-warnings"]
    if is_foundry and not have("forge"):
        return False, "foundry project but `forge` is not installed (curl -L https://foundry.paradigm.xyz | bash; foundryup)"
    r = subprocess.run(cmd, cwd=unit_dir, capture_output=True, text=True, env=env, timeout=1800)
    ok = os.path.exists(out)
    return ok, (r.stderr or r.stdout)[-800:]


def parse_slither(out):
    try:
        j = json.load(open(out))
    except (OSError, ValueError):
        return []
    res = []
    for d in (j.get("results") or {}).get("detectors") or []:
        el = next((e for e in d.get("elements") or [] if e.get("source_mapping")), {})
        sm = el.get("source_mapping") or {}
        res.append(dict(check=d.get("check", ""), severity=(d.get("impact") or "").lower(),
                        confidence=(d.get("confidence") or "").lower(), file=sm.get("filename_relative") or "",
                        line=(sm.get("lines") or [0])[0], text=(d.get("description") or "").strip()[:600]))
    return res


def run_aderyn(unit_dir, out):
    r = subprocess.run(["aderyn", ".", "-o", out, "--skip-update-check"], cwd=unit_dir, capture_output=True, text=True, timeout=1800)
    return os.path.exists(out), (r.stderr or r.stdout)[-800:]


def parse_aderyn(out):
    try:
        j = json.load(open(out))
    except (OSError, ValueError):
        return []
    res = []
    for sev in ("high_issues", "low_issues"):
        for iss in (j.get(sev) or {}).get("issues") or []:
            for inst in iss.get("instances") or []:
                res.append(dict(check=iss.get("detector_name") or iss.get("title", ""), severity="high" if sev == "high_issues" else "low",
                                confidence="medium", file=inst.get("contract_path", ""), line=inst.get("line_no", 0),
                                text=(iss.get("description") or iss.get("title") or "")[:600]))
    return res


def run_semgrep(unit_dir, out):
    r = subprocess.run(["semgrep", "scan", "--config", "p/smart-contracts", "--json", "-o", out, "--quiet", "--metrics=off", "."],
                       cwd=unit_dir, capture_output=True, text=True, timeout=1800)
    return os.path.exists(out), (r.stderr or r.stdout)[-800:]


def parse_semgrep(out):
    try:
        j = json.load(open(out))
    except (OSError, ValueError):
        return []
    res = []
    for r in j.get("results") or []:
        ex = r.get("extra") or {}
        md = ex.get("metadata") or {}
        if (md.get("category") or "security") not in ("security", "correctness"):
            continue   # p/smart-contracts is mostly gas / style rules; keep the security ones
        res.append(dict(check=(r.get("check_id") or "").split(".")[-1], severity=(ex.get("severity") or "").lower(),
                        confidence=str(md.get("confidence") or "medium").lower(), file=r.get("path", ""),
                        line=(r.get("start") or {}).get("line", 0), text=(ex.get("message") or "")[:600]))
    return res


NOISE_DIRS = ("lib/", "node_modules/", "test/", "tests/", "script/", "scripts/", "mocks/", "mock/", "forge-std/", "openzeppelin")


def in_scope(unit, path):
    """Listed paths win when the program pins files; otherwise anything that isn't a dependency/test tree."""
    p = (path or "").lstrip("./")
    if not p.endswith((".sol", ".vy", ".go", ".rs")):
        return False
    if any(seg in p for seg in NOISE_DIRS) or p.endswith("__settings.json"):
        return False
    listed = [x.rstrip("/") for x in unit.get("paths") or [] if x]
    if listed:
        return any(p == x or p.startswith(x + "/") for x in listed)
    return True


def run_grep(unit_dir, out, cs=None, ptypes=None, unit=None):
    """Card regexes over in-scope source. Aggregated per (card, file): one lead naming which of the card's
    patterns matched and how often, so a file that touches several facets of a class rises to the top."""
    cs = cs or cards()
    files = [p for p in glob.glob(os.path.join(unit_dir, "**", "*"), recursive=True)
             if os.path.isfile(p) and in_scope(unit or {}, os.path.relpath(p, unit_dir))]
    compiled = {cid: [(pn, re.compile(rx)) for pn, rx in (card.get("grep") or {}).items() if _rx_ok(rx)]
                for cid, card in cs.items() if applicable(card, ptypes)}
    res = []
    for fp in files:
        rel = os.path.relpath(fp, unit_dir)
        base = os.path.basename(rel)
        if re.match(r"^I[A-Z]\w*\.sol$", base) or "/interfaces/" in f"/{rel}" or base.endswith(("Events.sol", "Errors.sol")):
            continue   # interfaces and event/error bags: nothing to read there
        try:
            lines = open(fp, errors="replace").read().splitlines()
        except OSError:
            continue
        for cid, pats in compiled.items():
            matched, first_line, total = {}, None, 0
            for pn, creg in pats:
                n = 0
                for i, line in enumerate(lines, 1):
                    if creg.search(line):
                        n += 1
                        if first_line is None:
                            first_line = (i, line.strip()[:160])
                if n:
                    matched[pn] = n
                    total += n
            if not matched:
                continue
            facets = len(matched)
            res.append(dict(check=cid, card=cid, severity="info", file=rel, line=first_line[0],
                            confidence="high" if facets >= 3 else "medium" if facets == 2 else "low",
                            text=f"{facets} facet(s), {total} match(es): " + ", ".join(f"{k} x{v}" for k, v in matched.items())
                                 + f" | first: {first_line[1]}"))
    json.dump(res, open(out, "w"))
    return True, f"{len(files)} files"


def _rx_ok(rx):
    try:
        re.compile(rx)
        return True
    except re.error:
        return False


def parse_grep(out):
    try:
        return json.load(open(out))
    except (OSError, ValueError):
        return []


NO_COMPILE = ("semgrep", "grep")

RUNNERS = {"slither": (run_slither, parse_slither, "slither.json"),
           "grep": (run_grep, parse_grep, "grep.json"),
           "aderyn": (run_aderyn, parse_aderyn, "aderyn.json"),
           "semgrep": (run_semgrep, parse_semgrep, "semgrep.json")}
CONF = {"high": 1.0, "medium": 0.7, "low": 0.4, "": 0.5}
SEV = {"critical": 1.0, "high": 1.0, "medium": 0.7, "error": 0.9, "warning": 0.6, "low": 0.4, "info": 0.2, "": 0.5}


def cmd_run(a):
    row, rec = program(a.ref)
    slug = row["slug"]
    d = pdir(slug)
    mpath = os.path.join(d, "manifest.json")
    if not os.path.exists(mpath):
        sys.exit(f"nothing pulled for {slug}; run `recon.py pull {slug}` first")
    manifest = json.load(open(mpath))
    cs = cards()
    idx = detector_index(cs)
    ptypes = json.loads(row["product_types"] or "[]")
    fit = (row["fit"] if row["fit"] is not None else row["signal"] or 50) / 100.0
    c = db()
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    wanted = a.tools.split(",") if a.tools else ("grep", "semgrep", "slither", "aderyn")
    tools = [t for t in wanted if t == "grep" or have(t)]
    missing = [t for t in wanted if t != "grep" and not have(t)]
    if missing:
        log(f"[{slug}] not installed, skipping: {', '.join(missing)}  (recon.py tools)")
    total = 0
    if a.force:
        c.execute("DELETE FROM leads WHERE slug=? AND status='new'", (slug,))
    for u in manifest["units"]:
        if not u.get("ok"):
            continue
        unit_dir = os.path.join(d, "src", u["unit"])
        outdir = os.path.join(d, "out", u["unit"])
        os.makedirs(outdir, exist_ok=True)
        stamp = os.path.join(outdir, "done.json")
        prev = json.load(open(stamp)) if os.path.exists(stamp) else {}
        if prev.get("assets_hash") == manifest["assets_hash"] and set(prev.get("tools", [])) >= set(tools) and not a.force:
            continue
        ver = solc_pragma(unit_dir)
        comp = u.get("impl_compiler") or u.get("compiler") or ""
        if u["kind"] == "explorer" and comp.startswith("v"):
            ver = comp.lstrip("v").split("+")[0]
        ensure_solc(ver)
        for tool in tools:
            runner, parser, fname = RUNNERS[tool]
            out = os.path.join(outdir, fname)
            if os.path.exists(out):
                os.remove(out)
            try:
                if tool == "grep":
                    ok, detail = runner(unit_dir, out, cs=cs, ptypes=ptypes, unit=u)
                else:
                    ok, detail = runner(unit_dir, out)
            except subprocess.TimeoutExpired:
                ok, detail = False, "timeout"
            hits = parser(out) if ok else []
            c.execute("INSERT INTO recon_runs VALUES (?,?,?,?,?,?,?)", (slug, u["unit"], tool, now, int(ok), len(hits), detail[-400:]))
            n = 0
            for h in hits:
                if not in_scope(u, h["file"]):
                    continue
                if h.get("card"):
                    card = h["card"]
                else:
                    card_ids = idx.get((tool, h["check"].lower())) or []
                    card_ids = [cid for cid in card_ids if applicable(cs[cid], ptypes)] or card_ids
                    card = card_ids[0] if card_ids else "uncarded"
                weight = cs[card]["weight"] if card in cs else 1
                base = 0.25 if tool == "grep" else 1.0
                rank = round(base * weight * CONF.get(h["confidence"], 0.5) * SEV.get(h["severity"], 0.5) * (0.5 + fit), 3)
                lid = hashlib.sha1(f"{slug}|{u['unit']}|{tool}|{h['check']}|{h['file']}|{h['line']}".encode()).hexdigest()[:10]
                c.execute("""INSERT INTO leads (id, program_id, slug, unit, tool, check_id, card, severity, confidence, file, line,
                             text, rank, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET rank=excluded.rank, last_seen=excluded.last_seen, text=excluded.text""",
                          (lid, row["id"], slug, u["unit"], tool, h["check"], card, h["severity"], h["confidence"], h["file"],
                           h["line"], h["text"], rank, now, now))
                n += 1
            total += n
            log(f"[{slug}] {u['unit'][:40]} {tool}: {'ok' if ok else 'FAILED'} {n} hits  {'' if ok else detail[-160:].replace(chr(10), ' ')}")
        json.dump({"assets_hash": manifest["assets_hash"], "tools": tools, "at": now, "solc": ver}, open(stamp, "w"))
        c.commit()
    log(f"[{slug}] {total} leads filed; `recon.py leads --program {slug}`")


# ---------------------------------------------------------------- leads

def fmt_lead(r, long=False):
    s = (f"{r['id']}  {r['rank']:>5.2f}  {r['status']:<9} {r['slug'][:14]:<14} {r['card'][:26]:<26} {r['tool']:<7} "
         f"{r['check_id'][:28]:<28} {os.path.basename(r['file'] or '')[:34]}:{r['line']}")
    if long:
        s += f"\n  unit: {r['unit']}\n  severity {r['severity']} / confidence {r['confidence']}\n  {r['text']}\n  notes: {r['notes'] or '-'}"
    return s


def cmd_leads(a):
    q, p = "SELECT * FROM leads WHERE rank >= ?", [a.min]
    if a.program:
        q += " AND (program_id LIKE ? OR slug=?)"
        p += [a.program + "%", a.program]
    if a.status:
        q += " AND status=?"
        p.append(a.status)
    else:
        q += " AND status NOT IN ('rejected','known')"
    if a.card:
        q += " AND card=?"
        p.append(a.card)
    q = (f"SELECT *, COUNT(*) AS n_units FROM ({q}) GROUP BY slug, tool, check_id, file, line "
         f"ORDER BY rank DESC, slug LIMIT ?")
    rows = db().execute(q, p + [a.n]).fetchall()
    for r in rows:
        print(fmt_lead(r) + (f"  x{r['n_units']}" if r["n_units"] > 1 else ""))
    if not rows:
        print("no leads (pull + run a program first)")


def cmd_lead(a):
    r = db().execute("SELECT * FROM leads WHERE id LIKE ?", (a.id + "%",)).fetchone() or sys.exit("not found")
    print(fmt_lead(r, long=True))
    cs = cards()
    if r["card"] in cs:
        card = cs[r["card"]]
        print(f"\n== card: {card['title']}  (weight {card['weight']})")
        print("impacts:", "; ".join(card.get("impacts") or []))
        print("confirm before spending time:")
        for item in card.get("confirm") or []:
            print("  -", item)
        for ref in card.get("references") or []:
            print("  ref:", ref)
    rules = os.path.join(RECON, r["slug"], "RULES.md")
    if os.path.exists(rules):
        txt = open(rules).read()
        ki = re.search(r"## Known issues.*?\n(.*?)\n## ", txt, re.S)
        print("\n== program known issues:", (ki.group(1).strip()[:800] if ki else "-"))
        print(f"full rules: {rules}")
    src = os.path.join(RECON, r["slug"], "src", r["unit"], r["file"] or "")
    if r["file"] and os.path.exists(src):
        lines = open(src, errors="replace").read().splitlines()
        lo, hi = max(0, (r["line"] or 1) - 6), min(len(lines), (r["line"] or 1) + 8)
        print(f"\n== {src}:{r['line']}")
        for i in range(lo, hi):
            print(f"{'>' if i + 1 == r['line'] else ' '} {i + 1:>5}  {lines[i]}")


def cmd_mark(a):
    if a.status not in LEAD_STATUSES:
        sys.exit(f"status must be one of {LEAD_STATUSES}")
    c = db()
    c.execute("UPDATE leads SET status=?, notes=coalesce(notes,'')||? WHERE id LIKE ?",
              (a.status, f"[{dt.date.today()}] {a.note or ''}\n", a.id + "%"))
    c.commit()
    print(a.id, "->", a.status)


def cmd_cards(a):
    for cid, c in cards().items():
        dets = ", ".join(f"{t}:{len(v)}" for t, v in (c.get("detectors") or {}).items())
        print(f"{cid:<30} w{c['weight']}  {c['title'][:44]:<44} cats={','.join(c.get('categories') or ['*'])[:30]:<30} {dets}")


def cmd_tools(a):
    for t in TOOLS:
        print(f"{t:<12} {shutil.which(t) or 'MISSING'}")
    print(f"{'forge':<12} {shutil.which('forge') or 'MISSING (needed only for Foundry-layout repos: foundryup)'}")
    print(f"{'ETHERSCAN_API_KEY':<12} {'set' if os.environ.get('ETHERSCAN_API_KEY') or hunt.cfg().get('bounty', {}).get('etherscan_api_key') else 'MISSING (explorer-hosted scope cannot be pulled)'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("pull").add_argument("ref")
    s = sp.add_parser("run")
    s.add_argument("ref")
    s.add_argument("--tools", help="comma list: slither,aderyn,semgrep")
    s.add_argument("--force", action="store_true")
    s = sp.add_parser("leads")
    s.add_argument("--program")
    s.add_argument("--status")
    s.add_argument("--card")
    s.add_argument("--min", type=float, default=0)
    s.add_argument("-n", type=int, default=60)
    sp.add_parser("lead").add_argument("id")
    s = sp.add_parser("mark")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("--note")
    sp.add_parser("cards")
    sp.add_parser("tools")
    a = ap.parse_args()
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
