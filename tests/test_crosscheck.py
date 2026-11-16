"""Unit tests for the cross-cell cross-check matching algorithm."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crosscheck import (  # noqa: E402
    MIN_NONZERO_PAIRS,
    find_matching_segments,
)


def shift(samples, delta):
    """Shift every timestamp of one cell by delta seconds."""
    return [(ts + delta, cur) for ts, cur in samples]


# A complete charge/discharge cycle, 10 s cadence, currents in microamps.
ONE_CYCLE = [
    (0, 0),
    (10, 2_000_000),
    (20, 2_000_000),
    (30, 0),
    (40, -3_000_000),
    (50, -3_000_000),
    (60, 0),
]


class BasicMatchTests(unittest.TestCase):
    def test_fixed_offset_full_match(self):
        segs = find_matching_segments(ONE_CYCLE, shift(ONE_CYCLE, 30), -120, 120)
        self.assertEqual(len(segs), 1)
        s = segs[0]
        self.assertEqual(s.offset, 30)
        self.assertEqual((s.a_start_ts, s.a_end_ts), (0, 60))
        self.assertEqual((s.b_start_ts, s.b_end_ts), (30, 90))
        self.assertEqual(s.pair_count, 7)
        self.assertEqual(s.nonzero_pairs, 4)
        self.assertFalse(s.ambiguous)
        self.assertEqual(s.alternative_offsets, [])

    def test_offset_outside_range_no_match(self):
        # B is 200 s behind: outside a +/-120 window -> nothing
        segs = find_matching_segments(ONE_CYCLE, shift(ONE_CYCLE, 200), -120, 120)
        self.assertEqual(segs, [])

    def test_offset_range_boundary_is_inclusive(self):
        segs = find_matching_segments(ONE_CYCLE, shift(ONE_CYCLE, 120), 120, 120)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].offset, 120)

    def test_negative_offset(self):
        segs = find_matching_segments(ONE_CYCLE, shift(ONE_CYCLE, -45), -120, 120)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].offset, -45)
        self.assertEqual(segs[0].b_end_ts, 15)


class GapAndContiguityTests(unittest.TestCase):
    def test_gap_over_60s_splits_stretch(self):
        # identical current pattern on both sides with a 61 s internal gap;
        # the gap truncates the run and the two pieces each stay below the
        # 4 non-zero-pair threshold -> nothing reported
        a = [
            (0, 0),
            (10, 2_000_000),
            (20, 2_000_000),
            (30, 0),
            (91, -3_000_000),  # 61 s after t=30
            (101, -3_000_000),
            (111, 0),
        ]
        segs = find_matching_segments(a, shift(a, 15), -120, 120)
        self.assertEqual(segs, [])

    def test_gap_exactly_60s_keeps_stretch(self):
        a = [
            (0, 0),
            (10, 2_000_000),
            (20, 2_000_000),
            (30, 0),
            (90, -3_000_000),  # exactly 60 s after t=30 -> still contiguous
            (100, -3_000_000),
            (110, 0),
        ]
        segs = find_matching_segments(a, shift(a, 15), -120, 120)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].pair_count, 7)

    def test_skipping_an_intermediate_sample_breaks_stretch(self):
        # B misses one interior sample: runs cannot skip it, so the walk
        # stops; remaining matches never reach 4 non-zero pairs.
        b = [row for row in ONE_CYCLE if row[0] != 20]
        segs = find_matching_segments(ONE_CYCLE, b, -120, 120)
        self.assertEqual(segs, [])

    def test_different_current_breaks_stretch(self):
        b = list(ONE_CYCLE)
        b[1] = (10, 9_000_000)  # colliding timestamp, different current
        segs = find_matching_segments(ONE_CYCLE, b, -120, 120)
        self.assertEqual(segs, [])

    def test_gap_between_two_runs_yields_two_segments_same_offset(self):
        # two complete cycles separated by a >60 s gap: the walk splits
        # into two maximal segments under the same offset (not ambiguous,
        # since the offset itself is undisputed)
        a = ONE_CYCLE + [
            (ts + 200, cur) for ts, cur in ONE_CYCLE
        ]
        segs = find_matching_segments(a, shift(a, 10), 0, 20)
        self.assertEqual(len(segs), 2)
        self.assertEqual([s.b_start_ts for s in segs], [10, 210])
        self.assertTrue(all(s.offset == 10 for s in segs))
        self.assertFalse(any(s.ambiguous for s in segs))


class NonzeroThresholdTests(unittest.TestCase):
    def test_three_nonzero_pairs_rejected(self):
        samples = [
            (0, 0),
            (10, 1_000_000),
            (20, 1_000_000),
            (30, 0),
            (40, -1_000_000),  # only 3 non-zero pairs
            (50, 0),
        ]
        segs = find_matching_segments(samples, shift(samples, 5), -120, 120)
        self.assertEqual(segs, [])

    def test_four_nonzero_pairs_accepted(self):
        samples = [
            (0, 0),
            (10, 1_000_000),
            (20, 1_000_000),
            (30, 0),
            (40, -1_000_000),
            (50, -2_000_000),  # 4 non-zero pairs
            (60, 0),
        ]
        segs = find_matching_segments(samples, shift(samples, 5), -120, 120)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].nonzero_pairs, MIN_NONZERO_PAIRS)

    def test_long_zero_only_stretch_reported_nowhere(self):
        samples = [(i * 10, 0) for i in range(10)]
        segs = find_matching_segments(samples, shift(samples, 0), -120, 120)
        self.assertEqual(segs, [])


class AmbiguityTests(unittest.TestCase):
    def test_constant_current_makes_multiple_offsets_ambiguous(self):
        # constant 1 A, 10 s cadence: many integer offsets align the whole
        # span; every candidate must be retained and flagged ambiguous.
        a = [(i * 10, 1_000_000) for i in range(8)]
        b = shift(a, 20)
        segs = find_matching_segments(a, b, 0, 30)
        offsets = sorted(s.offset for s in segs)
        # alignments at offset 20 (full), 30 (7 pairs), 10 (7), 0 (6)...
        self.assertIn(20, offsets)
        self.assertGreaterEqual(len(segs), 2)
        for s in segs:
            self.assertTrue(s.ambiguous)
            self.assertTrue(s.alternative_offsets)
        # full-span candidate under the true offset lists every other offset
        full = next(s for s in segs if s.pair_count == 8)
        self.assertEqual(sorted(set(full.alternative_offsets) | {full.offset}),
                         offsets)

    def test_unique_offset_is_not_ambiguous(self):
        segs = find_matching_segments(ONE_CYCLE, shift(ONE_CYCLE, 30), -120, 120)
        self.assertEqual(len(segs), 1)
        self.assertFalse(segs[0].ambiguous)

    def test_periodic_pattern_two_offsets_both_kept_and_flagged(self):
        # a periodic current pattern aligns under two different offsets;
        # both explain overlapping samples of the same A stretch, so both
        # candidates are kept and marked ambiguous with each other listed
        pattern = [(i * 10, ((i % 4) * 1_000_000)) for i in range(12)]
        b = shift(pattern, 10)  # period is 40 s -> offsets 10 and 50 align
        segs = find_matching_segments(pattern, b, 0, 60)
        offsets = sorted(s.offset for s in segs)
        self.assertEqual(offsets, [10, 50])
        self.assertTrue(all(s.ambiguous for s in segs))
        for s in segs:
            self.assertEqual(s.alternative_offsets,
                             [o for o in offsets if o != s.offset])
        # longer alignment wins more pairs, but both stay as candidates
        full = next(s for s in segs if s.offset == 10)
        self.assertEqual(full.pair_count, 12)
        self.assertEqual(full.a_start_ts, 0)
        self.assertEqual(full.a_end_ts, 110)

    def test_disjoint_spans_under_different_offsets_are_unambiguous(self):
        # two A cycles separated by a gap; each aligns with a B copy under
        # its own offset, and the matched A spans do not overlap -> no
        # ambiguity, two clean candidates
        a = ONE_CYCLE + [(ts + 200, cur) for ts, cur in ONE_CYCLE]
        b = shift(ONE_CYCLE, 10) + [
            (ts + 200, cur) for ts, cur in shift(ONE_CYCLE, -10)
        ]
        segs = find_matching_segments(a, b, -120, 120)
        self.assertEqual(len(segs), 2)
        self.assertEqual(sorted(s.offset for s in segs), [-10, 10])
        self.assertFalse(any(s.ambiguous for s in segs))


class EmptyInputTests(unittest.TestCase):
    def test_empty_either_side(self):
        self.assertEqual(find_matching_segments([], ONE_CYCLE, -120, 120), [])
        self.assertEqual(find_matching_segments(ONE_CYCLE, [], -120, 120), [])
        self.assertEqual(find_matching_segments([], [], -120, 120), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
