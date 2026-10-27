import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from bencode2 import bencode
from sqlalchemy import select

from app.adapters.p115 import AdapterError, CloudFile, OfflineStatus, P115Adapter
from app.domain.torrents import MAX_TORRENT_BYTES, parse_torrent
from app.persistence.models import Offline, ResourceLink, Task
from tests.test_offline import execute


def seed(name=b"example.zip", multi=False):
    info = {b"name": name, b"piece length": 16384, b"pieces": b"a" * 20}
    if multi:
        info[b"files"] = [{b"length": 123, b"path": [b"one.zip"]},
                          {b"length": 45, b"path": [b"notes.txt"]}]
    else:
        info[b"length"] = 123
    return bencode({b"announce": b"https://tracker.invalid/announce", b"info": info})


@pytest.mark.parametrize("multi", [False, True])
async def test_seed_import_submission_poll_and_dedup(runtime, multi):
    state, client, adapter = runtime
    raw = seed(multi=multi)
    response = client.post("/api/v1/offline/import", files={
        "file": ("example.torrent", raw, "application/x-bittorrent"),
    }, data={"auto_extract": "true"})
    assert response.status_code == 202 and response.json()["queued"] == 1
    with state.db.session() as session:
        task = session.scalar(select(Offline))
        identifier, digest = task.id, task.hash_value
        assert task.kind == "magnet" and not task.auto_extract and task.size == 0
    assert not (state.config.data_dir / "torrents").exists()
    duplicate = client.post("/api/v1/offline/import", files={"file": ("renamed.torrent", raw)})
    assert duplicate.json()["duplicates"] == 1
    magnet = client.post("/api/v1/offline/import", json={"text": parse_torrent(raw).original})
    assert magnet.json()["duplicates"] == 1
    await execute(state, "offline_submit")
    assert adapter.submissions[0][0] == [parse_torrent(raw).original]
    assert len(adapter.folders) == 1
    adapter.statuses = [OfflineStatus(digest, "", "example.zip", "success")]
    await state.engine.offline.poll(manual=True)
    with state.db.session() as session:
        task = session.get(Offline, identifier)
        assert task.status == "success"
        assert task.target_cid == adapter.submissions[0][1]


@pytest.mark.parametrize("raw", [
    b"", b"not a seed", seed() + b"garbage", bencode({}), bencode({b"info": []}),
    bencode({b"info": {}}), bencode({b"info": {b"meta version": 2}}),
    seed(b"bad\nname"),
])
def test_invalid_seed_rejected(runtime, raw):
    _, client, _ = runtime
    response = client.post("/api/v1/offline/import", files={"file": ("a.torrent", raw)})
    assert response.status_code == 422


def test_local_info_hash_name_and_no_file_metadata():
    name = "\u6d4b\u8bd5 & +.zip"
    info = {b"name": name.encode()}
    link = parse_torrent(bencode({b"info": info}))
    assert link.hash_value == hashlib.sha1(bencode(info)).hexdigest()
    assert link.kind == "magnet" and link.size == 0 and link.name == name
    query = parse_qs(urlsplit(link.original).query)
    assert query == {"xt": ["urn:btih:" + link.hash_value], "dn": [name]}
    assert link.original.startswith("magnet:?xt=urn:btih:" + link.hash_value + "&dn=")
    from app.domain.links import parse_link
    assert parse_link(link.original) == link
    fallback = parse_torrent(bencode({b"info": {b"length": 123}}))
    assert fallback.name == "Unknown_Torrent"


def test_unsorted_seed_hashes_original_info_bytes():
    info = b"d4:name7:foo.zip6:lengthi123ee"
    raw = b"d4:info" + info + b"8:announce3:urle"
    link = parse_torrent(raw)
    assert link.hash_value == hashlib.sha1(info).hexdigest()
    assert link.name == "foo.zip"


def test_duplicate_info_keys_rejected():
    with pytest.raises(ValueError, match="INVALID_TORRENT"):
        parse_torrent(b"d4:infod4:name1:ae4:infod4:name1:bee")


def test_seed_limits_and_auto_extract_false(runtime):
    state, client, _ = runtime
    assert client.post("/api/v1/offline/import",
                       files={"file": ("large.torrent", b"x" * (MAX_TORRENT_BYTES + 1))}).status_code == 413
    assert client.post("/api/v1/offline/import", files={"file": ("a.torrent", seed())},
                       data={"text": "magnet:?ignored"}).status_code == 422
    assert client.post("/api/v1/offline/import", files={"file": ("a.torrent", seed())},
                       data={"auto_extract": "false"}).status_code == 202
    with state.db.session() as session:
        assert not session.scalar(select(Offline)).auto_extract


async def test_legacy_seed_submits_without_source_file(runtime):
    state, client, adapter = runtime
    client.post("/api/v1/offline/import", files={"file": ("a.torrent", seed())})
    with state.db.session() as session:
        session.scalar(select(Offline)).kind = "torrent"
    await execute(state, "offline_submit")
    assert adapter.submissions[0][0] == [parse_torrent(seed()).original]
    assert len(adapter.folders) == 1


async def test_legacy_failed_seed_retries_as_magnet(runtime):
    state, client, adapter = runtime
    client.post("/api/v1/offline/import", files={"file": ("a.torrent", seed())})
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        identifier = item.id
        item.kind, item.status, item.error = "torrent", "submit_failed", "TORRENT_REMOTE_PARSE_FAILED"
    assert state.engine.offline.retry(identifier) == 1
    await execute(state, "offline_submit")
    assert adapter.submissions[0][0] == [parse_torrent(seed()).original]
    with state.db.session() as session:
        item = session.get(Offline, identifier)
        assert item.status == "submitted" and not item.error


async def test_normal_magnet_batch_limit_unchanged(runtime):
    state, client, adapter = runtime
    links = [parse_torrent(seed(f"item-{number}.zip".encode())).original for number in range(11)]
    response = client.post("/api/v1/offline/import", json={"text": "\n".join(links)})
    assert response.json()["queued"] == 11
    await execute(state, "offline_submit")
    assert adapter.submissions[0][0] == links[:10]
    assert len(adapter.folders) == 1
    with state.db.session() as session:
        assert len(list(session.scalars(select(Offline).where(Offline.status == "queued")))) == 1


async def test_seed_multi_file_archives_do_not_enter_global_extraction(runtime):
    state, client, _ = runtime
    client.post("/api/v1/offline/import",
                files={"file": ("a.torrent", seed(b"example", multi=True))})
    await execute(state, "offline_submit")
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        cid = item.target_cid
        item.status = "success"
        candidates = [
            CloudFile("dir", cid, "example", 0, is_dir=True),
            CloudFile("archive", "dir", "one.zip", 123, pickcode="fake"),
            CloudFile("plain", "dir", "notes.txt", 45),
        ]
        state.engine.offline.bind_candidates(
            session, cid, candidates, state.engine.archives.adopt,
        )
    with state.db.session() as session:
        archives = list(session.scalars(select(Task)))
        assert not archives
        assert session.scalar(select(ResourceLink)) is None



@pytest.mark.parametrize("uncertain", [False, True])
async def test_seed_submission_error_and_retry(runtime, monkeypatch, uncertain):
    state, client, adapter = runtime
    client.post("/api/v1/offline/import", files={"file": ("a.torrent", seed())})
    original = adapter.submit

    async def reject(*args):
        raise AdapterError("REMOTE_UNAVAILABLE", uncertain=uncertain)

    monkeypatch.setattr(adapter, "submit", reject)
    await execute(state, "offline_submit")
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        identifier = item.id
        assert item.status == ("submission_unknown" if uncertain else "submit_failed")
    monkeypatch.setattr(adapter, "submit", original)
    if uncertain:
        with pytest.raises(ValueError, match="ONLY_TERMINAL_FAILURE_CAN_RETRY"):
            state.engine.offline.retry(identifier)
        assert not adapter.submissions
    else:
        assert state.engine.offline.retry(identifier) == 1
        await execute(state, "offline_submit")
        assert adapter.submissions[0][0][0].startswith("magnet:")


def test_seed_idempotency(runtime):
    _, client, _ = runtime
    kwargs = {"files": {"file": ("a.torrent", seed())}, "headers": {"Idempotency-Key": "seed-test"}}
    first = client.post("/api/v1/offline/import", **kwargs)
    second = client.post("/api/v1/offline/import", **kwargs)
    assert first.json()["batch_id"] == second.json()["batch_id"]
    response = client.post("/api/v1/offline/import",
                           files={"file": ("other.torrent", seed(b"other.zip"))},
                           headers={"Idempotency-Key": "seed-test"})
    assert response.status_code == 409


async def test_real_adapter_only_uses_normal_offline_submit(monkeypatch):
    adapter = object.__new__(P115Adapter)
    calls = []

    async def call(name, *args, **kwargs):
        calls.append((name, args, kwargs))
        return {"state": True}

    monkeypatch.setattr(adapter, "call", call)
    link = parse_torrent(seed()).original
    await adapter.submit([link], "destination")
    assert calls == [("clouddownload_task_add_urls",
                      ({"url[0]": link, "wp_path_id": "destination"},),
                      {"mutation": True})]
    assert not hasattr(adapter, "submit_torrent")
