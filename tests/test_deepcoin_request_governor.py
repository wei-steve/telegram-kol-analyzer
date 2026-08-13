import fcntl
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

import telegram_kol_research.deepcoin_request_governor as governor_module

from telegram_kol_research.deepcoin_request_governor import (
    DeepcoinGovernorDeadlineExceeded,
    DeepcoinGovernorStateError,
    DeepcoinRequestGovernor,
    GovernorMode,
)
from telegram_kol_research.deepcoin_request_policy import (
    RequestPriority,
    RequestProfile,
)


class _FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = float(now)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fixed_profile(profile: RequestProfile):
    return lambda method, path: profile


def _governor(
    tmp_path: Path,
    *,
    clock: _FakeClock | None = None,
    api_key: str = "uid-key",
    mode: GovernorMode = GovernorMode.ENFORCE_ALL,
    profile: RequestProfile | None = None,
) -> DeepcoinRequestGovernor:
    active_clock = clock or _FakeClock()
    return DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key=api_key,
        mode=mode,
        state_directory=tmp_path,
        monotonic_factory=active_clock,
        sleep_fn=active_clock.sleep,
        profile_resolver=(_fixed_profile(profile) if profile else None),
    )


def test_first_request_does_not_wait(tmp_path):
    clock = _FakeClock()
    governor = _governor(tmp_path, clock=clock)

    lease = governor.acquire(
        method="GET",
        request_path="/deepcoin/trade/trigger-orders-pending?limit=100",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=10,
    )

    assert lease.waited_ms == 0
    assert lease.observed_delay_ms == 0
    assert lease.state_error is None
    assert clock.sleeps == []


def test_governor_creates_a_missing_private_state_directory(tmp_path):
    state_directory = tmp_path / "new-governor-state"

    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key="new-state-directory-uid",
        mode=GovernorMode.ENFORCE_ALL,
        state_directory=state_directory,
    )
    governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=time.monotonic() + 1,
    )

    assert state_directory.is_dir()
    assert state_directory.stat().st_mode & 0o777 == 0o700


def test_fifth_request_waits_for_safe_four_per_second_window(tmp_path):
    clock = _FakeClock()
    governor = _governor(tmp_path, clock=clock)

    for _ in range(5):
        governor.acquire(
            method="GET",
            request_path="/deepcoin/trade/trigger-orders-pending?limit=100",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=10,
        )

    assert clock.sleeps == [pytest.approx(1.0)]


def test_minute_window_is_enforced_independently(tmp_path):
    clock = _FakeClock()
    governor = _governor(
        tmp_path,
        clock=clock,
        profile=RequestProfile(1000, 3, 1000, 3),
    )

    for _ in range(4):
        governor.acquire(
            method="GET",
            request_path="/test/minute",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=61,
        )

    assert clock.sleeps == [pytest.approx(60.0)]


def test_strict_profile_waits_explicit_one_point_two_five_seconds(tmp_path):
    clock = _FakeClock()
    governor = _governor(tmp_path, clock=clock)

    for _ in range(2):
        governor.acquire(
            method="GET",
            request_path="/deepcoin/account/positions-history?instType=SWAP",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=2,
        )

    assert clock.sleeps == [pytest.approx(1.25)]


def test_query_variants_share_one_endpoint_budget(tmp_path):
    clock = _FakeClock()
    governor = _governor(tmp_path, clock=clock)

    governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions-history?instId=BTC-USDT-SWAP",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=2,
    )
    governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions-history?instId=ETH-USDT-SWAP",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=2,
    )

    assert clock.sleeps == [pytest.approx(1.25)]


def test_two_governor_instances_share_uid_state(tmp_path):
    clock = _FakeClock()
    first = _governor(tmp_path, clock=clock)
    second = _governor(tmp_path, clock=clock)

    first.acquire(
        method="GET",
        request_path="/deepcoin/account/positions-history",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=2,
    )
    second.acquire(
        method="GET",
        request_path="/deepcoin/account/positions-history",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=2,
    )

    assert first.uid_scope_hash == second.uid_scope_hash
    assert clock.sleeps == [pytest.approx(1.25)]


def test_distinct_uids_do_not_share_state(tmp_path):
    clock = _FakeClock()
    first = _governor(tmp_path, clock=clock, api_key="uid-a")
    second = _governor(tmp_path, clock=clock, api_key="uid-b")

    for governor in (first, second):
        governor.acquire(
            method="GET",
            request_path="/deepcoin/account/positions-history",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=1,
        )

    assert first.uid_scope_hash != second.uid_scope_hash
    assert clock.sleeps == []


def test_background_sub_budget_reserves_capacity_for_critical_request(tmp_path):
    clock = _FakeClock()
    governor = _governor(
        tmp_path,
        clock=clock,
        profile=RequestProfile(4, 120, 2, 60),
    )

    for _ in range(2):
        governor.acquire(
            method="GET",
            request_path="/test/priority",
            priority=RequestPriority.BACKGROUND,
            deadline_monotonic=clock(),
        )
    with pytest.raises(DeepcoinGovernorDeadlineExceeded):
        governor.acquire(
            method="GET",
            request_path="/test/priority",
            priority=RequestPriority.BACKGROUND,
            deadline_monotonic=clock(),
        )

    critical = governor.acquire(
        method="GET",
        request_path="/test/priority",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=clock(),
    )

    assert critical.waited_ms == 0
    assert clock.sleeps == []


def test_deadline_refuses_before_sleep_or_reservation(tmp_path):
    clock = _FakeClock()
    governor = _governor(tmp_path, clock=clock)
    governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions-history",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=clock(),
    )

    with pytest.raises(DeepcoinGovernorDeadlineExceeded):
        governor.acquire(
            method="GET",
            request_path="/deepcoin/account/positions-history",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=clock(),
        )

    assert clock.sleeps == []


def test_expired_deadline_refuses_even_when_endpoint_has_capacity(tmp_path):
    clock = _FakeClock(now=5)
    governor = _governor(tmp_path, clock=clock)
    state_path = governor.state_path_for(
        method="POST",
        request_path="/deepcoin/trade/order",
    )

    with pytest.raises(DeepcoinGovernorDeadlineExceeded):
        governor.acquire(
            method="POST",
            request_path="/deepcoin/trade/order",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=4,
        )

    assert state_path.exists() is False


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -1])
def test_invalid_deadline_refuses_before_state_or_reservation(tmp_path, deadline):
    governor = _governor(tmp_path)

    with pytest.raises(
        DeepcoinGovernorDeadlineExceeded,
        match="deepcoin_governor_deadline_invalid",
    ):
        governor.acquire(
            method="POST",
            request_path="/deepcoin/trade/order",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=deadline,
        )

    assert list(tmp_path.iterdir()) == []


def test_endpoint_lock_wait_honors_deadline_before_capacity_check(tmp_path):
    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key="lock-deadline-uid",
        mode=GovernorMode.ENFORCE_ALL,
        state_directory=tmp_path,
    )
    state_path = governor.state_path_for(
        method="POST",
        request_path="/deepcoin/trade/order",
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)

    def release_later():
        time.sleep(0.3)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    release_thread = threading.Thread(target=release_later)
    release_thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(DeepcoinGovernorDeadlineExceeded):
            governor.acquire(
                method="POST",
                request_path="/deepcoin/trade/order",
                priority=RequestPriority.CRITICAL,
                deadline_monotonic=started + 0.05,
            )
        elapsed = time.monotonic() - started
    finally:
        release_thread.join(timeout=1)

    assert elapsed < 0.2


def test_telemetry_mode_does_not_block_on_a_busy_endpoint_lock(tmp_path):
    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key="telemetry-lock-uid",
        mode=GovernorMode.TELEMETRY,
        state_directory=tmp_path,
    )
    state_path = governor.state_path_for(
        method="GET",
        request_path="/deepcoin/account/positions",
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)

    def release_later():
        time.sleep(0.3)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    release_thread = threading.Thread(target=release_later)
    release_thread.start()
    started = time.monotonic()
    try:
        lease = governor.acquire(
            method="GET",
            request_path="/deepcoin/account/positions",
            priority=RequestPriority.NORMAL,
            deadline_monotonic=None,
        )
        elapsed = time.monotonic() - started
    finally:
        release_thread.join(timeout=1)

    assert elapsed < 0.1
    assert lease.waited_ms == 0
    assert lease.state_error == "governor_lock_busy"


def test_lock_wait_reloads_time_after_another_process_reserves_slot(tmp_path):
    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key="lock-refresh-uid",
        mode=GovernorMode.ENFORCE_ALL,
        state_directory=tmp_path,
        profile_resolver=_fixed_profile(
            RequestProfile(100, 1000, 100, 1000, 0.2)
        ),
    )
    state_path = governor.state_path_for(
        method="GET",
        request_path="/deepcoin/account/positions-history",
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    outcome = {}

    def reserve_after_lock():
        outcome["lease"] = governor.acquire(
            method="GET",
            request_path="/deepcoin/account/positions-history",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=time.monotonic() + 1,
        )
        outcome["finished_at"] = time.monotonic()

    request_thread = threading.Thread(target=reserve_after_lock)
    request_thread.start()
    time.sleep(0.05)
    existing_start = time.monotonic()
    state_path.write_text(
        json.dumps({"starts": [existing_start], "version": 1}),
        encoding="utf-8",
    )
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)
    request_thread.join(timeout=1)

    assert request_thread.is_alive() is False
    assert outcome["finished_at"] - existing_start >= 0.15
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(payload["starts"]) == 2


def test_telemetry_records_observed_delay_without_waiting(tmp_path):
    clock = _FakeClock()
    governor = _governor(
        tmp_path,
        clock=clock,
        mode=GovernorMode.TELEMETRY,
        profile=RequestProfile(1, 48, 1, 24, 1.25),
    )

    first = governor.acquire(
        method="GET",
        request_path="/test/telemetry",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=clock(),
    )
    second = governor.acquire(
        method="GET",
        request_path="/test/telemetry",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=clock(),
    )

    assert first.observed_delay_ms == 0
    assert second.observed_delay_ms == 1250
    assert second.waited_ms == 0
    assert clock.sleeps == []


def test_enforce_reads_observes_but_does_not_wait_for_post(tmp_path):
    clock = _FakeClock()
    governor = _governor(
        tmp_path,
        clock=clock,
        mode=GovernorMode.ENFORCE_READS,
        profile=RequestProfile(1, 48, 1, 24, 1.25),
    )

    governor.acquire(
        method="POST",
        request_path="/test/write",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=clock(),
    )
    second = governor.acquire(
        method="POST",
        request_path="/test/write",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=clock(),
    )

    assert second.observed_delay_ms == 1250
    assert second.waited_ms == 0
    assert clock.sleeps == []


def test_disabled_mode_does_not_create_state_files(tmp_path):
    clock = _FakeClock()
    governor = _governor(
        tmp_path,
        clock=clock,
        mode=GovernorMode.DISABLED,
    )

    lease = governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions-history",
        priority=RequestPriority.CRITICAL,
        deadline_monotonic=clock(),
    )

    assert lease.waited_ms == 0
    assert list(tmp_path.iterdir()) == []


def test_state_paths_and_payload_do_not_contain_api_key(tmp_path):
    secret_key = "secret-uid-key-do-not-store"
    governor = _governor(tmp_path, api_key=secret_key)
    governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=10,
    )

    for path in tmp_path.iterdir():
        assert secret_key not in path.name
        if path.is_file():
            assert secret_key not in path.read_text(encoding="utf-8")


def test_malformed_state_fails_closed_when_enforced(tmp_path):
    governor = _governor(tmp_path)
    state_path = governor.state_path_for(
        method="GET",
        request_path="/deepcoin/account/positions",
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{", encoding="utf-8")

    with pytest.raises(DeepcoinGovernorStateError):
        governor.acquire(
            method="GET",
            request_path="/deepcoin/account/positions",
            priority=RequestPriority.NORMAL,
            deadline_monotonic=10,
        )


def test_malformed_state_does_not_block_telemetry_mode(tmp_path):
    governor = _governor(tmp_path, mode=GovernorMode.TELEMETRY)
    state_path = governor.state_path_for(
        method="GET",
        request_path="/deepcoin/account/positions",
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{", encoding="utf-8")

    lease = governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=10,
    )

    assert lease.state_error == "governor_state_invalid"
    assert lease.waited_ms == 0


def test_deeply_nested_state_is_a_bounded_state_error(tmp_path):
    governor = _governor(tmp_path, mode=GovernorMode.TELEMETRY)
    state_path = governor.state_path_for(
        method="GET",
        request_path="/deepcoin/account/positions",
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("[" * 1_100 + "0" + "]" * 1_100, encoding="utf-8")

    lease = governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=10,
    )

    assert lease.state_error == "governor_state_invalid"


def test_default_120_per_minute_boundary_waits_for_next_window(tmp_path):
    clock = _FakeClock()
    governor = _governor(
        tmp_path,
        clock=clock,
        profile=RequestProfile(1_000, 120, 500, 60),
    )

    for _ in range(121):
        governor.acquire(
            method="GET",
            request_path="/test/120-minute-window",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=61,
        )

    assert clock.sleeps == [pytest.approx(60.0)]


@pytest.mark.parametrize(
    ("method", "path", "capacity"),
    [
        ("GET", "/deepcoin/account/positions", 8),
        ("POST", "/deepcoin/trade/order", 12),
    ],
)
def test_default_endpoint_second_boundaries(tmp_path, method, path, capacity):
    clock = _FakeClock()
    governor = _governor(tmp_path, clock=clock)

    for _ in range(capacity + 1):
        governor.acquire(
            method=method,
            request_path=path,
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=2,
        )

    assert clock.sleeps == [pytest.approx(1.0)]


def _reserve_strict_slot(state_directory: str, queue) -> None:
    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key="process-shared-uid",
        mode=GovernorMode.ENFORCE_ALL,
        state_directory=Path(state_directory),
    )
    try:
        governor.acquire(
            method="GET",
            request_path="/deepcoin/account/positions-history",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=time.monotonic() + 0.1,
        )
    except DeepcoinGovernorDeadlineExceeded:
        queue.put("deadline")
    else:
        queue.put("leased")


def test_two_processes_cannot_reserve_the_same_strict_slot(tmp_path):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_reserve_strict_slot, args=(str(tmp_path), queue))
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert sorted(queue.get(timeout=1) for _ in processes) == ["deadline", "leased"]


def test_governor_state_is_canonical_bounded_json(tmp_path):
    governor = _governor(tmp_path)
    governor.acquire(
        method="GET",
        request_path="/deepcoin/account/positions",
        priority=RequestPriority.NORMAL,
        deadline_monotonic=10,
    )
    state_path = governor.state_path_for(
        method="GET",
        request_path="/deepcoin/account/positions",
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload == {"starts": [0.0], "version": 1}
    assert state_path.stat().st_size < 65_536


@pytest.mark.parametrize(
    "mode",
    ["telemetry", "enforce_reads", "enforce_all"],
)
def test_request_governor_mode_loads_only_from_valid_environment(
    tmp_path, mode
):
    state_directory = tmp_path / "governor-state"
    state_directory.mkdir(mode=0o700)
    state_directory.chmod(0o700)

    config = governor_module.load_deepcoin_governor_environment(
        {
            "DEEPCOIN_REQUEST_GOVERNOR_MODE": mode,
            "DEEPCOIN_GOVERNOR_STATE_DIR": str(state_directory.resolve()),
        }
    )

    assert config.mode == GovernorMode(mode)
    assert config.state_directory == state_directory.resolve()


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"DEEPCOIN_REQUEST_GOVERNOR_MODE": "unknown"},
        {"DEEPCOIN_REQUEST_GOVERNOR_MODE": "enforce_all"},
        {
            "DEEPCOIN_REQUEST_GOVERNOR_MODE": "enforce_reads",
            "DEEPCOIN_GOVERNOR_STATE_DIR": "relative/state",
        },
        {
            "DEEPCOIN_REQUEST_GOVERNOR_MODE": "enforce_all",
            "DEEPCOIN_GOVERNOR_STATE_DIR": "/tmp/invalid\0state",
        },
    ],
)
def test_request_governor_mode_invalid_environment_defaults_to_disabled(environment):
    config = governor_module.load_deepcoin_governor_environment(environment)

    assert config.mode == GovernorMode.DISABLED
    assert config.state_directory is None


def test_request_governor_mode_rejects_unprotected_or_symlink_state_directory(
    tmp_path,
):
    unprotected = tmp_path / "unprotected"
    unprotected.mkdir(mode=0o755)
    unprotected.chmod(0o755)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    symlink = tmp_path / "linked"
    symlink.symlink_to(target, target_is_directory=True)

    for path in (unprotected, symlink):
        config = governor_module.load_deepcoin_governor_environment(
            {
                "DEEPCOIN_REQUEST_GOVERNOR_MODE": "enforce_all",
                "DEEPCOIN_GOVERNOR_STATE_DIR": str(path),
            }
        )
        assert config.mode == GovernorMode.DISABLED
        assert config.state_directory is None


def test_governor_refuses_replaced_state_directory_without_touching_symlink_target(
    tmp_path,
):
    state_directory = tmp_path / "governor-state"
    state_directory.mkdir(mode=0o700)
    state_directory.chmod(0o700)
    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key="directory-swap-uid",
        mode=GovernorMode.ENFORCE_ALL,
        state_directory=state_directory,
    )
    original_directory = tmp_path / "original-governor-state"
    state_directory.rename(original_directory)
    replacement_target = tmp_path / "replacement-target"
    replacement_target.mkdir(mode=0o755)
    replacement_target.chmod(0o755)
    state_directory.symlink_to(replacement_target, target_is_directory=True)

    with pytest.raises(
        DeepcoinGovernorStateError,
        match="governor_state_directory_invalid",
    ):
        governor.acquire(
            method="POST",
            request_path="/deepcoin/trade/order",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=time.monotonic() + 1,
        )

    assert replacement_target.stat().st_mode & 0o777 == 0o755
    assert list(replacement_target.iterdir()) == []
    assert list(original_directory.iterdir()) == []


def test_governor_refuses_a_different_private_directory_at_the_same_path(tmp_path):
    state_directory = tmp_path / "governor-state"
    state_directory.mkdir(mode=0o700)
    state_directory.chmod(0o700)
    governor = DeepcoinRequestGovernor(
        base_url="https://api.deepcoin.test",
        api_key="directory-inode-swap-uid",
        mode=GovernorMode.ENFORCE_ALL,
        state_directory=state_directory,
    )
    original_directory = tmp_path / "original-governor-state"
    state_directory.rename(original_directory)
    state_directory.mkdir(mode=0o700)
    state_directory.chmod(0o700)

    with pytest.raises(
        DeepcoinGovernorStateError,
        match="governor_state_directory_invalid",
    ):
        governor.acquire(
            method="POST",
            request_path="/deepcoin/trade/order",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=time.monotonic() + 1,
        )

    assert list(state_directory.iterdir()) == []
    assert list(original_directory.iterdir()) == []


def test_governor_refuses_lock_symlink_without_touching_target(tmp_path):
    governor = _governor(tmp_path)
    state_path = governor.state_path_for(
        method="POST",
        request_path="/deepcoin/trade/order",
    )
    target = tmp_path.parent / f"{tmp_path.name}-lock-target"
    target.write_text("unchanged", encoding="utf-8")
    target.chmod(0o644)
    state_path.with_suffix(".lock").symlink_to(target)

    with pytest.raises(
        DeepcoinGovernorStateError,
        match="governor_lock_failed",
    ):
        governor.acquire(
            method="POST",
            request_path="/deepcoin/trade/order",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=10,
        )

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert target.stat().st_mode & 0o777 == 0o644


def test_governor_refuses_state_symlink_without_touching_target(tmp_path):
    governor = _governor(tmp_path)
    state_path = governor.state_path_for(
        method="POST",
        request_path="/deepcoin/trade/order",
    )
    target = tmp_path.parent / f"{tmp_path.name}-state-target"
    target.write_text(
        '{"starts":[],"version":1}',
        encoding="utf-8",
    )
    target.chmod(0o644)
    state_path.symlink_to(target)

    with pytest.raises(
        DeepcoinGovernorStateError,
        match="governor_state_invalid",
    ):
        governor.acquire(
            method="POST",
            request_path="/deepcoin/trade/order",
            priority=RequestPriority.CRITICAL,
            deadline_monotonic=10,
        )

    assert target.read_text(encoding="utf-8") == '{"starts":[],"version":1}'
    assert target.stat().st_mode & 0o777 == 0o644


def test_request_governor_mode_disabled_never_guesses_a_state_directory(tmp_path):
    config = governor_module.load_deepcoin_governor_environment(
        {
            "DEEPCOIN_REQUEST_GOVERNOR_MODE": "disabled",
            "DEEPCOIN_GOVERNOR_STATE_DIR": str(tmp_path.resolve()),
        }
    )

    assert config.mode == GovernorMode.DISABLED
    assert config.state_directory is None
