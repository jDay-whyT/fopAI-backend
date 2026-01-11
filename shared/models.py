from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import text
from sqlalchemy.sql import func

metadata = MetaData()

offsets = Table(
    "offsets",
    metadata,
    Column("chat_id", BigInteger, primary_key=True),
    Column("last_message_id", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

raw_messages = Table(
    "raw_messages",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", Integer, nullable=False),
    Column("date", DateTime(timezone=True), nullable=True),
    Column("text", Text, nullable=True),
    Column("meta_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("chat_id", "message_id", name="uq_raw_messages_chat_message"),
)

draft_posts = Table(
    "draft_posts",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("raw_id", BigInteger, ForeignKey("raw_messages.id"), unique=True),
    Column("title", Text, nullable=True),
    Column("body", Text, nullable=True),
    Column("image_prompt", Text, nullable=True),
    Column("status", String, nullable=False),
    Column("reason", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("model", String, nullable=True),
    Column("tokens", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

published_posts = Table(
    "published_posts",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("draft_id", BigInteger, ForeignKey("draft_posts.id"), unique=True),
    Column("target_chat_id", BigInteger, nullable=False),
    Column("channel_message_id", BigInteger, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)
