import argparse
import io
import json
import os
import sys

from . import ffrows, ingest
from .analyze import analyze
from .dump import load_dump
from .generate import plan
from .optionmap import BY_KEY, FF_ROW_MAX, FF_ROW_MIN
from .simulate import simulate


def _apply_ffrows_option(dump, path):
    """Merge an external FF rows file into a dump (shared by --ffrows)."""
    try:
        rows = ffrows.parse_file(path)
    except ValueError as e:
        raise SystemExit("bad FF rows file %s: %s" % (path, e))
    return ffrows.merge_into(dump, rows)


def _write_dump_ffrows(dump_path, rows):
    """Update only ff_rows in a saved card JSON, preserving other fields."""
    with io.open(dump_path, encoding="utf-8") as f:
        raw = json.load(f)
    merged = dict((int(k, 0), v) for k, v in (raw.get("ff_rows") or {}).items())
    for row, value in sorted(rows.items()):
        merged[row] = value
    raw["ff_rows"] = dict(("%d" % r, "0x%08x" % v)
                          for r, v in sorted(merged.items()))
    with io.open(dump_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, sort_keys=True)
    return ffrows.coverage({"ff_rows": merged})


def _parse_sets(pairs):
    out = {}
    for p in pairs or ():
        if "=" not in p:
            raise SystemExit("--set expects key=hex, got %r" % p)
        k, v = p.split("=", 1)
        if k not in BY_KEY:
            raise SystemExit("unknown option key %r; known: %s"
                             % (k, ",".join(sorted(BY_KEY))))
        out[k] = int(v, 0)
    return out


def _print_analysis(rep):
    w = rep["words"]
    if rep.get("uuid"):
        print("Card UUID: %s   fingerprint: %s"
              % (rep["uuid"], rep.get("fingerprint")))
    if rep.get("ff_rows_state") == "complete":
        rows_desc = "%d used, %d free (rows %d-%d)" % (
            rep["ff_rows_used"], rep["ff_rows_free"], FF_ROW_MIN, FF_ROW_MAX)
    else:
        rows_desc = ("NOT READ (%s) -- no burn plan until FF rows are "
                     "captured and merged (ffcalc mergeff)"
                     % rep.get("ff_rows_state"))
    print("SKU: %s   FF rows: %s" % (rep["sku"], rows_desc))
    if rep["usable_fbp"] is not None:
        print("\n[Framebuffer potential]")
        print("  fbp_disable  current=0x%x  target=0x%x"
              % (w.get("fbp_disable", 0), rep["targets"].get("fbp_disable", 0)))
        print("  usable FBP: %d / 12" % rep["usable_fbp"])
        print("  potential framebuffer: %d GiB" % rep["fb_gib"])
    if rep["sm_count"] is not None:
        print("\n[SM potential]")
        print("  gpc_disable target=0x%x" % rep["targets"].get("gpc_disable", 0))
        print("  SM per GPC: %s" % ",".join(map(str, rep["sm_per_gpc"])))
        print("  potential SMs: %d" % rep["sm_count"])
    print("\n[Other unlocks]")
    nv = rep["nvlink_ok"]
    print("  NVLink: %s%s" % (
        "unlockable" if nv else "stays disabled",
        "  (needs U11 bridge mod + ROM check bypass)" if nv else ""))
    print("  PCIe Gen3/4: %s" % ("bits clearable" if rep["pcie_gen4_ok"]
                                 else "stays restricted"))
    for msg in rep["warnings"]:
        print("  WARN: %s" % msg)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ffcalc",
                                 description="GA100 CMP 170HX FF fuse tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report", help="unlock potential of one card dump")
    p.add_argument("dump")
    p.add_argument("--set", action="append", metavar="KEY=HEX",
                   help="override a target option value")

    p = sub.add_parser("gen", help="generate FF burn records from a dump "
                       "(refuses if current FF records were not read)")
    p.add_argument("dump")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--set", action="append", metavar="KEY=HEX")
    p.add_argument("--only", help="comma-separated keys to consider")
    p.add_argument("--skip", help="comma-separated keys to ignore")
    p.add_argument("--ffrows", metavar="FILE",
                   help="FF rows file to merge before planning "
                        "(see ffcalc mergeff)")

    p = sub.add_parser("mergeff", help="merge an externally-read FF rows "
                       "dump (e.g. fuse macro CMD_READ) into a card dump")
    p.add_argument("dump", help="card dump json to update in place")
    p.add_argument("rows", help="FF rows file: one '<row> <value>' per line")

    p = sub.add_parser("sim", help="replay dump + burn plan and verify")
    p.add_argument("dump")
    p.add_argument("burn")
    p.add_argument("--ffrows", metavar="FILE",
                   help="FF rows file to merge before replaying")

    p = sub.add_parser("scan", help="analyze every card in a collected dir; "
                       "print table, save UUID-keyed dumps and burn plans")
    p.add_argument("--dir", required=True,
                   help="collected dir (bar0map.tsv + probe_*.txt)")
    p.add_argument("--outdir", default="cards")
    p.add_argument("--plans", default="plans")
    p.add_argument("--sku", default="8gb", choices=("8gb", "10gb"))
    p.add_argument("--no-gen", action="store_true")

    p = sub.add_parser("verify", help="check that a burn plan belongs to "
                       "the card currently in the machine")
    p.add_argument("burn")
    p.add_argument("--dir", help="fresh collected dir")
    p.add_argument("--dump", help="fresh single-card dump json")
    p.add_argument("--ffrows", metavar="FILE",
                   help="FF rows file to merge into the fresh read before "
                        "replaying the plan")

    args = ap.parse_args(argv)

    if args.cmd == "report":
        dump = load_dump(args.dump)
        _print_analysis(analyze(dump, _parse_sets(args.set)))
        return 0

    if args.cmd == "mergeff":
        try:
            rows = ffrows.parse_file(args.rows)
        except ValueError as e:
            print("FAIL: bad FF rows file %s: %s" % (args.rows, e))
            return 2
        state, missing = _write_dump_ffrows(args.dump, rows)
        print("merged %d FF row(s) into %s" % (len(rows), args.dump))
        if state == "complete":
            print("FF rows %d-%d fully covered; burn plan available: "
                  "ffcalc gen %s -o plans/<uuid>.burn.json"
                  % (FF_ROW_MIN, FF_ROW_MAX, args.dump))
            return 0
        print("STILL %s: %d of %d FF rows unknown (rows %d-%d; e.g. %s)"
              % (state, len(missing), ffrows.N_FF_ROWS, FF_ROW_MIN,
                 FF_ROW_MAX, ffrows.fmt_rows(missing)))
        print("unburnt rows must be listed as 0; how to fix: %s"
              % ffrows.GUIDANCE)
        return 1

    if args.cmd == "gen":
        dump = load_dump(args.dump)
        if args.ffrows:
            _apply_ffrows_option(dump, args.ffrows)
        only = args.only.split(",") if args.only else None
        skip = args.skip.split(",") if args.skip else []
        try:
            obj = plan(dump, only=only, skip=skip,
                       overrides=_parse_sets(args.set))
        except ffrows.FFRowsNotRead as e:
            print("REFUSED: %s" % e.detail)
            print("how to fix: %s" % ffrows.GUIDANCE)
            print("no burn plan was written to %s" % args.out)
            return 2
        with io.open(args.out, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
        print("card: %s  fingerprint: %s"
              % (dump.get("uuid") or "none (no nvidia-smi data)",
                 dump.get("fingerprint")))
        print("planned %d FF record(s):" % len(obj["burn"]))
        for rec in obj["burn"]:
            print("  row %-4d rec %s  chain%d blk%-3d %s -> %s  [%s]"
                  % (rec["row"], rec["record"], rec["chain"], rec["block"],
                     rec["old"], rec["new"], ",".join(rec["fields"])))
        a = obj["analysis"]
        print("potential: %s GiB FB, %s SMs" % (a["fb_gib"], a["sm_count"]))
        for msg in obj["warnings"]:
            print("WARN: %s" % msg)
        return 0

    if args.cmd == "sim":
        dump = load_dump(args.dump)
        if args.ffrows:
            _apply_ffrows_option(dump, args.ffrows)
        with io.open(args.burn, encoding="utf-8") as f:
            burn_obj = json.load(f)
        res = simulate(dump, burn_obj)
        print("%-24s %-9s %-8s %-10s %-10s" %
              ("key", "chain/bit", "before", "after", "status"))
        for row in res["table"]:
            st = "CHANGED" if row["changed"] else "same"
            print("%-24s c%d b%-6s %-10s %-10s %s"
                  % (row["key"], row["chain"], row["bit"],
                     row["before"], row["after"], st))
        for msg in res["warnings"]:
            print("WARN: %s" % msg)
        if res["pending"]:
            print("PENDING (targeted but not in this burn set): %s"
                  % ",".join(res["pending"]))
        for msg in res["errors"]:
            print("FAIL: %s" % msg)
        if res["errors"]:
            print("SIMULATION FAILED (%d issue(s))" % len(res["errors"]))
            return 1
        print("SIMULATION OK")
        return 0

    if args.cmd == "scan":
        m = ingest.load_map()
        rows = []
        no_ffrows = []
        for dump in ingest.read_collected(args.dir, m, sku=args.sku):
            saved = os.path.join(args.outdir,
                                 ingest.card_id(dump) + ".json")
            if not dump["ff_rows"] and os.path.exists(saved):
                try:
                    prev = load_dump(saved)
                except ValueError:
                    prev = None
                if prev and prev.get("ff_rows"):
                    dump["ff_rows"] = prev["ff_rows"]
            rep = analyze(dump)
            dpath = ingest.save_dump(args.outdir, dump)
            pinfo = "-" if args.no_gen else "SKIPPED (FF rows not read)"
            if not args.no_gen:
                try:
                    obj = plan(dump)
                except ffrows.FFRowsNotRead:
                    no_ffrows.append(dpath)
                else:
                    if not os.path.isdir(args.plans):
                        os.makedirs(args.plans)
                    ppath = os.path.join(args.plans,
                                         ingest.card_id(dump) + ".burn.json")
                    with io.open(ppath, "w", encoding="utf-8") as f:
                        json.dump(obj, f, indent=2, sort_keys=True)
                    pinfo = "%s (%d records)" % (ppath, len(obj["burn"]))
            rows.append((dump, rep, pinfo))
        if not rows:
            print("no cards found under %s/bar0map.tsv" % args.dir)
            return 1
        print("%-38s  %-12s  %-13s  %4s  %-9s  plan"
              % ("card UUID", "BDF", "FB now->max", "SM", "FP"))
        for dump, rep, pinfo in rows:
            fb = ("%s->%s GiB" % (rep["fb_now_gib"], rep["fb_gib"])
                  if rep["fb_now_gib"] is not None else "?")
            print("%-38s  %-12s  %-13s  %4s  %-9s  %s"
                  % (dump.get("uuid") or "GPU-" + dump["fingerprint"],
                     dump.get("bdf", "?"), fb,
                     rep["sm_count"] if rep["sm_count"] is not None else "?",
                     dump["fingerprint"], pinfo))
        for dump, rep, _ in rows:
            for wmsg in rep["warnings"]:
                print("WARN %s: %s" % (dump.get("uuid") or
                                       dump.get("fingerprint"), wmsg))
        if no_ffrows:
            print("\nNO BURN PLAN for %d card(s): their current FF records "
                  "were never read (read-only probe cannot see FF rows; "
                  "burning blind could reuse occupied rows)."
                  % len(no_ffrows))
            for dpath in no_ffrows:
                print("  %s: ffcalc mergeff %s <ffrows.txt>" % (dpath, dpath))
            print("how to fix: %s" % ffrows.GUIDANCE)
            return 1
        return 0

    if args.cmd == "verify":
        with io.open(args.burn, encoding="utf-8") as f:
            burn_obj = json.load(f)
        puuid, pfp = burn_obj.get("uuid"), burn_obj.get("fingerprint")
        if not puuid and not pfp:
            print("FAIL: plan has no uuid/fingerprint; re-generate it")
            return 1
        if args.dir:
            dumps = list(ingest.read_collected(args.dir))
        elif args.dump:
            dumps = [load_dump(args.dump)]
        else:
            print("FAIL: verify needs --dir <collected> or --dump <json>")
            return 2
        if args.ffrows:
            for d in dumps:
                _apply_ffrows_option(d, args.ffrows)
        match = None
        for d in dumps:
            if (puuid and d.get("uuid") == puuid) or \
                    (pfp and d.get("fingerprint") == pfp):
                match = d
                break
        if match is None:
            print("FAIL: plan for %s (%s) not present among %d card(s)"
                  % (puuid or "?", pfp or "?", len(dumps)))
            for d in dumps:
                print("  present: %s fp=%s"
                      % (d.get("uuid") or "?", d.get("fingerprint")))
            return 1
        if pfp and match.get("fingerprint") != pfp:
            print("FAIL: UUID matches but fingerprint differs "
                  "(%s vs %s): defective words changed?"
                  % (pfp, match.get("fingerprint")))
            return 1
        print("OK: plan %s belongs to card %s (bdf %s, fingerprint %s)"
              % (args.burn, match.get("uuid") or "GPU-" + pfp,
                 match.get("bdf", "?"), pfp))
        res = simulate(match, burn_obj)
        for msg in res["errors"]:
            print("FAIL: %s" % msg)
        return 1 if res["errors"] else 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
