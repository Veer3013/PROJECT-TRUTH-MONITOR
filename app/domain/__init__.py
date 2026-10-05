"""Pure deterministic domain types used by collectors, parsers and services."""

from .changes import DetectedChange
from .enums import (
    AccessCompleteness,
    AccessStatus,
    ChangeType,
    ComparisonState,
    ConfidenceBand,
    ConflictState,
    ErrorCategory,
    ReviewRequirement,
    ReviewState,
    Severity,
    SourceClass,
    SourceHealthStatus,
    ValidationStatus,
)
from .facts import EvidenceReference, ExtractedField, FactValue, ParsedSourceSnapshot
from .signals import Signal, SignalRuleCode
from .source import (
    FetchRequestContext,
    FetchResult,
    RequestContext,
    SourceAccessPolicy,
    SourceHealth,
    SourcePolicy,
)

__all__ = [
    "AccessCompleteness",
    "AccessStatus",
    "ChangeType",
    "ComparisonState",
    "ConfidenceBand",
    "ConflictState",
    "ErrorCategory",
    "DetectedChange",
    "EvidenceReference",
    "ExtractedField",
    "FactValue",
    "FetchRequestContext",
    "FetchResult",
    "ParsedSourceSnapshot",
    "RequestContext",
    "ReviewRequirement",
    "ReviewState",
    "Severity",
    "Signal",
    "SignalRuleCode",
    "SourceAccessPolicy",
    "SourceClass",
    "SourceHealth",
    "SourceHealthStatus",
    "SourcePolicy",
    "ValidationStatus",
]
