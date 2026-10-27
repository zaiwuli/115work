from types import SimpleNamespace

import pytest
from sqlalchemy import select, func

from app.persistence.models import Outbox, Settings
from app.services.events import record
from app.services.notification_options import CATALOG, effective_options, event_option
from app.services.notifications import enabled, ms_payload


@pytest.mark.parametrize('kind,stage,category,option', [
    ('SUBMITTED', 'offline_download', 'offline', 'offline.submitted'),
    ('DOWNLOADING', 'offline_download', 'offline', 'offline.started'),
    ('SUCCESS', 'complete', 'offline', 'offline.success'),
    ('FAILED', 'offline_download', 'offline', 'offline.failed'),
    ('UNKNOWN', 'offline_download', 'offline', 'offline.unknown'),
    ('SEED_CLEANUP_STARTED', 'seed_cleanup', 'offline', 'seed.started'),
    ('SEED_CLEANUP_COMPLETED', 'complete', 'offline', 'seed.success'),
    ('SEED_CLEANUP_FAILED', 'seed_cleanup', 'offline', 'seed.failed'),
    ('DISCOVERED', 'cloud_prepare', 'cloud', 'cloud.discovery'),
    ('RUNNING', 'cloud_progress', 'cloud', 'cloud.started'),
    ('SUCCEEDED', 'complete', 'cloud', 'cloud.success'),
    ('FAILED', 'cloud_output', 'cloud', 'cloud.failed'),
    ('WAITING_APPROVAL', 'cloud_password', 'cloud', 'cloud.password'),
    ('RECONCILING', 'cloud_output', 'cloud', 'cloud.unknown'),
    ('RUNNING', 'cache', 'local', 'local.download_started'),
    ('DOWNLOAD_COMPLETED', 'cache', 'local', 'local.download_success'),
    ('FAILED', 'cache', 'local', 'local.download_failed'),
    ('RUNNING', 'local_extract', 'local', 'local.started'),
    ('SUCCEEDED', 'complete', 'local', 'local.success'),
    ('FAILED', 'local_extract', 'local', 'local.failed'),
    ('POSTPROCESS_SUCCEEDED', 'postprocess', 'cloud', 'post.success'),
    ('POSTPROCESS_FAILED', 'postprocess', 'local', 'post.failed'),
    ('OUTPUT_CLEANUP_SUCCEEDED', 'output_cleanup', 'local', 'post.cleanup_success'),
    ('OUTPUT_CLEANUP_FAILED', 'output_cleanup', 'cloud', 'post.cleanup_failed'),
    ('CANCELED', 'cache', 'local', 'control.canceled'),
])
def test_independent_switches_and_enqueue(runtime, kind, stage, category, option):
    state, _, _ = runtime
    event = SimpleNamespace(kind=kind, stage=stage, message='', created_at=0)
    assert event_option(event, category) == option
    settings = {'notification_events': {option: True}, 'notification_' + category: False}
    assert enabled(settings, event, category)
    assert not enabled(settings | {'notification_events': {option: False}}, event, category)
    assert not enabled(settings | {'notification_enabled': False}, event, category)
    assert ms_payload(settings, event, category, 'sample')['title'] == next(x['label'] for x in CATALOG if x['id'] == option)
    with state.db.session() as session:
        session.merge(Settings(key='notification_events', value={'value': {option: True}}))
        session.merge(Settings(key='notification_' + category, value={'value': False}))
        record(session, 'sample', kind, stage, '', category)
        record(session, 'sample', kind, stage, '', category)
        assert session.scalar(select(func.count()).select_from(Outbox)) == 1


def test_preview_timeout_and_migration():
    preview = SimpleNamespace(kind='SEED_CLEANUP_COMPLETED', stage='complete', message='仅预演', created_at=0)
    assert event_option(preview, 'offline') == 'seed.preview'
    timeout = SimpleNamespace(kind='WAITING_APPROVAL', stage='local_fallback', message='CLOUD_PROCESSING_TIMEOUT')
    assert event_option(timeout, 'cloud') == 'cloud.timeout'
    converted = effective_options({'notification_offline': False, 'notification_cleanup': False})
    assert not converted['offline.failed']
    assert not converted['seed.success']
    assert not converted['post.failed']
    assert converted['cloud.success']


def test_settings_stage_roundtrip(runtime):
    state, client, _ = runtime
    settings = client.get('/api/v1/settings').json()
    assert len(settings['notification_catalog']) == len(CATALOG)
    options = {item['id']: item['attention'] for item in CATALOG}
    assert client.post('/api/settings', json={'notification_events': options}).status_code == 200
    assert state.db.settings()['notification_events'] == options
    assert client.get('/api/v1/settings').json()['notification_events'] == options
    for invalid in [{'unknown': True}, {'cloud.failed': 'false'}, []]:
        assert client.post('/api/settings', json={'notification_events': invalid}).status_code == 422
