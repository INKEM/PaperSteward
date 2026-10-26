"""放宽归档快照的主标签与归属桶为可空，支撑外部已读论文导入归档。"""

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("archive_snapshot") as batch:
        batch.alter_column("primary_tag", existing_type=sa.String(length=64), nullable=True)
        batch.alter_column("filing_bucket_id", existing_type=sa.String(length=64), nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    orphans = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM archive_snapshot "
            "WHERE primary_tag IS NULL OR filing_bucket_id IS NULL"
        )
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"存在 {orphans} 条缺少主标签或归属桶的导入归档快照，无法回退迁移 0020；"
            "请先处理这些导入的归档记录"
        )
    with op.batch_alter_table("archive_snapshot") as batch:
        batch.alter_column("primary_tag", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column("filing_bucket_id", existing_type=sa.String(length=64), nullable=False)
