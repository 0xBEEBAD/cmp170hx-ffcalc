#!/usr/bin/env python3
"""Execute a verified ffcalc burn plan on a live CMP 170HX.

    python3 -m ffcalc.burntool status --bdf 0000:05:00.0
    python3 -m ffcalc.burntool burn  plans/<uuid>.burn.json \
        --dump cards/<uuid>.json --bdf 0000:05:00.0 [--rows 315] [--yes]

burn refuses to touch the card unless:
  1. simulate() on the plan against the card dump is error-free (row
     occupancy, old values, re-encode, FF range all check out);
  2. the fuse controller sits in the write window (idle, FUSEDEBUG=0,
     CRC=0x55, EFP=1, DSOV=0) -- checked again after CMD_SENSE;
  3. every target row still reads blank (0x00000000) at burn time -- or
     already holds exactly its planned record (interrupted run: that row is
     skipped, re-running the same plan is safe).
Each record is programmed one at a time and read back; a poisoned
controller (FUSECTRL=0xBADFxxxx) aborts the whole run and you must
cold-boot before any further access. See docs/BURNING.md.
"""
import argparse
import functools
import sys

from . import ffmacro
from .capture import open_bar0, rd as _rd, wr as _wr
from .dump import BadSnapshot, load_dump, load_plan
from .simulate import simulate


def _load_pair(plan_path, dump_path, log):
    """(plan, dump) or None after a REFUSED line.

    Both files are load-bearing: the snapshot is what every row is re-read
    against before an irreversible burn, so an unreadable or truncated one
    means "do not touch the card" -- said out loud, not as a Python
    traceback in front of somebody holding a live card. BadPlan is a
    BadSnapshot, so one handler covers both files.
    """
    try:
        plan = load_plan(plan_path)
        dump = load_dump(dump_path)
    except BadSnapshot as e:
        log("REFUSED: %s" % e)
        return None
    return plan, dump


def cmd_status(rd, log=print):
    st = ffmacro.status(rd)
    for name in sorted(st):
        log("%-16s 0x%08x" % (name, st[name]))
    if ffmacro.is_poisoned(st["FUSECTRL"]):
        log("=> POISONED: stop reads, cold-boot the card")
        return 3
    log("=> write window: %s" % ("OPEN" if ffmacro.in_write_window(st)
                                 else "closed"))
    return 0


def cmd_burn(rd, wr, plan_path, dump_path, rows=None, log=print,
             sleep=None, clock=None, force=False):
    loaded = _load_pair(plan_path, dump_path, log)
    if loaded is None:
        return 1
    plan, dump = loaded
    res = simulate(dump, plan)
    for w in res["warnings"]:
        log("warn: %s" % w)
    if res["errors"]:
        for e in res["errors"]:
            log("REFUSED: %s" % e)
        return 1
    wanted = None
    if rows:
        wanted = {int(r, 0) for r in rows.split(",")}
        unknown = wanted - {rec["row"] for rec in plan["burn"]}
        if unknown:
            log("REFUSED: rows %s not in plan" % sorted(unknown))
            return 1
    kw = {}
    if sleep is not None:
        kw["sleep"] = sleep
    if clock is not None:
        kw["clock"] = clock

    st = ffmacro.status(rd)
    if ffmacro.is_poisoned(st["FUSECTRL"]):
        log("REFUSED: controller poisoned (FUSECTRL=0x%08x); cold-boot first"
            % st["FUSECTRL"])
        return 3
    if not force and not ffmacro.in_write_window(st):
        log("REFUSED: not in write window: %s"
            % (ffmacro.armed_problems(rd, wr) or "idle/CRC/EFP/DSOV check"))
        return 1

    log("CMD_SENSE ...")
    ffmacro.sense(rd, wr, **kw)
    log("CMD_SENSE ok (DSOV=0, inside window)")
    already = set()
    if not force:
        missing = {rec["row"] for rec in plan["burn"]} - \
                  {r for r in dump["ff_rows"]}
        if missing:
            log("REFUSED: dump does not cover rows %s" % sorted(missing))
            return 1
        for rec in plan["burn"]:
            live = ffmacro.read_row(rd, wr, rec["row"], **kw)
            snap = dump["ff_rows"].get(rec["row"])
            if live == snap:
                continue
            if live == int(rec["record"], 0):
                log("row %d already holds this record; will skip "
                    "(interrupted run? resuming is safe)" % rec["row"])
                already.add(rec["row"])
                continue
            log("REFUSED: row %d changed since snapshot "
                "(snapshot 0x%08x, live 0x%08x) -- regenerate the plan"
                % (rec["row"], snap, live))
            return 1
        log("snapshot matches card (%d rows re-read, %d already burned)"
            % (len(plan["burn"]), len(already)))

    try:
        log("arm (SBFE shadow + WDATA_WR_SECURE) ...")
        ffmacro.arm(rd, wr)
        done = 0
        for rec in plan["burn"]:
            if wanted is not None and rec["row"] not in wanted:
                continue
            if rec["row"] in already:
                done += 1
                log("row %3d: already burned, skipped (%s)"
                    % (rec["row"], ",".join(rec.get("fields", []))))
                continue
            record = int(rec["record"], 0)
            got = ffmacro.program_row(rd, wr, rec["row"], record, **kw)
            done += 1
            log("row %3d: burned 0x%08x ok (%s)"
                % (rec["row"], got, ",".join(rec.get("fields", []))))
    except ffmacro.FuseControllerError as exc:
        log("ABORT: %s" % exc)
        log("treat the card as suspect: stop all BAR0 reads, cold-boot, "
            "then re-check with `status` (docs/BURNING.md)")
        return 3
    st = ffmacro.status(rd)
    log("post: FUSECTRL=0x%08x FUSEDEBUG=0x%x CRC=0x%x DSOV=%d"
        % (st["FUSECTRL"], st["FUSEDEBUG"], st["CRC_STATUS"],
           st["DSOV_STATUS"]))
    log("done: %d/%d records. Cold boot now, then re-run ffscan.sh: "
        "fingerprint must be unchanged, plan must shrink"
        % (done, len(plan["burn"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ffcalc.burntool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("status")
    sp.add_argument("--bdf")
    bp = sub.add_parser("burn")
    bp.add_argument("plan")
    bp.add_argument("--dump", required=True)
    bp.add_argument("--bdf")
    bp.add_argument("--rows", help="subset, e.g. '315' for a first test row")
    bp.add_argument("--force", action="store_true",
                    help="skip live-snapshot re-match (NOT recommended)")
    bp.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    bdf, mm = open_bar0(args.bdf)
    r = functools.partial(_rd, mm)
    w = functools.partial(_wr, mm)
    print("card at %s" % bdf)
    if args.cmd == "status":
        return cmd_status(r)
    if not args.yes:
        ans = input("burn %s onto %s? type YES to continue: "
                    % (args.plan, bdf))
        if ans.strip() != "YES":
            print("aborted")
            return 2
    return cmd_burn(r, w, args.plan, args.dump, rows=args.rows,
                    force=args.force)


if __name__ == "__main__":
    sys.exit(main())
