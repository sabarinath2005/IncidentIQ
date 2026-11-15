# Architecture and data integrity

The application is a modular monolith. FastAPI serves product APIs; Celery workers run long investigations. PostgreSQL is the domain source of truth, Redis transports Celery messages, LangGraph checkpoints preserve execution state, and uploaded artifacts live outside relational rows.

## Evidence distinctions

- EvidenceItem is observed incident material, with source, time, hash, scope and optional raw-artifact key.
- InvestigationRunEvidence associates only actual available evidence with one run. FindingEvidenceLink references this association, so a finding cannot cite evidence outside its run.
- InvestigationRunSourceStatus records completed, unavailable, failed or skipped source queries. A completed query may return zero results. Gaps are never fabricated into evidence.
- InvestigationRunContextReference stores historical retrieval provenance. Historical sources are constrained to the current project, retain original incident/document identity, and are linked to findings only through FindingContextLink.

## Persistence and retries

InvestigationRun uses a stable idempotency key and LangGraph thread ID. A partial unique index allows only one queued, running or waiting-for-review run per incident. Human review completes the run only after every finding has a decision; resolving an incident cancels only that incident's active runs. Completed, failed and cancelled runs release the active-run slot. Idempotency keys are project-unique, but reuse against a different incident is a conflict.

The API inserts a new run and its OutboxEvent in one transaction. The outbox relay publishes a small event ID to Celery. Workers claim runs under a PostgreSQL row lock and hold an expiring lease. Duplicate deliveries cannot claim an active lease; expired leases are recovered by Celery Beat and queued through the outbox. Retryable provider/network and transient database errors use bounded exponential backoff and retain run-attempt error details. Permanent errors fail the run. Logical LangGraph nodes emit start/completion callbacks, and each attempt is stored in InvestigationStepAttempt. Composite foreign keys tie attempts and latest-attempt pointers to their step. The PostgreSQL checkpointer resumes the same stable LangGraph thread after recovery.

Finding and recommendation writes are committed with the transition to waiting-for-review. Findings are unique by run and rank; recommendations have run-scoped idempotency keys. Terminal run status, lease release and execution-attempt completion are committed together.

Graph checkpoints are maintained by LangGraph's PostgreSQL checkpointer, separate from product-domain tables. Redis is not used as a durable run store. External actions use action hashes and idempotency keys; the MVP executor only simulates.

## Authorization

Workspace/project scope is repeated on child entities to support composite foreign keys. Teams belong to workspaces; team membership does not grant project access. The default policy requires explicit ProjectMember membership. API authorization still checks the caller; valid foreign keys do not confer permission.

Historical retrieval filters by the authorized current project before search and is restricted by a database scope check to that same project.

## Database migrations

Alembic is the schema history. Revision `0002_investigation_reliability` adds run attempts and leases, step/attempt integrity, finding-review status constraints and run-scoped recommendations. It uses PostgreSQL `IF NOT EXISTS` guards because the original `0001` baseline creates tables from current model metadata. Test upgrades on an empty database and from a database already stamped at `0001` using a disposable PostgreSQL database.

API startup, worker startup, and dispatcher startup do not apply migrations. Migrations are explicit deployment operations. Before applying one, create and verify a restorable backup of the target database and obtain explicit approval from the responsible maintainer. From the repository root, the approved command is `docker compose run --rm api alembic upgrade head`. Never add it to ordinary service startup or deployment commands.

Phase 2 adds Alertmanager alert deduplication and provider provenance in `0005_observability_alerts`; review the detailed observability profile and its explicit migration prerequisite in [phase2-observability.md](phase2-observability.md). Provider evidence is collected through bounded Celery tasks and remains scoped to the incident project.

## Research boundary

The mock provider is deterministic and suitable for product-flow testing, not for measuring model quality. The feature-hash vector is a small reproducible retrieval mechanism and is not represented as pretrained semantic embedding. Evaluate against keyword/no-retrieval baselines and held-out labeled scenarios before making claims.
