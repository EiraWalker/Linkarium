"""Check the SQLite capability used when preparing a verified restore database."""
import sqlite3
import unittest


class SQLiteRuntimeTests(unittest.TestCase):
    def test_json_constraints_can_be_written_with_untrusted_schema(self):
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE fixture (payload TEXT CHECK (JSON_VALID(payload)))")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("INSERT INTO fixture VALUES ('{}')")
            self.assertEqual(connection.execute("SELECT payload FROM fixture").fetchone(), ("{}",))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO fixture VALUES ('invalid JSON')")


if __name__ == "__main__":
    unittest.main()
