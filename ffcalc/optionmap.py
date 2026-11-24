from dataclasses import dataclass

FF_ROW_MIN = 222
FF_ROW_MAX = 486
# Physical fuse array: every row a snapshot or a rows file may name.
PHYS_ROW_MIN = 0
PHYS_ROW_MAX = 511

TOTAL_GPC = 8
TPC_PER_GPC = 8
SM_PER_TPC = 2
TOTAL_FBP = 12
BYTES_PER_FBP = {"8gb": 8, "10gb": 4}


@dataclass(frozen=True)
class OptionField:
    key: str
    name: str
    chain: int
    bit: int
    width: int
    category: str
    defect_key: str = ""
    note: str = ""
    burnable: bool = True
    opt_in: bool = False


def _tpc_fields():
    out = []
    for i in range(8):
        out.append(OptionField(
            "tpc_gpc%d_disable" % i,
            "NV_FUSE_OPT_TPC_GPC%d_DISABLE" % i,
            1, 187 + 8 * i, 8, "floorsweep", "tpc_gpc%d_defective" % i))
    return out


FIELDS = [
    OptionField("fbio_disable", "NV_FUSE_OPT_FBIO_DISABLE",
                1, 83, 24, "floorsweep", "fbio_defective"),
    OptionField("fbpa_disable", "NV_FUSE_OPT_FBPA_DISABLE",
                1, 107, 24, "floorsweep", "fbpa_defective"),
    OptionField("gpc_disable", "NV_FUSE_OPT_GPC_DISABLE",
                1, 131, 8, "floorsweep", "gpc_defective"),
] + _tpc_fields() + [
    OptionField("pes_disable", "NV_FUSE_OPT_PES_DISABLE",
                1, 251, 24, "floorsweep", "pes_defective"),
    OptionField("fbp_disable", "NV_FUSE_OPT_FBP_DISABLE",
                1, 307, 12, "floorsweep", "fbp_defective"),
    OptionField("rop_l2_disable", "NV_FUSE_OPT_ROP_L2_DISABLE",
                1, 319, 24, "floorsweep", "rop_l2_defective"),
    OptionField("nvlink_disable", "NV_FUSE_OPT_NVLINK_DISABLE",
                1, 368, 3, "nvlink", "nvlink_defective",
                "NVLink additionally needs the U11 bridge mod + ROM check bypass"),
    OptionField("sys_pipe_disable", "NV_FUSE_OPT_SYS_PIPE_DISABLE",
                1, 372, 8, "mig", "sys_pipe_defective",
                "MIG/ECC/NVDEC unlock untested per kate-land blog"),
    OptionField("pcie_devida", "NV_FUSE_OPT_PCIE_DEVIDA",
                2, 593, 16, "identity", "", "device identity, display only",
                burnable=False),
    OptionField("pcie_devidb", "NV_FUSE_OPT_PCIE_DEVIDB",
                2, 609, 16, "identity", "", "device identity, display only",
                burnable=False),
    OptionField("disable_gen3_speed_bit1", "NV_FUSE_OPT_DISABLE_GEN3_SPEED",
                2, 39 * 16 + 1, 1, "pcie", "",
                "which of bits 1/9 is unresolved upstream; burn both by default"),
    OptionField("disable_gen3_speed_bit9", "NV_FUSE_OPT_DISABLE_GEN3_SPEED",
                2, 39 * 16 + 9, 1, "pcie", "",
                "which of bits 1/9 is unresolved upstream; burn both by default"),
    OptionField("pcie_magic_gen4_dis", "NV_FUSE_OPT_PCIE_MAGIC_BITS_D[25]",
                2, 47 * 16 + 3, 1, "pcie", "", "Gen4 speed-disable flag"),
    OptionField("pcie_boot_gen23_dis", "NV_FUSE_OPT_PCIE_BOOT_GEN23_DISABLE",
                2, 49 * 16 + 6, 1, "pcie", ""),
    OptionField("pcie_boot_gen3_dis", "NV_FUSE_OPT_PCIE_BOOT_GEN3_DISABLE",
                2, 49 * 16 + 7, 1, "pcie", ""),
    # Whole MAGIC_BITS_D register (BAR0 0x820520). Chain-2 mapping confirmed
    # on three CMP 170HX cards: MAGIC = (block47<<22)|(block46<<6), matching
    # the upstream bit25=block47.3 anchor. opt_in: never a default target.
    # The A100 value below is an UNTESTED community guess (see --a100).
    OptionField("magic_d", "NV_FUSE_OPT_PCIE_MAGIC_BITS_D",
                2, 730, 32, "identity", "",
                "whole MAGIC_BITS_D word; --a100 / --set magic_d=HEX",
                opt_in=True),
]

A100_MAGIC_D = 0x00200000

BY_KEY = {f.key: f for f in FIELDS}


def blocks_touched(field):
    return sorted({(field.bit + i) // 16 for i in range(field.width)})


def get_bits(blocks_by_chain, chain, bit, width):
    value = 0
    chain_blocks = blocks_by_chain.get(chain, {})
    for i in range(width):
        b = bit + i
        blk = chain_blocks.get(b // 16, 0)
        value |= ((blk >> (b % 16)) & 1) << i
    return value


def set_bits(chain_blocks, bit, width, value):
    for i in range(width):
        b = bit + i
        blk = chain_blocks.get(b // 16, 0)
        blk = (blk & ~(1 << (b % 16))) | (((value >> i) & 1) << (b % 16))
        chain_blocks[b // 16] = blk


def decode_field(blocks_by_chain, field):
    return get_bits(blocks_by_chain, field.chain, field.bit, field.width)


def popcount(v):
    return bin(v).count("1")
