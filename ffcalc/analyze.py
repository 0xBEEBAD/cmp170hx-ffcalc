from . import ffrecord
from .optionmap import (BY_KEY, FIELDS, FF_ROW_MAX, FF_ROW_MIN, TOTAL_FBP,
                        TOTAL_GPC, TPC_PER_GPC, SM_PER_TPC, BYTES_PER_FBP,
                        popcount)


def ff_rows_only(dump):
    return {r: v for r, v in dump["ff_rows"].items()
            if FF_ROW_MIN <= r <= FF_ROW_MAX}


def used_ff_rows(dump):
    return sorted(r for r, v in ff_rows_only(dump).items() if v != 0)


def free_ff_rows(dump):
    used = set(used_ff_rows(dump))
    return [r for r in range(FF_ROW_MIN, FF_ROW_MAX + 1) if r not in used]


def replay_blocks(dump):
    return ffrecord.replay(ff_rows_only(dump))


def decode_current(blocks_by_chain):
    from .optionmap import decode_field
    return {f.key: decode_field(blocks_by_chain, f) for f in FIELDS}


def resolve_targets(dump, overrides=None):
    words = dump["option_words"]
    overrides = overrides or {}
    targets = {}
    warnings = []
    for f in FIELDS:
        if not f.burnable:
            continue
        if f.key in overrides:
            targets[f.key] = overrides[f.key]
        elif f.defect_key and f.defect_key in words:
            targets[f.key] = words[f.defect_key]
        elif f.key in words:
            targets[f.key] = words[f.key]
            if f.category != "pcie":
                warnings.append(
                    "%s: no %s in dump; assuming current value (no-op)"
                    % (f.key, f.defect_key))
        elif f.category == "pcie":
            targets[f.key] = 0
        else:
            warnings.append("%s: no value and no defect key; skipped" % f.key)
        if f.key in targets and f.defect_key and f.defect_key in words:
            defect = words[f.defect_key]
            if (targets[f.key] & defect) != defect:
                warnings.append(
                    "%s: target 0x%x does not contain defective 0x%x "
                    "(would enable known-bad blocks)"
                    % (f.key, targets[f.key], defect))
    return targets, warnings


def analyze(dump, overrides=None):
    targets, warnings = resolve_targets(dump, overrides)
    words = dump["option_words"]

    fbp_cur = words.get("fbp_disable")
    fbp_tgt = targets.get("fbp_disable")
    usable_fbp = None
    fb_gib = None
    fb_now = None
    if fbp_cur is not None:
        fb_now = (TOTAL_FBP - popcount(fbp_cur & 0xfff)) * \
            BYTES_PER_FBP[dump["sku"]]
    if fbp_tgt is not None:
        usable_fbp = TOTAL_FBP - popcount(fbp_tgt & 0xfff)
        fb_gib = usable_fbp * BYTES_PER_FBP[dump["sku"]]
        if dump["sku"] == "8gb" and usable_fbp > 10:
            warnings.append(
                "%d FBP usable -> %d GiB; production A100 caps at 10 FBP/80 "
                "GiB, verify geometry encoding before trusting >80" %
                (usable_fbp, fb_gib))

    gpc_tgt = targets.get("gpc_disable", 0)
    tpc_tgts = [targets.get("tpc_gpc%d_disable" % i, 0) for i in range(TOTAL_GPC)]
    sm_count = None
    per_gpc = []
    if "gpc_disable" in targets:
        sm_count = 0
        for i in range(TOTAL_GPC):
            if (gpc_tgt >> i) & 1:
                per_gpc.append(0)
                continue
            t = TPC_PER_GPC - popcount(tpc_tgts[i] & 0xff)
            per_gpc.append(t * SM_PER_TPC)
            sm_count += t * SM_PER_TPC

    nvlink = targets.get("nvlink_disable")
    pcie_clear = all(targets.get(k) == 0 for k in (
        "disable_gen3_speed_bit1", "disable_gen3_speed_bit9",
        "pcie_magic_gen4_dis", "pcie_boot_gen23_dis", "pcie_boot_gen3_dis"))

    return {
        "sku": dump["sku"],
        "uuid": dump.get("uuid"),
        "fingerprint": dump.get("fingerprint"),
        "ff_rows_used": len(used_ff_rows(dump)),
        "ff_rows_free": len(free_ff_rows(dump)),
        "current": decode_current(replay_blocks(dump)),
        "words": words,
        "targets": targets,
        "usable_fbp": usable_fbp,
        "fb_gib": fb_gib,
        "fb_now_gib": fb_now,
        "sm_count": sm_count,
        "sm_per_gpc": per_gpc,
        "nvlink_ok": (nvlink == 0) if nvlink is not None else None,
        "pcie_gen4_ok": pcie_clear,
        "warnings": warnings,
    }
