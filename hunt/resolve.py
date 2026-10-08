"""Find the employer's own posting for an aggregator job (HN, RemoteOK, Jobicy, ...).

Aggregator copies are often truncated and their "apply" buttons go through sign-up walls.
Resolution order:
  1. ATS boards linked from the posting text or the company's site (careers pages included)
  2. ATS boards guessed from the company name
  3. A page linked from the posting whose link text matches the role (e.g. /jobs/lead-engineer/)
Returns dict(apply_url, apply_method, description) or None. apply_method is one of
"ats:greenhouse|lever|ashby|workable", "email:<address>", "page".
"""
import re
import urllib.parse

import sources

STOP = {"senior", "sr", "remote", "usa", "us", "only", "100", "full", "time", "fulltime", "the", "a",
        "and", "of", "for", "in", "-", "ft", "contract", "hybrid", "role", "roles", "multiple", "staff"}
ATS_RE = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([a-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)"),
    ("workable", r"apply\.workable\.com/([a-z0-9_-]+)"),
]
EMAIL_RE = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}"


def tokens(s):
    return {t for t in re.findall(r"[a-z0-9#.+]+", (s or "").lower()) if t not in STOP and len(t) > 1}


def role_of(job):
    """HN titles are 'Company | Role | Location | ...' — pick the segment that names the role."""
    parts = [p.strip() for p in job["title"].split("|")]
    if len(parts) == 1:
        return job["title"]
    kw = ("engineer", "developer", "architect", "lead", "programmer", "swe")
    return next((p for p in parts[1:] if any(k in p.lower() for k in kw)), parts[1])


def similar(a, b):
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / max(1, len(ta | tb)) if ta and tb else 0


def _fetch(url):
    try:
        return sources._get(url)
    except Exception:
        return ""


def _workable(slug):
    d = sources._json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    return [dict(title=j["title"], url=j["url"],
                 description=sources.strip_html(j.get("description", "")) + "\n\n" +
                 sources.strip_html(j.get("requirements", "")))
            for j in d.get("jobs", [])]


def board_jobs(ats, slug):
    try:
        if ats == "workable":
            return _workable(slug)
        return [dict(title=j["title"], url=j["url"], description=j["description"])
                for j in sources.ATS[ats](slug)]
    except Exception:
        return []


def slug_guesses(company):
    base = re.sub(r"\(.*?\)|,? (inc|llc|ltd|corp|co)\.?$", "", (company or "").lower()).strip()
    words = re.findall(r"[a-z0-9]+", base)
    return list(dict.fromkeys(["".join(words), "-".join(words)] + (words[:1] if len(words) > 1 else [])))


def links(html_text, base):
    for _, href, text in re.findall(r"""<a[^>]+href=(["'])(.+?)\1[^>]*>(.*?)</a>""", html_text,
                                    flags=re.S | re.I):
        yield urllib.parse.urljoin(base, href), sources.strip_html(text)


def best_match(jobs, role, threshold=0.5):
    scored = sorted(((similar(role, j["title"]), j) for j in jobs), key=lambda x: -x[0])
    return scored[0][1] if scored and scored[0][0] >= threshold else None


def resolve(job):
    role = role_of(job)
    text = job.get("description") or ""
    # HN text glues link text to the next word (".../jobs/We're") — cut at a capitalized tail.
    urls = [re.sub(r"(/)[A-Z][A-Za-z]*$", r"\1", u.rstrip(".,)"))
            for u in re.findall(r"https?://[^\s<>\"']+", text)]
    # Company site: from posting URLs (non-aggregator domains) or guessed from the name.
    sites = [u for u in urls if not re.search(r"ycombinator|remoteok|jobicy|himalayas|remotive|"
                                                r"weworkremotely|linkedin|twitter|x\.com|github", u)]
    for g in slug_guesses(job["company"])[:2]:
        sites.append(f"https://{g}.com")
    pages = {}
    for u in list(dict.fromkeys(sites))[:4]:
        root = re.match(r"https?://[^/]+", u).group(0)
        for p in dict.fromkeys([u, root, root + "/careers", root + "/jobs"]):
            if p not in pages:
                pages[p] = _fetch(p)

    # 1 + 2: ATS boards found on pages or guessed from the name.
    boards = []
    blob = text + "\n" + "\n".join(pages.values())
    for ats, rx in ATS_RE:
        boards += [(ats, s) for s in re.findall(rx, blob, flags=re.I)]
    for g in slug_guesses(job["company"]):
        boards += [(ats, g) for ats in ("greenhouse", "lever", "ashby", "workable")]
    for ats, slug in dict.fromkeys(boards):
        m = best_match(board_jobs(ats, slug.lower()), role)
        if m:
            return dict(apply_url=m["url"], apply_method=f"ats:{ats}", description=m["description"])

    # 3: a page linked from the posting/company site whose link text matches the role.
    for base, html_text in pages.items():
        for href, label in links(html_text, base):
            if similar(role, label) >= 0.6:
                page = sources.strip_html(_fetch(href))
                if len(page) < 500:
                    continue
                how = re.search(r"(how to apply|to apply)(.{0,600})", page, flags=re.I | re.S)
                email = re.search(EMAIL_RE, how.group(2)) if how else None
                return dict(apply_url=href,
                            apply_method=f"email:{email.group(0)}" if email else "page",
                            description=page[:15000])
    # HN posts often just say "email X".
    m = re.search(r"(?:apply|email|send)[^.\n]{0,80}?(" + EMAIL_RE + ")", text, flags=re.I)
    if m:
        return dict(apply_url=job["url"], apply_method=f"email:{m.group(1)}", description=text)
    return None
