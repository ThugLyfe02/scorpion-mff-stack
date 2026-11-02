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
- Account: Robinhood Agentic only (`••••0448`)
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

## Forward paper rules — Options King stock trade (frozen 2026-11-01, fwd-v2-FINAL)

Machine-readable source for `scorpion.config.load_forward_rules`. Code must not hard-code these
values; `scorpion preflight` refuses to start if this block's canonical SHA-256 differs from
`scorpion.config.FORWARD_RULES_SHA256`. Evidence: edge_study oos116/oos128/oos130–oos145.
Paper only: there is no live order sink anywhere in this repository.

<!-- forward-rules:begin -->
```json
{
  "rules_version": "fwd-v2-FINAL (frozen 2026-11-01)",
  "channel": "options-king",
  "side": "call",
  "instrument": "underlying_shares",
  "excluded_tickers": ["QQQ", "SPY", "SPX", "TSLA", "NVDA"],
  "entry_delay_min": 2,
  "limit_window_min": 3,
  "chase_once": true,
  "skip_if_seen_after_min": 15,
  "hold_min": 60,
  "session_open_et": "09:30",
  "session_last_bar_et": "15:59",
  "exit_cap_et": "15:55",
  "latest_post_et": "15:00",
  "min_session_bars": 250,
  "trade_through_bps": 2,
  "cost_bps_round_trip": 10,
  "stop": "none",
  "target": "none",
  "trail": "none",
  "hedge": "none",
  "qqq_filter": "none",
  "shadow_track_from_et": "11:00",
  "main_tier_below_usd": 20,
  "sub10_below_usd": 10,
  "hold_to_close_shadow_tier": "sub20",
  "paper_notional_usd": 1000,
  "account_track_start_equity_usd": 150
}
```
<!-- forward-rules:end -->
