from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import sleep

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from app.core.database import create_sqlite_engine
from app.middleware.performance import (
    PerformanceObservabilityMiddleware,
    slow_request_threshold_ms,
)
from app.web_assets import SelectiveGZipMiddleware, conditional_file_response


def _build_application(database_path: Path) -> tuple[FastAPI, object]:
    engine = create_sqlite_engine(database_path)
    application = FastAPI()
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=0,
    )

    @application.get("/api/query/{count}")
    def query(count: int) -> dict[str, int]:
        with engine.connect() as connection:
            for _ in range(count):
                connection.exec_driver_sql("SELECT 1").scalar_one()
        return {"count": count}

    @application.get("/api/sensitive-query")
    def sensitive_query() -> dict[str, bool]:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "SELECT ?",
                ("DO_NOT_LOG_PARAMETER_VALUE",),
            ).scalar_one()
        return {"ok": True}

    @application.get("/api/query-error")
    def query_error() -> None:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "SELECT ? FROM table_that_must_not_exist",
                ("DO_NOT_LOG_ERROR_PARAMETER",),
            ).all()

    @application.get("/api/empty")
    def empty() -> Response:
        return Response(status_code=204)

    @application.get("/api/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(iter((b"first", b"second")))

    @application.get("/not-api")
    def not_api() -> dict[str, bool]:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1").scalar_one()
        return {"ok": True}

    return application, engine


def _record_messages(caplog, path: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "erp.performance" and f"path={path} " in record.getMessage()
    ]


def _record_message(caplog, path: str) -> str:
    matches = _record_messages(caplog, path)
    assert len(matches) == 1
    return matches[0]


def _field(message: str, field_name: str) -> str:
    match = re.search(rf"(?:^| ){re.escape(field_name)}=([^ ]+)", message)
    assert match is not None, message
    return match.group(1)


def test_slow_api_log_contains_request_scoped_database_and_size_metrics(
    tmp_path: Path,
    caplog,
) -> None:
    application, engine = _build_application(tmp_path / "metrics.sqlite3")
    try:
        with caplog.at_level(logging.WARNING, logger="erp.performance"):
            with TestClient(application) as client:
                response = client.get("/api/query/3?ignored=secret-query-value")

        assert response.status_code == 200
        message = _record_message(caplog, "/api/query/{count}")
        assert message.startswith(
            "slow_api method=GET path=/api/query/{count} status=200"
        )
        assert _field(message, "query_count") == "3"
        assert float(_field(message, "db_duration_ms")) >= 0
        assert int(_field(message, "response_bytes")) == len(response.content)
        assert _field(message, "request_id") == response.headers["x-request-id"]
        assert _field(message, "db_scope") == "sqlalchemy"
        assert response.headers["server-timing"].startswith("app;dur=")
        assert ", db;dur=" in response.headers["server-timing"]
        assert "secret-query-value" not in caplog.text
        assert "/api/query/3" not in caplog.text
    finally:
        engine.dispose()


def test_sql_text_parameters_and_response_body_are_never_logged(
    tmp_path: Path,
    caplog,
) -> None:
    application, engine = _build_application(tmp_path / "sensitive.sqlite3")
    try:
        with caplog.at_level(logging.WARNING, logger="erp.performance"):
            with TestClient(application) as client:
                response = client.get("/api/sensitive-query")

        assert response.json() == {"ok": True}
        message = _record_message(caplog, "/api/sensitive-query")
        assert _field(message, "query_count") == "1"
        assert "SELECT" not in caplog.text
        assert "DO_NOT_LOG_PARAMETER_VALUE" not in caplog.text
        assert '"ok"' not in caplog.text
    finally:
        engine.dispose()


def test_sequential_and_concurrent_requests_do_not_share_query_counts(
    tmp_path: Path,
    caplog,
) -> None:
    application, engine = _build_application(tmp_path / "concurrent.sqlite3")

    def call(count: int) -> int:
        with TestClient(application) as client:
            return client.get(f"/api/query/{count}").status_code

    try:
        with caplog.at_level(logging.WARNING, logger="erp.performance"):
            assert call(1) == 200
            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(call, (2, 5)))

        assert statuses == [200, 200]
        messages = _record_messages(caplog, "/api/query/{count}")
        assert sorted(int(_field(message, "query_count")) for message in messages) == [
            1,
            2,
            5,
        ]
    finally:
        engine.dispose()


def test_database_error_is_safe_api_error_and_next_request_starts_clean(
    tmp_path: Path,
    caplog,
) -> None:
    application, engine = _build_application(tmp_path / "error.sqlite3")
    try:
        with caplog.at_level(logging.WARNING, logger="erp.performance"):
            with TestClient(application, raise_server_exceptions=False) as client:
                failed = client.get("/api/query-error")
                recovered = client.get("/api/query/1")

        assert failed.status_code == 500
        assert recovered.status_code == 200
        failed_message = _record_message(caplog, "/api/query-error")
        assert failed_message.startswith(
            "api_error method=GET path=/api/query-error status=500"
        )
        assert _field(failed_message, "query_count") == "1"
        assert _field(failed_message, "response_bytes") == "0"
        assert _field(failed_message, "error_type") == "OperationalError"
        assert "table_that_must_not_exist" not in caplog.text
        assert "DO_NOT_LOG_ERROR_PARAMETER" not in caplog.text
        recovered_messages = _record_messages(caplog, "/api/query/{count}")
        assert len(recovered_messages) == 1
        assert _field(recovered_messages[0], "query_count") == "1"
    finally:
        engine.dispose()


def test_empty_streaming_and_non_api_paths_use_safe_size_and_log_scope(
    tmp_path: Path,
    caplog,
) -> None:
    application, engine = _build_application(tmp_path / "responses.sqlite3")
    try:
        with caplog.at_level(logging.WARNING, logger="erp.performance"):
            with TestClient(application) as client:
                empty = client.get("/api/empty")
                streamed = client.get("/api/stream")
                not_api = client.get("/not-api")

        assert empty.status_code == 204
        assert streamed.content == b"firstsecond"
        assert not_api.status_code == 200
        assert _field(_record_message(caplog, "/api/empty"), "response_bytes") == "0"
        assert _field(_record_message(caplog, "/api/stream"), "response_bytes") == "11"
        assert "path=/not-api " not in caplog.text
    finally:
        engine.dispose()


def test_gzip_304_and_range_bytes_are_counted_without_rewriting_responses(
    tmp_path: Path,
    caplog,
) -> None:
    shell = tmp_path / "shell.html"
    shell.write_text("<main>天明ERP</main>", encoding="utf-8")
    drawing = tmp_path / "drawing.pdf"
    drawing.write_bytes(b"%PDF" + (b"x" * 2_044))

    application = FastAPI()
    application.add_middleware(
        SelectiveGZipMiddleware,
        minimum_size=100,
        compresslevel=6,
    )
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=0,
    )

    @application.get("/api/large")
    def large() -> Response:
        return Response(content=b"x" * 5_000, media_type="application/json")

    @application.get("/api/large-stream")
    def large_stream() -> StreamingResponse:
        return StreamingResponse(iter((b"x" * 2_500, b"x" * 2_500)))

    @application.get("/api/shell")
    def shell_file(request: Request):
        return conditional_file_response(request, shell)

    @application.get("/api/drawing.pdf")
    def drawing_file() -> FileResponse:
        return FileResponse(drawing, media_type="application/pdf")

    try:
        with caplog.at_level(logging.WARNING, logger="erp.performance"):
            with TestClient(application) as client:
                large_response = client.get(
                    "/api/large",
                    headers={"Accept-Encoding": "gzip"},
                )
                streamed_response = client.get(
                    "/api/large-stream",
                    headers={"Accept-Encoding": "gzip"},
                )
                shell_first = client.get("/api/shell")
                shell_cached = client.get(
                    "/api/shell",
                    headers={"If-None-Match": shell_first.headers["etag"]},
                )
                ranged = client.get(
                    "/api/drawing.pdf",
                    headers={"Accept-Encoding": "gzip", "Range": "bytes=100-199"},
                )

        assert large_response.headers["content-encoding"] == "gzip"
        large_message = _record_message(caplog, "/api/large")
        assert int(_field(large_message, "response_bytes")) == int(
            large_response.headers["content-length"]
        )

        assert streamed_response.headers["content-encoding"] == "gzip"
        stream_message = _record_message(caplog, "/api/large-stream")
        assert 0 < int(_field(stream_message, "response_bytes")) < len(
            streamed_response.content
        )

        shell_messages = _record_messages(caplog, "/api/shell")
        assert sorted(int(_field(message, "status")) for message in shell_messages) == [
            200,
            304,
        ]
        cached_message = next(
            message for message in shell_messages if _field(message, "status") == "304"
        )
        assert _field(cached_message, "response_bytes") == "0"
        assert shell_cached.content == b""

        assert ranged.status_code == 206
        assert ranged.content == drawing.read_bytes()[100:200]
        assert "content-encoding" not in ranged.headers
        range_message = _record_message(caplog, "/api/drawing.pdf")
        assert _field(range_message, "response_bytes") == "100"
    finally:
        pass


def test_fast_api_is_not_logged_but_keeps_request_and_timing_headers(
    caplog,
) -> None:
    application = FastAPI()
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=999_999,
    )

    @application.get("/api/fast")
    def fast() -> dict[str, bool]:
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="erp.performance"):
        with TestClient(application) as client:
            response = client.get(
                "/api/fast",
                headers={"X-Request-ID": "client-id-must-not-win"},
            )

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "client-id-must-not-win"
    assert response.headers["server-timing"].startswith("app;dur=")
    assert ", db;dur=0.0" in response.headers["server-timing"]
    assert "path=/api/fast " not in caplog.text


def test_uat_can_capture_200_to_499ms_without_changing_factory_default(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setenv("ERP_SLOW_REQUEST_MS", "200")
    assert slow_request_threshold_ms() == 200

    application = FastAPI()
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=slow_request_threshold_ms(),
    )

    @application.get("/api/uat-250ms")
    def uat_250ms() -> dict[str, bool]:
        sleep(0.25)
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="erp.performance"):
        with TestClient(application) as client:
            response = client.get("/api/uat-250ms")

    assert response.status_code == 200
    message = _record_message(caplog, "/api/uat-250ms")
    assert message.startswith(
        "slow_api method=GET path=/api/uat-250ms status=200"
    )
    assert float(_field(message, "duration_ms")) >= 200

    monkeypatch.delenv("ERP_SLOW_REQUEST_MS")
    assert slow_request_threshold_ms() == 500
