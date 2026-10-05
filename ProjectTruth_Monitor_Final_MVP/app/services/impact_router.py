from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.signals import SignalRuleCode


@dataclass(frozen=True, slots=True)
class SuggestedReviewAction:
    role: str
    rule_code: SignalRuleCode
    action: str
    reason: str
    limitation: str = "Suggested review action only; it is not legal, credit, contract or purchase advice."


_COMPLETION_ACTIONS = {
    "property_adviser": "Review and update the client brief and comparison sheet.",
    "supplier": "Review the delivery schedule, outstanding commitments and sales timing.",
    "lender": "Review the monitoring file and the assumptions supporting current exposure.",
    "marketing_team": "Review possession or completion claims in active collateral.",
    "contractor": "Review the recorded programme and outstanding coordination milestones.",
    "due_diligence": "Review the updated official evidence and note the change in the diligence file.",
}


_GENERIC_ACTIONS: dict[SignalRuleCode, str] = {
    SignalRuleCode.PROJECT_MARKED_LAPSED: "Review the exact official status evidence and any affected internal records.",
    SignalRuleCode.PROJECT_STATUS_CHANGED: "Review the status evidence before updating client-facing material.",
    SignalRuleCode.QPR_FLAG_OBSERVED: "Review the exact QPR-related flag and request another source if context is incomplete.",
    SignalRuleCode.QPR_FLAG_CLEARED: "Review the latest official capture before clearing internal follow-up items.",
    SignalRuleCode.SOURCE_UNAVAILABLE: "Retry according to policy or upload an authorized copy; keep last verified facts visible.",
    SignalRuleCode.SOURCE_MONITORING_DEGRADED: "Review source health and do not treat this check as evidence of no change.",
    SignalRuleCode.IMPORTANT_FIELD_REMOVED: "Review old and new evidence because removal may reflect a source-layout change.",
    SignalRuleCode.NEW_PHASE_OR_REGISTRATION: "Review parent/phase identity before linking the new registration.",
    SignalRuleCode.OFFICIAL_VS_MARKETING_CONFLICT: "Review both records and update marketing collateral only after confirmation.",
    SignalRuleCode.OFFICIAL_VS_DEVELOPER_CONFLICT: "Review both official-source captures and keep the conflict unresolved until confirmed.",
    SignalRuleCode.EXACT_LINKED_NOTICE_ADDED: "Open and review the exact linked notice; do not infer legal effect automatically.",
    SignalRuleCode.VALUE_REVERSED: "Review the full timeline to confirm whether the value returned to an earlier state.",
}


ROLE_ALIASES = {
    "property adviser": "property_adviser",
    "property_advisor": "property_adviser",
    "adviser": "property_adviser",
    "marketing": "marketing_team",
    "credit": "lender",
    "consultant": "due_diligence",
}


def suggested_review_actions(
    rule_code: SignalRuleCode | str,
    roles: Iterable[str],
) -> tuple[SuggestedReviewAction, ...]:
    code = rule_code if isinstance(rule_code, SignalRuleCode) else SignalRuleCode(str(rule_code))
    completion_rule = code in {
        SignalRuleCode.COMPLETION_DATE_EXTENDED,
        SignalRuleCode.COMPLETION_DATE_MOVED_EARLIER,
        SignalRuleCode.REPEATED_EXTENSION,
    }
    results: list[SuggestedReviewAction] = []
    for raw_role in roles:
        normalized = raw_role.strip().lower().replace("-", "_")
        normalized = ROLE_ALIASES.get(normalized, normalized.replace(" ", "_"))
        if completion_rule:
            action = _COMPLETION_ACTIONS.get(normalized, "Review the completion-date evidence in the relevant operational record.")
        else:
            action = _GENERIC_ACTIONS.get(code, "Review the supporting evidence before changing downstream records.")
        results.append(
            SuggestedReviewAction(
                role=normalized,
                rule_code=code,
                action=action,
                reason=f"Generated from deterministic signal rule {code.value}.",
            )
        )
    return tuple(results)


class BusinessImpactRouter:
    def route(self, rule_code: SignalRuleCode | str, roles: Iterable[str]) -> tuple[SuggestedReviewAction, ...]:
        return suggested_review_actions(rule_code, roles)

