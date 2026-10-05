"""ProjectTruth relational model registry.

Importing this package registers every table with :class:`app.db.Base`. Enum-like
workflow values are stored as ordinary strings so the same migrations work on
SQLite and PostgreSQL.
"""

from app.models.documents import Document, DocumentClaim, DocumentExtraction, DocumentVersion
from app.models.enums import (
    ComparisonState,
    ConfidenceBand,
    MembershipRole,
    MonitoringCadence,
    ReportState,
    ReviewState,
    Severity,
)
from app.models.governance import (
    AuditEvent,
    DeletionTombstone,
    EntityMergeProposal,
    Report,
    ReportItem,
    UserCorrection,
)
from app.models.identity import (
    Developer,
    DeveloperAlias,
    Location,
    Organization,
    OrganizationMembership,
    Project,
    ProjectAlias,
    ProjectSourceLink,
    Source,
    SourceAccessPolicy,
    User,
    Watchlist,
    WatchlistItem,
)
from app.models.intelligence import (
    Alert,
    AlertReview,
    ChangeEvidence,
    DetectedChange,
    MarketSnapshot,
    Signal,
    SourceConflict,
)
from app.models.monitoring import (
    ImmutableSnapshotError,
    MonitoringJob,
    MonitoringRun,
    SnapshotField,
    SourceSnapshot,
)

__all__ = [
    "Alert",
    "AlertReview",
    "AuditEvent",
    "ChangeEvidence",
    "ComparisonState",
    "ConfidenceBand",
    "DeletionTombstone",
    "DetectedChange",
    "Developer",
    "DeveloperAlias",
    "Document",
    "DocumentClaim",
    "DocumentExtraction",
    "DocumentVersion",
    "EntityMergeProposal",
    "ImmutableSnapshotError",
    "Location",
    "MarketSnapshot",
    "MembershipRole",
    "MonitoringCadence",
    "MonitoringJob",
    "MonitoringRun",
    "Organization",
    "OrganizationMembership",
    "Project",
    "ProjectAlias",
    "ProjectSourceLink",
    "Report",
    "ReportItem",
    "ReportState",
    "ReviewState",
    "Severity",
    "Signal",
    "SnapshotField",
    "Source",
    "SourceAccessPolicy",
    "SourceConflict",
    "SourceSnapshot",
    "User",
    "UserCorrection",
    "Watchlist",
    "WatchlistItem",
]
