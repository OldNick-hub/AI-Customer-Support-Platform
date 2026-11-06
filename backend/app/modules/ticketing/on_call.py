"""On-Call Support Workflow Dispatcher.

Handles urgent after-hours handoffs to configured on-call systems (e.g., PagerDuty,
incident webhooks). Strictly prohibits fabricated deliveries: statuses are honestly
marked as CONFIRMED only upon actual delivery, or FAILED if unconfigured or unreachable.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


@dataclass(frozen=True)
class OnCallDispatchResult:
    """Immutable audit outcome of an on-call dispatch attempt."""

    status: str  # "CONFIRMED", "FAILED"
    destination: str
    dispatched_at: datetime
    dispatched_payload: Dict[str, Any]
    response_code: Optional[int] = None
    failure_details: Optional[str] = None


class OnCallDispatcher:
    """Dispatches urgent incidents to configured on-call destinations."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        api_key: Optional[str] = None,
        enabled: Optional[bool] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.webhook_url = webhook_url or getattr(settings, "ON_CALL_WEBHOOK_URL", None)
        self.api_key = api_key or getattr(settings, "ON_CALL_API_KEY", None)
        self.enabled = (
            enabled if enabled is not None else getattr(settings, "ON_CALL_WORKFLOW_ENABLED", True)
        )
        self._http_client = http_client

    async def dispatch(
        self,
        incident_id: str,
        priority: str,
        summary: str,
        urgency_score: float,
        conversation_id: Optional[str] = None,
        ticket_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OnCallDispatchResult:
        """Dispatches an urgent after-hours incident notification.

        CRITICAL INTEGRITY REQUIREMENT:
        Never returns CONFIRMED unless the downstream destination successfully acknowledged receipt.
        If no webhook is configured or the request fails, status is marked as FAILED with
        verifiable failure details.
        """
        now = datetime.now(timezone.utc)
        target = self.webhook_url or "UNCONFIGURED_ON_CALL_CHANNEL"

        payload = {
            "incident_id": incident_id,
            "priority": priority,
            "summary": summary,
            "urgency_score": urgency_score,
            "conversation_id": conversation_id,
            "ticket_id": ticket_id,
            "metadata": metadata or {},
            "timestamp": now.isoformat(),
        }

        if not self.enabled:
            logger.info("On-call dispatch skipped: on-call workflow is disabled in configuration.")
            return OnCallDispatchResult(
                status="FAILED",
                destination="DISABLED",
                dispatched_at=now,
                dispatched_payload=payload,
                failure_details="On-call workflow is administratively disabled in settings.",
            )

        if not self.webhook_url or not self.webhook_url.strip():
            logger.warning(
                "Urgent incident '%s' could not be delivered to on-call: destination unconfigured.",
                incident_id,
            )
            return OnCallDispatchResult(
                status="FAILED",
                destination=target,
                dispatched_at=now,
                dispatched_payload=payload,
                failure_details="No valid ON_CALL_WEBHOOK_URL is configured in settings. Notification not delivered.",
            )

        # Attempt HTTP transmission
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AI-Customer-Support-OnCall/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            if self._http_client:
                resp = await self._http_client.post(
                    self.webhook_url, json=payload, headers=headers, timeout=5.0
                )
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        self.webhook_url, json=payload, headers=headers, timeout=5.0
                    )

            if 200 <= resp.status_code < 300:
                logger.info(
                    "On-call dispatch CONFIRMED for incident '%s' (HTTP %s).",
                    incident_id,
                    resp.status_code,
                )
                return OnCallDispatchResult(
                    status="CONFIRMED",
                    destination=self.webhook_url,
                    dispatched_at=now,
                    dispatched_payload=payload,
                    response_code=resp.status_code,
                    failure_details=None,
                )
            else:
                fail_msg = f"Downstream returned non-2xx status code {resp.status_code}: {resp.text[:200]}"
                logger.error(
                    "On-call dispatch FAILED for incident '%s': %s", incident_id, fail_msg
                )
                return OnCallDispatchResult(
                    status="FAILED",
                    destination=self.webhook_url,
                    dispatched_at=now,
                    dispatched_payload=payload,
                    response_code=resp.status_code,
                    failure_details=fail_msg,
                )

        except Exception as exc:
            fail_msg = f"Network or connection error during on-call dispatch: {type(exc).__name__}: {str(exc)}"
            logger.error("On-call dispatch exception for incident '%s': %s", incident_id, fail_msg)
            return OnCallDispatchResult(
                status="FAILED",
                destination=self.webhook_url,
                dispatched_at=now,
                dispatched_payload=payload,
                failure_details=fail_msg,
            )


# Default singleton instance
default_on_call_dispatcher = OnCallDispatcher()

