"""Validate and concatenate explicit POV death-label CSVs, without recomputation.

Cells are copied as strings, including empty cells and original timestamp
precision. Only the input order changes: match, actual time, Radiant then Dire.
CSV checks cannot re-prove raw visibility, event delivery or the confirmed match
end; those guarantees belong to the upstream replay_pov_features/future_deaths
exports. No split or model is created.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Sequence
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path

from app.replay_future_deaths import HORIZONS, LABEL_COLUMNS
from app.replay_pov_alignment import TEAMS, _features, _integer, _lineage
from app.replay_pov_features import TEAM_COLUMNS


COLUMNS = TEAM_COLUMNS + LABEL_COLUMNS
IDENTITY_COLUMNS = TEAM_COLUMNS[:5]
FEATURE_COLUMNS = TEAM_COLUMNS[5:]
TARGET_COLUMNS = tuple(f'{name}_next_{h}s' for h in HORIZONS
                       for name in ('own_deaths', 'enemy_deaths', 'any_death'))
AUDIT_COLUMNS = tuple(c for c in LABEL_COLUMNS if c not in TARGET_COLUMNS)
STATUSES = ('VALID', 'CENSORED', 'AMBIGUOUS_TIME_BOUNDARY')
CSV_NAME = 'pov-death-corpus.csv'


def _decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('Expected a finite nonnegative decimal') from exc
    if not number.is_finite() or number < 0:
        raise ValueError('Expected a finite nonnegative decimal')
    return number


def _labels(row: dict, h: int) -> tuple:
    status = row[f'label_status_{h}s']
    if status not in STATUSES:
        raise ValueError(f'Invalid label status: {status}')
    if _decimal(row[f'label_window_end_{h}s']) != _decimal(row['game_time_seconds']) + h:
        raise ValueError('Label window must end at actual t+h without rounding')
    count = _integer(row[f'label_ambiguous_event_count_{h}s'])
    events = json.loads(row[f'label_ambiguous_events_{h}s'])
    if not isinstance(events, list) or len(events) != count:
        raise ValueError('Invalid ambiguity audit')
    if (status == 'VALID' and count) or (status == 'AMBIGUOUS_TIME_BOUNDARY' and not count):
        raise ValueError('Label status contradicts ambiguity audit')
    values = tuple(row[f'{name}_next_{h}s'] for name in ('own_deaths', 'enemy_deaths', 'any_death'))
    if status == 'VALID':
        own, enemy, any_death = map(_integer, values)
        if any_death not in (0, 1) or any_death != int(own + enemy > 0):
            raise ValueError('any_death must agree with own/enemy deaths')
        values = (own, enemy, any_death)
    elif any(v != '' for v in values):
        raise ValueError('Non-VALID targets must be empty, including CENSORED')
    return status, values, count, events


def build_corpus(input_paths: Sequence[Path], output_dir: Path) -> dict:
    """Reject incompatible/duplicate data before writing either output file."""
    paths = [Path(p) for p in input_paths]
    resolved = [p.resolve() for p in paths]
    output = output_dir / CSV_NAME
    manifest_path = output_dir / 'manifest.json'
    if not paths or len(set(resolved)) != len(paths):
        raise ValueError('Provide a nonempty explicit list without duplicate input paths')
    if any(p.resolve() in resolved for p in (output, manifest_path)):
        raise ValueError('Corpus output must not overwrite an input')
    frames, lineages, replay_matches, sources = {}, {}, {}, []
    for path in paths:
        raw = path.read_bytes()
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8'), newline=''))
        if reader.fieldnames != list(COLUMNS):
            raise ValueError(f'Incompatible POV death-label schema: {path}')
        rows = list(reader)
        if not rows:
            raise ValueError(f'Empty corpus input: {path}')
        source_matches = set()
        for row in rows:
            if None in row or any(v is None for v in row.values()):
                raise ValueError('Malformed CSV row')
            mid, sha = _lineage(row)
            if lineages.setdefault(mid, sha) != sha or replay_matches.setdefault(sha, mid) != mid:
                raise ValueError('Match/replay identity conflict')
            time, tick, team = _decimal(row['game_time_seconds']), _integer(row['replay_tick']), row['pov_team']
            if team not in TEAMS:
                raise ValueError('Invalid POV team')
            _features(row)  # Validate types without converting stored feature cells.
            labels = {h: _labels(row, h) for h in HORIZONS}
            if labels[30][0] == 'CENSORED' and labels[60][0] != 'CENSORED':
                raise ValueError('Longer horizon cannot become uncensored')
            if all(labels[h][0] == 'VALID' for h in HORIZONS):
                if any(a > b for a, b in zip(labels[30][1], labels[60][1])):
                    raise ValueError('Death counts must not decrease across nested horizons')
            frame = frames.setdefault((mid, tick), {'time': time, 'teams': {}})
            if frame['time'] != time:
                raise ValueError('Team observations disagree on actual frame time')
            if team in frame['teams']:
                raise ValueError('Duplicate match/frame/team observation')
            frame['teams'][team] = (row, labels)
            source_matches.add(mid)
        sources.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                        'rows': len(rows), 'match_ids': sorted(source_matches)})

    rows, per_match, status_counts = [], {}, {h: Counter() for h in HORIZONS}
    previous, censored = {}, defaultdict(set)
    for (mid, tick), frame in sorted(frames.items(), key=lambda item: (item[0][0], item[1]['time'], item[0][1])):
        if set(frame['teams']) != set(TEAMS):
            raise ValueError('Every frame must have exactly one Radiant and one Dire row')
        time = frame['time']
        if mid in previous and (time <= previous[mid][0] or tick <= previous[mid][1]):
            raise ValueError('Match frame time and replay tick must strictly increase')
        previous[mid] = (time, tick)
        radiant, dire = (frame['teams'][t] for t in TEAMS)
        for h in HORIZONS:
            rs, rv, rc, re = radiant[1][h]
            ds, dv, dc, de = dire[1][h]
            if (rs, rc, re) != (ds, dc, de) or rv != (dv[1], dv[0], dv[2]):
                raise ValueError('Paired POV labels/status/audit disagree or own/enemy are not swapped')
            if h in censored[mid] and rs != 'CENSORED':
                raise ValueError('CENSORED must remain a tail within each match/horizon')
            if rs == 'CENSORED':
                censored[mid].add(h)
            status_counts[h][rs] += 2
        match = per_match.setdefault(str(mid), {'replay_sha256': lineages[mid], 'frames': 0, 'rows': 0,
                                                'label_statuses': {str(h): Counter() for h in HORIZONS}})
        match['frames'] += 1
        match['rows'] += 2
        for team in TEAMS:
            row, labels = frame['teams'][team]
            rows.append(row)
            for h in HORIZONS:
                match['label_statuses'][str(h)][labels[h][0]] += 1

    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    data = stream.getvalue().encode('utf-8')
    manifest = {
        'schema_version': 1, 'artifact_kind': 'team_pov_death_corpus',
        'columns': list(COLUMNS),
        'column_groups': {'identifiers_and_observation_time': list(IDENTITY_COLUMNS),
                          'features_at_t': list(FEATURE_COLUMNS), 'future_targets': list(TARGET_COLUMNS),
                          'future_label_metadata': list(AUDIT_COLUMNS)},
        'enum_values': {'pov_team': list(TEAMS), 'label_status': list(STATUSES)},
        'types': {c: ('sha256_hex' if c == 'replay_sha256' else
                      'positive_integer' if c == 'match_id' else
                      'team_enum' if c == 'pov_team' else
                      'binary_integer' if c.startswith('any_death_next_') else
                      'label_status_enum' if c.startswith('label_status_') else
                      'json_array' if c.startswith('label_ambiguous_events_') else
                      'categorical' if c.endswith('_status') else
                      'nonnegative_integer' if c.endswith('_count') or c in ('match_id', 'replay_tick')
                      or c in TARGET_COLUMNS or c.startswith('label_ambiguous_event_count_') else
                      'nonnegative_decimal') for c in COLUMNS},
        'counts': {'matches': len(per_match), 'frames': len(frames), 'team_rows': len(rows),
                   'label_statuses': {str(h): {s: status_counts[h][s] for s in STATUSES} for h in HORIZONS}},
        'matches': per_match, 'sources': sources,
        'csv': {'filename': CSV_NAME, 'sha256': hashlib.sha256(data).hexdigest()},
        'time_semantics': {'observation': 'actual normalized game time and source replay tick; no rounding',
                           'future_window': '(t,t+h]', 'horizons_seconds': list(HORIZONS)},
        'feature_policy': 'Exact existing POV whitelist; no targets, audit metadata, GLOBAL or hidden coordinates are features. Empty cells are preserved without imputation. visible_enemy_count counts raw flags; living positions use visible_enemy_with_position_count.',
        'validation_scope': 'CSV schema, types, lineage, paired POVs, temporal ordering, t+h, targets and censoring consistency. Raw visibility, event-vs-delivery boundaries and confirmed match end are upstream guarantees, not re-proved from CSV.',
        'evaluation_policy': {'minimum_split_group': ['match_id'], 'paired_views_group': ['match_id', 'replay_tick'],
                              'any_death_shared_between_views': True, 'own_enemy_targets_are_team_oriented': True,
                              'paired_views_and_overlapping_windows_are_not_independent': True,
                              'split_created': False},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', action='append', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_corpus(args.input, args.output_dir)
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest['counts']))
    print(f'WROTE {args.output_dir / CSV_NAME} and manifest.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
