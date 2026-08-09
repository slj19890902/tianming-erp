from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from app.web_assets import SelectiveGZipMiddleware, conditional_file_response


ROOT = Path(__file__).resolve().parents[1]


def test_shell_file_is_compressed_and_revalidates_with_304(tmp_path: Path) -> None:
    shell = tmp_path / "index.html"
    shell.write_text("<main>" + ("天明ERP" * 2_000) + "</main>", encoding="utf-8")
    application = FastAPI()
    application.add_middleware(SelectiveGZipMiddleware, minimum_size=1024, compresslevel=6)

    @application.get("/")
    def index(request: Request):
        return conditional_file_response(request, shell)

    with TestClient(application) as client:
        first = client.get("/", headers={"Accept-Encoding": "gzip"})
        second = client.get(
            "/",
            headers={
                "Accept-Encoding": "gzip",
                "If-None-Match": f"W/{first.headers['etag']}, \"other\"",
            },
        )

    assert first.status_code == 200
    assert first.headers["content-encoding"] == "gzip"
    assert first.headers["cache-control"] == "no-cache"
    assert first.headers["vary"] == "Accept-Encoding"
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == first.headers["etag"]
    assert second.headers["cache-control"] == "no-cache"
    assert second.headers["vary"] == "Accept-Encoding"


def test_phase2_shell_routes_enable_compression_and_conditional_delivery() -> None:
    phase2_main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    legacy_main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "SelectiveGZipMiddleware," in phase2_main
    assert "minimum_size=1024" in phase2_main
    assert "compresslevel=6" in phase2_main
    assert "_conditional_file_endpoint(index_path)" in phase2_main
    assert "conditional_file_response(request, index_html_path())" in legacy_main


def test_binary_and_range_responses_bypass_gzip(tmp_path: Path) -> None:
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"%PDF" + (b"x" * 2_044))
    application = FastAPI()
    application.add_middleware(SelectiveGZipMiddleware, minimum_size=100, compresslevel=6)

    @application.get("/drawing.pdf")
    def drawing():
        return FileResponse(pdf, media_type="application/pdf")

    @application.get("/partial")
    def partial():
        return Response(
            content=b"x" * 100,
            status_code=206,
            media_type="text/plain",
            headers={"Content-Range": "bytes 100-199/2048"},
        )

    @application.get("/workbook.xlsx")
    def workbook():
        return Response(
            content=b"xlsx" * 512,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="report.xlsx"'},
        )

    with TestClient(application) as client:
        ranged = client.get(
            "/drawing.pdf",
            headers={"Accept-Encoding": "gzip", "Range": "bytes=100-199"},
        )
        full = client.get("/drawing.pdf", headers={"Accept-Encoding": "gzip"})
        synthetic_206 = client.get("/partial", headers={"Accept-Encoding": "gzip"})
        xlsx = client.get("/workbook.xlsx", headers={"Accept-Encoding": "gzip"})

    assert ranged.status_code == 206
    assert ranged.content == pdf.read_bytes()[100:200]
    assert ranged.headers["content-range"] == "bytes 100-199/2048"
    assert ranged.headers["content-length"] == "100"
    assert "content-encoding" not in ranged.headers

    assert full.status_code == 200
    assert full.content == pdf.read_bytes()
    assert "content-encoding" not in full.headers

    assert synthetic_206.status_code == 206
    assert synthetic_206.content == b"x" * 100
    assert "content-encoding" not in synthetic_206.headers

    assert xlsx.status_code == 200
    assert xlsx.content == b"xlsx" * 512
    assert "content-encoding" not in xlsx.headers
