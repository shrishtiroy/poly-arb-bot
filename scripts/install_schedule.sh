#!/bin/zsh
# Install the daily collection schedule as a launchd agent.
#
# Run this from an interactive shell (it needs your Terminal's Full Disk /
# Documents access) after changing collect_daily.sh:
#
#     ./scripts/install_schedule.sh
#
# Why the wrapper is copied out of the repo: ~/Documents is TCC-protected, and
# launchd-spawned processes can execute binaries there but cannot *read* files.
# zsh must read a script to run it, so the wrapper is staged in $DATA instead.
set -eu

PROJECT=/Users/shrishtiroy/Documents/projects/poly-arb-bot
DATA=/Users/shrishtiroy/polyarb-data
LABEL=com.shrishtiroy.polyarb-collect
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist

install -m 755 "$PROJECT/scripts/collect_daily.sh" "$DATA/collect_daily.sh"
echo "staged wrapper -> $DATA/collect_daily.sh"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "loaded $LABEL (fires daily at 00:05)"
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|runs" || true
