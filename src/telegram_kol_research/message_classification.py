"""The first-pass ``message_classes`` contract: constants, parsing, shadow compare.

Specification: ``docs/plans/2026-09-24-first-pass-classification-contract-design.md``
(the section numbers quoted below are that document's).

This module is **phase 1 (shadow)**. Nothing here decides anything: it parses the
new field, derives the same shape from the two existing fields, and compares the
two. No execution, routing, trigger, or notification path imports it, and it
imports nothing from those paths either -- it is pure and side-effect free.

Phase 1 never raises on a contract violation
--------------------------------------------
``parse_message_classes`` returns violations as data. The production prompt
(``trading.analysis.shared``, currently v8) does not emit ``message_classes`` at
all, and rolling a newer version back to v8 must keep recognition working. A
parser that raised would turn a prompt rollback into a recognition outage, so
absence is recorded as ``present=False`` with **no** violation (design §8,
"解析器必须容忍没有新字段的旧提示词版本（记 missing 而不是违规）"). Promoting a
violation to a hard failure is phase 3 work (design §6).

Violation vocabulary
--------------------
Codes are stable English snake_case and closed (``MESSAGE_CLASS_VIOLATIONS``).
Each one names the cell of the design it enforces:

``classes_not_a_list``
    §2.1 "不是数组". The key is present but is not a JSON array (``null``
    included). Key *absent* is not this -- that is ``present=False``.
``classes_empty``
    §2.1 "非空。空列表 ... = 契约违规".
``too_many_classes``
    §2.1 "长度上限 4".
``exclusive_class_not_alone``
    §2.1 "`闲话` 与 `图片不可读` 必须独占：出现它们时列表长度必须为 1".
``duplicate_class_target``
    §2.1 "`(class, target)` 完全重复 = 契约违规". The same ``class`` with a
    *different* target is legal ("BTC 和 ETH 都平掉").
``class_order_violation``
    §2.1 "元素顺序：管理类在前、新开仓在后".
``element_not_an_object``
    §2.1, structural: a list entry that is not a JSON object.
``class_missing``
    §2.2, the element carries no ``class``.
``class_unknown``
    §2.2, ``class`` outside the five values.
``target_required``
    §2.4, rows `策略管理` / `仓位管理`: "必须为对象" -- the target is null.
``target_not_allowed``
    §2.4, rows `新策略` / `闲话` / `图片不可读`: "必须 null".
``target_not_an_object``
    §2.3, structural: ``target`` is neither null nor an object.
``resolution_missing``
    §2.3 "`resolution`：三态，`target` 非 null 时必须非 null".
``resolution_unknown``
    §2.3, ``resolution`` outside ``TARGET_RESOLUTIONS``.
``forthcoming_not_allowed_for_position``
    §2.4, row `仓位管理`: "不得 `forthcoming`，持仓不可能还没发生".
``lifecycle_id_required``
    §2.3 "`exact` —— ... `lifecycle_id` 必须给出".
``lifecycle_id_not_allowed``
    §2.3 "`lifecycle_id`：只有 `exact` 时非 null，其余两态必须 null".
``lifecycle_id_outside_candidate_set``
    §6 "或 `exact` 的 `lifecycle_id` 不在候选集合内". Only checked when the
    caller supplies ``allowed_lifecycle_ids``.
``forthcoming_symbol_required``
    §2.3 "`forthcoming` —— ... 必须给出 `symbol`，否则这份定量无处归属".
``forthcoming_without_entry_context``
    §2.4 "`策略管理` 用 `forthcoming` 时，必须同时出现 `entry_context` 或
    `entry_fragments`".
``strategy_required``
    §2.4, row `新策略`: "`strategy` 必须非空" (null, or every field null).
``strategy_missing_symbol`` / ``strategy_missing_side`` /
``strategy_missing_entry`` / ``strategy_missing_stop_loss``
    §2.4, row `新策略`: "`symbol`/`side`/`entry`/`stop_loss` 四项非 null".
    ``take_profit`` is deliberately *not* here -- §2.4 allows it to be null, and
    §3.1 makes the stop loss the hard criterion.
``strategy_not_allowed``
    §2.4 "列表里**没有** `新策略` 元素时，`strategy` 必须为 `null`", which also
    covers the `闲话` / `图片不可读` rows of the table.
``image_unreadable_without_image``
    §3.5 "`input_reading.image_quality = none`（没有图片）时出现 `图片不可读`
    = 契约违规".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CLASS_IMAGE_UNREADABLE",
    "CLASS_NEW_STRATEGY",
    "CLASS_POSITION_MANAGEMENT",
    "CLASS_SMALL_TALK",
    "CLASS_STRATEGY_MANAGEMENT",
    "EXCLUSIVE_MESSAGE_CLASSES",
    "MANAGEMENT_MESSAGE_CLASSES",
    "MAX_MESSAGE_CLASSES",
    "MESSAGE_CLASSES",
    "MESSAGE_CLASS_VIOLATIONS",
    "MessageClassElement",
    "MessageClassTarget",
    "ParsedMessageClasses",
    "TARGET_FORBIDDEN_CLASSES",
    "TARGET_REQUIRED_CLASSES",
    "TARGET_RESOLUTIONS",
    "compare_message_classes",
    "message_class_identities",
    "derive_message_classes",
    "parse_message_classes",
]


CLASS_NEW_STRATEGY = "新策略"
CLASS_STRATEGY_MANAGEMENT = "策略管理"
CLASS_POSITION_MANAGEMENT = "仓位管理"
CLASS_SMALL_TALK = "闲话"
CLASS_IMAGE_UNREADABLE = "图片不可读"

#: §2.2, ordered as the design's table lists them.
MESSAGE_CLASSES: tuple[str, ...] = (
    CLASS_NEW_STRATEGY,
    CLASS_STRATEGY_MANAGEMENT,
    CLASS_POSITION_MANAGEMENT,
    CLASS_SMALL_TALK,
    CLASS_IMAGE_UNREADABLE,
)

#: §2.3, the three target states.
TARGET_RESOLUTIONS: tuple[str, ...] = ("exact", "forthcoming", "unknown")

RESOLUTION_EXACT = "exact"
RESOLUTION_FORTHCOMING = "forthcoming"
RESOLUTION_UNKNOWN = "unknown"

#: §2.4, the two rows whose ``target`` must be an object.
TARGET_REQUIRED_CLASSES = frozenset(
    {CLASS_STRATEGY_MANAGEMENT, CLASS_POSITION_MANAGEMENT}
)
#: §2.4, the three rows whose ``target`` must be ``null``.
TARGET_FORBIDDEN_CLASSES = frozenset(
    {CLASS_NEW_STRATEGY, CLASS_SMALL_TALK, CLASS_IMAGE_UNREADABLE}
)
#: §2.1, the two values that must be the only element of the list.
EXCLUSIVE_MESSAGE_CLASSES = frozenset({CLASS_SMALL_TALK, CLASS_IMAGE_UNREADABLE})
#: §2.1 ordering: these sort before ``新策略``.
MANAGEMENT_MESSAGE_CLASSES = frozenset(
    {CLASS_STRATEGY_MANAGEMENT, CLASS_POSITION_MANAGEMENT}
)

#: §2.1 "长度上限 4".
MAX_MESSAGE_CLASSES = 4

#: §2.4, row `新策略`: the four fields that must be present.
REQUIRED_STRATEGY_FIELDS: tuple[str, ...] = (
    "symbol",
    "side",
    "entry",
    "stop_loss",
)

MESSAGE_CLASS_VIOLATIONS = frozenset(
    {
        "classes_not_a_list",
        "classes_empty",
        "too_many_classes",
        "exclusive_class_not_alone",
        "duplicate_class_target",
        "class_order_violation",
        "element_not_an_object",
        "class_missing",
        "class_unknown",
        "target_required",
        "target_not_allowed",
        "target_not_an_object",
        "resolution_missing",
        "resolution_unknown",
        "forthcoming_not_allowed_for_position",
        "lifecycle_id_required",
        "lifecycle_id_not_allowed",
        "lifecycle_id_outside_candidate_set",
        "forthcoming_symbol_required",
        "forthcoming_without_entry_context",
        "strategy_required",
        "strategy_missing_symbol",
        "strategy_missing_side",
        "strategy_missing_entry",
        "strategy_missing_stop_loss",
        "strategy_not_allowed",
        "image_unreadable_without_image",
    }
)

#: §4, the two existing ``event_type`` groups the derivation reads.
_POSITION_EVENT_TYPES = frozenset({"position_update", "exit_position"})
_STRATEGY_EVENT_TYPES = frozenset({"cancel_entry", "entry_confirm"})


@dataclass(frozen=True)
class MessageClassTarget:
    """One element's ``target`` object (§2.3)."""

    resolution: str | None
    lifecycle_id: int | None = None
    symbol: str | None = None
    side: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "lifecycle_id": self.lifecycle_id,
            "symbol": self.symbol,
            "side": self.side,
        }

    def identity(self) -> tuple[str | None, int | None]:
        """The part of a target that decides whether two elements are the same.

        ``symbol`` / ``side`` are evidence fields the design allows to be
        missing (§2.3 "证据充分才填，不得猜测"), so they never take part in
        duplicate detection or in the shadow comparison.
        """

        return (self.resolution, self.lifecycle_id)


@dataclass(frozen=True)
class MessageClassElement:
    """One ``{class, target}`` pair (§2)."""

    message_class: str
    target: MessageClassTarget | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.message_class,
            "target": self.target.to_dict() if self.target is not None else None,
        }

    def identity(self) -> tuple[str, str | None, int | None]:
        if self.target is None:
            return (self.message_class, None, None)
        resolution, lifecycle_id = self.target.identity()
        return (self.message_class, resolution, lifecycle_id)


@dataclass(frozen=True)
class ParsedMessageClasses:
    """The result of reading ``message_classes`` off a first-pass payload.

    ``present`` distinguishes "the prompt never produced this field" from "the
    field is there and wrong"; phase 1 must not confuse the two.
    ``elements`` holds the elements that parsed into a well-formed shape, which
    can be fewer than the raw list when some entries were unusable.
    """

    present: bool
    elements: tuple[MessageClassElement, ...] = ()
    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """The field is there and every rule in §2.1 / §2.4 held."""

        return self.present and not self.violations

    def to_payload(self) -> list[dict[str, Any]]:
        """The canonical (normalized) form, for persistence and projection."""

        return [element.to_dict() for element in self.elements]


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _has_any_value(mapping: Mapping[str, Any]) -> bool:
    return any(value is not None for value in mapping.values())


def parse_message_classes(
    payload: Mapping[str, Any] | None,
    *,
    allowed_lifecycle_ids: Sequence[int] | None = None,
) -> ParsedMessageClasses:
    """Parse and validate ``payload['message_classes']`` without ever raising.

    The whole first-pass payload is required, not just the sub-object: §2.4 ties
    the list to ``strategy`` / ``entry_context`` / ``entry_fragments``, and §3.5
    ties `图片不可读` to ``input_reading.image_quality``.

    ``allowed_lifecycle_ids`` is optional. When given, §6's "``exact`` 的
    ``lifecycle_id`` 不在候选集合内" is checked too; when omitted the candidate
    set is simply unknown and that one rule is skipped.
    """

    payload = _as_mapping(payload)
    if "message_classes" not in payload:
        return ParsedMessageClasses(present=False)

    raw = payload.get("message_classes")
    violations: list[str] = []

    def flag(code: str) -> None:
        if code not in violations:
            violations.append(code)

    if not isinstance(raw, list):
        # ``null`` lands here too: §2.1 admits only a non-empty array. Absence
        # was already handled above and is *not* a violation in phase 1.
        flag("classes_not_a_list")
        return ParsedMessageClasses(present=True, violations=tuple(violations))

    if not raw:
        flag("classes_empty")
        return ParsedMessageClasses(present=True, violations=tuple(violations))

    if len(raw) > MAX_MESSAGE_CLASSES:
        flag("too_many_classes")

    allowed_ids = (
        None if allowed_lifecycle_ids is None else {int(x) for x in allowed_lifecycle_ids}
    )

    image_quality = _clean_str(
        _as_mapping(payload.get("input_reading")).get("image_quality")
    )
    message_has_no_image = image_quality == "none"

    has_entry_carrier = bool(payload.get("entry_context")) or bool(
        payload.get("entry_fragments")
    )

    elements: list[MessageClassElement] = []
    for raw_element in raw:
        if not isinstance(raw_element, Mapping):
            flag("element_not_an_object")
            continue

        message_class = _clean_str(raw_element.get("class"))
        if message_class is None:
            flag("class_missing")
            continue
        if message_class not in MESSAGE_CLASSES:
            flag("class_unknown")
            continue

        raw_target = raw_element.get("target")
        target: MessageClassTarget | None = None

        if raw_target is None:
            if message_class in TARGET_REQUIRED_CLASSES:
                flag("target_required")
        elif not isinstance(raw_target, Mapping):
            flag("target_not_an_object")
        else:
            if message_class in TARGET_FORBIDDEN_CLASSES:
                flag("target_not_allowed")
            resolution = _clean_str(raw_target.get("resolution"))
            lifecycle_id = _int_or_none(raw_target.get("lifecycle_id"))
            symbol = _clean_str(raw_target.get("symbol"))
            side = _clean_str(raw_target.get("side"))

            if resolution is None:
                flag("resolution_missing")
            elif resolution not in TARGET_RESOLUTIONS:
                flag("resolution_unknown")
                resolution = None
            else:
                if (
                    resolution == RESOLUTION_FORTHCOMING
                    and message_class == CLASS_POSITION_MANAGEMENT
                ):
                    flag("forthcoming_not_allowed_for_position")
                if resolution == RESOLUTION_EXACT:
                    if lifecycle_id is None:
                        flag("lifecycle_id_required")
                    elif allowed_ids is not None and lifecycle_id not in allowed_ids:
                        flag("lifecycle_id_outside_candidate_set")
                else:
                    if lifecycle_id is not None:
                        flag("lifecycle_id_not_allowed")
                if resolution == RESOLUTION_FORTHCOMING:
                    if symbol is None:
                        flag("forthcoming_symbol_required")
                    if (
                        message_class == CLASS_STRATEGY_MANAGEMENT
                        and not has_entry_carrier
                    ):
                        flag("forthcoming_without_entry_context")

            target = MessageClassTarget(
                resolution=resolution,
                lifecycle_id=lifecycle_id,
                symbol=symbol,
                side=side,
            )

        if message_class == CLASS_IMAGE_UNREADABLE and message_has_no_image:
            flag("image_unreadable_without_image")

        elements.append(
            MessageClassElement(message_class=message_class, target=target)
        )

    present_classes = {element.message_class for element in elements}

    if present_classes & EXCLUSIVE_MESSAGE_CLASSES and len(raw) != 1:
        flag("exclusive_class_not_alone")

    identities = [element.identity() for element in elements]
    if len(identities) != len(set(identities)):
        flag("duplicate_class_target")

    seen_new_strategy = False
    for element in elements:
        if element.message_class == CLASS_NEW_STRATEGY:
            seen_new_strategy = True
        elif seen_new_strategy and element.message_class in MANAGEMENT_MESSAGE_CLASSES:
            flag("class_order_violation")
            break

    _validate_strategy_coupling(
        payload,
        present_classes=present_classes,
        flag=flag,
    )

    return ParsedMessageClasses(
        present=True,
        elements=tuple(elements),
        violations=tuple(violations),
    )


def _validate_strategy_coupling(
    payload: Mapping[str, Any],
    *,
    present_classes: set[str],
    flag,
) -> None:
    """§2.4's ``strategy`` column, which is per-message rather than per-element."""

    raw_strategy = payload.get("strategy")
    strategy = _as_mapping(raw_strategy)
    strategy_is_empty = not isinstance(raw_strategy, Mapping) or not _has_any_value(
        strategy
    )

    if CLASS_NEW_STRATEGY in present_classes:
        if strategy_is_empty:
            flag("strategy_required")
            return
        for field in REQUIRED_STRATEGY_FIELDS:
            if _clean_str(strategy.get(field)) is None:
                flag(f"strategy_missing_{field}")
        return

    # "列表里没有 `新策略` 元素时，`strategy` 必须为 `null`". The long-standing
    # all-fields-null object is the prompt's own compatibility shape for null
    # (see the 价格与字段归一化 section of the shared prompt), so it counts as
    # null here rather than as a violation.
    if not strategy_is_empty:
        flag("strategy_not_allowed")


def _derive_target(source: Mapping[str, Any]) -> MessageClassTarget:
    """§4: ``target_lifecycle_id`` 非空 → ``exact``，空 → ``unknown``."""

    lifecycle_id = _int_or_none(source.get("target_lifecycle_id"))
    if lifecycle_id is None:
        return MessageClassTarget(
            resolution=RESOLUTION_UNKNOWN,
            lifecycle_id=None,
            symbol=_clean_str(source.get("symbol")),
            side=_clean_str(source.get("side")),
        )
    return MessageClassTarget(
        resolution=RESOLUTION_EXACT,
        lifecycle_id=lifecycle_id,
        symbol=_clean_str(source.get("symbol")),
        side=_clean_str(source.get("side")),
    )


def _lifecycle_target_sources(
    lifecycle_event: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Expand ``lifecycle_event.targets`` (§4, "多目标 fanout")."""

    raw_targets = lifecycle_event.get("targets")
    if isinstance(raw_targets, list) and raw_targets:
        expanded = [item for item in raw_targets if isinstance(item, Mapping)]
        if expanded:
            return expanded
    return [lifecycle_event]


def derive_message_classes(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """§4: rebuild the same shape from ``recognition_result`` + ``lifecycle_event``.

    Read-only by contract -- the returned dicts are fresh objects and ``payload``
    is never written back to. This is the baseline the shadow comparison in
    phase 1 measures the model's explicit answer against; it is not, and must
    not become, an input to any decision.
    """

    payload = _as_mapping(payload)
    recognition_result = _clean_str(payload.get("recognition_result"))

    if recognition_result == "识别失败":
        # The old value conflates a run-level failure with "the model could not
        # read the picture"; the derivation side cannot tell them apart, which
        # is why §4 counts this row separately in the phase 2 agreement rate.
        return [MessageClassElement(CLASS_IMAGE_UNREADABLE).to_dict()]

    lifecycle_event = _as_mapping(payload.get("lifecycle_event"))
    event_type = _clean_str(lifecycle_event.get("event_type")) or "none"

    elements: list[MessageClassElement] = []
    if event_type in _POSITION_EVENT_TYPES:
        derived_class: str | None = CLASS_POSITION_MANAGEMENT
    elif event_type in _STRATEGY_EVENT_TYPES:
        derived_class = CLASS_STRATEGY_MANAGEMENT
    else:
        derived_class = None

    if derived_class is not None:
        for source in _lifecycle_target_sources(lifecycle_event):
            elements.append(
                MessageClassElement(
                    message_class=derived_class,
                    target=_derive_target(source),
                )
            )

    if recognition_result == "是策略":
        elements.append(MessageClassElement(CLASS_NEW_STRATEGY))

    if not elements:
        elements.append(MessageClassElement(CLASS_SMALL_TALK))

    return [element.to_dict() for element in elements]


def _identity_of(element: Any) -> tuple[str, str | None, int | None] | None:
    if isinstance(element, MessageClassElement):
        return element.identity()
    if not isinstance(element, Mapping):
        return None
    message_class = _clean_str(element.get("class"))
    if message_class is None:
        return None
    target = element.get("target")
    if not isinstance(target, Mapping):
        return (message_class, None, None)
    return (
        message_class,
        _clean_str(target.get("resolution")),
        _int_or_none(target.get("lifecycle_id")),
    )


def _identity_payload(
    identity: tuple[str, str | None, int | None]
) -> dict[str, Any]:
    message_class, resolution, lifecycle_id = identity
    if resolution is None and lifecycle_id is None:
        return {"class": message_class, "target": None}
    return {
        "class": message_class,
        "target": {"resolution": resolution, "lifecycle_id": lifecycle_id},
    }


def message_class_identities(
    elements: Sequence[Any] | None,
) -> tuple[tuple[str, str | None, int | None], ...]:
    """The order-independent comparable form of a classification list.

    ``symbol`` / ``side`` are dropped for the reason given on
    ``MessageClassTarget.identity``.
    """

    identities = [
        identity
        for identity in (_identity_of(item) for item in (elements or []))
        if identity is not None
    ]
    return tuple(sorted(identities, key=_sort_key))


def compare_message_classes(
    explicit: Sequence[Any] | None,
    derived: Sequence[Any] | None,
) -> dict[str, Any]:
    """Shadow comparison of the explicit list against the §4 derivation.

    ``symbol`` and ``side`` are ignored: §2.3 lets the model leave them out when
    the evidence is thin, so a difference there says nothing about whether the
    two sides classified the message the same way. Only ``class``,
    ``target.resolution`` and ``target.lifecycle_id`` are compared, as multisets
    -- order is a contract rule (§2.1) checked by the parser, not a disagreement.
    """

    explicit_ids = Counter(
        identity
        for identity in (_identity_of(item) for item in (explicit or []))
        if identity is not None
    )
    derived_ids = Counter(
        identity
        for identity in (_identity_of(item) for item in (derived or []))
        if identity is not None
    )
    only_explicit = explicit_ids - derived_ids
    only_derived = derived_ids - explicit_ids
    return {
        "agrees": not only_explicit and not only_derived,
        "only_in_explicit": [
            _identity_payload(identity)
            for identity in sorted(only_explicit.elements(), key=_sort_key)
        ],
        "only_in_derived": [
            _identity_payload(identity)
            for identity in sorted(only_derived.elements(), key=_sort_key)
        ],
    }


def _sort_key(identity: tuple[str, str | None, int | None]) -> tuple[str, str, int]:
    message_class, resolution, lifecycle_id = identity
    return (message_class, resolution or "", lifecycle_id if lifecycle_id is not None else -1)
