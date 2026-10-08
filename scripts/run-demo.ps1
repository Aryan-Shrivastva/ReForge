param(
    [ValidateSet("flask-legacy-app", "fastapi-pydantic-v1", "stdlib-deprecations", "setup-py-migration", "no-tests-characterization")]
    [string]$Benchmark = "flask-legacy-app"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root "benchmarks/$Benchmark"
$archive = Join-Path ([System.IO.Path]::GetTempPath()) "reforge-$Benchmark.zip"

if (-not (Test-Path $source)) { throw "Benchmark fixture not found: $source" }
Remove-Item -LiteralPath $archive -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $source "*") -DestinationPath $archive -Force

Write-Host "Starting autonomous discovery run for $Benchmark..."
$form = @{ upload = Get-Item -LiteralPath $archive }
$created = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/runs" -Form $form
$runId = $created.run_id
Write-Host "Run ID: $runId"

do {
    Start-Sleep -Seconds 2
    $run = Invoke-RestMethod -Uri "http://127.0.0.1:8000/runs/$runId"
    Write-Host "Stage: $($run.stage)"
} while ($null -eq $run.final_status)

Write-Host "Completed with status: $($run.final_status)"
Write-Host "Markdown report: http://127.0.0.1:8000/runs/$runId/report"
Write-Host "JSON report:     http://127.0.0.1:8000/runs/$runId/report?format=json"
