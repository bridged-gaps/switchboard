# Contributing

Thanks for helping. Issues and PRs are welcome.

## Ground rules

- **Never commit personal data.** The `.gitignore` is an allowlist, so new files are ignored by default.
  If you add a file that *should* ship, add a `!` line for it, and double-check it has no names, emails,
  resumes, client names, or credentials. Use the `*.example` files for anything that needs sample data.
- **Stdlib only** for `hunt/` (Python 3.9+). It should run on a stock macOS `python3` with no `pip install`.
- **Keep the human in the loop.** Nothing submits an application or sends a message without the user's
  explicit go-ahead. Don't add sources that require logging in to automate a site whose terms forbid it.
- **Keep it honest.** Drafts must only use facts from the user's master resume.

## Good first contributions

- New job sources (another ATS, a niche board). See "Adding a source" in `hunt/README.md`.
- Linux scheduling (systemd timer / cron) to go with `install-schedule.sh`.
- A Gmail-alerts source that turns LinkedIn/Indeed alert emails into jobs.
- Follow-up reminders for applications that have gone quiet.

## Testing a change

```sh
cd hunt
python3 hunt.py fetch && python3 hunt.py score --limit 8 && python3 hunt.py list --min 0
```

Your local DB lives at `hunt/data/hunt.db` (gitignored). Delete it to start fresh.
