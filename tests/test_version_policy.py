import pathlib
import tomllib
import unittest

import parley_version


class VersionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(__file__).resolve().parents[1]

    def test_canonical_version_value(self):
        self.assertEqual(parley_version.__version__, "1.1.0")

    def test_pyproject_reads_canonical_version_module(self):
        with (self.root / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)

        self.assertIn("version", config["project"]["dynamic"])
        self.assertEqual(
            config["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "parley_version.__version__",
        )
        self.assertIn("parley_version", config["tool"]["setuptools"]["py-modules"])

    def test_runtime_surfaces_use_canonical_version(self):
        package_init = (self.root / "parley" / "__init__.py").read_text(encoding="utf-8")
        mcp_server = (self.root / "parley_mcp.py").read_text(encoding="utf-8")

        self.assertIn("from parley_version import __version__", package_init)
        self.assertIn("from parley_version import __version__", mcp_server)
        self.assertIn('"version": __version__', mcp_server)
        self.assertNotIn('__version__ = "1.1.0"', package_init)
        self.assertNotIn('"version": "1.1.0"', mcp_server)


if __name__ == "__main__":
    unittest.main()
