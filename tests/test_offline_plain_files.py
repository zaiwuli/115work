from sqlalchemy import select

from app.adapters.p115 import CloudFile
from app.persistence.models import Offline, Task, Job
from tests.conftest import ed2k


async def test_plain_offline_file_does_not_create_extraction_task(runtime):
    state, _, _ = runtime
    await state.engine.offline.import_text(ed2k("movie.mp4"))
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        item.target_cid = "test-folder"
        item.status = "downloading"
        session.flush()
        state.engine.offline.bind_candidates(
            session, "test-folder", [CloudFile("plain", "test-folder", "movie.mp4", 123)],
            state.engine.archives.adopt,
        )
        assert item.status == "success" and item.stage == "complete"
        assert session.scalar(select(Task)) is None
        assert session.scalar(select(Job).where(Job.kind == "cloud_extract")) is None


def test_default_document_is_not_registered_as_extraction(runtime):
    state, _, _ = runtime
    with state.db.session() as session:
        assert state.engine.archives.adopt(
            session, CloudFile("readme", "test-folder", "readme.txt", 123),
        ) is None
        assert session.scalar(select(Task)) is None


async def test_offline_documents_ignore_document_archive_rule(runtime):
    state, _, _ = runtime
    from app.persistence.models import Rule
    with state.db.session() as session:
        session.add(Rule(name="archive documents", source_id="115-offline", config={
            "pattern": "*", "document_action": "archive", "document_links": True,
        }))
        session.flush()
        for name in ["readme.txt", "poster.jpg", "movie.mp4", "source.torrent"]:
            assert state.engine.archives.adopt(
                session, CloudFile(name, "folder", name, 123), source_id="115-offline",
            ) is None
        assert session.scalar(select(Task)) is None


async def test_magnet_folder_keeps_folder_name_without_child_tasks(runtime):
    state, _, _ = runtime
    await state.engine.offline.import_text("magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Resource")
    with state.db.session() as session:
        item = session.scalar(select(Offline))
        item.target_cid, item.status = "date", "downloading"
        state.engine.offline.bind_candidates(session, "date", [
            CloudFile("resource", "date", "Resource", 0, is_dir=True),
            CloudFile("video", "resource", "movie.mp4", 123),
            CloudFile("poster", "resource", "poster.jpg", 12),
        ], lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not adopt plain files")))
        assert item.name == "Resource"
        assert item.stage == "complete" and item.status == "success"
        assert session.scalar(select(Task)) is None
