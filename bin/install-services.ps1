<#
    Install the three API wrappers as Windows services.

    They have been running as bare uvicorn processes in RDP console windows,
    which die when the window closes or the session ends. Three died in a
    single day, each discovered only when a query failed.

    Services run as LocalSystem, which reads Machine-scope environment
    variables - not the ones in your shell. Credentials are checked at that
    scope before anything is installed, because a service that starts and then
    fails every request is harder to diagnose than one that never starts.

    Run from an elevated PowerShell:
        powershell -ExecutionPolicy Bypass -File C:\MCP\bin\install-services.ps1
#>

param(
    [string]$Root   = "C:\MCP",
    [string]$Python = "C:\Python\python.exe"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated PowerShell (Run as Administrator)."
    }
}

function Get-MachineVar($name) {
    [Environment]::GetEnvironmentVariable($name, "Machine")
}

$services = @(
    @{ Name = "mcp-vcenter";  Dir = "vcenter";     Module = "vcenter_api:app";       Port = 8080
       Required = @("VCENTER_USER", "VCENTER_PASSWORD") }
    @{ Name = "mcp-vcfops";   Dir = "vcfops";      Module = "vcf_ops_api:app";       Port = 8081
       Required = @("OPS_PASSWORD") }
    @{ Name = "mcp-networks"; Dir = "vcfNetworks"; Module = "vcf_networks_api:app";  Port = 8082
       Required = @("NI_USERNAME", "NI_PASSWORD") }
    @{ Name = "mcp-logs";     Dir = "vcfLogs";     Module = "vcf_logs_api:app";      Port = 8083
       Required = @("LOGS_PASSWORD") }
    @{ Name = "mcp-veeam";    Dir = "veeam";       Module = "veeam_api:app";         Port = 8084
       Required = @("VEEAM_USER", "VEEAM_PASSWORD") }
)

Assert-Admin

if (-not (Test-Path $Python)) { throw "Python not found at $Python" }

# --- Check credentials at the scope the services will actually read ---------
$missing = @()
foreach ($svc in $services) {
    foreach ($var in $svc.Required) {
        if ([string]::IsNullOrWhiteSpace((Get-MachineVar $var))) {
            $missing += "$var  (needed by $($svc.Name))"
        }
    }
}
if ($missing.Count -gt 0) {
    Write-Host "`nMissing Machine-scope environment variables:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Host @"

These exist in your shell but not where a service can see them, or not at all.
Set each one with:

  [Environment]::SetEnvironmentVariable("NAME","value","Machine")

Nothing has been installed.
"@
    exit 1
}
Write-Host "All required credentials present at Machine scope." -ForegroundColor Green

# --- NSSM: uvicorn is not a service-aware binary and needs a wrapper --------
$nssm = "C:\Windows\System32\nssm.exe"
if (-not (Test-Path $nssm)) {
    Write-Host "Installing NSSM..."
    $zip = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $env:TEMP -Force
    Copy-Item (Join-Path $env:TEMP "nssm-2.24\win64\nssm.exe") $nssm -Force
    Remove-Item $zip -Force
}

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

foreach ($svc in $services) {
    $name    = $svc.Name
    $workDir = Join-Path $Root $svc.Dir
    if (-not (Test-Path $workDir)) { throw "Missing directory: $workDir" }

    Write-Host "`n=== $name (port $($svc.Port)) ===" -ForegroundColor Cyan

    # Free the port first: a console instance would keep the service from binding
    Get-NetTCPConnection -LocalPort $svc.Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object {
            Write-Host "  stopping process $_ holding port $($svc.Port)"
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }

    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        & $nssm stop   $name confirm | Out-Null
        & $nssm remove $name confirm | Out-Null
    }

    $svcArgs = "-m uvicorn $($svc.Module) --host 0.0.0.0 --port $($svc.Port)"
    & $nssm install $name $Python $svcArgs    | Out-Null
    & $nssm set $name AppDirectory $workDir | Out-Null
    & $nssm set $name AppStdout (Join-Path $logDir "$name.log") | Out-Null
    & $nssm set $name AppStderr (Join-Path $logDir "$name.log") | Out-Null
    & $nssm set $name AppRotateFiles 1        | Out-Null
    & $nssm set $name AppRotateBytes 10485760 | Out-Null
    & $nssm set $name Start SERVICE_AUTO_START | Out-Null
    # Restart on failure, but back off so a bad config does not spin
    & $nssm set $name AppExit Default Restart | Out-Null
    & $nssm set $name AppRestartDelay 5000    | Out-Null
    & $nssm set $name Description "Read-only API wrapper for the on-prem AI assistant" | Out-Null

    Start-Service $name
    Write-Host "  installed and started" -ForegroundColor Green
}

Start-Sleep -Seconds 6

Write-Host "`n=== Health ===" -ForegroundColor Cyan
$checks = @{ 8080 = "/health"; 8081 = "/ops/health"; 8082 = "/ni/health"
             8083 = "/logs/health"; 8084 = "/veeam/health" }
foreach ($port in 8080, 8081, 8082, 8083, 8084) {
    $url = "http://localhost:$port$($checks[$port])"
    try {
        $resp = Invoke-RestMethod -Uri $url -TimeoutSec 20
        $state = if ($resp.status) { $resp.status } else { "responded" }
        $colour = if ($state -eq "ok") { "Green" } else { "Yellow" }
        Write-Host ("  {0}  {1}" -f $port, $state) -ForegroundColor $colour
        if ($resp.error) { Write-Host "        $($resp.error)" -ForegroundColor Yellow }
    } catch {
        Write-Host ("  {0}  FAILED: {1}" -f $port, $_.Exception.Message) -ForegroundColor Red
        Write-Host ("        see $logDir\") -ForegroundColor Red
    }
}

Write-Host @"

Done. These now start at boot and restart on failure.

  Restart one:  Restart-Service mcp-networks
  Logs:         Get-Content $logDir\mcp-networks.log -Tail 40

Reboot to confirm they come back without a login - that test has never been run.
"@
