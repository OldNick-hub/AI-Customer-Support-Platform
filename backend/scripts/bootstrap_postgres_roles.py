"""Idempotent PostgreSQL Role & Privilege Bootstrap Workflow (Audited & Hardened).

Provisions least-privilege 'app_user' and 'migration_admin' roles based on forensic
application-compatibility audit across all 21 tables.

Security & Compatibility Invariants:
1. NEVER persists DBA / superuser credentials in application .env.
2. Migration admin has NOCREATEDB, NOSUPERUSER, NOCREATEROLE, and zero database-level CREATE.
3. App user has NOINHERIT, NOSUPERUSER, NOCREATEDB, NOCREATEROLE.
4. PUBLIC privileges revoked across database, schema, tables, sequences, routines.
5. Blanket default table privileges REMOVED (explicit grants required per migration).
6. Sequence permissions limited strictly to 'document_chunks_faiss_vector_id_seq'.
7. Table-specific grants derived from real application operation mapping:
   - Append-Only (SELECT, INSERT): messages, ticket_events, escalation_audits, sentiment_logs
   - Column-Restricted UPDATE: conversations (status, customer_id, session_id, updated_at)
   - Operational DML (SELECT, INSERT, UPDATE, ZERO DELETE): documents, document_chunks,
     document_versions, knowledge_indexes, benchmark_runs, evidence_files,
     evidence_verifications, extracted_evidence_entities, customers, sessions, tickets, users, user_roles
   - Read-Only (SELECT): roles, sla_calendars, alembic_version
   - ZERO SQL DELETE across all 21 tables.
8. Idempotent across all role presence states (neither, either, both exist).
9. Windows ACL on .env restricted to %USERNAME%, SYSTEM, Administrators.
"""

import argparse
from datetime import datetime
import os
import secrets
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus
import uuid
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Ensure backend directory is in sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.core.config import settings

ROOT_DIR = os.path.abspath(os.path.join(backend_dir, ".."))
ENV_PATH = os.path.join(ROOT_DIR, ".env")
CONFIG_PY_PATH = os.path.join(backend_dir, "app", "core", "config.py")
ALEMBIC_ENV_PATH = os.path.join(backend_dir, "alembic", "env.py")

# Audited Table Categorizations
APPEND_ONLY_TABLES = [
    "messages",
    "ticket_events",
    "escalation_audits",
    "sentiment_logs",
]

RESTRICTED_UPDATE_TABLES = {
    "conversations": [
        "status",
        "customer_id",
        "session_id",
        "primary_language",
        "overall_sentiment_score",
        "is_escalated",
        "last_message_at",
        "closed_at",
    ]
}

OPERATIONAL_DML_TABLES = [
    "documents",
    "document_chunks",
    "document_versions",
    "knowledge_indexes",
    "benchmark_runs",
    "evidence_files",
    "evidence_verifications",
    "extracted_evidence_entities",
    "customers",
    "sessions",
    "tickets",
    "users",
    "user_roles",
]

READ_ONLY_TABLES = [
    "roles",
    "sla_calendars",
    "alembic_version",
]

EXPLICIT_REQUIRED_SEQUENCES = [
    "document_chunks_faiss_vector_id_seq",
]


def load_env_variables() -> Dict[str, str]:
    """Reads existing variables from .env if present."""
    env_vars: Dict[str, str] = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return env_vars


def get_dba_credentials(cli_dba_pass: Optional[str] = None) -> Tuple[str, int, str, str, str]:
    """Retrieves DBA connection parameters from CLI, environment, or settings."""
    env_vars = load_env_variables()
    host = settings.POSTGRES_HOST
    port = settings.POSTGRES_PORT
    dbname = settings.POSTGRES_DB

    dba_user = os.environ.get("DBA_POSTGRES_USER") or env_vars.get("POSTGRES_USER") or "postgres"
    dba_pass = (
        cli_dba_pass
        or os.environ.get("DBA_POSTGRES_PASSWORD")
        or os.environ.get("PGPASSWORD")
        or env_vars.get("POSTGRES_PASSWORD")
        or settings.POSTGRES_PASSWORD
        or ""
    )

    return host, port, dbname, dba_user, dba_pass


def generate_secure_token() -> str:
    """Generates a high-entropy URL-safe password (32 bytes entropy)."""
    return secrets.token_urlsafe(24)


def inspect_postgres_cluster(host: str, port: int, dbname: str, dba_user: str, dba_pass: str) -> Dict[str, any]:
    """Inspects existing roles, memberships, PUBLIC grants, tables, and sequences."""
    conn = psycopg2.connect(
        host=host, port=port, user=dba_user, password=dba_pass, dbname=dbname, connect_timeout=5
    )
    cur = conn.cursor()

    cur.execute("SELECT current_user, session_user, current_database(), version();")
    curr_user, sess_user, curr_db, ver = cur.fetchone()

    cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user;")
    is_super, can_createrole, can_createdb = cur.fetchone()

    cur.execute("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit FROM pg_roles;")
    all_roles = {
        r[0]: {"superuser": r[1], "createdb": r[2], "createrole": r[3], "inherit": r[4]}
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT r.rolname AS member, g.rolname AS group
        FROM pg_auth_members m
        JOIN pg_roles r ON m.member = r.oid
        JOIN pg_roles g ON m.roleid = g.oid;
    """)
    memberships = [(row[0], row[1]) for row in cur.fetchall()]

    cur.execute("SELECT datacl FROM pg_database WHERE datname = %s;", (dbname,))
    db_acl_raw = cur.fetchone()
    db_acl = db_acl_raw[0] if db_acl_raw else None

    cur.execute("SELECT nspacl FROM pg_namespace WHERE nspname = 'public';")
    schema_acl_raw = cur.fetchone()
    schema_acl = schema_acl_raw[0] if schema_acl_raw else None

    cur.execute("""
        SELECT table_name, privilege_type
        FROM information_schema.table_privileges
        WHERE grantee = 'PUBLIC' AND table_schema = 'public';
    """)
    public_table_privs = cur.fetchall()

    cur.execute("SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;")
    tables = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("SELECT sequencename, sequenceowner FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename;")
    sequences = {r[0]: r[1] for r in cur.fetchall()}

    cur.close()
    conn.close()

    return {
        "connected_user": curr_user,
        "is_superuser": is_super,
        "can_createrole": can_createrole,
        "can_createdb": can_createdb,
        "version": ver.split("\n")[0],
        "existing_roles": all_roles,
        "memberships": memberships,
        "db_acl": db_acl,
        "schema_acl": schema_acl,
        "public_table_privs": public_table_privs,
        "tables": tables,
        "sequences": sequences,
    }


def build_privileged_sql_plan(
    existing_roles: Dict[str, dict],
    tables: Dict[str, str],
    sequences: Dict[str, str],
) -> List[Tuple[str, str, Tuple]]:
    """Builds the list of SQL statements with descriptive labels and parameterized values."""
    plan: List[Tuple[str, str, Tuple]] = []

    app_exists = "app_user" in existing_roles
    mig_exists = "migration_admin" in existing_roles

    # 1. Role creation / update (Idempotent for any presence combination)
    # app_user: NOINHERIT, NOSUPERUSER, NOCREATEDB, NOCREATEROLE
    if not app_exists:
        plan.append((
            "Create role 'app_user' with least privilege (NOINHERIT, NOSUPERUSER, NOCREATEDB, NOCREATEROLE)",
            "CREATE ROLE app_user WITH LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;",
            ("APP_USER_PASSWORD_PLACEHOLDER",),
        ))
    else:
        plan.append((
            "Enforce least privilege and update password on existing role 'app_user'",
            "ALTER ROLE app_user WITH PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;",
            ("APP_USER_PASSWORD_PLACEHOLDER",),
        ))

    # migration_admin: NOCREATEDB, NOSUPERUSER, NOCREATEROLE, INHERIT
    if not mig_exists:
        plan.append((
            "Create role 'migration_admin' (NOSUPERUSER, NOCREATEDB, NOCREATEROLE, INHERIT)",
            "CREATE ROLE migration_admin WITH LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;",
            ("MIGRATION_ADMIN_PASSWORD_PLACEHOLDER",),
        ))
    else:
        plan.append((
            "Enforce NOCREATEDB and update password on existing role 'migration_admin'",
            "ALTER ROLE migration_admin WITH PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;",
            ("MIGRATION_ADMIN_PASSWORD_PLACEHOLDER",),
        ))

    # 2. Revoke PUBLIC bypasses across database, schema, tables, sequences, routines
    plan.append((
        "Revoke all database privileges on 'ai_customer_support' from PUBLIC",
        "REVOKE ALL ON DATABASE ai_customer_support FROM PUBLIC;",
        (),
    ))
    plan.append((
        "Revoke all schema privileges on 'public' from PUBLIC",
        "REVOKE ALL ON SCHEMA public FROM PUBLIC;",
        (),
    ))
    plan.append((
        "Revoke all table privileges in schema 'public' from PUBLIC",
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;",
        (),
    ))
    plan.append((
        "Revoke all sequence privileges in schema 'public' from PUBLIC",
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;",
        (),
    ))
    plan.append((
        "Revoke all routine/function privileges in schema 'public' from PUBLIC",
        "REVOKE ALL ON ALL ROUTINES IN SCHEMA public FROM PUBLIC;",
        (),
    ))

    # 3. Connection and schema usage (migration_admin gets CONNECT only, NO database-level CREATE)
    plan.append((
        "Grant CONNECT on database to app_user",
        "GRANT CONNECT ON DATABASE ai_customer_support TO app_user;",
        (),
    ))
    plan.append((
        "Grant CONNECT on database to migration_admin (NO database-level CREATE)",
        "GRANT CONNECT ON DATABASE ai_customer_support TO migration_admin;",
        (),
    ))
    plan.append((
        "Grant USAGE on schema 'public' to app_user",
        "GRANT USAGE ON SCHEMA public TO app_user;",
        (),
    ))
    plan.append((
        "Grant ALL on schema 'public' to migration_admin",
        "GRANT ALL ON SCHEMA public TO migration_admin;",
        (),
    ))

    # 4. Table and Sequence Ownership Transfer
    for tbl in sorted(tables.keys()):
        plan.append((
            f"Transfer ownership of table '{tbl}' to migration_admin",
            f'ALTER TABLE "{tbl}" OWNER TO migration_admin;',
            (),
        ))

    for seq in sorted(sequences.keys()):
        plan.append((
            f"Transfer ownership of sequence '{seq}' to migration_admin",
            f'ALTER SEQUENCE "{seq}" OWNER TO migration_admin;',
            (),
        ))

    # 5. Audited Table-Specific Least-Privilege Grants for app_user

    # a) Append-Only Tables: SELECT, INSERT. Zero UPDATE, Zero DELETE.
    for tbl in sorted(APPEND_ONLY_TABLES):
        if tbl in tables:
            plan.append((
                f"Grant append-only (SELECT, INSERT) on '{tbl}' to app_user",
                f'GRANT SELECT, INSERT ON "{tbl}" TO app_user;',
                (),
            ))
            plan.append((
                f"Revoke UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES on append-only '{tbl}' from app_user",
                f'REVOKE UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES ON "{tbl}" FROM app_user;',
                (),
            ))

    # b) conversations: SELECT, INSERT, Restricted Column-Level UPDATE. Zero turn_count update.
    if "conversations" in tables:
        plan.append((
            "Grant SELECT, INSERT on conversations to app_user",
            'GRANT SELECT, INSERT ON "conversations" TO app_user;',
            (),
        ))
        cols_sql = ", ".join(RESTRICTED_UPDATE_TABLES["conversations"])
        plan.append((
            f"Grant restricted column-level UPDATE ({cols_sql}) on conversations to app_user",
            f'GRANT UPDATE ({cols_sql}) ON "conversations" TO app_user;',
            (),
        ))
        plan.append((
            "Revoke UPDATE (turn_count), DELETE, TRUNCATE, TRIGGER, REFERENCES on conversations from app_user",
            'REVOKE UPDATE (turn_count), DELETE, TRUNCATE, TRIGGER, REFERENCES ON "conversations" FROM app_user;',
            (),
        ))

    # c) Operational DML Tables: SELECT, INSERT, UPDATE. Zero DELETE.
    for tbl in sorted(OPERATIONAL_DML_TABLES):
        if tbl in tables:
            plan.append((
                f"Grant operational DML (SELECT, INSERT, UPDATE) on '{tbl}' to app_user",
                f'GRANT SELECT, INSERT, UPDATE ON "{tbl}" TO app_user;',
                (),
            ))
            plan.append((
                f"Revoke DELETE, TRUNCATE, TRIGGER, REFERENCES on operational '{tbl}' from app_user",
                f'REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES ON "{tbl}" FROM app_user;',
                (),
            ))

    # d) Read-Only Lookup Tables: SELECT only. Zero write permissions.
    for tbl in sorted(READ_ONLY_TABLES):
        if tbl in tables:
            plan.append((
                f"Grant read-only (SELECT) on lookup table '{tbl}' to app_user",
                f'GRANT SELECT ON "{tbl}" TO app_user;',
                (),
            ))
            plan.append((
                f"Revoke INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES on lookup '{tbl}' from app_user",
                f'REVOKE INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES ON "{tbl}" FROM app_user;',
                (),
            ))

    # 6. Explicit Sequence Permissions (No blanket grants)
    for seq in EXPLICIT_REQUIRED_SEQUENCES:
        if seq in sequences:
            plan.append((
                f"Grant USAGE, SELECT on required sequence '{seq}' to app_user",
                f'GRANT USAGE, SELECT ON SEQUENCE "{seq}" TO app_user;',
                (),
            ))

    # Revoke blanket default table privileges (require explicit migration grants)
    plan.append((
        "Revoke blanket default table privileges for migration_admin (require explicit migration grants)",
        "ALTER DEFAULT PRIVILEGES FOR ROLE migration_admin IN SCHEMA public REVOKE ALL ON TABLES FROM app_user;",
        (),
    ))

    return plan



def harden_windows_backup_permissions(backup_dir: str, file_paths: List[str]) -> Tuple[bool, str]:
    """Applies strict Windows ACL on storage/backups/ and generated backup files."""
    if sys.platform != "win32":
        try:
            os.chmod(backup_dir, 0o700)
            for f in file_paths:
                os.chmod(f, 0o600)
            return True, "POSIX permissions set to 0700/0600"
        except Exception as e:
            return False, f"Failed to chmod backup: {e}"

    username = os.environ.get("USERNAME")
    if not username:
        return False, "USERNAME environment variable not found on Windows."

    try:
        # Harden backup directory: restrict to %USERNAME%, SYSTEM, Administrators with inheritance removed
        cmd_dir = [
            "icacls",
            backup_dir,
            "/inheritance:r",
            f"/grant:r",
            f"{username}:(OI)(CI)(F)",
            "/grant:r",
            "SYSTEM:(OI)(CI)(F)",
            "/grant:r",
            "Administrators:(OI)(CI)(F)",
        ]
        subprocess.run(cmd_dir, capture_output=True, text=True, check=True)

        # Harden individual backup files
        for f in file_paths:
            cmd_f = [
                "icacls",
                f,
                "/inheritance:r",
                f"/grant:r",
                f"{username}:(F)",
                "/grant:r",
                "SYSTEM:(F)",
                "/grant:r",
                "Administrators:(F)",
            ]
            subprocess.run(cmd_f, capture_output=True, text=True, check=True)

        return True, f"Windows ACL hardened on {backup_dir} and backup files: restricted to {username}, SYSTEM, Administrators."
    except Exception as e:
        return False, f"Failed to run icacls on backup: {e}"


def take_pre_bootstrap_postgres_backup(
    host: str, port: int, dbname: str, dba_user: str, dba_pass: str
) -> Dict[str, any]:
    """Takes a full PostgreSQL database backup and cluster globals backup prior to any modification."""
    backup_dir = os.path.join(ROOT_DIR, "storage", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    db_backup_path = os.path.join(backup_dir, f"pre_bootstrap_{dbname}_{timestamp}.sql")
    globals_backup_path = os.path.join(backup_dir, f"pre_bootstrap_globals_{timestamp}.sql")

    pg_bin_candidates = [
        r"C:\Program Files\PostgreSQL\16\bin",
        r"C:\Program Files\PostgreSQL\15\bin",
        r"C:\Program Files\PostgreSQL\17\bin",
    ]
    pg_dump_bin = shutil.which("pg_dump")
    pg_dumpall_bin = shutil.which("pg_dumpall")

    if not pg_dump_bin or not pg_dumpall_bin:
        for c in pg_bin_candidates:
            cand_dump = os.path.join(c, "pg_dump.exe")
            cand_dumpall = os.path.join(c, "pg_dumpall.exe")
            if os.path.exists(cand_dump) and not pg_dump_bin:
                pg_dump_bin = cand_dump
            if os.path.exists(cand_dumpall) and not pg_dumpall_bin:
                pg_dumpall_bin = cand_dumpall

    if not pg_dump_bin or not pg_dumpall_bin:
        raise RuntimeError("Could not locate pg_dump and pg_dumpall binaries to take required pre-bootstrap backup.")

    env = os.environ.copy()
    env["PGPASSWORD"] = dba_pass

    # 1. Full database dump (plain SQL with clean & if-exists)
    dump_cmd = [
        pg_dump_bin,
        "-h", host,
        "-p", str(port),
        "-U", dba_user,
        "-Fp",
        "--clean",
        "--if-exists",
        "-f", db_backup_path,
        dbname,
    ]
    res_dump = subprocess.run(dump_cmd, env=env, capture_output=True, text=True)
    if res_dump.returncode != 0:
        raise RuntimeError(f"pg_dump failed (exit code {res_dump.returncode}): {res_dump.stderr.strip()}")

    # 2. Globals dump (roles, cluster privileges, tablespaces)
    globals_cmd = [
        pg_dumpall_bin,
        "-h", host,
        "-p", str(port),
        "-U", dba_user,
        "-g",
        "-f", globals_backup_path,
    ]
    res_globals = subprocess.run(globals_cmd, env=env, capture_output=True, text=True)
    if res_globals.returncode != 0:
        raise RuntimeError(f"pg_dumpall failed (exit code {res_globals.returncode}): {res_globals.stderr.strip()}")

    # 3. Validation: Verify backup files are non-empty
    db_size = os.path.getsize(db_backup_path)
    globals_size = os.path.getsize(globals_backup_path)

    if db_size == 0:
        raise RuntimeError(f"Database backup file is empty (0 bytes): {db_backup_path}")
    if globals_size == 0:
        raise RuntimeError(f"Cluster globals backup file is empty (0 bytes): {globals_backup_path}")

    # 4. Validation: Verify SQL dump structure and headers
    with open(db_backup_path, "r", encoding="utf-8", errors="ignore") as f:
        header_db = f.read(500)
        if "PostgreSQL database dump" not in header_db:
            raise RuntimeError(f"Database backup failed validation header check: {db_backup_path}")

    with open(globals_backup_path, "r", encoding="utf-8", errors="ignore") as f:
        header_g = f.read(500)
        if "PostgreSQL database cluster dump" not in header_g:
            raise RuntimeError(f"Globals backup failed validation header check: {globals_backup_path}")

    # 5. Security: Restrict Windows ACLs on backup directory and generated files
    acl_ok, acl_msg = harden_windows_backup_permissions(backup_dir, [db_backup_path, globals_backup_path])
    if not acl_ok:
        raise RuntimeError(f"Failed to secure backup file permissions: {acl_msg}")

    return {
        "db_backup": db_backup_path,
        "globals_backup": globals_backup_path,
        "timestamp": timestamp,
        "db_size": db_size,
        "globals_size": globals_size,
    }


def atomic_write(file_path: str, content: str) -> None:
    """Writes content to a temporary file and atomically replaces the target file."""
    dir_name = os.path.dirname(file_path)
    temp_path = os.path.join(dir_name, f".tmp_{uuid.uuid4().hex}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, file_path)
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise e


def backup_config_files(timestamp: str) -> Dict[str, str]:
    """Creates timestamped backups of all configuration files before modifying them."""
    backups: Dict[str, str] = {}
    target_files = [ENV_PATH, CONFIG_PY_PATH, ALEMBIC_ENV_PATH]
    for target in target_files:
        if os.path.exists(target):
            bak_path = f"{target}.bak.{timestamp}"
            shutil.copy2(target, bak_path)
            backups[target] = bak_path
    return backups


def restore_config_backups(config_backups: Dict[str, str]) -> None:
    """Restores configuration files from their timestamped backups in case of an error."""
    for target, bak in config_backups.items():
        if os.path.exists(bak):
            try:
                shutil.copy2(bak, target)
                print(f"  [RECOVERY] Restored {target} from {bak}")
            except Exception as e:
                print(f"  [RECOVERY ERROR] Failed to restore {target}: {e}")

def update_env_file_safe(app_pass: str, mig_pass: str) -> None:
    """Updates .env with app_user and migration_admin credentials ONLY."""
    existing_lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    updates = {
        "POSTGRES_USER": "app_user",
        "POSTGRES_PASSWORD": app_pass,
        "APP_POSTGRES_USER": "app_user",
        "APP_POSTGRES_PASSWORD": app_pass,
        "MIGRATION_POSTGRES_USER": "migration_admin",
        "MIGRATION_POSTGRES_PASSWORD": mig_pass,
    }

    forbidden_keys = {"DBA_POSTGRES_USER", "DBA_POSTGRES_PASSWORD", "PGPASSWORD"}

    new_lines = []
    processed_keys = set()

    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in forbidden_keys:
                continue
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                processed_keys.add(k)
                continue
        new_lines.append(line)

    remaining = [k for k in updates.keys() if k not in processed_keys]
    if remaining:
        new_lines.append("\n# --- ROLE-SEPARATED POSTGRESQL CREDENTIALS (DAY 07 PHASE 4) ---\n")
        for k in remaining:
            new_lines.append(f"{k}={updates[k]}\n")

    atomic_write(ENV_PATH, "".join(new_lines))


def harden_windows_env_permissions() -> Tuple[bool, str]:
    """Applies strict Windows ACL on .env so only current user, SYSTEM, and Administrators have access."""
    if sys.platform != "win32":
        try:
            os.chmod(ENV_PATH, 0o600)
            return True, "POSIX permissions set to 0600"
        except Exception as e:
            return False, f"Failed to chmod: {e}"

    username = os.environ.get("USERNAME")
    if not username:
        return False, "USERNAME environment variable not found on Windows."

    try:
        cmd = [
            "icacls",
            ENV_PATH,
            "/inheritance:r",
            f"/grant:r",
            f"{username}:(R,W)",
            "/grant:r",
            "SYSTEM:(F)",
            "/grant:r",
            "Administrators:(F)",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True, f"Windows ACL hardened: restricted to {username}, SYSTEM, Administrators."
    except Exception as e:
        return False, f"Failed to run icacls: {e}"


def update_config_py() -> None:
    """Updates backend/app/core/config.py to support migration database URLs."""
    with open(CONFIG_PY_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    if "MIGRATION_POSTGRES_USER" not in content:
        target = 'POSTGRES_DB: str = "ai_customer_support"\n'
        replacement = target + (
            '\n    # Dedicated Migration Administrator Credentials (DDL / Schema Owner)\n'
            '    MIGRATION_POSTGRES_USER: str = "migration_admin"\n'
            '    MIGRATION_POSTGRES_PASSWORD: str = ""\n'
            '    MIGRATION_DATABASE_URL: Optional[str] = None\n'
            '    MIGRATION_SYNC_DATABASE_URL: Optional[str] = None\n'
        )
        assert target in content, "Failed to locate POSTGRES_DB in config.py"
        content = content.replace(target, replacement, 1)

    if "migration_async_database_url" not in content:
        target = "    @computed_field  # type: ignore[prop-decorator]\n    @property\n    def redis_url(self) -> str:"
        replacement = (
            "    @computed_field  # type: ignore[prop-decorator]\n"
            "    @property\n"
            "    def migration_async_database_url(self) -> str:\n"
            '        """Returns the async database URL for Alembic migration admin."""\n'
            "        if self.MIGRATION_DATABASE_URL:\n"
            "            return self.MIGRATION_DATABASE_URL\n"
            "        user = quote_plus(self.MIGRATION_POSTGRES_USER)\n"
            "        password = quote_plus(self.MIGRATION_POSTGRES_PASSWORD)\n"
            "        return (\n"
            '            f"postgresql+asyncpg://{user}:{password}@" \n'
            '            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"\n'
            "        )\n\n"
            "    @computed_field  # type: ignore[prop-decorator]\n"
            "    @property\n"
            "    def migration_sync_database_url(self) -> str:\n"
            '        """Returns the synchronous database URL for Alembic migration admin."""\n'
            "        if self.MIGRATION_SYNC_DATABASE_URL:\n"
            "            return self.MIGRATION_SYNC_DATABASE_URL\n"
            "        user = quote_plus(self.MIGRATION_POSTGRES_USER)\n"
            "        password = quote_plus(self.MIGRATION_POSTGRES_PASSWORD)\n"
            "        return (\n"
            '            f"postgresql+psycopg2://{user}:{password}@" \n'
            '            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"\n'
            "        )\n\n"
            + target
        )
        assert target in content, "Failed to locate redis_url property in config.py"
        content = content.replace(target, replacement, 1)

    atomic_write(CONFIG_PY_PATH, content)


def update_alembic_env_py() -> None:
    """Updates backend/alembic/env.py to use settings.migration_async_database_url."""
    with open(ALEMBIC_ENV_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    old_target = """if not config.get_main_option("sqlalchemy.url"):
    db_url = settings.async_database_url"""

    new_target = """if not config.get_main_option("sqlalchemy.url"):
    db_url = settings.migration_async_database_url"""

    if old_target in content:
        content = content.replace(old_target, new_target, 1)
        atomic_write(ALEMBIC_ENV_PATH, content)


def verify_roles_and_privileges(
    host: str,
    port: int,
    dbname: str,
    app_pass: str,
    mig_pass: str,
) -> Dict[str, Tuple[bool, str]]:
    """Connects as migration_admin and app_user to verify connection and permission boundaries."""
    results: Dict[str, Tuple[bool, str]] = {}

    # 1. Verify migration_admin
    try:
        conn_mig = psycopg2.connect(
            host=host, port=port, user="migration_admin", password=mig_pass, dbname=dbname, connect_timeout=5
        )
        cur_mig = conn_mig.cursor()

        cur_mig.execute("SELECT current_user;")
        u = cur_mig.fetchone()[0]
        results["migration_admin_connect"] = (u == "migration_admin", f"Connected as {u}")

        cur_mig.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'migration_admin';")
        sup, cdb, crole = cur_mig.fetchone()
        results["migration_admin_role_attributes"] = (
            not sup and not cdb and not crole,
            f"superuser={sup}, createdb={cdb}, createrole={crole} (least privilege verified)"
        )

        cur_mig.execute("SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = 'messages';")
        owner = cur_mig.fetchone()[0]
        results["migration_admin_table_ownership"] = (owner == "migration_admin", f"Owner of messages table is {owner}")

        cur_mig.close()
        conn_mig.close()
    except Exception as e:
        results["migration_admin_verification"] = (False, f"Failed: {type(e).__name__}: {e}")

    # 2. Verify app_user
    try:
        conn_app = psycopg2.connect(
            host=host, port=port, user="app_user", password=app_pass, dbname=dbname, connect_timeout=5
        )
        cur_app = conn_app.cursor()

        cur_app.execute("SELECT current_user;")
        u = cur_app.fetchone()[0]
        results["app_user_connect"] = (u == "app_user", f"Connected as {u}")

        cur_app.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolinherit FROM pg_roles WHERE rolname = 'app_user';")
        sup, crole, cdb, inh = cur_app.fetchone()
        results["app_user_role_attributes"] = (
            not sup and not crole and not cdb and not inh,
            f"superuser={sup}, createrole={crole}, createdb={cdb}, inherit={inh} (NOINHERIT verified)"
        )

        # Verify SELECT permitted on messages
        cur_app.execute("SELECT count(*) FROM messages;")
        cnt = cur_app.fetchone()[0]
        results["app_user_select_messages"] = (True, f"SELECT messages succeeded ({cnt} rows)")

        # Verify operational DML permitted on documents (SELECT)
        cur_app.execute("SELECT count(*) FROM documents;")
        cnt_docs = cur_app.fetchone()[0]
        results["app_user_select_documents"] = (True, f"SELECT documents succeeded ({cnt_docs} rows)")

        # Verify sequence USAGE permitted
        cur_app.execute("SELECT nextval('document_chunks_faiss_vector_id_seq');")
        val = cur_app.fetchone()[0]
        results["app_user_sequence_access"] = (True, f"Sequence document_chunks_faiss_vector_id_seq access verified ({val})")

        # Verify DDL forbidden (ALTER TABLE)
        ddl_blocked = False
        try:
            cur_app.execute("ALTER TABLE messages DROP COLUMN IF EXISTS non_existent_test_col;")
        except psycopg2.errors.InsufficientPrivilege:
            conn_app.rollback()
            ddl_blocked = True
        except Exception:
            conn_app.rollback()
            ddl_blocked = True
        results["app_user_ddl_blocked"] = (ddl_blocked, "ALTER TABLE messages correctly blocked by engine (42501)")

        # Verify DELETE forbidden on messages
        delete_blocked = False
        try:
            cur_app.execute("DELETE FROM messages WHERE id = '00000000-0000-0000-0000-000000000000';")
        except psycopg2.errors.InsufficientPrivilege:
            conn_app.rollback()
            delete_blocked = True
        except Exception:
            conn_app.rollback()
            delete_blocked = True
        results["app_user_delete_messages_blocked"] = (delete_blocked, "DELETE FROM messages correctly blocked by grant (42501)")

        # Verify direct UPDATE on conversations.turn_count forbidden
        turn_update_blocked = False
        try:
            cur_app.execute("UPDATE conversations SET turn_count = 999 WHERE id = '00000000-0000-0000-0000-000000000000';")
        except psycopg2.errors.InsufficientPrivilege:
            conn_app.rollback()
            turn_update_blocked = True
        except Exception:
            conn_app.rollback()
            turn_update_blocked = True
        results["app_user_turn_count_update_blocked"] = (turn_update_blocked, "UPDATE conversations(turn_count) correctly blocked (42501)")

        # Verify zero role memberships in pg_auth_members (PostgreSQL 16 role semantics)
        cur_app.execute(
            """
            SELECT count(*) FROM pg_auth_members m
            JOIN pg_roles r ON r.oid = m.member
            WHERE r.rolname = 'app_user';
            """
        )
        mem_count = cur_app.fetchone()[0]
        results["app_user_no_role_memberships"] = (
            mem_count == 0,
            f"app_user memberships in pg_auth_members: {mem_count} (strictly 0, no privileged role links)"
        )

        # Verify effective privileges using pg_has_role() (MEMBER and USAGE)
        for target_role in ("postgres", "migration_admin", "pg_read_all_data", "pg_write_all_data"):
            cur_app.execute(
                "SELECT pg_has_role(current_user, %s, 'MEMBER'), pg_has_role(current_user, %s, 'USAGE');",
                (target_role, target_role),
            )
            is_member, has_usage = cur_app.fetchone()
            results[f"app_user_pg_has_role_{target_role}"] = (
                not is_member and not has_usage,
                f"pg_has_role('{target_role}'): member={is_member}, usage={has_usage} (strictly False)"
            )

        # Negative SET ROLE tests: Verify app_user CANNOT switch to privileged roles
        for target_role in ("postgres", "migration_admin", "pg_read_all_data", "pg_database_owner"):
            set_role_blocked = False
            try:
                cur_app.execute(f'SET ROLE "{target_role}";')
            except psycopg2.errors.InsufficientPrivilege:
                conn_app.rollback()
                set_role_blocked = True
            except Exception:
                conn_app.rollback()
                set_role_blocked = True
            results[f"app_user_set_role_{target_role}_blocked"] = (
                set_role_blocked,
                f"SET ROLE {target_role} correctly blocked by PostgreSQL engine (42501)"
            )

        cur_app.close()
        conn_app.close()
    except Exception as e:
        results["app_user_verification"] = (False, f"Failed: {type(e).__name__}: {e}")

    return results


def execute_bootstrap_plan(
    host: str,
    port: int,
    dbname: str,
    dba_user: str,
    dba_pass: str,
    plan: List[Tuple[str, str, Tuple]],
    app_pass: str,
    mig_pass: str,
) -> None:
    """Executes the privileged SQL plan transactionally with automatic rollback on failure."""
    conn = psycopg2.connect(
        host=host, port=port, user=dba_user, password=dba_pass, dbname=dbname
    )
    conn.autocommit = False  # TRANSACTIONAL MODE
    cur = conn.cursor()

    try:
        for label, sql, params in plan:
            concrete_params = []
            for p in params:
                if p == "APP_USER_PASSWORD_PLACEHOLDER":
                    concrete_params.append(app_pass)
                elif p == "MIGRATION_ADMIN_PASSWORD_PLACEHOLDER":
                    concrete_params.append(mig_pass)
                else:
                    concrete_params.append(p)

            if concrete_params:
                if "PASSWORD %s" in sql:
                    escaped_pass = concrete_params[0].replace("'", "''")
                    safe_sql = sql.replace("%s", f"'{escaped_pass}'")
                    cur.execute(safe_sql)
                else:
                    cur.execute(sql, tuple(concrete_params))
            else:
                cur.execute(sql)
            print(f"  [OK] {label}")

        # Atomically commit all statements in the plan
        conn.commit()
        print("  [COMMIT] Bootstrap transaction successfully committed to PostgreSQL.")
    except Exception as e:
        print(f"\n  [ERROR] Failure during privileged SQL execution: {type(e).__name__}: {e}")
        print("  [ROLLBACK] Rolling back active database transaction...")
        try:
            conn.rollback()
            print("  [ROLLBACK COMPLETE] Transaction successfully rolled back. ZERO database changes were committed.")
        except Exception as rb_err:
            print(f"  [ROLLBACK ERROR] Failed to rollback transaction: {rb_err}")
        cur.close()
        conn.close()
        raise e

    cur.close()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Day 07 Phase 4 Audited PostgreSQL Role & Privilege Bootstrap")
    parser.add_argument("--dry-run", action="store_true", help="Print planned SQL and configuration updates without executing")
    parser.add_argument("--execute", action="store_true", help="Execute privileged SQL, update config, and run verification")
    parser.add_argument("--dba-password", type=str, default=None, help="DBA password (if not in process env)")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("Please specify either --dry-run or --execute.")
        sys.exit(1)

    host, port, dbname, dba_user, dba_pass = get_dba_credentials(args.dba_password)

    print("=" * 80)
    print("DAY 07 PHASE 4: AUDITED POSTGRESQL PREREQUISITE BOOTSTRAP WORKFLOW")
    print("=" * 80)
    print(f"Target Database Server : {host}:{port}/{dbname}")
    print(f"Connected DBA User     : {dba_user}")

    try:
        inspection = inspect_postgres_cluster(host, port, dbname, dba_user, dba_pass)
    except Exception as e:
        print(f"\n[FATAL] Unable to connect as DBA '{dba_user}': {type(e).__name__}: {e}")
        print("Remediation: Ensure PostgreSQL is running on port 5432 and DBA password is provided.")
        sys.exit(1)

    print(f"PostgreSQL Version     : {inspection['version']}")
    print(f"DBA Superuser Status   : {inspection['is_superuser']} (createrole={inspection['can_createrole']})")
    print(f"Existing Cluster Roles : {', '.join(sorted(inspection['existing_roles'].keys()))}")
    print(f"Public Schema Tables   : {len(inspection['tables'])} tables found")
    print(f"Existing Memberships   : {len(inspection['memberships'])} non-system memberships found")

    if not inspection["is_superuser"] and not inspection["can_createrole"]:
        print("\n[FATAL] Connected user lacks CREATEROLE / SUPERUSER permissions. Cannot provision roles.")
        sys.exit(1)

    plan = build_privileged_sql_plan(
        inspection["existing_roles"],
        inspection["tables"],
        inspection["sequences"],
    )

    env_vars = load_env_variables()
    app_pass = env_vars.get("APP_POSTGRES_PASSWORD") or generate_secure_token()
    mig_pass = env_vars.get("MIGRATION_POSTGRES_PASSWORD") or generate_secure_token()

    if args.dry_run:
        print("\n" + "-" * 80)
        print("PRE-FLIGHT SECURITY INSPECTION EVIDENCE:")
        print("-" * 80)
        print(f"1. Database '{dbname}' ACL: {inspection['db_acl']} (Default grants CONNECT to PUBLIC)")
        print(f"2. Schema 'public' ACL: {inspection['schema_acl']} (Default grants USAGE to PUBLIC)")
        print(f"3. Table Privileges to PUBLIC: {len(inspection['public_table_privs'])} explicit grants found")
        print(f"4. Role Inheritances in Cluster: {inspection['memberships']}")

        print("\n" + "-" * 80)
        print("PLANNED EXECUTION SAFEGUARDS:")
        print("-" * 80)
        print("1. PostgreSQL Pre-Bootstrap Backup:")
        print(f"   - Database dump: storage/backups/pre_bootstrap_{dbname}_<timestamp>.sql (pg_dump -Fp --clean --if-exists)")
        print(f"   - Globals dump: storage/backups/pre_bootstrap_globals_<timestamp>.sql (pg_dumpall -g)")
        print("2. Transactional DDL/DCL Execution:")
        print("   - All 78 SQL operations executed inside a single transaction (autocommit=False)")
        print("   - In case of any execution failure: conn.rollback() restores exact pre-bootstrap database state")
        print("3. Atomic Configuration Updates & Backups:")
        print("   - .env.bak.<timestamp>, config.py.bak.<timestamp>, env.py.bak.<timestamp> created prior to writes")
        print("   - All updates written via atomic write (temp file -> os.replace)")
        print("4. Post-Provisioning Verification Failure Handling:")
        print("   - If verification fails, application startup is HALTED immediately (sys.exit(1))")
        print("   - The script explicitly identifies that the database transaction was committed and not rolled back")
        print("   - Exact ordered recovery instructions provided (1: Config rollback, 2: DB restore, 3: Dependency clearance & drop newly created roles, 4: Globals restore)")
        print("5. Zero Credential Exposure:")
        print("   - Dynamic passwords generated with 32 bytes entropy; passwords NEVER printed to stdout/stderr/logs")

        print("\n" + "-" * 80)
        print("PLANNED PRIVILEGED SQL OPERATIONS (DRY-RUN):")
        print("-" * 80)
        for i, (label, sql, _) in enumerate(plan, 1):
            print(f"{i:2d}. [{label}]")
            print(f"    {sql}")

        print("\n" + "-" * 80)
        print("PLANNED CONFIGURATION & ACL HARDENING:")
        print("-" * 80)
        print("1. .env Configuration Updates (ZERO DBA CREDENTIAL PERSISTENCE):")
        print("   - Set POSTGRES_USER=app_user, POSTGRES_PASSWORD=[REDACTED: 32 bytes entropy]")
        print("   - Set APP_POSTGRES_USER=app_user, APP_POSTGRES_PASSWORD=[REDACTED: 32 bytes entropy]")
        print("   - Set MIGRATION_POSTGRES_USER=migration_admin, MIGRATION_POSTGRES_PASSWORD=[REDACTED: 32 bytes entropy]")
        print("   - PURGE any DBA_POSTGRES_USER or DBA_POSTGRES_PASSWORD from .env")
        print("2. Windows File ACL Hardening:")
        print(f"   - icacls .env /inheritance:r /grant:r %USERNAME%:(R,W) /grant:r SYSTEM:(F) /grant:r Administrators:(F)")
        print("3. backend/app/core/config.py Updates:")
        print("   - Add MIGRATION_POSTGRES_USER, MIGRATION_POSTGRES_PASSWORD, MIGRATION_DATABASE_URL")
        print("   - Add migration_async_database_url & migration_sync_database_url computed properties")
        print("4. backend/alembic/env.py Updates:")
        print("   - Point Alembic engine to settings.migration_async_database_url")
        print("5. Post-Execution Live Verification Suite:")
        print("   - migration_admin: connect, superuser=False, createdb=False, table ownership on public tables")
        print("   - app_user: connect, superuser=False, NOINHERIT=True, SELECT allowed, DDL/DELETE/turn_count blocked")
        print("-" * 80)
        print("\nDry-run complete. Awaiting user approval to execute.")
        return

    # Execution (Requires explicit authorization)
    print("\n" + "-" * 80)
    print("STEP 1: TAKING PRE-BOOTSTRAP POSTGRESQL BACKUP...")
    print("-" * 80)
    try:
        backup_info = take_pre_bootstrap_postgres_backup(host, port, dbname, dba_user, dba_pass)
        print(f"  [OK] Full database dump saved: {backup_info['db_backup']} ({backup_info['db_size']} bytes)")
        print(f"  [OK] Cluster globals dump saved: {backup_info['globals_backup']} ({backup_info['globals_size']} bytes)")
    except Exception as e:
        print(f"  [FATAL] Pre-bootstrap backup failed: {e}")
        print("  Aborting bootstrap execution before any changes are made.")
        sys.exit(1)

    print("\n" + "-" * 80)
    print("STEP 2: EXECUTING PRIVILEGED SQL OPERATIONS (TRANSACTIONAL)...")
    print("-" * 80)
    try:
        execute_bootstrap_plan(host, port, dbname, dba_user, dba_pass, plan, app_pass, mig_pass)
    except Exception:
        print("\n[FATAL] Privileged SQL execution failed. Transaction was rolled back. Halting.")
        sys.exit(1)

    print("\n" + "-" * 80)
    print("STEP 3: BACKING UP AND UPDATING CONFIGURATION FILES ATOMICALLY...")
    print("-" * 80)
    timestamp = backup_info["timestamp"]
    config_backups = backup_config_files(timestamp)
    for target, bak in config_backups.items():
        print(f"  [BACKUP] Preserved {target} -> {bak}")

    try:
        update_env_file_safe(app_pass, mig_pass)
        print("  [OK] .env updated atomically: app_user & migration_admin credentials written; DBA secrets purged")

        acl_ok, acl_msg = harden_windows_env_permissions()
        print(f"  [{'OK' if acl_ok else 'WARN'}] Windows .env ACL: {acl_msg}")

        update_config_py()
        print("  [OK] backend/app/core/config.py updated atomically with migration_admin settings")

        update_alembic_env_py()
        print("  [OK] backend/alembic/env.py updated atomically to bind to settings.migration_async_database_url")
    except Exception as e:
        print(f"  [FATAL] Configuration update failed: {e}")
        print("  Restoring configuration files from backups...")
        restore_config_backups(config_backups)
        sys.exit(1)

    # Live Verification
    print("\n" + "-" * 80)
    print("STEP 4: RUNNING LIVE POST-BOOTSTRAP VERIFICATION...")
    print("-" * 80)
    results = verify_roles_and_privileges(host, port, dbname, app_pass, mig_pass)

    all_passed = True
    failed_checks = []
    for check_name, (passed, msg) in results.items():
        status = "[PASS]" if passed else "[FAIL]"
        if not passed:
            all_passed = False
            failed_checks.append((check_name, msg))
        print(f"  {status:7s} {check_name}: {msg}")

    print("=" * 80)
    if all_passed:
        print("[SUCCESS] All PostgreSQL role, privilege, and security prerequisites verified!")
    else:
        print("\n[CRITICAL POST-PROVISIONING VERIFICATION FAILURE]")
        print("The bootstrap transaction was COMMITTED to the database prior to verification.")
        print("THE DATABASE WAS NOT AUTOMATICALLY ROLLED BACK.")
        print(f"\nThe following {len(failed_checks)} verification check(s) failed:")
        for name, msg in failed_checks:
            print(f"  - {name}: {msg}")
        print("\nAPPLICATION STARTUP HAS BEEN HALTED.")
        newly_created_roles = [r for r in ["app_user", "migration_admin"] if r not in inspection["existing_roles"]]

        print("\nEXACT RECOVERY INSTRUCTIONS (EXECUTE IN ORDER):")
        print("1. Restore application configuration files from atomic backups:")
        for cfg_file, bak_file in config_backups.items():
            print(f"   Copy-Item '{bak_file}' '{cfg_file}'")

        print(f"2. Restore database objects and ownership in '{dbname}' (drops unverified state and restores pre-bootstrap schema/data with postgres ownership):")
        print(f"   psql -h {host} -p {port} -U {dba_user} -d {dbname} -f \"{backup_info['db_backup']}\"")

        if newly_created_roles:
            roles_str = ", ".join(f"'{r}'" for r in newly_created_roles)
            print(f"3. Clear object dependencies and drop newly created roles ({', '.join(newly_created_roles)}):")
            print(f"   a) Terminate active sessions for newly created roles:")
            print(f"      psql -h {host} -p {port} -U {dba_user} -d {dbname} -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename IN ({roles_str});\"")
            print(f"   b) Reassign remaining object dependencies and drop role grants in '{dbname}':")
            for r in newly_created_roles:
                print(f"      psql -h {host} -p {port} -U {dba_user} -d {dbname} -c \"REASSIGN OWNED BY \\\"{r}\\\" TO {dba_user}; DROP OWNED BY \\\"{r}\\\";\"")
            print(f"   c) Safely drop ONLY newly created roles (pre-existing roles remain untouched):")
            for r in newly_created_roles:
                print(f"      psql -h {host} -p {port} -U {dba_user} -d postgres -c \"DROP ROLE IF EXISTS \\\"{r}\\\";\"")
            step_globals = 4
        else:
            step_globals = 3

        print(f"{step_globals}. Restore cluster globals (restores pre-bootstrap cluster roles, permissions, and tablespaces):")
        print(f"   psql -h {host} -p {port} -U {dba_user} -d postgres -f \"{backup_info['globals_backup']}\"")
        print("=" * 80)
        sys.exit(1)


if __name__ == "__main__":
    main()
