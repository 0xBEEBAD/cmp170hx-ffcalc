import json
import os
import shutil
import tempfile
import unittest

from ffcalc import cli, ffrecord, ingest
from ffcalc.analyze import analyze
from ffcalc.dump import load_dump
from ffcalc.generate import plan
from ffcalc.optionmap import BY_KEY
from ffcalc.simulate import simulate

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

    def test_scan_cli(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        rc = cli.main(["scan", "--dir", self._collect_dir(),
                       "--outdir", os.path.join(out, "cards"),
                       "--plans", os.path.join(out, "plans")])
        self.assertEqual(rc, 0)
        for _bdf, uuid in UUIDS.values():
            cid = uuid[4:]
            self.assertTrue(os.path.exists(
                os.path.join(out, "cards", cid + ".json")))
            with open(os.path.join(out, "plans",
                                   cid + ".burn.json")) as f:
                self.assertEqual(json.load(f)["uuid"], uuid)

    def test_verify_cli_ok_and_mismatch(self):
        out = tempfile.mkdtemp(prefix="ffcalc-out-")
        self.addCleanup(shutil.rmtree, out, True)
        cli.main(["scan", "--dir", self._collect_dir(),
                  "--outdir", os.path.join(out, "cards"),
                  "--plans", os.path.join(out, "plans")])
        plan5 = os.path.join(out, "plans",
                             UUIDS[0xfa000000][1][4:] + ".burn.json")
        rc = cli.main(["verify", plan5, "--dir", self._collect_dir()])
        self.assertEqual(rc, 0)
        wrong = {bar: (bdf, "GPU-" + "f" * 32)
                 for bar, (bdf, _u) in UUIDS.items()}
        rc = cli.main(["verify", plan5,
                       "--dir", self._collect_dir(wrong)])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
