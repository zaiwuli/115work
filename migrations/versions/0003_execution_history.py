"""Freeze offline generations and mapping revisions."""
from alembic import op

from app.persistence.models import MappingRevision, OfflineExecution

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    for model in (OfflineExecution, MappingRevision):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Restore a matching backup to roll back execution history")
