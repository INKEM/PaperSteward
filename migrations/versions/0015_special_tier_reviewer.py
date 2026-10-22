"""Record who manually overrode a special-bucket tier."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("special_tier_snapshot") as batch:
        batch.add_column(sa.Column("reviewed_by", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("special_tier_snapshot") as batch:
        batch.drop_column("reviewed_by")
