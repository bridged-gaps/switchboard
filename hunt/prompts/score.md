You are screening job/contract postings for the candidate described in PROFILE below.
For EACH posting in JOBS, return one entry in `results` with:

- `id`: the posting id exactly as given.
- `track`: one of `w2`, `contract`, `freelance`, `lead`, `reject`.
  - `reject` = fails a hard constraint (not remote/US, too junior, wrong field, current employer, clearly below comp floor).
  - `lead` only when the posting reveals a company needing the kind of consulting the candidate's business offers
    (see PROFILE) and it is not a fit as a job.
- `fit` (0–100): how well the candidate's actual experience matches what the role needs. 85+ = core strengths
  (payments, fintech backends, blockchain, Go/C#). 60–84 = solid generalist senior backend match. <50 = weak.
  **Stack gates fit:** if the role's primary/required stack is outside the candidate's languages (see PROFILE's
  "Not my stack"), cap `fit` at 55 even when the domain is a great match — domain doesn't get them past a
  stack screen.
- `oe_compat` (0–100): per the work-style (OE) compatibility rules in PROFILE. Default ~60 when the posting says little.
- `red_flags`: short strings, each an OE or hard-constraint concern actually evidenced in the text. Empty if none.
- `why`: ONE sentence — the concrete reason it is or isn't a match.
- `angle`: ONE sentence — the strongest hook from the candidate's real history to lead an application with.
- `company_size`: `small` (<~200 people), `mid` (~200–1000), `large` (1000+, public, big tech, or a famous
  unicorn/exchange), or `unknown`. Use what you know about the company plus posting cues.
- `comp`: compensation as stated (normalized, e.g. "$180–220k base" or "$100/hr"), or "" if not stated.

Be strict and honest; do not inflate. Judge only from the text given.

# PROFILE
{profile}

# JOBS
{jobs}
