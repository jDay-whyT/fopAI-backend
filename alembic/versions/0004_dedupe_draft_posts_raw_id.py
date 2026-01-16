"""dedupe draft_posts raw_id and enforce uniqueness

Revision ID: 0004
Revises: 0003
Create Date: 2026-01-11 00:30:00.000000

"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   raw_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY raw_id
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM draft_posts
            WHERE raw_id IS NOT NULL
        )
        DELETE FROM draft_posts
        USING ranked
        WHERE draft_posts.id = ranked.id
          AND ranked.rn > 1;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_draft_posts_raw_id ON draft_posts (raw_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_draft_posts_raw_id;")
