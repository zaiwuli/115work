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
        if not isinstance(info, dict):
            raise ValueError()
        if b"pieces" not in info:
            raise ValueError("TORRENT_V2_UNSUPPORTED")
        pieces = info[b"pieces"]
        if not isinstance(pieces, bytes) or len(pieces) % 20 or not pieces:
            raise ValueError()
        if type(info[b"piece length"]) is not int or info[b"piece length"] <= 0:
            raise ValueError()
        name = info.get(b"name.utf-8", info.get(b"name"))
        if not isinstance(name, bytes) or not name:
            raise ValueError()
        name = name.decode("utf-8", errors="replace")
        if any(ord(char) < 32 for char in name) or len(name) > 1024:
            raise ValueError()
        files = info.get(b"files")
        if files is None:
            sizes = [info[b"length"]]
        else:
            if not isinstance(files, list) or not files or len(files) > 100000:
                raise ValueError()
            sizes = [file[b"length"] for file in files]
            for file in files:
                path = file[b"path"]
                if not isinstance(path, list) or not path or any(
                    not isinstance(part, bytes) or not part or part in {b".", b".."}
                    or b"/" in part or b"\\" in part or b"\0" in part for part in path
                ):
                    raise ValueError()
        if any(type(size) is not int or size < 0 for size in sizes):
            raise ValueError()
        size = sum(sizes)
        if not 0 < size <= 2**63 - 1:
            raise ValueError()
        if len(pieces) // 20 != (size + info[b"piece length"] - 1) // info[b"piece length"]:
            raise ValueError()
        digest = hashlib.sha1(bencode(info)).hexdigest()
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        if isinstance(metadata, dict):
            info = metadata.get(b"info")
            if isinstance(info, dict) and b"meta version" in info and b"pieces" not in info:
                raise ValueError("TORRENT_V2_UNSUPPORTED") from None
        raise ValueError("INVALID_TORRENT") from None
    link = "magnet:?" + urlencode({"xt": "urn:btih:" + digest, "dn": name})
    return Link(link, "btih:" + digest, "torrent", name, size, digest)
