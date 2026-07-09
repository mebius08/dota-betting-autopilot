# Public Match Page Data Feasibility

Probe date: 2026-07-08

Repository HEAD/base stage: `5db9304 Add OpenDota project User-Agent`, with
uncommitted probe code in this stage.

Scope: read-only public professional Dota match page feasibility. No database
persistence, no raw HTML dumps, no page payload dumps, no browser automation,
no login, no Authorization header, no STRATZ token, no GraphQL credentials.

## 1. Source Policy Check

### STRATZ

Checked URL: `https://stratz.com/robots.txt`

Observed HTTP status: `200`

Relevant generic crawler rules observed:

- `Allow: /`
- `Allow: /matches/live`
- `Allow: /matches/graphs`
- `Disallow: /matches/*`

The probed public match page path is singular:
`/match/8886013461`.

The observed generic robots rules did not disallow `/match/<id>`. They did
disallow `/matches/*`, which is a different plural path.

Observed content signal:
`Content-Signal: search=yes,ai-train=no,use=reference`

No clearly linked current STRATZ terms/public-use page was fetched by the probe.
This document records observable source policy only and does not make a legal
conclusion.

### Sofascore Fallback

Checked URL: `https://www.sofascore.com/robots.txt`

Observed HTTP status: `403`

The response was blocked before robots rules could be read. Because the fallback
policy check did not complete with the honest project User-Agent, no Sofascore
page parser was built in this stage.

## 2. Exact Public-Page Sample IDs

The smoke sample used these EWC 2026 Valve match IDs:

- `8886013461`
- `8886005043`
- `8885974297`
- `8885928262`
- `8885871759`
- `8885859920`
- `8885784652`
- `8885775271`
- `8885723512`
- `8885665054`
- `8885633666`
- `8885614030`

These are EWC-only and therefore a smoke/coverage sample, not final
multi-family source evidence.

## 3. HTTP Behavior

Command:

```powershell
python -m app.cli probe-public-match-pages --source stratz --delay-seconds 1 --match-id 8886013461 --match-id 8886005043 --match-id 8885974297 --match-id 8885928262 --match-id 8885871759 --match-id 8885859920 --match-id 8885784652 --match-id 8885775271 --match-id 8885723512 --match-id 8885665054 --match-id 8885633666 --match-id 8885614030
```

Observed requests: `13` total, including one robots request and twelve match
page requests.

All 12 STRATZ public match URLs returned HTTP `200` with `text/html` and page
sizes roughly `657 KB` to `807 KB`.

No HTTP 403, HTTP 429, or page-not-found response occurred in the final probe.

## 4. Page Architecture

STRATZ public match pages are JavaScript application pages, but the initial HTML
delivers match data in a React/Next Flight stream:

`self.__next_f.push(...)`

The probe parses that embedded public page state without executing JavaScript.

No static `data-field` HTML fields were found.

No `__NEXT_DATA__`, Apollo cache, or directly referenced public JSON page-data
resource was needed for the final probe.

No hidden endpoint was guessed or enumerated.

## 5. Static HTML Findings

Static visible HTML did not carry the useful structured fields in a simple
`data-field` form.

Static title/meta content did show basic page identity such as the match ID, but
the feasibility matrix is based on structured public page state rather than
free-text title parsing.

## 6. Embedded Public State Findings

Embedded public state was found on all 12 sampled pages.

Provenance for parsed fields:
`EMBEDDED_PUBLIC_PAGE_STATE`

The parsed state included match identity, team identity, player account IDs,
hero IDs, side/orientation, complete 5v5 picks, explicit bans, duration,
winner, team kills, player KDA/economy/damage, final items, patch/game version,
series context, and advantage timelines.

## 7. Public Referenced-Resource Findings

No public referenced JSON/page-data resource was fetched in the final STRATZ
probe. The initial public HTML already delivered the parsed state required for
this feasibility stage.

## 8. Field Coverage Matrix

Coverage from 12 public STRATZ EWC pages:

- stable match ID: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- start timestamp: `0/12`, `NOT_FOUND`
- end timestamp: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- duration: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- winner side: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- Radiant/Dire orientation: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- league/event: `0/12`, `NOT_FOUND`
- series context: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- patch/game version ID: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- team IDs: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- team display names: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- player account IDs: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- player sides: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- player hero IDs: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- player display names: `0/12`, `NOT_FOUND`
- 5 Radiant picks: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- 5 Dire picks: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- complete 5v5 picks: `12/12`, `DERIVED_FROM_PUBLIC_FIELDS`
- bans: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- ordered draft actions: `0/12`, `NOT_FOUND`
- draft action order: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- draft action kind: `0/12`, `NOT_FOUND`
- draft action side: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- draft action hero ID: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- first-pick side: `0/12`, `NOT_FOUND`
- team kills/final score: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- individual kills: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- deaths: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- assists: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- final net worth: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- last hits: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- denies: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- GPM: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- XPM: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- level: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- hero/building/heal damage: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- final inventory/items: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- timed item data: `0/12`, `NOT_FOUND`
- kill events: `0/12`, `NOT_FOUND`
- minute farm/economy timeline: `0/12`, `NOT_FOUND`
- advantage timeline: `12/12`, `EMBEDDED_PUBLIC_PAGE_STATE`
- tower/barracks objectives: `0/12`, `NOT_FOUND`
- Roshan objectives: `0/12`, `NOT_FOUND`
- Tormentor objectives: `0/12`, `NOT_FOUND`

## 9. Draft Coverage

The public page state covers complete 5v5 hero picks for all 12 maps.

Radiant picks: `12/12`

Dire picks: `12/12`

Complete exact 5v5 picks: `12/12`

Bans: `12/12`

Draft data is usable as `TARGET_GAME_PRE_OUTCOME_INPUT` only for completed
current-game drafts at POST_DRAFT MAP prediction time.

## 10. Ordered Pick/Ban Coverage

The public state exposes draft action order, side, and hero ID for draft rows.

However, the probe did not verify a complete explicit ordered pick/ban sequence:

- ordered draft actions: `0/12`
- draft action kind: `0/12`
- first-pick side: `0/12`

The implementation does not call visual display order draft order. It only
counts ordered draft actions when explicit order and pick/ban semantics are both
present.

## 11. Team / Player / Hero Identity

Team IDs and names are present for both sides in all 12 maps.

Player stable account IDs are present for all ten players in all 12 maps.

Hero IDs are present for all ten players in all 12 maps.

Player display names were not found in the parsed public state; stable IDs are
therefore the identity anchor.

No organization aliases are created.

## 12. Kills / Stat Coverage

The public state covers:

- duration: `12/12`
- winner: `12/12`
- team kills/final score: `12/12`
- individual kills: `12/12`
- deaths: `12/12`
- assists: `12/12`
- final net worth: `12/12`
- last hits: `12/12`
- denies: `12/12`
- GPM: `12/12`
- XPM: `12/12`
- level: `12/12`
- hero damage / building damage / healing: `12/12`

These are post-game values. They are not target-map POST_DRAFT prediction
features.

## 13. Farm / Economy Coverage

Final net worth, last hits, denies, GPM, XPM, level, and advantage timeline
coverage is present for all 12 maps.

Minute farm timelines and player net-worth timelines were not proven in this
probe.

## 14. Item Coverage

Final inventory/item fields are present for all 12 maps.

Timed item acquisition/build-order data was not found by the current parser.

Final inventory is not treated as timed item data.

## 15. Objective Coverage

Tower/barracks, Roshan, Tormentor, and other objective event coverage was not
proven by the current parser.

This may be a parser gap or absent data in the delivered state; it should not be
promoted without additional evidence.

## 16. Timeline Coverage

Advantage timelines are present for all 12 maps.

Kill/combat timeline events and minute farm/economy timelines were not proven.

## 17. Patch Provenance

Patch/game version ID is present for all 12 maps.

Patch is accepted only when explicitly delivered by the public page state. The
probe never infers patch from date.

## 18. Access / Rate Observations

The STRATZ public-page probe used an honest project User-Agent:
`dota-betting-autopilot/1.0`

No Authorization header was sent.

No STRATZ token was read or used.

No rate limit was observed in the final 12-page probe with one-second sequential
pacing.

PowerShell ad-hoc fetches sometimes encountered Cloudflare challenge behavior,
but the Python probe with the project User-Agent successfully fetched all 12
pages as public HTML.

## 19. Source Fragility Risks

The parser depends on the public frontend's React/Next Flight stream shape.

Frontend framework changes, stream record changes, field renames, or bot
protection changes can break extraction.

This stage does not establish long-term crawler robustness, only current
public-page feasibility.

## 20. Leakage Semantics

For target prediction timestamp `T`, historical match influence requires:

`ended_at < T`

For POST_DRAFT MAP prediction, completed current-game draft may be input.

Target-game winner, kills, duration, net worth, objectives, player final stats,
final items, and timelines are post-game values. They may be labels/targets or
prior-game historical context only when strict chronology permits.

Future game drafts remain forbidden.

## 21. Comparison Candidate Status

Sofascore remains an unimplemented fallback comparison candidate. The initial
robots fetch returned HTTP `403`, so this stage did not proceed to page parsing.

Dotabuff is intentionally excluded from automated source work in this stage
because current public robots rules disallow generic crawler access to match
paths and related esports paths.

## 22. Formal Source Contract Classification

The public-page source contract uses these classifications:

- `SUPPORTED`: source state directly and consistently exposes enough evidence.
- `PARTIAL`: related evidence exists, but exact semantics or completeness are
  not enough for full support.
- `DERIVABLE`: the semantic value can be deterministically derived from
  supported fields without model inference.
- `MISSING`: the inspected public page state does not expose enough evidence.
- `UNSTABLE`: a value can be found, but the current extraction/semantics are too
  fragile or ambiguous for a stable contract without more safeguards.

Important contract rows from the 12-page EWC smoke sample:

| Semantic field | Classification | Source evidence | Current parser status | Caveat |
| --- | --- | --- | --- | --- |
| Valve/source match ID | `SUPPORTED` | `stable_match_id` `12/12` from embedded public page state | normalized as field coverage | Source-local identity only. |
| start time / timestamp | `DERIVABLE` | `end_timestamp` `12/12` plus `duration` `12/12`; direct `start_timestamp` `0/12` | direct field checked; derivation not normalized yet | Start time should be derived as `end - duration` when direct start is absent. |
| duration | `SUPPORTED` | `duration` `12/12` | normalized as field coverage | Post-game context/label only. |
| Radiant/Dire identity | `SUPPORTED` | `radiant_dire_orientation` `12/12` | normalized as side/orientation coverage | Preserve side through ingestion. |
| team identity | `SUPPORTED` | `team_ids` `12/12`, `team_display_names` `12/12` | normalized as IDs and names | STRATZ team IDs remain source-local. |
| winner/result | `SUPPORTED` | `winner_side` `12/12` | normalized as field coverage | Winner is a label, not a feature. |
| league or competition identity | `PARTIAL` | `league_event` `0/12`, but `series_context` `12/12` | checked separately from series context | Scope classification may need known match lists or normalized series/event extraction. |
| series identity | `SUPPORTED` | `series_context` `12/12` | presence-only coverage | Production mapping must split explicit series fields. |
| game/map number | `PARTIAL` | carried inside `series_context` evidence | not normalized as a standalone field | Needs a mapper-level extraction rule. |
| hero picks | `DERIVABLE` | `player_hero_ids`, `player_sides`, and side picks `12/12` | derived from per-player hero/side fields | This is complete side picks, not draft order. |
| pick order | `PARTIAL` | order, side, and hero ID present; complete ordered action semantics not proven | requires explicit action order and pick/ban kind | Display order is not promoted to semantic draft order. |
| bans | `SUPPORTED` | `bans` `12/12` | normalized as field coverage | Ban presence is separate from complete sequence support. |
| ban order | `PARTIAL` | ban evidence and action order evidence present | requires complete ordered draft action semantics | Order alone is insufficient without reliable action kind. |
| team/side ownership of picks | `SUPPORTED` | Radiant/Dire picks and player sides `12/12` | normalized as side pick coverage | Ownership is side-based. |
| team/side ownership of bans | `PARTIAL` | bans and action side evidence present | checked from draft action side and ban evidence | Depends on stable pick/ban action semantics. |
| complete draft sequence | `PARTIAL` | action order/side/hero present, but `ordered_draft_actions` `0/12` | not normalized unless all action semantics are explicit | Complete ordered pick/ban sequence is not proven. |
| captain/drafter identity | `MISSING` | no evidence | not implemented by current parser | Not required for the next ingestion stage. |
| player account/Steam identity | `SUPPORTED` | `player_account_ids` `12/12` | normalized as all-ten-player ID coverage | Stable account IDs are the anchor. |
| player display identity | `MISSING` | `player_display_names` `0/12` | checked separately from account IDs | Display names are not identity anchors. |
| player-to-team association | `DERIVABLE` | player side plus team identity `12/12` | derived through side/team mapping | Does not create organization aliases. |
| player-to-side association | `SUPPORTED` | `player_sides` `12/12` | normalized as all-ten-player side coverage | Suitable for Radiant/Dire draft mapping. |
| hero per player | `SUPPORTED` | `player_hero_ids` `12/12` | normalized as all-ten-player hero coverage | Hero IDs remain provider namespace values. |
| final K/D/A | `SUPPORTED` | kills/deaths/assists `12/12` | normalized as final player stat coverage | Post-game values only. |
| player slot/position semantics | `PARTIAL` | side/player-slot orientation is present; lane/role semantics not proven | side coverage only | Role/position fields need separate proof. |
| stable roster identity | `DERIVABLE` | player account IDs, sides, and team IDs `12/12` | derivable as lineup fingerprint | A lineup fingerprint is not a provider roster version ID. |
| substitutes/stand-ins | `MISSING` | no explicit evidence | not implemented by current parser | Must not be inferred from names alone. |
| final net worth | `SUPPORTED` | `final_net_worth` `12/12` | normalized as final stat coverage | Post-game context only. |
| gold | `PARTIAL` | GPM and net worth present; raw gold state not proven | not normalized as raw player gold | Related fields do not equal raw gold. |
| XP | `PARTIAL` | XPM and level present; raw XP state not proven | not normalized as raw player XP | Related fields do not equal raw XP. |
| GPM/XPM | `SUPPORTED` | `gpm` and `xpm` `12/12` | normalized as final stat coverage | Post-game summary rates. |
| item inventory | `SUPPORTED` | `final_items` `12/12` | normalized as final inventory coverage | Final inventory is not item timing. |
| neutral items | `SUPPORTED` | final inventory slot coverage `12/12` | covered through final item fields | Neutral item timing is not proven. |
| item purchase/timing history | `MISSING` | `timed_item_data` `0/12` | checked separately from final inventory | Important limitation for rich state research. |
| item state over time | `MISSING` | no full inventory-over-time evidence | not normalized beyond timed item evidence | Important limitation for rich state research. |
| buybacks | `MISSING` | no evidence | not implemented by current parser | Not proven. |
| Radiant/Dire gold advantage progression | `SUPPORTED` | `advantage_timeline` `12/12` | presence-only timeline coverage | Needs normalized point extraction. |
| XP advantage progression | `SUPPORTED` | `advantage_timeline` `12/12` | presence-only timeline coverage | Gold and XP arrays are not split in the current matrix. |
| time-series timestamps/resolution | `UNSTABLE` | advantage timeline exists, but resolution semantics are not normalized | presence-only; no timestamp/interval metadata | Validate resolution before stable ingestion. |
| kill progression | `MISSING` | `kill_events` `0/12` | requires timed kill event rows | Final kills are supported, kill timeline is not. |
| player death events | `MISSING` | no timed combat events | requires timed combat event rows | Final deaths are supported, death events are not. |
| objective progression | `MISSING` | tower/barracks/Roshan/Tormentor evidence `0/12` | requires objective state or timed objective rows | Important limitation for rich state research. |
| tower/building state | `MISSING` | `tower_barracks_objectives` `0/12` | checked as objective coverage | Not proven. |
| tower destruction timing | `MISSING` | no timed building events | requires timed building rows | Not proven. |
| barracks state/timing | `MISSING` | no barracks state/timing evidence | requires barracks state or timed building rows | Not proven. |
| Roshan events/timing | `MISSING` | `roshan_objectives` `0/12` | requires timed Roshan event rows | Not proven. |
| rune events | `MISSING` | no evidence | not implemented by current parser | Not proven. |
| teamfight/event timeline | `MISSING` | no combat timeline evidence | requires timed combat event rows | Not proven. |
| patch/game version | `SUPPORTED` | `patch_id` `12/12` | normalized as explicit patch/version coverage | Never infer patch by date. |
| final damage/healing/building stats | `SUPPORTED` | `damage` `12/12` | normalized as final stat coverage | Post-game summary values. |
| last hits and denies | `SUPPORTED` | `last_hits` and `denies` `12/12` | normalized as final stat coverage | Post-game summary values. |

## 23. Workload Suitability

| Workload | Suitability | Reasoning |
| --- | --- | --- |
| `POST_DRAFT win probability` | `SUFFICIENT_WITH_LIMITATIONS` | Completed 5v5 picks, side ownership, bans, teams, players, and result labels are supported or derivable. Complete ordered pick/ban sequencing and first-pick side remain partial/missing. |
| `PRE_MAP / historical features` | `SUFFICIENT_WITH_LIMITATIONS` | Match labels, teams, player account IDs, sides, roster fingerprints, and timestamps are available or derivable. League identity and display names are incomplete. |
| `live state estimation` | `INSUFFICIENT` | This is historical public-page data, not a verified real-time live feed. Advantage curves are present, but kill, objective, item-timing, and event timelines are insufficient for live-state model validation. |
| `cash-out policy research` | `SUFFICIENT_WITH_LIMITATIONS` | Advantage curves, drafts, final stats, and results can support coarse historical state-trajectory research. Historical bookmaker price/cash-out data remains outside this source, and objective/item event timing is missing. |
| `planned multi-step betting sequence research` | `SUFFICIENT_WITH_LIMITATIONS` | Draft context plus advantage curves can support early/late transition studies. Objective timing, item timing, kill events, and market-price history are not covered. |

## 24. Architecture Decision

`STRATZ_PUBLIC_SUFFICIENT`

The STRATZ public `/match/<Valve match id>` page source is sufficient to proceed
to a production historical ingestion/backfill design for the next historical
data stage. This decision is limited to public pages, not STRATZ GraphQL.

The decision does not claim production reliability from a 12-match sample. It
means the missing fields are not critical enough to justify implementing another
public-source feasibility probe before designing STRATZ public-page ingestion.
The ingestion design must carry the contract limitations above, especially:

- direct league/event coverage is partial;
- ordered pick/ban sequence semantics are partial;
- advantage timeline resolution is unstable until normalized;
- kill, objective, item-timing, buyback, rune, and teamfight event timelines are
  missing;
- historical bookmaker odds/cash-out prices are outside this source.

There are no critical gaps requiring a Dotabuff probe before the next
architecture stage. Dotabuff should remain a gap-filler candidate only if a
later ingestion or modeling stage proves one of the named limitations is
blocking.

## 25. Next Roadmap Step

Design the STRATZ public-page historical ingestion/backfill adapter around this
source contract, including a small multi-family canary before any large
backfill.

Do not add persistence, migrations, Draft ML rows, or training in this
feasibility closeout stage.
