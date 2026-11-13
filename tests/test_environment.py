import sqlite3
import unittest

from backend.core.config import settings


class EnvironmentTests(unittest.TestCase):
    def test_sqlite_fts5_is_available(self):
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE test_fts USING fts5(content)")

    def test_external_apis_are_disabled_by_default(self):
        self.assertFalse(settings.external_api_enabled)

    def test_data_directory_is_local_storage_by_default(self):
        self.assertEqual(settings.data_dir, settings.project_root / "storage")


if __name__ == "__main__":
    unittest.main()
