"""Static public-release checks for environment-only GraphRAG credentials."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicSecurityConfigurationTests(unittest.TestCase):
    def test_graphrag_modules_have_no_embedded_credential_defaults(self) -> None:
        legacy_development_value = "".join(("123", "456", "789"))
        for relative_path in (
            "src/graphrag_client.py",
            "src/graphrag_proxy.py",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn(legacy_development_value, source, relative_path)
            self.assertIn('os.getenv("NEO4J_USERNAME", "")', source, relative_path)
            self.assertIn('os.getenv("NEO4J_PASSWORD", "")', source, relative_path)
            self.assertIn("_neo4j_configured", source, relative_path)

    def test_no_google_api_key_literal_in_python_sources(self) -> None:
        pattern = re.compile(r"AIza[0-9A-Za-z_-]{20,}")
        for path in ROOT.rglob("*.py"):
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path)


if __name__ == "__main__":
    unittest.main()
