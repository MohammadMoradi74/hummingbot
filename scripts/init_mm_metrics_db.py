#!/usr/bin/env python3
"""Create strategy metrics tables in PostgreSQL."""

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize Hummingbot strategy metrics schema.")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("MM_METRICS_DATABASE_URL", ""),
        help="SQLAlchemy Postgres URL (default: MM_METRICS_DATABASE_URL env var)",
    )
    args = parser.parse_args()
    if not args.database_url:
        logger.error(
            "Set MM_METRICS_DATABASE_URL or pass --database-url.\n"
            "Example: postgresql+psycopg://user:pass@localhost:5432/hummingbot"
        )
        return 1

    from hummingbot.strategy_metrics.db import MetricsDatabase, apply_all_schemas

    db = MetricsDatabase(args.database_url)
    if not db.ping():
        logger.error("Cannot connect to PostgreSQL.")
        return 1

    apply_all_schemas(args.database_url)
    logger.info("Metrics schemas ready (mm_* + pt_*).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
