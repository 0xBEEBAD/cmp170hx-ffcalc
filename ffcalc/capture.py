#!/usr/bin/env python3
"""Capture option words + raw FF rows from a live CMP 170HX, read-only.

Pure reads against BAR0 via /sys/.../resource0 (O_RDONLY, no writes at all).
Address table is vendored at ffcalc/data/ga100-fuses.json.

Raw FF rows are read through the fuse macro's CMD_READ mechanism. That path
requires WRITING the command register, so it is OFF by default in this tool;
enable with --read-ff-rows only on an idle card.

Run on the card's host as root:
    python3 -m ffcalc.capture --bdf 0000:05:00.0 --sku 8gb -o dump.json
"""
import argparse
import io
import json
import mmap
import os
import struct
import sys

BAR0_LEN = 0x1000000

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAP = os.path.join(_HERE, "data", "ga100-fuses.json")

FF_ROW_MIN = 222
FF_ROW_MAX = 486


def open_bar0(bdf=None):
    base = "/sys/bus/pci/devices"
    cands = []
    for dev in sorted(os.listdir(base)):
        vp = os.path.join(base, dev, "device")
        try:
            with io.open(vp) as f:
                dev_id = f.read().strip()
        except IOError:
            continue
        if dev_id != "0x20c2":
            continue
        cands.append(dev)
    if bdf:
        want = bdf if len(bdf.split(":")) == 3 else "0000:" + bdf
        cands = [c for c in cands if c == want] or [want]
    if not cands:
        raise SystemExit("no device 0x20c2 found")
    path = os.path.join(base, cands[0], "resource0")
    # Same access pattern cmpunlocker uses against a live nvidia driver:
    # fd opened O_RDWR (we never write), mapping ACCESS_WRITE, reads only.
    fd = os.open(path, os.O_SYNC | os.O_RDWR)
    try:
        length = os.path.getsize(path) or BAR0_LEN
        mm = mmap.mmap(fd, length, access=mmap.ACCESS_WRITE)
    finally:
        os.close(fd)
    return cands[0], mm


def rd(mm, addr):
    mm.seek(int(addr, 0))
    return struct.unpack("<I", mm.read(4))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bdf")
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--sku", default="8gb", choices=("8gb", "10gb"))
    ap.add_argument("-o", "--out", default="dump.json")
    ap.add_argument("--read-ff-rows", action="store_true",
                    help="write CMD_READ to fuse macro (NOT read-only)")
    args = ap.parse_args()

    with io.open(args.map, encoding="utf-8") as f:
        m = json.load(f)

    bdf, mm = open_bar0(args.bdf)
    words = {}
    for key, loc in m["fields"].items():
        fused = rd(mm, loc["fused"]) if loc.get("fused") else None
        status = rd(mm, loc["status"]) if loc.get("status") else None
        cur = status if status is not None else fused
        words[key] = "0x%x" % cur
        if fused is not None and status is not None and fused != status:
            words[key + "__fused"] = "0x%x" % fused

    for key, spec in m["bit_fields"].items():
        w = rd(mm, spec["reg"])
        words[key] = "0x%x" % ((w >> spec["bit"]) & 1)

    for key, addr in m["defective"].items():
        words[key] = "0x%x" % rd(mm, addr)

    info = {}
    for name, addr in m["info"].items():
        info[name] = "0x%x" % rd(mm, addr)

    dump = {"sku": args.sku, "bdf": bdf, "ff_rows": {}, "option_words": words,
            "info": info, "read_only": not args.read_ff_rows}
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, sort_keys=True)
    print("wrote %s (%s)" % (args.out, bdf))


if __name__ == "__main__":
    sys.exit(main())
