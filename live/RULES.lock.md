# LOCKED — MoneyForFun → Robinhood Agentic (do not change without PR)

Last locked: 2026-09-08

## Channels (include)
- options-king `968352649437126676`
- high-confidence-options `1448448931116748993`
- etf-options `1231301953972207667`
- platinum-options `946975031399972884`
Guild: `912747256736800838`

## Channels (exclude)
- lotto-options, lotto-friday, NKE, Fridays (live hone)

## Entry
- Account: Robinhood Agentic only (`717950448`)
- Limit: `min(live_ask, his_bid * 1.15)`
- Skip if already `>+25%` off his bid
- First trade of day: **1 contract** pipe test
- After clean pipe: king/etf/platinum **$1500** (add **$2250**); high-confidence **$3000** (add **$4500**)
- Max concurrent: **2**
- Add only if he posts added/avg — once
- No CoS buy/sell gate

## Exit
- Discord close/trim/%/stop/sold runners → trim to 1 + BE or flatten last
- Never chase a closed trade
- STOP/stand-down from user/CoS = halt

## Architecture requirement
Hot path must be event-driven (Discord Gateway), **not** LLM chat-queue cron.
