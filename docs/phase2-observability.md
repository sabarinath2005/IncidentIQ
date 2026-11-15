# Phase 2 observability integrations

This optional local profile connects IncidentIQ to Prometheus, Alertmanager, Loki, Tempo and an OpenTelemetry Collector. A small Payment API produces synthetic payments, metrics, structured logs and traces. It uses a separate SQLite file inside its container; it does not access the IncidentIQ application database.

## Safety and scope

- The profile is disabled unless `--profile observability` is supplied. It adds separate named volumes for Alertmanager, Loki and Tempo and does not replace the existing PostgreSQL, Redis, Prometheus or Grafana volumes.
- The Payment API binds to `127.0.0.1:8080`. Its controlled failure scenarios are disabled by default. Do not expose the demo endpoints on a public interface.
- Provider HTTP requests are bounded by timeout, response size and query windows. HTTP is allowed only for exact local Compose service hostnames. External endpoints are denied unless their exact hostnames are added to `INTEGRATION_ALLOWED_EXTERNAL_HOSTS`; they must use HTTPS and resolve only to public addresses. Configure a service allowlist and network egress policy before using endpoints outside the local stack.
- Provider payloads, logs and traces are treated as untrusted. Sensitive attributes are filtered/redacted before evidence persistence. Do not send production personal data to this demo stack.
- API startup runs Uvicorn directly and never migrates automatically.

## Schema prerequisite

Phase 2 adds migration `0005_observability_alerts` for alert fingerprints and evidence provenance. The existing live database remains at its previous revision until an operator approves migration. **Only after making and verifying a database backup and receiving explicit approval**, apply it with:

```powershell
docker compose run --rm api alembic upgrade head
```

Never apply it to a live or shared database before that backup and approval. A migration is not needed to start ordinary API, worker or dispatcher containers, but telemetry evidence/webhook persistence requires 0005.

## Configure integrations

1. Start/confirm the ordinary IncidentIQ app and create one project service named `payment-api`.
2. In **Integrations**, create Prometheus, Loki and Tempo integrations with the configured service UUID. Use these base URLs from the Compose network:

   ```json
   {"base_url":"http://prometheus:9090","service_id":"<service-uuid>"}
   {"base_url":"http://loki:3100","service_id":"<service-uuid>"}
   {"base_url":"http://tempo:3200","service_id":"<service-uuid>","service_name":"payment-api"}
   ```

   Enable each integration and use **Test** to verify its health endpoint. Do not put credentials in `config`; use the credential replacement field.
3. Create an Alertmanager integration with `{"service_id":"<service-uuid>"}`. Generate a long random webhook token locally and store it as an encrypted credential using **Replace credential**. Record the integration UUID shown by the API/UI.
4. From the repository root, run the setup script with that UUID. It prompts for the same token without echoing it and writes ignored local files under `infra/alertmanager/local/`:

   ```powershell
   .\infra\alertmanager\New-AlertmanagerLocalConfig.ps1 -IntegrationId <integration-uuid>
   ```

   The script refuses to overwrite existing local Alertmanager files. Protect these files and do not commit them.
5. Start the optional services:

   ```powershell
   docker compose --profile observability up -d --build
   docker compose --profile observability ps
   ```

   Grafana is available at `http://localhost:3000`; the provisioned dashboard is **IncidentIQ Payment API**. Loki and Tempo HTTP ports and the OTLP receiver ports bind to loopback only. Grafana still uses the project's configured `GRAFANA_PASSWORD`.

## Generate a controlled demo incident

Set `DEMO_FAILURES_ENABLED=true` in the shell used to start Compose (or configure it through the local deployment environment) to enable deliberately failing requests. No application source change or application database migration is needed for this toggle.

```powershell
$env:DEMO_FAILURES_ENABLED = 'true'
docker compose --profile observability up -d payment-api
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/payments -ContentType 'application/json' -Body '{"amount_cents":1250}'
1..8 | ForEach-Object { try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/payments -ContentType 'application/json' -Body '{"amount_cents":1250,"scenario":"http_500"}' } catch {} }
```

The Payment API metrics are scraped by Prometheus. Alert rules cover error ratio and p95 latency; Alertmanager sends authenticated firing and resolved notifications to the configured IncidentIQ integration. Alert fingerprints are deduplicated. A new firing incident queues bounded Prometheus/Loki/Tempo collection through Celery and records source evidence with provider/query/window provenance. Transient provider or database errors receive up to three bounded Celery retries with exponential backoff; evidence writes are idempotent. Automatic resolution of an IncidentIQ incident is intentionally not performed; incident closure remains a human decision.

For an existing incident, **Collect telemetry** queues collection for its last hour (or a caller-supplied bounded window). Collection accepts at most 24 hours and persists provider outcomes in integration sync history. The incident page displays the latest source state.

## Verify the data path

- `http://127.0.0.1:9090/targets` should show `payment-api` and `otel-collector` targets up.
- Query `payment_http_requests_total{service_name="payment-api"}` in Prometheus.
- In Grafana, inspect request rate, error ratio, p95 latency, database pool utilization, and Loki logs.
- Search Tempo for service `payment-api`; select a trace and verify spans have a trace ID.
- In IncidentIQ, check the Alertmanager integration sync history, the generated incident, the evidence ledger and source-availability indicators.
- Check startup logs with `docker compose --profile observability logs --tail 100 api worker dispatcher prometheus alertmanager loki tempo otel-collector payment-api`.

## Troubleshooting

- **Alertmanager reports 401:** ensure the token in the encrypted IncidentIQ integration credential exactly matches the ignored local token file, and that the URL contains the same integration UUID. Recreate local files only after manually moving existing files aside.
- **Alertmanager integration returns 404:** confirm the provider is Alertmanager and the integration is enabled.
- **No incident appears:** check Prometheus targets, alert rule state at `http://localhost:9090/alerts`, Alertmanager receiver status, the API logs and the integration's configured service UUID.
- **No telemetry evidence:** confirm the provider integration is enabled, service UUID matches the incident's project, provider readiness test succeeds, and the worker/dispatcher are running. Review sync-run error codes.
- **Logs or traces are absent:** confirm Collector logs show successful exports and the Payment API OTLP endpoint is `http://otel-collector:4318`.
- **Unsafe endpoint rejected:** local provider endpoints must use exact Compose service names; arbitrary private networks, redirects, credentials in URLs and non-HTTPS public endpoints are rejected.

## Tests

Run non-database backend tests and frontend checks:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest -q
Set-Location ..\frontend
npm test -- --run
npm run build
```

PostgreSQL migration, authorization and Alertmanager persistence tests require `TEST_DATABASE_URL` to point only to the dedicated disposable database named `incident_platform_test`, using `postgresql+psycopg`. They create and drop random schemas; they do not fall back to `DATABASE_URL`. Never set this variable to the live application database.
