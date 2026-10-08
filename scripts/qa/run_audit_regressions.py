"""Targeted audit regressions, using a verified Python 3.12+ runtime.

This runner never starts a formal service or opens a browser. Copied-database
checks must separately use scripts/uat/run_task.py and an isolated source.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SUITES = {
    "business": [
        "tests/test_quotation_reliability.py", "tests/test_quotation_frontend_reliability.py",
        "tests/test_quotation_reliability_migration.py", "tests/test_quotations.py",
        "tests/test_n028_quotation_permission_boundaries.py", "tests/test_n028_sensitive_business_permissions.py",
        "tests/test_p1_03_customer_quote_preferences.py", "tests/test_p1_09c_97_quotation_conversion_guard.py",
        "tests/test_approval_field_contract.py", "tests/test_scoped_business_approvals.py", "tests/test_completion_input_audit.py",
        "tests/test_stock_preparation_query_budget.py",
    ],
    "assistant": [
        "tests/desktop_assistant/test_quotation_writer_contract.py",
        "tests/desktop_assistant/test_cross_revision.py", "tests/desktop_assistant/test_build.py",
        "tests/desktop_assistant/test_schema_contract.py", "tests/desktop_assistant/test_migration.py",
        "tests/desktop_assistant/test_retention.py", "tests/test_order_inventory_reader_contract.py",
    ],
}
FRONTEND = ["factory_twin/frontend/tests/workspaceUi.test.mjs", "factory_twin/frontend/tests/stockPreparation.test.mjs"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["all", "business", "assistant", "frontend"], default="all")
    parser.add_argument("--node", help="Absolute Node executable; required when Node is not on PATH")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Use Python 3.12 or newer; do not weaken junction/cleanup checks for Python 3.10")
    selected = list(SUITES) if args.suite == "all" else [args.suite]
    if any(name in SUITES for name in selected):
        missing = [name for name in ("pytest", "psutil") if importlib.util.find_spec(name) is None]
        if missing:
            parser.error("Test runtime is missing: " + ", ".join(missing))
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    commands = [[sys.executable, "-m", "pytest", "-q", *SUITES[name]] for name in selected if name in SUITES]
    if args.suite in ("all", "frontend"):
        node = args.node or shutil.which("node")
        if not node or not Path(node).is_file():
            parser.error("Provide --node with an existing Node executable")
        commands.append([node, "--test", *FRONTEND])
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, env=env)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
