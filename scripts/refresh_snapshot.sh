#!/bin/zsh
# Refresh the read-only SQLite snapshot that the Vercel dashboard serves.
#
#     ./scripts/refresh_snapshot.sh && git commit -m "Refresh snapshot" data/live.sqlite
#
# Two things here are load-bearing:
#
#  1. .backup (not cp) - the live DB is written by a running collector, so a
#     plain copy can capture a torn page mid-transaction.
#  2. journal_mode=delete - the live DB runs in WAL, and WAL *requires* creating
#     -wal/-shm sidecars beside the file even to read. Vercel's function
#     filesystem is read-only, so a WAL snapshot fails to open at all with
#     "unable to open database file" and every page renders its empty state.
set -eu

LIVE=${POLYARB_LIVE_DB:-/Users/shrishtiroy/polyarb-data/live.sqlite}
DEST=$(cd "$(dirname "$0")/.." && pwd)/data/live.sqlite
TMP=$(mktemp -t polyarb-snap)

trap 'rm -f "$TMP" "$TMP-wal" "$TMP-shm"' EXIT

sqlite3 "$LIVE" ".backup '$TMP'"
sqlite3 "$TMP" "pragma journal_mode=delete; vacuum;" > /dev/null

mode=$(sqlite3 "$TMP" "pragma journal_mode")
if [[ $mode == wal ]]; then
  echo "refusing to publish: snapshot is still in WAL mode" >&2
  exit 1
fi
[[ $(sqlite3 "$TMP" "pragma integrity_check") == ok ]] || {
  echo "refusing to publish: integrity_check failed" >&2; exit 1; }

mv -f "$TMP" "$DEST"
rm -f "$DEST-wal" "$DEST-shm"
echo "snapshot -> $DEST (journal=$mode, $(du -h "$DEST" | cut -f1))"
sqlite3 "$DEST" "select 'newest row: ' || datetime(max(ts),'unixepoch') from bs_measurements"
