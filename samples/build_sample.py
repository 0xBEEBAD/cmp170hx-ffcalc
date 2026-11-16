import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ffcalc import ffrecord
from ffcalc.optionmap import BY_KEY, set_bits

EFFECTIVE = {
    "fbio_disable": 0x000000,
    "fbpa_disable": 0xC0330C,
    "gpc_disable": 0x00,
    "tpc_gpc0_disable": 0xC0,
    "tpc_gpc1_disable": 0xC0,
    "tpc_gpc2_disable": 0xE0,
    "tpc_gpc3_disable": 0xE0,
    "tpc_gpc4_disable": 0xE0,
    "tpc_gpc5_disable": 0xF0,
    "tpc_gpc6_disable": 0xF0,
    "tpc_gpc7_disable": 0xF8,
    "pes_disable": 0x000000,
    "fbp_disable": 0x852,
    "rop_l2_disable": 0x000C40,
    "nvlink_disable": 0x7,
    "sys_pipe_disable": 0x2,
}

DEFECTIVE = {
    "fbio_defective": 0x000000,
    "fbpa_defective": 0xC03000,
    "gpc_defective": 0x00,
    "tpc_gpc0_defective": 0x80,
    "tpc_gpc1_defective": 0x40,
    "tpc_gpc2_defective": 0x20,
    "tpc_gpc3_defective": 0x10,
    "tpc_gpc4_defective": 0x08,
    "tpc_gpc5_defective": 0x04,
    "tpc_gpc6_defective": 0x02,
    "tpc_gpc7_defective": 0x01,
    "pes_defective": 0x000000,
    "fbp_defective": 0x840,
    "rop_l2_defective": 0x840,
    "nvlink_defective": 0x0,
    "sys_pipe_defective": 0x0,
}

PCIE_EFFECTIVE = {
    "disable_gen3_speed_bit1": 1,
    "disable_gen3_speed_bit9": 0,
    "pcie_magic_gen4_dis": 1,
    "pcie_boot_gen23_dis": 1,
    "pcie_boot_gen3_dis": 1,
}


def build_rows(values):
    blocks = {}
    for key, value in values.items():
        f = BY_KEY[key]
        set_bits(blocks.setdefault(f.chain, {}), f.bit, f.width, value)
    rows = {}
    row = 222
    for chain in sorted(blocks):
        for blk in sorted(blocks[chain]):
            rows[row] = "0x%08x" % ffrecord.encode(
                chain, blk, blocks[chain][blk])
            row += 1
    return rows


FF_ROW_MIN, FF_ROW_MAX = 222, 486


def main():
    rows = build_rows(dict(EFFECTIVE, **PCIE_EFFECTIVE))
    # A real readout lists every FF row; unburnt rows read back as 0.
    for r in range(FF_ROW_MIN, FF_ROW_MAX + 1):
        rows.setdefault(r, "0x00000000")
    dump = {
        "sku": "8gb",
        "comment": "synthetic dump modeled on kate-land blog card "
                   "(FBP_DISABLE=0x852, FBP_DEFECTIVE=0x840); FF region "
                   "fully listed, unburnt rows = 0",
        "ff_rows": rows,
        "option_words": dict(EFFECTIVE, **DEFECTIVE),
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "blog_card_example.json")
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, sort_keys=True)
    print(out)
    for r in sorted(rows, key=lambda x: int(x)):
        if int(rows[r], 0):
            print("  row %s = %s" % (r, rows[r]))


if __name__ == "__main__":
    main()
