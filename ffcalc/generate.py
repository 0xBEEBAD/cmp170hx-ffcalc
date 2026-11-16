from . import ffrecord, ffrows
from .analyze import analyze, replay_blocks, used_ff_rows
from .optionmap import (FIELDS, FF_ROW_MAX, FF_ROW_MIN, blocks_touched,
                        get_bits, set_bits)


def plan(dump, only=None, skip=(), overrides=None):
    # Without a complete FF-row snapshot we cannot know which rows are
    # occupied or what the current block values are; refuse rather than
    # append records onto already-burnt rows.
    ffrows.require(dump, "generate a burn plan")
    rep = analyze(dump, overrides)
    targets = rep["targets"]
    warnings = list(rep["warnings"])
    words = dump["option_words"]
    only = set(only) if only else None
    skip = set(skip)

    base = {c: dict(b) for c, b in replay_blocks(dump).items()}
    pending = {c: dict(b) for c, b in base.items()}
    selected = []

    for f in FIELDS:
        if not f.burnable or f.key in skip or f.key not in targets:
            continue
        if only is not None and f.key not in only:
            continue
        cur = get_bits(pending, f.chain, f.bit, f.width)
        tgt = targets[f.key]
        if cur == tgt:
            continue
        set_bits(pending.setdefault(f.chain, {}), f.bit, f.width, tgt)
        selected.append(f)

    used = set(used_ff_rows(dump))
    next_row = (max(used) + 1) if used else FF_ROW_MIN
    records = []
    for chain in sorted(pending):
        for blk in sorted(pending[chain]):
            old = base.get(chain, {}).get(blk, 0)
            new = pending[chain][blk]
            if old == new:
                continue
            if next_row > FF_ROW_MAX:
                raise RuntimeError("ran out of free FF rows")
            why = [f.key for f in selected
                   if f.chain == chain and blk in blocks_touched(f)]
            records.append({
                "row": next_row, "chain": chain, "block": blk,
                "old": "0x%04x" % old, "new": "0x%04x" % new,
                "record": "0x%08x" % ffrecord.encode(chain, blk, new),
                "fields": why,
            })
            next_row += 1

    return {
        "sku": dump["sku"],
        "source": dump["source"],
        "uuid": dump.get("uuid"),
        "fingerprint": dump.get("fingerprint"),
        "burn": records,
        "targets": {k: "0x%x" % v for k, v in targets.items()},
        "analysis": {
            "usable_fbp": rep["usable_fbp"], "fb_gib": rep["fb_gib"],
            "sm_count": rep["sm_count"], "sm_per_gpc": rep["sm_per_gpc"],
            "nvlink_ok": rep["nvlink_ok"], "pcie_gen4_ok": rep["pcie_gen4_ok"],
        },
        "warnings": warnings,
    }
