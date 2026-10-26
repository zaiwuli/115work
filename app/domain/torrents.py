import hashlib
from urllib.parse import urlencode

from bencode2 import bdecode, bencode

from app.domain.links import Link


MAX_TORRENT_BYTES = 2 * 1024 * 1024


def parse_torrent(raw: bytes) -> Link:
    if not raw or len(raw) > MAX_TORRENT_BYTES:
        raise ValueError("TORRENT_SIZE_LIMIT")
    metadata = None
    try:
        metadata = bdecode(raw)
        if not isinstance(metadata, dict) or bencode(metadata) != raw:
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
        digest = hashlib.sha1(bencode(info)).hexdigest()
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        if isinstance(metadata, dict):
            info = metadata.get(b"info")
            if isinstance(info, dict) and b"meta version" in info and b"pieces" not in info:
                raise ValueError("TORRENT_V2_UNSUPPORTED") from None
        raise ValueError("INVALID_TORRENT") from None
    link = "magnet:?" + urlencode({"xt": "urn:btih:" + digest, "dn": name})
    return Link(link, "btih:" + digest, "magnet", name, 0, digest)
