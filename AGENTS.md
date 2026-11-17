# Ponytail — Lazy Senior Dev Mode & Platform Engineering Guidelines

**Project:** AI Customer Support Platform / Command Center  
**Mode:** Ponytail `full` (Active)  
**Philosophy:** *The best code is the code never written. Solve the whole problem with the least new code without sacrificing security, validation, reliability, test coverage, or maintainability.*

---

## 1. Before You Write

Read the task and the code it touches. List every place your change must reach: callers, tests, fixtures, config, exports. Check what your change could break for users: data it would destroy or expose, callers that stop working. That is scope. Extra features are not.

---

## 2. The Smallest Complete Change

Take the first option that fully works:

1. **Does it need to exist?** Skip features, options, and flexibility nobody asked for, and name them in one line. A vague request gets the smallest version that does the core job.
2. **Already in this codebase?** Use existing helpers, components, services, and patterns the way the surrounding code does.
3. **Standard library or native platform feature?** Use standard library (`json`, `re`, `pathlib`, `hashlib`, `asyncio`), unless the project has its own established house pattern.
4. **An installed dependency?** Use already installed libraries (SQLAlchemy 2.0, FastAPI, Pydantic, Alembic). Never add a new dependency for a few lines of logic.
5. **Can it be one line a reader gets at a glance?** One line.
6. **Otherwise:** The minimum code that works.

### Key Coding Invariants:
- **Be lazy about the solution, never about the change itself:** Finish every part the task needs, including the callers, tests, and fixtures your change touches.
- **No speculative abstractions:** No wrappers, unneeded type conversions, redundant config flags, boilerplate, or "for later" code nobody asked for. Keep values in the form the platform already gives you. Deletion beats addition.
- **Diff efficiency:** The shortest working diff wins, once you know everything it must touch. A one-liner that needs decoding is not short.
- **Comment discipline:** Comment only the *why* the code cannot show, in one line.
- **Bug fixes:** Grep every caller of the function you touch, then fix the root cause once in the shared code.
- **Refactoring:** Code you move or merge keeps its error handling and validation.
- **Edge cases:** Between options of equal size, take the one that is correct on edge cases.
- **Tests for non-trivial logic:** Lazy code without its check is unfinished. Any new non-trivial logic (a branch, a loop, a parser, financial math, or security check) leaves one small test behind.
- **Debt tracking:** A deliberate shortcut with a known limit gets a code comment in this exact format:
  `shortcut: <the limit>, <when to upgrade>`
  *(Tracked via `/ponytail-debt`)*.

---

## 3. Platform Non-Negotiable Invariants & Security Boundaries

**NEVER CUT:** Validation at trust boundaries, error handling that prevents data loss, security, accessibility, or anything the user explicitly requested.

### Security & Multi-Tenancy:
- **Tenant Isolation:** Every database query, model, and cache operation must enforce tenant boundaries (`tenant_id`). Cross-tenant access is strictly forbidden.
- **Application-Layer Encryption:** Sensitive payloads must use AES-256-GCM encryption with cryptographic nonces and associated data bound to record IDs.
- **PII & PCI-DSS Protection:** Automatic zero-logging masking of credit cards (Luhn mod 10), SSNs, phone numbers, and emails via `PIIMaskingEngine`. Never store raw PANs.
- **Adversarial Prompt Defense:** All customer claims and uploaded evidence text must pass `AdversarialEvidenceGuard` (Unicode normalization, zero-width stripping, Cyrillic/Greek homoglyphs, multi-language injection checks, and delimiter token stripping).
- **Financial Verification Hierarchy:** Document-extracted data is classified as `DOCUMENT_CORROBORATED` only. Refunds must NEVER be auto-authorized on customer-uploaded evidence alone; authoritative payment gateway verification is mandatory.
- **Storage Retention & Purge Safety:** File purge routines must enforce 30-day retention limits, PostgreSQL `SKIP LOCKED` concurrency locks, strict directory containment checks (`.is_relative_to()`), and idempotent unlinking.

---

## 4. Testing & Verification Standards

- **100% Pass Rate Baseline:** Full platform test suite (728/728 passing tests) must remain 100% green with zero regressions.
- **Targeted Tests:** Run module tests before running full regression suites.
- **Alembic Reversibility:** All schema migrations must support clean `upgrade head` and `downgrade -1` round-trips on live PostgreSQL.

---

## 5. Git & Workspace Preservation Rules

- **Do NOT Stage, Commit, Push, Reset, Stash, or Discard:** Never execute `git add`, `git commit`, `git push`, `git reset`, `git checkout .`, `git clean`, or `git stash` without explicit, unambiguous user command.
- **Preserve Frontend & Gitignore:** Preserve all pending frontend modifications, untracked components, and `.gitignore` updates untouched.
- **No Day-Number Filenames:** Technical documentation and implementation reports must use descriptive domain filenames, never raw day-number prefixes.

---

## 6. Reply Discipline

Every substantive reply must end with one or two concise lines:
- What was skipped or not checked
- Any risk or limitation the user must know

