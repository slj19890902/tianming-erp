from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import Base
from app.core.request_context import (
    bind_current_request,
    get_current_request,
    reset_current_request,
)
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.user import User
from app.services.audit_log import (
    AUDIT_SCHEMA_VERSION,
    MAX_AUDIT_DETAILS_JSON_CHARS,
    REDACTED,
    AuditContractError,
    append_audit_event,
    sanitize_audit_value,
    serialize_audit_details,
)


STRUCTURED_FIELDS = {
    "event_category",
    "result",
    "source",
    "module_code",
    "action_code",
    "actor_user_id_snapshot",
    "operator_name_snapshot",
    "object_ref",
    "customer_id_snapshot",
    "customer_name_snapshot",
    "request_id",
    "batch_id",
    "schema_version",
}


def test_request_context_is_isolated_between_concurrent_tasks() -> None:
    async def worker(path: str) -> str:
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 18179),
                "server": ("127.0.0.1", 18179),
            }
        )
        token = bind_current_request(request)
        try:
            await asyncio.sleep(0)
            current = get_current_request()
            assert current is request
            return current.url.path
        finally:
            reset_current_request(token)

    async def run_workers() -> list[str]:
        return list(
            await asyncio.gather(
                worker("/api/orders/1"),
                worker("/api/incoming/2"),
            )
        )

    assert asyncio.run(run_workers()) == [
        "/api/orders/1",
        "/api/incoming/2",
    ]
    assert get_current_request() is None

STRUCTURED_INDEXES = {
    "ix_operation_logs_category_created_id",
    "ix_operation_logs_operator_created_id",
    "ix_operation_logs_module_action_created_id",
    "ix_operation_logs_object_ref_created_id",
    "ix_operation_logs_customer_created_id",
    "ix_operation_logs_result_source_created_id",
    "ix_operation_logs_request_id",
    "ix_operation_logs_batch_id",
    "ix_operation_logs_schema_version",
}


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/orders",
            "raw_path": b"/api/orders",
            "query_string": b"",
            "headers": [
                (b"user-agent", b"Q1-02 audit test browser"),
                (b"x-request-id", b"client-forged-request-id"),
            ],
            "client": ("192.0.2.25", 43210),
            "server": ("testserver", 80),
        }
    )
    request.state.request_id = "server-generated-request-id"
    return request


def _request_with_user_agent(user_agent: str) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/orders",
            "raw_path": b"/api/orders",
            "query_string": b"",
            "headers": [(b"user-agent", user_agent.encode("ascii"))],
            "client": ("192.0.2.25", 43210),
            "server": ("testserver", 80),
        }
    )
    request.state.request_id = "server-generated-request-id"
    return request


def _user() -> User:
    return User(
        username="audit_operator_a",
        password_hash="test-only",
        role="admin",
        real_name="独立操作员甲",
        display_name="操作员甲",
        is_active=True,
        must_change_password=False,
    )


def test_operation_log_metadata_has_nullable_structured_envelope() -> None:
    table = Base.metadata.tables["operation_logs"]
    assert STRUCTURED_FIELDS <= set(table.c.keys())
    for field_name in STRUCTURED_FIELDS:
        assert table.c[field_name].nullable
        assert table.c[field_name].server_default is None
    assert STRUCTURED_INDEXES <= {index.name for index in table.indexes}


def test_recursive_sanitizer_redacts_secrets_and_omits_binary_content() -> None:
    payload = {
        "safe": "订单 TM-001",
        "password": "123456",
        "nested": {
            "Authorization": "Bearer should-never-survive",
            "comment": "token=abc123 and still safe text",
        },
        "attachment_content": b"private attachment bytes",
        "digest_source": b"binary is summarized, never copied",
    }
    sanitized = sanitize_audit_value(payload)

    assert sanitized["safe"] == "订单 TM-001"
    assert sanitized["password"] == REDACTED
    assert sanitized["nested"]["Authorization"] == REDACTED
    assert REDACTED in sanitized["nested"]["comment"]
    assert sanitized["attachment_content"] == REDACTED
    assert sanitized["digest_source"]["binary_omitted"] is True
    assert sanitized["digest_source"]["size"] == len(
        b"binary is summarized, never copied"
    )
    assert "private attachment bytes" not in json.dumps(
        sanitized,
        ensure_ascii=False,
    )
    for camel_case_key in (
        "passwordHash",
        "authToken",
        "sessionToken",
        "apiKey",
        "rawPdf",
        "fileContent",
        "databaseUrl",
        "connectionString",
        "APIToken",
        "JWTToken",
        "PDFBytes",
        "PDFContent",
    ):
        assert sanitize_audit_value(
            {camel_case_key: f"{camel_case_key}-secret"}
        )[camel_case_key] == REDACTED


def test_sanitizer_only_allows_known_sensitive_boolean_flags() -> None:
    sanitized = sanitize_audit_value(
        {
            "credential_change_required": True,
            "credential_changed": False,
            "credential": True,
            "other_credential_changed": True,
        }
    )

    assert sanitized["credential_change_required"] is True
    assert sanitized["credential_changed"] is False
    assert sanitized["credential"] == REDACTED
    assert sanitized["other_credential_changed"] == REDACTED
    assert sanitize_audit_value(
        {"credential_changed": "true"}
    )["credential_changed"] == REDACTED


def test_sanitizer_never_calls_unknown_object_string_representation() -> None:
    class DangerousRepresentation:
        def __str__(self) -> str:
            raise AssertionError("unknown object __str__ must not be called")

    assert sanitize_audit_value(DangerousRepresentation()) == (
        "<DangerousRepresentation>"
    )


def test_serialized_details_are_deterministic_and_bounded() -> None:
    details = {
        f"field_{index:03d}": ('"\\' * 300) + str(index)
        for index in range(100)
    }
    first = serialize_audit_details(details)
    second = serialize_audit_details(details)

    assert first == second
    assert first is not None
    assert len(first) <= MAX_AUDIT_DETAILS_JSON_CHARS
    parsed = json.loads(first)
    assert parsed["_truncated"] is True
    assert len(parsed["original_sha256"]) == 64


def test_append_audit_event_flushes_without_commit_and_keeps_snapshots(
    isolated_engine,
) -> None:
    Base.metadata.create_all(isolated_engine)
    committed = 0

    with Session(isolated_engine) as session:
        @event.listens_for(session, "after_commit")
        def _count_commit(_session) -> None:
            nonlocal committed
            committed += 1

        actor = _user()
        session.add(actor)
        session.flush()
        row = append_audit_event(
            session,
            request=_request(),
            actor=actor,
            event_category="business",
            result="success",
            source="web",
            module_code="orders",
            action_code="order.create",
            legacy_action="CREATE",
            resource="Order",
            entity_type="order",
            entity_id=91,
            object_ref="TM20260729091",
            customer_id=5,
            customer_name="匿名客户",
            request_id="explicit-id-must-not-win",
            batch_id="batch-q1-02",
            description="创建订单",
            details={
                "before": None,
                "after": {"quantity": 10},
                "password": "must-be-redacted",
            },
        )

        assert row.id is not None
        assert committed == 0
        assert session.in_transaction()
        assert row.schema_version == AUDIT_SCHEMA_VERSION
        assert row.user_id == actor.id
        assert row.actor_user_id_snapshot == actor.id
        assert row.username == actor.username
        assert row.operator_name_snapshot == "操作员甲"
        assert row.customer_id_snapshot == 5
        assert row.customer_name_snapshot == "匿名客户"
        assert row.request_id == "server-generated-request-id"
        assert row.batch_id == "batch-q1-02"
        assert row.ip_address == "192.0.2.25"
        assert row.event_category == "business"
        assert row.result == "success"
        assert row.source == "web"
        assert row.module_code == "orders"
        assert row.action == "CREATE"
        assert row.action_code == "order.create"
        assert json.loads(row.details or "{}")["password"] == REDACTED

        session.rollback()

    with Session(isolated_engine) as verification:
        assert verification.scalar(select(func.count(OperationLog.id))) == 0


def test_user_agent_preserves_legacy_256_character_prefix(
    isolated_engine,
) -> None:
    Base.metadata.create_all(isolated_engine)
    with Session(isolated_engine) as session:
        row = append_audit_event(
            session,
            request=_request_with_user_agent("A" * 300),
            event_category="security",
            result="failed",
            source="web",
            module_code="auth",
            action_code="login.failed",
            legacy_action="LOGIN_FAIL",
            resource="Session",
        )
        assert row.user_agent == "A" * 256
        session.rollback()


@pytest.mark.parametrize(
    "result",
    ["success", "failed", "denied", "partial", "legacy", "no_change"],
)
def test_all_confirmed_audit_results_are_accepted(
    isolated_engine,
    result: str,
) -> None:
    Base.metadata.create_all(isolated_engine)
    with Session(isolated_engine) as session:
        row = append_audit_event(
            session,
            event_category="system" if result == "legacy" else "business",
            result=result,
            source="system" if result == "legacy" else "web",
            module_code="audit",
            action_code="RESULT_CONTRACT",
            resource="Audit",
        )
        assert row.result == result
        session.rollback()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"event_category": "guess"}, "event_category"),
        ({"result": "maybe"}, "result"),
        ({"source": "unknown-client"}, "source"),
        ({"module_code": ""}, "module_code"),
        ({"action_code": ""}, "action_code"),
        ({"resource": ""}, "resource"),
    ],
)
def test_append_audit_event_rejects_invalid_contract_before_insert(
    isolated_engine,
    overrides: dict[str, str],
    message: str,
) -> None:
    Base.metadata.create_all(isolated_engine)
    values = {
        "event_category": "business",
        "result": "success",
        "source": "web",
        "module_code": "orders",
        "action_code": "UPDATE",
        "resource": "Order",
    }
    values.update(overrides)
    with Session(isolated_engine) as session:
        with pytest.raises(AuditContractError, match=message):
            append_audit_event(session, **values)
        assert session.scalar(select(func.count(OperationLog.id))) == 0


def test_audit_flush_failure_is_not_swallowed_and_business_can_rollback(
    isolated_engine,
) -> None:
    Base.metadata.create_all(isolated_engine)
    with Session(isolated_engine) as session:
        customer = Customer(name="Q1-02 rollback customer")
        session.add(customer)

        def _reject_audit(
            current_session: Session,
            _flush_context,
            _instances,
        ) -> None:
            if any(
                isinstance(row, OperationLog)
                for row in current_session.new
            ):
                raise RuntimeError("simulated audit persistence failure")

        event.listen(session, "before_flush", _reject_audit)
        try:
            with pytest.raises(
                RuntimeError,
                match="simulated audit persistence failure",
            ):
                append_audit_event(
                    session,
                    event_category="business",
                    result="success",
                    source="web",
                    module_code="customers",
                    action_code="CREATE",
                    resource="Customer",
                    description="创建客户",
                    details={"name": customer.name},
                )
            session.rollback()
        finally:
            event.remove(session, "before_flush", _reject_audit)

    with Session(isolated_engine) as verification:
        assert verification.scalar(select(func.count(Customer.id))) == 0
        assert verification.scalar(select(func.count(OperationLog.id))) == 0
