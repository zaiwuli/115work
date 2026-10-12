import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.adapters.documents import parse_document
from app.runtime.process_limits import limit_worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--max-bytes", type=int, required=True)
    args = parser.parse_args()
    try:
        limit_worker(512 * 1024**2, 30)
        value = parse_document(Path(args.path), args.max_bytes)
        print(json.dumps({"ok": True, **asdict(value)}))
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc).isupper() else "DOCUMENT_PARSE_FAILED"
        print(json.dumps({"ok": False, "error": code[:100]}))


if __name__ == "__main__":
    main()
