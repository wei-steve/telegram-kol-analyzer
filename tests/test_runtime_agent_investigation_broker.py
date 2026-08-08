from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeAgentInvestigationAudit
from telegram_kol_research.runtime_agent_investigation_broker import (
    BROAD_READ_ONLY_EVIDENCE_KINDS,
    InvestigationBroker,
    InvestigationDenied,
    InvestigationRequest,
    NetworkReadPolicy,
    ReadOnlyFileEvidenceReader,
    SqliteReadOnlyEvidenceStore,
    build_sqlalchemy_audit_recorder,
)


NOW = datetime(2026, 8, 9, tzinfo=UTC)


@pytest.mark.parametrize("evidence_kind", sorted(BROAD_READ_ONLY_EVIDENCE_KINDS))
def test_broker_allows_every_closed_read_category_and_audits_it(evidence_kind):
    audit_rows = []

    def provider(request):
        return {
            "data": {"kind": request.evidence_kind, "incident_id": request.incident_id},
            "evidence_refs": [f"{request.evidence_kind}:17"],
        }

    broker = InvestigationBroker(
        providers={evidence_kind: provider},
        incident_exists=lambda incident_id: incident_id == 17,
        audit_recorder=audit_rows.append,
        clock=lambda: NOW,
    )
    result = broker.execute(
        InvestigationRequest(incident_id=17, evidence_kind=evidence_kind)
    )

    assert result["data"]["incident_id"] == 17
    assert result["evidence_refs"] == [f"{evidence_kind}:17"]
    assert len(audit_rows) == 1
    assert audit_rows[0].incident_id == 17
    assert audit_rows[0].evidence_kind == evidence_kind
    assert audit_rows[0].result_status == "allowed"
    assert audit_rows[0].denial_code is None
    assert audit_rows[0].result_bytes > 0
    assert len(audit_rows[0].arguments_fingerprint) == 64


@pytest.mark.parametrize(
    ("investigation_request", "denial_code"),
    [
        (InvestigationRequest(incident_id=18, evidence_kind="message_evidence"), "incident_not_found"),
        (InvestigationRequest(incident_id=17, evidence_kind="run_shell"), "evidence_kind_denied"),
        (InvestigationRequest(incident_id=17, evidence_kind="database_projection", query="DELETE FROM raw_messages"), "query_denied"),
        (InvestigationRequest(incident_id=17, evidence_kind="deployed_code", object_ids=("../config/telegram.env",)), "sensitive_argument"),
        (InvestigationRequest(incident_id=17, evidence_kind="message_evidence", maximum_bytes=64), "bounds_invalid"),
    ],
)
def test_broker_refuses_unsafe_requests_and_audits_the_denial(
    investigation_request, denial_code
):
    audit_rows = []
    broker = InvestigationBroker(
        providers={},
        incident_exists=lambda incident_id: incident_id == 17,
        audit_recorder=audit_rows.append,
        clock=lambda: NOW,
    )

    with pytest.raises(InvestigationDenied, match=denial_code):
        broker.execute(investigation_request)

    assert len(audit_rows) == 1
    assert audit_rows[0].result_status == "denied"
    assert audit_rows[0].denial_code == denial_code
    assert audit_rows[0].evidence_reference is None


def test_broker_refuses_unbounded_time_ids_and_sensitive_provider_output():
    audit_rows = []
    broker = InvestigationBroker(
        providers={
            "message_evidence": lambda request: {
                "data": {"api_token": "must-not-leak"},
                "evidence_refs": ["raw-message:17"],
            }
        },
        incident_exists=lambda incident_id: True,
        audit_recorder=audit_rows.append,
        clock=lambda: NOW,
    )

    bad_requests = (
        InvestigationRequest(
            incident_id=17,
            evidence_kind="message_evidence",
            object_ids=tuple(str(i) for i in range(33)),
        ),
        InvestigationRequest(
            incident_id=17,
            evidence_kind="message_evidence",
            since=NOW - timedelta(days=32),
            until=NOW,
        ),
    )
    for request in bad_requests:
        with pytest.raises(InvestigationDenied, match="bounds_invalid"):
            broker.execute(request)

    with pytest.raises(InvestigationDenied, match="sensitive_result"):
        broker.execute(
            InvestigationRequest(incident_id=17, evidence_kind="message_evidence")
        )
    assert [row.denial_code for row in audit_rows] == [
        "bounds_invalid",
        "bounds_invalid",
        "sensitive_result",
    ]


def test_sensitive_invalid_kind_and_evidence_reference_are_never_audited_verbatim():
    audit_rows = []
    broker = InvestigationBroker(
        providers={
            "message_evidence": lambda request: {
                "data": {"status": "failed"},
                "evidence_refs": ["api-key:must-not-persist"],
            }
        },
        incident_exists=lambda incident_id: True,
        audit_recorder=audit_rows.append,
        clock=lambda: NOW,
    )

    with pytest.raises(InvestigationDenied, match="evidence_kind_denied"):
        broker.execute(
            InvestigationRequest(incident_id=17, evidence_kind="api_key=secret")
        )
    with pytest.raises(InvestigationDenied, match="sensitive_result"):
        broker.execute(
            InvestigationRequest(incident_id=17, evidence_kind="message_evidence")
        )

    assert audit_rows[0].evidence_kind == "invalid"
    assert audit_rows[0].evidence_reference is None
    assert audit_rows[1].evidence_reference is None
    assert "must-not-persist" not in repr(audit_rows)


def test_sqlite_evidence_store_is_uri_read_only_and_query_only(tmp_path):
    database = tmp_path / "facts.db"
    connection = sqlite3.connect(database)
    connection.execute("create table facts (id integer primary key, value text)")
    connection.execute("insert into facts(value) values ('ok')")
    connection.commit()
    connection.close()

    store = SqliteReadOnlyEvidenceStore(database)
    assert store.select("select id, value from facts", maximum_rows=10) == [
        {"id": 1, "value": "ok"}
    ]
    for statement in (
        "insert into facts(value) values ('bad')",
        "update facts set value='bad'",
        "delete from facts",
        "drop table facts",
        "attach database ':memory:' as extra",
        "pragma writable_schema=on",
    ):
        with pytest.raises(InvestigationDenied, match="database_write_denied"):
            store.select(statement, maximum_rows=10)

    verify = sqlite3.connect(database)
    assert verify.execute("select value from facts").fetchall() == [("ok",)]
    verify.close()


def test_sqlite_read_only_store_sees_committed_wal_rows_without_mutating(tmp_path):
    database = tmp_path / "live-wal.db"
    writer = sqlite3.connect(database)
    writer.execute("pragma journal_mode=wal")
    writer.execute("pragma wal_autocheckpoint=0")
    writer.execute("create table facts (id integer primary key, value text)")
    writer.commit()
    writer.execute("insert into facts(value) values ('latest')")
    writer.commit()

    store = SqliteReadOnlyEvidenceStore(database)
    assert store.select("select value from facts", maximum_rows=10) == [
        {"value": "latest"}
    ]
    writer.close()


def test_file_reader_is_bounded_read_only_and_refuses_credentials(tmp_path):
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    source_root.mkdir()
    scratch_root.mkdir()
    code = source_root / "worker.py"
    code.write_text("print('safe')\n", encoding="utf-8")
    credential = source_root / "telegram.env"
    credential.write_text("BOT_TOKEN=secret\n", encoding="utf-8")
    reader = ReadOnlyFileEvidenceReader(
        allowed_roots=(source_root,),
        scratch_root=scratch_root,
        maximum_bytes=1024,
    )

    assert reader.read_text(code) == "print('safe')\n"
    with pytest.raises(InvestigationDenied, match="credential_path_denied"):
        reader.read_text(credential)
    with pytest.raises(InvestigationDenied, match="path_denied"):
        reader.read_text(tmp_path / "outside.txt")
    assert not hasattr(reader, "write_text")
    assert not hasattr(reader, "delete")


def test_network_policy_allows_only_reviewed_get_endpoints_without_proxying():
    policy = NetworkReadPolicy(
        allowed_hosts=frozenset({"api.deepcoin.com", "api.telegram.org"}),
        deepcoin_read_paths=frozenset({"/deepcoin/market/instruments", "/deepcoin/account/balances"}),
    )

    policy.authorize("GET", "https://api.deepcoin.com/deepcoin/market/instruments", headers={})
    for method, url, headers, denial in (
        ("POST", "https://api.deepcoin.com/deepcoin/trade/order", {}, "method_denied"),
        ("GET", "https://evil.example/data", {}, "host_denied"),
        ("GET", "http://api.deepcoin.com/deepcoin/account/balances", {}, "scheme_denied"),
        ("GET", "https://api.deepcoin.com:444/deepcoin/account/balances", {}, "port_denied"),
        ("GET", "https://api.deepcoin.com/deepcoin/trade/cancel", {}, "exchange_endpoint_denied"),
        ("GET", "https://api.telegram.org/botredacted/getChat", {}, "telegram_direct_access_denied"),
        ("GET", "https://api.telegram.org/botredacted/sendMessage", {}, "telegram_direct_access_denied"),
    ):
        with pytest.raises(InvestigationDenied, match=denial):
            policy.authorize(method, url, headers=headers)


def test_broker_persists_one_bounded_audit_row_per_request(tmp_path):
    session_factory = create_session_factory(tmp_path / "audit.db")
    broker = InvestigationBroker(
        providers={
            "prior_incidents": lambda request: {
                "data": {"count": 0},
                "evidence_refs": [f"incident:{request.incident_id}"],
            }
        },
        incident_exists=lambda incident_id: incident_id == 17,
        audit_recorder=build_sqlalchemy_audit_recorder(session_factory),
        clock=lambda: NOW,
    )

    broker.execute(
        InvestigationRequest(incident_id=17, evidence_kind="prior_incidents")
    )
    with pytest.raises(InvestigationDenied, match="query_denied"):
        broker.execute(
            InvestigationRequest(
                incident_id=17,
                evidence_kind="database_projection",
                query="UPDATE runtime_incidents SET status='deleted'",
            )
        )

    with session_factory() as session:
        rows = session.query(RuntimeAgentInvestigationAudit).order_by(
            RuntimeAgentInvestigationAudit.id
        ).all()
    assert [(row.result_status, row.denial_code) for row in rows] == [
        ("allowed", None),
        ("denied", "query_denied"),
    ]
    assert all(row.result_bytes <= 32_768 for row in rows)


def test_provider_exception_is_audited_without_exception_text():
    audit_rows = []
    broker = InvestigationBroker(
        providers={
            "journal_summary": lambda request: (_ for _ in ()).throw(
                RuntimeError("credential-like-sensitive-detail")
            )
        },
        incident_exists=lambda incident_id: True,
        audit_recorder=audit_rows.append,
        clock=lambda: NOW,
    )

    with pytest.raises(InvestigationDenied, match="provider_error"):
        broker.execute(
            InvestigationRequest(incident_id=17, evidence_kind="journal_summary")
        )

    assert len(audit_rows) == 1
    assert audit_rows[0].result_status == "error"
    assert audit_rows[0].denial_code == "provider_error"
    assert "sensitive-detail" not in repr(audit_rows[0])
