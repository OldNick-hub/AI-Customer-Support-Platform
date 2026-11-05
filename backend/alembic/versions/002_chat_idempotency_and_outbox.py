"""add chat_idempotency_records, chat_outbox_events tables, messages.turn_sequence_number, and triggers

Revision ID: 002_chat_idempotency_and_outbox
Revises: 001_initial_schema
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002_chat_idempotency_and_outbox"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None

JSONBType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # 1. messages: add turn_sequence_number and composite unique index for composite FKs
    op.add_column("messages", sa.Column("turn_sequence_number", sa.Integer(), nullable=True))
    op.create_index("ix_messages_turn_seq", "messages", ["conversation_id", "turn_sequence_number"])
    op.create_index("uq_messages_id_conversation", "messages", ["id", "conversation_id"], unique=True)
    
    # Partial unique index: at most one customer and one AI agent message per turn
    op.create_index(
        "uq_messages_conv_turn_sender",
        "messages",
        ["conversation_id", "turn_sequence_number", "sender_type"],
        unique=True,
        postgresql_where=sa.text("turn_sequence_number IS NOT NULL"),
    )

    # 2. Historical Message Backfill (Executed BEFORE triggers are installed)
    op.execute("""
    WITH candidate_conversations AS (
        SELECT conversation_id
        FROM messages
        GROUP BY conversation_id
        HAVING 
            COUNT(*) FILTER (WHERE sender_type NOT IN ('CUSTOMER', 'AI_AGENT')) = 0
            AND COUNT(*) FILTER (WHERE sender_type = 'CUSTOMER') = COUNT(*) FILTER (WHERE sender_type = 'AI_AGENT')
            AND COUNT(*) = (SELECT turn_count * 2 FROM conversations WHERE id = messages.conversation_id)
            AND COUNT(DISTINCT created_at) = COUNT(*)
    ),
    ordered_turns AS (
        SELECT 
            m.id,
            m.conversation_id,
            m.sender_type,
            m.created_at,
            ROW_NUMBER() OVER (PARTITION BY m.conversation_id ORDER BY m.created_at ASC) AS row_num
        FROM messages m
        WHERE m.conversation_id IN (SELECT conversation_id FROM candidate_conversations)
    ),
    verified_conversations AS (
        SELECT conversation_id
        FROM ordered_turns
        GROUP BY conversation_id
        HAVING 
            COUNT(*) FILTER (WHERE (row_num % 2 = 1 AND sender_type != 'CUSTOMER') OR (row_num % 2 = 0 AND sender_type != 'AI_AGENT')) = 0
    )
    UPDATE messages m
    SET turn_sequence_number = CEIL(ot.row_num / 2.0)::integer
    FROM ordered_turns ot
    WHERE m.id = ot.id
      AND m.conversation_id IN (SELECT conversation_id FROM verified_conversations);
    """)

    # 3. Message Sequence Insertion Validation Trigger
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_validate_message_turn_sequence_insert()
    RETURNS TRIGGER AS $$
    DECLARE
        v_current_turn INT;
    BEGIN
        IF NEW.turn_sequence_number IS NOT NULL THEN
            IF NEW.sender_type NOT IN ('CUSTOMER', 'AI_AGENT') THEN
                RAISE EXCEPTION 'NON_DIALOG_MESSAGE_SEQUENCE_FORBIDDEN: sender_type % cannot have turn_sequence_number', NEW.sender_type;
            END IF;

            SELECT turn_count INTO v_current_turn
            FROM conversations WHERE id = NEW.conversation_id;

            IF v_current_turn IS NULL THEN
                RAISE EXCEPTION 'CONVERSATION_NOT_FOUND: conversation % does not exist', NEW.conversation_id;
            END IF;

            IF NEW.turn_sequence_number != v_current_turn THEN
                RAISE EXCEPTION 'SEQUENCE_ALLOCATION_MISMATCH: turn_sequence_number % does not match conversations.turn_count %',
                    NEW.turn_sequence_number, v_current_turn;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    CREATE TRIGGER tg_validate_message_turn_sequence_insert
    BEFORE INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION fn_validate_message_turn_sequence_insert();
    """)

    # 4. Message Immutability & Deletion Prohibition Trigger (NULL-Safe via IS DISTINCT FROM)
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_message_turn_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        -- Forbid deleting numbered messages
        IF TG_OP = 'DELETE' THEN
            IF OLD.turn_sequence_number IS NOT NULL THEN
                RAISE EXCEPTION 'NUMBERED_MESSAGE_DELETION_PROHIBITED: Cannot delete committed dialog message % with turn_sequence_number %',
                    OLD.id, OLD.turn_sequence_number;
            END IF;
            RETURN OLD;
        END IF;

        -- Forbid assigning sequence numbers to legacy unsequenced messages post-migration
        IF OLD.turn_sequence_number IS NULL AND NEW.turn_sequence_number IS NOT NULL THEN
            RAISE EXCEPTION 'LEGACY_SEQUENCE_MUTATION_FORBIDDEN: Cannot assign turn_sequence_number to unsequenced message % post-migration', OLD.id;
        END IF;

        -- Enforce strict immutability once turn_sequence_number is assigned
        IF OLD.turn_sequence_number IS NOT NULL THEN
            IF NEW.turn_sequence_number IS DISTINCT FROM OLD.turn_sequence_number THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify turn_sequence_number on committed message % (from % to %)',
                    OLD.id, OLD.turn_sequence_number, NEW.turn_sequence_number;
            END IF;
            IF NEW.conversation_id IS DISTINCT FROM OLD.conversation_id THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot reassign conversation_id on committed message %', OLD.id;
            END IF;
            IF NEW.sender_type IS DISTINCT FROM OLD.sender_type THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify sender_type on committed message % (from % to %)',
                    OLD.id, OLD.sender_type, NEW.sender_type;
            END IF;
            IF NEW.sender_id IS DISTINCT FROM OLD.sender_id THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify sender_id on committed message %', OLD.id;
            END IF;
            IF NEW.content IS DISTINCT FROM OLD.content THEN
                RAISE EXCEPTION 'MESSAGE_IMMUTABILITY_VIOLATION: Cannot modify content on committed message %', OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    CREATE TRIGGER tg_enforce_message_turn_immutability
    BEFORE UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_message_turn_immutability();
    """)

    # 5. chat_idempotency_records table
    op.create_table(
        "chat_idempotency_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("fingerprint_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="IN_PROGRESS"),
        sa.Column("owner_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encrypted_response_payload", sa.LargeBinary(), nullable=True),
        sa.Column("response_status_code", sa.Integer(), nullable=True, server_default="200"),
        sa.Column("customer_message_id", sa.String(36), nullable=True),
        sa.Column("agent_message_id", sa.String(36), nullable=True),
        sa.Column("turn_sequence_number", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_chat_idempotency_cust_sess_key",
        "chat_idempotency_records",
        ["customer_id", "session_id", "idempotency_key"],
    )
    op.create_index("ix_chat_idempotency_cust_sess", "chat_idempotency_records", ["customer_id", "session_id"])
    op.create_index("ix_chat_idempotency_conv_id", "chat_idempotency_records", ["conversation_id"])
    op.create_index("ix_chat_idempotency_lease", "chat_idempotency_records", ["status", "lease_expires_at"])
    op.create_index("ix_chat_idempotency_expires", "chat_idempotency_records", ["expires_at"])

    # 6. chat_outbox_events with RESTRICT Foreign Keys
    op.create_table(
        "chat_outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_message_id", sa.String(36), nullable=False),
        sa.Column("agent_message_id", sa.String(36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False, server_default="CONVERSATION_TURN_COMPLETED"),
        sa.Column("payload", JSONBType, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_outbox_cust_msg_conv",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_outbox_agent_msg_conv",
            ondelete="RESTRICT",
        ),
    )
    op.create_unique_constraint(
        "uq_chat_outbox_conv_seq",
        "chat_outbox_events",
        ["conversation_id", "sequence_number"],
    )
    op.create_unique_constraint("uq_chat_outbox_cust_msg", "chat_outbox_events", ["customer_message_id"])
    op.create_unique_constraint("uq_chat_outbox_agent_msg", "chat_outbox_events", ["agent_message_id"])
    op.create_index("ix_chat_outbox_status_retry", "chat_outbox_events", ["status", "next_retry_at"])
    op.create_index("ix_chat_outbox_conv_seq", "chat_outbox_events", ["conversation_id", "sequence_number"])
    op.create_index("ix_chat_outbox_cust_sess", "chat_outbox_events", ["customer_id", "session_id"])

    # 7. Outbox Trigger: Validate Message Linkage, Sender Types, and Turn Sequences
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_validate_chat_outbox_turn()
    RETURNS TRIGGER AS $$
    DECLARE
        v_cust_sender VARCHAR(20);
        v_cust_conv VARCHAR(36);
        v_cust_seq INT;
        v_agent_sender VARCHAR(20);
        v_agent_conv VARCHAR(36);
        v_agent_seq INT;
    BEGIN
        SELECT sender_type, conversation_id, turn_sequence_number
        INTO v_cust_sender, v_cust_conv, v_cust_seq
        FROM messages WHERE id = NEW.customer_message_id;

        IF v_cust_sender IS NULL THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id % does not exist', NEW.customer_message_id;
        END IF;
        IF v_cust_sender != 'CUSTOMER' THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id % has sender_type %, expected CUSTOMER', NEW.customer_message_id, v_cust_sender;
        END IF;
        IF v_cust_conv != NEW.conversation_id THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id belongs to conv %, expected %', v_cust_conv, NEW.conversation_id;
        END IF;
        IF v_cust_seq != NEW.sequence_number THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: customer_message_id has turn_seq %, expected %', v_cust_seq, NEW.sequence_number;
        END IF;

        SELECT sender_type, conversation_id, turn_sequence_number
        INTO v_agent_sender, v_agent_conv, v_agent_seq
        FROM messages WHERE id = NEW.agent_message_id;

        IF v_agent_sender IS NULL THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id % does not exist', NEW.agent_message_id;
        END IF;
        IF v_agent_sender != 'AI_AGENT' THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id % has sender_type %, expected AI_AGENT', NEW.agent_message_id, v_agent_sender;
        END IF;
        IF v_agent_conv != NEW.conversation_id THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id belongs to conv %, expected %', v_agent_conv, NEW.conversation_id;
        END IF;
        IF v_agent_seq != NEW.sequence_number THEN
            RAISE EXCEPTION 'OUTBOX_INTEGRITY_VIOLATION: agent_message_id has turn_seq %, expected %', v_agent_seq, NEW.sequence_number;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    CREATE TRIGGER tg_validate_chat_outbox_turn
    BEFORE INSERT OR UPDATE ON chat_outbox_events
    FOR EACH ROW EXECUTE FUNCTION fn_validate_chat_outbox_turn();
    """)

    # 8. Atomic Customer/AI Dialog Pair & Outbox Integrity Constraint Trigger (DEFERRABLE)
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_validate_conversation_turn_pair_completeness()
    RETURNS TRIGGER AS $$
    DECLARE
        v_cust_count INT;
        v_agent_count INT;
        v_turn_count INT;
        v_outbox_count INT;
    BEGIN
        IF NEW.turn_sequence_number IS NOT NULL THEN
            -- 1. Verify exact customer/AI dialog pairing for this turn sequence
            SELECT 
                COUNT(*) FILTER (WHERE sender_type = 'CUSTOMER'),
                COUNT(*) FILTER (WHERE sender_type = 'AI_AGENT')
            INTO v_cust_count, v_agent_count
            FROM messages
            WHERE conversation_id = NEW.conversation_id
              AND turn_sequence_number = NEW.turn_sequence_number;

            IF v_cust_count != 1 OR v_agent_count != 1 THEN
                RAISE EXCEPTION 'INCOMPLETE_TURN_PAIR: Turn % in conversation % must contain exactly 1 CUSTOMER message and 1 AI_AGENT message at commit time (found % customer, % agent)',
                    NEW.turn_sequence_number, NEW.conversation_id, v_cust_count, v_agent_count;
            END IF;

            -- 2. Verify sequence number matches allocated turn_count
            SELECT turn_count INTO v_turn_count
            FROM conversations
            WHERE id = NEW.conversation_id;

            IF v_turn_count IS NULL OR v_turn_count < NEW.turn_sequence_number THEN
                RAISE EXCEPTION 'UNALLOCATED_TURN_SEQUENCE: Turn % exceeds conversations.turn_count % in conversation %',
                    NEW.turn_sequence_number, COALESCE(v_turn_count, -1), NEW.conversation_id;
            END IF;

            -- 3. Verify exactly 1 chat_outbox_events record exists for this turn sequence
            SELECT COUNT(*) INTO v_outbox_count
            FROM chat_outbox_events
            WHERE conversation_id = NEW.conversation_id
              AND sequence_number = NEW.turn_sequence_number;

            IF v_outbox_count != 1 THEN
                RAISE EXCEPTION 'MISSING_OUTBOX_EVENT: Turn % in conversation % must have exactly 1 chat_outbox_events record at commit time (found %)',
                    NEW.turn_sequence_number, NEW.conversation_id, v_outbox_count;
            END IF;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    CREATE CONSTRAINT TRIGGER tg_enforce_turn_pair_completeness
    AFTER INSERT OR UPDATE ON messages
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_validate_conversation_turn_pair_completeness();
    """)

    # 9. Atomic Turn Sequence Allocator (SECURITY DEFINER)
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_allocate_next_turn_sequence(p_conversation_id VARCHAR)
    RETURNS INTEGER
    SECURITY DEFINER
    SET search_path = public, pg_temp
    LANGUAGE plpgsql AS $$
    DECLARE
        v_allocated_turn INT;
    BEGIN
        UPDATE conversations
        SET turn_count = turn_count + 1,
            last_message_at = NOW()
        WHERE id = p_conversation_id::text
        RETURNING turn_count INTO v_allocated_turn;

        IF v_allocated_turn IS NULL THEN
            RAISE EXCEPTION 'CONVERSATION_NOT_FOUND: conversation % does not exist', p_conversation_id;
        END IF;

        RETURN v_allocated_turn;
    END;
    $$;
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION fn_allocate_next_turn_sequence(p_conversation_id UUID)
    RETURNS INTEGER
    SECURITY DEFINER
    SET search_path = public, pg_temp
    LANGUAGE plpgsql AS $$
    BEGIN
        RETURN fn_allocate_next_turn_sequence(p_conversation_id::text);
    END;
    $$;
    """)

    # 10. Monotonic Turn Count Trigger on conversations
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_turn_count_monotonicity()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NEW.turn_count IS DISTINCT FROM OLD.turn_count THEN
            IF NEW.turn_count != OLD.turn_count + 1 THEN
                RAISE EXCEPTION 'INVALID_TURN_COUNT_MUTATION: turn_count cannot be arbitrarily modified. It must increment by exactly 1 (from % to %)',
                    OLD.turn_count, NEW.turn_count;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    CREATE TRIGGER tg_enforce_turn_count_monotonicity
    BEFORE UPDATE OF turn_count ON conversations
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_turn_count_monotonicity();
    """)

    # 11. Immutability & Deletion Prohibition on chat_idempotency_records
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_idempotency_record_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'IDEMPOTENCY_DELETION_PROHIBITED: Deleting chat_idempotency_records is strictly forbidden';
        END IF;

        IF OLD.status = 'COMPLETED' THEN
            IF NEW.status IS DISTINCT FROM OLD.status OR
               NEW.encrypted_response_payload IS DISTINCT FROM OLD.encrypted_response_payload OR
               NEW.turn_sequence_number IS DISTINCT FROM OLD.turn_sequence_number OR
               NEW.customer_message_id IS DISTINCT FROM OLD.customer_message_id OR
               NEW.agent_message_id IS DISTINCT FROM OLD.agent_message_id THEN
                RAISE EXCEPTION 'COMPLETED_IDEMPOTENCY_IMMUTABLE: Completed idempotency record % cannot be mutated', OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    CREATE TRIGGER tg_enforce_idempotency_record_immutability
    BEFORE UPDATE OR DELETE ON chat_idempotency_records
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_idempotency_record_immutability();
    """)

    # 12. Immutability & Deletion Prohibition on chat_outbox_events
    op.execute("""
    CREATE OR REPLACE FUNCTION fn_enforce_outbox_event_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            IF OLD.status = 'DELIVERED' AND OLD.delivered_at IS NOT NULL AND OLD.delivered_at <= NOW() - INTERVAL '7 days' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'OUTBOX_DELETION_PROHIBITED: Deleting chat_outbox_events is strictly forbidden. Use retention archival.';
        END IF;

        IF NEW.id IS DISTINCT FROM OLD.id OR
           NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
           NEW.conversation_id IS DISTINCT FROM OLD.conversation_id OR
           NEW.sequence_number IS DISTINCT FROM OLD.sequence_number OR
           NEW.customer_message_id IS DISTINCT FROM OLD.customer_message_id OR
           NEW.agent_message_id IS DISTINCT FROM OLD.agent_message_id OR
           NEW.payload::text IS DISTINCT FROM OLD.payload::text OR
           NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'OUTBOX_IMMUTABILITY_VIOLATION: Core event headers and payload on outbox event % cannot be mutated', OLD.id;
        END IF;

        IF OLD.status = 'DELIVERED' AND NEW.status != 'DELIVERED' THEN
            RAISE EXCEPTION 'DELIVERED_OUTBOX_REGRESSION_FORBIDDEN: Event % is already delivered', OLD.id;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    CREATE TRIGGER tg_enforce_outbox_event_immutability
    BEFORE UPDATE OR DELETE ON chat_outbox_events
    FOR EACH ROW EXECUTE FUNCTION fn_enforce_outbox_event_immutability();
    """)

    # 13. Grant explicit least-privilege permissions to app_user and ensure ownership
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            GRANT SELECT, INSERT ON chat_idempotency_records TO app_user;
            GRANT UPDATE (status, response_status_code, encrypted_response_payload, customer_message_id, agent_message_id, turn_sequence_number, owner_token, lease_expires_at, heartbeat_at, failure_reason, attempt_count, updated_at) ON chat_idempotency_records TO app_user;
            REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES ON chat_idempotency_records FROM app_user;

            GRANT SELECT, INSERT ON chat_outbox_events TO app_user;
            GRANT UPDATE (status, attempt_count, next_retry_at, delivered_at, last_error) ON chat_outbox_events TO app_user;
            REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES ON chat_outbox_events FROM app_user;

            REVOKE ALL ON FUNCTION fn_allocate_next_turn_sequence(VARCHAR) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION fn_allocate_next_turn_sequence(VARCHAR) TO app_user;
            REVOKE ALL ON FUNCTION fn_allocate_next_turn_sequence(UUID) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION fn_allocate_next_turn_sequence(UUID) TO app_user;
        END IF;
        
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_admin') THEN
            ALTER TABLE chat_idempotency_records OWNER TO migration_admin;
            ALTER TABLE chat_outbox_events OWNER TO migration_admin;
            ALTER FUNCTION fn_allocate_next_turn_sequence(VARCHAR) OWNER TO migration_admin;
            ALTER FUNCTION fn_allocate_next_turn_sequence(UUID) OWNER TO migration_admin;
            ALTER FUNCTION fn_validate_message_turn_sequence_insert() OWNER TO migration_admin;
            ALTER FUNCTION fn_enforce_message_turn_immutability() OWNER TO migration_admin;
            ALTER FUNCTION fn_validate_chat_outbox_turn() OWNER TO migration_admin;
            ALTER FUNCTION fn_validate_conversation_turn_pair_completeness() OWNER TO migration_admin;
            ALTER FUNCTION fn_enforce_turn_count_monotonicity() OWNER TO migration_admin;
            ALTER FUNCTION fn_enforce_idempotency_record_immutability() OWNER TO migration_admin;
            ALTER FUNCTION fn_enforce_outbox_event_immutability() OWNER TO migration_admin;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_outbox_event_immutability ON chat_outbox_events;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_outbox_event_immutability();")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_idempotency_record_immutability ON chat_idempotency_records;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_idempotency_record_immutability();")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_turn_count_monotonicity ON conversations;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_turn_count_monotonicity();")
    op.execute("DROP FUNCTION IF EXISTS fn_allocate_next_turn_sequence(VARCHAR);")
    op.execute("DROP FUNCTION IF EXISTS fn_allocate_next_turn_sequence(UUID);")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_turn_pair_completeness ON messages;")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_conversation_turn_pair_completeness();")
    op.execute("DROP TRIGGER IF EXISTS tg_validate_chat_outbox_turn ON chat_outbox_events;")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_chat_outbox_turn();")
    op.drop_table("chat_outbox_events")
    op.drop_table("chat_idempotency_records")
    op.execute("DROP TRIGGER IF EXISTS tg_enforce_message_turn_immutability ON messages;")
    op.execute("DROP FUNCTION IF EXISTS fn_enforce_message_turn_immutability();")
    op.execute("DROP TRIGGER IF EXISTS tg_validate_message_turn_sequence_insert ON messages;")
    op.execute("DROP FUNCTION IF EXISTS fn_validate_message_turn_sequence_insert();")
    op.drop_index("uq_messages_conv_turn_sender", table_name="messages")
    op.drop_index("uq_messages_id_conversation", table_name="messages")
    op.drop_index("ix_messages_turn_seq", table_name="messages")
    op.drop_column("messages", "turn_sequence_number")
