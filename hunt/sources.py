"""Job sources. Each fetcher returns a list of normalized job dicts:

    source, ext_id, url, title, company, location, salary, posted, description, kind_hint

Only public, unauthenticated endpoints that permit programmatic access.
LinkedIn/Upwork/Indeed are deliberately NOT scraped (ToS + account bans) —
their alert emails are a future Gmail-based source instead.
"""
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Macintosh; hunt/0.1 personal job search)"
TIMEOUT = 30


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _json(url):
    return json.loads(_get(url))


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(s)  # some APIs double-encode
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"</a>", " ", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _salary(lo, hi, cur="USD", period=""):
    if not lo and not hi:
        return ""
    fmt = lambda v: f"{int(v):,}" if v else "?"
    return f"{fmt(lo)}-{fmt(hi)} {cur or ''} {period or ''}".strip()


def _job(source, ext_id, url, title, company, location="", salary="", posted="",
         description="", kind_hint=""):
    return dict(source=source, ext_id=str(ext_id), url=url or "", title=(title or "").strip(),
                company=(company or "").strip(), location=(location or "").strip(),
                salary=salary or "", posted=posted or "", description=description or "",
                kind_hint=kind_hint)


# ---------------------------------------------------------------- aggregators

def hn_whos_hiring(cfg):
    """Latest 'Ask HN: Who is hiring?' thread; each top-level comment is a post."""
    hits = _json("https://hn.algolia.com/api/v1/search_by_date"
                 "?tags=story,author_whoishiring&hitsPerPage=10")["hits"]
    story = next(h for h in hits if h["title"].startswith("Ask HN: Who is hiring?"))
    thread = _json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out = []
    for c in thread.get("children", []):
        text = strip_html(c.get("text") or "")
        if not text:
            continue
        first = text.split("\n", 1)[0]
        parts = [p.strip() for p in first.split("|")]
        company = parts[0][:80] if parts else ""
        out.append(_job("hn", c["id"], f"https://news.ycombinator.com/item?id={c['id']}",
                        first[:200], company, location=first, posted=c.get("created_at", ""),
                        description=text))
    return out


def remoteok(cfg):
    data = _json("https://remoteok.com/api")
    out = []
    for j in data[1:]:  # [0] is a legal notice
        out.append(_job("remoteok", j["id"], j.get("url") or j.get("apply_url"),
                        j.get("position"), j.get("company"), j.get("location") or "Remote",
                        _salary(j.get("salary_min"), j.get("salary_max"), "USD", "annual"),
                        j.get("date"), strip_html(j.get("description")),
                        kind_hint=",".join(j.get("tags") or [])))
    return out


def remotive(cfg):
    data = _json("https://remotive.com/api/remote-jobs?category=software-dev")
    return [_job("remotive", j["id"], j["url"], j["title"], j["company_name"],
                 j.get("candidate_required_location"), j.get("salary"),
                 j.get("publication_date"), strip_html(j.get("description")),
                 kind_hint=j.get("job_type", ""))
            for j in data["jobs"]]


def himalayas(cfg):
    out = []
    for offset in range(0, 200, 20):  # API caps limit at 20
        jobs = _json(f"https://himalayas.app/jobs/api?limit=20&offset={offset}").get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            out.append(_job("himalayas", j.get("guid") or j.get("applicationLink"),
                            j.get("applicationLink"), j.get("title"), j.get("companyName"),
                            ", ".join(j.get("locationRestrictions") or []) or "Remote",
                            _salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency"),
                                    j.get("salaryPeriod")),
                            str(j.get("pubDate", "")),
                            strip_html(j.get("description") or j.get("excerpt")),
                            kind_hint=j.get("employmentType", "")))
    return out


def jobicy(cfg):
    data = _json("https://jobicy.com/api/v2/remote-jobs?count=100&industry=dev")
    return [_job("jobicy", j["id"], j["url"], j["jobTitle"], j["companyName"], j.get("jobGeo"),
                 _salary(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryCurrency"),
                         j.get("salaryPeriod")),
                 j.get("pubDate"), strip_html(j.get("jobDescription")),
                 kind_hint=",".join(j.get("jobType") or []) if isinstance(j.get("jobType"), list)
                 else str(j.get("jobType", "")))
            for j in data.get("jobs", [])]


def weworkremotely(cfg):
    out = []
    for cat in ("remote-back-end-programming-jobs", "remote-full-stack-programming-jobs",
                "remote-devops-sysadmin-jobs"):
        root = ET.fromstring(_get(f"https://weworkremotely.com/categories/{cat}.rss"))
        for it in root.iter("item"):
            t = it.findtext("title") or ""
            company, _, title = t.partition(": ")
            link = it.findtext("link") or ""
            out.append(_job("wwr", link, link, title or t, company,
                            it.findtext("region") or "Remote", "", it.findtext("pubDate"),
                            strip_html(it.findtext("description")),
                            kind_hint=it.findtext("type") or ""))
    return out


# ------------------------------------------------- direct company ATS boards

def greenhouse(slug):
    data = _json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [_job("greenhouse", j["id"], j["absolute_url"], j["title"],
                 j.get("company_name") or slug, (j.get("location") or {}).get("name", ""),
                 "", j.get("updated_at"), strip_html(j.get("content")))
            for j in data.get("jobs", [])]


def lever(slug):
    data = _json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        rng = j.get("salaryRange") or {}
        out.append(_job("lever", j["id"], j.get("hostedUrl"), j.get("text"), slug,
                        f"{cats.get('location', '')} {j.get('workplaceType', '')}".strip(),
                        _salary(rng.get("min"), rng.get("max"), rng.get("currency"),
                                rng.get("interval")),
                        str(j.get("createdAt", "")),
                        (j.get("descriptionPlain") or "") + "\n" + strip_html(
                            "".join(l.get("content", "") for l in j.get("lists", []))),
                        kind_hint=cats.get("commitment", "")))
    return out


def ashby(slug):
    data = _json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        loc = j.get("location", "")
        if j.get("isRemote") or j.get("workplaceType") == "Remote":
            loc = f"Remote ({loc})"
        out.append(_job("ashby", j["id"], j.get("jobUrl"), j.get("title"), slug, loc, comp,
                        j.get("publishedAt"), j.get("descriptionPlain") or "",
                        kind_hint=j.get("employmentType", "")))
    return out


def workable_search(cfg):
    """Cross-company Workable search (lots of small/mid companies). Keyword list in config."""
    import urllib.parse
    out, seen = [], set()
    for q in cfg.get("workable_queries", []):
        tok = None
        for _ in range(cfg.get("workable_pages", 3)):
            p = {"query": q, "workplace": "remote", "location": "United States"}
            if tok:
                p["pageToken"] = tok
            d = _json("https://jobs.workable.com/api/v1/jobs?" + urllib.parse.urlencode(p))
            for j in d.get("jobs", []):
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                co = j.get("company") or {}
                locs = [l for l in j.get("locations") or [] if l != "TELECOMMUTE"]
                desc = "\n\n".join(strip_html(j.get(k)) for k in
                                   ("description", "requirementsSection", "benefitsSection") if j.get(k))
                out.append(_job("workable", j["id"], j.get("url"), j.get("title"), co.get("title"),
                                "Remote; " + "; ".join(locs), "", j.get("updated") or j.get("created"),
                                desc, kind_hint=j.get("employmentType", "")))
            tok = d.get("nextPageToken")
            if not tok:
                break
    return out


def linkedin_guest(cfg):
    """LinkedIn's public (logged-out) job search — never touches the user's account.
    Low volume and polite: title-prefiltered before fetching details, 1 req/sec."""
    import time
    import urllib.parse
    pf = cfg["prefilter"]
    ok = lambda t: (any(k in f" {t.lower()} " for k in pf["title_include"])
                    and not any(k in f" {t.lower()} " for k in pf["title_exclude"]))
    cards = {}
    for q in cfg.get("linkedin_queries", []):
        for page in range(cfg.get("linkedin_pages", 2)):
            p = {"keywords": q, "f_WT": 2, "f_TPR": "r604800", "geoId": 103644278, "start": page * 10}
            s = _get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
                     + urllib.parse.urlencode(p))
            for block in s.split("</li>"):
                m = re.search(r"urn:li:jobPosting:(\d+)", block)
                if not m:
                    continue
                grab = lambda cls: strip_html((re.search(cls + r'[^"]*">(.*?)</', block, re.S) or
                                               re.search("()", "")).group(1))
                cards.setdefault(m.group(1), dict(title=grab("base-search-card__title"),
                                                  company=grab("base-search-card__subtitle"),
                                                  location=grab("job-search-card__location"),
                                                  posted=(re.search(r'datetime="([^"]+)"', block) or
                                                          re.search("()", "")).group(1)))
            time.sleep(1)
    out = []
    for jid, c in cards.items():
        if not c["title"] or not ok(c["title"]):
            continue
        try:
            s = _get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}")
        except Exception:
            continue
        time.sleep(1)
        m = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", s, re.S)
        easy = "apply-link-onsite" in s
        out.append(_job("linkedin", jid, f"https://www.linkedin.com/jobs/view/{jid}", c["title"],
                        c["company"], f"Remote; {c['location']}", "", c["posted"],
                        strip_html(m.group(1)) if m else "",
                        kind_hint="easy_apply" if easy else "offsite"))
    return out


AGGREGATORS = {
    "linkedin": linkedin_guest,
    "workable": workable_search,
    "hn": hn_whos_hiring,
    "remoteok": remoteok,
    "remotive": remotive,
    "himalayas": himalayas,
    "jobicy": jobicy,
    "wwr": weworkremotely,
}
ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
