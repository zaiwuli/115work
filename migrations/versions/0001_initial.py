"""Initial persistent workflow schema."""
from alembic import op

from app.persistence.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    names = {
        "settings", "offline_batches", "offline_tasks", "batch_import_items",
        "date_folder_cache", "task_chains", "offline_resource_links",
        "processed_identities", "task_events", "notification_outbox", "jobs",
        "operation_journal", "sources", "document_imports", "file_observations",
    }
    Base.metadata.create_all(op.get_bind(), tables=[
        table for table in Base.metadata.sorted_tables if table.name in names
    ])


def downgrade():
    raise RuntimeError("Restore a matching database backup to roll back this schema.")
