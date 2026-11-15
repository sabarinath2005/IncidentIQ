param([string]$ProjectName = 'incidentiq-phase2-e2e', [string]$NetworkSubnet = '10.255.240.0/24')
# Run Phase 2/3 runtime verification under a separate Compose project and ports.
$ErrorActionPreference = 'Stop'

function New-RandomHex([int]$byteCount = 32) {
    $bytes = New-Object byte[] $byteCount
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

# Reject a candidate subnet if it overlaps any current Docker allocation or
# visible Windows IPv4 route. This runs immediately before Compose is invoked.
$networkSubnets = @()
foreach ($networkId in (docker network ls -q)) {
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Docker networks for subnet validation.' }
    $network = (docker network inspect $networkId | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect Docker network '$networkId'." }
    foreach ($ipam in $network.IPAM.Config) {
        if ($ipam.Subnet) { $networkSubnets += "$($network.Name)=$($ipam.Subnet)" }
    }
}
$hostRoutes = @(Get-NetRoute -AddressFamily IPv4 -ErrorAction Stop |
    Where-Object { $_.DestinationPrefix -and $_.DestinationPrefix -ne '0.0.0.0/0' } |
    Select-Object -ExpandProperty DestinationPrefix -Unique)
$networkJson = $networkSubnets -join ','
$routeJson = $hostRoutes -join ','
$overlapCheck = @'
import ipaddress, json, sys
candidate = ipaddress.ip_network(sys.argv[1], strict=True)
conflicts = []
for kind, entries in (("Docker network", sys.argv[2].split(",")), ("Windows route", sys.argv[3].split(","))):
    for entry in filter(None, entries):
        name, cidr = entry.split("=", 1) if "=" in entry else (entry, entry)
        if candidate.overlaps(ipaddress.ip_network(cidr, strict=False)):
            conflicts.append(f"{kind} {name} ({cidr})")
print("\n".join(conflicts))
'@
$overlapsOutput = $overlapCheck | & .\backend\.venv\Scripts\python.exe -c "import sys; exec(sys.stdin.read())" $NetworkSubnet $networkJson $routeJson
if ($LASTEXITCODE -ne 0) { throw 'Could not validate the temporary Compose subnet.' }
$overlaps = @($overlapsOutput | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($overlaps.Count -gt 0) { throw "Temporary subnet '$NetworkSubnet' overlaps: $($overlaps -join '; ')" }

$testPassword = New-RandomHex 24
$jwtSecret = New-RandomHex 48
$grafanaPassword = New-RandomHex 24
$webhookToken = New-RandomHex 32
$env:POSTGRES_PASSWORD = $testPassword
$env:JWT_SECRET = $jwtSecret
$env:GRAFANA_PASSWORD = $grafanaPassword
$env:INTEGRATION_ENCRYPTION_KEY = (& .\backend\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").Trim()
$env:INTEGRATION_ALLOWED_EXTERNAL_HOSTS = ''
$env:LLM_PROVIDER = 'mock'
$env:OPENAI_API_KEY = ''
$env:LLM_TIMEOUT_SECONDS = '2'
$env:LLM_MAX_RETRIES = '0'
$env:EMBEDDING_PROVIDER = 'disabled'
$env:OPENAI_EMBEDDING_MODEL = 'text-embedding-3-small'
$env:EMBEDDING_DIMENSIONS = '1536'
$env:EMBEDDING_CHUNK_WORDS = '900'
$env:EMBEDDING_CHUNK_OVERLAP_WORDS = '120'
$env:KNOWLEDGE_MAX_FILE_BYTES = '2000000'
$env:DEMO_FAILURES_ENABLED = 'true'

$projectName = $ProjectName + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$tempDir = Join-Path $env:TEMP ('incidentiq-phase2-e2e-' + [guid]::NewGuid().ToString('N'))
$configPath = Join-Path $tempDir 'alertmanager.yml'
$tokenPath = Join-Path $tempDir 'webhook-token'
$overridePath = Join-Path $tempDir 'ports.yml'
$compose = @('compose','--project-name',$projectName,'--profile','observability','-f','docker-compose.yml','-f',$overridePath)
$result = [ordered]@{}
try {
New-Item -ItemType Directory -Path $tempDir | Out-Null
$template = Get-Content -LiteralPath .\infra\alertmanager\alertmanager.yml.template -Raw
[IO.File]::WriteAllText($configPath, $template.Replace('__INTEGRATION_ID__', '00000000-0000-4000-8000-000000000001'), [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($tokenPath, $webhookToken, [Text.UTF8Encoding]::new($false))
$configDockerPath = $configPath.Replace('\','/')
$tokenDockerPath = $tokenPath.Replace('\','/')
$override = @"
services:
  db:
    ports: !override ["127.0.0.1:55440:5432"]
  redis:
    ports: !override ["127.0.0.1:56380:6379"]
  api:
    environment:
      FRONTEND_ORIGIN: http://localhost:55173
    ports: !override ["127.0.0.1:58000:8000"]
  frontend:
    environment:
      VITE_API_URL: http://localhost:58000/api/v1
    ports: !override ["127.0.0.1:55173:5173"]
  prometheus:
    ports: !override ["127.0.0.1:59090:9090"]
  grafana:
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
    ports: !override ["127.0.0.1:53000:3000"]
  alertmanager:
    volumes:
      - "${configDockerPath}:/etc/alertmanager/alertmanager.yml:ro"
      - "${tokenDockerPath}:/run/secrets/alertmanager_webhook_token:ro"
    ports: !override ["127.0.0.1:59093:9093"]
  loki:
    ports: !override ["127.0.0.1:53100:3100"]
  tempo:
    ports: !override ["127.0.0.1:53200:3200"]
  otel-collector:
    ports: !override
      - "127.0.0.1:54317:4317"
      - "127.0.0.1:54318:4318"
  payment-api:
    ports: !override ["127.0.0.1:58080:8080"]
networks:
  default:
    ipam:
      config:
        - subnet: "$NetworkSubnet"
"@
[IO.File]::WriteAllText($overridePath, $override, [Text.UTF8Encoding]::new($false))
function Invoke-Phase2Compose([string[]]$CmdArgs) {
    & docker @compose @CmdArgs
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($CmdArgs -join ' ') failed with exit code $LASTEXITCODE" }
}

    Invoke-Phase2Compose @('up','-d','--build','api','frontend','prometheus','grafana','payment-api')
    $apiReady = $false
    for ($i = 0; $i -lt 60; $i++) {
        try { if ((Invoke-RestMethod 'http://127.0.0.1:58000/health' -TimeoutSec 2).status -eq 'ok') { $apiReady = $true; break } } catch {}
        Start-Sleep -Seconds 2
    }
    if (-not $apiReady) { throw 'Isolated API did not become healthy.' }
    foreach ($endpoint in @('http://127.0.0.1:59090/-/ready','http://127.0.0.1:53100/ready','http://127.0.0.1:53200/ready')) {
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            try { if ((Invoke-WebRequest -UseBasicParsing $endpoint -TimeoutSec 3).StatusCode -eq 200) { $ready = $true; break } } catch {}
            Start-Sleep -Seconds 2
        }
        if (-not $ready) { throw "Observability service did not become ready at $endpoint" }
    }
    Invoke-Phase2Compose @('run','--rm','api','alembic','upgrade','head')
    $result.revision = (& docker @compose exec -T api alembic current | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not read isolated migration revision.' }
    Invoke-Phase2Compose @('exec','-T','db','createdb','-U','incident_app','incident_platform_test')
    $env:TEST_DATABASE_URL = "postgresql+psycopg://incident_app:$testPassword@127.0.0.1:55440/incident_platform_test"
    Push-Location .\backend
    try {
        & .\.venv\Scripts\python.exe -m pytest -q --tb=short
        if ($LASTEXITCODE -ne 0) { throw 'Isolated backend PostgreSQL and unit tests failed.' }
    } finally {
        Pop-Location
        $env:TEST_DATABASE_URL = $null
    }

    $registerBody = @{ email = ('phase2-' + [guid]::NewGuid().ToString('N') + '@example.com'); full_name = 'Phase 2 E2E'; password = 'temporary-e2e-password' } | ConvertTo-Json -Compress
    $register = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:58000/api/v1/auth/register' -ContentType 'application/json' -Body $registerBody
    $headers = @{ Authorization = 'Bearer ' + $register.access_token }
    $workspace = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:58000/api/v1/workspaces' -Headers $headers -ContentType 'application/json' -Body (@{name='Phase 2 E2E Workspace'} | ConvertTo-Json -Compress)
    $project = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:58000/api/v1/projects' -Headers $headers -ContentType 'application/json' -Body (@{workspace_id=$workspace.id;name='Observability E2E';description='Disposable integration verification'} | ConvertTo-Json -Compress)
    $service = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:58000/api/v1/projects/$($project.id)/services" -Headers $headers -ContentType 'application/json' -Body (@{name='payment-api';environment='e2e';description='Synthetic payment API'} | ConvertTo-Json -Compress)

    $integrationIds = @{}
    foreach ($provider in @('prometheus','loki','tempo')) {
        $baseUrl = switch ($provider) { 'prometheus' {'http://prometheus:9090'} 'loki' {'http://loki:3100'} 'tempo' {'http://tempo:3200'} }
        $config = @{base_url=$baseUrl;service_id=$service.id}
        if ($provider -eq 'tempo') { $config.service_name='payment-api' }
        $body = @{provider=$provider;config=$config;permission_scope=@()} | ConvertTo-Json -Depth 8 -Compress
        $created = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:58000/api/v1/projects/$($project.id)/integrations" -Headers $headers -ContentType 'application/json' -Body $body
        Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:58000/api/v1/integrations/$($created.id)" -Headers $headers -ContentType 'application/json' -Body (@{enabled=$true} | ConvertTo-Json -Compress) | Out-Null
        $tested = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:58000/api/v1/integrations/$($created.id)/test" -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 10
        if ($tested.connection_test -ne 'CONNECTED') { throw "$provider readiness test failed" }
        $integrationIds[$provider] = $created.id
    }
    $alertIntegration = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:58000/api/v1/projects/$($project.id)/integrations" -Headers $headers -ContentType 'application/json' -Body (@{provider='alertmanager';config=@{service_id=$service.id};permission_scope=@()} | ConvertTo-Json -Depth 8 -Compress)
    Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:58000/api/v1/integrations/$($alertIntegration.id)" -Headers $headers -ContentType 'application/json' -Body (@{enabled=$true} | ConvertTo-Json -Compress) | Out-Null
    Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:58000/api/v1/integrations/$($alertIntegration.id)/credentials" -Headers $headers -ContentType 'application/json' -Body (@{credentials=@{token=$webhookToken}} | ConvertTo-Json -Depth 4 -Compress) | Out-Null
    [IO.File]::WriteAllText($configPath, $template.Replace('__INTEGRATION_ID__', $alertIntegration.id), [Text.UTF8Encoding]::new($false))
    Invoke-Phase2Compose @('up','-d','--build','worker','dispatcher','alertmanager')

    $health = [ordered]@{}
    foreach ($endpoint in @(
        @{name='api';url='http://127.0.0.1:58000/health'},
        @{name='payment-api';url='http://127.0.0.1:58080/health'},
        @{name='prometheus';url='http://127.0.0.1:59090/-/ready'},
        @{name='alertmanager';url='http://127.0.0.1:59093/-/ready'},
        @{name='loki';url='http://127.0.0.1:53100/ready'},
        @{name='tempo';url='http://127.0.0.1:53200/ready'},
        @{name='grafana';url='http://127.0.0.1:53000/api/health'}
    )) {
        $statusCode = 0
        for ($i = 0; $i -lt 30; $i++) {
            try {
                $statusCode = (Invoke-WebRequest -UseBasicParsing $endpoint.url -TimeoutSec 5).StatusCode
                if ($statusCode -eq 200) { break }
            } catch { $statusCode = 0 }
            Start-Sleep -Seconds 2
        }
        if ($statusCode -ne 200) { throw "Isolated $($endpoint.name) health check failed at $($endpoint.url)." }
        $health[$endpoint.name] = $statusCode
    }

    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:58080/payments' -ContentType 'application/json' -Body '{"amount_cents":1250}' | Out-Null
    # Spread failures across Prometheus scrapes so rate() observes counter growth.
    1..10 | ForEach-Object {
        try { Invoke-WebRequest -UseBasicParsing -Method Post -Uri 'http://127.0.0.1:58080/payments' -ContentType 'application/json' -Body '{"amount_cents":1250,"scenario":"http_500"}' -TimeoutSec 10 | Out-Null } catch {}
        Start-Sleep -Seconds 3
    }

    $alertFired = $false
    for ($i = 0; $i -lt 36; $i++) {
        try {
            $alerts = Invoke-RestMethod 'http://127.0.0.1:59090/api/v1/alerts' -TimeoutSec 5
            if ($alerts.data.alerts | Where-Object { $_.labels.alertname -eq 'PaymentApiHighErrorRate' -and $_.state -eq 'firing' }) { $alertFired = $true; break }
        } catch {}
        Start-Sleep -Seconds 5
    }
    if (-not $alertFired) { throw 'Prometheus did not fire PaymentApiHighErrorRate within three minutes.' }
    $incident = $null
    for ($i = 0; $i -lt 30 -and -not $incident; $i++) {
        try {
            $incidents = Invoke-RestMethod -Uri "http://127.0.0.1:58000/api/v1/projects/$($project.id)/incidents" -Headers $headers -TimeoutSec 10
            $incident = $incidents | Where-Object { $_.title -like '*PaymentApiHighErrorRate*' } | Select-Object -First 1
        } catch {
            if ($i -eq 29) { throw }
        }
        if (-not $incident) { Start-Sleep -Seconds 2 }
    }
    if (-not $incident) { throw 'Alertmanager notification did not create an IncidentIQ incident.' }
    $details = $null
    $requiredProviders = @('prometheus','loki','tempo')
    for ($i = 0; $i -lt 60 -and -not $details; $i++) {
        try {
            $candidate = Invoke-RestMethod -Uri "http://127.0.0.1:58000/api/v1/incidents/$($incident.id)" -Headers $headers -TimeoutSec 10
            $collectedProviders = @($candidate.evidence | ForEach-Object { $_.provider } | Where-Object { $_ })
            if (($requiredProviders | Where-Object { $_ -notin $collectedProviders }).Count -eq 0) { $details = $candidate }
        } catch {
            if ($i -eq 59) { throw }
        }
        if (-not $details) { Start-Sleep -Seconds 3 }
    }
    if (-not $details) { throw 'Celery did not persist metrics, logs and trace evidence for the alert-created incident.' }
    if ($incident.reporter_type -ne 'system') { throw 'Monitoring-created incident did not retain explicit system reporter attribution.' }
    if (-not ($details.activity | Where-Object { $_.event_type -eq 'monitoring_alert_received' -and $_.actor_type -eq 'system' })) {
        throw 'Monitoring timeline activity did not retain explicit system actor attribution.'
    }

    $metricQuery = [uri]::EscapeDataString('payment_http_requests_total{service_name="payment-api"}')
    $metricResult = Invoke-RestMethod "http://127.0.0.1:59090/api/v1/query?query=$metricQuery" -TimeoutSec 10
    if ($metricResult.data.result.Count -lt 1) { throw 'Collector-exported payment metrics were not queryable in Prometheus.' }
    $rateQuery = [uri]::EscapeDataString('sum(rate(payment_http_requests_total{service_name="payment-api"}[5m]))')
    $rateResult = Invoke-RestMethod "http://127.0.0.1:59090/api/v1/query?query=$rateQuery" -TimeoutSec 10
    if ($rateResult.data.result.Count -lt 1) { throw 'Payment request rate panel query had no data in Prometheus.' }

    # Exercise outage, Celery retry, recovery and same-window evidence idempotency.
    $windowEnd = [DateTime]::UtcNow
    $windowStart = $windowEnd.AddMinutes(-15)
    $collectBody = @{ start=$windowStart.ToString('o'); end=$windowEnd.ToString('o') } | ConvertTo-Json -Compress
    Invoke-Phase2Compose @('stop','loki')
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:58000/api/v1/incidents/$($incident.id)/telemetry/collect" -Headers $headers -ContentType 'application/json' -Body $collectBody | Out-Null
    $outageRecorded = $false
    for ($i = 0; $i -lt 40; $i++) {
        $candidate = Invoke-RestMethod -Uri "http://127.0.0.1:58000/api/v1/incidents/$($incident.id)" -Headers $headers -TimeoutSec 10
        $lokiSource = $candidate.telemetry_sources | Where-Object { $_.provider -eq 'loki' } | Select-Object -First 1
        if ($lokiSource.status -eq 'unavailable') { $outageRecorded = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $outageRecorded) { throw 'Loki provider outage was not recorded as unavailable.' }
    Invoke-Phase2Compose @('start','loki')
    $recovered = $false
    for ($i = 0; $i -lt 60; $i++) {
        $candidate = Invoke-RestMethod -Uri "http://127.0.0.1:58000/api/v1/incidents/$($incident.id)" -Headers $headers -TimeoutSec 10
        $lokiSource = $candidate.telemetry_sources | Where-Object { $_.provider -eq 'loki' } | Select-Object -First 1
        if ($lokiSource.status -eq 'completed' -and $lokiSource.last_checked_at -and
            ([DateTimeOffset]$lokiSource.last_checked_at) -gt $windowEnd) { $recovered = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $recovered) { throw 'Celery did not retry telemetry collection successfully after Loki recovered.' }
    $evidenceKeys = @($candidate.evidence | ForEach-Object { "$($_.provider)|$($_.source_type)|$($_.content_hash)" })
    $uniqueEvidenceKeys = @($evidenceKeys | Sort-Object -Unique)
    if ($evidenceKeys.Count -ne $uniqueEvidenceKeys.Count) { throw 'Provider retry produced duplicate evidence hashes.' }
    $idempotent = $true

    # Browser-level dashboard check against the running isolated Grafana instance.
    $env:GRAFANA_BASE_URL = 'http://127.0.0.1:53000'
    $env:PLAYWRIGHT_BASE_URL = 'http://localhost:55173'
    $env:E2E_API_URL = 'http://127.0.0.1:58000/api/v1'
    Push-Location .\frontend
    try {
        & npm.cmd run test:e2e -- e2e/core-flow.spec.ts
        if ($LASTEXITCODE -ne 0) { throw 'IncidentIQ integration and evidence browser E2E failed.' }
        & npm.cmd exec playwright -- test e2e/phase2-grafana.spec.ts
        if ($LASTEXITCODE -ne 0) { throw 'Grafana browser dashboard verification failed.' }
    } finally {
        Pop-Location
        $env:GRAFANA_BASE_URL = $null
        $env:PLAYWRIGHT_BASE_URL = $null
        $env:E2E_API_URL = $null
    }

    $result.health = $health
    $result.collector_metrics_queryable = $true
    $result.prometheus_alert_fired = $alertFired
    $result.incident_created = $true
    $result.provider_outage_recorded = $outageRecorded
    $result.provider_retry_recovered = $recovered
    $result.evidence_idempotent = $idempotent
    $result.grafana_browser_dashboard = $true
    $result.evidence_providers = @($details.evidence | ForEach-Object { $_.provider } | Where-Object { $_ } | Sort-Object -Unique)
    $result.telemetry_sources = $details.telemetry_sources
    Write-Output ($result | ConvertTo-Json -Depth 8 -Compress)
}
finally {
    try { Invoke-Phase2Compose @('stop') } catch {}
    # Capture volumes mounted by this unique project before Compose removes
    # the containers, including anonymous volumes created by image VOLUME paths.
    $testVolumeNames = @()
    try {
        foreach ($containerId in (docker ps -aq --filter "label=com.docker.compose.project=$projectName")) {
            $container = (docker inspect $containerId | ConvertFrom-Json)[0]
            if ($LASTEXITCODE -eq 0 -and $container.Config.Labels.'com.docker.compose.project' -eq $projectName) {
                $testVolumeNames += @($container.Mounts | Where-Object Type -eq 'volume' | ForEach-Object { $_.Name })
            }
        }
        $testVolumeNames += @(docker volume ls -q --filter "label=com.docker.compose.project=$projectName")
        $testVolumeNames = @($testVolumeNames | Where-Object { $_ } | Sort-Object -Unique)
    } catch { Write-Warning "Could not enumerate all isolated test volumes: $($_.Exception.Message)" }

    # Remove only containers/network named by this unique Compose project.
    try {
        & docker @compose down --remove-orphans
        if ($LASTEXITCODE -ne 0) { throw "docker compose down exited with code $LASTEXITCODE" }
    } catch { Write-Warning "Could not clean isolated Compose project '$projectName': $($_.Exception.Message)" }
    # Remove a captured volume only after confirming it has no other container
    # references and is either project-labelled or Docker-created anonymous.
    try {
        foreach ($volumeName in $testVolumeNames) {
            $references = @(docker ps -aq --filter "volume=$volumeName")
            $volume = (docker volume inspect $volumeName | ConvertFrom-Json)[0]
            $labelOwned = $LASTEXITCODE -eq 0 -and
                $volume.Labels.'com.docker.compose.project' -eq $projectName -and
                $volume.Name.StartsWith($projectName + '_', [StringComparison]::Ordinal)
            $anonymousOwned = $LASTEXITCODE -eq 0 -and
                $volume.Labels.'com.docker.volume.anonymous' -ne $null -and
                $volume.Name -match '^[0-9a-f]{64}$'
            if ($LASTEXITCODE -eq 0 -and $references.Count -eq 0 -and ($labelOwned -or $anonymousOwned)) {
                docker volume rm $volume.Name | Out-Null
                if ($LASTEXITCODE -ne 0) { Write-Warning "Could not remove test-owned volume '$($volume.Name)'" }
            } else {
                Write-Warning "Retained volume '$volumeName' because ownership or exclusive use could not be confirmed."
            }
        }
    } catch { Write-Warning "Could not completely clean isolated test volumes: $($_.Exception.Message)" }
    try {
        $tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        $tempFull = [IO.Path]::GetFullPath($tempDir)
        if ($tempFull.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
            foreach ($file in @($configPath,$tokenPath,$overridePath)) {
                if (Test-Path -LiteralPath $file) { [IO.File]::Delete($file) }
            }
            # Do not recurse: retain any logs or reports written into this folder.
            if ((Test-Path -LiteralPath $tempFull) -and -not (Get-ChildItem -LiteralPath $tempFull -Force | Select-Object -First 1)) {
                [IO.Directory]::Delete($tempFull)
            }
        } else {
            Write-Warning "Skipped temporary-file cleanup because the path was outside TEMP: '$tempFull'"
        }
    } catch { Write-Warning "Could not completely clean temporary verification files: $($_.Exception.Message)" }
    finally {
        $testPassword=$null; $jwtSecret=$null; $grafanaPassword=$null; $webhookToken=$null
        $env:INTEGRATION_ENCRYPTION_KEY=$null; $env:JWT_SECRET=$null; $env:POSTGRES_PASSWORD=$null
        $env:GRAFANA_PASSWORD=$null; $env:DEMO_FAILURES_ENABLED=$null; $env:OPENAI_API_KEY=$null
        $env:LLM_TIMEOUT_SECONDS=$null; $env:LLM_MAX_RETRIES=$null; $env:EMBEDDING_PROVIDER=$null
        $env:OPENAI_EMBEDDING_MODEL=$null; $env:EMBEDDING_DIMENSIONS=$null
        $env:EMBEDDING_CHUNK_WORDS=$null; $env:EMBEDDING_CHUNK_OVERLAP_WORDS=$null
        $env:KNOWLEDGE_MAX_FILE_BYTES=$null
    }
}
