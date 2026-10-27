import hashlib
from urllib.parse import urlencode

from bencode2 import bdecode

from app.domain.links import Link


MAX_TORRENT_BYTES = 2 * 1024 * 1024


def _decode_torrent(raw: bytes):
    info_raw = None

    def decode(pos, depth=0):
        nonlocal info_raw
        if depth > 128:
            raise ValueError()
        token = raw[pos:pos + 1]
        if token == b"i":
            end = raw.index(b"e", pos) + 1
            return bdecode(raw[pos:end]), end
        if token in {b"d", b"l"}:
            dictionary = token == b"d"
            result = {} if dictionary else []
            pos += 1
            while raw[pos:pos + 1] != b"e":
                if dictionary:
                    key, pos = decode(pos, depth + 1)
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError()
                    start = pos
                    value, pos = decode(pos, depth + 1)
                    result[key] = value
                    if depth == 0 and key == b"info":
                        info_raw = raw[start:pos]
                else:
                    value, pos = decode(pos, depth + 1)
                    result.append(value)
            return result, pos + 1
        colon = raw.index(b":", pos)
        length = raw[pos:colon]
        if not length.isdigit():
            raise ValueError()
        end = colon + 1 + int(length)
        if end > len(raw):
            raise ValueError()
        return bdecode(raw[pos:end]), end

    metadata, end = decode(0)
    if end != len(raw) or info_raw is None:
        raise ValueError()
    return metadata, info_raw


def parse_torrent(raw: bytes) -> Link:
    if not raw or len(raw) > MAX_TORRENT_BYTES:
        raise ValueError("TORRENT_SIZE_LIMIT")
    metadata = None
    try:
        metadata, info_raw = _decode_torrent(raw)
        if not isinstance(metadata, dict):
            raise ValueError()
        info = metadata[b"info"]
        if not isinstance(info, dict) or not info:
            raise ValueError()
        if b"meta version" in info and b"pieces" not in info:
            raise ValueError("TORRENT_V2_UNSUPPORTED")
        name = info.get(b"name.utf-8", info.get(b"name", b"Unknown_Torrent"))
        if not isinstance(name, bytes) or not name:
            raise ValueError()
        name = name.decode("utf-8", errors="replace")
        if any(ord(char) < 32 for char in name) or len(name) > 1024:
            raise ValueError()
        # BT identity uses the original info bytes, including their key order.
        digest = hashlib.sha1(info_raw).hexdigest()
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        if isinstance(metadata, dict):
            info = metadata.get(b"info")
            if isinstance(info, dict) and b"meta version" in info and b"pieces" not in info:
                raise ValueError("TORRENT_V2_UNSUPPORTED") from None
        raise ValueError("INVALID_TORRENT") from None
    link = "magnet:?xt=urn:btih:" + digest + "&" + urlencode({"dn": name})
    return Link(link, "btih:" + digest, "magnet", name, 0, digest)
