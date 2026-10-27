import pytest
from sqlalchemy import select
from app.persistence.models import Offline, Settings, Rule
from app.adapters.p115 import CloudFile, OfflineStatus
from app.services.seed_cleanup import validate_policy, should_remove
from tests.test_torrent_import import seed
from tests.test_offline import execute


def test_cleanup_matching():
    p = validate_policy({"mode": "include", "extensions": ["MP4"], "keywords": ["ads"]})
    assert not should_remove("movie.MP4", 100, p)
    assert should_remove("junk/a.jpg", 100, p)
    assert should_remove("ADS/movie.mp4", 100, p)


@pytest.mark.parametrize("dry_run", [True, False])
async def test_seed_waits_for_remote_completion_and_cleans(runtime, monkeypatch, dry_run):
    state, client, adapter = runtime
    with state.db.session() as session:
        session.add(Rule(name="seed", source_id="115-torrent", config={"seed_cleanup": {
            "enabled": True, "mode": "include", "extensions": [".mp4"],
            "dry_run": dry_run, "empty_dirs": True}}))
    assert client.post("/api/v1/offline/import", files={"file": ("a.torrent", seed(b"example"))}).status_code == 202
    await execute(state, "offline_submit")
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        identifier, cid, digest = item.id, item.target_cid, item.hash_value
    adapter.listings[cid] = [CloudFile("root", cid, "example", 0, is_dir=True)]
    adapter.listings["root"] = [CloudFile("video", "root", "movie.mp4", 100), CloudFile("junk", "root", "ads", 0, is_dir=True)]
    adapter.listings["junk"] = [CloudFile("poster", "junk", "poster.jpg", 10)]
    deleted = []
    async def remove(locator):
        deleted.append(locator["fid"])
        adapter.listings[locator["cid"]] = [f for f in adapter.listings[locator["cid"]] if f.fid != locator["fid"]]
    monkeypatch.setattr(adapter, "delete_verified", remove, raising=False)
    adapter.statuses = [OfflineStatus(digest, "", "example", "success")]
    await state.engine.offline.poll(manual=True)
    await execute(state, "seed_cleanup")
    assert deleted == ([] if dry_run else ["poster", "junk"])
    with state.db.session() as session:
        assert session.get(Settings, "seed-policy:" + identifier).value["result"] == ("PREVIEW" if dry_run else "SUCCEEDED")
        assert session.get(Offline, identifier).stage == "complete"
