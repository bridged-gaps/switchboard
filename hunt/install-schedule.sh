#!/bin/zsh
# Install a macOS launchd job that runs hunt at 7:30 and 13:00 daily.
#   ./install-schedule.sh                      install (uses your default Claude login)
#   HUNT_CLAUDE_CONFIG_DIR=~/.claude-me ./install-schedule.sh
#   ./install-schedule.sh uninstall
set -e
dir="${0:A:h}"
label="com.${USER}.hunt"
plist="$HOME/Library/LaunchAgents/$label.plist"
if [ "$1" = "uninstall" ]; then
  launchctl unload "$plist" 2>/dev/null || true; rm -f "$plist"; echo "removed $label"; exit 0
fi
mkdir -p "$dir/logs"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$dir/run.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>HUNT_CLAUDE_CONFIG_DIR</key><string>${HUNT_CLAUDE_CONFIG_DIR:-}</string></dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>13</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardErrorPath</key><string>$dir/logs/launchd.err</string>
</dict>
</plist>
PLIST
launchctl unload "$plist" 2>/dev/null || true
launchctl load "$plist"
echo "installed $label -> $plist  (run now: launchctl start $label)"
