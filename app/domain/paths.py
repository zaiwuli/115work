from pathlib import Path, PurePosixPath


def contained(root: Path, candidate: Path) -> Path:
    root, candidate = root.resolve(), candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("PATH_OUTSIDE_AUTHORIZED_ROOT")
    return candidate


def safe_relative(value: str) -> PurePosixPath:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not value or "\x00" in value or path.is_absolute()
        or ".." in path.parts or any(":" in p for p in path.parts)
    ):
        raise ValueError("UNSAFE_RELATIVE_PATH")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)
    }
    if any(part.rstrip(". ").split(".")[0].upper() in reserved or
           part.endswith((".", " ")) for part in path.parts):
        raise ValueError("UNSAFE_PLATFORM_FILENAME")
    return path


def resolve_mapping(virtual_path: str, mappings: list[dict], connection: str = "") -> Path:
    path = PurePosixPath(virtual_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("INVALID_VIRTUAL_PATH")
    matches = []
    for mapping in mappings:
        if connection and mapping.get("connection", "") != connection:
            continue
        root = PurePosixPath(mapping["virtual_root"])
        if path.is_relative_to(root) and mapping.get("enabled", True):
            matches.append((len(root.parts), mapping, path.relative_to(root)))
    if not matches:
        raise ValueError("NO_MAPPING")
    matches.sort(key=lambda x: x[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        raise ValueError("AMBIGUOUS_MAPPING")
    _, mapping, relative = matches[0]
    root = Path(mapping["container_root"])
    if not root.is_dir():
        raise ValueError("UNMOUNTED")
    return contained(root, root.joinpath(*relative.parts))
