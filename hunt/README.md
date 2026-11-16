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

Or review in the browser: `python3 ui.py`, then open http://localhost:8765. You can work through the
inbox with the keyboard (j/k to move, s to shortlist, x to skip, d to draft, a to open the application),
read drafts with their [CONFIRM] items highlighted, copy form answers, and launch `/apply` in a Terminal.

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

## `bounty.py` — web3 bug bounty programs, filtered like Immunefi's panel

```sh
python3 bounty.py run            # fetch Immunefi's public feed → filter → local signal → Claude fit → list
python3 bounty.py list --changed # programs whose scope or rewards changed since the last fetch
python3 bounty.py show <id>
python3 bounty.py mark <id> watching|researching|submitted|paid|skipped --note "..."
```

Three sources, one table, each row tagged with a `track` (`crypto`, `fintech`, `c-cpp`, `web`, `ai`) so
`list --track fintech` is a queue of its own:

- **Immunefi** `https://immunefi.com/public-api/bounties.json`: every program with scope assets, reward table,
  KYC and PoC rules, audits and tags. `config.json` → `bounty` carries the same filters as the site's panel
  (ecosystem, language, product/program/project type, KYC) plus a minimum max-bounty and paused / invite-only
  exclusions. Finished audit competitions are dropped.
- **HackerOne** public directory over `hackerone.com/graphql`, no account needed: bounty stats, industry,
  response efficiency, structured scopes. `bounty.hackerone` filters by industry (web track only), top
  bounty and response efficiency; `c_cpp_handles` pins programs to the C/C++ track. Fintech is detected from
  industry and name.
- **Curated** off-platform programs listed by hand in `bounty.curated` (Chrome VRP, Google OSS VRP,
  OSS-Fuzz rewards, Mozilla, Microsoft, Apple, ZDI, GitHub Security Lab, AI programs). Static rows with
  notes, scored like the rest.

Scoring is tuned for the chance of a valid paid finding, not headline payout. For HackerOne rows the local
`signal` rewards responsive programs, healthy average bounties, code in scope and the fintech / C-C++ edge,
and penalises programs with thousands of resolved reports. For Immunefi rows it rewards
recently added scope, new programs, stale or missing audits, broad scope, escrowed (Vault) rewards and
mediums paid without a PoC, and penalises $1M+ crowd magnets, KYC, pay-to-submit and invite-only. Claude
then ranks the filtered set against `profile.md` with a `fit`, a one-line `angle` naming the scope area
where your experience applies, and `blockers` from the program's own rules.

Scope changes are detected per fetch (asset list hash, max bounty) and re-queue the program for scoring.

This is a research queue. Work inside each program's published rules: listed assets only, PoCs on a
local fork, disclosure through the platform.

## `recon.py` — pattern library over a program's in-scope code

```sh
python3 recon.py tools                       # slither / aderyn / semgrep / solc-select / forge, and the explorer key
python3 recon.py pull enzyme-onyx            # GitHub assets at the pinned commit, explorer assets as verified source
python3 recon.py run  enzyme-onyx            # detectors over every pulled unit → normalised leads
python3 recon.py leads --program enzyme-onyx # ranked: card weight × tool confidence × program fit
python3 recon.py lead <id>                   # context lines, the card's manual checklist, the program's known issues
python3 recon.py mark <id> reading|confirmed|rejected|known|reported --note "..."
```

`patterns/*.json` is the library: one card per vulnerability class with the Immunefi impacts it maps to, the
protocol categories it applies to, the detector ids per tool that hint at it, a set of `grep` review
heuristics (plain regexes that need no compiler, aggregated per file so a file touching several facets of a
class rises), a weight, and a manual checklist. Add a card whenever an audit report or paid writeup teaches you a class; detector hits that no
card claims land in `uncarded` at low weight. Runs are skipped until the program's scope hash changes, so
the daily `bounty.py fetch` plus a `recon.py run` on watched programs keeps new scope covered the day it lands.

Tools run in two tiers. `grep` and `semgrep` work on the pulled text and always run. `slither` and `aderyn`
need the unit compiled: GitHub units build with their own Foundry/Hardhat setup, explorer units get a
generated `foundry.toml` carrying the verified solc version, optimizer and remappings. Run those two
yourself the first time (`recon.py run <slug> --tools slither,aderyn`) and fix any missing submodule with
`git submodule update --init` inside the unit folder. Proxies are followed: the implementation's source
lands in `impl/` under the proxy's unit and is scanned with it.

Needs: `pipx install slither-analyzer solc-select semgrep`, `brew install cyfrin/tap/aderyn`, Foundry for
Foundry-layout repos, and `ETHERSCAN_API_KEY` (one free key covers every Etherscan-family chain) for
explorer-hosted scope; Blockscout explorers need no key.

Leads are reading orders, not findings. Only listed assets are pulled, nothing touches a chain RPC, and the
program's known issues and audits sit next to every lead so duplicates are caught before any time is spent.

## Adding a source

Write a function in `sources.py` that returns a list of `_job(...)` dicts, register it in `AGGREGATORS`
(or `ATS` for a per-company board), and add its name to `aggregators` in your config.
