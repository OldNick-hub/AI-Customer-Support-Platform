"""Tests for Conversation Rename (PATCH) and Delete (DELETE) Endpoints.

Verifies:
1. Customer can rename their conversation; returned title is updated and persisted.
2. Customer can delete their conversation; conversation and messages cascade cleanly.
3. Customer isolation: Unowned conversations return 404 for PATCH and DELETE.
"""

from datetime import datetime, timedelta, timezone
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.main import app
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from tests.test_staff_conversation_fixes import create_test_customer


@pytest.mark.asyncio
async def test_customer_rename_and_delete_conversation(session: AsyncSession):
    """Verifies that a customer can rename and delete their own conversation thread."""
    app.dependency_overrides[get_db] = lambda: session

    # 1. Setup Customer and Session
    cust_data = await create_test_customer(session, email="rename_cust@test.internal", full_name="Rename Test")
    cust = cust_data["customer"]
    cust_sess = cust_data["session"]
    token = cust_data["session_token"]

    # 2. Setup Conversation & Messages
    conv = Conversation(
        session_id=cust_sess.id,
        customer_id=cust.id,
        primary_language="en",
    )
    session.add(conv)
    await session.flush()

    msg = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content="I have an inquiry about my order.",
    )
    session.add(msg)
    await session.commit()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-Session-Token": token}

            # 3. Test PATCH (Rename)
            patch_res = await client.patch(
                f"/api/v1/conversations/{conv.id}",
                json={"custom_title": "Order ORD-9812 Dispute"},
                headers=headers,
            )
            assert patch_res.status_code == 200, patch_res.text
            data = patch_res.json()
            assert data["custom_title"] == "Order ORD-9812 Dispute"
            assert data["id"] == str(conv.id)

            # Verify GET returns updated custom_title
            get_res = await client.get(f"/api/v1/conversations/{conv.id}", headers=headers)
            assert get_res.status_code == 200
            assert get_res.json()["custom_title"] == "Order ORD-9812 Dispute"

            # 4. Test DELETE
            del_res = await client.delete(f"/api/v1/conversations/{conv.id}", headers=headers)
            assert del_res.status_code == 200
            assert del_res.json()["success"] is True

            # Verify subsequent GET returns 404
            get_after_del = await client.get(f"/api/v1/conversations/{conv.id}", headers=headers)
            assert get_after_del.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_conversation_ownership_isolation(session: AsyncSession):
    """Verifies that unauthorized customers cannot rename or delete another customer's thread."""
    app.dependency_overrides[get_db] = lambda: session

    cust_a_data = await create_test_customer(session, email="cust_a@test.internal", full_name="Customer A")
    conv_a = Conversation(
        session_id=cust_a_data["session"].id,
        customer_id=cust_a_data["customer"].id,
        primary_language="en",
    )
    session.add(conv_a)

    cust_b_data = await create_test_customer(session, email="cust_b@test.internal", full_name="Customer B")
    await session.commit()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Customer B attempts to rename Customer A's conversation
            patch_res = await client.patch(
                f"/api/v1/conversations/{conv_a.id}",
                json={"custom_title": "Malicious Hijack"},
                headers={"X-Session-Token": cust_b_data["session_token"]},
            )
            assert patch_res.status_code == 404

            # Customer B attempts to delete Customer A's conversation
            del_res = await client.delete(
                f"/api/v1/conversations/{conv_a.id}",
                headers={"X-Session-Token": cust_b_data["session_token"]},
            )
            assert del_res.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)
