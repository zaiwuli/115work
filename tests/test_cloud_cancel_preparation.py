import pytest
from sqlalchemy import select

from app.adapters.p115 import AdapterError
from app.persistence.models import Job, Operation, Settings, Task
from app.scheduler.store import enqueue
from tests.test_offline import execute


def setup_task(state, stage="cloud_inspect", cancel=True, remote_id=""):
    with state.db.session() as session:
        task = Task(object_key="cancel-prep", content_key="cancel-prep", name="sample.zip",
                    source="115", size=123, state="WAITING_EXTERNAL", stage=stage,
                    cancel_requested=cancel, remote_id=remote_id, attempts=1,
                    locator={"pickcode": "fake", "fid": "sample", "cid": "root"})
        session.add(task)
        session.flush()
        identifier = task.id
        session.add(Settings(key="cloud_active", value={"value": identifier}))
        session.add(Operation(key="prep-test", task_id=identifier, kind="cloud_prepare"))
        enqueue(session, "archive:" + identifier, "cloud_extract", {"task_id": identifier})
    return identifier


@pytest.mark.parametrize("stage", ["cloud_prepare", "cloud_inspect", "cloud_password"])
async def test_cancel_preparation_releases_slot_and_allows_delete(runtime, monkeypatch, stage):
    state, client, adapter = runtime
    identifier = setup_task(state, stage)

    async def forbidden(*args):
        raise AssertionError("Canceled preparation must not call 115")

    monkeypatch.setattr(adapter, "inspect_extract", forbidden)
    monkeypatch.setattr(adapter, "prepare_extract", forbidden)
    await execute(state, "cloud_extract")
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "CANCELED"
        assert session.get(Settings, "cloud_active").value["value"] == ""
        assert session.get(Operation, "prep-test").status == "CANCELED"
        assert session.scalar(select(Job).where(Job.kind == "cloud_extract")).state == "SUCCEEDED"
    assert client.delete("/api/v1/tasks/" + identifier).status_code == 200


@pytest.mark.parametrize("uncertain,expected", [(False, "FAILED"), (True, "WAITING_EXTERNAL")])
async def test_rejection_vs_uncertain_response(runtime, monkeypatch, uncertain, expected):
    state, _, adapter = runtime
    identifier = setup_task(state, cancel=False)

    async def reject(*args):
        raise AdapterError("REMOTE_REJECTED", uncertain=uncertain)

    monkeypatch.setattr(adapter, "inspect_extract", reject)
    await execute(state, "cloud_extract")
    with state.db.session() as session:
        assert session.get(Task, identifier).state == expected
        active = session.get(Settings, "cloud_active").value["value"]
        assert bool(active) is uncertain


async def test_cancel_during_preparation_response(runtime, monkeypatch):
    state, _, adapter = runtime
    identifier = setup_task(state, cancel=False)

    async def respond(*args):
        with state.db.session() as session:
            session.get(Task, identifier).cancel_requested = True
        return "success"

    monkeypatch.setattr(adapter, "inspect_extract", respond)
    await execute(state, "cloud_extract")
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "CANCELED"
        assert session.get(Settings, "cloud_active").value["value"] == ""


async def test_submitted_extraction_cancel_still_observes_remote(runtime, monkeypatch):
    state, _, adapter = runtime
    identifier = setup_task(state, stage="cloud_progress", remote_id="remote")
    calls = []

    async def observe(*args):
        calls.append(args)
        return "running"

    monkeypatch.setattr(adapter, "extract_status", observe)
    await execute(state, "cloud_extract")
    assert len(calls) == 1
    with state.db.session() as session:
        assert session.get(Task, identifier).state == "WAITING_EXTERNAL"
        assert session.get(Settings, "cloud_active").value["value"] == identifier


def test_cancel_settles_delayed_preparation_job_and_allows_delete(runtime):
    import time
    state, client, _ = runtime
    identifier = setup_task(state, cancel=False)
    with state.db.session() as session:
        session.scalar(select(Job).where(Job.kind == "cloud_extract")).due = time.time() + 300
    assert client.post("/api/v1/tasks/" + identifier + "/commands",
                       json={"action": "cancel"}).status_code == 202
    with state.db.session() as session:
        assert session.scalar(select(Job).where(Job.kind == "cloud_extract")).state == "SUCCEEDED"
        assert session.get(Operation, "prep-test").status == "CANCELED"
    assert client.delete("/api/v1/tasks/" + identifier).status_code == 200


async def test_previously_canceled_preparation_closes_journal(runtime):
    state, client, _ = runtime
    identifier = setup_task(state)
    with state.db.session() as session:
        session.get(Task, identifier).state = "CANCELED"
    await execute(state, "cloud_extract")
    assert client.delete("/api/v1/tasks/" + identifier).status_code == 200
