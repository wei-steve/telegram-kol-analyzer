import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict

import pytest

import telegram_kol_research.context_authority_cutover as cutover_module
from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
    load_ai_recognition_config,
    save_ai_recognition_config,
)
from telegram_kol_research.context_authority_cutover import (
    apply_context_authority_cutover,
    plan_context_authority_cutover,
    rollback_context_authority_cutover,
)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_config(path, *, mimo_base_url="https://api.xiaomimimo.com/v1", mimo_text=True):
    save_ai_recognition_config(
        path,
        AiRecognitionConfig(
            mode="ai_provider",
            active_text_model_id="deepseek-v4-flash",
            active_image_model_id="mimo-v2.5",
            context_resolution_model_id="deepseek-v4-flash",
            ai_models=[
                AiModelConfig(
                    id="deepseek-v4-flash",
                    label="DeepSeek V4 Flash",
                    base_url="https://api.deepseek.com",
                    api_key="deepseek-secret",
                    model="deepseek-v4-flash",
                    supports_text=True,
                    supports_image=False,
                ),
                AiModelConfig(
                    id="glm-ocr",
                    label="GLM OCR",
                    base_url="https://open.bigmodel.cn/api/paas/v4",
                    api_key="glm-secret",
                    model="glm-ocr",
                    supports_text=False,
                    supports_image=True,
                ),
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url=mimo_base_url,
                    api_key="mimo-secret",
                    model="mimo-v2.5",
                    supports_text=mimo_text,
                    supports_image=True,
                ),
            ],
        ),
    )


def test_plan_is_dry_run_and_receipt_contains_no_secrets(tmp_path):
    config_path = tmp_path / "ai.yaml"
    _write_config(config_path)
    before = config_path.read_bytes()

    receipt = plan_context_authority_cutover(
        config_path,
        new_model_id="mimo-v2.5",
        expected_old_model_id="deepseek-v4-flash",
    )

    assert receipt.mode == "dry_run"
    assert receipt.before_sha256 == hashlib.sha256(before).hexdigest()
    assert receipt.after_sha256 != receipt.before_sha256
    assert receipt.old_model_id == "deepseek-v4-flash"
    assert receipt.new_model_id == "mimo-v2.5"
    assert receipt.backup_path is None
    assert config_path.read_bytes() == before
    serialized_receipt = json.dumps(asdict(receipt), sort_keys=True)
    assert "deepseek-secret" not in serialized_receipt
    assert "mimo-secret" not in serialized_receipt


def test_apply_preserves_secrets_and_exact_backup(tmp_path):
    config_path = tmp_path / "ai.yaml"
    backup_path = tmp_path / "backups" / "ai.before.yaml"
    _write_config(config_path)
    os.chmod(config_path, 0o640)
    before = config_path.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()

    receipt = apply_context_authority_cutover(
        config_path,
        new_model_id="mimo-v2.5",
        expected_before_sha256=before_sha,
        expected_old_model_id="deepseek-v4-flash",
        backup_path=backup_path,
    )

    assert receipt.mode == "applied"
    assert receipt.before_sha256 == before_sha
    assert receipt.after_sha256 == _sha256(config_path)
    assert receipt.backup_path == str(backup_path)
    assert backup_path.read_bytes() == before
    assert config_path.stat().st_mode & 0o777 == 0o640
    assert load_ai_recognition_config(config_path).context_resolution_model_id == "mimo-v2.5"
    updated = config_path.read_text(encoding="utf-8")
    assert "deepseek-secret" in updated
    assert "mimo-secret" in updated
    assert "deepseek-secret" not in repr(receipt)
    assert "mimo-secret" not in repr(receipt)


def test_repeated_exact_apply_is_idempotent(tmp_path):
    config_path = tmp_path / "ai.yaml"
    backup_path = tmp_path / "ai.before.yaml"
    _write_config(config_path)
    before_sha = _sha256(config_path)
    first = apply_context_authority_cutover(
        config_path,
        new_model_id="mimo-v2.5",
        expected_before_sha256=before_sha,
        expected_old_model_id="deepseek-v4-flash",
        backup_path=backup_path,
    )
    after = config_path.read_bytes()
    backup = backup_path.read_bytes()

    repeated = apply_context_authority_cutover(
        config_path,
        new_model_id="mimo-v2.5",
        expected_before_sha256=before_sha,
        expected_old_model_id="deepseek-v4-flash",
        backup_path=backup_path,
    )

    assert repeated.mode == "already_applied"
    assert repeated.after_sha256 == first.after_sha256
    assert config_path.read_bytes() == after
    assert backup_path.read_bytes() == backup


def test_apply_rejects_wrong_before_hash_or_old_model(tmp_path):
    config_path = tmp_path / "ai.yaml"
    _write_config(config_path)

    with pytest.raises(ValueError, match="before SHA"):
        apply_context_authority_cutover(
            config_path,
            new_model_id="mimo-v2.5",
            expected_before_sha256="0" * 64,
            expected_old_model_id="deepseek-v4-flash",
            backup_path=tmp_path / "wrong-hash.yaml",
        )
    with pytest.raises(ValueError, match="old model"):
        apply_context_authority_cutover(
            config_path,
            new_model_id="mimo-v2.5",
            expected_before_sha256=_sha256(config_path),
            expected_old_model_id="another-model",
            backup_path=tmp_path / "wrong-model.yaml",
        )
    assert not (tmp_path / "wrong-hash.yaml").exists()
    assert not (tmp_path / "wrong-model.yaml").exists()


@pytest.mark.parametrize(
    ("new_model_id", "mimo_base_url", "reason"),
    [
        ("mimo-v2.5", "", "configured"),
        ("glm-ocr", "https://api.xiaomimimo.com/v1", "text"),
    ],
)
def test_plan_rejects_unconfigured_or_non_text_new_model(
    tmp_path, new_model_id, mimo_base_url, reason
):
    config_path = tmp_path / "ai.yaml"
    _write_config(config_path, mimo_base_url=mimo_base_url)

    with pytest.raises(ValueError, match=reason):
        plan_context_authority_cutover(
            config_path,
            new_model_id=new_model_id,
            expected_old_model_id="deepseek-v4-flash",
        )


def test_rollback_requires_hashes_and_restores_exact_backup(tmp_path):
    config_path = tmp_path / "ai.yaml"
    backup_path = tmp_path / "ai.before.yaml"
    _write_config(config_path)
    original = config_path.read_bytes()
    applied = apply_context_authority_cutover(
        config_path,
        new_model_id="mimo-v2.5",
        expected_before_sha256=hashlib.sha256(original).hexdigest(),
        expected_old_model_id="deepseek-v4-flash",
        backup_path=backup_path,
    )

    with pytest.raises(ValueError, match="current SHA"):
        rollback_context_authority_cutover(
            config_path,
            backup_path=backup_path,
            expected_current_sha256="0" * 64,
            expected_backup_sha256=hashlib.sha256(original).hexdigest(),
        )
    with pytest.raises(ValueError, match="backup SHA"):
        rollback_context_authority_cutover(
            config_path,
            backup_path=backup_path,
            expected_current_sha256=applied.after_sha256,
            expected_backup_sha256="0" * 64,
        )

    receipt = rollback_context_authority_cutover(
        config_path,
        backup_path=backup_path,
        expected_current_sha256=applied.after_sha256,
        expected_backup_sha256=hashlib.sha256(original).hexdigest(),
    )

    assert receipt.mode == "rolled_back"
    assert receipt.before_sha256 == applied.after_sha256
    assert receipt.after_sha256 == hashlib.sha256(original).hexdigest()
    assert receipt.old_model_id == "mimo-v2.5"
    assert receipt.new_model_id == "deepseek-v4-flash"
    assert config_path.read_bytes() == original


def test_apply_uses_same_directory_atomic_replace(tmp_path, monkeypatch):
    config_path = tmp_path / "ai.yaml"
    _write_config(config_path)
    calls = []
    real_replace = os.replace

    def recording_replace(source, destination):
        calls.append((os.fspath(source), os.fspath(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(cutover_module.os, "replace", recording_replace)

    apply_context_authority_cutover(
        config_path,
        new_model_id="mimo-v2.5",
        expected_before_sha256=_sha256(config_path),
        expected_old_model_id="deepseek-v4-flash",
        backup_path=tmp_path / "ai.before.yaml",
    )

    assert len(calls) == 1
    source, destination = calls[0]
    assert os.path.dirname(source) == str(config_path.parent)
    assert destination == str(config_path)


def test_module_cli_defaults_to_dry_run_and_hides_secrets(tmp_path):
    config_path = tmp_path / "ai.yaml"
    _write_config(config_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_kol_research.context_authority_cutover",
            str(config_path),
            "--new-model-id",
            "mimo-v2.5",
            "--expected-old-model",
            "deepseek-v4-flash",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["mode"] == "dry_run"
    assert "deepseek-secret" not in completed.stdout + completed.stderr
    assert "mimo-secret" not in completed.stdout + completed.stderr
