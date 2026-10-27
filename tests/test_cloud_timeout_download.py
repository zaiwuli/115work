import time

import pytest
from sqlalchemy import select

from app.persistence.models import Job, Operation, Settings, Task
from tests.test_cloud_cancel_preparation import setup_task
from tests.test_offline import execute


@pytest.mark.parametrize("remote_id", ["", "remote"])
async def test_cancel_timed_out_task_allows_delete(runtime, remote_id):
    state, client, _ = runtime
    identifier = setup_task(state, stage="cloud_progress", cancel=False, remote_id=remote_id)
    with state.db.session() as session:
        task = session.get(Task, identifier)
        task.policy = {"cloud_started_at": time.time() - 1000}
    await execute(state, "cloud_extract")
    with state.db.session() as session:
        session.get(Operation, "prep-test").status = "UNKNOWN"
        session.scalar(select(Job).where(Job.kind == "cloud_extract")).state = "RECONCILING"
    assert client.post("/api/v1/tasks/" + identifier + "/commands",
                       json={"action": "cancel"}).status_code == 202
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "CANCELED"
        assert session.get(Task, identifier).remote_id == remote_id
        assert session.get(Operation, "prep-test").status == "ABANDONED"
    assert client.delete("/api/v1/tasks/" + identifier).status_code == 200


@pytest.mark.parametrize("stage", ["cloud_inspect", "cloud_progress", "cloud_output"])
async def test_timeout_releases_cloud_queue(runtime, monkeypatch, stage):
    state, client, adapter = runtime
    identifier = setup_task(state, stage=stage, cancel=False,
                            remote_id="remote" if stage == "cloud_progress" else "")
    with state.db.session() as session:
        task = session.get(Task, identifier)
        task.policy = {"cloud_started_at": time.time() - 1000, "auto_fallback": False}

    async def forbidden(*args):
        raise AssertionError("Expired work must not call 115")

    monkeypatch.setattr(adapter, "inspect_extract", forbidden)
    monkeypatch.setattr(adapter, "extract_status", forbidden)
    await execute(state, "cloud_extract")
    with state.db.session() as session:
        task = session.get(Task, identifier)
        assert (task.state, task.stage, task.error) == (
            "WAITING_APPROVAL", "local_fallback", "CLOUD_PROCESSING_TIMEOUT",
        )
        assert session.get(Settings, "cloud_active").value["value"] == ""
    assert client.post("/api/v1/tasks/" + identifier + "/commands",
                       json={"action": "download_local"}).status_code == 202


@pytest.mark.parametrize("stage", ["cloud_prepare", "cloud_inspect", "cloud_progress", "cloud_password"])
def test_direct_local_download_without_retries(runtime, stage):
    state, client, _ = runtime
    identifier = setup_task(state, stage=stage, cancel=False,
                            remote_id="remote" if stage == "cloud_progress" else "")
    with state.db.session() as session:
        session.get(Task, identifier).policy = {"original_action": "delete"}
    response = client.post("/api/v1/tasks/" + identifier + "/commands",
                           json={"action": "download_local"})
    assert response.status_code == 202
    with state.db.session() as session:
        task = session.get(Task, identifier)
        assert task.stage == "cache" and task.state == "READY"
        assert task.policy["fallback_approved"]
        assert task.attempts == 1
        assert not task.remote_id
        if stage == "cloud_progress":
            assert task.policy["original_action"] == "delete"
            assert task.policy["previous_cloud_remote_id"] == "remote"
        assert session.get(Settings, "cloud_active").value["value"] == ""
        assert session.scalar(select(Job).where(Job.kind == "cloud_extract")).state == "SUCCEEDED"
        assert session.scalar(select(Job).where(Job.kind == "local_extract")).state == "READY"
    assert client.post("/api/v1/tasks/" + identifier + "/commands",
                       json={"action": "download_local"}).status_code == 409


def test_direct_download_rejects_inflight_cloud_request(runtime):
    state, client, _ = runtime
    identifier = setup_task(state, cancel=False)
    state.engine.store.claim(time.time() + 86400, ["cloud_extract"])
    assert client.post("/api/v1/tasks/" + identifier + "/commands",
                       json={"action": "download_local"}).status_code == 409
    with state.db.session() as session:
        assert session.get(Task, identifier).stage == "cloud_inspect"


def test_cloud_timeout_setting_validation(runtime):
    _, client, _ = runtime
    for value in [None, True, 0, 86401, "900"]:
        assert client.post("/api/v1/settings", json={
            "cloud_extract_timeout_seconds": value,
        }).status_code == 422
    assert client.post("/api/v1/settings", json={
        "cloud_extract_timeout_seconds": 600,
    }).status_code == 200


@pytest.mark.parametrize("inflight", [False, True])
def test_watchdog_handles_unpolled_reconciliation(runtime, inflight):
    state, _, _ = runtime
    identifier = setup_task(state, stage="cloud_export_unknown", cancel=False)
    with state.db.session() as session:
        task = session.get(Task, identifier)
        task.state = "RECONCILING"
        task.policy = {"cloud_started_at": time.time() - 1000, "auto_fallback": True}
        job = session.scalar(select(Job).where(Job.kind == "cloud_extract"))
        job.state = "RUNNING" if inflight else "RECONCILING"
    state.engine.plan()
    with state.db.session() as session:
        task = session.get(Task, identifier)
        assert task.state == ("RECONCILING" if inflight else "WAITING_APPROVAL")
        if not inflight:
            assert task.policy["cloud_remote_unsettled"]
            assert not task.policy.get("fallback_approved")
            assert session.get(Settings, "cloud_active").value["value"] == ""
