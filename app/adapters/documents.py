import csv
import hashlib
import io
import json
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedDocument:
    digest: str
    text: str
    encoding: str


def parse_document(path: Path, max_bytes: int) -> ParsedDocument:
    if path.stat().st_size > max_bytes:
        raise ValueError("DOCUMENT_SIZE_LIMIT")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("DOCUMENT_SIZE_LIMIT")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(x.file_size for x in entries) > 32 * 1024**2:
                raise ValueError("DOCUMENT_EXPANSION_LIMIT")
            if any(x.flag_bits & 1 for x in entries):
                raise ValueError("DOCUMENT_ENCRYPTED")
            xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                raise ValueError("DOCUMENT_ENTITY_REJECTED")
            tree = ElementTree.fromstring(xml)
            content = "\n".join(x.text or "" for x in tree.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
            ))
        return ParsedDocument(hashlib.sha256(raw).hexdigest(), content, "docx-ooxml")
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError("DOCUMENT_ENCRYPTED")
        if len(reader.pages) > 200:
            raise ValueError("DOCUMENT_PAGE_LIMIT")
        texts = []
        for page in reader.pages:
            contents = page.get_contents()
            if contents and len(contents.get_data()) > 4 * 1024**2:
                raise ValueError("DOCUMENT_PAGE_STREAM_LIMIT")
            texts.append(page.extract_text() or "")
            if sum(map(len, texts)) > 8 * 1024**2:
                raise ValueError("DOCUMENT_TEXT_LIMIT")
        content = "\n".join(texts)
        if not content.strip():
            raise ValueError("DOCUMENT_REQUIRES_OCR")
        return ParsedDocument(hashlib.sha256(raw).hexdigest(), content, "pdf-text")
    if b"\x00" in raw:
        raise ValueError("BINARY_DOCUMENT_REJECTED")
    try:
        text = raw.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
        encoding = "gb18030"
    if suffix in {".txt", ".md"}:
        content = text
    elif suffix == ".csv":
        rows = csv.DictReader(io.StringIO(text))
        if not rows.fieldnames or not ({"url", "link"} & set(rows.fieldnames)):
            raise ValueError("CSV_REQUIRES_URL_OR_LINK_COLUMN")
        content = "\n".join(str(row.get("url") or row.get("link") or "") for row in rows)
    elif suffix == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON_REQUIRES_LINK_ARRAY")
        content = "\n".join(
            x if isinstance(x, str) else str(x.get("url") or x.get("link") or "")
            for x in data if isinstance(x, (str, dict))
        )
    else:
        raise ValueError("DOCUMENT_FORMAT_NOT_ENABLED")
    return ParsedDocument(hashlib.sha256(raw).hexdigest(), content, encoding)
