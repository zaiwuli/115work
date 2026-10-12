import fnmatch
from pathlib import PurePath

from sqlalchemy import select

from app.persistence.models import Rule


DOCUMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".json",
                     ".jpg", ".jpeg", ".png", ".webp"}


def evaluate(session, source_id: str, name: str, size: int) -> dict:
    selected = []
    for rule in session.scalars(select(Rule).where(Rule.enabled)):
        config = rule.config
        if rule.source_id and rule.source_id != source_id:
            continue
        if not fnmatch.fnmatchcase(name.casefold(), config.get("pattern", "*").casefold()):
            continue
        if any(fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
               for pattern in config.get("exclude", [])):
            continue
        if config.get("extensions") and PurePath(name).suffix.lower() not in config["extensions"]:
            continue
        if size < config.get("min_size", 0) or size > config.get("max_size", 2**63 - 1):
            continue
        selected.append(rule)
    if len(selected) > 1:
        raise ValueError("AMBIGUOUS_RULE")
    if not selected:
        return {"retain_original": True, "document_action": "register"}
    rule = selected[0]
    return {"retain_original": True, **rule.config, "rule_id": rule.id, "rule_revision": rule.revision}
