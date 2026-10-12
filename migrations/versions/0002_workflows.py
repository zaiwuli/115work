"""Persist connections, rules, schedules, steps, resources and transfer evidence."""
from alembic import op

from app.persistence.models import (
    ArchiveGroup, Audit, CacheEntry, Connection, Locator, Resource, Rule, Schedule, Step, Transfer,
)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    for model in (Connection, Rule, Schedule, Step, Resource, Locator, Transfer, CacheEntry,
                  ArchiveGroup, Audit):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Restore a consistent backup when rolling back workflow persistence")
