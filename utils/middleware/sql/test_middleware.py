"""Tests for SQL middleware."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from utils.middleware.sql.middleware import CursorMiddleware
from utils.log.types import SQLLog


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 3

    def execute(self, query, params=None):
        self.executed.append((query, params))


class CaptureLogger:
    def __init__(self):
        self.entries = []

    def sql_log(self, ctx, entry: SQLLog):
        self.entries.append(entry)


class TestSQLMiddleware(unittest.TestCase):
    def test_mutating_statement_logged(self):
        cursor = FakeCursor()
        logger = CaptureLogger()
        mw = CursorMiddleware(cursor, logger=logger)

        mw.execute(None, "INSERT INTO orders VALUES (%s)", ("v",))

        self.assertEqual(len(logger.entries), 1)
        e = logger.entries[0]
        self.assertIn("INSERT", e.statement)
        self.assertEqual(e.rows_affected, 3)

    def test_select_not_logged(self):
        cursor = FakeCursor()
        logger = CaptureLogger()
        mw = CursorMiddleware(cursor, logger=logger)

        mw.execute(None, "SELECT * FROM orders")

        self.assertEqual(len(logger.entries), 0)
        self.assertEqual(len(cursor.executed), 1)

    def test_no_logger_no_crash(self):
        cursor = FakeCursor()
        mw = CursorMiddleware(cursor, logger=None)
        mw.execute(None, "INSERT INTO t VALUES (1)")

    def test_delete_logged(self):
        cursor = FakeCursor()
        logger = CaptureLogger()
        mw = CursorMiddleware(cursor, logger=logger)
        mw.execute(None, "DELETE FROM orders WHERE id = %s", (1,))
        self.assertEqual(len(logger.entries), 1)


if __name__ == "__main__":
    unittest.main()
