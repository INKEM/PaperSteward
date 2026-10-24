"""引导重设计：三阶段状态机、材料表，移除校准表与旧门禁约束。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("onboarding_calibration")

    op.create_table(
        "intake_material",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intake_material")),
        sa.CheckConstraint("kind IN ('text', 'file')", name=op.f("ck_intake_material_kind_values")),
    )

    # 先移除旧四门禁 CHECK，否则把 calibration/dry_run 行改成 setup 会违反旧约束。
    with op.batch_alter_table("onboarding") as batch:
        batch.drop_constraint("ck_onboarding_stage_matches_revision", type_="check")

    # 旧四门禁阶段（seeds/profile/taxonomy/calibration/dry_run）全部映射到 setup；
    # active 保持可用（revision 5 → 2，与新的 stage_matches_revision 约束对齐）。
    op.execute("UPDATE onboarding SET stage = 'setup', revision = 0 WHERE stage <> 'active'")
    op.execute("UPDATE onboarding SET revision = 2 WHERE stage = 'active'")
    with op.batch_alter_table("onboarding") as batch:
        batch.create_check_constraint(
            "ck_onboarding_stage_matches_revision",
            "(stage = 'setup' AND revision = 0) OR "
            "(stage = 'draft' AND revision = 1) OR "
            "(stage = 'active' AND revision = 2)",
        )


def downgrade() -> None:
    op.execute("DELETE FROM onboarding WHERE stage IN ('setup', 'draft')")
    with op.batch_alter_table("onboarding") as batch:
        batch.drop_constraint("ck_onboarding_stage_matches_revision", type_="check")
    op.execute("UPDATE onboarding SET revision = 5 WHERE stage = 'active'")
    with op.batch_alter_table("onboarding") as batch:
        batch.create_check_constraint(
            "ck_onboarding_stage_matches_revision",
            "(stage = 'profile' AND revision = 1) OR "
            "(stage = 'taxonomy' AND revision = 2) OR "
            "(stage = 'calibration' AND revision = 3) OR "
            "(stage = 'dry_run' AND revision = 4) OR "
            "(stage = 'active' AND revision = 5)",
        )
    op.drop_table("intake_material")
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
