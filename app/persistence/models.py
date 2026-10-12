import time
import uuid

from sqlalchemy import JSON, Float, String, Text, UniqueConstraint, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def identifier() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Settings(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Batch(Base):
    __tablename__ = "offline_batches"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    created_at: Mapped[float] = mapped_column(default=time.time)
    target_date: Mapped[str]
    root_cid: Mapped[str]
    target_cid: Mapped[str] = mapped_column(default="")
    auto_extract: Mapped[bool] = mapped_column(default=True)
    hidden: Mapped[bool] = mapped_column(default=False)
    invalid_count: Mapped[int] = mapped_column(default=0)


class Offline(Base):
    __tablename__ = "offline_tasks"
    __table_args__ = (UniqueConstraint("account_id", "identity"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(default="115-main")
    identity: Mapped[str]
    original_link: Mapped[str] = mapped_column(Text)
    name: Mapped[str]
    kind: Mapped[str]
    size: Mapped[int] = mapped_column(default=0)
    hash_value: Mapped[str] = mapped_column(default="")
    batch_id: Mapped[str] = mapped_column(ForeignKey("offline_batches.id"))
    target_date: Mapped[str]
    root_cid: Mapped[str]
    target_cid: Mapped[str] = mapped_column(default="")
    auto_extract: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(default="queued", index=True)
    stage: Mapped[str] = mapped_column(default="offline_submit")
    error: Mapped[str] = mapped_column(default="")
    check_count: Mapped[int] = mapped_column(default=0)
    next_check: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    retry_count: Mapped[int] = mapped_column(default=0)
    generation: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[float] = mapped_column(default=time.time)
    updated_at: Mapped[float] = mapped_column(default=time.time)


class BatchItem(Base):
    __tablename__ = "batch_import_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("offline_batches.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("offline_tasks.id", ondelete="CASCADE"))
    duplicate: Mapped[bool] = mapped_column(default=False)


class DateFolder(Base):
    __tablename__ = "date_folder_cache"
    __table_args__ = (UniqueConstraint("account_id", "root_cid", "target_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(default="115-main")
    root_cid: Mapped[str]
    target_date: Mapped[str]
    cid: Mapped[str]


class Task(Base):
    __tablename__ = "task_chains"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    object_key: Mapped[str] = mapped_column(index=True)
    content_key: Mapped[str] = mapped_column(index=True)
    source: Mapped[str]
    name: Mapped[str]
    size: Mapped[int]
    state: Mapped[str] = mapped_column(default="READY", index=True)
    stage: Mapped[str]
    wait_reason: Mapped[str] = mapped_column(default="")
    error: Mapped[str] = mapped_column(default="")
    attempts: Mapped[int] = mapped_column(default=0)
    password_attempts: Mapped[int] = mapped_column(default=0)
    locator: Mapped[dict] = mapped_column(JSON, default=dict)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    remote_id: Mapped[str] = mapped_column(default="")
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    hidden: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[float] = mapped_column(default=time.time)
    updated_at: Mapped[float] = mapped_column(default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    version: Mapped[int] = mapped_column(default=1)
    __mapper_args__ = {"version_id_col": version}


class ResourceLink(Base):
    __tablename__ = "offline_resource_links"
    __table_args__ = (UniqueConstraint("offline_id", "task_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    offline_id: Mapped[str] = mapped_column(ForeignKey("offline_tasks.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("task_chains.id"))
    evidence: Mapped[str]


class Identity(Base):
    __tablename__ = "processed_identities"
    key: Mapped[str] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_chains.id"))
    result: Mapped[str]


class Event(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str]
    stage: Mapped[str]
    message: Mapped[str]
    created_at: Mapped[float] = mapped_column(default=time.time)


class Outbox(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("task_events.id"), unique=True)
    category: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_run: Mapped[float] = mapped_column(default=time.time)
    error: Mapped[str] = mapped_column(default="")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    key: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(default="READY", index=True)
    due: Mapped[float] = mapped_column(default=time.time, index=True)
    owner: Mapped[str] = mapped_column(default="")
    generation: Mapped[int] = mapped_column(default=0)
    dispatch_count: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    dirty: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str] = mapped_column(default="")


class Operation(Base):
    __tablename__ = "operation_journal"
    key: Mapped[str] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(default="PREPARED")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(default=time.time)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    name: Mapped[str]
    kind: Mapped[str]
    config: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=True)
    revision: Mapped[int] = mapped_column(default=1)
    interval: Mapped[int] = mapped_column(default=60)
    next_run: Mapped[float] = mapped_column(default=time.time)
    cursor: Mapped[int] = mapped_column(default=0)


class DocumentImport(Base):
    __tablename__ = "document_imports"
    digest: Mapped[str] = mapped_column(primary_key=True)
    path: Mapped[str]
    batch_id: Mapped[str] = mapped_column(default="")
    statistics: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(default="PARSED")
    error: Mapped[str] = mapped_column(default="")


class Observation(Base):
    __tablename__ = "file_observations"
    key: Mapped[str] = mapped_column(primary_key=True)
    size: Mapped[int]
    mtime: Mapped[float]
    stable_since: Mapped[float]


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    name: Mapped[str]
    kind: Mapped[str]
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    credential: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(default=1)
    health: Mapped[str] = mapped_column(default="UNKNOWN")
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    name: Mapped[str]
    source_id: Mapped[str] = mapped_column(default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    name: Mapped[str]
    source_id: Mapped[str] = mapped_column(default="")
    trigger: Mapped[str] = mapped_column(default="interval")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    timezone: Mapped[str] = mapped_column(default="Asia/Shanghai")
    misfire: Mapped[str] = mapped_column(default="coalesce")
    enabled: Mapped[bool] = mapped_column(default=True)
    revision: Mapped[int] = mapped_column(default=1)
    next_run: Mapped[float] = mapped_column(default=time.time, index=True)
    last_slot: Mapped[str] = mapped_column(default="")


class Step(Base):
    __tablename__ = "task_steps"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    operation_key: Mapped[str] = mapped_column(unique=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_chains.id"), index=True)
    kind: Mapped[str]
    state: Mapped[str] = mapped_column(default="READY")
    dependencies: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(default="")
    updated_at: Mapped[float] = mapped_column(default=time.time)


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    object_key: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    size: Mapped[int]
    strong_hash: Mapped[str] = mapped_column(default="")
    version: Mapped[dict] = mapped_column(JSON, default=dict)
    task_id: Mapped[str] = mapped_column(default="", index=True)


class Locator(Base):
    __tablename__ = "locators"
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"))
    connection_id: Mapped[str]
    kind: Mapped[str]
    revision: Mapped[int] = mapped_column(default=1)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class Transfer(Base):
    __tablename__ = "transfer_tasks"
    task_id: Mapped[str] = mapped_column(ForeignKey("task_chains.id"), primary_key=True)
    state: Mapped[str] = mapped_column(default="READY")
    expected_size: Mapped[int]
    bytes_done: Mapped[int] = mapped_column(default=0)
    speed: Mapped[float] = mapped_column(default=0)
    eta: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    version: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(default="")
    updated_at: Mapped[float] = mapped_column(default=time.time)


class CacheEntry(Base):
    __tablename__ = "cache_entries"
    task_id: Mapped[str] = mapped_column(ForeignKey("task_chains.id"), primary_key=True)
    path: Mapped[str]
    size: Mapped[int]
    verification: Mapped[str]
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(default=time.time)


class ArchiveGroup(Base):
    __tablename__ = "archive_groups"
    id: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str]
    name: Mapped[str]
    state: Mapped[str]
    manifest: Mapped[dict] = mapped_column(JSON)


class Audit(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str]
    action: Mapped[str]
    target: Mapped[str]
    request_id: Mapped[str] = mapped_column(default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(default=time.time)


class OfflineExecution(Base):
    __tablename__ = "offline_executions"
    __table_args__ = (UniqueConstraint("offline_id", "generation"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=identifier)
    offline_id: Mapped[str] = mapped_column(ForeignKey("offline_tasks.id", ondelete="CASCADE"))
    generation: Mapped[int]
    target_date: Mapped[str]
    target_cid: Mapped[str]
    auto_extract: Mapped[bool]
    status: Mapped[str]
    error: Mapped[str] = mapped_column(default="")
    created_at: Mapped[float] = mapped_column(default=time.time)


class MappingRevision(Base):
    __tablename__ = "mapping_revisions"
    __table_args__ = (UniqueConstraint("mapping_id", "revision"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[str]
    revision: Mapped[int]
    config: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default="UNKNOWN")
    created_at: Mapped[float] = mapped_column(default=time.time)
