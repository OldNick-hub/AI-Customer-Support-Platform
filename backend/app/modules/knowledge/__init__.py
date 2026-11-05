"""Knowledge Base Update Pipeline & Document Management Module.

Provides change detection, document sanitization, prompt injection defense,
quarantine management, and deduplication services.
"""

from app.modules.knowledge.change_detector import (
    ChangeDetectionSummary,
    ChangeDetector,
    DocumentChangeItem,
)
from app.modules.knowledge.chunker import (
    ExtractedChunk,
    StructureAwareChunker,
)
from app.modules.knowledge.sanitizer import (
    IngestionSanitizer,
    SanitizationResult,
)
from app.modules.knowledge.vector_builder import (
    CandidateBuildResult,
    CandidateVectorBuilder,
)
from app.modules.knowledge.benchmark_evaluator import (
    BenchmarkEvaluator,
    BenchmarkResult,
    BenchmarkSuite,
    BenchmarkTestCase,
    QueryEvaluationResult,
)
from app.modules.knowledge.maintenance import (
    MaintenanceSchedule,
    MaintenanceWindowEligibility,
    MaintenanceWindowService,
    TimeRange,
)
from app.modules.knowledge.promoter import (
    IndexPromoter,
    PromotionResult,
    ReconciliationResult,
)
from app.modules.knowledge.reader import (
    ActiveIndexReader,
)
from app.modules.knowledge.rollback_manager import (
    RollbackManager,
    RollbackResult,
)
from app.modules.knowledge.watchdog import (
    CanaryCheckResult,
    IndexHealthWatchdog,
    WatchdogEvaluationResult,
)

__all__ = [
    "ChangeDetector",
    "ChangeDetectionSummary",
    "DocumentChangeItem",
    "IngestionSanitizer",
    "SanitizationResult",
    "StructureAwareChunker",
    "ExtractedChunk",
    "CandidateVectorBuilder",
    "CandidateBuildResult",
    "BenchmarkEvaluator",
    "BenchmarkResult",
    "BenchmarkSuite",
    "BenchmarkTestCase",
    "QueryEvaluationResult",
    "MaintenanceSchedule",
    "MaintenanceWindowEligibility",
    "MaintenanceWindowService",
    "TimeRange",
    "IndexPromoter",
    "PromotionResult",
    "ReconciliationResult",
    "ActiveIndexReader",
    "RollbackManager",
    "RollbackResult",
    "IndexHealthWatchdog",
    "CanaryCheckResult",
    "WatchdogEvaluationResult",
]

