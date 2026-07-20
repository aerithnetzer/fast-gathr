from pathlib import Path

from django.test import SimpleTestCase

from emtl_site.database_config import database_config_from_env


class DatabaseConfigTests(SimpleTestCase):
    def test_default_remains_local_sqlite(self):
        config = database_config_from_env({}, base_dir=Path("/project"))
        self.assertEqual(config["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(config["NAME"], Path("/project/db.sqlite3"))

    def test_postgresql_url_requires_no_source_change(self):
        config = database_config_from_env(
            {"DATABASE_URL": "postgresql://user:pass@db.example:5433/emtl?sslmode=require"},
            base_dir=Path("/project"),
        )
        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["NAME"], "emtl")
        self.assertEqual(config["HOST"], "db.example")
        self.assertEqual(config["PORT"], "5433")
        self.assertEqual(config["OPTIONS"], {"sslmode": "require"})
