import contextlib
import functools
import glob
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from ffcalc import capture, cli, ffmacro, ffrecord, ffrows, ingest
from ffcalc import dump as snapshot
from ffcalc.analyze import analyze
from ffcalc.dump import load_dump
from ffcalc.generate import plan
from ffcalc.optionmap import BY_KEY
from ffcalc.simulate import simulate


def quiet(argv):
    """Run one CLI command, returning (rc, stdout) -- failure messages are
    part of the contract, so tests assert on them."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


def write_rows_file(dump, path):
    with open(path, "w") as f:
        f.write("# physical FF row readout (CMD_READ), unburnt = 0\n")
        for r, v in sorted(dump["ff_rows"].items()):
            r = int(r, 0) if isinstance(r, str) else r
            f.write("%d %s\n" % (r, v))
    return path

SAMPLE = os.path.join(os.path.dirname(__file__), "..",
                      "samples", "blog_card_example.json")


class TestCodec(unittest.TestCase):
    def test_blog_example(self):
        d = ffrecord.decode(0x0fe805c1)
        self.assertEqual(d, {"chain": 1, "block": 23, "data": 0x0fe8})
        self.assertEqual(ffrecord.encode(1, 23, 0x0fe8), 0x0fe805c1)

    def test_replay_later_wins(self):
        rows = {222: ffrecord.encode(1, 5, 0xffff),
                223: ffrecord.encode(1, 5, 0x0f0f)}
        blocks = ffrecord.replay(rows)
        self.assertEqual(blocks[1][5], 0x0f0f)

    def test_iff_rejected(self):
        with self.assertRaises(ValueError):
            ffrecord.decode(0x12340021)


class TestDump(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def test_potential(self):
        rep = analyze(self.dump)
        self.assertEqual(rep["targets"]["fbp_disable"], 0x840)
        self.assertEqual(rep["usable_fbp"], 10)
        self.assertEqual(rep["fb_gib"], 80)
        self.assertEqual(rep["sm_count"], 112)
        self.assertTrue(rep["nvlink_ok"])
        self.assertTrue(rep["pcie_gen4_ok"])

    def test_defective_not_subset_warns(self):
        rep = analyze(self.dump, {"fbp_disable": 0x0})
        self.assertTrue(any("does not contain defective" in w
                            for w in rep["warnings"]))

    def test_gen_sim_roundtrip(self):
        obj = plan(self.dump)
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])
        changed = {r["key"] for r in res["table"] if r["changed"]}
        self.assertIn("fbp_disable", changed)
        self.assertIn("tpc_gpc0_disable", changed)
        self.assertNotIn("pes_disable", changed)

    def test_block_merge_preserves_neighbors(self):
        obj = plan(self.dump)
        blk19 = [r for r in obj["burn"] if r["block"] == 19]
        self.assertEqual(len(blk19), 1)
        self.assertEqual(int(blk19[0]["new"], 0), 0x4200)

    def test_noop_fields_produce_no_rows(self):
        obj = plan(self.dump, only=["fbio_disable"])
        self.assertEqual(obj["burn"], [])

    def test_skip_and_only(self):
        obj = plan(self.dump, only=["fbp_disable"])
        self.assertTrue(all("fbp_disable" in r["fields"]
                            for r in obj["burn"]))
        obj2 = plan(self.dump, skip=["fbp_disable"])
        self.assertFalse(any("fbp_disable" in r["fields"]
                             for r in obj2["burn"]))

    def test_rows_after_last_used(self):
        obj = plan(self.dump)
        rows = [r["row"] for r in obj["burn"]]
        self.assertEqual(rows, sorted(rows))
        self.assertTrue(all(r >= 238 for r in rows))

    def test_overrides_flow_into_targets(self):
        obj = plan(self.dump, overrides={"fbp_disable": 0x0})
        self.assertTrue(any("does not contain defective" in w
                            for w in obj["warnings"]))

    def test_plan_refused_without_ff_rows(self):
        for rows in ({}, {222: 0x141, 223: 0x181}):
            dump = dict(self.dump, ff_rows=rows)
            with self.assertRaises(ffrows.FFRowsNotRead):
                plan(dump)

    def test_sim_refused_without_ff_rows(self):
        obj = plan(self.dump)
        res = simulate(dict(self.dump, ff_rows={}), obj)
        self.assertTrue(any("ff rows readout" in e for e in res["errors"]))

    def test_a100_rewrites_magic_whole_word(self):
        obj = plan(self.dump, a100=True)
        b46 = [r for r in obj["burn"] if r["block"] == 46]
        b47 = [r for r in obj["burn"] if r["block"] == 47]
        self.assertEqual(len(b46), 1)
        self.assertEqual(int(b46[0]["new"], 0), 0x8000)
        self.assertIn("magic_d", b46[0]["fields"])
        self.assertEqual(len(b47), 1)
        self.assertEqual(int(b47[0]["new"], 0), 0)
        self.assertIn("magic_d", b47[0]["fields"])
        self.assertEqual(obj["targets"]["magic_d"], "0x200000")
        self.assertTrue(any("EXPERIMENTAL" in w for w in obj["warnings"]))
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])

    def test_a100_noop_when_magic_already_a100(self):
        rows = dict(self.dump["ff_rows"])
        free = sorted(int(r, 0) if isinstance(r, str) else r
                      for r, v in rows.items()
                      if (v if isinstance(v, int) else int(v, 0)) == 0)
        rows[free[0]] = ffrecord.encode(2, 46, 0x8000)
        rows[free[1]] = ffrecord.encode(2, 47, 0x0000)
        obj = plan(dict(self.dump, ff_rows=rows), a100=True)
        self.assertFalse([r for r in obj["burn"] if r["block"] in (46, 47)])

    def test_magic_not_touched_without_a100(self):
        obj = plan(self.dump)
        self.assertNotIn("magic_d", obj["targets"])
        self.assertFalse([r for r in obj["burn"] if r["block"] == 46])


class TestFfRowsParsing(unittest.TestCase):
    def test_formats(self):
        rows = ffrows.parse_rows(
            "# comment\n"
            "222 0x0fe805c1\n"
            "0xde 0xfe805c1\n"
            "FFROW 223 141\n"
            "[ 1569.145419] FFROW 224 0x19801c1   # trailing comment\n"
            "486 0\n")
        self.assertEqual(rows[222], 0x0fe805c1)
        self.assertEqual(rows[223], 141)
        self.assertEqual(rows[224], 0x19801c1)
        self.assertEqual(rows[486], 0)

    def test_bad_lines(self):
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("512 0\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222 0x100000000\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222 1\n222 2\n")
        ffrows.parse_rows("222 1\n222 0x1\n")

    def test_coverage_states(self):
        self.assertEqual(ffrows.coverage({"ff_rows": {}})[0], "missing")
        self.assertEqual(ffrows.coverage({"ff_rows": {"0": 5}})[0], "missing")
        self.assertEqual(ffrows.coverage({"ff_rows": {"222": 1}})[0],
                         "partial")
        full = dict((r, 0) for r in range(222, 487))
        self.assertEqual(ffrows.coverage({"ff_rows": full})[0], "complete")


class TestMergeffCli(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)
        self.tmp = tempfile.mkdtemp(prefix="ffcalc-merge-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.card = os.path.join(self.tmp, "card.json")
        with open(self.card, "w") as f:
            json.dump({"sku": "8gb", "ff_rows": {}}, f)
        self.rows = write_rows_file(self.dump,
                                    os.path.join(self.tmp, "rows.txt"))

    def test_gen_refused_then_available(self):
        out = os.path.join(self.tmp, "plan.json")
        rc = cli.main(["gen", self.card, "-o", out])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(cli.main(["mergeff", self.card, self.rows]), 0)
        rc = cli.main(["gen", self.card, "-o", out])
        self.assertEqual(rc, 0)
        with open(out) as f:
            obj = json.load(f)
        self.assertTrue(all(r["row"] >= 240 for r in obj["burn"]))

    def test_mergeff_partial_still_refused(self):
        partial = os.path.join(self.tmp, "partial.txt")
        with open(partial, "w") as f:
            for r, v in sorted(self.dump["ff_rows"].items()):
                if r != 486:
                    f.write("%s %s\n" % (r, v))
        rc = cli.main(["mergeff", self.card, partial])
        self.assertEqual(rc, 1)
        rc = cli.main(["gen", self.card, "-o",
                       os.path.join(self.tmp, "plan.json")])
        self.assertEqual(rc, 2)

    def test_mergeff_over_capture_style_dump(self):
        """mergeff over a capture snapshot: the rows it carries over are the
        `"0x%08x"` strings `capture.py` wrote, and they survive the round trip.

        `capture.py` writes row values as `"0x%08x"` strings, `ingest` writes
        ints, and both are legal snapshots. An external rows file normally
        covers only the FF region (222-486), so mergeff has to carry every row
        outside it through unchanged. Carrying them through *unexamined* is
        what crashed mergeff with `"0x%08x" % "0x00000000"` (real card,
        2026-11-24); merely writing them back without checking is what would
        push an invalid row into the snapshot to fail during `gen`, far from
        the cause.
        """
        card = os.path.join(self.tmp, "capture-style.json")
        outside = {0: 0x53557C3D, 1: 0x53557C3D, 221: 0x00000000}
        with open(card, "w") as f:
            json.dump({"sku": "8gb", "option_words": {},
                       "ff_rows": dict(("%d" % r, "0x%08x" % v)
                                       for r, v in outside.items())}, f)
        self.assertEqual(cli.main(["gen", card, "-o",
                                   os.path.join(self.tmp, "p.json")]), 2)
        self.assertEqual(cli.main(["mergeff", card, self.rows]), 0)
        self.assertEqual(cli.main(["gen", card, "-o",
                                   os.path.join(self.tmp, "plan.json")]), 0)
        # The invariant is not how the file looks but that loading it back
        # gives the merged rows plus the rows that were already there, as-is.
        rows = load_dump(card)["ff_rows"]
        ff = dict((r, v) for r, v in self.dump["ff_rows"].items()
                  if 222 <= r <= 486)
        self.assertEqual(dict((r, rows[r]) for r in ff), ff)
        self.assertEqual(dict((r, rows[r]) for r in outside), outside)
        # Idempotent: the file mergeff wrote is a snapshot mergeff can read
        # back, and merging the same readout again changes nothing.
        before = open(card).read()
        self.assertEqual(cli.main(["mergeff", card, self.rows]), 0)
        self.assertEqual(open(card).read(), before)

    def test_sim_with_ffrows_option(self):
        self.assertEqual(cli.main(["mergeff", self.card, self.rows]), 0)
        plan_path = os.path.join(self.tmp, "plan.json")
        cli.main(["gen", self.card, "-o", plan_path])
        empty = os.path.join(self.tmp, "empty.json")
        with open(empty, "w") as f:
            json.dump({"sku": "8gb", "ff_rows": {}}, f)
        rc = cli.main(["sim", empty, plan_path])
        self.assertEqual(rc, 1)
        rc = cli.main(["sim", empty, plan_path, "--ffrows", self.rows])
        self.assertEqual(rc, 0)


class TestUuidMismatch(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def test_sim_rejects_foreign_plan(self):
        obj = plan(self.dump)
        obj["uuid"] = "GPU-00000000-0000-0000-0000-000000000000"
        self.dump["uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        res = simulate(self.dump, obj)
        self.assertTrue(any("uuid" in e for e in res["errors"]))

    def test_sim_accepts_matching_ids(self):
        self.dump["uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        obj = plan(self.dump)
        self.assertEqual(obj["uuid"], self.dump["uuid"])
        res = simulate(self.dump, obj)
        self.assertEqual([e for e in res["errors"] if "uuid" in e], [])


class FakeFuseBar(object):
    IDLE = 4 << 16

    def __init__(self, pmc=0x10000001, idle=True, re_idle=True,
                 debug=0, latch_ok=True):
        self.pmc = pmc
        self.idle = idle
        self.re_idle = re_idle
        self.debug = debug
        self.latch_ok = latch_ok
        self.addr = 77
        self.data = 0
        self.cmds = 0

    def rd(self, off):
        if off == ffmacro.PMC_BOOT_0:
            return self.pmc
        if off == ffmacro.FUSECTRL:
            return FakeFuseBar.IDLE if self.idle else 0xe0050000
        if off == ffmacro.FUSEADDR:
            return self.addr
        if off == ffmacro.FUSEDATA:
            return self.data
        if off == ffmacro.FUSEDEBUG:
            return self.debug
        return 0

    def wr(self, off, val):
        if off == ffmacro.FUSEADDR:
            self.addr = val if self.latch_ok else val + 1
        elif off == ffmacro.FUSECTRL and val == ffmacro.CMD_READ:
            self.cmds += 1
            self.idle = self.re_idle
            self.data = self.addr * 3 % 0x100000000


class TestFfMacro(unittest.TestCase):
    def test_full_read_restores_address(self):
        bar = FakeFuseBar()
        rows = ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertEqual(len(rows), 512)
        self.assertEqual(rows[100], 300)
        self.assertEqual(bar.addr, 77)
        self.assertEqual(bar.cmds, 512)

    def test_partial_rows_survive_error(self):
        bar = FakeFuseBar(re_idle=False)
        clock_times = iter([0.0] * 4 + [0.5] * 100)
        out = {}
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(bar.rd, bar.wr, sleep=lambda s: None,
                                 clock=lambda: next(clock_times),
                                 rows=out)
        self.assertEqual(out, {})
        self.assertEqual(bar.addr, 77)  # FUSEADDR restored on failure

    def test_busy_controller_refuses_to_start(self):
        bar = FakeFuseBar(idle=False)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertIn("busy", str(cm.exception))
        self.assertEqual(bar.cmds, 0)

    def test_debug_register_and_sentinel_and_latch(self):
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(FakeFuseBar(debug=1).rd, FakeFuseBar().wr)
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(FakeFuseBar(pmc=0).rd,
                                 FakeFuseBar().wr)
        bar = FakeFuseBar(latch_ok=False)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertIn("latch", str(cm.exception))

    def test_generated_probe_c_carries_protocol(self):
        src = ffmacro.probe_c_source()
        self.assertNotIn("@", src)
        for needle in ("ffprobe_read_rows", "ffprobe_idle",
                       "writel(1, h + 8519680)",  # CMD_READ -> FUSECTRL
                       "h + 8519684",             # FUSEADDR
                       "h + 8519688"):            # FUSEDATA
            self.assertIn(needle, src)


class FakeMmap(object):
    """BAR0 word array with mmap's seek/read/write, enough to run one
    CMD_READ: a command write to FUSECTRL completes at once and FUSEDATA
    answers with f(FUSEADDR)."""

    def __init__(self, words=()):
        self.words = dict(words)
        self.pos = 0

    def seek(self, off):
        self.pos = off

    def read(self, n):
        assert n == 4
        return struct.pack("<I", self.words.get(self.pos, 0))

    def write(self, buf):
        assert len(buf) == 4
        val = struct.unpack("<I", buf)[0]
        if self.pos == ffmacro.FUSECTRL and val == ffmacro.CMD_READ:
            self.words[ffmacro.FUSEDATA] = \
                self.words.get(ffmacro.FUSEADDR, 0) * 3 % 0x100000000
            val = ffmacro.IDLE_STATE << 16      # command done: back to IDLE
        self.words[self.pos] = val


class TestCaptureAccessors(unittest.TestCase):
    """capture.rd/wr are the accessors used against a live card, so they have
    to take both address styles: hex strings from the JSON map, and the plain
    ints ffmacro passes for FUSECTRL/FUSEADDR/FUSEDATA."""

    def test_offsets_accept_ints_and_strings(self):
        mm = FakeMmap({ffmacro.FUSECTRL: 4 << 16})
        self.assertEqual(capture.rd(mm, ffmacro.FUSECTRL), 4 << 16)
        self.assertEqual(capture.rd(mm, "0x00820000"), 4 << 16)
        capture.wr(mm, ffmacro.FUSEADDR, 9)
        self.assertEqual(mm.words[ffmacro.FUSEADDR], 9)
        capture.wr(mm, "0x0082000c", 0x100000000 + 5)   # masked to 32 bits
        self.assertEqual(mm.words[ffmacro.FUSEWDATA], 5)

    def test_capture_rows_over_real_accessors(self):
        # regression: ffmacro calls rd(PMC_BOOT_0) with an int, which used to
        # raise TypeError: int() can't convert non-string with explicit base
        mm = FakeMmap({ffmacro.PMC_BOOT_0: 0x10000001,
                       ffmacro.FUSECTRL: 4 << 16,
                       ffmacro.FUSEADDR: 77})
        rows = ffmacro.capture_rows(functools.partial(capture.rd, mm),
                                    functools.partial(capture.wr, mm),
                                    first=0, last=3, sleep=lambda s: None)
        self.assertEqual(rows, dict((r, r * 3) for r in range(4)))
        self.assertEqual(mm.words[ffmacro.FUSEADDR], 77)  # address restored


ALLLOG = os.path.join(os.path.dirname(__file__), "..",
                      "samples", "ffprobe_all.txt")
UUIDS = {
    0xfa000000: ("0000:05:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000005"),
    0xf9000000: ("0000:06:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000006"),
    0xfb000000: ("0000:09:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000009"),
}


class TestIngest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ALLLOG) as f:
            text = f.read()
        cls.bybar = {}
        for line in text.splitlines():
            m = ingest.LINE.search(line)
            if m:
                bar = int(m.group(2), 16) & 0xff000000
                cls.bybar.setdefault(bar, []).append(line)

    def _collect_dir(self, uuids=None):
        d = tempfile.mkdtemp(prefix="ffcalc-collect-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "bar0map.tsv"), "w") as f:
            for bar, (bdf, uuid) in sorted((uuids or UUIDS).items()):
                f.write("%s\t0x%08x\t%s\n" % (bdf, bar, uuid))
                with open(os.path.join(d, "probe_%s.txt"
                                        % ingest.sanitize(bdf)), "w") as lf:
                    lf.write("\n".join(self.bybar[bar]))
        return d

    def test_grouping(self):
        self.assertEqual(sorted(self.bybar), sorted(UUIDS))
        for lines in self.bybar.values():
            self.assertEqual(len(lines), 61)

    def test_dumps_distinct_uuids_and_fingerprints(self):
        m = ingest.load_map()
        dumps = list(ingest.read_collected(self._collect_dir(), m))
        self.assertEqual(len(dumps), 3)
        ids = {d["uuid"] for d in dumps}
        fps = {d["fingerprint"] for d in dumps}
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(fps), 3)
        for d in dumps:
            want = 0x2d if d["bdf"] == "0000:06:00.0" else 0x840
            self.assertEqual(d["option_words"]["fbp_defective"], want)
            self.assertEqual(ingest.card_id(d), d["uuid"][4:])

    def test_card_id_falls_back_to_fingerprint(self):
        d = {"uuid": None, "fingerprint": "deadbeef00000000"}
        self.assertEqual(ingest.card_id(d), "fp-deadbeef00000000")

    def _ffrow_lines(self):
        return ["[ 1569.9] FFPROBE ffrow.%d 08200008 %08x"
                % (r, 0x0fe805c1 if r == 222 else 0) for r in range(512)]

    def test_cmd_read_log_fills_ff_rows(self):
        m = ingest.load_map()
        entries = ingest.parse_log("\n".join(self._ffrow_lines()))
        dump = ingest.build_dump(m, entries)
        self.assertEqual(ffrows.coverage(dump)[0], "complete")
        self.assertEqual(dump["ff_rows"][222], 0x0fe805c1)
        self.assertFalse(dump["read_only"])

    def test_scan_with_cmd_read_log_yields_plans(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        d = self._collect_dir()
        for _bdf, _uuid in UUIDS.values():
            path = os.path.join(
                d, "probe_%s.txt" % ingest.sanitize(_bdf))
            with open(path, "a") as f:
                f.write("\n" + "\n".join(self._ffrow_lines()))
        self.assertEqual(
            cli.main(["scan", "--dir", d,
                      "--outdir", os.path.join(out, "cards"),
                      "--plans", os.path.join(out, "plans")]), 0)
        for _bdf, uuid in UUIDS.values():
            with open(os.path.join(out, "plans",
                                   uuid[4:] + ".burn.json")) as f:
                obj = json.load(f)
            self.assertEqual(obj["uuid"], uuid)
            # row 222 is occupied by the captured record
            self.assertTrue(all(r["row"] >= 223 for r in obj["burn"]))

    def _scan(self, out):
        return cli.main(["scan", "--dir", self._collect_dir(),
                         "--outdir", os.path.join(out, "cards"),
                         "--plans", os.path.join(out, "plans")])

    def _merge_rows(self, out):
        rows = write_rows_file(load_dump(SAMPLE),
                               os.path.join(out, "rows.txt"))
        for _bdf, uuid in UUIDS.values():
            rc = cli.main(["mergeff",
                           os.path.join(out, "cards", uuid[4:] + ".json"),
                           rows])
            self.assertEqual(rc, 0)
        return rows

    def test_scan_cli_refuses_plans_without_ff_rows_then_ok(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        # read-only probe cannot see FF rows -> dumps saved, plans refused
        self.assertEqual(self._scan(out), 1)
        plans = os.path.join(out, "plans")
        self.assertFalse(os.path.isdir(plans))
        for _bdf, uuid in UUIDS.values():
            self.assertTrue(os.path.exists(
                os.path.join(out, "cards", uuid[4:] + ".json")))
        # after merging a full FF readout, a rescan yields the plans
        self._merge_rows(out)
        self.assertEqual(self._scan(out), 0)
        for _bdf, uuid in UUIDS.values():
            with open(os.path.join(plans, uuid[4:] + ".burn.json")) as f:
                self.assertEqual(json.load(f)["uuid"], uuid)

    def test_verify_cli_ok_and_mismatch(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        self._scan(out)
        rows = self._merge_rows(out)
        self._scan(out)
        plan5 = os.path.join(out, "plans",
                             UUIDS[0xfa000000][1][4:] + ".burn.json")
        rc = cli.main(["verify", plan5, "--dir", self._collect_dir(),
                       "--ffrows", rows])
        self.assertEqual(rc, 0)
        # fresh read without the FF rows file -> replay cannot be trusted
        rc = cli.main(["verify", plan5, "--dir", self._collect_dir()])
        self.assertEqual(rc, 1)
        wrong = {bar: (bdf, "GPU-" + "f" * 32)
                 for bar, (bdf, _u) in UUIDS.items()}
        rc = cli.main(["verify", plan5,
                       "--dir", self._collect_dir(wrong),
                       "--ffrows", rows])
        self.assertEqual(rc, 1)


class TestSnapshotCodec(unittest.TestCase):
    """A snapshot is the only basis a burn plan is built from, so the codec
    rules hold for every writer and every reader: any shape that can be a
    real readout is believed exactly, anything that cannot is refused out loud
    before a byte is written, and no failure may damage the file."""

    INVALID_ROWS = [
        ("row outside the array", {"999": "0x00000000"}),
        ("negative row", {"-1": 0}),
        ("value is not a number", {"222": "0xGG"}),
        ("value is null", {"222": None}),
        ("value is a bool", {"222": True}),
        ("value is a float", {"222": 1.5}),
        ("value wider than 32 bits", {"222": "0x1ffffffff"}),
        ("same row listed twice with two values", {"222": 1, "0xde": 2}),
        ("row key is not a number", {"ff222": 0}),
    ]

    FORMS = [("capture", lambda v: "0x%08x" % v),
             ("probe", lambda v: v),
             ("mixed", lambda v: "0x00000000" if v == 0 else v)]

    def setUp(self):
        self.dump = load_dump(SAMPLE)
        self.tmp = tempfile.mkdtemp(prefix="ffcalc-snap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ff_rows = dict((r, v) for r, v in self.dump["ff_rows"].items()
                            if ffrows.FF_ROW_MIN <= r <= ffrows.FF_ROW_MAX)
        self.rows = write_rows_file(self.dump,
                                    os.path.join(self.tmp, "rows.txt"))

    def _card(self, rows, name="card.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            json.dump({"sku": "8gb", "option_words": {}, "ff_rows": rows}, f)
        return path

    def _disk_rows(self, path):
        with open(path) as f:
            return json.load(f)["ff_rows"]

    def test_every_legal_shape_is_believed_exactly(self):
        expected = {0: 0x53557C3D}                  # a row outside the FF region
        expected.update(self.ff_rows)
        for name, fmt in self.FORMS:
            rows = dict(("%d" % r, fmt(v)) for r, v in sorted(expected.items()))
            card = self._card(rows, "card-%s.json" % name)
            self.assertEqual(load_dump(card)["ff_rows"], expected, name)
            rc, out = quiet(["report", card])
            self.assertEqual(rc, 0, (name, out))
            rc, out = quiet(["mergeff", card, self.rows])
            self.assertEqual(rc, 0, (name, out))
            back = load_dump(card)["ff_rows"]
            self.assertEqual(dict((r, back[r]) for r in self.ff_rows),
                             self.ff_rows, name)
            self.assertEqual(back[0], 0x53557C3D, name)

    def test_invalid_rows_are_refused_before_anything_is_written(self):
        for why, rows in self.INVALID_ROWS:
            card = self._card(dict(rows))
            before = open(card).read()
            rc, out = quiet(["mergeff", card, self.rows])
            self.assertEqual(rc, 2, (why, out))
            self.assertIn("FAIL:", out, why)
            self.assertEqual(open(card).read(), before,
                             "%s: a refused merge must leave the snapshot "
                             "exactly as it was" % why)
            for cmd in (["report", card],
                        ["gen", card, "-o", os.path.join(self.tmp, "p.json")]):
                rc, out = quiet(cmd)
                self.assertEqual(rc, 2, (why, cmd[0], out))
                self.assertIn("FAIL:", out, "%s: %s" % (why, cmd[0]))
            self.assertEqual(glob.glob(os.path.join(self.tmp, "*.tmp")), [])

    def test_missing_file_is_a_message_not_a_traceback(self):
        rc, out = quiet(["report", os.path.join(self.tmp, "nope.json")])
        self.assertEqual(rc, 2)
        self.assertIn("FAIL:", out)
        self.assertIn("cannot be read", out)

    def test_plan_file_that_is_not_a_plan_is_refused(self):
        """A plan is a burn order: a truncated or foreign file must be
        refused with a message (rc 2), not a traceback -- `sim` and `verify`
        are what stand between a plan file and burntool."""
        card = self._card(dict(("%d" % r, "0x%08x" % v)
                               for r, v in sorted(self.ff_rows.items())))
        plan_path = os.path.join(self.tmp, "plan.json")
        self.assertEqual(quiet(["gen", card, "-o", plan_path])[0], 0)
        good = open(plan_path).read()
        cases = {"truncated": good[:len(good) // 2],
                 "not a plan": '{"burn": "no"}'}
        for why, body in cases.items():
            with open(plan_path, "w") as f:
                f.write(body)
            for cmd in (["sim", card, plan_path],
                        ["verify", plan_path, "--dump", card]):
                rc, out = quiet(cmd)
                self.assertEqual(rc, 2, (why, cmd[0], out))
                self.assertIn("FAIL:", out, (why, cmd[0]))
        with open(plan_path, "w") as f:
            f.write(good)

    def test_bad_rows_file_names_the_line_that_broke(self):
        path = os.path.join(self.tmp, "rows-bad.txt")
        with open(path, "w") as f:
            f.write("222 0x0fe805c1\n999 0\n")
        rc, out = quiet(["mergeff", self._card({}), path])
        self.assertEqual(rc, 2)
        self.assertIn("line 2", out)

    def test_truncated_snapshot_says_so_instead_of_raising(self):
        # What an interrupted, non-atomic write by an older ffcalc leaves
        # behind -- the fix is re-capturing, so say that, not "JSONDecodeError".
        path = os.path.join(self.tmp, "truncated.json")
        with open(path, "w") as f:
            f.write('{"sku": "8gb", "ff_rows": {"222": "0x0f')
        for cmd in (["report", path], ["mergeff", path, self.rows]):
            rc, out = quiet(cmd)
            self.assertEqual(rc, 2, (cmd[0], out))
            self.assertIn("not valid JSON", out)

    def test_mergeff_shows_which_rows_it_overwrote(self):
        rows = dict(("%d" % r, "0x%08x" % v)
                    for r, v in sorted(self.ff_rows.items()))
        rows["222"] = "0xdeadbeef"          # snapshot disagrees with the readout
        card = self._card(rows)
        rc, out = quiet(["mergeff", card, self.rows])
        self.assertEqual(rc, 0)
        self.assertIn("note:", out)
        self.assertIn("222 0xdeadbeef->0x%08x" % self.ff_rows[222], out)
        # the live readout wins: that is the whole point of merging one in
        self.assertEqual(load_dump(card)["ff_rows"][222], self.ff_rows[222])

    def test_write_failure_keeps_the_snapshot_that_was_there(self):
        card = self._card(dict(("%d" % r, "0x%08x" % v)
                               for r, v in sorted(self.ff_rows.items())))
        good = open(card).read()
        for where, exc in (("os.replace", OSError(28, "No space left on device")),
                           ("json.dumps", TypeError("not serializable"))):
            with mock.patch("ffcalc.dump." + where, side_effect=exc):
                rc, out = quiet(["mergeff", card, self.rows])
            self.assertEqual(rc, 2, (where, out))
            self.assertIn("FAIL:", out, where)
            self.assertEqual(open(card).read(), good,
                             "%s: a failed write must leave the old snapshot"
                             % where)
            self.assertFalse(os.path.exists(card + ".tmp"),
                             "%s: the temp file must not be left behind" % where)

    def test_coverage_refuses_a_key_that_is_not_a_row(self):
        # coverage() is the gate that decides "burn or not", so a key that is
        # not a physical row must not quietly read as "some rows are known":
        # that is how a corrupt snapshot could look "partial" instead of bad.
        for rows in ({"999": "0x00000000", "222": 0},
                     {"ff222": 0},
                     {"-3": 0}):
            self.assertRaises(snapshot.BadSnapshot, ffrows.coverage,
                              {"ff_rows": rows})

    def test_both_writer_channels_emit_one_shape(self):
        """Whatever a dump looked like in memory, both writers put the same
        canonical rows on disk: probe saves through ingest.save_dump, capture
        through to_json_form + atomic_write_json, and neither passes an int
        value through for the next reader to trip over."""
        mem = {"sku": "8gb", "ff_rows": dict(self.ff_rows),
               "option_words": {"fbp_disable": 0x852}}
        via_probe = ingest.save_dump(os.path.join(self.tmp, "cards"), mem)
        via_capture = os.path.join(self.tmp, "capture.json")
        snapshot.atomic_write_json(via_capture,
                                   snapshot.to_json_form(mem, via_capture))
        self.assertEqual(self._disk_rows(via_probe),
                         self._disk_rows(via_capture))
        for path in (via_probe, via_capture):
            self.assertTrue(all(isinstance(v, str) and len(v) == 10
                                for v in self._disk_rows(path).values()), path)
            self.assertEqual(load_dump(path)["ff_rows"],
                             dict((r, v) for r, v in
                                  load_dump(via_probe)["ff_rows"].items()))


if __name__ == "__main__":
    unittest.main()
