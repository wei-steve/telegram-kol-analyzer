from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os

import pytest


NOW = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)


def _payload(action: str, **changes):
    from telegram_kol_research.reviewed_pending_entry_cancel import (
        REVIEWED_PENDING_ENTRY_TARGETS,
    )

    common = {
        "schema_version": 1,
        "action": action,
        "action_id": "maintenance-001",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "candidate_commit": "a" * 40,
        "release_manifest_sha256": "b" * 64,
        "expected_fingerprint": "c" * 64,
    }
    if action == "seed-entry-authority":
        common.update(
            database_path="/var/lib/telegram-kol/research.db",
            backup_path="/var/lib/telegram-kol-maintenance/research.seed.bak",
        )
    elif action == "drain-one":
        common.update(
            database_path="/var/lib/telegram-kol/research.db",
            target_order_id=REVIEWED_PENDING_ENTRY_TARGETS[0].order_id,
            evidence_sha256="d" * 64,
        )
    elif action == "bootstrap-control":
        common.update(
            database_path="/var/lib/telegram-kol/research.db",
            candidate_release_path="/opt/telegram-kol-releases/candidate",
            rollback_release_path="/opt/telegram-kol-releases/control",
            unit_manifest_sha256="e" * 64,
        )
    common.update(changes)
    return common


def _write(tmp_path, payload, *, mode=0o600):
    path = tmp_path / "action.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.mark.parametrize(
    "action",
    ("seed-entry-authority", "drain-one", "bootstrap-control"),
)
def test_exact_action_manifests_parse_with_bounded_expiry(tmp_path, action):
    from telegram_kol_research.deepcoin_maintenance_manifest import (
        MaintenanceAction,
        load_maintenance_manifest,
    )

    path = _write(tmp_path, _payload(action))
    manifest = load_maintenance_manifest(
        path,
        expected_action=MaintenanceAction(action),
        expected_uid=os.getuid(),
        now=NOW,
    )

    assert manifest.action.value == action
    assert manifest.action_id == "maintenance-001"
    assert len(manifest.file_sha256) == 64


def test_drain_target_must_be_one_canonical_scalar(tmp_path):
    from telegram_kol_research.deepcoin_maintenance_manifest import (
        MaintenanceAction,
        ManifestRefused,
        load_maintenance_manifest,
    )

    for target in (["1001"], "*", "not-canonical"):
        path = _write(tmp_path, _payload("drain-one", target_order_id=target))
        with pytest.raises(ManifestRefused, match="target_order_id"):
            load_maintenance_manifest(
                path,
                expected_action=MaintenanceAction.DRAIN_ONE,
                expected_uid=os.getuid(),
                now=NOW,
            )


@pytest.mark.parametrize(
    "mutation,reason",
    (
        ({"unexpected": True}, "keys"),
        ({"candidate_commit": "a" * 39}, "candidate_commit"),
        ({"release_manifest_sha256": "x" * 64}, "release_manifest_sha256"),
        ({"expected_fingerprint": "c" * 63}, "expected_fingerprint"),
    ),
)
def test_unknown_keys_and_invalid_hashes_are_rejected(tmp_path, mutation, reason):
    from telegram_kol_research.deepcoin_maintenance_manifest import (
        MaintenanceAction,
        ManifestRefused,
        load_maintenance_manifest,
    )

    path = _write(tmp_path, _payload("drain-one", **mutation))
    with pytest.raises(ManifestRefused, match=reason):
        load_maintenance_manifest(
            path,
            expected_action=MaintenanceAction.DRAIN_ONE,
            expected_uid=os.getuid(),
            now=NOW,
        )


def test_expired_or_more_than_fifteen_minutes_is_rejected(tmp_path):
    from telegram_kol_research.deepcoin_maintenance_manifest import (
        MaintenanceAction,
        ManifestRefused,
        load_maintenance_manifest,
    )

    cases = (
        _payload("drain-one", expires_at=(NOW - timedelta(seconds=1)).isoformat()),
        _payload("drain-one", expires_at=(NOW + timedelta(minutes=16)).isoformat()),
    )
    for payload in cases:
        path = _write(tmp_path, payload)
        with pytest.raises(ManifestRefused, match="expiry"):
            load_maintenance_manifest(
                path,
                expected_action=MaintenanceAction.DRAIN_ONE,
                expected_uid=os.getuid(),
                now=NOW,
            )


def test_file_size_mode_owner_and_symlink_are_fail_closed(tmp_path, monkeypatch):
    from telegram_kol_research.deepcoin_maintenance_manifest import (
        MaintenanceAction,
        ManifestRefused,
        load_maintenance_manifest,
    )

    loose = _write(tmp_path, _payload("drain-one"), mode=0o644)
    with pytest.raises(ManifestRefused, match="mode"):
        load_maintenance_manifest(
            loose,
            expected_action=MaintenanceAction.DRAIN_ONE,
            expected_uid=os.getuid(),
            now=NOW,
        )

    loose.chmod(0o600)
    linked = tmp_path / "linked.json"
    linked.symlink_to(loose)
    with pytest.raises(ManifestRefused, match="regular"):
        load_maintenance_manifest(
            linked,
            expected_action=MaintenanceAction.DRAIN_ONE,
            expected_uid=os.getuid(),
            now=NOW,
        )

    original = os.lstat
    monkeypatch.setattr(
        os,
        "lstat",
        lambda path: type(
            "Stat",
            (),
            {**{name: getattr(original(path), name) for name in dir(original(path)) if name.startswith("st_")}, "st_uid": os.getuid() + 1},
        )(),
    )
    with pytest.raises(ManifestRefused, match="owner"):
        load_maintenance_manifest(
            loose,
            expected_action=MaintenanceAction.DRAIN_ONE,
            expected_uid=os.getuid(),
            now=NOW,
        )


def test_manifest_larger_than_eight_kib_is_rejected_before_json_parse(tmp_path):
    from telegram_kol_research.deepcoin_maintenance_manifest import (
        MaintenanceAction,
        ManifestRefused,
        load_maintenance_manifest,
    )

    path = tmp_path / "large.json"
    path.write_bytes(b"{" + b"x" * 8192 + b"}")
    path.chmod(0o600)
    with pytest.raises(ManifestRefused, match="size"):
        load_maintenance_manifest(
            path,
            expected_action=MaintenanceAction.DRAIN_ONE,
            expected_uid=os.getuid(),
            now=NOW,
        )


def test_duplicated_target_field_is_rejected(tmp_path):
    from telegram_kol_research.deepcoin_maintenance_manifest import (
        MaintenanceAction,
        ManifestRefused,
        load_maintenance_manifest,
    )

    payload = json.dumps(_payload("drain-one"))
    raw = payload[:-1] + ',"target_order_id":"duplicated"}'
    path = tmp_path / "duplicate.json"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ManifestRefused, match="duplicated field"):
        load_maintenance_manifest(
            path,
            expected_action=MaintenanceAction.DRAIN_ONE,
            expected_uid=os.getuid(),
            now=NOW,
        )
