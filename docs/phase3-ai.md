# Phase 3: AI and engineering integrations

Phase 3 adds an explicit real AI provider, semantic retrieval with source provenance, managed project knowledge, GitHub deployment/change evidence, and approval-gated Jira task creation. The existing FastAPI, PostgreSQL/pgvector, Celery and React architecture remains in place.

## AI provider configuration

Set these values in the untracked root `.env`; do not expose them through `VITE_*` variables:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=<server-side key>
OPENAI_MODEL=gpt-4.1-mini
LLM_TIMEOUT_SECONDS=35
LLM_MAX_RETRIES=2
EMBEDDING_PROVIDER=openai
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
```

`LLM_PROVIDER=mock` is deterministic and intended for offline product-flow testing. Real mode validates structured output and citations against the run's persisted evidence; invalid or unavailable providers fail visibly and never return mock findings. Provider/model, duration, token counts and estimated cost metadata are stored per investigation run when available. Cost is an estimate based on provider-reported token counts, not a billing statement.

Semantic retrieval is disabled by default. When enabled, documents are split into overlapping chunks, embedded by the configured provider, and searched using PostgreSQL cosine distance. Revision `0008` preserves existing 64-dimensional feature-hash vectors in their original column and adds a separate 1536-dimensional provider vector column with model and dimension metadata. Retrieval filters by the configured model and dimension, so legacy or incompatible vectors are never compared. Existing documents need real-provider re-indexing to populate the new column; their source content and old vectors remain available during that process. Retrieval is project-scoped, limited by query timeout and top-K, and stores the selected chunk, document version, timestamp, excerpt and score as historical context. Current incident evidence remains a separate source category.

Supported document types are UTF-8 `.txt`, `.md`, `.csv` and `.json` under the configured size limit. Invalid/unsupported content, missing embedding configuration and provider errors are recorded as non-indexed states. An administrator can retry or replace a document. Replacing creates a new version; referenced versions remain available for historical provenance. Removing a referenced document hides it from active retrieval while preserving provenance records.

## GitHub

Create a GitHub integration from the project Integrations page/API, store a webhook secret through the encrypted credentials API, and map the repository to an IncidentIQ service. Configure the repository webhook to `POST /api/v1/webhooks/github/{integration_id}` with the GitHub `X-Hub-Signature-256` signature. Supported event families include push, release and deployment status. Delivery IDs are unique and duplicate deliveries do not duplicate deployment/evidence rows. Events near an active incident may be associated by time window; the recorded explanation explicitly describes temporal correlation and does not claim causation.

The GitHub page shows repository mappings and received delivery summaries. GitHub API credentials are not needed to verify inbound HMAC-signed webhooks; the webhook secret is required. Keep the endpoint reachable only over HTTPS in a deployed environment.

## Jira

Connect a Jira Cloud site using its hostname, account email and API token. Credentials are encrypted at rest by the existing integration credential service. Map an IncidentIQ project to a Jira project key. A Jira task can only be created from an approved recommendation. The platform embeds a deterministic idempotency marker in the task and searches for that marker before retrying uncertain creates. Linked task status can be synchronized back to IncidentIQ.

This integration targets Jira Cloud REST API v3 and creates `Task` issue types; Jira projects with a different required issue type may reject creation. Configure the project accordingly. Jira operations require outbound HTTPS and should be tested against a non-production Jira project before use.

## Migrations and verification

Phase 3 migration chain: `0006_system_incident_identity`, `0007_ai_provider_metadata`, `0008_real_knowledge_embeddings`, `0009_engineering_integrations`. Revision 0006 backfills existing alert-generated identities as system while preserving user reporter/actor IDs and types. The chain is tested both from an empty schema and from revision 0005 with preserved user-attributed rows.

Downgrading revision `0008` is guarded: it refuses before changing schema if 1536-dimensional vectors or chunk-level historical references exist, because the prior schema cannot represent those records losslessly. Archive or explicitly remove those Phase 3 records before a downgrade. Existing 64-dimensional vectors are not dropped by the upgrade or downgrade.

Never run migration tests against the application database. Use a disposable PostgreSQL/pgvector database as `TEST_DATABASE_URL`. The isolated runtime verification script creates its own named Compose project and random credentials, migrates only that project's database, and runs backend and browser checks:

```powershell
.\scripts\phase2-runtime-e2e.ps1 -ProjectName incidentiq-phase3-verify
```

The run validates collector metrics in Prometheus, generated dashboard query data, Grafana panels in a browser, Alertmanager-to-incident flow, provider outage and retry recovery, evidence deduplication, system identity, and integration/evidence browser workflows. The script stops its own services and deletes its exact temporary config/token files; it does not remove named volumes. Remove the isolated project volumes separately only when you have verified the project name and no longer need its test data.

Unit tests mock remote OpenAI/GitHub/Jira HTTP responses. They verify request and response handling without requiring external credentials. A successful mock test does not prove a real provider account or remote service is available; perform a controlled test with dedicated credentials before presenting live-provider operation as verified.

## API reference

Use the running FastAPI Swagger UI at `/docs`. Main Phase 3 routes include `/ai/provider`, project knowledge-document CRUD/retry, GitHub repository mapping/events and signed webhook ingestion, Jira project mapping, approved recommendation issue creation, and external issue status sync. Authorization and audit behavior use the existing project membership and audit services.
