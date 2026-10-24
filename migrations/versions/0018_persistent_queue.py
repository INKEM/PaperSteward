"""旧延期论文恢复长期队列，历史延期事件仅保留审计。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE paper SET status = 'queued' WHERE status = 'snoozed'")


def downgrade() -> None:
    # 不猜测哪些 queued 原先是 snoozed；恢复旧行为需使用升级前备份。
    pass
