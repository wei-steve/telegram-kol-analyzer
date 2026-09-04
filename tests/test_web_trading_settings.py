from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_removed_legacy_freeze_key_has_no_runtime_or_serialized_effect(tmp_path):
    app = create_web_app(database_path=tmp_path / "web-internal-freeze.db")

    with TestClient(app) as client:
        rejected = client.post(
            "/api/trading-settings",
            json={"legacy_entry_submission_frozen": True},
        )
        loaded = client.get("/api/trading-settings")

    assert rejected.status_code == 200
    assert "legacy_entry_submission_frozen" not in loaded.json()


def test_position_management_liveness_v2_api_round_trip(tmp_path):
    app = create_web_app(database_path=tmp_path / "web-liveness-v2.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/trading-settings",
            json={"position_management_liveness_v2_mode": "shadow"},
        )
        loaded = client.get("/api/trading-settings")

    assert response.status_code == 200
    assert response.json()["position_management_liveness_v2_mode"] == "shadow"
    assert loaded.json()["position_management_liveness_v2_mode"] == "shadow"


def test_position_management_liveness_v2_api_fails_closed(tmp_path):
    app = create_web_app(database_path=tmp_path / "web-liveness-v2-invalid.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/trading-settings",
            json={"position_management_liveness_v2_mode": "unsafe"},
        )

    assert response.status_code == 422
    assert "position_management_liveness_v2_mode" in response.json()["detail"]


def test_position_management_liveness_v2_form_renders_all_modes(tmp_path):
    app = create_web_app(database_path=tmp_path / "web-liveness-v2-form.db")

    with TestClient(app) as client:
        response = client.get("/more-panel")

    assert response.status_code == 200
    assert 'name="position_management_liveness_v2_mode"' in response.text
    assert all(f'value="{mode}"' in response.text for mode in ("disabled", "shadow", "live"))


def test_trigger_protection_lineage_api_round_trips_shadow_and_watermark(tmp_path):
    app = create_web_app(database_path=tmp_path / "web-lineage.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/trading-settings",
            json={
                "trigger_protection_lineage_attribution_mode": "shadow",
                "trigger_protection_lineage_activation_after_intent_id": 177,
            },
        )
        loaded = client.get("/api/trading-settings")

    assert response.status_code == 200
    assert response.json()["trigger_protection_lineage_attribution_mode"] == "shadow"
    assert response.json()["trigger_protection_lineage_activation_after_intent_id"] == 177
    assert loaded.json()["trigger_protection_lineage_attribution_mode"] == "shadow"


def test_trigger_protection_lineage_form_warns_and_renders_all_modes(tmp_path):
    app = create_web_app(database_path=tmp_path / "web-lineage-form.db")

    with TestClient(app) as client:
        response = client.get("/more-panel")

    assert response.status_code == 200
    assert 'name="trigger_protection_lineage_attribution_mode"' in response.text
    assert 'name="trigger_protection_lineage_activation_after_intent_id"' in response.text
    assert "仅处理水位之后的新 intent" in response.text
