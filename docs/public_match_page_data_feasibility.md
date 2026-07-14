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
| complete draft sequence | `PARTIAL` | action order/side/hero present, but `ordered_draft_actions` `0/12` | not normalized unless order, side, hero, and explicit kind or explicit phase/kind are present | Complete ordered pick/ban sequence is not proven. |
| captain/drafter identity | `MISSING` | no evidence | not implemented by current parser | Not required for the next ingestion stage. |
| player account/Steam identity | `SUPPORTED` | `player_account_ids` `12/12` | normalized as all-ten-player ID coverage | Stable account IDs are the anchor. |
| player display identity | `MISSING` | `player_display_names` `0/12` | checked separately from account IDs | Display names are not identity anchors. |
| player-to-team association | `DERIVABLE` | player side plus team identity `12/12` | derived through side/team mapping | Does not create organization aliases. |
| player-to-side association | `SUPPORTED` | `player_sides` `12/12` | normalized as all-ten-player side coverage | Suitable for Radiant/Dire draft mapping. |
| hero per player | `SUPPORTED` | `player_hero_ids` `12/12` | normalized as all-ten-player hero coverage | Hero IDs remain provider namespace values. |
| final K/D/A | `SUPPORTED` | kills/deaths/assists `12/12` | normalized as final player stat coverage | Post-game values only. |
| player slot/position semantics | `MISSING` | no lane/role/position evidence | not normalized as lane/role/position coverage | Radiant/Dire side support does not prove player role or lane semantics. |
| stable roster identity | `DERIVABLE` | player account IDs, sides, and team IDs `12/12` | derivable as lineup fingerprint | A lineup fingerprint is not a provider roster version ID. |
| substitutes/stand-ins | `MISSING` | no explicit evidence | not implemented by current parser | Must not be inferred from names alone. |
| final net worth | `SUPPORTED` | `final_net_worth` `12/12` | normalized as final stat coverage | Post-game context only. |
| gold | `PARTIAL` | GPM and net worth present; raw gold state not proven | not normalized as raw player gold | Related fields do not equal raw gold. |
| XP | `PARTIAL` | XPM and level present; raw XP state not proven | not normalized as raw player XP | Related fields do not equal raw XP. |
| GPM/XPM | `SUPPORTED` | `gpm` and `xpm` `12/12` | normalized as final stat coverage | Post-game summary rates. |
| item inventory | `SUPPORTED` | `final_items` `12/12` | normalized as final inventory coverage | Final inventory is not item timing. |
| neutral items | `SUPPORTED` | final inventory slot coverage `12/12` | covered through final item fields | Neutral item timing is not proven. |
| item purchase/timing history | `MISSING` | `timed_item_data` `0/12` | checked separately from final inventory; parser requires explicit item ID plus time pairs | Important limitation for rich state research. |
| item state over time | `MISSING` | no full inventory-over-time evidence | not normalized beyond timed item evidence | Important limitation for rich state research. |
| buybacks | `MISSING` | no evidence | not implemented by current parser | Not proven. |
| Radiant/Dire gold advantage progression | `SUPPORTED` | `gold_advantage_timeline` `12/12` | gold/net-worth advantage array coverage | Production ingestion preserves point-level time semantics. |
| XP advantage progression | `SUPPORTED` | `xp_advantage_timeline` `12/12` | XP advantage array coverage | Production ingestion preserves point-level time semantics. |
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

## 25. Feasibility Closeout Roadmap Step

Design the next STRATZ public-page stage as a deliberately bounded historical
trajectory backfill around this source contract.

Do not start a large crawl, trajectory model, Draft ML target change, or
bookmaker odds collection before the bounded backfill design and trajectory
corpus audit are complete.

## 26. Production-Shaped Ingestion Adapter

Post-feasibility adapter status: implemented behind
`sync-drafts --provider stratz-public`.

Progression recorded by the repository:

```text
OpenDota UA fix
        ->
OpenDota free-scale limitation
        ->
STRATZ GraphQL PERMISSION_RESTRICTED closeout
        ->
STRATZ public-page source contract
        ->
STRATZ_PUBLIC_SUFFICIENT
        ->
production-shaped public-page ingestion adapter
        ->
real Next Flight shallow-root regression found
        ->
shared page-to-semantics boundary
        ->
single live regression match INGESTED successfully
        ->
live idempotency: repeated match UNCHANGED
        ->
7-match real multi-family/source-shape canary
        ->
patches 177 / 180 / 182
families pgl / the_international / dreamleague
0 parse failures
0 critical invariant failures
7 storage successes
        ->
STRATZ_PUBLIC_READY_FOR_BOUNDED_BACKFILL
```

The adapter reuses the existing historical Dota game/draft storage instead of
creating a second historical database architecture. It stores public-page games
with provider/source provenance `stratz_public` and Valve match ID as
`source_game_id`.

Normalized ingestion boundary:

- match metadata: match ID, started/ended timestamps when direct or safely
  derivable, duration, winner, Radiant/Dire teams, patch/version, series,
  league/tournament fields when exposed, and map/game number when available;
- draft state: complete 5v5 composition is derived from player hero/side rows,
  while ordered draft rows are stored only when order, pick/ban kind, side, and
  hero are explicit;
- player final state: account ID, side/team association, hero, final K/D/A,
  economy/farm/damage summaries, and final inventory;
- advantage trajectory: gold and XP advantage points are preserved as curves,
  not reduced to final/max scalars.

Storage extension:

- `historical_dota_player_final_stats` stores one final-state row per
  game/account ID;
- `historical_dota_advantage_points` stores one point per game, metric, and
  source index.

Advantage trajectory semantics:

- every point keeps `source_index`;
- raw/source time evidence, when present, is preserved as `source_time_value`;
- `normalized_time_seconds` is set only when the source point itself exposes a
  confidently parseable time/second coordinate;
- `time_semantics_status` is `normalized_seconds` only for those points and
  `source_index_unstable` for number-only arrays.

This makes it explicit that an unstable public source index is not canonical
elapsed match seconds.

Idempotency and safe resume:

- game rows remain idempotent by `source + source_game_id`;
- repeated ingestion of unchanged normalized content reports `UNCHANGED`;
- stronger existing game metadata is not erased by later null/partial public
  fields;
- existing ordered draft rows are preserved when a later public page has only
  unordered derived composition;
- final player stats preserve stronger existing optional values during partial
  re-ingestion;
- final player stats are replaced per game/account set on re-ingestion rather
  than appended;
- gold and XP advantage points are replaced per game/metric/source-index set on
  re-ingestion rather than appended;
- one failed match returns a per-match failure outcome and does not roll back
  unrelated successful match ingestions.

Request behavior:

- ordinary public HTML only;
- project User-Agent;
- robots-policy check before match pages;
- explicit bounded match IDs;
- sequential fetching with conservative delay;
- bounded retry only for retryable transport/HTTP failures;
- no STRATZ token, GraphQL, login, Selenium, Playwright, JavaScript execution,
  proxies, CAPTCHA handling, or anti-bot evasion.

Successful one-match live regression retest:

| Match ID | Local evidence | Result |
| --- | --- | --- |
| `8886013461` | `data/autopilot.db` now has one `stratz_public` game, 10 player final-stat rows, 63 gold points, and 63 XP points. | Production extraction regression fixed for this match. |

Completed bounded live source-shape canary:

| Match ID | Family | Patch | Result |
| --- | --- | --- | --- |
| `8886013461` | `unknown` | `182` | `UNCHANGED`, 10 players, complete team identity, 63 gold and 63 XP points. |
| `8655240937` | `unknown` | `182` | `INGESTED`, 10 players, complete team identity, 38 gold and 38 XP points. |
| `8639790960` | `unknown` | `182` | `INGESTED`, 10 players, complete team identity, 22 gold and 22 XP points. |
| `8358745059` | `unknown` | `180` | `INGESTED`, 10 players, complete team identity, 48 gold and 48 XP points. |
| `8346430978` | `pgl` | `180` | `INGESTED`, 10 players, complete team identity, 38 gold and 38 XP points. |
| `8327632578` | `the_international` | `180` | `INGESTED`, 10 players, complete team identity, 26 gold and 26 XP points. |
| `8011794134` | `dreamleague` | `177` | `INGESTED`, 10 players, complete team identity, 33 gold and 33 XP points. |

Aggregate live evidence:

- requested/fetched pages: `7/7`;
- storage successes: `7`;
- parse failures: `0`;
- ingestion-critical invariant failures: `0`;
- explicit source patches: `177`, `180`, `182`;
- recognized families: `pgl`, `the_international`, `dreamleague`;
- repeated regression match idempotency: `8886013461` returned `UNCHANGED`;
- time semantics: advantage arrays remain `source_index_unstable`.

Current post-adapter decision:

`STRATZ_PUBLIC_READY_FOR_BOUNDED_BACKFILL`

This means the public STRATZ page adapter is sufficiently validated to design a
deliberately scoped historical trajectory backfill. It does not prove long-term
source stability, unlimited crawling, real-time feed support, production SLA,
complete objective/item/kill timelines, or historical bookmaker odds support.

Synthetic deterministic fixture canary:

| Fixture ID | Fixture family | Patch | Purpose |
| --- | --- | --- | --- |
| `8886013461` | Esports World Cup | `176` | EWC-shaped public page with complete team identity. |
| `7770000001` | DreamLeague | `175` | Non-EWC page shape with normalized advantage point time values. |
| `6660000001` | FISSURE Playground | `174` | Non-EWC page shape with partial team identity limitation. |

Deterministic fixture result:

- requested match pages: `3`;
- fetched fixture pages: `3`;
- parse failures: `0`;
- ingestion-critical invariant failures: `0`;
- storage successes: `3`;
- known-limitation cases: at least the partial-team-identity fixture;
- live request reporting: `LIVE_REQUEST_EXECUTED` when a match page receives an
  HTTP response;
- live canary evidence status: `LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE`.

Roadmap note:

The future trading objective is short-horizon in-play position trading over
approximately the next five minutes. Future decision semantics should distinguish
`OPEN`, `HOLD`, `CASH OUT`, and `FLIP`, rather than assuming every position is a
hold-to-final-result match-winner bet. Final match win probability may remain a
long-horizon context feature, auxiliary target, or baseline, but it is not
necessarily the primary trading target.

The future conceptual decomposition is:

1. game-state trajectory prediction;
2. market probability / odds reaction modeling;
3. position execution and cash-out policy.

STRATZ historical match trajectories provide game-state trajectory data. They do
not provide historical bookmaker probability or odds trajectories, so future
market-reaction or cash-out modeling requires a separate bookmaker odds-history
dataset or prospectively collected odds time series.

## 15. Bounded Trajectory Backfill Manifest and Corpus Audit

The source-validation stage is closed with:

`STRATZ_PUBLIC_READY_FOR_BOUNDED_BACKFILL`

The bounded source-readiness manifest is:

`stratz-public-trajectory-v1`

Manifest selection source:

- the 12-match EWC public-page smoke sample documented in this report;
- the 7-match multi-family/source-shape canary documented above;
- overlap removed;
- fixture-only IDs excluded.

Ordered manifest IDs:

```text
8011794134
8327632578
8346430978
8358745059
8639790960
8655240937
8885614030
8885633666
8885665054
8885723512
8885775271
8885784652
8885859920
8885871759
8885928262
8885974297
8886005043
8886013461
```

The manifest is deliberately bounded to `18` real repository-documented Valve
match IDs. At manifest selection time, `14` were already persisted as
`stratz_public` games and `4` were documented EWC smoke IDs not yet persisted.
The bounded live manifest run inserted those 4 rows; the current local corpus
has all `18` manifest IDs persisted. The broader `historical_matches` table
contains PandaScore provider match IDs, not Valve match IDs for STRATZ
`/match/<id>` ingestion.

Execution path:

```powershell
python -m app.cli sync-drafts --provider stratz-public --db data/autopilot.db --manifest stratz-public-trajectory-v1 --delay-seconds 1 --max-retries 1
```

The manifest path reuses `sync-drafts --provider stratz-public`; it does not add
a generic crawler. It remains explicit, deterministic, sequential, conservative,
resumable, and idempotent by `source + source_game_id`.

Read-only persisted corpus audit:

```powershell
python -m app.cli stratz-trajectory-audit --db data/autopilot.db
```

Current local audit summary:

- STRATZ public games: `18`;
- unique Valve/source game IDs: `18`;
- duplicate source game IDs: `0`;
- patch distribution: `177=1`, `180=3`, `182=14`;
- family distribution: `dreamleague=1`, `pgl=1`,
  `the_international=1`, `unknown=15`;
- games with 10 players: `18`;
- complete 5v5 compositions: `18`;
- complete team identity: `18`;
- games with both gold and XP curves: `18`;
- gold point-count distribution: min `22`, median `43`, p90 `71`, max `78`;
- XP point-count distribution: min `22`, median `43`, p90 `71`, max `78`;
- equal gold/XP point counts: `18`;
- malformed, duplicate, non-monotonic, or conflicting stored source indices:
  `0`;
- `source_time_value` points: `0`;
- `normalized_time_seconds` points: `0`;
- all `1,626` persisted trajectory points remain
  `source_index_unstable`.

Temporal coordinate conclusion:

`TRAJECTORY_TIME_SEMANTICS_UNRESOLVED`

Trajectory corpus architecture decision:

`STRATZ_TRAJECTORY_CORPUS_NEEDS_SOURCE_SEMANTICS_WORK`

Exact blocker:

`trajectory point coordinates are not confirmed as elapsed match time`

This means the corpus is useful as a preserved source-readiness corpus, but a
real `t -> t+5 minute` window dataset is still blocked. Do not infer elapsed
minutes or seconds from array index, curve length, or match duration unless a
future approved public embedded-state diagnostic proves that temporal mapping.

Next roadmap step:

Resolve or further evidence STRATZ public trajectory temporal-coordinate
semantics from the approved public embedded page state before defining real
`t -> t+5 minute` window labels.

## 16. Trajectory Time Semantics Diagnostic

The bounded trajectory-time diagnostic is implemented as:

```powershell
python -m app.cli stratz-trajectory-time-diagnostic --db data/autopilot.db --delay-seconds 1 --inspect-client-assets --max-client-assets 4
```

Default representative match IDs:

```text
8011794134
8346430978
8886013461
```

The diagnostic is read-only. It fetches only ordinary public STRATZ Overview
pages after the same robots-policy check used by the ingestion adapter, then
decodes the public page state without GraphQL credentials, STRATZ tokens,
login, browser automation, JavaScript execution, proxies, CAPTCHA handling, or
hidden endpoint enumeration.

The diagnostic checks:

- the public `/match/<id>` overview route;
- the robots-policy decision for `/matches/<id>/graphs/networth`, without
  fetching that disallowed graph route or depending on it;
- decoded public state counts;
- advantage source keys, source paths, raw shapes, first/last source indices,
  raw source-time values, normalized elapsed-second values, adjacent parent
  keys, and coordinate-candidate fields;
- optional bounded client asset snippets from same-origin
  `/_next/static/*.js` script and script-preload resources directly referenced
  by the allowed Overview HTML when `--inspect-client-assets` is passed;
- timed item rows, counted only when an explicit item ID and explicit time are
  associated with the same player/item row;
- ordered draft sequence support, counted only when action order, pick/ban
  kind, side, and hero are all explicit or safely derived from explicit
  phase/kind fields;
- persisted point-count versus duration relationships for the 18-match local
  corpus when `--db` points at an existing database.

The persisted corpus has an exact point-count relationship in the current local
database: for all 18 STRATZ public games, gold point count equals
`floor(duration_minutes) + 2` and `ceil(duration_minutes) + 1`. Gold and XP
point counts are equal for every persisted game. Seconds per gold interval are
roughly one minute across the corpus.

The repaired live diagnostic found five directly referenced public static
JavaScript assets in the allowed Overview HTML and inspected all five. None
contained either exact trajectory field identifier (`radiantNetworthLeads` or
`radiantExperienceLeads`), and no deterministic index-to-elapsed-time mapping
was present. No source maps, unreferenced chunks, or disallowed graph pages were
inspected.

The point-count relationship remains useful corroborating evidence, but it is
not sufficient proof of elapsed-time semantics. It does not prove the likely
`(source_index - 1) * 60` mapping: specifically, it does not prove the origin
offset, whether index `0` is pregame, horn, or minute zero, how short final
intervals are represented, or whether gold and XP curves always share exactly
the same coordinate contract. Therefore trajectory time semantics remain
unresolved unless ordinary public source points expose explicit time
coordinates or bounded public-client evidence proves the index-to-time mapping.

Current temporal decision remains:

`TRAJECTORY_TIME_SEMANTICS_UNRESOLVED`

Persisted point normalization remains unchanged:

- `source_index` is preserved;
- `source_time_value` remains `NULL` for number-only STRATZ public curves;
- `normalized_time_seconds` remains `NULL`;
- `time_semantics_status` remains `source_index_unstable`.

Trajectory normalization is allowed only after the source contract is proven by
public-page or public-client evidence. Until then, do not build real
`t -> t+5 minute` labels from STRATZ trajectory indices.
