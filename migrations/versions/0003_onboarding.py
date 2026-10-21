"""Add isolated onboarding state; existing domain data is unchanged."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_onboarding"),
        sa.CheckConstraint("id = 1", name="ck_onboarding_singleton"),
        sa.CheckConstraint(
            "(stage = 'profile' AND revision = 1) OR "
            "(stage = 'taxonomy' AND revision = 2) OR "
            "(stage = 'calibration' AND revision = 3) OR "
            "(stage = 'dry_run' AND revision = 4) OR "
            "(stage = 'active' AND revision = 5)",
            name="ck_onboarding_stage_matches_revision",
        ),
    )


def downgrade() -> None:
    op.drop_table("onboarding")
