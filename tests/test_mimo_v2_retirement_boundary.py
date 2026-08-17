from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_RUNTIME_PATHS = (
    "src/telegram_kol_research/mimo_contract_circuit.py",
    "src/telegram_kol_research/mimo_recognition_runs.py",
    "src/telegram_kol_research/mimo_v2_contract.py",
    "src/telegram_kol_research/mimo_v2_execution_adapter.py",
    "src/telegram_kol_research/mimo_v2_replay.py",
)
FORBIDDEN_RUNTIME_MARKERS = {
    "src/telegram_kol_research/authoritative_recognition.py": (
        "v2_live_adapter",
        "infer_mimo_authoritative_v2",
    ),
    "src/telegram_kol_research/cli.py": ("mimo-v2-replay",),
    "src/telegram_kol_research/trading_settings.py": (
        "mimo_contract_mode",
        "mimo_v2_activation_after_raw_message_id",
    ),
    "src/telegram_kol_research/web_app.py": ("mimo-v2", "v2_live_adapter"),
}


def test_mimo_v2_runtime_modules_are_retired():
    present = [path for path in FORBIDDEN_RUNTIME_PATHS if (ROOT / path).exists()]
    assert present == []


def test_mimo_v2_activation_surfaces_are_retired():
    found = []
    for relative_path, markers in FORBIDDEN_RUNTIME_MARKERS.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        found.extend(
            f"{relative_path}:{marker}" for marker in markers if marker in source
        )
    assert found == []
