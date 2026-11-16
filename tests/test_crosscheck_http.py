"""End-to-end HTTP tests for the cross-cell cross-check API."""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="battery-cc-test-")
_DB = os.path.join(_TMP, "cc.db")
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _suffix)
    except FileNotFoundError:
        pass
os.environ["BATTERY_DB_PATH"] = _DB

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def csv_text(rows, header="timestamp,current\n"):
    return header + "".join(f"{ts},{cur}\n" for ts, cur in rows)


def upload(client, cell_id, rows):
    return client.post(
        f"/cells/{cell_id}/samples",
        files={"file": ("samples.csv", csv_text(rows), "text/csv")},
    )


def xcheck(client, a, b, **params):
    return client.get(f"/cells/{a}/cross-check/{b}", params=params)


# one closed cycle followed by another, 10 s cadence, no gaps > 60 s
TWO_CYCLES = [
    (0, 0.0),
    (10, 2.0),
    (20, 0.0),
    (30, -3.0),
    (40, 0.0),    # cycle 1 closes (non-zero pairs: 2)
    (50, 2.0),
    (60, 0.0),
    (70, -3.0),
    (80, 0.0),    # cycle 2 closes (non-zero pairs: 2)
]


class CrossCheckHttpTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.a = "CELL-CC-A"
        self.b = "CELL-CC-B"
        upload(self.c, self.a, TWO_CYCLES)
        b_rows = [(ts + 30, cur) for ts, cur in TWO_CYCLES]
        upload(self.c, self.b, b_rows)

    def test_matches_with_cycle_intersection_and_versions(self):
        va = self.c.get(f"/cells/{self.a}/report").json()["version"]
        vb = self.c.get(f"/cells/{self.b}/report").json()["version"]
        r = xcheck(self.c, self.a, self.b, min_offset=-60, max_offset=60)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["cell_a_version"], va)
        self.assertEqual(body["cell_b_version"], vb)
        self.assertEqual(body["segment_count"], 1)
        seg = body["segments"][0]
        self.assertEqual(seg["offset"], 30)
        self.assertEqual((seg["a_start_ts"], seg["a_end_ts"]), (0, 80))
        self.assertEqual((seg["b_start_ts"], seg["b_end_ts"]), (30, 110))
        self.assertEqual(seg["pair_count"], 9)
        self.assertEqual(seg["nonzero_pairs"], 4)
        self.assertFalse(seg["ambiguous"])
        self.assertEqual(seg["alternative_offsets"], [])
        # the stretch overlaps both cycles on each cell
        self.assertEqual(seg["a_cycle_nos"], [1, 2])
        self.assertEqual(seg["b_cycle_nos"], [1, 2])

    def test_partial_overlap_reports_only_intersecting_cycles(self):
        # a denser cell whose first cycle alone carries 4 non-zero pairs;
        # cell E only carries that first cycle (shifted), so the stretch
        # intersects cycle 1 only, even though D already has a cycle 2
        dense = [
            (0, 0.0), (5, 1.0), (10, 1.0), (15, 0.0),
            (20, -1.0), (25, -1.0), (30, 0.0),   # cycle 1 (4 non-zero)
            # gap > 60 s, then cycle 2 well away from the first segment
            (100, 0.0), (105, 1.0), (110, 0.0),
            (115, -1.0), (120, 0.0),
        ]
        cell_d = "CELL-CC-DENSE"
        cell_e = "CELL-CC-DENSE-SHIFT"
        upload(self.c, cell_d, dense)
        # E mirrors D's cycle-1 samples (up to its closing zero at t=30)
        upload(self.c, cell_e, [(ts + 12, cur) for ts, cur in dense[:7]])

        r = xcheck(self.c, cell_d, cell_e, min_offset=12, max_offset=12)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["segment_count"], 1)
        seg = body["segments"][0]
        self.assertEqual(seg["nonzero_pairs"], 4)
        self.assertEqual(seg["a_end_ts"], 30)
        self.assertEqual(seg["a_cycle_nos"], [1])
        self.assertEqual(seg["b_cycle_nos"], [1])

    def test_default_window_and_inclusive_bounds(self):
        # default window (-60..60) covers the +30 s clock offset
        r = xcheck(self.c, self.a, self.b)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["segment_count"], 1)
        # single-offset window, inclusive boundary
        r2 = xcheck(self.c, self.a, self.b, min_offset=30, max_offset=30)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["segments"][0]["offset"], 30)
        # window whose upper edge is one second short of the true offset
        r3 = xcheck(self.c, self.a, self.b, min_offset=-60, max_offset=29)
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["segments"], [])
        # window ending exactly on the offset still matches (inclusive)
        r4 = xcheck(self.c, self.a, self.b, min_offset=30, max_offset=60)
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.json()["segment_count"], 1)

    def test_late_upload_rewrites_result_from_new_committed_state(self):
        before = xcheck(self.c, self.a, self.b).json()

        # append a conflicting-value batch -> rejected, state must not move
        # (B holds a +2 A sample at t=40 = 10+30)
        bad = upload(self.c, self.b, [(40, 9.9)])
        self.assertEqual(bad.status_code, 409)
        same = xcheck(self.c, self.a, self.b).json()
        self.assertEqual(same["cell_b_version"], before["cell_b_version"])
        self.assertEqual(same["segments"], before["segments"])

        # late points on A: second copy of the pattern shifted +200 s gives
        # a second same-offset segment, and the report version advances
        extra = [(ts + 200, cur) for ts, cur in TWO_CYCLES]
        ok = upload(self.c, self.a, extra)
        self.assertEqual(ok.status_code, 200, ok.text)
        after = xcheck(self.c, self.a, self.b).json()
        self.assertEqual(after["cell_a_version"], before["cell_a_version"] + 1)
        # B still holds only one copy: only the original stretch matches
        self.assertEqual(after["segment_count"], 1)
        self.assertEqual(after["segments"][0]["a_end_ts"], 80)

    def test_unknown_cell_404(self):
        r = xcheck(self.c, "NOPE", self.b, min_offset=-60, max_offset=60)
        self.assertEqual(r.status_code, 404)
        r2 = xcheck(self.c, self.a, "NOPE-EITHER", min_offset=-60, max_offset=60)
        self.assertEqual(r2.status_code, 404)

    def test_same_cell_rejected(self):
        r = xcheck(self.c, self.a, self.a, min_offset=-60, max_offset=60)
        self.assertEqual(r.status_code, 400)

    def test_invalid_offset_ranges(self):
        r = xcheck(self.c, self.a, self.b, min_offset=10, max_offset=0)
        self.assertEqual(r.status_code, 422)
        r2 = xcheck(self.c, self.a, self.b, min_offset=-60, max_offset=61)
        self.assertEqual(r2.status_code, 422)  # width 121 > 120
        r3 = xcheck(self.c, self.a, self.b, min_offset=-121, max_offset=0)
        self.assertEqual(r3.status_code, 422)  # Query(ge=-120) violated
        r4 = xcheck(self.c, self.a, self.b, min_offset=0, max_offset=121)
        self.assertEqual(r4.status_code, 422)
        r5 = xcheck(self.c, self.a, self.b, min_offset=1.5, max_offset=2)
        self.assertEqual(r5.status_code, 422)  # offset must be integer
        # full allowed 120 s-wide windows at either edge are accepted
        r6 = xcheck(self.c, self.a, self.b, min_offset=-120, max_offset=0)
        self.assertEqual(r6.status_code, 200)
        r7 = xcheck(self.c, self.a, self.b, min_offset=0, max_offset=120)
        self.assertEqual(r7.status_code, 200)

    def test_ambiguous_candidate_is_flagged_with_alternatives(self):
        # constant 1 A runs on both cells; several integer offsets align
        const_a = "CELL-CC-CONST-A"
        const_b = "CELL-CC-CONST-B"
        upload(self.c, const_a, [(i * 10, 1.0) for i in range(8)])
        upload(self.c, const_b, [(i * 10 + 20, 1.0) for i in range(8)])
        r = xcheck(self.c, const_a, const_b, min_offset=0, max_offset=30)
        self.assertEqual(r.status_code, 200, r.text)
        segs = r.json()["segments"]
        self.assertGreaterEqual(len(segs), 2)
        self.assertTrue(all(s["ambiguous"] for s in segs))
        self.assertTrue(all(s["alternative_offsets"] for s in segs))
        # every alternative listed is itself among the returned offsets
        offsets = {s["offset"] for s in segs}
        for s in segs:
            self.assertTrue(set(s["alternative_offsets"]).issubset(offsets))


class CrossCheckConcurrencyTests(unittest.TestCase):
    """The check must read one committed state while uploads commit."""

    def setUp(self):
        self.c = TestClient(app)
        from app.main import db as live_db

        self.db_path = live_db.db_path
        self.a = "CELL-CC-CONC-A"
        self.b = "CELL-CC-CONC-B"
        # two matching cycles on each cell, B shifted by +30 s
        upload(self.c, self.a, TWO_CYCLES)
        upload(self.c, self.b, [(ts + 30, cur) for ts, cur in TWO_CYCLES])

    def _cycles_of_version(self, cell, version):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT cycle_no, started_at, ended_at FROM cycles"
                " WHERE cell_id=? AND version=? ORDER BY cycle_no",
                (cell, version),
            ).fetchall()
        finally:
            conn.close()

    def test_checks_stay_consistent_under_concurrent_uploads(self):
        errors = []
        barrier = threading.Barrier(6)
        stop = threading.Event()

        def writer(n):
            try:
                barrier.wait()
                cli = TestClient(app)
                # disjoint far-future time regions, each a closed cycle;
                # every commit produces a new B report version
                base = 10_000 + n * 1000
                rows = [
                    (base, 0.0), (base + 10, 1.0), (base + 20, 0.0),
                    (base + 30, -1.0), (base + 40, 0.0),
                ]
                r = upload(cli, self.b, rows)
                if r.status_code != 200:
                    errors.append(f"writer {n}: {r.status_code} {r.text}")
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        def reader():
            try:
                barrier.wait()
                cli = TestClient(app)
                for _ in range(100):
                    if stop.is_set():
                        break
                    r = xcheck(cli, self.a, self.b)
                    if r.status_code != 200:
                        errors.append(f"reader status {r.status_code}")
                        continue
                    body = r.json()
                    va, vb = body["cell_a_version"], body["cell_b_version"]
                    # every cycle number in the response must exist in the
                    # exact report version named by the same response: the
                    # samples, versions and cycles are one committed state
                    a_cycles = self._cycles_of_version(self.a, va)
                    b_cycles = self._cycles_of_version(self.b, vb)
                    a_nos = {row[0] for row in a_cycles}
                    b_nos = {row[0] for row in b_cycles}
                    for seg in body["segments"]:
                        if not set(seg["a_cycle_nos"]).issubset(a_nos):
                            errors.append(
                                f"stale A cycle nos {seg['a_cycle_nos']}"
                                f" vs version {va}"
                            )
                        if not set(seg["b_cycle_nos"]).issubset(b_nos):
                            errors.append(
                                f"stale B cycle nos {seg['b_cycle_nos']}"
                                f" vs version {vb}"
                            )
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads[:4]:
            t.join()
        stop.set()
        for t in threads[4:]:
            t.join()

        self.assertEqual(errors, [])
        # the original matching stretch is still reported exactly once
        final = xcheck(self.c, self.a, self.b).json()
        main_segs = [s for s in final["segments"] if s["a_end_ts"] == 80]
        self.assertEqual(len(main_segs), 1)
        self.assertEqual(main_segs[0]["offset"], 30)
        self.assertEqual(main_segs[0]["a_cycle_nos"], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
