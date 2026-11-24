"""Saved card snapshots (cards/<uuid>.json): strict load, atomic save.

A snapshot is the only basis a burn plan is built from, so reading and
writing it is held to the same standard as reading a row back after
programming it:

* Values may be ints (the probe/``ingest`` channel) or ``"0x%08x"`` strings
  (the fuse-macro ``capture`` channel). Both were written by released tools,
  both may be hand-edited or produced by an external reader, so both are
  accepted -- but nothing is passed through unexamined. Passing values
  through unexamined is what made ``mergeff`` crash on its own output for a
  capture snapshot (``"0x%08x" % "0x00000000"``, found on a real card
  2026-11-24) and what lets an invalid row be written back into the file and
  fail later during ``gen``, far away from the cause.
* Anything that cannot be a physical fuse row -- key outside 0-511, value
  outside 32 bits, ``null``/``true``/float, the same row twice with two
  different values -- is refused as :class:`BadSnapshot`, naming the
  offending key, before a plan is emitted or a single byte is written.
* Saves serialize first, then write a sibling temp file, then re-read and
  re-parse it, and only then rename over the original. An interrupted write
  can never leave the one file a plan depends on half-written.

Bare numbers without a ``0x`` prefix are decimal (same convention as
ffrows.parse_rows); hex must be written ``0x...``.
"""
import io
import json
import os
import operator

from .optionmap import PHYS_ROW_MAX, PHYS_ROW_MIN

SKUS = ("8gb", "10gb")


class BadSnapshot(ValueError):
    """A saved snapshot (or a row file merged into it) cannot be trusted.

    Subclasses ValueError so callers that already guard row-file parsing
    keep working; the CLI turns it into ``FAIL: ...`` plus a non-zero exit
    code instead of a traceback.
    """


class BadPlan(BadSnapshot):
    """A burn plan file cannot be trusted: unreadable, or not a plan at all.

    A plan is a burn instruction, so it gets the same treatment as the
    snapshot it was derived from -- refused out loud, never a traceback.
    """


def _hex(v):
    return "0x%x" % v


def int_value(v, what="value"):
    """A 32-bit fuse value from any legal on-disk form -> int. Strict.

    Accepts ints and ``0x``-prefixed or plain decimal strings. Rejects
    bool/float/None (``true`` and ``1.5`` in a fuse snapshot mean somebody
    edited it wrongly, not a value to guess at) and out-of-range numbers.
    """
    if isinstance(v, bool) or v is None:
        raise BadSnapshot("%s: %r is not a fuse value (want an int or a "
                          "'0x...' string)" % (what, v))
    if isinstance(v, int):
        out = v
    elif isinstance(v, str):
        try:
            out = int(v.strip(), 0)
        except ValueError:
            raise BadSnapshot("%s: %r is not a number; write hex as '0x...' "
                              "(a bare number is decimal)" % (what, v))
    else:
        raise BadSnapshot("%s: %r is not a fuse value (want an int or a "
                          "'0x...' string)" % (what, v))
    if not 0 <= out <= 0xffffffff:
        raise BadSnapshot("%s: 0x%x is not a 32-bit fuse value" % (what, out))
    return out


def row_index(k, context="card dump"):
    """A physical row number from any legal on-disk form -> int, 0-511."""
    if isinstance(k, bool):
        raise BadSnapshot("%s: fuse row key %r is not a number"
                          % (context, k))
    if isinstance(k, str):
        try:
            out = int(k.strip(), 0)
        except ValueError:
            raise BadSnapshot("%s: fuse row key %r is not a number"
                              % (context, k))
    else:
        try:
            out = operator.index(k)
        except TypeError:
            raise BadSnapshot("%s: fuse row key %r is not a number"
                              % (context, k))
    if not PHYS_ROW_MIN <= out <= PHYS_ROW_MAX:
        raise BadSnapshot("%s: fuse row key %r is out of range %d-%d"
                          % (context, k, PHYS_ROW_MIN, PHYS_ROW_MAX))
    return out


def normalize_rows(raw, context="card dump"):
    """Any on-disk ``ff_rows`` shape -> {int row: int value}. Invalid data is
    refused rather than guessed at.

    The same physical row listed twice with different values (e.g. a
    hand-merged file carrying both ``"222"`` and ``"0xde"``) is refused
    rather than silently collapsed to whichever entry came last.
    """
    out = {}
    for k, v in (raw or {}).items():
        row = row_index(k, context)
        value = int_value(v, "%s: row %d value" % (context, row))
        if row in out and out[row] != value:
            raise BadSnapshot("%s: row %d listed twice with different values "
                              "(0x%08x vs 0x%08x)"
                              % (context, row, out[row], value))
        out[row] = value
    return out


def serialize_rows(rows, context="card dump"):
    """{row: value} -> on-disk form: decimal keys, fixed-width ``0x%08x``."""
    return dict(("%d" % row, "0x%08x" % value)
                for row, value in sorted(normalize_rows(rows, context).items()))


def normalize_words(raw, context="card dump"):
    """option_words/info maps -> {key: int}; invalid values are refused."""
    out = {}
    for k, v in (raw or {}).items():
        if not isinstance(k, str) or not k.strip():
            raise BadSnapshot("%s: option word key %r is not a name"
                              % (context, k))
        out[k] = int_value(v, "%s: option word %s" % (context, k))
    return out


def _check_sku(raw, context):
    sku = raw.get("sku", "8gb")
    if sku not in SKUS:
        raise BadSnapshot("%s: sku must be one of %s, got %r"
                          % (context, "/".join(SKUS), sku))
    return sku


def to_json_form(dump_dict, context="card dump"):
    """A dump held in memory -> the canonical JSON form written to disk.

    Rows go out as fixed-width ``0x%08x`` strings, words/info as ``0x%x``;
    every value is checked on the way. The result is JSON-canonical (all
    keys are strings), which is what lets atomic_write_json verify the file
    by re-parsing it and comparing against the object it just wrote.
    """
    if not isinstance(dump_dict, dict):
        raise BadSnapshot("%s: not an object" % context)
    out = dict(dump_dict)
    if "ff_rows" in dump_dict:
        out["ff_rows"] = serialize_rows(dump_dict["ff_rows"], context)
    for field in ("option_words", "info"):
        if field in dump_dict:
            out[field] = dict((k, _hex(v)) for k, v in
                              normalize_words(dump_dict[field], context).items())
    return out


def load_dump(path):
    """Read a saved snapshot into an all-int working dump. Refuses anything
    that cannot be a physical row or a 32-bit value."""
    raw = _load_raw(path)
    sku = _check_sku(raw, path)
    rows = normalize_rows(raw.get("ff_rows"), path)
    words = normalize_words(raw.get("option_words"), path)
    out = {"sku": sku, "ff_rows": rows, "option_words": words,
           "source": path}
    for extra in ("uuid", "fingerprint", "bdf", "info"):
        if extra in raw:
            out[extra] = raw[extra]
    return out


def _load_raw(path, hint="re-capture the card"):
    try:
        with io.open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise BadSnapshot("%s: cannot be read (%s)" % (path, e))
    except ValueError as e:
        # A truncated file here is what an interrupted non-atomic write
        # leaves behind; say so, it is the actionable part.
        raise BadSnapshot("%s: not valid JSON (%s); if this file was written "
                          "by an older ffcalc, an interrupted run may have "
                          "truncated it -- %s" % (path, e, hint))
    if not isinstance(raw, dict):
        raise BadSnapshot("%s: expected a JSON object, got %s"
                          % (path, type(raw).__name__))
    return raw


def load_plan(path):
    """Read a burn plan file, refusing anything that is not one. Lives next
    to load_dump because a plan is the other file a burn decision is made
    from, and it gets the same treatment."""
    raw = _load_raw(path, hint="regenerate it with ffcalc gen")
    if not isinstance(raw.get("burn"), list):
        raise BadPlan("%s: not a burn plan (no 'burn' list); regenerate it "
                      "with ffcalc gen" % path)
    return raw


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


def atomic_write_json(path, obj):
    """Write JSON so the file holds either the old content or the new one,
    never a half-written mix: serialize first (so a value that cannot be
    written never truncates the file), write a sibling temp file, fsync it,
    read it back and re-parse it, and only then rename it over the original.

    Any failure is a BadSnapshot that also says the existing file is
    unchanged: for a burn snapshot, "the write did not happen" and "the
    snapshot is unusable" are the same answer -- do not plan, do not burn.
    """
    try:
        text = json.dumps(obj, indent=2, sort_keys=True)
    except (TypeError, ValueError) as e:
        raise BadSnapshot("%s: cannot be written as JSON (%s); nothing was "
                          "written" % (path, e))
    tmp = path + ".tmp"          # same directory, so the rename is atomic
    try:
        with io.open(tmp, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        try:
            back = _load_raw(tmp)
        except BadSnapshot as e:
            raise BadSnapshot("%s: read-back check failed (%s)" % (path, e))
        if back != json.loads(text):
            raise BadSnapshot("%s: read-back check failed (the file does not "
                              "hold what was written to it)" % path)
        os.replace(tmp, path)
    except BadSnapshot:
        _discard(tmp)
        raise
    except OSError as e:
        _discard(tmp)
        raise BadSnapshot("%s: write failed (%s); the file that was already "
                          "there is unchanged" % (path, e))
    return path


def update_rows(path, rows):
    """Merge rows into a saved snapshot's ff_rows in place, atomically.

    Both sides are normalized and checked first, so a snapshot row that
    cannot exist (row 999, a 33-bit value, a null) is refused here instead
    of being written back and failing in ``gen`` later. Returns
    ``(merged_rows, conflicts)``; conflicts lists ``(row, old, new)`` for
    rows the incoming readout overwrites with a different value -- the
    caller shows them, because a row that changed under us means the card
    was burnt since the snapshot or the wrong file was merged.
    """
    raw = _load_raw(path)
    _check_sku(raw, path)
    merged = normalize_rows(raw.get("ff_rows"), path)
    incoming = normalize_rows(rows, "FF rows file")
    conflicts = sorted((row, merged[row], incoming[row])
                       for row in set(incoming) & set(merged)
                       if merged[row] != incoming[row])
    merged.update(incoming)
    raw["ff_rows"] = serialize_rows(merged, path)
    atomic_write_json(path, raw)
    return merged, conflicts
