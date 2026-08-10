from telegram_kol_research.execution_state_projection import project_execution_state


def test_price_touched_without_exchange_binding_is_not_presented_as_holding():
    projection = project_execution_state(lifecycle_status="entered")

    assert projection.state == "price_touched"
    assert projection.label == "价格触发，未提交交易所订单"
    assert projection.price_touched is True
    assert projection.exchange_verified is False


def test_deferred_contract_is_presented_as_waiting_for_adjacent_context():
    projection = project_execution_state(contract_state="deferred")

    assert projection.state == "deferred"
    assert projection.label == "等待相邻消息确认"


def test_submitting_contract_is_presented_as_exchange_submission_in_progress():
    projection = project_execution_state(contract_state="submitting")

    assert projection.state == "submitting"
    assert projection.label == "正在提交交易所"


def test_submit_unknown_is_non_retryable_in_presentation():
    projection = project_execution_state(contract_state="submit_unknown")

    assert projection.state == "submit_unknown"
    assert projection.label == "交易所结果待核对，禁止重试"
    assert projection.retry_allowed is False


def test_verified_binding_and_live_position_is_presented_as_holding():
    projection = project_execution_state(
        contract_state="verified",
        contract_terminal_kind="verified_entry",
        binding_status="open",
        has_live_position=True,
    )

    assert projection.state == "holding"
    assert projection.label == "持仓中"
    assert projection.exchange_verified is True


def test_verified_refusal_is_presented_as_no_order():
    projection = project_execution_state(
        contract_state="verified",
        contract_terminal_kind="verified_refusal",
    )

    assert projection.state == "verified_refusal"
    assert projection.label == "已明确拒绝，未下单"
    assert projection.exchange_verified is True


def test_explicit_contradiction_has_highest_priority():
    projection = project_execution_state(
        lifecycle_status="entered",
        contract_state="verified",
        contract_terminal_kind="verified_entry",
        contradiction_reason_codes=("verified_without_binding",),
    )

    assert projection.state == "contradiction"
    assert projection.label == "执行状态异常"
    assert projection.severity == "critical"
    assert projection.reason_codes == ("verified_without_binding",)


def test_verified_entry_without_binding_is_derived_as_a_contradiction():
    projection = project_execution_state(
        contract_state="verified",
        contract_terminal_kind="verified_entry",
    )

    assert projection.state == "contradiction"
    assert projection.reason_codes == ("verified_without_binding",)


def test_legacy_live_binding_with_position_remains_holding_without_contract_backfill():
    projection = project_execution_state(
        lifecycle_status="entered",
        binding_status="active",
        has_live_position=True,
    )

    assert projection.state == "holding"
    assert projection.exchange_verified is True


def test_verified_live_order_without_position_is_not_called_holding_or_unsubmitted():
    projection = project_execution_state(
        lifecycle_status="entered",
        contract_state="verified",
        contract_terminal_kind="verified_entry",
        binding_status="active",
        has_live_position=False,
    )

    assert projection.state == "exchange_order_verified"
    assert projection.label == "交易所订单已核验，等待成交"
    assert projection.exchange_verified is True


def test_legacy_live_binding_without_position_is_not_called_unsubmitted():
    projection = project_execution_state(
        lifecycle_status="entered",
        binding_status="open",
        has_live_position=False,
    )

    assert projection.state == "exchange_order_bound"
    assert projection.label == "交易所订单已绑定，成交待核验"
    assert projection.exchange_verified is False


def test_failed_contract_with_live_binding_is_a_contradiction():
    projection = project_execution_state(
        contract_state="failed",
        binding_status="open",
    )

    assert projection.state == "contradiction"
    assert projection.reason_codes == ("terminal_contract_with_live_exchange_evidence",)
    assert projection.exchange_verified is False


def test_expired_contract_without_exchange_evidence_is_not_exchange_verified():
    projection = project_execution_state(contract_state="expired")

    assert projection.state == "expired"
    assert projection.label == "执行已过期，未下单"
    assert projection.exchange_verified is False


def test_fresh_submitting_contract_with_writer_binding_is_not_a_contradiction():
    projection = project_execution_state(
        contract_state="submitting",
        binding_status="open",
    )

    assert projection.state == "submitting"
    assert projection.label == "正在提交交易所"
