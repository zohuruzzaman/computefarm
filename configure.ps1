param(
    [string]$RpiIP = "",
    [string]$StoragePC = "",
    [string]$StoragePCIP = "",
    [string]$ShareName = "ComputeFarm",
    [string]$RedisUser = "computefarm",
    [string]$RedisPassword = "",
    [int]$CpuConcurrency = 2,
    [int]$GpuConcurrency = 1,
    [string]$SolveScript = "solve_gsz_geocmd.ps1",
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Read-Value {
    param(
        [string]$Prompt,
        [string]$Default = "",
        [switch]$Required
    )

    if ($NonInteractive) {
        if ($Required -and [string]::IsNullOrWhiteSpace($Default)) {
            throw "$Prompt is required in non-interactive mode."
        }
        return $Default
    }

    if ([string]::IsNullOrWhiteSpace($Default)) {
        $value = Read-Host $Prompt
    } else {
        $value = Read-Host "$Prompt [$Default]"
        if ([string]::IsNullOrWhiteSpace($value)) {
            $value = $Default
        }
    }

    if ($Required -and [string]::IsNullOrWhiteSpace($value)) {
        throw "$Prompt is required."
    }
    return $value
}

function Set-Text {
    param(
        [string]$Path,
        [scriptblock]$Transform
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "File not found: $Path"
    }

    $text = Get-Content -LiteralPath $Path -Raw
    $updated = & $Transform $text
    if ($updated -ne $text) {
        Set-Content -LiteralPath $Path -Value $updated -Encoding utf8
        Write-Host "Updated $Path" -ForegroundColor Green
    } else {
        Write-Host "No changes needed: $Path" -ForegroundColor DarkGray
    }
}

$RpiIP = Read-Value -Prompt "Raspberry Pi / head-node IP" -Default $RpiIP -Required
$StoragePC = Read-Value -Prompt "Windows storage PC hostname" -Default $StoragePC -Required
$StoragePCIP = Read-Value -Prompt "Windows storage PC IP" -Default $StoragePCIP -Required
$ShareName = Read-Value -Prompt "SMB share name for orchestrator folder" -Default $ShareName -Required
$RedisUser = Read-Value -Prompt "Redis username" -Default $RedisUser
$RedisPassword = Read-Value -Prompt "Redis password (blank if none)" -Default $RedisPassword
$CpuConcurrency = [int](Read-Value -Prompt "Celery CPU concurrency per worker" -Default ([string]$CpuConcurrency) -Required)
$GpuConcurrency = [int](Read-Value -Prompt "Celery GPU concurrency per worker" -Default ([string]$GpuConcurrency) -Required)
$SolveScript = Read-Value -Prompt "Solver script in orchestrator/tools" -Default $SolveScript -Required

$configYaml = Join-Path $RepoRoot "orchestrator\framework\config.yaml"
$connectDrive = Join-Path $RepoRoot "orchestrator\connect_drive.ps1"
$raySetupWorker = Join-Path $RepoRoot "worker\cluster\setup_worker.ps1"
$rayWorker = Join-Path $RepoRoot "worker\cluster\worker.py"

Set-Text -Path $configYaml -Transform {
    param($text)
    $text = $text -replace '(?m)^redis_host:\s*.*$', "redis_host: `"$RpiIP`""
    $text = $text -replace '(?m)^redis_user:\s*.*$', "redis_user: `"$RedisUser`""
    $text = $text -replace '(?m)^redis_password:\s*.*$', "redis_password: `"$RedisPassword`""
    $text = $text -replace '(?m)^solve_script:\s*.*$', "solve_script: `"$SolveScript`""
    $text = $text -replace '(?m)^cpu_concurrency:\s*.*$', "cpu_concurrency: $CpuConcurrency"
    $text = $text -replace '(?m)^gpu_concurrency:\s*.*$', "gpu_concurrency: $GpuConcurrency"
    return $text
}

Set-Text -Path $connectDrive -Transform {
    param($text)
    $text = $text -replace '(?m)^\$share_host\s*=.*$', "`$share_host = `"$StoragePC`""
    $text = $text -replace '(?m)^\$share_ip\s*=.*$', "`$share_ip   = `"$StoragePCIP`""
    $text = $text -replace '(?m)^\$share_name\s*=.*$', "`$share_name = `"$ShareName`""
    return $text
}

Set-Text -Path $raySetupWorker -Transform {
    param($text)
    $text = $text -replace '\[string\]\$HeadIP\s*=\s*"[^"]*"', "[string]`$HeadIP    = `"$RpiIP`""
    return $text
}

Set-Text -Path $rayWorker -Transform {
    param($text)
    $text = $text -replace 'HEAD_IP\s*=\s*"[^"]*"', "HEAD_IP            = `"$RpiIP`""
    $text = $text -replace 'STORAGE_UNC\s*=\s*r"[^"]*"', "STORAGE_UNC        = r`"\\$RpiIP\storage`""
    return $text
}

Write-Host ""
Write-Host "Configuration complete." -ForegroundColor Cyan
Write-Host "Next steps:"
Write-Host "  1. Run orchestrator\framework\setup.bat on the storage PC."
Write-Host "  2. Choose 'y' when asked to share the orchestrator folder."
Write-Host "  3. On worker PCs, run \\$StoragePC\$ShareName\connect_drive.ps1."
Write-Host "  4. Then run Z:\framework\setup_check.bat and Z:\framework\start_workers.bat."
