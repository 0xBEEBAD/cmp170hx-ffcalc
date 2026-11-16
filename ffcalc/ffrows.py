"""FF-row readout state: coverage checks and external row-dump import.

A burn plan is only safe when the *complete* current FF region is known.
Unburnt FF rows read back as 0 through the fuse macro (CMD_READ), so a
full readout lists every row in FF_ROW_MIN..FF_ROW_MAX, zeros included.

If ff_rows is missing or only partially covered we cannot tell "empty"
from "never read": already-burnt rows would look free, `old` values would
be wrong, and appended records could land on occupied rows. In that state
the tool refuses to produce or verify a burn plan instead of assuming
every existing record is zero (burning over a burnt row is irreversible).
"""
import io
import re

from .optionmap import FF_ROW_MAX, FF_ROW_MIN

N_FF_ROWS = FF_ROW_MAX - FF_ROW_MIN + 1

GUIDANCE = (
    "read all 512 physical fuse rows while the card is idle (fuse macro "
    "CMD_READ, e.g. the GA100VFI method from kate's blog), save them as "
    "a rows file (one '<row> <value>' pair per line, decimal or 0x hex, "
    "unburnt rows listed as 0, '#' comments ok), then merge it into the "
    "card dump: python -m ffcalc mergeff cards/<uuid>.json ffrows.txt "
    "-- and regenerate the plan (ffcalc gen / ffcalc scan).")

LINE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:FFROW\s+)?"
                  r"(0[xX][0-9a-fA-F]+|\d+)\s+(0[xX][0-9a-fA-F]+|\d+)\s*$")


class FFRowsNotRead(ValueError):
    """Current FF records were never read, or the read was incomplete."""

    def __init__(self, state, missing, context):
        self.state = state
        self.missing = missing
        if state == "missing":
            self.detail = ("ff_rows is empty: current FF records of this "
                           "card were never read")
        else:
            self.detail = ("ff_rows covers only %d of %d FF rows (%d-%d); "
                           "missing e.g. %s: FF readout incomplete or failed"
                           % (N_FF_ROWS - len(missing), N_FF_ROWS,
                              FF_ROW_MIN, FF_ROW_MAX, fmt_rows(missing)))
        super().__init__("cannot %s: %s\nhow to fix: %s"
                         % (context, self.detail, GUIDANCE))


def fmt_rows(rows, limit=8):
    parts = []
    for r in sorted(rows)[:limit]:
        parts.append(str(r))
    if len(rows) > limit:
        parts.append("...(+%d)" % (len(rows) - limit))
    return ",".join(parts)


def coverage(dump):
    """(state, missing_rows); state: 'complete' | 'partial' | 'missing'.

    Row keys may be ints or str/int-parseable strings (JSON dumps).
    """
    rows = dump.get("ff_rows") or {}
    have = set(int(k, 0) if isinstance(k, str) else k for k in rows)
    if not any(FF_ROW_MIN <= r <= FF_ROW_MAX for r in have):
        return "missing", list(range(FF_ROW_MIN, FF_ROW_MAX + 1))
    missing = [r for r in range(FF_ROW_MIN, FF_ROW_MAX + 1) if r not in have]
    return ("partial" if missing else "complete"), missing


def require(dump, context="generate a burn plan"):
    """Raise FFRowsNotRead unless the whole FF region is explicitly known."""
    state, missing = coverage(dump)
    if state != "complete":
        raise FFRowsNotRead(state, missing, context)


def parse_rows(text):
    """Rows-file text -> {row: value}.

    Accepted lines (case-insensitive hex, '#' starts a comment):
        222 0x0fe805c1
        0xde 0xfe805c1        (row in hex)
        FFROW 222 266331329   (plain decimal)
        [ 1569.145419] FFROW 222 0x0fe805c1   (dmesg prefix tolerated)
    Every unburnt row must be listed as 0; rows outside the FF region are
    kept too (0-511) so the file is a faithful physical snapshot.
    """
    out = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0]
        if not line.strip():
            continue
        m = LINE.match(line)
        if not m:
            raise ValueError("ffrows line %d: expected '<row> <value>', "
                             "got %r" % (lineno, raw.strip()))
        row, value = int(m.group(1), 0), int(m.group(2), 0)
        if not 0 <= row <= 511:
            raise ValueError("ffrows line %d: physical row %d out of "
                             "range 0-511" % (lineno, row))
        if not 0 <= value <= 0xffffffff:
            raise ValueError("ffrows line %d: value 0x%x out of 32-bit "
                             "range" % (lineno, value))
        if row in out and out[row] != value:
            raise ValueError("ffrows line %d: row %d given twice with "
                             "different values (0x%08x vs 0x%08x)"
                             % (lineno, row, out[row], value))
        out[row] = value
    return out


def parse_file(path):
    with io.open(path, encoding="utf-8") as f:
        return parse_rows(f.read())


def merge_into(dump, rows):
    """Merge parsed rows into dump['ff_rows']; return (state, missing)."""
    merged = dump.setdefault("ff_rows", {})
    merged.update(rows)
    return coverage(dump)
