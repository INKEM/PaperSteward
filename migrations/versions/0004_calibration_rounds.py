"""Persist independent, immutable calibration rounds."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_calibration",
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("g2_event_id", sa.String(64), nullable=False),
        sa.Column("binding_json", sa.JSON(), nullable=False),
        sa.Column("cohort_json", sa.JSON(), nullable=False),
        sa.Column("rubric_version", sa.String(64), nullable=False),
        sa.Column("sample_kind", sa.String(16), nullable=False),
        sa.Column("agent_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("user_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("report_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("round_number", name=op.f("pk_onboarding_calibration")),
        sa.ForeignKeyConstraint(
            ["g2_event_id"], ["event.id"], name=op.f("fk_onboarding_calibration_g2_event_id_event")
        ),
        sa.CheckConstraint(
            "round_number IN (1, 2)", name=op.f("ck_onboarding_calibration_round_limit")
        ),
        sa.CheckConstraint(
            "sample_kind IN ('user', 'synthetic')",
            name=op.f("ck_onboarding_calibration_sample_kind_values"),
        ),
        sa.CheckConstraint(
            "report_json IS NULL OR (agent_json IS NOT NULL AND user_json IS NOT NULL)",
            name=op.f("ck_onboarding_calibration_report_requires_labels"),
        ),
    )


def downgrade() -> None:
    op.drop_table("onboarding_calibration")
