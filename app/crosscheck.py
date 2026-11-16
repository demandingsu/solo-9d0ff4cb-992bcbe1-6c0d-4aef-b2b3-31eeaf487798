"""Cross-cell sample cross-check ("串档" detection).

Given the imported samples of two cells, find the maximal contiguous
stretches whose current values are identical point-by-point under one fixed
integer clock offset. Such stretches indicate the two files may carry the
same physical sampling run under duplicated labels.

Rules (see task statement):
  * offset convention: ``b_ts == a_ts + offset`` (offset = B clock minus A
    clock, in integer UTC seconds).
  * both sides are sorted ``(ts, current_ma)`` sequences; inside a matched
    stretch neither side may skip an intermediate sample, and matched pairs
    must have identical currents.
  * adjacent samples on each side must be at most 60 s apart; a longer gap
    truncates the stretch (the same gap rule used by cycle detection).
  * a reported stretch must contain at least 4 pairs with non-zero current.
  * only maximal stretches are reported (they cannot be extended while the
    rules above hold).
  * when the same sample stretch on one side is explainable by more than
    one feasible offset, every candidate is kept and flagged ``ambiguous``
    with the competing offsets listed for manual review.

Currents are compared as the stored integer microamp values, so equality is
exact (no float round-trip).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence, Set, Tuple

GAP_SECONDS = 60
MIN_NONZERO_PAIRS = 4

Sample = Tuple[int, int]  # (utc_seconds, current_microamps)


@dataclass
class Segment:
    """One maximal matching stretch under a fixed offset."""

    offset: int
    a_start_ts: int
    a_end_ts: int
    b_start_ts: int
    b_end_ts: int
    pair_count: int
    nonzero_pairs: int
    ambiguous: bool = False
    alternative_offsets: List[int] = field(default_factory=list)
    a_cycle_nos: List[int] = field(default_factory=list)
    b_cycle_nos: List[int] = field(default_factory=list)


def find_matching_segments(
    samples_a: Sequence[Sample],
    samples_b: Sequence[Sample],
    min_offset: int,
    max_offset: int,
) -> List[Segment]:
    """Return all maximal same-current stretches under integer offsets.

    The scan enumerates matching sample pairs (equal current and a feasible
    offset) and walks each pair's forward run while both sequences stay
    contiguous (no skipped sample) and gap-free (<= 60 s between adjacent
    samples on each side). A global ``(offset, i, j)`` visit set makes each
    pair contribute to exactly one run walk, so only the maximal run rooted
    at the earliest pair is produced.
    """
    segments: List[Segment] = []
    seen: Set[Tuple[int, int, int]] = set()

    for ia, (ta, ca) in enumerate(samples_a):
        for ib, (tb, cb) in enumerate(samples_b):
            offset = tb - ta
            if offset < min_offset or offset > max_offset or ca != cb:
                continue
            key = (offset, ia, ib)
            if key in seen:
                continue  # interior pair of a run already walked from earlier

            # Walk the maximal forward run under this offset.
            ja, jb = ia, ib
            nonzero = 0 if ca == 0 else 1
            seen.add(key)
            while True:
                na, nb = ja + 1, jb + 1
                if na >= len(samples_a) or nb >= len(samples_b):
                    break
                nta, nca = samples_a[na]
                ntb, ncb = samples_b[nb]
                if (
                    ntb - nta != offset
                    or nca != ncb
                    or nta - samples_a[ja][0] > GAP_SECONDS
                    or ntb - samples_b[jb][0] > GAP_SECONDS
                ):
                    break
                ja, jb = na, nb
                seen.add((offset, ja, jb))
                if nca != 0:
                    nonzero += 1

            if nonzero >= MIN_NONZERO_PAIRS:
                segments.append(
                    Segment(
                        offset=offset,
                        a_start_ts=ta,
                        a_end_ts=samples_a[ja][0],
                        b_start_ts=tb,
                        b_end_ts=samples_b[jb][0],
                        pair_count=ja - ia + 1,
                        nonzero_pairs=nonzero,
                    )
                )

    segments.sort(key=lambda s: (s.offset, s.a_start_ts, s.b_start_ts))
    _mark_ambiguous(segments)
    return segments


def _mark_ambiguous(segments: List[Segment]) -> None:
    """Flag stretches that admit more than one feasible offset.

    If two reported segments (under *different* offsets) overlap on one
    cell's timeline -- i.e. they share at least one matched sample of that
    cell -- then that cell's stretch can be explained by more than one
    offset. Every such candidate is kept and marked ambiguous, carrying the
    sorted list of competing offsets. Segments sharing only the same offset
    are never ambiguous (the offset itself is not in dispute).
    """
    for side in ("a", "b"):
        ordered = sorted(
            segments,
            key=lambda s: s.a_start_ts if side == "a" else s.b_start_ts,
        )
        for i, s1 in enumerate(ordered):
            start1 = s1.a_start_ts if side == "a" else s1.b_start_ts
            end1 = s1.a_end_ts if side == "a" else s1.b_end_ts
            for s2 in ordered[i + 1 :]:
                start2 = s2.a_start_ts if side == "a" else s2.b_start_ts
                end2 = s2.a_end_ts if side == "a" else s2.b_end_ts
                if start2 > end1:
                    break  # sorted by start: no later segment can overlap
                if s1.offset == s2.offset or start1 > end2:
                    continue
                s1.ambiguous = True
                s2.ambiguous = True
                if s2.offset not in s1.alternative_offsets:
                    s1.alternative_offsets.append(s2.offset)
                if s1.offset not in s2.alternative_offsets:
                    s2.alternative_offsets.append(s1.offset)
    for seg in segments:
        seg.alternative_offsets.sort()
