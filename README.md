# switchboard

A small toolkit for independent engineers who work with several clients at once and are always lining up
the next engagement. Built around [Claude Code](https://claude.com/claude-code).

| Tool | What it does |
|---|---|
| [`work`](#work--one-isolated-claude-code-session-per-client) | Launch or attach one **isolated** Claude Code session per client: its own login, git identity, and SSH key. Drive them all from one place. |
| [`hunt/`](hunt/README.md) | Find remote contract/W2/freelance work, score each posting against *your* profile with Claude, draft tailored applications, and fill forms in your browser for you to submit. |

Everything runs locally on your machine. No server, no hosted service, no API key: LLM calls go through the
`claude` CLI on your own subscription.

---

## `work` — one isolated Claude Code session per client

```sh
cp clients.conf.example clients.conf    # one row per client
./work                                  # list clients
./work acme                             # start/attach tmux session running `claude --remote-control`
./work acme code                        # open VS Code in acme's workspace with acme's env
./work acme shell                       # plain shell with acme's env exported
```

Per client it sets:

- `CLAUDE_CONFIG_DIR` → `config/<slug>`: separate Claude login, MCP servers, credentials, history.
- `GIT_AUTHOR_*` / `GIT_COMMITTER_*`: commits can't land under the wrong identity.
- `GIT_SSH_COMMAND`: pushes use that client's key.

Sessions run with Remote Control on, so every client shows up in your [claude.ai/code](https://claude.ai/code)
session list. Check on and drive them from a browser or your phone, while each one stays in its own sandbox.

**Requirements:** bash, tmux (`brew install tmux`), the `claude` CLI, and optionally VS Code's `code` command.

> Respect your clients' policies. Don't run their code or credentials through tooling they haven't approved.
> Keep client-issued machines and accounts out of this setup.

---

## `hunt` — find and apply to work

```sh
cd hunt
cp profile.example.md profile.md               # who you are, constraints, what you want  ← most important
cp resume/master.example.md resume/master.md   # the only facts drafts may use
cp config.example.json config.json             # sources, filters, thresholds (optional)
python3 hunt.py run                            # fetch → score → enrich → digest
python3 hunt.py list
python3 hunt.py draft <id>                     # tailored resume (PDF), cover note, ATS answers
```

Then in Claude Code from the repo root: `/apply <id>`. It fills the application in your Chrome via
[Claude in Chrome](https://claude.ai/chrome) and **stops before Submit**. Full docs: [hunt/README.md](hunt/README.md).

---

## What's never committed

The `.gitignore` is an **allowlist**: everything is ignored unless explicitly listed. Your `clients.conf`,
`config/` (per-client logins), client workspaces, `hunt/profile.md`, resume, database, drafts, digests, and
logs all stay local. A new client folder can't be committed by accident.

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.
