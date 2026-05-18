from __future__ import annotations

import asyncio
import shutil
import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_toolkit_mcp.server import create_server, get_tool_definitions
from agent_toolkit_mcp.tools import BinaryResolver


class ServerSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        fake_binary = shutil.which("python3")
        assert fake_binary, "python3 is required for the test environment"
        self.environ = {
            "AGENT_TOOLKIT_SYSTEM_JANITOR_BIN": fake_binary,
            "AGENT_TOOLKIT_SYSTEM_UPDATER_BIN": fake_binary,
            "HOME": str(Path.home()),
            "PATH": str(Path(fake_binary).parent),
        }

    def test_imports_work(self) -> None:
        self.assertTrue(callable(create_server))
        self.assertTrue(callable(get_tool_definitions))

    def test_env_binary_resolution(self) -> None:
        resolver = BinaryResolver(self.environ)
        janitor = resolver.resolve("janitor")
        updater = resolver.resolve("updater")
        self.assertIsNotNone(janitor.path)
        self.assertIsNotNone(updater.path)
        self.assertEqual(janitor.source, "$AGENT_TOOLKIT_SYSTEM_JANITOR_BIN")
        self.assertEqual(updater.source, "$AGENT_TOOLKIT_SYSTEM_UPDATER_BIN")

    def test_tools_are_advertised_with_descriptions(self) -> None:
        resolver = BinaryResolver(self.environ)
        definitions = get_tool_definitions(resolver)
        self.assertGreaterEqual(len(definitions), 8)
        for definition in definitions:
            self.assertTrue(definition.description.strip())

        server = create_server(resolver)
        advertised = asyncio.run(server.list_tools())
        self.assertGreaterEqual(len(advertised), 8)
        for tool in advertised:
            self.assertTrue((tool.description or "").strip())


if __name__ == "__main__":
    unittest.main()
