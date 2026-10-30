"""The suite keeps TCS Prover's private state out of this checkout (see tests/isolated_state.py)."""

import ast
import os
from pathlib import Path
import unittest

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from runner import providers

ROOT = Path(__file__).resolve().parents[1]
# Importing any of these may write private state (importing the server saves a workflow copy).
TCS_PROVER = {"workflow_runner", "web_ui", "ui", "runner", "retrieval", "transcript", "experiments", "tests"}


def imported_modules(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [f"{node.module}.{alias.name}" for alias in node.names]
    return []


class PrivateStateIsolationTests(unittest.TestCase):
    def test_the_private_state_of_the_suite_is_outside_the_checkout(self):
        state = providers.state_directory().resolve()
        self.assertEqual(state, Path(os.environ[isolated_state.FOLDER_ENV]).resolve())
        self.assertFalse(state.is_relative_to(ROOT), state)
        for path in providers.private_paths():
            self.assertFalse(path.is_relative_to(ROOT), path)

    def test_every_test_module_isolates_the_state_before_importing_tcs_prover(self):
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(module=path.name):
                names = [name for node in ast.parse(path.read_text(encoding="utf-8")).body
                         for name in imported_modules(node)]
                ours = [name for name in names if name.split(".")[0] in TCS_PROVER or name.startswith("test_")]
                if ours:  # Browser-script tests import no TCS Prover code.
                    self.assertEqual(ours[0], "tests.isolated_state",
                                     f"{path.name} imports {ours[0]} before tests.isolated_state")


if __name__ == "__main__":
    unittest.main()
