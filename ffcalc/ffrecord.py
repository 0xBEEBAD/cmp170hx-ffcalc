def encode(chain, block, data):
    if not 0 <= chain <= 31:
        raise ValueError("chain out of range: %r" % chain)
    if not 0 <= block <= 1023:
        raise ValueError("block out of range: %r" % block)
    if not 0 <= data <= 0xffff:
        raise ValueError("data out of range: 0x%x" % data)
    return (data & 0xffff) << 16 | (block & 0x3ff) << 6 | (chain & 0x1f)


def decode(record):
    if (record >> 5) & 1:
        raise ValueError("not an FF record (IFF bit set): 0x%08x" % record)
    return {
        "chain": record & 0x1f,
        "block": (record >> 6) & 0x3ff,
        "data": (record >> 16) & 0xffff,
    }


def replay(rows):
    blocks = {}
    for row in sorted(rows):
        rec = rows[row]
        if rec == 0:
            continue
        d = decode(rec)
        blocks.setdefault(d["chain"], {})[d["block"]] = d["data"]
    return blocks
