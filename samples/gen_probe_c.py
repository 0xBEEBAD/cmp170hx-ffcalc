#!/usr/bin/env python3
"""Generate a tiny read-only kernel module (ffprobe.ko) from the vendored
fuse register map. Build on the target, insmod once per GPU (bar0=<phys>),
values land in dmesg, module auto-removes itself. No writes to the device.

    python3 gen_probe_c.py > ffprobe.c
    make -C /lib/modules/$(uname -r)/build M=$PWD modules
    sudo insmod ffprobe.ko bar0=0xfa000000
    sudo dmesg | grep FFPROBE
"""
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(HERE, "..", "ffcalc", "data", "ga100-fuses.json")

TPL = """\
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/io.h>
#include <linux/types.h>

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("ffcalc read-only GA100 fuse-word probe");

static u64 bar0;
module_param(bar0, ullong, 0444);
MODULE_PARM_DESC(bar0, "physical BAR0 base address of the GPU");

struct probe_entry { u32 off; const char *name; };
static const struct probe_entry entries[] = {
@BODY@
};

static int __init ffprobe_init(void)
{
	void __iomem *h;
	size_t i;

	if (!bar0)
		return -EINVAL;
	h = ioremap(bar0, 0x00830000);
	if (!h)
		return -ENOMEM;
	for (i = 0; i < ARRAY_SIZE(entries); i++)
		pr_info("FFPROBE %s %08llx %08x\\n", entries[i].name,
			(u64)bar0 + entries[i].off,
			readl(h + entries[i].off));
	iounmap(h);
	return -EAGAIN;
}

static void __exit ffprobe_exit(void) { }

module_init(ffprobe_init);
module_exit(ffprobe_exit);
"""


def main():
    with io.open(MAP, encoding="utf-8") as f:
        m = json.load(f)
    regs = []
    for key, spec in sorted(m["bit_fields"].items()):
        regs.append((int(spec["reg"], 0), "bitfield." + key))
    for key, addr in sorted(m["defective"].items()):
        regs.append((int(addr, 0), "defective." + key))
    for key, addr in sorted(m["info"].items()):
        regs.append((int(addr, 0), "info." + key))
    entries = []
    for key, loc in sorted(m["fields"].items()):
        for kind in ("status", "fused"):
            if loc.get(kind):
                entries.append((int(loc[kind], 0), "%s.%s" % (key, kind)))
    entries += regs
    body = ['\t{ 0x%08x, "%s" },' % (off, name)
            for off, name in sorted(entries)]
    print(TPL.replace("@BODY@", "\n".join(body)))


if __name__ == "__main__":
    main()
