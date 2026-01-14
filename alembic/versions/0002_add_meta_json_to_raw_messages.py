"""add meta_json to raw_messages

Revision ID: 0002
Revises: 0001
Create Date: 2026-01-11 00:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE raw_messages ADD COLUMN IF NOT EXISTS meta_json JSONB"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE raw_messages DROP COLUMN IF EXISTS meta_json"))
