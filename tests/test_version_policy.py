import pathlib
import tomllib
import unittest

import parley
import parley_version


class VersionPolicyTests(unittest.TestCase):
    def test_package_uses_canonical_version_module(self):
        self.assertEqual(parley.__version__, parley_version.__version__)

    def test_pyproject_reads_canonical_version_module(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)

        self.assertIn("version", config["project"]["dynamic"])
        self.assertEqual(
            config["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "parley_version.__version__",
        )


if __name__ == "__main__":
    unittest.main()
