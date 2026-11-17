#!/bin/bash
# One-shot: collect fuse words (read-only MMIO) plus all 512 physical
# fuse rows (fuse-macro CMD_READ pass, command-register writes only)
# from every CMP 170HX, print the potential table, save cards/<uuid>.json
# and plans/<uuid>.burn.json. Pass FFROWS=0 for a words-only probe on a
# busy card; plans then need `ffcalc mergeff cards/<uuid>.json rows.txt`
# and a rerun. Exit code is nonzero while any card lacks FF rows.
# Run from the repo root on the card host.
#   sudo ./scripts/ffscan.sh [--a100] [collect-dir]
# --a100 (or A100=1) makes plans also rewrite MAGIC_BITS_D to 0x200000
# (EXPERIMENTAL, community-sourced, untested on a real A100).
set -e
cd "$(dirname "$0")/.."
A100=${A100:-0}
DIR=""
for a in "$@"; do
	case "$a" in
	--a100) A100=1 ;;
	-h|--help) sed -n '2,11p' "$0"; exit 0 ;;
	-*) echo "unknown option: $a" >&2; exit 2 ;;
	*) DIR=$a ;;
	esac
done
DIR=${DIR:-collected}
KDIR=$DIR/kprobe
K=/lib/modules/$(uname -r)/build
PY=python3

mkdir -p "$DIR" "$KDIR"
if [ ! -f "$KDIR/ffprobe.c" ] ||
   [ ffcalc/data/ga100-fuses.json -nt "$KDIR/ffprobe.c" ] ||
   [ samples/gen_probe_c.py -nt "$KDIR/ffprobe.c" ] ||
   [ ffcalc/ffmacro.py -nt "$KDIR/ffprobe.c" ]; then
	$PY samples/gen_probe_c.py > "$KDIR/ffprobe.c"
	printf 'obj-m += ffprobe.o\n' > "$KDIR/Makefile"
fi
N=$(grep -c '{ 0x' "$KDIR/ffprobe.c")
FFROWS=${FFROWS:-1}
if [ "$FFROWS" = "1" ]; then N=$((N + 512 + 1)); fi
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
	sudo insmod "$KDIR/ffprobe.ko" bar0="$bar0" ffrows="$FFROWS" || true
	sudo dmesg | grep FFPROBE | tail -n "$N" > "$DIR/probe_${bdf//[:.]/_}.txt"
	printf '%s\t%s\t%s\n' "$bdf" "$bar0" "$uuid" >> "$DIR/bar0map.tsv"
done
SCAN_FLAGS=""
if [ "$A100" = "1" ]; then SCAN_FLAGS="--a100"; fi
$PY -m ffcalc scan --dir "$DIR" --outdir cards --plans plans $SCAN_FLAGS
