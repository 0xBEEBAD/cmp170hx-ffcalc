import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ffcalc import ffmacro
from ffcalc import burntool


class BurnBar(object):
    """Fake fuse controller with OTP rows; programs are one-shot fuses."""

    def __init__(self, rows=None, poison_row=None, dsov=0, crc=0x55,
                 efp=1, debug=0):
        self.rows = dict(rows or {})
        self.ctrl = 0xE0040000
        self.addr = 0
        self.data = 0
        self.wdata = 0
        self.reg = {ffmacro.FUSEDEBUG: debug, ffmacro.CRC_STATUS: crc,
                    ffmacro.EFP_STATUS: efp, ffmacro.DSOV_STATUS: dsov,
                    ffmacro.SBFE_SHADOW: 1,
                    ffmacro.WDATA_WR_SECURE: 1}
        self.poison_row = poison_row
        self.programs = []

    def rd(self, off):
        if off == ffmacro.FUSECTRL:
            return self.ctrl
        if off == ffmacro.FUSEADDR:
            return self.addr
        if off == ffmacro.FUSEDATA:
            return self.data
        return self.reg.get(off, 0)

    def wr(self, off, val):
        if off == ffmacro.FUSEADDR:
            self.addr = val
        elif off == ffmacro.FUSEWDATA:
            self.wdata = val
        elif off == ffmacro.FUSECTRL:
            cmd = val & 0x1F
            if cmd == ffmacro.CMD_READ:
                self.data = self.rows.get(self.addr, 0)
            elif cmd == ffmacro.CMD_SENSE:
                self.reg[ffmacro.SBFE_SHADOW] = 1
                self.reg[ffmacro.EFP_STATUS] = 1
            elif cmd == ffmacro.CMD_PROGRAM:
                if self.addr == self.poison_row:
                    self.ctrl = 0xBADF510E
                    return
                self.rows[self.addr] = self.wdata
                self.programs.append((self.addr, self.wdata))
        else:
            self.reg[off] = val


def noop(_s):
    pass


def zero_clock():
    return 0.0


PLAN = {"uuid": "GPU-test", "fingerprint": "abc",
        "targets": {"fbp_disable": "0x840"}, "warnings": [],
        "burn": [{"row": 315, "record": "0x420004c1", "chain": 1,
                  "block": 19, "old": "0x4290", "new": "0x4200",
                  "fields": ["fbp_disable"]}]}


class TestBurnOps(unittest.TestCase):
    def setUp(self):
        self.bar = BurnBar(rows={290: 0x429004C1})

    def test_read_row_and_restore(self):
        self.bar.addr = 0x123
        got = ffmacro.read_row(self.bar.rd, self.bar.wr, 290,
                               sleep=noop, clock=zero_clock)
        self.assertEqual(got, 0x429004C1)
        self.assertEqual(self.bar.addr, 0x123)

    def test_sense_checks_dsov(self):
        bar = BurnBar(dsov=1)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.sense(bar.rd, bar.wr, sleep=noop, clock=zero_clock)
        self.assertIn("DSOV", str(cm.exception))
        ffmacro.sense(self.bar.rd, self.bar.wr, sleep=noop,
                      clock=zero_clock)

    def test_arm_requires_window(self):
        bar = BurnBar(efp=0)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.arm(bar.rd, bar.wr)
        self.assertIn("EFP", str(cm.exception))
        ffmacro.arm(self.bar.rd, self.bar.wr)
        self.assertEqual(self.bar.programs, [])

    def test_program_row_full_chain(self):
        ffmacro.arm(self.bar.rd, self.bar.wr)
        got = ffmacro.program_row(self.bar.rd, self.bar.wr, 315,
                                  0x420004C1, sleep=noop, clock=zero_clock)
        self.assertEqual(got, 0x420004C1)
        self.assertEqual(self.bar.programs, [(315, 0x420004C1)])

    def test_program_row_refuses_nonblank(self):
        ffmacro.arm(self.bar.rd, self.bar.wr)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.program_row(self.bar.rd, self.bar.wr, 290, 0x420004C1,
                                sleep=noop, clock=zero_clock)
        self.assertIn("not blank", str(cm.exception))
        self.assertEqual(self.bar.programs, [])

    def test_program_row_already_burned_is_skipped(self):
        self.bar.rows[315] = 0x420004C1
        ffmacro.arm(self.bar.rd, self.bar.wr)
        got = ffmacro.program_row(self.bar.rd, self.bar.wr, 315,
                                  0x420004C1, sleep=noop, clock=zero_clock)
        self.assertEqual(got, 0x420004C1)
        self.assertEqual(self.bar.programs, [])

    def test_poison_aborts_with_guidance(self):
        bar = BurnBar(poison_row=315)
        ffmacro.arm(bar.rd, bar.wr)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.program_row(bar.rd, bar.wr, 315, 0x420004C1,
                                sleep=noop, clock=zero_clock)
        self.assertIn("cold-boot", str(cm.exception))


class TestBurnTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plan_path = os.path.join(self.tmp, "plan.json")
        self.dump_path = os.path.join(self.tmp, "dump.json")
        with open(self.plan_path, "w") as f:
            json.dump(PLAN, f)
        rows = dict(("%d" % r, "0") for r in range(512))
        rows["290"] = "0x429004c1"
        dump = {"sku": "8gb", "uuid": "GPU-test", "fingerprint": "abc",
                "ff_rows": rows,
                "option_words": {"fbp_disable": "0x852",
                                 "fbp_defective": "0x0",
                                 "fbpa_defective": "0x0",
                                 "fbio_defective": "0x0",
                                 "gpc_defective": "0x0",
                                 "pes_defective": "0x0"}}
        for i in range(8):
            dump["option_words"]["tpc_gpc%d_defective" % i] = "0x0"
        with open(self.dump_path, "w") as f:
            json.dump(dump, f)

    def test_offline_gate_blocks_used_row(self):
        bar = BurnBar(rows={315: 0xDEADBEEF})
        plan2 = json.loads(json.dumps(PLAN))
        plan2["burn"][0]["row"] = 290     # row used by a snapshot record
        p = os.path.join(self.tmp, "bad.json")
        with open(p, "w") as f:
            json.dump(plan2, f)
        rc = burntool.cmd_burn(bar.rd, bar.wr, p, self.dump_path,
                               sleep=noop, clock=zero_clock, log=lambda *_: None)
        self.assertEqual(rc, 1)
        self.assertEqual(bar.programs, [])

    def test_happy_path_burns_and_verifies(self):
        bar = BurnBar(rows={290: 0x429004C1})
        bar.reg[ffmacro.SBFE_SHADOW] = 0
        bar.reg[ffmacro.WDATA_WR_SECURE] = 0
        rc = burntool.cmd_burn(bar.rd, bar.wr, self.plan_path,
                               self.dump_path, sleep=noop,
                               clock=zero_clock, log=lambda *_: None)
        self.assertEqual(rc, 0)
        self.assertEqual(bar.programs, [(315, 0x420004C1)])

    def test_live_mismatch_refuses(self):
        bar = BurnBar(rows={290: 0x429004C1, 315: 0x1234})
        bar.reg[ffmacro.SBFE_SHADOW] = 0
        bar.reg[ffmacro.WDATA_WR_SECURE] = 0
        rc = burntool.cmd_burn(bar.rd, bar.wr, self.plan_path,
                               self.dump_path, sleep=noop,
                               clock=zero_clock, log=lambda *_: None)
        self.assertEqual(rc, 1)
        self.assertEqual(bar.programs, [])

    def test_interrupted_run_resumes_and_skips_burned_row(self):
        bar = BurnBar(rows={290: 0x429004C1, 315: 0x420004C1})
        rc = burntool.cmd_burn(bar.rd, bar.wr, self.plan_path,
                               self.dump_path, sleep=noop,
                               clock=zero_clock, log=lambda *_: None)
        self.assertEqual(rc, 0)
        self.assertEqual(bar.programs, [])

    def test_poison_during_burn_returns_3(self):
        bar = BurnBar(rows={290: 0x429004C1}, poison_row=315)
        rc = burntool.cmd_burn(bar.rd, bar.wr, self.plan_path,
                               self.dump_path, sleep=noop,
                               clock=zero_clock, log=lambda *_: None)
        self.assertEqual(rc, 3)
        self.assertEqual(bar.programs, [])

    def test_poisoned_controller_refused(self):
        bar = BurnBar()
        bar.ctrl = 0xBADF510E
        rc = burntool.cmd_burn(bar.rd, bar.wr, self.plan_path,
                               self.dump_path, log=lambda *_: None)
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
