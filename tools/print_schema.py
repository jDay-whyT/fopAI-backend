from __future__ import annotations

from sqlalchemy import text

from shared.db import get_engine


def main() -> int:
    engine = get_engine()
    query = text(
        """
        SELECT table_name,
               column_name,
               data_type,
               ordinal_position
        FROM information_schema.columns
        WHERE table_name IN ('draft_posts', 'drafts', 'raw_messages')
        ORDER BY table_name, ordinal_position
        """
    )

    with engine.connect() as connection:
        result = connection.execute(query)
        for row in result:
            print(
                f"{row.table_name} {row.column_name} {row.data_type} {row.ordinal_position}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
