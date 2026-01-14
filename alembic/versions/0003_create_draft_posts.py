"""create draft_posts

Revision ID: 0003
Revises: 0002
Create Date: 2026-01-11 00:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "draft_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "raw_id",
            sa.BigInteger(),
            sa.ForeignKey("raw_messages.id", ondelete="CASCADE"),
            unique=True,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("image_prompt", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_draft_posts_raw_id", "draft_posts", ["raw_id"], unique=True)
    op.create_index("ix_draft_posts_status", "draft_posts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_draft_posts_status", table_name="draft_posts")
    op.drop_index("ix_draft_posts_raw_id", table_name="draft_posts")
    op.drop_table("draft_posts")
