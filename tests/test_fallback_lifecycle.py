import pytest
from sqlalchemy import select

from app.adapters.p115 import AdapterError
from app.persistence.models import Job, Operation, Task
from app.scheduler.store import enqueue
from app.services.fallback import approve_automatic
from tests.test_cloud_cancel_preparation import setup_task
from tests.test_offline import execute


@pytest.mark.parametrize("action", ["approve", "download_local", "automatic"])
async def test_fallback_clears_cloud_state_and_can_cancel_delete(runtime, action):
    state, client, _ = runtime
    identifier = setup_task(state, cancel=False, remote_id="completed-remote")
    with state.db.session() as session:
        task = session.get(Task, identifier)
        task.state, task.stage, task.error = "WAITING_APPROVAL", "local_fallback", "CLOUD_CONTENT_FAILED"
        task.policy = {"auto_fallback": True, "delete_approved": True}
        task.output = {"cid": "old-output", "failure_archive_result": "SUCCEEDED"}
        session.scalar(select(Job)).state = "SUCCEEDED"
        if action == "automatic":
            approve_automatic(session, task)
    if action != "automatic":
        assert client.post(f"/api/v1/tasks/{identifier}/commands", json={"action": action}).status_code == 202
    with state.db.session() as session:
        task = session.get(Task, identifier)
        assert task.stage == "cache" and not task.remote_id and task.output == {}
        assert not task.policy["delete_approved"]
        assert task.policy["execution_generation"] == 1
        assert session.get(Operation, "prep-test").status == "ABANDONED"
    assert client.post(f"/api/v1/tasks/{identifier}/commands", json={"action": "cancel"}).status_code == 202
    assert client.delete(f"/api/v1/tasks/{identifier}").status_code == 200


async def test_cancel_content_failure_with_remote_id(runtime):
    state, client, _ = runtime
    identifier = setup_task(state, cancel=False, remote_id="completed")
    with state.db.session() as session:
        task = session.get(Task, identifier)
        task.state, task.stage, task.error = "WAITING_APPROVAL", "local_fallback", "CLOUD_CONTENT_FAILED"
    assert client.post(f"/api/v1/tasks/{identifier}/commands", json={"action": "cancel"}).status_code == 202
    assert client.delete(f"/api/v1/tasks/{identifier}").status_code == 200


@pytest.mark.parametrize("raise_error", [False, True])
async def test_cancel_during_download_finishes_without_retry(runtime, monkeypatch, raise_error):
    state, client, _ = runtime
    with state.db.session() as session:
        task = Task(object_key="download-cancel", content_key="download-cancel", source="115",
                    name="sample.zip", size=1, state="READY", stage="cache", locator={"pickcode": "fake"})
        session.add(task)
        session.flush()
        identifier = task.id
        enqueue(session, "local:" + identifier, "local_extract", {"task_id": identifier})

    async def download(task, job):
        with state.db.session() as session:
            current = session.get(Task, identifier)
            current.state, current.cancel_requested = "RUNNING", True
        if raise_error:
            raise AdapterError("REMOTE_UNAVAILABLE")
        return None

    monkeypatch.setattr(state.engine.local.transfer, "download", download)
    await execute(state, "local_extract")
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "CANCELED"
        assert session.scalar(select(Job)).state == "SUCCEEDED"
    assert client.delete(f"/api/v1/tasks/{identifier}").status_code == 200


async def test_cancel_cloud_failure_does_not_retry(runtime, monkeypatch):
    state, client, adapter = runtime
    identifier = setup_task(state, stage="cloud_progress", cancel=True, remote_id="remote")
    async def failed(*args):
        return "failed"
    monkeypatch.setattr(adapter, "extract_status", failed)
    await execute(state, "cloud_extract")
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "CANCELED"
        session.get(Operation, "prep-test").status = "ACCEPTED"
    assert client.delete(f"/api/v1/tasks/{identifier}").status_code == 200


async def test_document_cancel_during_download(runtime, monkeypatch):
    state, client, _ = runtime
    with state.db.session() as session:
        task = Task(object_key="doc-cancel", content_key="doc-cancel", source="115",
                    name="sample.txt", size=1, state="READY", stage="document_copy",
                    locator={"pickcode": "fake"}, policy={"document_action": "copy"})
        session.add(task)
        session.flush()
        identifier = task.id
        enqueue(session, "document-copy:" + identifier, "document_copy", {"task_id": identifier})
    async def download(task, job):
        assert client.post(f"/api/v1/tasks/{identifier}/commands", json={"action": "cancel"}).status_code == 202
        return None
    monkeypatch.setattr(state.engine.local.transfer, "download", download)
    await execute(state, "document_copy")
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "CANCELED"
        assert session.scalar(select(Job)).state == "SUCCEEDED"
    assert client.delete(f"/api/v1/tasks/{identifier}").status_code == 200


def test_reprocess_cloud_clears_fallback_flags(runtime):
    state, client, _ = runtime
    with state.db.session() as session:
        task = Task(object_key="reprocess-flags", content_key="reprocess-flags", source="115",
                    name="sample.zip", size=1, state="CANCELED", stage="cache",
                    policy={"fallback_approved": True, "cloud_started_at": 1,
                            "cloud_remote_unsettled": True, "previous_cloud_remote_id": "old"})
        session.add(task)
        session.flush()
        identifier, version = task.id, task.version
    assert client.post(f"/api/v1/tasks/{identifier}/reprocess", json={
        "expected_version": version, "confirm_name": "sample.zip"}).status_code == 202
    with state.db.session() as session:
        task = session.get(Task, identifier)
        assert task.stage == "cloud_prepare"
        assert not task.policy.get("fallback_approved")
        assert not task.policy.get("cloud_started_at")


def test_retry_rejects_inflight_job(runtime):
    state, client, _ = runtime
    identifier = setup_task(state, cancel=False)
    with state.db.session() as session:
        session.get(Task, identifier).state = "FAILED"
        session.scalar(select(Job)).state = "LEASED"
    assert client.post(f"/api/v1/tasks/{identifier}/commands", json={"action": "retry"}).status_code == 409
    assert client.delete(f"/api/v1/tasks/{identifier}").status_code == 409


async def test_offline_virtual_source_document_registers(runtime):
    state, _, _ = runtime
    with state.db.session() as session:
        task = Task(object_key="virtual-document", content_key="virtual-document", source="115",
                    name="sample.txt", size=1, state="READY", stage="document_copy",
                    locator={"source_id": "115-offline", "pickcode": "fake"},
                    policy={"document_action": "register"})
        session.add(task)
        session.flush()
        identifier = task.id
        enqueue(session, "document-copy:" + identifier, "document_copy", {"task_id": identifier})
    await execute(state, "document_copy")
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "SUCCEEDED"
