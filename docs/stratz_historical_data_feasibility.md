# STRATZ Historical Game Data Feasibility

Probe date: 2026-07-08

Source: STRATZ free GraphQL API / Default Token

Repository HEAD used for this implementation: `5db9304 Add OpenDota project User-Agent`

This document records the repository-integrated feasibility probe for STRATZ
historical professional Dota game data. It is intentionally not a production
bulk sync adapter, database migration, Draft ML training stage, or betting
market model. The implementation is read-only and keeps all probe data in
process memory.

## Sample Selection

The command is:

```powershell
python -m app.cli probe-stratz-history --sample-size 12
```

It reads `STRATZ_TOKEN` from the environment and never accepts, prints, logs, or
persists the token. A missing token fails before network access with an
actionable message naming `STRATZ_TOKEN`.

Automatic professional-match discovery is intentionally schema-gated. If the
current live STRATZ schema does not expose a verified professional discovery
path, the command asks for repeated `--match-id` values instead of sending a
guessed query.

When a discovery path is verified in the live schema, selection should filter
candidates through the repository's centralized historical competition
classifier and scope policy. The selector prefers matches on or after
`2025-07-08T00:00:00Z`, excludes qualifiers through existing classifier
semantics, and round-robins across eligible families when possible:

- `THE_INTERNATIONAL`
- `ESPORTS_WORLD_CUP`
- `DREAMLEAGUE`
- `FISSURE_PLAYGROUND`
- `BETBOOM_DACHA`
- `BLAST`
- `PGL`
- `ESL`

Explicit samples can be supplied with repeated `--match-id` values when a human
has already identified stable STRATZ/Valve match IDs. The probe does not assume
PandaScore numeric match IDs are Valve/STRATZ match IDs and does not fuzzy-match
teams by name to force cross-provider linkage.

The current live smoke sample supplied by the user contains 12 EWC 2026 match
IDs. That sample is useful for validating STRATZ access behavior, but because
it is a single competition family, it is not final multi-family source
evidence.

Sampled match IDs from a real STRATZ run: none yet.

Tournament/league names from a real STRATZ run: none yet.

Competition families from a real STRATZ run: none yet.

Sample dates from a real STRATZ run: none yet.

## GraphQL Schema And Query Findings

The first authenticated live run exposed an implementation schema-assumption
failure. The invalid client query attempted `matches(request: ...)`, requested
`MatchType.tournament`, and also had a separate guessed `match(id: ...)` shape.
The live STRATZ endpoint returned GraphQL validation errors proving:

- `DotaQuery.matches` does not accept the guessed `request` argument;
- `DotaQuery.matches` requires `ids: [Long]!`;
- `MatchType.tournament` is not present;
- the live schema suggests `tournamentId` and `tournamentRound` exist.

That failure is client-side evidence, not source-capability evidence. It does
not prove STRATZ is insufficient.

The repaired probe is schema-first. It sends focused introspection before any
operational query:

- `__schema { queryType { name } }`;
- `__type(name: "DotaQuery")`;
- `__type(name: "MatchType")`;
- a bounded set of nested types returned by desired `MatchType` fields.

The implementation renders nested GraphQL type references such as `[Long]!`,
validates the `matches(ids: ...)` argument shape, filters desired `MatchType`
fields against introspected fields, and excludes unsupported fields from the
operational query. For example, when `tournament` is absent but `tournamentId`
and `tournamentRound` are present, the query requests only the latter fields
and reports that richer tournament metadata requires an additional public API
query.

The planned match query asks only for fields needed by the feasibility matrix:
match identity, timestamps, duration, winner, game mode, lobby/pro context,
league/tournament/series metadata, Radiant/Dire team identity, picks/bans,
player identities, heroes, basic post-game stats, final items, item timings,
parsed combat/objective events, and advantage timelines.

Current verified live findings from the user's real error:

- `DotaQuery.matches`: exists;
- `ids`: required argument on `matches`;
- `ids` type: `[Long]!`;
- `MatchType.tournament`: absent;
- `MatchType.tournamentId`: suggested by live schema validation;
- `MatchType.tournamentRound`: suggested by live schema validation.

Current verified live findings from the user's explicit-ID probe:

- requesting 12 match IDs in one `matches(ids: ...)` request returns
  `Requesting Too Many MatchIds. Max Request Size 10.`;
- requesting the first 10 IDs with the previously rich query returns
  `User is not an admin.`;
- the later minimal verified fetch path `matches(ids: [Long]!) { id }` also
  returns `User is not an admin.` with the user's Default Token;
- the minimal fetch result is `PERMISSION_RESTRICTED`, with `Samples: 0` and
  `Sample-fetch requests: 0`;
- schema presence is therefore not enough to prove field accessibility for the
  current STRATZ Default Token;
- the exact restricted field or subtree is not proven by that error alone.

The repaired probe now runs an access-capability phase before the rich sample
query. It first sends a minimal `matches(ids: ...) { id }` probe, then probes
semantic field groups, narrows permission failures to a top-level field or
nested subtree when possible, and builds the sample query only from fields that
are schema-present and access-proven for the current token.

Successful authenticated introspection from Codex: not available yet because
`STRATZ_TOKEN` is not visible in the Codex process.

Current GraphQL API path finding:
`STRATZ DEFAULT-TOKEN MATCH FETCH PERMISSION RESTRICTED`.

This is only a GraphQL API path finding. It is not evidence that STRATZ public
match pages lack data.

Authentication behavior from code:

- `Authorization: Bearer <token>` is used.
- `Accept: application/json`, `Content-Type: application/json`, and a project
  `User-Agent` are sent.
- HTTP 401/403, HTTP 429, transport failures, invalid JSON, unexpected JSON
  shapes, and GraphQL `errors` payloads are distinct failures.
- GraphQL HTTP 200 with `errors` is not treated as success.

Official public entry point checked for context:
`https://stratz.com/api`, which links to `https://api.stratz.com/graphiql`.

## Request And Rate-Limit Behavior

The probe is sequential. It does not fire concurrent requests, use proxies, retry
indefinitely, evade rate limits, or sleep for hours. The default delay between
match batches is one second.

The current observed STRATZ `matches(ids: ...)` request size limit is 10 match
IDs. Explicit `--match-id` samples are chunked into batches of at most 10 before
the sample fetch phase.

Real request count from a live run: none yet.

Observable real rate-limit metadata: none yet.

## Field Coverage Matrix

The implementation computes field-by-field counts, denominators, percentages,
classification, semantics, and point-in-time usage for these families:

- Match / map identity
- Team identity
- Player identity
- Hero / draft data
- Basic map outcome data
- Player economy / farm
- Combat / objectives
- Items
- Timeline
- Objectives

Supported coverage classifications:

- `ALWAYS_PRESENT`
- `PARTIAL`
- `ABSENT`
- `DERIVABLE_FROM_RETURNED_FIELDS`
- `REQUIRES_ADDITIONAL_PUBLIC_API_QUERY`
- `PROVIDER_DERIVED`
- `UNKNOWN_SEMANTICS`

Current real-source field coverage: unavailable pending a real STRATZ probe.

Coverage semantics distinguish schema absence from sample missingness. If a
desired field is absent from the verified current GraphQL type, its semantics
state that explicitly. If a field exists in the schema but sampled rows omit
values, that is treated as sample missingness rather than schema absence.
If a field exists in the schema but the current STRATZ Default Token receives a
permission error for that field or subtree, coverage marks it as access
restricted instead of schema absent or sample missing.

## Draft Completeness And Ordered Pick/Ban Findings

The probe separately measures:

- five Radiant picks;
- five Dire picks;
- exact complete 5v5 pick coverage;
- bans;
- ordered draft action sequence;
- action/order number;
- pick vs ban;
- action team/side;
- hero ID;
- first-pick side derived only from explicit ordered actions;
- draft completion status;
- duplicate/malformed draft behavior.

The probe does not sort picks by hero ID or player slot and call that draft
order. Ordered draft coverage requires explicit returned action order.

Current real-source draft findings: unavailable pending a real STRATZ probe.

## Identity Findings

The probe reports STRATZ match IDs, deterministic public match references,
team IDs, player/account IDs, hero IDs, league IDs, and series IDs when present.
It treats identities as provider-local unless a real source response and
provider semantics prove otherwise.

Cross-provider linkage limits:

- PandaScore numeric match IDs are not assumed to equal Valve/STRATZ match IDs.
- Team names are not converted into permanent organization aliases.
- Organization identity is kept separate from roster/player identity.
- Existing roster lineage semantics are not modified.

Current real-source stable identity findings: unavailable pending a real STRATZ
probe.

## Rich Game-Stat Findings

The coverage matrix separately checks:

- team kills / final score;
- individual kills, deaths, assists;
- duration and winner;
- final net worth;
- last hits and denies;
- GPM and XPM;
- level;
- hero damage, tower damage, and healing;
- final inventory, backpack, and neutral items;
- timed item data.

These fields are post-game values. They are not target-map POST_DRAFT features.
They may become labels/targets or prior-game historical context only under
strict point-in-time rules.

Current real-source rich stat findings: unavailable pending a real STRATZ probe.

## Timeline And Objective Findings

The probe separately checks:

- kill/combat event timeline;
- minute farm/economy timelines;
- net-worth, XP, or gold advantage timeline;
- towers and barracks;
- Roshan events;
- other building/objective events.

Timeline and parsed objective data is classified as provider-derived when
returned.

Current real-source timeline/objective findings: unavailable pending a real
STRATZ probe.

## Patch Provenance

The probe only accepts explicit provider patch/game version fields or trusted
provider version metadata. It never infers patch from date.

Current real-source patch/version provenance: unavailable pending a real STRATZ
probe.

## Point-In-Time And Leakage Classification

Important field families are classified by intended safe usage:

- Completed target-game draft:
  `TARGET_GAME_PRE_OUTCOME_INPUT`
- Match, team, player, hero, league, series, side, and patch identity:
  `IDENTITY_OR_CONTEXT`
- Target-game winner, duration, kills, farm, damage, objectives, item timings,
  and timelines:
  `POST_GAME_TARGET_OR_LABEL`
- The same post-game fields from games with `ended_at < target.started_at`:
  `PRIOR_GAME_HISTORICAL_CONTEXT_ONLY`
- Role/lane and organization-vs-roster semantics when unclear:
  `UNSUITABLE_OR_UNCLEAR`

Target-game final net worth, kills, duration, objectives, and player statistics
must not become POST_DRAFT prediction features.

## Known Missing Or Pending Fields

Pending successful real source verification:

- whether any practical professional discovery path exists in the current
  schema;
- any future free non-privileged GraphQL path that is explicitly documented and
  access-permitted;
- actual tournament coverage across allowed families beyond the current
  EWC-only smoke sample;
- exact match ID semantics, including Valve ID vs provider-specific ID;
- team ID organization-vs-roster semantics;
- explicit patch ID to human-readable patch name mapping;
- whether patch name requires an additional public metadata query;
- item timing, combat timeline, objective timeline, and advantage timeline
  coverage on free Default Token responses.

## Source Recommendation

Current recommendation: source role pending successful authenticated
representative probe. The first live failure was an implementation query-shape
bug, not evidence that STRATZ source capability is insufficient. The second live
failure proves an access boundary on the rich query, but not which field caused
it. The later minimal-match failure proves the Default Token cannot fetch even
`matches(ids) { id }`.

GraphQL API path verdict:
`STRATZ DEFAULT-TOKEN MATCH FETCH PERMISSION RESTRICTED`

Closeout decision: STRATZ Default Token GraphQL is closed for this project
stage. The verified minimal `matches(ids: [Long]!) { id }` path was
permission-restricted, so richer schema fields do not make the free/default
GraphQL path viable.

No further work in this stage should try to discover privileged STRATZ GraphQL
resolvers, admin-token workarounds, token spoofing, or paid-token paths. The
free-source pivot is tracked separately in
`docs/public_match_page_data_feasibility.md`.
