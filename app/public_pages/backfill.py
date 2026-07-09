from __future__ import annotations

from dataclasses import dataclass


STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME = "stratz-public-trajectory-v1"
STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_VERSION = "1"

STRATZ_PUBLIC_LIVE_EWC_SMOKE_MATCH_IDS: tuple[str, ...] = (
    "8886013461",
    "8886005043",
    "8885974297",
    "8885928262",
    "8885871759",
    "8885859920",
    "8885784652",
    "8885775271",
    "8885723512",
    "8885665054",
    "8885633666",
    "8885614030",
)

STRATZ_PUBLIC_LIVE_SOURCE_SHAPE_CANARY_MATCH_IDS: tuple[str, ...] = (
    "8886013461",
    "8655240937",
    "8639790960",
    "8358745059",
    "8346430978",
    "8327632578",
    "8011794134",
)

STRATZ_PUBLIC_FIXTURE_ONLY_MATCH_IDS: tuple[str, ...] = (
    "7770000001",
    "6660000001",
)


@dataclass(frozen=True)
class StratzPublicBackfillManifest:
    name: str
    version: str
    selection_source: str
    match_ids: tuple[str, ...]
    selection_rule: str
    rationale: str
    excluded_match_ids: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.match_ids)


def available_stratz_public_backfill_manifest_names() -> tuple[str, ...]:
    return (STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME,)


def get_stratz_public_backfill_manifest(
    name: str = STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME,
) -> StratzPublicBackfillManifest:
    normalized_name = name.strip()
    if normalized_name != STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME:
        available = ", ".join(available_stratz_public_backfill_manifest_names())
        raise ValueError(
            f"Unknown STRATZ public backfill manifest: {name}. "
            f"Available manifests: {available}."
        )

    match_ids = tuple(
        sorted(
            set(STRATZ_PUBLIC_LIVE_EWC_SMOKE_MATCH_IDS)
            | set(STRATZ_PUBLIC_LIVE_SOURCE_SHAPE_CANARY_MATCH_IDS),
            key=int,
        )
    )
    return StratzPublicBackfillManifest(
        name=STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME,
        version=STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_VERSION,
        selection_source=(
            "Repository-documented live STRATZ public page evidence: "
            "12-match EWC smoke sample plus 7-match multi-family source-shape "
            "canary."
        ),
        match_ids=match_ids,
        selection_rule=(
            "Union documented live-probed Valve match IDs, exclude fixture-only "
            "IDs, then sort by numeric Valve match ID ascending."
        ),
        rationale=(
            "Use every locally approved real STRATZ public-page match ID while "
            "remaining deliberately bounded below broad-crawl scale."
        ),
        excluded_match_ids=STRATZ_PUBLIC_FIXTURE_ONLY_MATCH_IDS,
    )


def render_stratz_public_backfill_manifest(
    manifest: StratzPublicBackfillManifest,
) -> str:
    lines = [
        "STRATZ public trajectory backfill manifest",
        f"Name: {manifest.name}",
        f"Version: {manifest.version}",
        f"Selection source: {manifest.selection_source}",
        f"Selection rule: {manifest.selection_rule}",
        f"Match IDs: {manifest.size}",
        f"Ordered match IDs: {', '.join(manifest.match_ids)}",
        f"Excluded fixture-only IDs: {', '.join(manifest.excluded_match_ids)}",
        f"Rationale: {manifest.rationale}",
    ]
    return "\n".join(lines)
