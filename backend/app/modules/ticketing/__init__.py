"""Ticketing Module Exports.

Exposes the Intelligent Ticket Management Engine components:
- TicketSynthesisEngine: LLM-backed conversation-to-structured-ticket extraction.
- TicketCompletenessEvaluator: Deterministic category-specific completeness scoring.
- TicketClarificationEngine: Targeted, non-redundant clarification question generation.
- TicketSynthesisService: Lifecycle coordination, state machine, and DB persistence.
"""

from app.modules.ticketing.clustering import (
    DuplicateMatchResult,
    IssueGroupResult,
    TicketClusterMatcher,
)
from app.modules.ticketing.after_hours import (
    add_business_minutes,
    calculate_sla_deadlines,
    evaluate_after_hours,
    is_after_hours,
)
from app.modules.ticketing.goodwill_policy import (
    GoodwillCreditCalculationResult,
    GoodwillCreditPolicy,
    get_goodwill_credit_policy,
)
from app.modules.ticketing.priority_engine import (
    PriorityEngine,
    PriorityEvaluationRequest,
    PriorityEvaluationResult,
    PriorityLevel,
    get_priority_engine,
)
from app.modules.ticketing.routing_engine import (
    AgentCandidate,
    AgentCapacityExceededError,
    AgentNotFoundError,
    AgentRoleNotAuthorizedError,
    CATEGORY_DEPARTMENT_MAP,
    DEPARTMENT_SKILLS_MAP,
    RoutingDecision,
    RoutingEngine,
    acquire_agent_capacity_lock,
    get_routing_engine,
)
from app.modules.ticketing.action_orchestrator import (
    ActionConcurrencyConflictError,
    ActionMaxAttemptsExceededError,
    ActionNotApprovedError,
    ActionNotFoundError,
    ActionOrchestrationError,
    ActionOrchestrator,
    compute_action_params_hash,
    get_action_orchestrator,
)
from app.modules.ticketing.resolution_engine import (
    ResolutionEngine,
    get_resolution_engine,
)
from app.modules.ticketing.resolution_playbooks import (
    BasePlaybook,
    PlaybookCategory,
    PlaybookEvaluationResult,
    PlaybookRegistry,
    ProposedActionPlan,
    get_playbook_registry,
    normalize_iso4217_currency,
)
from app.modules.ticketing.resolution_service import (
    ResolutionAuthorizationError,
    ResolutionError,
    ResolutionNotFoundError,
    ResolutionVerificationError,
    TicketResolutionService,
    get_ticket_resolution_service,
)
from app.modules.ticketing.routing_service import (
    TicketRoutingAuthorizationError,
    TicketRoutingService,
    get_ticket_routing_service,
)
from app.modules.ticketing.ticket_clarification import (
    ClarificationQuestion,
    ClarificationRoundRequest,
    ClarificationRoundResult,
    CustomerResponseResult,
    TicketClarificationEngine,
    get_clarification_engine,
    MAX_CLARIFICATION_ROUNDS,
    MAX_QUESTIONS_PER_ROUND,
)
from app.modules.ticketing.ticket_completeness import (
    CompletenessResult,
    CompletenessViolation,
    CATEGORY_REQUIREMENTS,
    FieldRequirement,
    TicketCompletenessEvaluator,
    get_completeness_evaluator,
)
from app.modules.ticketing.ticket_service import (
    InvalidSynthesisTransitionError,
    SYNTHESIS_STATUS_COMPLETE,
    SYNTHESIS_STATUS_DRAFT,
    SYNTHESIS_STATUS_NEEDS_INFORMATION,
    SYNTHESIS_STATUS_READY_FOR_REVIEW,
    SYNTHESIS_STATUS_SYNTHESIZED,
    SYNTHESIS_STATUS_SYNTHESIZING,
    TicketAuthorizationError,
    TicketNotFoundError,
    TicketSynthesisConflictError,
    TicketSynthesisService,
    get_ticket_synthesis_service,
)
from app.modules.ticketing.ticket_synthesis import (
    SynthesisRequest,
    SynthesisResult,
    TicketSynthesisEngine,
    get_synthesis_engine,
)

__all__ = [
    # Synthesis Engine
    "SynthesisRequest",
    "SynthesisResult",
    "TicketSynthesisEngine",
    "get_synthesis_engine",
    # Completeness Evaluator
    "CompletenessResult",
    "CompletenessViolation",
    "FieldRequirement",
    "CATEGORY_REQUIREMENTS",
    "TicketCompletenessEvaluator",
    "get_completeness_evaluator",
    # Clarification Engine
    "ClarificationQuestion",
    "ClarificationRoundRequest",
    "ClarificationRoundResult",
    "CustomerResponseResult",
    "TicketClarificationEngine",
    "get_clarification_engine",
    "MAX_CLARIFICATION_ROUNDS",
    "MAX_QUESTIONS_PER_ROUND",
    # Service & Lifecycle
    "TicketSynthesisService",
    "get_ticket_synthesis_service",
    "TicketNotFoundError",
    "TicketAuthorizationError",
    "TicketSynthesisConflictError",
    "InvalidSynthesisTransitionError",
    "SYNTHESIS_STATUS_DRAFT",
    "SYNTHESIS_STATUS_SYNTHESIZING",
    "SYNTHESIS_STATUS_SYNTHESIZED",
    "SYNTHESIS_STATUS_NEEDS_INFORMATION",
    "SYNTHESIS_STATUS_READY_FOR_REVIEW",
    "SYNTHESIS_STATUS_COMPLETE",
    # Priority Engine
    "PriorityEngine",
    "PriorityEvaluationRequest",
    "PriorityEvaluationResult",
    "PriorityLevel",
    "get_priority_engine",
    # Routing Engine & Capacity Locker
    "RoutingEngine",
    "RoutingDecision",
    "AgentCandidate",
    "AgentCapacityExceededError",
    "AgentNotFoundError",
    "AgentRoleNotAuthorizedError",
    "acquire_agent_capacity_lock",
    "get_routing_engine",
    "CATEGORY_DEPARTMENT_MAP",
    "DEPARTMENT_SKILLS_MAP",
    # Routing Service
    "TicketRoutingService",
    "TicketRoutingAuthorizationError",
    "get_ticket_routing_service",
    # Resolution Engine & Playbooks
    "PlaybookCategory",
    "BasePlaybook",
    "ProposedActionPlan",
    "PlaybookEvaluationResult",
    "PlaybookRegistry",
    "get_playbook_registry",
    "ResolutionEngine",
    "get_resolution_engine",
    "normalize_iso4217_currency",
    # Smart Goodwill Credit Policy
    "GoodwillCreditPolicy",
    "GoodwillCreditCalculationResult",
    "get_goodwill_credit_policy",
    # Action Orchestration
    "ActionOrchestrationError",
    "ActionNotFoundError",
    "ActionNotApprovedError",
    "ActionMaxAttemptsExceededError",
    "ActionConcurrencyConflictError",
    "ActionOrchestrator",
    "get_action_orchestrator",
    # Resolution Service
    "TicketResolutionService",
    "ResolutionError",
    "ResolutionNotFoundError",
    "ResolutionAuthorizationError",
    "ResolutionVerificationError",
    "get_ticket_resolution_service",
    # Clustering & SLA Recalculation
    "TicketClusterMatcher",
    "DuplicateMatchResult",
    "IssueGroupResult",
    "calculate_sla_deadlines",
    "add_business_minutes",
    "evaluate_after_hours",
    "is_after_hours",
]
