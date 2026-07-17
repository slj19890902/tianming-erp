from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
VUE_FILES = [
    ROOT / "tm_frontend/src/views/OrderView.vue",
    ROOT / "tm_frontend/src/views/DeliveryView.vue",
    ROOT / "tm_frontend/src/views/StatementView.vue",
    ROOT / "tm_frontend/src/views/ExcelRequisitionView.vue",
]


def test_static_pages_use_the_shared_time_contract() -> None:
    pages = [
        STATIC / "index.html",
        STATIC / "warehouse.html",
        STATIC / "incoming.html",
        STATIC / "delivery-print.html",
        STATIC / "requisition-print.html",
        STATIC / "customers.html",
    ]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert '/static/assets/time-utils.js' in text, page
        assert 'toISOString().slice(0, 10)' not in text, page
        assert '${value}Z' not in text, page
        assert '${data.created_at}Z' not in text, page
    assert 'new Date(value).toLocaleString' not in (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'new Date(x.created_at).toLocaleString' not in (STATIC / "warehouse.html").read_text(encoding="utf-8")


def test_requisition_print_keeps_business_date_out_of_browser_timezone() -> None:
    page = (STATIC / "requisition-print.html").read_text(encoding="utf-8")
    assert "TmTime.formatBusinessDate(value)" in page
    assert "T00:00:00" not in page

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = (
        "const fs=require('fs');"
        "eval(fs.readFileSync(process.argv[1],'utf8'));"
        "const page=fs.readFileSync(process.argv[2],'utf8');"
        "const start=page.indexOf('const shortDate = value => {');"
        "const end=page.indexOf('\\n    };',start)+7;"
        "if(start<0||end<7)process.exit(2);"
        "eval(page.slice(start,end)+';globalThis.__shortDate=shortDate;');"
        "if(globalThis.__shortDate('2026-07-17')!=='26.7.17')process.exit(3);"
    )
    for timezone_name in ("America/Los_Angeles", "Pacific/Kiritimati"):
        environment = os.environ.copy()
        environment["TZ"] = timezone_name
        result = subprocess.run(
            [
                node,
                "-e",
                script,
                str(STATIC / "assets/time-utils.js"),
                str(STATIC / "requisition-print.html"),
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, (
            timezone_name + ": " + result.stdout + result.stderr
        )


def test_index_converts_utc_instants_and_keeps_calendar_days() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "h.effective_date || formatBeijingDate(h.created_at)" in index
    assert "formatBeijingDate(row.created_at)" in index
    assert "TmTime.formatBeijingDate(value).split" in index
    assert "(h.created_at||'').slice(0,10)" not in index
    assert "row.created_at.slice(0,10)" not in index
    assert "formatBeijingDateTime(value).slice(0, 10)" not in index


def test_backup_timestamps_are_labeled_and_rendered_as_beijing_time() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert index.count("创建时间（北京时间）") == 3
    assert "{{ formatDateTime(row.created_at) }}" in index
    assert index.count("{{ formatDateTime(f.created_at) }}") == 2
    assert "{{ row.created_at }}" not in index
    assert "{{ f.created_at }}" not in index


def test_static_time_utility_has_a_strict_contract() -> None:
    utility = (STATIC / "assets/time-utils.js").read_text(encoding="utf-8")
    for name in ("formatBeijingDateTime", "formatBeijingDate", "beijingToday", "addCalendarDays", "formatBusinessDate"):
        assert name in utility
    assert 'Asia/Shanghai' in utility
    assert 'Z|[+-]\\d{2}:\\d{2}' in utility


def test_vue_views_use_business_date_helpers() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in VUE_FILES)
    assert "from '../utils/time'" in combined
    assert "toISOString().slice(0, 10)" not in combined
    assert "new Date().toISOString()" not in combined
    assert (ROOT / "tm_frontend/src/utils/time.ts").exists()


def test_shared_js_contract_and_timezone_boundary() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    utility = STATIC / "assets/time-utils.js"
    syntax = subprocess.run([node, "--check", str(utility)], cwd=ROOT, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    script = (
        "const fs=require('fs');"
        "eval(fs.readFileSync(process.argv[1],'utf8'));"
        "const t=globalThis.TmTime;"
        "if(t.formatBeijingDateTime('2026-07-16T16:00:00Z')!=='2026-07-17 00:00:00')process.exit(1);"
        "if(t.formatBeijingDateTime('2026-07-16T23:00:00-01:00')!=='2026-07-17 08:00:00')process.exit(2);"
        "if(t.formatBeijingDate('2026-07-16T16:00:00Z')!=='2026-07-17')process.exit(3);"
        "if(t.formatBeijingDate('2026-07-16T23:00:00-01:00')!=='2026-07-17')process.exit(4);"
        "if(t.beijingToday(new Date('2026-07-16T16:00:00Z'))!=='2026-07-17')process.exit(5);"
        "if(t.addCalendarDays('2026-07-17',1)!=='2026-07-18')process.exit(6);"
        "if(t.formatBusinessDate('2026-07-17')!=='2026-07-17')process.exit(7);"
        "let rejected=false;try{t.formatBeijingDateTime('2026-07-17T00:00:00')}catch(_){rejected=true}"
        "if(!rejected)process.exit(8);"
        "rejected=false;try{t.formatBeijingDate('2026-07-17')}catch(_){rejected=true}"
        "if(!rejected)process.exit(9);"
    )
    result = subprocess.run([node, "-e", script, str(utility)], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_typescript_time_contract_is_syntax_checkable() -> None:
    local_vue_tsc = ROOT / "tm_frontend/node_modules/.bin/vue-tsc.cmd"
    if not local_vue_tsc.exists():
        local_vue_tsc = ROOT / "tm_frontend/node_modules/.bin/vue-tsc"
    if not local_vue_tsc.exists():
        pytest.skip("local vue-tsc dependency is not installed")
    result = subprocess.run(
        [str(local_vue_tsc), "--noEmit"],
        cwd=ROOT / "tm_frontend",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
