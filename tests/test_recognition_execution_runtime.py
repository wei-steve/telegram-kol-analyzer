import asyncio
import threading
from types import SimpleNamespace

import pytest

from telegram_kol_research.recognition_execution_runtime import (
    RecognitionExecutionRegistry,
)


def test_cancelled_to_thread_waiter_does_not_remove_underlying_execution():
    async def scenario():
        registry = RecognitionExecutionRegistry()
        entered = threading.Event()
        release = threading.Event()

        def work():
            with registry.admitted("attempt-token"):
                entered.set()
                release.wait(5)

        task = asyncio.create_task(asyncio.to_thread(work))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert registry.snapshot().active == 1
        registry.stop_admission()
        assert registry.wait_drained(0.01) is False
        release.set()
        assert await asyncio.to_thread(registry.wait_drained, 2) is True

    asyncio.run(scenario())


def test_drain_gate_refuses_new_claims_but_does_not_clear_active_token():
    registry = RecognitionExecutionRegistry()
    context = registry.admitted("active")
    context.__enter__()
    registry.stop_admission()
    assert registry.snapshot().active == 1
    with pytest.raises(RuntimeError, match="draining"):
        with registry.admitted("new"):
            pass
    context.__exit__(None, None, None)
    assert registry.snapshot().active == 0


def test_drain_gate_can_be_checked_before_a_durable_claim():
    registry = RecognitionExecutionRegistry()
    registry.stop_admission()

    with pytest.raises(RuntimeError, match="draining"):
        registry.require_accepting()

    assert registry.snapshot().active == 0


def test_shutdown_drain_waits_for_underlying_registered_execution():
    from telegram_kol_research.web_app import _wait_recognition_execution_drain

    async def scenario():
        registry = RecognitionExecutionRegistry()
        entered = threading.Event()
        release = threading.Event()

        def work():
            with registry.admitted("underlying-thread"):
                entered.set()
                release.wait(5)

        thread = threading.Thread(target=work)
        thread.start()
        await asyncio.to_thread(entered.wait, 2)
        app = SimpleNamespace(
            state=SimpleNamespace(
                recognition_execution_registry=registry,
                message_processing_activity=SimpleNamespace(
                    snapshot=lambda: {"active_chat_lanes": 0}
                ),
                recognition_execution_schema_valid=False,
                recognition_execution_owner=None,
            )
        )
        waiting = asyncio.create_task(
            _wait_recognition_execution_drain(app, timeout_seconds=1.0)
        )
        await asyncio.sleep(0.05)
        assert waiting.done() is False
        release.set()
        assert await waiting is True
        thread.join(timeout=2)

    asyncio.run(scenario())
