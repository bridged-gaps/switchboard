# hunt — find, score, and apply to remote engineering work

Find → score → draft → (you approve) → apply. Python 3.9+ standard library only. LLM calls go through
`claude -p` (Claude Code's headless mode), so they use your Claude subscription, not an API key.

```
sources ─▶ prefilter (title, remote+US, age) ─▶ SQLite dedupe
        ─▶ Claude scores fit + work-style compat + track (w2 | contract | freelance | lead | reject)
        ─▶ enrich: resolve aggregator jobs to the employer's own posting (ATS or email); rescore if richer
        ─▶ digests/YYYY-MM-DD.md + macOS notification
        ─▶ hunt.py draft <id>   tailored resume (md + pdf), cover/proposal, likely ATS answers
        ─▶ /apply <id>          Claude in Chrome fills the form and stops before Submit
```

## Setup

```sh
cp profile.example.md profile.md               # REQUIRED for useful scores
cp resume/master.example.md resume/master.md   # REQUIRED for drafts
cp config.example.json config.json             # optional; the example is used if missing
python3 hunt.py run
```

These personal files are gitignored. Until you create them, hunt falls back to the `*.example` templates.

- **`profile.md`**: the scorer judges every posting against this. Include your strengths, the stacks you *don't*
  have (the scorer caps fit when a role's primary stack isn't yours), rate floors, hard constraints, and red flags.
- **`resume/master.md`**: the only facts drafts may use. Drafts reorder and reframe but never invent
  employers, dates, metrics, or skills, and they flag gaps instead of hiding them.
- **`config.json`**: sources, target companies, filters, score thresholds, models.

## Daily use

```sh
python3 hunt.py list [--min 65] [--track contract]
python3 hunt.py show <id>
python3 hunt.py draft <id>                 # -> drafts/<id>-.../
python3 hunt.py mark <id> skipped|applied|interviewing|offer|rejected [--note "..."]
python3 hunt.py stats
```

Then in Claude Code (repo root): `/apply <id>`. Before filling anything, it lists the form's questions and
any deal-breakers (onsite weeks, rate below floor, and so on). It fills the form, then stops so you can review
and submit.

## Schedule (macOS)

```sh
./install-schedule.sh                                     # 7:30 and 13:00 daily
HUNT_CLAUDE_CONFIG_DIR=~/.claude-personal ./install-schedule.sh   # pin a specific Claude login
./install-schedule.sh uninstall
```

## Sources

| Source | Notes |
|---|---|
| HN "Who is hiring" | latest monthly thread, via the Algolia API |
| RemoteOK, Remotive, Himalayas, Jobicy, WeWorkRemotely | public APIs/RSS |
| Workable search | cross-company keyword search, lots of small/mid companies |
| Greenhouse / Lever / Ashby boards | per-company list in `config.json` |
| LinkedIn (opt-in) | logged-out public job search only, low volume. Add `"linkedin"` to `aggregators` to enable. Check LinkedIn's terms before using it. |

Sites that require logging in to automate (LinkedIn applications, Upwork, Indeed) are not scraped. For
LinkedIn Easy Apply, `/apply` works in *your* browser at human pace, and you submit.

## Adding a source

Write a function in `sources.py` that returns a list of `_job(...)` dicts, register it in `AGGREGATORS`
(or `ATS` for a per-company board), and add its name to `aggregators` in your config.
