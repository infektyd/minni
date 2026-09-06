#!/usr/bin/env python3
"""Report legacy learning aliases in an explicit database, without changing it.

Usage: .venv/bin/python scripts/diagnose_legacy_aliases.py --db DB --vault VAULT
The vault must be the one associated with that database; it locates canonical
document paths. This command proposes review candidates, never migrations.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _ReadOnlyStore:
    def __init__(self, connection, vault):
        self.connection = connection
        self.config = SimpleNamespace(vault_path=str(vault))

    def _get_conn(self):
        return self.connection

    @contextmanager
    def cursor(self):
        cursor = self.connection.cursor()
        try:
            yield cursor
        finally:
            cursor.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")

    from minni.minnid_runtime.health import (
        diagnose_legacy_aliases,
        format_legacy_alias_report,
    )

    connection = None
    try:
        connection = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        report = diagnose_legacy_aliases(
            _ReadOnlyStore(connection, args.vault.expanduser().resolve()),
            limit=args.limit,
        )
        print(format_legacy_alias_report(report))
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Cannot diagnose aliases: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
