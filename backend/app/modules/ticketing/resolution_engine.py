"""Resolution Intelligence Engine.

Deterministically evaluates ticket context, synthesis completeness, evidence
reconciliation, and priority against category-specific playbooks to generate
explainable resolution plans and auditable actions.
"""

import logging
from typing import Any, Dict, List, Optional

from app.core.logging import configure_safe_logger
from app.modules.ticketing.resolution_playbooks import (
    BasePlaybook,
    PlaybookCategory,
    PlaybookEvaluationResult,
    PlaybookRegistry,
    ProposedActionPlan,
    get_playbook_registry,
)

logger = configure_safe_logger(logging.getLogger(__name__))


class ResolutionEngine:
    """Evaluates ticket resolution eligibility and derives proposed action plans."""

    def __init__(self, registry: Optional[PlaybookRegistry] = None):
        self.registry = registry or get_playbook_registry()

    def evaluate_ticket(
        self,
        ticket_id: str,
        category: str,
        description: str,
        synthesis_data: Optional[Dict[str, Any]] = None,
        evidence_data: Optional[List[Dict[str, Any]]] = None,
        priority: str = "MEDIUM",
        sla_breached: bool = False,
    ) -> PlaybookEvaluationResult:
        """Evaluates ticket against matching category playbook using deterministic rules."""
        playbook: BasePlaybook = self.registry.resolve_playbook(category, description)
        evidence = evidence_data or []

        logger.info(
            "Evaluating ticket resolution playbook",
            extra={
                "ticket_id": ticket_id,
                "category": category,
                "playbook": playbook.category.value,
            },
        )

        return playbook.evaluate(
            ticket_id=ticket_id,
            category=category,
            description=description,
            synthesis_data=synthesis_data,
            evidence_data=evidence,
            priority=priority,
            sla_breached=sla_breached,
        )


_default_engine: Optional[ResolutionEngine] = None


def get_resolution_engine() -> ResolutionEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = ResolutionEngine()
    return _default_engine

