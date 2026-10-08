#!/bin/zsh
# Scheduled entrypoint (launchd/cron). Fetch -> score -> enrich -> digest, logged per day.
# Set HUNT_CLAUDE_CONFIG_DIR to run `claude -p` under a specific Claude login
# (e.g. your personal one, never a client's).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
[ -n "$HUNT_CLAUDE_CONFIG_DIR" ] && export CLAUDE_CONFIG_DIR="$HUNT_CLAUDE_CONFIG_DIR"
cd "${0:A:h}" || exit 1
mkdir -p logs
exec >>"logs/$(date +%F).log" 2>&1
echo "=== run $(date) ==="
/usr/bin/env python3 hunt.py run
