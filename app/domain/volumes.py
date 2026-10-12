import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath


@dataclass(frozen=True)
class VolumeGroup:
    key: str
    name: str
    main: Path
    parts: tuple[Path, ...]
    complete: bool

    @property
    def size(self):
        return sum(path.stat().st_size for path in self.parts)


def groups(paths: list[Path], companions: dict | None = None) -> list[VolumeGroup]:
    buckets = {}
    singles = []
    for path in paths:
        name = path.name
        numbered = re.match(r"(?i)^(.+)\.part(\d+)\.rar$", name)
        split = re.match(r"(?i)^(.+\.(?:7z|zip))\.(\d{3})$", name)
        old = re.match(r"(?i)^(.+)\.r(\d{2,3})$", name)
        if numbered or split or old:
            match = numbered or split or old
            family = "part" if numbered else "split" if split else "old"
            base = match[1] + (".rar" if family in {"part", "old"} else "")
            index = int(match[2]) + (1 if family == "old" else 0)
            key = (str(path.parent), base.casefold(), family)
            buckets.setdefault(key, []).append((index, path))
        else:
            singles.append(path)
    result = []
    for (parent, base, family), parts in buckets.items():
        if family == "old":
            main = next((p for p in singles if str(p.parent) == parent and
                         p.name.casefold() == base), None)
            if main:
                singles.remove(main)
                parts.append((0, main))
        ordered = sorted(parts)
        start = 0 if family == "old" and ordered[0][0] == 0 else 1
        complete = [index for index, _ in ordered] == list(range(start, ordered[-1][0] + 1))
        # Only an extractor's CRC/end-of-archive test can confirm the last volume.
        main = ordered[0][1]
        result.append(VolumeGroup("", base, main, tuple(p for _, p in ordered), complete))
    for path in singles:
        if path.suffix.lower() == ".cue":
            parts = (companions or {}).get(path, ())
            result.append(VolumeGroup("", path.name, path, parts or (path,), bool(parts)))
        else:
            result.append(VolumeGroup("", path.name, path, (path,), True))
    return [
        VolumeGroup(hashlib.sha256("|".join(
            f"{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in item.parts
        ).encode()).hexdigest(), item.name, item.main, item.parts, item.complete)
        for item in result
    ]


def remote_groups(files):
    """Group immutable remote DTOs without consulting the host filesystem."""
    buckets, singles = {}, []
    for file in files:
        match = re.match(r"(?i)^(.+)\.part(\d+)\.rar$", file.name)
        family = "part"
        if not match:
            match = re.match(r"(?i)^(.+\.(?:7z|zip))\.(\d{3})$", file.name)
            family = "split"
        if not match:
            match = re.match(r"(?i)^(.+)\.r(\d{2,3})$", file.name)
            family = "old"
        if not match:
            singles.append(file)
            continue
        base = match[1] + (".rar" if family in {"part", "old"} else "")
        key = (str(PurePosixPath(file.path).parent), base.casefold(), family)
        index = int(match[2]) + (1 if family == "old" else 0)
        buckets.setdefault(key, []).append((index, file))
    result = []
    for (parent, base, family), items in buckets.items():
        if family == "old":
            main = next((f for f in singles if str(PurePosixPath(f.path).parent) == parent
                         and f.name.casefold() == base), None)
            if main:
                singles.remove(main)
                items.append((0, main))
        items.sort(key=lambda x: x[0])
        start = 0 if family == "old" and items[0][0] == 0 else 1
        complete = [i for i, _ in items] == list(range(start, items[-1][0] + 1))
        result.append((base, [f for _, f in items], complete))
    result.extend((f.name, [f], True) for f in singles)
    return result
