import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from tagger.integrations.contracts import IntegrationNotConfigured
from tagger.integrations.local_artifacts import LocalArtifactStore
from tagger.integrations.stubs import (
    UnconfiguredAwsChatbotProvider,
    UnconfiguredPostgresRepository,
    UnconfiguredS3ArtifactStore,
)


class IntegrationContractTests(SimpleTestCase):
    def test_local_artifact_adapter_checks_hash_and_path(self):
        content = b"export payload"
        digest = hashlib.sha256(content).hexdigest()
        with TemporaryDirectory() as directory:
            store = LocalArtifactStore(Path(directory))
            reference = store.put(
                key="doc/export.json", content=content,
                content_type="application/json", sha256=digest,
            )
            self.assertEqual(store.get(reference), content)
            with self.assertRaises(ValueError):
                store.put(key="../escape", content=content, content_type="text/plain", sha256=digest)

    def test_external_stubs_fail_explicitly(self):
        with self.assertRaises(IntegrationNotConfigured):
            UnconfiguredAwsChatbotProvider().submit({})
        with self.assertRaises(IntegrationNotConfigured):
            UnconfiguredS3ArtifactStore().put()
        with self.assertRaises(IntegrationNotConfigured):
            UnconfiguredPostgresRepository().health()

    def test_machine_readable_contract_files(self):
        root = Path(__file__).resolve().parents[2]
        schema = json.loads((root / "contracts" / "emtl_workflow_export_v1.schema.json").read_text())
        ddl = (root / "contracts" / "postgresql_schema_v1.sql").read_text()
        self.assertEqual(schema["properties"]["schema_version"]["const"], "emtl-workflow-export-v1")
        self.assertIn("CREATE TABLE emtl_export_package", ddl)
        self.assertIn("idempotency_key text NOT NULL UNIQUE", ddl)
