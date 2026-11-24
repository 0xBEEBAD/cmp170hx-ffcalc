from . import ffrecord, ffrows
from .analyze import analyze, replay_blocks, used_ff_rows
from .optionmap import (A100_DEVID, A100_MAGIC_D, CMP_DEVID, DEVID_KEYS,
                        FIELDS, FF_ROW_MAX, FF_ROW_MIN, block_mask, get_bits,
                        set_bits)

# What the fused-device-id rewrite means, said out loud in every plan that
# carries it: it is the one burn that changes what the card *is*, and it drags
# an invasive vBIOS flash in with it. The two records come from a community
# conversion report -- this repo has never walked the path on its own card, so
# the warning says "reported", not "verified".
A100_ID_WARNINGS = (
    "pcie_devida/pcie_devidb: rewrites the FUSED device id 0x%04x -> 0x%04x "
    "(chain2 blk37/38 -> 0x%04x, the records the community conversion burned: "
    "0x416A0942/0x416A0982, not yet reproduced on our own card). From the next "
    "cold boot the chip itself is 10de:%04x: lspci, the driver and the vBIOS "
    "boot gate all see an A100, so the card needs an A100 80GB PCIe vBIOS in "
    "its SPI rom to come up (nvflash --save the CMP rom first, only your own "
    "dump counts). Full capability + Gen4 do NOT need this: the default plan "
    "already gives them"
    % (CMP_DEVID, A100_DEVID, A100_DEVID << 1, A100_DEVID),
    "pcie_devida/pcie_devidb: this plan adds an invasive step on top of the "
    "fuse burn -- a vBIOS flash. Fused as an A100 while the rom is still the "
    "CMP one, the id mismatch keeps the card from initialising, and if the "
    "platform then fails to POST there is no in-band way back (no driver, no "
    "nvflash): CH341A + SOP8 clip on the SPI rom is the only recovery, so do "
    "not burn these rows without it. The platform BIOS must map a 128 GiB "
    "64-bit prefetchable BAR1 (Above 4G Decoding on, CSM off, Re-Size BAR on, "
    "MMIO High Size >= 128G); the judgement is the MMIO window, not how new "
    "the board is",
    "pcie_devida/pcie_devidb: the id can be pointed at another value by a "
    "later chain-2 record (later records win), but only while the card still "
    "boots into a burn window",
)

# --a100 rewrites the whole PCIe capability declaration word along with the id:
# someone paying the vBIOS price to look like an A100 wants the full A100
# declaration too, not a half-converted word that still carries CMP-era bits.
# That is our reasoning, not the community report's -- the conversion they
# describe leaves MAGIC_BITS_D alone, and nobody has read the word off a real
# A100 -- so it warns, and says how to check the value. It is not part of
# A100_ID_WARNINGS because --set magic_d=HEX can reach it without --a100.
A100_MAGIC_WARNING = (
    "magic_d -> 0x%08x: MAGIC_BITS_D is a PCIe capability declaration word "
    "(bit25 = 'no Gen4'), so rewriting it changes what the link may claim, "
    "not anything measured. Clearing bit25 alone is already enough to run "
    "Gen4, and the community conversion report does not touch this word at "
    "all; it is rewritten here because a card that takes an A100 id should "
    "not go on declaring CMP-era capability next to it. Any whole-word value, "
    "0x%08x included, is an EXPERIMENTAL guess nobody has read off a real "
    "A100: check it against one (BAR0 0x820520) first, or --skip magic_d to "
    "burn only what the report actually did"
)


def plan(dump, only=None, skip=(), overrides=None, a100=False):
    # Without a complete FF-row snapshot we cannot know which rows are
    # occupied or what the current block values are; refuse rather than
    # append records onto already-burnt rows.
    ffrows.require(dump, "generate a burn plan")
    only = set(only) if only else None
    skip = set(skip)
    if a100:
        # Community-reported path: the boot gate that refuses an A100 vBIOS
        # compares the *fused* device id with the rom's, so the conversion
        # burns DEVIDA/DEVIDB to 0x20b5 and then flashes an A100 80GB PCIe
        # vBIOS -- the rom itself stays byte-identical to stock. We have not
        # walked it on our own card, hence the warnings below.
        #
        # MAGIC_BITS_D is added on top: the word states what the link may
        # claim, and a card that goes as far as a new identity should not keep
        # declaring CMP-only capability next to an A100 id. Not in the
        # conversion report, hence its own warning.
        #
        # --skip/--only are respected here, not just downstream: a target named
        # without any record behind it would leave `sim` reporting an unfilled
        # target for a field this plan was told to leave alone.
        overrides = dict(overrides or {})
        for key in DEVID_KEYS + ("magic_d",):
            if key in skip or (only is not None and key not in only):
                continue
            overrides.setdefault(key, A100_MAGIC_D if key == "magic_d"
                                 else A100_DEVID)
    rep = analyze(dump, overrides)
    targets = rep["targets"]
    warnings = list(rep["warnings"])
    words = dump["option_words"]

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
                   if f.chain == chain and
                   (old ^ new) & block_mask(f, blk)]
            records.append({
                "row": next_row, "chain": chain, "block": blk,
                "old": "0x%04x" % old, "new": "0x%04x" % new,
                "record": "0x%08x" % ffrecord.encode(chain, blk, new),
                "fields": why,
            })
            next_row += 1

    # Say what the identity rows in this plan actually do -- a plan file is a
    # burn order, and whoever executes it must not have to know the history.
    in_plan = {key for rec in records for key in rec["fields"]}
    ids_in_plan = [key for key in DEVID_KEYS if key in in_plan]
    if ids_in_plan:
        warnings.extend(A100_ID_WARNINGS)
        for key in ids_in_plan:
            # A card that already answers to 0x20c2 replays it from its FF
            # records. When it does not, `old` is not the id in use, and the
            # operator should hear that from the plan rather than conclude the
            # fuse register was lying.
            live = words.get(key)
            if live is not None and rep["current"].get(key) != live:
                replayed = rep["current"].get(key, 0)
                warnings.append(
                    "%s: this snapshot replays 0x%04x while the fuse register "
                    "reads 0x%04x, so the planned old value is not the id the "
                    "card answers to today (the id can live in rows this "
                    "readout does not cover); the record replaces the whole "
                    "block either way" % (key, replayed, live))
        if len(ids_in_plan) == 1:
            warnings.append(
                "only %s is rewritten here: the two halves of the id are "
                "meant to agree, and which one the boot gate reads is not "
                "documented, so burn both in the same window unless you know "
                "why you are not" % ids_in_plan[0])
    if "magic_d" in in_plan:
        # Whoever burns this word, from --a100 or an explicit --set, has to
        # know the value is a guess and what the cheaper alternative was.
        warnings.append(A100_MAGIC_WARNING % (targets["magic_d"],
                                             A100_MAGIC_D))

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
