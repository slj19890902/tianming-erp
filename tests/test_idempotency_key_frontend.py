from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(
    encoding="utf-8"
)


def _helper_source(html: str) -> str:
    match = re.search(
        r"// BEGIN IDEMPOTENCY_KEY_HELPER(?P<body>.*?)"
        r"// END IDEMPOTENCY_KEY_HELPER",
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return textwrap.dedent(match.group("body")).strip()


def _node() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.fail("Node.js is required for frontend syntax and fallback tests")
    return executable


def test_inventory_actions_share_compatible_idempotency_key_helper() -> None:
    assert _helper_source(INDEX_HTML) == _helper_source(WAREHOUSE_HTML)
    assert "idempotency_key:crypto.randomUUID()" not in INDEX_HTML
    assert "idempotency_key:crypto.randomUUID()" not in WAREHOUSE_HTML
    # New guarded write flows may add more call sites. Keep this assertion
    # focused on the shared helper contract instead of a stale exact count.
    assert INDEX_HTML.count("idempotency_key:createIdempotencyKey()") >= 3
    assert WAREHOUSE_HTML.count("idempotency_key:createIdempotencyKey()") >= 3


def test_idempotency_key_helper_prefers_uuid_then_uses_both_fallbacks() -> None:
    helper = _helper_source(INDEX_HTML)
    script = f"""
const vm = require("vm");
const helper = {json.dumps(helper)};

function contextFor(cryptoValue, dateValue, randomValue) {{
  const context = {{
    crypto: cryptoValue,
    Uint8Array,
    Date: {{ now: () => dateValue }},
    Math: {{ random: () => randomValue }},
  }};
  vm.createContext(context);
  vm.runInContext(helper, context);
  return context;
}}

let getRandomValuesCalled = false;
const primary = contextFor({{
  randomUUID: () => "primary-random-uuid",
  getRandomValues: () => {{ getRandomValuesCalled = true; }},
}}, 1, 0.1);
if (primary.createIdempotencyKey() !== "primary-random-uuid") throw new Error("uuid primary failed");
if (getRandomValuesCalled) throw new Error("getRandomValues should not run when randomUUID works");

const randomValues = contextFor({{
  getRandomValues: (bytes) => {{
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = index;
  }},
}}, 2, 0.2);
if (randomValues.createIdempotencyKey() !== "00010203-0405-4607-8809-0a0b0c0d0e0f") {{
  throw new Error("getRandomValues fallback failed");
}}

const weakFallback = contextFor({{}}, 1700000000000, 0.123456789);
const weakFirst = weakFallback.createIdempotencyKey();
const weakSecond = weakFallback.createIdempotencyKey();
if (!weakFirst.startsWith("idemp-") || weakFirst === weakSecond) {{
  throw new Error("Date/Math fallback failed");
}}
"""
    result = subprocess.run(
        [_node()],
        input=script,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("filename", "html"),
    (("index-inline.js", INDEX_HTML), ("warehouse-inline.js", WAREHOUSE_HTML)),
    ids=("index", "warehouse"),
)
def test_inline_javascript_is_syntactically_valid(
    filename: str,
    html: str,
    tmp_path: Path,
) -> None:
    inline_scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            html,
            flags=re.DOTALL,
        )
        if script.strip()
    ]
    assert len(inline_scripts) == 1
    target = tmp_path / filename
    target.write_text(inline_scripts[0], encoding="utf-8")
    result = subprocess.run(
        [_node(), "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_only_real_inventory_candidates_have_an_editable_reserve_quantity() -> None:
    assert re.search(
        r'v-model(?:\.number)?="[^"]*inventory_deducted_qty',
        INDEX_HTML,
    ) is None
    assert re.search(
        r'v-model(?:\.number)?="[^"]*inventory_deducted_qty',
        WAREHOUSE_HTML,
    ) is None
    assert 'v-model.number="candidate._reserve_qty"' in INDEX_HTML
    assert "candidate.quantity_available" in INDEX_HTML
    assert "finishedInventoryRemainingRequirement" in INDEX_HTML
    assert "const lotId=Number(candidate?.lot_id||0);" in INDEX_HTML
    assert "const expectedVersion=Number(candidate?.version||0);" in INDEX_HTML
    assert "inventory_lot_id:lotId" in INDEX_HTML
    assert "expected_version:expectedVersion" in INDEX_HTML
