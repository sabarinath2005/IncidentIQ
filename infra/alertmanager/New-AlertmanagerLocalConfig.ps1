param(
    [Parameter(Mandatory = $true)]
    [string]$IntegrationId
)

$ErrorActionPreference = 'Stop'
$parsedId = [guid]::Empty
if (-not [guid]::TryParse($IntegrationId, [ref]$parsedId)) {
    throw 'IntegrationId must be a UUID from the IncidentIQ Alertmanager integration.'
}

$secureToken = Read-Host 'Enter the same webhook_token saved in the IncidentIQ integration credentials' -AsSecureString
$tokenPointer = [IntPtr]::Zero
try {
    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if ([string]::IsNullOrWhiteSpace($token)) { throw 'Webhook token cannot be empty.' }

    $localDir = Join-Path $PSScriptRoot 'local'
    New-Item -ItemType Directory -Path $localDir -Force | Out-Null
    $configPath = Join-Path $localDir 'alertmanager.yml'
    $secretPath = Join-Path $localDir 'webhook-token'
    if ((Test-Path -LiteralPath $configPath) -or (Test-Path -LiteralPath $secretPath)) {
        throw 'Local Alertmanager files already exist. Move them aside yourself before regenerating; this script will not overwrite them.'
    }

    $template = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'alertmanager.yml.template') -Raw
    $config = $template.Replace('__INTEGRATION_ID__', $parsedId.ToString())
    [IO.File]::WriteAllText($configPath, $config, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($secretPath, $token, [Text.UTF8Encoding]::new($false))
    Write-Host 'Local Alertmanager configuration is ready. The webhook token was not displayed.'
} finally {
    if ($tokenPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer) }
    $token = $null
    if ($secureToken) { $secureToken.Dispose() }
}
