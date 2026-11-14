#!/bin/bash
# One-shot: collect fuse words from every CMP 170HX (read-only kernel
# probe), print the potential table, save cards/<uuid>.json and
# plans/<uuid>.burn.json. Run from the repo root on the card host.
#   sudo ./scripts/ffscan.sh [collect-dir]
set -e
cd "$(dirname "$0")/.."
DIR=${1:-collected}
KDIR=$DIR/kprobe
K=/lib/modules/$(uname -r)/build
PY=python3

mkdir -p "$DIR" "$KDIR"
if [ ! -f "$KDIR/ffprobe.c" ] || [ ffcalc/data/ga100-fuses.json -nt "$KDIR/ffprobe.c" ]; then
	$PY samples/gen_probe_c.py > "$KDIR/ffprobe.c"
	printf 'obj-m += ffprobe.o\n' > "$KDIR/Makefile"
fi
N=$(grep -c '{ 0x' "$KDIR/ffprobe.c")
sudo make -C "$K" M="$PWD/$KDIR" modules >/dev/null
nvidia-smi --query-gpu=pci.bus_id,uuid --format=csv,noheader \
	| tr 'A-F' 'a-f' | tr -d ' ' | sed 's/^00000000:/0000:/' \
	> "$DIR/uuid.csv"
: > "$DIR/bar0map.tsv"
for dev in /sys/bus/pci/devices/*; do
	[ "$(cat "$dev/device" 2>/dev/null)" = "0x20c2" ] || continue
	bdf=$(basename "$dev")
	bar0=$(head -1 "$dev/resource" | cut -d' ' -f1)
	uuid=$(awk -F, -v b="$bdf" 'tolower($1)==b {print $2; exit}' "$DIR/uuid.csv")
	sudo insmod "$KDIR/ffprobe.ko" bar0="$bar0" || true
	sudo dmesg | grep FFPROBE | tail -n "$N" > "$DIR/probe_${bdf//[:.]/_}.txt"
	printf '%s\t%s\t%s\n' "$bdf" "$bar0" "$uuid" >> "$DIR/bar0map.tsv"
done
$PY -m ffcalc scan --dir "$DIR" --outdir cards --plans plans
