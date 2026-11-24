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
    # Device id as it is fused into the chip: lspci, the driver and the
    # vBIOS-vs-fuse boot gate compare *this*, not the ROM's own id. Chain-2
    # layout follows the community A100 conversion report (block37 = devida<<1,
    # block38 = devidb<<1, each id's 16th bit in bit0 of the next block, 0 for
    # both the CMP and the A100 id), so burning 0x20c2 -> 0x20b5 rewrites
    # blocks 37/38 to 0x416a -- records 0x416A0942 and 0x416A0982. The encoding
    # checks out against our own FF snapshots (which replay 0x4184 for these
    # blocks); the conversion itself we have not yet walked on our own card.
    # opt_in: this is an identity change that only makes sense together with an
    # A100 vBIOS flash, so it never happens unless --a100 / --set asks for it.
    OptionField("pcie_devida", "NV_FUSE_OPT_PCIE_DEVIDA",
                2, 593, 16, "identity", "",
                "fused device id; --a100 rewrites it to 0x20b5 (A100)",
                opt_in=True),
    OptionField("pcie_devidb", "NV_FUSE_OPT_PCIE_DEVIDB",
                2, 609, 16, "identity", "",
                "fused device id (mirror); --a100 rewrites it to 0x20b5",
                opt_in=True),
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
    # the upstream bit25=block47.3 anchor. opt_in: never a default target;
    # --a100 asks for it (and --skip magic_d refuses) because a card that takes
    # an A100 id should not keep declaring CMP-era capability. This word is a
    # PCIe *capability declaration*: it states what the link may train, it is
    # not a measured speed, which is why clearing bit25 alone (the default plan
    # does) is enough to run Gen4 and why the whole-word value is a guess.
    OptionField("magic_d", "NV_FUSE_OPT_PCIE_MAGIC_BITS_D",
                2, 730, 32, "identity", "",
                "whole MAGIC_BITS_D word; --a100 rewrites it, --set pins it",
                opt_in=True),
]

# The two halves of the fused device id, and what the community A100
# conversion burns them to. Everything that compares ids (lspci, the driver,
# the GSP/DevInit vBIOS boot gate) reads this, so an A100 vBIOS is refused on
# a card still fused as 0x20c2 and accepted, unmodified, on one fused 0x20b5.
DEVID_KEYS = ("pcie_devida", "pcie_devidb")
CMP_DEVID = 0x20C2
A100_DEVID = 0x20B5

# Community guess for the whole MAGIC_BITS_D word a real A100 is said to
# carry: UNTESTED, and the community conversion report does not touch this word
# at all (the boot gate compares the fused device id). Kept for
# `--set magic_d=...` experiments, where generate.py still warns about it.
A100_MAGIC_D = 0x00200000

BY_KEY = {f.key: f for f in FIELDS}


def block_mask(field, blk):
    """Which bits of one 16-bit block belong to `field`.

    A field can straddle a block boundary (both device-id fields do), and two
    fields can share a block (the whole-word MAGIC_BITS_D and its bit25), so
    "would this record change anything this field owns?" is answered per block,
    not per field -- that is what makes a record's `fields` list say why that
    record has to be burnt.
    """
    mask = 0
    for i in range(field.width):
        b = field.bit + i
        if b // 16 == blk:
            mask |= 1 << (b % 16)
    return mask


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
