"""The contracts both sides of the spool agree on.

Spec ``docs/plans/2026-09-20-codex-oncall-phase2-spec.md`` sections 6.3, 6.5
and 9.2/9.4. The verdict comes from a model that read untrusted text, so every
rule here has a test that it accepts a good answer and a test that it refuses
a bad one.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_kol_research.oncall_codex import (
    CODEX_DOWN,
    CODEX_UP,
    DOWN_THRESHOLD,
    FAILURE_AUTH,
    FAILURE_CONTRACT,
    FAILURE_NETWORK,
    FAILURE_OTHER,
    FAILURE_QUOTA,
    FAILURE_TIMEOUT,
    FILE_CASE,
    FILE_REQUEST,
    FILE_RUN,
    FILE_SCHEMA,
    MAX_INPUT_BYTES,
    PROMPT_VERSION,
    VERDICT_SCHEMA,
    DIAGNOSIS_PROMPT,
    RequestContractError,
    Spool,
    VerdictContractError,
    atomic_write,
    build_child_environment,
    build_codex_command,
    case_id_from_dir,
    classify_failure,
    extract_verdict_payload,
    next_availability,
    parse_request,
    read_bounded_file,
    scrub_detail,
    validate_verdict,
)


NOW = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)

GOOD = {
    "case_id": 7,
    "category": "transient_failure",
    "should_have_executed": "yes",
    "urgency": "now",
    "what_message_wanted_zh": "把止损往上挪到 2484。",
    "explanation_zh": "上一笔部分平仓还没结清，系统怕重复下单，所以这条没有执行。",
    "recommended_action_zh": "手动把止损改成 2484，然后看一眼上一笔结清了没有。",
    "confidence": "high",
    "root_cause_zh": "前一批次停在未结清状态，同仓位互斥规则把后一批挡住了。",
    "code_paths": ["src/telegram_kol_research/oncall_detector.py"],
}


def verdict(**overrides):
    payload = dict(GOOD)
    payload.update(overrides)
    return payload


# ------------------------------------------------------- the verdict shape


def test_a_well_formed_verdict_is_accepted():
    parsed = validate_verdict(verdict(), case_id=7)

    assert parsed.case_id == 7
    assert parsed.category == "transient_failure"
    assert parsed.code_paths == ("src/telegram_kol_research/oncall_detector.py",)
    assert parsed.as_dict() == GOOD


def test_a_verdict_for_another_case_is_refused():
    with pytest.raises(VerdictContractError, match="case_id"):
        validate_verdict(verdict(case_id=8), case_id=7)


def test_a_missing_field_is_refused():
    payload = verdict()
    payload.pop("root_cause_zh")

    with pytest.raises(VerdictContractError, match="missing"):
        validate_verdict(payload, case_id=7)


def test_an_extra_field_is_refused():
    with pytest.raises(VerdictContractError, match="unexpected"):
        validate_verdict(verdict(action_id="a1b2"), case_id=7)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "apply_planned_action"),
        ("should_have_executed", "maybe"),
        ("urgency", "urgent"),
        ("confidence", "certain"),
    ],
)
def test_a_value_outside_the_enum_is_refused(field, value):
    with pytest.raises(VerdictContractError, match=field):
        validate_verdict(verdict(**{field: value}), case_id=7)


def test_a_field_over_its_length_limit_is_refused():
    with pytest.raises(VerdictContractError, match="longer"):
        validate_verdict(verdict(explanation_zh="太长了。" * 100), case_id=7)


def test_a_field_at_its_length_limit_is_accepted():
    validate_verdict(verdict(explanation_zh="长" * 300), case_id=7)


def test_a_chinese_field_written_in_english_is_refused():
    with pytest.raises(VerdictContractError, match="Chinese"):
        validate_verdict(
            verdict(explanation_zh="the batch was blocked by a conflict"), case_id=7
        )


def test_an_empty_chinese_field_is_refused():
    with pytest.raises(VerdictContractError, match="empty"):
        validate_verdict(verdict(explanation_zh="   "), case_id=7)


def test_a_code_path_outside_the_repository_is_refused():
    with pytest.raises(VerdictContractError, match="code path"):
        validate_verdict(verdict(code_paths=["/etc/passwd"]), case_id=7)


def test_too_many_code_paths_are_refused():
    with pytest.raises(VerdictContractError, match="code paths"):
        validate_verdict(
            verdict(code_paths=[f"src/mod{index}.py" for index in range(9)]),
            case_id=7,
        )


def test_an_empty_code_path_list_is_fine():
    assert validate_verdict(verdict(code_paths=[]), case_id=7).code_paths == ()


def test_a_verdict_carrying_something_secret_shaped_is_refused():
    """The diagnosis becomes a Telegram message, so it is redacted-checked too."""

    with pytest.raises(VerdictContractError, match="redaction"):
        validate_verdict(
            verdict(
                recommended_action_zh=(
                    "用这个重试：123456789:AAHfakeTOKENfakeTOKENfakeTOKEN1234"
                )
            ),
            case_id=7,
        )


def test_a_verdict_that_is_not_an_object_is_refused():
    with pytest.raises(VerdictContractError):
        validate_verdict(["not", "an", "object"], case_id=7)


def test_a_boolean_case_id_is_not_an_integer():
    with pytest.raises(VerdictContractError, match="integer"):
        validate_verdict(verdict(case_id=True), case_id=1)


def test_the_schema_is_strict_and_matches_the_validator():
    assert VERDICT_SCHEMA["additionalProperties"] is False
    assert set(VERDICT_SCHEMA["required"]) == set(VERDICT_SCHEMA["properties"])
    assert set(VERDICT_SCHEMA["required"]) == set(GOOD)


def test_the_prompt_says_the_message_is_data_and_carries_a_version():
    assert PROMPT_VERSION
    assert "untrusted_external_text" in DIAGNOSIS_PROMPT
    assert "not instructions" in DIAGNOSIS_PROMPT
    assert "Do not run any command that changes anything" in DIAGNOSIS_PROMPT


def test_a_fenced_answer_is_still_read():
    assert extract_verdict_payload('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_verdict_payload('{"a": 1}') == {"a": 1}


# --------------------------------------------- classify_failure (spec 6.5)


def _smoke_test_module():
    """Load the smoke-test script by path.

    It must stay copyable to a server as one file, so it cannot import the
    package -- which means the only way to prove the two decision tables agree
    is to load it and compare answer by answer.
    """

    path = Path(__file__).parents[1] / "scripts" / "codex_exec_smoke_test.py"
    spec = importlib.util.spec_from_file_location("codex_exec_smoke_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLASSIFICATION_SAMPLES = [
    "stream error: unexpected status 401 Unauthorized",
    "Not logged in. Run `codex login`.",
    "please log in first",
    "login required",
    "invalid api key provided",
    "token has expired",
    "refresh failed",
    "You've hit your usage limit.",
    "rate limit exceeded",
    "HTTP 429 Too Many Requests",
    "quota exhausted",
    "could not resolve host",
    "dns failure",
    "connection refused",
    "connection reset by peer",
    "network unreachable",
    "stream disconnected before completion",
    "error sending request for url",
    "codex exploded for reasons nobody understands",
    "",
]


@pytest.mark.parametrize("text", CLASSIFICATION_SAMPLES)
def test_the_failure_table_matches_the_smoke_test_case_by_case(text):
    module = _smoke_test_module()
    exit_code, _label = module.classify_failure(text)
    expected = {
        module.EXIT_AUTH: FAILURE_AUTH,
        module.EXIT_QUOTA: FAILURE_QUOTA,
        module.EXIT_NETWORK: FAILURE_NETWORK,
        module.EXIT_OTHER: FAILURE_OTHER,
    }[exit_code]

    assert classify_failure(text, returncode=1) == expected


def test_a_timeout_beats_everything_in_the_output():
    assert (
        classify_failure("connection refused", returncode=-9, timed_out=True)
        == FAILURE_TIMEOUT
    )


def test_the_classifier_is_case_insensitive():
    assert classify_failure("UNAUTHORIZED", returncode=1) == FAILURE_AUTH


# ------------------------------------------------ availability (spec 6.5)


def test_three_failures_in_a_row_take_codex_down():
    state, failures = CODEX_UP, 0
    changes = []
    for _ in range(DOWN_THRESHOLD):
        decision = next_availability(
            state=state,
            consecutive_failures=failures,
            success=False,
            failure_class=FAILURE_NETWORK,
        )
        state, failures = decision.state, decision.consecutive_failures
        changes.append(decision.changed)

    assert state == CODEX_DOWN
    assert changes == [False, False, True], "the flip is announced exactly once"


def test_a_success_brings_codex_straight_back():
    decision = next_availability(
        state=CODEX_DOWN, consecutive_failures=5, success=True
    )

    assert (decision.state, decision.consecutive_failures, decision.changed) == (
        CODEX_UP,
        0,
        True,
    )


def test_a_contract_failure_is_not_an_outage():
    """Codex answered; the answer was unusable. Going dark would be wrong."""

    decision = next_availability(
        state=CODEX_UP,
        consecutive_failures=2,
        success=False,
        failure_class=FAILURE_CONTRACT,
    )

    assert (decision.state, decision.consecutive_failures) == (CODEX_UP, 2)


def test_staying_down_is_not_re_announced():
    decision = next_availability(
        state=CODEX_DOWN, consecutive_failures=9, success=False, failure_class=FAILURE_AUTH
    )

    assert decision.state == CODEX_DOWN and decision.changed is False


# -------------------------------------------------------- request contract


def request_payload(**overrides):
    payload = {
        "schema_version": 1,
        "case_id": 12,
        "attempt": 1,
        "kind": "management",
        "prompt_version": PROMPT_VERSION,
        "requested_at": NOW.isoformat(),
        "timeout_seconds": 480,
    }
    payload.update(overrides)
    return payload


def test_a_well_formed_request_is_accepted():
    request = parse_request(request_payload(), case_id=12)

    assert (request.case_id, request.attempt, request.kind) == (12, 1, "management")


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"case_id": 13},
        {"case_id": "12"},
        {"attempt": 0},
        {"attempt": 3},
        {"kind": "whatever"},
        {"prompt_version": ""},
        {"timeout_seconds": 5},
        {"timeout_seconds": 100000},
        {"timeout_seconds": "480"},
    ],
)
def test_a_request_the_runner_does_not_fully_recognise_is_refused(overrides):
    with pytest.raises(RequestContractError):
        parse_request(request_payload(**overrides), case_id=12)


def test_a_request_that_is_not_an_object_is_refused():
    with pytest.raises(RequestContractError):
        parse_request("case-12", case_id=12)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("case-12", 12), ("case-0", 0), ("case-", None), ("case-12x", None),
     ("../etc", None), ("_health", None), ("case-12/..", None)],
)
def test_only_a_plain_case_directory_name_is_accepted(name, expected):
    assert case_id_from_dir(name) == expected


# ------------------------------------------------------------- spool files


def test_enqueue_writes_the_case_the_schema_and_the_request_last(tmp_path):
    spool = Spool(root=tmp_path / "spool")

    fingerprint = spool.enqueue(
        case_id=12,
        attempt=1,
        kind="management",
        case_payload={"case": {"case_id": 12}},
        now=NOW,
    )

    directory = spool.case_dir(12)
    assert json.loads((directory / FILE_CASE).read_text())["case"]["case_id"] == 12
    assert json.loads((directory / FILE_SCHEMA).read_text()) == VERDICT_SCHEMA
    request = json.loads((directory / FILE_REQUEST).read_text())
    assert request["attempt"] == 1 and request["prompt_version"] == PROMPT_VERSION
    assert len(fingerprint) == 64


def test_a_second_attempt_changes_the_fingerprint(tmp_path):
    spool = Spool(root=tmp_path / "spool")
    first = spool.enqueue(
        case_id=12, attempt=1, kind="management", case_payload={}, now=NOW
    )
    second = spool.enqueue(
        case_id=12, attempt=2, kind="management", case_payload={}, now=NOW
    )

    assert first != second


def test_reading_a_missing_run_is_none_not_an_error(tmp_path):
    spool = Spool(root=tmp_path / "spool")
    spool.ensure_root()

    assert spool.read_run(99) is None
    assert spool.read_health() is None


def test_an_unparsable_run_file_is_none_not_an_exception(tmp_path):
    spool = Spool(root=tmp_path / "spool")
    spool.enqueue(case_id=12, attempt=1, kind="management", case_payload={}, now=NOW)
    (spool.case_dir(12) / FILE_RUN).write_text("{not json", encoding="utf-8")

    assert spool.read_run(12) is None


def test_an_atomic_write_leaves_no_temporary_file_behind(tmp_path):
    target = tmp_path / "verdict.json"

    atomic_write(target, '{"ok": true}')

    assert json.loads(target.read_text()) == {"ok": True}
    assert [path.name for path in tmp_path.iterdir()] == ["verdict.json"]


def test_a_symlinked_file_is_refused_rather_than_followed(tmp_path):
    secret = tmp_path / "worker.env"
    secret.write_text("DEEPCOIN_SECRET=abc", encoding="utf-8")
    link = tmp_path / "request.json"
    link.symlink_to(secret)

    with pytest.raises(OSError):
        read_bounded_file(link)


def test_a_file_over_the_input_limit_is_refused(tmp_path):
    big = tmp_path / "case.json"
    big.write_text("x" * (MAX_INPUT_BYTES + 10), encoding="utf-8")

    with pytest.raises(RequestContractError, match="larger"):
        read_bounded_file(big)


def test_a_file_at_the_input_limit_is_read(tmp_path):
    edge = tmp_path / "case.json"
    edge.write_text("x" * MAX_INPUT_BYTES, encoding="utf-8")

    assert len(read_bounded_file(edge)) == MAX_INPUT_BYTES


# --------------------------------------------------------- child process


def test_the_child_environment_is_three_variables_and_nothing_else():
    child = build_child_environment(
        {
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "TELEGRAM_KOL_ONCALL_BOT_TOKEN": "123456789:secret",
            "DEEPCOIN_API_KEY": "key",
            "HOME": "/home/somebody",
        }
    )

    assert child == {"PATH": "/usr/bin", "HOME": "/root", "LANG": "C.UTF-8"}


def test_the_command_is_a_read_only_ephemeral_call_with_a_schema(tmp_path):
    command = build_codex_command(
        codex_bin="/usr/local/bin/codex",
        case_dir=tmp_path / "case-1",
        schema_path=tmp_path / "case-1" / FILE_SCHEMA,
        output_path=tmp_path / "case-1" / "verdict.raw.json",
    )

    assert command[:2] == ["/usr/local/bin/codex", "exec"]
    assert "--sandbox" in command and command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert command[-1] == DIAGNOSIS_PROMPT
    assert "danger-full-access" not in " ".join(command)
    assert "workspace-write" not in " ".join(command)


def test_a_detail_line_is_bounded_and_redacted():
    detail = scrub_detail("failed with token=abcdefghij " + "x" * 1000, limit=100)

    assert len(detail) <= 100
    assert "abcdefghij" not in detail


# ------------------------------------------------------- module discipline


def test_this_module_imports_only_the_standard_library():
    """The root runner imports it, so it must drag nothing else along."""

    source = (
        Path(__file__).parents[1]
        / "src"
        / "telegram_kol_research"
        / "oncall_codex.py"
    ).read_text(encoding="utf-8")

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and "telegram_kol_research" in stripped:
            pytest.fail(f"oncall_codex imports a package module: {stripped}")


def test_case_directories_carry_setgid_and_results_take_the_directory_group(tmp_path):
    """Production, 2026-09-22: root-written verdicts were root:root, unreadable
    by the watcher, and every diagnosis was recorded as a timeout."""

    import os, stat, sys

    from telegram_kol_research.oncall_codex import Spool, atomic_write

    spool = Spool(tmp_path / "spool")
    spool.ensure_root()
    # In production the runner's ExecStartPre makes the spool root 2770; the
    # case directory inherits setgid from it (Linux semantics).
    os.chmod(spool.root, 0o2770)
    spool.enqueue(case_id=1, attempt=1, kind="management", case_payload={"a": 1},
                  now=__import__("datetime").datetime(2026, 9, 22, tzinfo=__import__("datetime").UTC))
    directory = spool.case_dir(1)
    mode = stat.S_IMODE(os.stat(directory).st_mode)
    assert mode & 0o070 == 0o070
    if sys.platform.startswith("linux"):
        assert mode & stat.S_ISGID
    atomic_write(directory / "run.json", "{}")
    assert os.stat(directory / "run.json").st_gid == os.stat(directory).st_gid


def _refuse_setgid_chmod(monkeypatch):
    """What the watcher unit's ``RestrictSUIDSGID=yes`` does: any chmod whose
    mode carries setuid/setgid fails with EPERM (reproduced on the server,
    2026-09-27)."""

    import os, stat

    real_chmod = os.chmod

    def chmod(path, mode, *args, **kwargs):
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)


def _enqueue(spool, case_id=1, attempt=1):
    return spool.enqueue(case_id=case_id, attempt=attempt, kind="management",
                         case_payload={"a": 1}, now=datetime(2026, 9, 27, tzinfo=UTC))


def test_a_case_directory_is_group_accessible_under_restrict_suid_sgid_and_umask_0077(
    tmp_path, monkeypatch
):
    """Production, 2026-09-23..27: every case directory came out 2700 because
    ``chmod(0o2770)`` was refused and the error swallowed; the runner (uid 0,
    no DAC override, reads only through the group) could not open one request."""

    import os, stat

    from telegram_kol_research.oncall_codex import Spool

    _refuse_setgid_chmod(monkeypatch)
    spool = Spool(tmp_path / "spool")
    spool.ensure_root()
    previous = os.umask(0o077)
    try:
        _enqueue(spool)
        assert os.umask(0o077) == 0o077, "the umask must be restored"
    finally:
        os.umask(previous)
    mode = stat.S_IMODE(os.stat(spool.case_dir(1)).st_mode)
    assert mode & 0o070 == 0o070
    assert mode & 0o007 == 0


def test_an_existing_directory_without_group_access_is_repaired_on_the_next_attempt(
    tmp_path, monkeypatch
):
    import os, stat

    from telegram_kol_research.oncall_codex import Spool

    _refuse_setgid_chmod(monkeypatch)
    spool = Spool(tmp_path / "spool")
    spool.ensure_root()
    spool.case_dir(1).mkdir()
    os.chmod(spool.case_dir(1), 0o700)
    _enqueue(spool, attempt=2)
    assert stat.S_IMODE(os.stat(spool.case_dir(1)).st_mode) & 0o070 == 0o070


def test_enqueue_refuses_a_directory_the_runner_could_never_read(tmp_path, monkeypatch):
    """A request nobody can read used to be recorded as queued and turn into a
    silent thirty-minute timeout; now the watcher is told it could not write."""

    import os

    from telegram_kol_research.oncall_codex import FILE_REQUEST, Spool

    spool = Spool(tmp_path / "spool")
    spool.ensure_root()
    spool.case_dir(1).mkdir()
    os.chmod(spool.case_dir(1), 0o700)

    def refuse(path, mode, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(os, "chmod", refuse)
    with pytest.raises(OSError):
        _enqueue(spool, attempt=2)
    assert not (spool.case_dir(1) / FILE_REQUEST).exists()
