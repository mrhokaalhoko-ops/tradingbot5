$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$base = Join-Path $projectRoot "research_data\v5_aggtrade_dataset"
$manifestPath = Join-Path $base "manifest.json"
$pidPath = Join-Path $base "builder.pid"
$expected = 920

if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "V5 manifest not found: $manifestPath"
}

$initialManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$initialDone = @(
    $initialManifest.tasks.PSObject.Properties |
        Where-Object { $_.Value.status -eq "OK" }
).Count
$watchStarted = Get-Date

while ($true) {
    try {
        $now = Get-Date
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $tasks = @($manifest.tasks.PSObject.Properties)
        $done = @($tasks | Where-Object { $_.Value.status -eq "OK" }).Count
        $missing = @($tasks | Where-Object { $_.Value.status -eq "MISSING" }).Count
        $invalid = @($tasks | Where-Object { $_.Value.status -eq "INVALID" }).Count
        $remaining = $expected - $done
        $percent = [math]::Round(100 * $done / $expected, 2)

        $parquets = @(
            Get-ChildItem -LiteralPath (Join-Path $base "features_1m") -Recurse -Filter *.parquet -File -ErrorAction SilentlyContinue
        )
        $archives = @(
            Get-ChildItem -LiteralPath (Join-Path $base "archives") -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like "*.zip" -or $_.Name -like "*.part" }
        )
        $parquetBytes = ($parquets | Measure-Object Length -Sum).Sum
        $archiveBytes = ($archives | Measure-Object Length -Sum).Sum
        $diskBytes = [double]$parquetBytes + [double]$archiveBytes

        $latestCompleted = $tasks |
            Where-Object { $_.Value.status -eq "OK" } |
            Select-Object -Last 1
        $latestFile = $archives | Sort-Object LastWriteTime -Descending | Select-Object -First 1

        $pidValue = [int](Get-Content -LiteralPath $pidPath | Select-Object -First 1)
        $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        $processStatus = if ($process) { "RUNNING" } else { "STOPPED" }
        $processColor = if ($process) { "Green" } else { "Red" }

        $newlyCompleted = $done - $initialDone
        $elapsedHours = [math]::Max(($now - $watchStarted).TotalHours, 0.0001)
        $rate = if ($newlyCompleted -gt 0) { $newlyCompleted / $elapsedHours } else { 0 }
        $eta = if ($rate -gt 0) { [math]::Round($remaining / $rate, 1) } else { $null }

        Clear-Host
        Write-Host "V5 MICROSTRUCTURE DATASET - LIVE PROGRESS" -ForegroundColor Cyan
        Write-Host ("Updated: {0}" -f $now.ToString("yyyy-MM-dd HH:mm:ss"))
        Write-Host ""
        Write-Host ("Process: {0}  PID: {1}" -f $processStatus, $pidValue) -ForegroundColor $processColor
        Write-Host ("Completed: {0} / {1}  ({2}%)" -f $done, $expected, $percent) -ForegroundColor Yellow
        Write-Host ("Remaining: {0} | Missing: {1} | Invalid: {2}" -f $remaining, $missing, $invalid)
        Write-Host ("Disk used: {0:N3} GB | Parquet: {1} | Archives/parts: {2}" -f ($diskBytes / 1GB), $parquets.Count, $archives.Count)
        Write-Host ("Latest completed: {0}" -f $(if ($latestCompleted) { $latestCompleted.Name } else { "None" }))
        Write-Host ("Current/latest file: {0} ({1:N2} MB)" -f $(if ($latestFile) { $latestFile.Name } else { "None" }), $(if ($latestFile) { $latestFile.Length / 1MB } else { 0 }))
        if ($process) {
            Write-Host ("RAM working set: {0:N1} MB | CPU total: {1:N1}s" -f ($process.WorkingSet64 / 1MB), $process.CPU)
        }
        if ($rate -gt 0) {
            Write-Host ("Rate since monitor start: {0:N1} files/hour | ETA: ~{1} hours" -f $rate, $eta)
        } else {
            Write-Host "Rate/ETA: measuring until the next file completes..."
        }
        Write-Host ""
        Write-Host "Read-only monitor. Press Ctrl+C to close it; the downloader will continue." -ForegroundColor DarkGray
    }
    catch {
        Write-Host ("Monitor read error: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }

    Start-Sleep -Seconds 10
}
