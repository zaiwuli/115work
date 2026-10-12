import base64
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, unquote_plus, urlsplit, urlunsplit


LINK_PATTERN = re.compile(
    r"ed2k://\|file\|.*?\|/|magnet:\?[^\s<>\"`]+|(?:https?|ftp)://[^\s<>\"`]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Link:
    original: str
    identity: str
    kind: str
    name: str
    size: int = 0
    hash_value: str = ""


def match_name(name: str) -> str:
    return unicodedata.normalize("NFC", unquote_plus(name)).strip().casefold()


def parse_link(value: str) -> Link:
    value = value.strip()
    if not value or len(value) > 16384 or any(ord(c) < 32 for c in value):
        raise ValueError("INVALID_LINK")
    if value.lower().startswith("ed2k://"):
        parts = value.split("|")
        if len(parts) < 6 or parts[1].lower() != "file" or not value.endswith("|/"):
            raise ValueError("INVALID_ED2K")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", parts[4]) or not parts[3].isdigit():
            raise ValueError("INVALID_ED2K_HASH_OR_SIZE")
        size, digest = int(parts[3]), parts[4].lower()
        name = unquote_plus(parts[2])
        if not name or size <= 0 or size > 2**63 - 1:
            raise ValueError("INVALID_ED2K_SIZE_OR_NAME")
        return Link(value, f"ed2k:{digest}:{size}", "ed2k", name, size, digest)
    parsed = urlsplit(value)
    if parsed.scheme.lower() == "magnet":
        query = parse_qs(parsed.query)
        candidates = [x[9:] for x in query.get("xt", []) if x.lower().startswith("urn:btih:")]
        if len(candidates) != 1:
            raise ValueError("MAGNET_REQUIRES_ONE_BTIH")
        digest = candidates[0]
        if re.fullmatch(r"[a-fA-F0-9]{40}", digest):
            digest = digest.lower()
        elif re.fullmatch(r"[A-Za-z2-7]{32}", digest):
            digest = base64.b32decode(digest.upper()).hex()
        else:
            raise ValueError("INVALID_BTIH")
        return Link(value, "btih:" + digest, "magnet", query.get("dn", ["Magnet"])[0], 0, digest)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "ftp"} or not parsed.hostname or parsed.username:
        raise ValueError("UNSUPPORTED_LINK")
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    if port and port != {"http": 80, "https": 443, "ftp": 21}[scheme]:
        host += ":" + str(port)
    # Signed queries and path case are identity-bearing; do not lowercase them.
    normalized = urlunsplit((scheme, host, parsed.path or "/", parsed.query, ""))
    identity = "url:" + hashlib.sha256(normalized.encode()).hexdigest()
    return Link(value, identity, scheme, unquote(parsed.path.rsplit("/", 1)[-1]) or host)


def extract_links(text: str, limit: int = 1000) -> tuple[list[Link], list[str]]:
    found = LINK_PATTERN.findall(text)
    if len(found) > limit:
        raise ValueError("IMPORT_LIMIT_1000")
    links, errors = [], []
    for item in found:
        try:
            links.append(parse_link(item))
        except ValueError as exc:
            errors.append(str(exc))
    return links, errors
