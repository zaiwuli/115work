from types import SimpleNamespace
from sqlalchemy import select, func
from app.persistence.models import Offline, Outbox
from app.services.events import record
from app.services.notifications import enabled, ms_payload
from tests.conftest import ed2k


def test_seed_notice_switches():
    e = SimpleNamespace(kind="SEED_CLEANUP_COMPLETED",stage="complete",message="仅预演",created_at=0)
    assert enabled({},e,"offline")
    assert not enabled({"notification_cleanup":False},e,"offline")
    assert "未删除" in ms_payload({},e,"offline","seed")["title"]
    e.kind = "SEED_CLEANUP_FAILED"
    assert enabled({"notification_cleanup":False},e,"offline")


async def test_retry_notice(runtime):
    state, _, _ = runtime
    await state.engine.offline.import_text(ed2k())
    with state.db.session() as s:
        t=s.scalar(select(Offline))
        identifier=t.id
        record(s,identifier,"SUCCESS","offline_download","success","offline")
        t.status="failed"
    state.engine.offline.retry(identifier)
    with state.db.session() as s:
        record(s,identifier,"SUCCESS","offline_download","success","offline")
        assert s.scalar(select(func.count()).select_from(Outbox)) == 2
