# IncidentIQ

IncidentIQ is a final-year project prototype for incident investigation and issue management. The MVP combines a FastAPI API, a React interface, PostgreSQL with pgvector, Celery/Redis background work, a resumable LangGraph workflow, run-specific evidence citations, historical context references, and human-approved simulated remediation.

The default investigation provider is deterministic demo logic. It makes no accuracy claims. An optional OpenAI chat-completions provider can be selected with server-side environment settings; it is never required for the demo. Findings show a heuristic ranking score, not a calibrated probability.

## Local environment

The supported full local path is Docker Compose with the pgvector PostgreSQL image. Do not replace the application database with SQLite. A separate SQLite file is used only by the optional synthetic Payment API sample.

## Run locally on Windows

1. Install Docker Desktop and start it.
2. Copy .env.example to .env. Replace POSTGRES_PASSWORD with a random hexadecimal value and JWT_SECRET with a long random value. For example, PowerShell can generate these with `[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))`. Keep LLM_PROVIDER=mock for an offline demo. Never commit .env.
   To enable encrypted integration credentials, install backend requirements and generate a stable Fernet key once with `backend\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Put that value in `INTEGRATION_ENCRYPTION_KEY` in `.env` and keep a protected backup. Never rotate or regenerate it without decrypting and re-encrypting stored integration credentials. Credential writes fail closed when the key is unset or invalid; the rest of the app can start without it.
3. From the repository root, start the stack:

       docker compose up --build

4. Open http://localhost:5173. The API and Swagger UI are at http://localhost:8000 and http://localhost:8000/docs.
5. Register a user, create a workspace and project, then choose Create demo scenario. Add an issue or more evidence, run an investigation, review its hypotheses, create an approval request, approve it, and run the remediation simulation.

API startup, worker startup, and dispatcher startup do not apply database migrations. PostgreSQL data and uploaded artifacts use named volumes. A Celery Beat dispatcher scans the transactional outbox every ten seconds, so committed requests can be recovered if immediate dispatch is interrupted. To stop the services without removing data, press Ctrl+C and run `docker compose down` (do not add `-v`).

### Explicit database migrations

Migrations are a manual deployment operation. Before applying one, create and verify a restorable backup of the target database and obtain explicit approval from the responsible maintainer. Only then, from the repository root, run:

       docker compose run --rm api alembic upgrade head

Do not include this command in ordinary API restarts or deployments. The API, worker, and dispatcher must not run migrations automatically.

### Optional hosted LLM

Set LLM_PROVIDER=openai and OPENAI_API_KEY in the untracked root .env. The key stays in the backend environment. If the provider is unset, the application uses its deterministic mock provider. Do not place provider credentials in frontend variables.

## Tests

Backend tests run from backend:

       .\.venv\Scripts\python.exe -m pytest

The database schema tests inspect declared PostgreSQL constraints without connecting to a database. Database-backed migration and integration tests require a dedicated PostgreSQL/pgvector test database and should not target personal or production data.

Frontend unit/component tests run from frontend:

       npm test
       npm run build

The browser workflow is in frontend/e2e. With the app stack running, install the Chromium browser once with npx playwright install chromium, then run:

       npm run test:e2e

## Architecture notes

- backend/app/models.py declares scoped domain entities and composite foreign keys.
- backend/migrations contains Alembic schema history. Review model changes and generate/review a migration before applying it.
- backend/app/services/investigations.py persists run steps and attempts, collects run-specific evidence, and stores findings/recommendations.
- backend/app/ai.py defines the LangGraph workflow. PostgreSQL checkpoints use the stable investigation thread ID.
- backend/app/services/retrieval.py filters historical retrieval to the same project and stores provenance references. Its current 64-dimensional feature-hash vector is a deterministic retrieval demonstration, not a pretrained semantic embedding model.
- backend/app/services/outbox.py publishes committed events to Celery. The queue contains IDs; PostgreSQL remains the source of truth.
- Large uploaded text artifacts are stored under the configured artifact directory. PostgreSQL holds their scoped metadata, hash and storage reference.

## MVP boundaries

The seeded demo supplies repeatable incident evidence and one historical case. The optional Phase 2 observability profile provides live Prometheus, Alertmanager, Loki, Tempo and OpenTelemetry telemetry flows for the synthetic Payment API. See [the Phase 2 observability guide](docs/phase2-observability.md). External provider availability and production suitability still depend on deployment-specific network, security and retention configuration. The simulated remediation executor records that no production system changed.

The research direction is evidence-grounded and uncertainty-aware diagnosis. No research results or novelty claims are included; evaluate against a fixed held-out scenario set before reporting results.
