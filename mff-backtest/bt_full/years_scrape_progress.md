# Discord historical options scrape progress

Date: 2026-09-06 (UTC-4)

## Scope and channel IDs

Read-only native Discord Search was used in the logged-in MoneyForFun Options KING server. Exact URLs are in `discord_channel_ids.json`.

| channel | channel ID | Discord search result count | earliest alert date observed | latest observed |
|---|---:|---:|---|---|
| options-king | 968352649437126676 | 4,971 | 2022-04-28 | 2026-09-03 |
| high-confidence-options | 1448448931116748993 | 253 | 2026-01-08 (channel intro Dec 2025) | 2026-09-03 |
| etf-options | 1231301953972207667 | 1,596 | 2024-04-22 | 2026-09-02 |
| platinum-options | 946975031399972884 | 2,741 | 2022-03-09 | 2026-09-03 |

Search pagination was used to jump to the oldest result pages (options page 199, high-confidence page 11, ETF page 64, platinum page 110). Visible entry/exit alerts were parsed and appended incrementally; usable target-channel rows from `trades_seed.csv` were merged and deduplicated.

## Output status

`trades_years.csv` has the required 11-column header and 59 deduplicated target-channel rows. Deduplication key: date+ticker+strike+side+entry_bid. Numeric exit percentages were only recorded when explicitly posted; otherwise `exit_pct` is blank and `outcome` is `unknown` unless a clear close/update was observed.

Rows currently span 2022-03-09 through 2026-09-03, including 18 rows with an explicit `exit_pct`.

## Blockers / limitations

- No authentication, permission, or login wall blocked access; the account could read all four target channels.
- Discord Search returned many results, but this run sampled visible alerts from newest/oldest result pages rather than exhaustively parsing every intermediate page.
- Some Discord search cards displayed `Message could not be loaded`; those cards were not treated as entries unless the visible text was sufficient.
- Discord's `before:` query was unreliable when combined with numeric channel IDs, so pagination was used instead.
- Excluded channels `lotto-options` and `lotto-friday` were not scraped or merged.

## Current CSV breakdown

- Rows by year: 2022=14, 2024=15, 2025=15, 2026=15 (no 2023 rows in the reachable/sample set).
- Rows by target channel: platinum-options=35, options-king=12, etf-options=8, high-confidence-options=4.
- Explicit exit_pct rows: 18; blank exit_pct rows: 41.
