#!/usr/bin/env bash
# CI guard: fail if forbidden artifacts are tracked, or if source contains scraping/spoofing
# markers or hard-coded credentials. Runs on tracked files only (git ls-files).
set -euo pipefail
status=0

tracked_bad="$(git ls-files | grep -E '(\.sqlite3?$|\.db$|\.db-(wal|shm)$|(^|/)cache/|(^|/)bars_cache/|\.dbn(\.zst)?$|\.parquet$|(^|/)raw_ticks/|(^|/)var/|discord[-_]export|(^|/)discord-history/)' || true)"
allow="$(dirname "$0")/hygiene_allowlist.txt"
if [[ -n "$tracked_bad" && -f "$allow" ]]; then
  grandfathered="$(grep -vxF -f <(grep -v '^#' "$allow") <<<"$tracked_bad" || true)"
  [[ "$grandfathered" != "$tracked_bad" ]] && echo "::warning::$(grep -cxF -f <(grep -v '^#' "$allow") <<<"$tracked_bad") grandfathered cache/ paths still tracked (see $allow)"
  tracked_bad="$grandfathered"
fi
if [[ -n "$tracked_bad" ]]; then
  echo "::error::forbidden artifacts are tracked (sqlite/cache/raw ticks/Discord exports):"
  echo "$tracked_bad"
  status=1
fi

# Scraping / spoofing / hard-coded credential markers in code and config.
pattern='(\.internal(/|:|\b)|Mozilla/5\.0|User-Agent["'"'"']?\s*[:=]|AUTH_TOKEN\s*=\s*["'"'"'][^"'"'"']+|EXTRACTION_SECRET|api\.robinhood\.com|db-[A-Za-z0-9]{20,})'
hits="$(git ls-files -- 'src/*' 'scripts/*' 'deploy/*' 'config/*' '.github/*' | grep -v 'scripts/check_repo_hygiene.sh' | xargs -r grep -nEI "$pattern" || true)"
if [[ -n "$hits" ]]; then
  echo "::error::scraping/spoofing/credential markers found:"
  echo "$hits"
  status=1
fi

# Discord-derived data already in the public tree is flagged (not silently removed): the owner
# must make the repository private first. Report only, so the flag is visible on every run.
for known in mff-backtest/bt_full/trades.json discord_channel_ids.json; do
  if git ls-files --error-unmatch "$known" >/dev/null 2>&1 || git ls-files | grep -q "/$known$"; then
    echo "::warning::Discord-derived file tracked in a PUBLIC repo: $known (owner decision pending)"
  fi
done
exit "$status"
