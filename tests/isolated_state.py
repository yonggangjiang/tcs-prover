"""Keep the private state of the TCS Prover code under test out of this checkout.

TCS Prover keeps its private state in the folder of the API model store,
`.tcs-prover/` in the checkout unless TCS_PROVER_API_MODELS names another
store (runner.providers.state_directory()): the API connections, the run and
server lock records, the browser session tokens, the last readable copies of
the bundled workflows, the private CLI homes and the installation settings.
Importing the server already saves a workflow copy there, so every test module
that imports TCS Prover code imports this module first (test_suite_hygiene
checks it). The test process then keeps that state in a temporary folder of
its own, removed when it exits, and the runners, workers and other processes
it starts inherit it. A store the shell names is replaced too: tests never
write into real private state. Tests that check the state itself still point
TCS_PROVER_API_MODELS at a folder of their own in setUp.
"""

import atexit
import os
from pathlib import Path
import shutil
import tempfile

# runner.providers.STORE_ENV, named here so that no TCS Prover module is imported first.
STORE_ENV = "TCS_PROVER_API_MODELS"
# The folder this process made, so a child test process (multiprocessing
# "spawn" imports the test modules again) keeps the store its parent gave it.
FOLDER_ENV = "TCS_PROVER_TEST_STATE"

if not os.environ.get(FOLDER_ENV):
    FOLDER = Path(tempfile.mkdtemp(prefix="tcs-prover-test-state-")).resolve()
    atexit.register(shutil.rmtree, FOLDER, ignore_errors=True)
    os.environ[FOLDER_ENV] = str(FOLDER)
    os.environ[STORE_ENV] = str(FOLDER / "api-models.json")
