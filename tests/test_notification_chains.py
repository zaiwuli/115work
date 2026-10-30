from types import SimpleNamespace

from sqlalchemy import select

from app.persistence.models import Event, Offline, ResourceLink, Settings, Task
from app.services.events import record
from app.services.notification_chains import collect, outcome
from app.services.notifications import enabled
from tests.conftest import ed2k


def make_task(session, identifier='child', **kwargs):
    task = Task(id=identifier, object_key=identifier, content_key=identifier, source='local',
                name=identifier + '.zip', size=1, state='SUCCEEDED', stage='complete',
                policy={}, output={})
    for key, value in kwargs.items():
        setattr(task, key, value)
    session.add(task)
    session.flush()
    return task


def test_postprocess_must_finish_before_result(runtime):
    state, _, _ = runtime
    settings = {'notification_mode': 'result', 'notification_mode_since': 0}
    with state.db.session() as session:
        task = make_task(session, policy={'original_action': 'delete'})
        record(session, task.id, 'SUCCEEDED', 'complete', 'done', 'local')
        collect(session, settings)
        assert not session.scalar(select(Event).where(Event.kind == 'CHAIN_SUCCEEDED'))
        task.output = {'postprocess_result': 'SUCCEEDED'}
        collect(session, settings)
        collect(session, settings)
        assert len(list(session.scalars(select(Event).where(Event.kind == 'CHAIN_SUCCEEDED')))) == 1
        assert outcome(task) == 'success'


async def test_seed_parent_waits_for_children_and_reports_partial_failure(runtime):
    state, _, _ = runtime
    await state.engine.offline.import_text(ed2k())
    with state.db.session() as session:
        parent = session.scalar(select(Offline))
        parent.status, parent.stage = 'success', 'complete'
        session.add(Settings(key='seed-policy:' + parent.id, value={'result': 'SUCCEEDED'}))
        a = make_task(session, 'one')
        b = make_task(session, 'two', state='RUNNING')
        session.add_all([ResourceLink(offline_id=parent.id, task_id=t.id, evidence='seed-in-place') for t in [a, b]])
        record(session, parent.id, 'SEED_CLEANUP_COMPLETED', 'complete', 'done', 'offline')
        collect(session, {'notification_mode': 'result'})
        assert not session.scalar(select(Event).where(Event.kind.like('CHAIN_%')))
        b.state, b.error = 'FAILED', 'bad archive'
        record(session, b.id, 'FAILED', 'local_extract', 'bad archive', 'local')
        collect(session, {'notification_mode': 'result'})
        events = list(session.scalars(select(Event).where(Event.kind.like('CHAIN_%'))))
        assert len(events) == 1
        assert events[0].task_id == parent.id
        assert events[0].kind == 'CHAIN_FAILED'
        assert 'two.zip' in events[0].message


def test_presets_suppress_intermediate_and_historical_events():
    settings = {'notification_mode': 'attention', 'notification_mode_since': 10}
    def event(kind, created=20):
        return SimpleNamespace(kind=kind, stage='cloud_prepare', created_at=created)
    assert not enabled(settings, event('WAITING_RETRY'), 'cloud')
    assert not enabled(settings, event('SUCCEEDED'), 'cloud')
    assert not enabled(settings, event('CHAIN_SUCCEEDED'), 'cloud')
    assert enabled(settings, event('CHAIN_FAILED'), 'cloud')
    assert enabled(settings, event('WAITING_APPROVAL'), 'cloud')
    assert not enabled(settings, event('CHAIN_FAILED', 1), 'cloud')
    assert enabled(settings | {'notification_mode': 'result'}, event('CHAIN_SUCCEEDED'), 'cloud')


def test_plan_validation_and_legacy_custom(runtime):
    state, client, _ = runtime
    assert client.post('/api/settings', json={'notification_mode': 'bad'}).status_code == 422
    assert client.post('/api/settings', json={'notification_mode_since': 0}).status_code == 422
    assert client.post('/api/settings', json={'notification_mode': 'custom', 'notification_events': {'seed.failed': True}}).status_code == 200
    settings = client.get('/api/v1/settings').json()
    assert settings['notification_mode'] == 'custom'
    assert settings['notification_events'] == {'seed.failed': True}


def test_muted_outcome_not_replayed_and_retry_can_notify(runtime):
    state, _, _ = runtime
    settings = {'notification_mode': 'result'}
    with state.db.session() as session:
        task = make_task(session)
        record(session, task.id, 'SUCCEEDED', 'complete', 'done', 'local')
        collect(session, settings | {'notification_enabled': False})
        collect(session, settings)
        events = list(session.scalars(select(Event).where(Event.kind == 'CHAIN_SUCCEEDED')))
        assert len(events) == 1
        task.policy = {'execution_event_cutoff': events[0].id}
        record(session, task.id, 'SUCCEEDED', 'complete', 'retry done', 'local')
        collect(session, settings)
        assert len(list(session.scalars(select(Event).where(Event.kind == 'CHAIN_SUCCEEDED')))) == 2


def test_switching_plan_does_not_replay_old_result(runtime):
    state, _, _ = runtime
    with state.db.session() as session:
        task = make_task(session)
        event = record(session, task.id, 'SUCCEEDED', 'complete', 'old result', 'local')
        collect(session, {'notification_mode': 'result', 'notification_mode_since': event.created_at + 1})
        assert not session.scalar(select(Event).where(Event.kind == 'CHAIN_SUCCEEDED'))
