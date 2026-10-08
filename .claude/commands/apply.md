---
description: Fill a job application in Chrome from hunt drafts, stopping before submit
argument-hint: <job-id>
---

Apply to hunt job `$ARGUMENTS` — prepare everything; nothing is submitted without the user's explicit go-ahead for THIS application.

1. `cd hunt (relative to the repo root)`. Run `python3 hunt.py show $ARGUMENTS`. If there is no `drafts/$ARGUMENTS-*` dir, run
   `python3 hunt.py draft $ARGUMENTS` first.
2. Read the draft dir: `NOTES.md`, `cover.md`, `answers.md`, `resume.md`, and `../profile.md`. Show the user the
   notes, any `[CONFIRM]` items, and red flags; get their answers to `[CONFIRM]` items before filling. Let them edit
   the cover/resume if they want (re-render the PDF with
   `python3 -c "import hunt; hunt.md_to_pdf('<dir>/resume.md','<dir>/'+hunt.resume_pdf_name())"`).
3. Use the **Apply:** line from `hunt.py show` (the employer's own posting, found by `hunt.py enrich`). If it's
   missing, run `python3 hunt.py enrich` or look for the employer's careers page / ATS yourself — aggregator
   "apply" buttons (RemoteOK, Jobicy) often hit sign-up walls. `email:<addr>` means apply by email: make a Gmail
   draft to that address, following the posting's instructions (subject line, required phrases) exactly.
   Otherwise load the claude-in-chrome skill, open a NEW tab at that URL, and find the application form
   (Greenhouse / Lever / Ashby / company form). For HN posts, the "apply" path is usually an email or a link in the
   post text — for email, create a **Gmail draft** (never send) with the cover note and attach nothing; tell the
   user to attach the resume PDF from the draft dir.
   Before filling anything, list every form question and surface deal-breakers first (required travel/onsite
   onboarding, camera/monitoring, exclusivity, on-call) so the user can skip early instead of after a full fill.
4. Fill fields from profile + drafts. Upload the draft dir's `*_Resume.pdf` via file upload. Paste the cover note where
   there's a cover-letter field. Demographic / EEO questions: choose "decline to self-identify" unless the user says
   otherwise. Never invent an answer — if a required field isn't covered, ask.
5. If you hit a login wall, captcha, or an account-creation step: stop and hand over to the user.
6. **Stop before the final Submit.** Screenshot the filled form, summarize what you entered, and wait. Click
   Submit only if the user explicitly says to (e.g. "submit"); otherwise they click it themselves.
7. Once it's submitted, confirm the success page, then `python3 hunt.py mark $ARGUMENTS applied`.
