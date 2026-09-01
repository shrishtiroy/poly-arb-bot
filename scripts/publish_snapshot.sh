#!/bin/zsh
# Periodic snapshot publish, driven by launchd.
#
# refresh_snapshot.sh alone only regenerates data/live.sqlite locally; the
# Vercel dashboard only picks up new data once that file is committed and
# pushed. This wrapper does both legs so the site can never again go stale
# just because nobody remembered to publish (as happened 2026-08-10 to
# 2026-08-31: the collector ran the whole time, nobody pushed the snapshot).
#
# Why refresh_snapshot.sh runs from a *staged copy* in $DATA instead of its
# real location under scripts/: ~/Documents is TCC-protected, and launchd's
# zsh cannot open() a .sh file there to interpret it (same restriction
# install_schedule.sh documents for collect_daily.sh) - it silently failed
# with "can't open input file" on every scheduled run for the first day
# (2026-09-01), even though a manual run from an interactive shell worked
# fine and made the bug easy to miss.
set -u

PROJECT=/Users/shrishtiroy/Documents/projects/poly-arb-bot
DATA=/Users/shrishtiroy/polyarb-data
LOG=$DATA/publish.log
LOCK=$DATA/publish.lock

cd "$PROJECT" || exit 1

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [publish] $1" >> "$LOG"; }

if [[ -f $LOG ]] && [[ $(stat -f%z "$LOG") -gt 52428800 ]]; then
  mv -f "$LOG" "$LOG.1"
fi

if ! /usr/bin/shlock -f "$LOCK" -p $$; then
  log "previous publish still running (pid $(cat "$LOCK" 2>/dev/null)); skipping this run"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT INT TERM

install -m 755 "$PROJECT/scripts/refresh_snapshot.sh" "$DATA/refresh_snapshot.sh"
if ! POLYARB_SNAPSHOT_DEST="$PROJECT/data/live.sqlite" "$DATA/refresh_snapshot.sh" >> "$LOG" 2>&1; then
  log "refresh_snapshot.sh failed; not publishing"
  exit 1
fi

if git diff --quiet -- data/live.sqlite; then
  log "snapshot unchanged; nothing to publish"
  exit 0
fi

git add data/live.sqlite
git commit -m "Refresh the Vercel snapshot (data through $(date '+%Y-%m-%d'))" >> "$LOG" 2>&1

# Vercel deploys production from main, not necessarily the branch this repo
# happens to be checked out on, so push both. (2026-08-31: pushes only went
# to the feature branch for three weeks and never reached main, so the site
# stayed on the 8/10 snapshot even though this script was "succeeding".)
ok=1
git push >> "$LOG" 2>&1 || ok=0
git push origin HEAD:main >> "$LOG" 2>&1 || ok=0
if [[ $ok == 1 ]]; then
  log "published snapshot"
else
  log "push failed"
  exit 1
fi
