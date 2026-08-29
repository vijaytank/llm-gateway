<#
.SYNOPSIS
    Cross-platform PowerShell launcher for LLM Gateway.
.DESCRIPTION
    Checks Docker prerequisites, starts the full gateway stack with Docker Compose,
    verifies service health, and opens the Web UI in the default browser.
#>

[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$Build,
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"

function Write-Banner {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host "           LLM Gateway — Starting Services              " -ForegroundColor Cyan
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host ""
}

function Test-CommandAvailable {
    param([string]$Command)
    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Check-Docker {
    Write-Host "--> Checking Docker installation..." -ForegroundColor Yellow
    if (-not (Test-CommandAvailable "docker")) {
        Write-Host "ERROR: 'docker' CLI was not found on your PATH." -ForegroundColor Red
        Write-Host "Please install Docker Desktop: https://www.docker.com/products/docker-desktop" -ForegroundColor Yellow
        exit 1
    }

    try {
        $null = docker info 2>&1
    } catch {
        Write-Host "ERROR: Docker daemon is not running." -ForegroundColor Red
        Write-Host "Please start Docker Desktop and wait for it to finish starting." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  [OK] Docker is installed and running." -ForegroundColor Green
}

function Ensure-Environment {
    $envPath = Join-Path $PSScriptRoot ".env"
    $configPath = Join-Path $PSScriptRoot "gateway_config.yaml"

    if (-not (Test-Path $envPath) -or -not (Test-Path $configPath)) {
        Write-Host "--> Initializing configuration..." -ForegroundColor Yellow
        if (Test-CommandAvailable "python") {
            try {
                python (Join-Path $PSScriptRoot "wizard\setup.py") --docker --regenerate-config --force
            } catch {
                Write-Host "  Note: Host Python configuration generator skipped. Using Docker init-env."
            }
        }
    }
}

function Start-DockerStack {
    Write-Host "--> Launching Docker Compose stack (profile: full)..." -ForegroundColor Yellow

    # Resolve UI port from .env (default 4002) so the health-check URL is accurate
    # even if the operator has customised UI_PORT.
    $script:UiPort = 4002
    $envFile = Join-Path $PSScriptRoot ".env"
    if (Test-Path $envFile) {
        $portLine = Get-Content $envFile | Where-Object { $_ -match '^UI_PORT=' } | Select-Object -First 1
        if ($portLine) { $script:UiPort = [int]($portLine -split '=', 2)[1].Trim() }
    }
    
    $composeCmd = "docker"
    $composeArgs = @("compose", "-f", "docker/docker-compose.yml", "--profile", "full", "up", "-d")
    if ($Build) {
        $composeArgs += "--build"
    }

    & $composeCmd @composeArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to start Docker Compose stack." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

function Wait-ForHealth {
    param([int]$MaxWait)
    Write-Host "--> Waiting for services to become healthy (timeout: ${MaxWait}s)..." -ForegroundColor Yellow
    
    $healthUrl = "http://localhost:$($script:UiPort)/health"
    $startTime = Get-Date
    $ready = $false

    while (((Get-Date) - $startTime).TotalSeconds -lt $MaxWait) {
        try {
            $resp = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($resp.status -eq "ok" -or $resp.status -eq "healthy") {
                $ready = $true
                break
            }
        } catch {
            # Still starting up
        }
        Start-Sleep -Seconds 2
    }

    if (-not $ready) {
        Write-Host "WARNING: Services did not report healthy within ${MaxWait} seconds." -ForegroundColor Yellow
        Write-Host "Check container logs: docker compose -f docker/docker-compose.yml logs" -ForegroundColor Yellow
    } else {
        Write-Host "  [OK] All services healthy!" -ForegroundColor Green
    }
}

function Show-SummaryAndOpenBrowser {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "         LLM Gateway is Ready and Serving!              " -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "  * Web UI:         http://localhost:$($script:UiPort)" -ForegroundColor Cyan
    Write-Host "  * OpenAI Gateway: http://localhost:4000" -ForegroundColor Cyan
    Write-Host "  * Anthropic Port: http://localhost:4001" -ForegroundColor Cyan
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host ""

    if (-not $NoBrowser) {
        Write-Host "Opening Web UI in your default browser..." -ForegroundColor Gray
        Start-Process "http://localhost:$($script:UiPort)"
    }
}

# --- Execution ---
Write-Banner
Check-Docker
Ensure-Environment
Start-DockerStack
Wait-ForHealth -MaxWait $TimeoutSeconds
Show-SummaryAndOpenBrowser
