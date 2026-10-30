from sqlalchemy import select

from app.adapters.p115 import AdapterError
from app.persistence.models import Event, Job, Offline, Settings
from app.scheduler.store import enqueue
from tests.conftest import ed2k
from tests.test_offline import execute


async def test_completed_offline_scan_exits_without_remote_read(runtime):
    state, _, adapter = runtime
    await state.engine.offline.import_text(ed2k())
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        item.target_cid, item.status, item.stage = '100', 'success', 'complete'
        enqueue(session, 'scan-cid:100', 'cloud_scan', {
            'cid': '100', 'offline_only': True, 'walk': [{'cid': 'deleted'}], 'candidates': []})
    async def fail(*args):
        raise AssertionError('obsolete scan must not access remote')
    adapter.files = fail
    await execute(state, 'cloud_scan')
    with state.db.session() as session:
        job = session.scalar(select(Job).where(Job.kind == 'cloud_scan'))
        assert job.state == 'SUCCEEDED'
        assert job.payload['walk'] == []
        assert session.scalar(select(Event).where(Event.kind == 'SCAN_OBSOLETE'))


async def test_seed_scan_exits_and_is_not_rescheduled(runtime):
    state, _, adapter = runtime
    await state.engine.offline.import_text(ed2k())
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        item.target_cid, item.status = '100', 'downloading'
        session.add(Settings(key='seed-policy:' + item.id, value={'result': 'WAITING_DOWNLOAD'}))
        session.flush()
        state.engine.offline.schedule_discovery(session, item.created_at + 500)
        assert not session.scalar(select(Job).where(Job.kind == 'cloud_scan'))
        enqueue(session, 'scan-cid:100', 'cloud_scan', {'cid': '100', 'offline_only': True})
    async def fail(*args):
        raise AssertionError('seed lifecycle owns discovery')
    adapter.files = fail
    await execute(state, 'cloud_scan')
    with state.db.session() as session:
        assert session.scalar(select(Job).where(Job.kind == 'cloud_scan')).state == 'SUCCEEDED'


async def test_invalid_subdirectory_stops_after_three_attempts_and_can_restart(runtime):
    state, _, adapter = runtime
    with state.db.session() as session:
        enqueue(session, 'scan-cid:100', 'cloud_scan', {
            'cid': '100', 'offline_only': False, 'walk': [{'cid': 'deleted'}]})
    async def fail(cid, offset=0):
        raise AdapterError('CID_NOT_VERIFIED', step=cid)
    adapter.files = fail
    for expected in ['READY', 'READY', 'FAILED']:
        await execute(state, 'cloud_scan')
        with state.db.session() as session:
            job = session.scalar(select(Job).where(Job.kind == 'cloud_scan'))
            assert job.state == expected
            assert job.payload['walk'] == [{'cid': 'deleted'}]
    with state.db.session() as session:
        event = session.scalar(select(Event).where(Event.kind == 'JOB_FAILED'))
        assert '100' in event.message and 'deleted' in event.message
        job = enqueue(session, 'scan-cid:100', 'cloud_scan', {'cid': '100', 'offline_only': False})
        assert job.state == 'READY'
        assert job.payload['scan_validation_failures'] == 0
        assert job.payload['walk'] == []
