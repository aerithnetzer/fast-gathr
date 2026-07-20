from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from tagger.services.summary_assembler_generation import (
    load_legacy_keyword_registry,
    validate_occurrence_conservation,
    validate_summary_keywords_output,
)


class SummaryAssemblerValidationTests(SimpleTestCase):
    def test_legacy_registry_is_explicit_and_loadable(self):
        registry = load_legacy_keyword_registry()
        self.assertEqual(registry["authority_status"], "legacy_temporary_authority")
        self.assertEqual(len(registry["records"]), 49)
        self.assertTrue(registry["sha256"])

    def test_summary_keyword_evidence_and_ids_validate(self):
        registry = {"records": [{"ID": "K-0001"}]}
        output = "SUMMARY\nA short summary.\n\nKEYWORDS\nTrade [K-0001]\n  Evidence: exact words"
        result = validate_summary_keywords_output(output, source_body="Some exact words here.", registry=registry)
        self.assertTrue(result["valid"])

    def test_assembler_conservation_rejects_changed_occurrence(self):
        source = "CLAUSE 001\nText\nE: Give [E-1] | Trigger: gave\nQ: Money [Q-1] | Value: 3"
        same = "CLAUSE 001\nText\nP: John [P-1]\nE: Give [E-1] | Trigger: gave\nQ: Money [Q-1] | Value: 3"
        changed = same.replace("Trigger: gave", "Trigger: give")
        self.assertTrue(validate_occurrence_conservation(source, same)["valid"])
        self.assertFalse(validate_occurrence_conservation(source, changed)["valid"])
