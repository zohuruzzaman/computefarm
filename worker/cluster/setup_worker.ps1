# setup_worker.ps1
# Run once as Administrator on each Windows worker node.
# Installs Python/Ray, deploys worker.py, registers auto-start scheduled task.

param(
    [string]$HeadIP    = "<RPI_IP>",   # <<< update to your RPi IP
    [int]   $NumCPUs   = 12,               # cores to expose to Ray (leave some for user)
    [int]   $NumGPUs   = 1,               # GPUs per machine
    [string]$WorkerDir = "C:\ray_worker",  # local directory on each worker
    [string]$SourceDir = "",               # optional: path with worker.py + jobs\
                                           #   if empty, copies from script's own directory
    [string]$TaskUser  = $env:USERNAME     # Windows user account to run the worker task
                                           # must be the logged-in user (needs share access)
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$StorageUNC = "\\$HeadIP\storage"

Write-Host ""
Write-Host "=== Ray Worker Setup ===" -ForegroundColor Cyan
Write-Host "Head node  : $HeadIP"
Write-Host "Resources  : $NumCPUs CPUs, $NumGPUs GPU(s)"
Write-Host "Worker dir : $WorkerDir"
Write-Host "Storage    : $StorageUNC"
Write-Host "Task user  : $TaskUser"
Write-Host ""

# ── Step 1: Python check ───────────────────────────────────────────────────────

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python not found. Installing Python 3.11 via winget..." -ForegroundColor Yellow
    winget install Python.Python.3.11 --silent --accept-package-agreements
    $env:PATH = "C:\Python311;C:\Python311\Scripts;" + $env:PATH
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Error "Python install failed. Install manually from python.org and re-run."
    }
}

$pyVer = python --version 2>&1
Write-Host "Python     : $pyVer" -ForegroundColor Green
$pythonExe = (Get-Command python).Source

# ── Step 2: Install Python packages ───────────────────────────────────────────

Write-Host "Installing Ray..." -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
python -m pip install "ray[default]" psutil --quiet

Write-Host "Installing PyTorch (CUDA 12.1)..." -ForegroundColor Cyan
python -m pip install torch torchvision `
    --index-url https://download.pytorch.org/whl/cu121 --quiet

Write-Host "Packages installed." -ForegroundColor Green

# ── Step 3: Prometheus exporters (metrics for Grafana) ────────────────────────

$ExporterDir = "C:\exporters"
New-Item -ItemType Directory -Force -Path $ExporterDir | Out-Null

# windows_exporter — CPU, memory, disk, network, temperatures
$WinExpVer = "0.25.1"
$WinExpMsi = "$ExporterDir\windows_exporter.msi"

Write-Host "Installing windows_exporter $WinExpVer..." -ForegroundColor Cyan
try {
    Invoke-WebRequest `
        -Uri "https://github.com/prometheus-community/windows_exporter/releases/download/v$WinExpVer/windows_exporter-$WinExpVer-amd64.msi" `
        -OutFile $WinExpMsi -UseBasicParsing

    # MSI installs and registers windows_exporter as a Windows service automatically
    # Enable collectors: cpu, memory, logical_disk, net, os, thermalzone, process
    $collectors = "cpu,memory,logical_disk,net,os,thermalzone,process"
    Start-Process msiexec.exe -ArgumentList `
        "/i `"$WinExpMsi`" /quiet ENABLED_COLLECTORS=$collectors" `
        -Wait -NoNewWindow

    Write-Host "windows_exporter installed (port 9182)." -ForegroundColor Green
} catch {
    Write-Warning "windows_exporter install failed: $_"
}

# nvidia_gpu_exporter — GPU%, VRAM, temperature, power, fan
$NvExpVer = "1.2.1"
$NvExpZip = "$ExporterDir\nvidia_gpu_exporter.zip"
$NvExpExe = "$ExporterDir\nvidia_gpu_exporter.exe"

Write-Host "Installing nvidia_gpu_exporter $NvExpVer..." -ForegroundColor Cyan
try {
    Invoke-WebRequest `
        -Uri "https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v$NvExpVer/nvidia_gpu_exporter_${NvExpVer}_windows_x86_64.zip" `
        -OutFile $NvExpZip -UseBasicParsing

    Expand-Archive -Path $NvExpZip -DestinationPath $ExporterDir -Force
    Remove-Item $NvExpZip

    # Register as Windows service using sc.exe
    $svcName = "nvidia_gpu_exporter"
    if (Get-Service -Name $svcName -ErrorAction SilentlyContinue) {
        Stop-Service $svcName -Force -ErrorAction SilentlyContinue
        sc.exe delete $svcName | Out-Null
    }
    sc.exe create $svcName `
        binPath= "`"$NvExpExe`" --web.listen-address=:9835" `
        start= auto DisplayName= "NVIDIA GPU Prometheus Exporter" | Out-Null
    Start-Service $svcName

    Write-Host "nvidia_gpu_exporter installed (port 9835)." -ForegroundColor Green
} catch {
    Write-Warning "nvidia_gpu_exporter install failed: $_"
}

# Open firewall for Prometheus scraping (RPi pulls from these ports)
Write-Host "Opening firewall ports 9182 and 9835..." -ForegroundColor Cyan
netsh advfirewall firewall add rule `
    name="windows_exporter" dir=in action=allow protocol=TCP localport=9182 | Out-Null
netsh advfirewall firewall add rule `
    name="nvidia_gpu_exporter" dir=in action=allow protocol=TCP localport=9835 | Out-Null
Write-Host "Firewall rules added." -ForegroundColor Green

# ── Step 4: Deploy worker files ───────────────────────────────────────────────

New-Item -ItemType Directory -Force -Path $WorkerDir          | Out-Null
New-Item -ItemType Directory -Force -Path "$WorkerDir\jobs"   | Out-Null

$source = if ($SourceDir -ne "") { $SourceDir } else { $ScriptDir }

# Copy worker.py
$workerSrc = Join-Path $source "worker.py"
if (Test-Path $workerSrc) {
    Copy-Item $workerSrc "$WorkerDir\worker.py" -Force
    Write-Host "Copied worker.py from $source" -ForegroundColor Green
} else {
    Write-Error "worker.py not found at $workerSrc. Place it next to this script."
}

# Copy jobs\ (optional — may be empty at setup time)
$jobsSrc = Join-Path $source "jobs"
if (Test-Path $jobsSrc) {
    Copy-Item "$jobsSrc\*" "$WorkerDir\jobs\" -Recurse -Force
    Write-Host "Copied jobs/ handlers" -ForegroundColor Green
} else {
    Write-Host "jobs/ not found at source — directory created, drop handlers in later" -ForegroundColor Yellow
}

# Patch HEAD_IP and STORAGE_UNC in worker.py
(Get-Content "$WorkerDir\worker.py") `
    -replace 'HEAD_IP\s*=\s*"[^"]*"',      "HEAD_IP = `"$HeadIP`"" `
    -replace 'STORAGE_UNC\s*=\s*r"[^"]*"', "STORAGE_UNC = r`"$StorageUNC`"" |
    Set-Content "$WorkerDir\worker.py" -Encoding utf8

Write-Host "Patched HEAD_IP=$HeadIP and STORAGE_UNC=$StorageUNC in worker.py" -ForegroundColor Green

# ── Step 5: Map storage share (user convenience + connectivity check) ─────────

Write-Host "Mapping $StorageUNC as a persistent network drive..." -ForegroundColor Cyan
$existingDrive = Get-PSDrive -Name "Z" -ErrorAction SilentlyContinue
if ($existingDrive) {
    net use Z: /delete /yes 2>$null | Out-Null
}
net use Z: $StorageUNC /persistent:yes 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Mapped Z: → $StorageUNC" -ForegroundColor Green
} else {
    Write-Warning "Could not map Z: drive. Workers will use UNC paths directly."
}

# ── Step 6: Register scheduled task ───────────────────────────────────────────

$taskName = "RayWorker"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask  -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed existing task." -ForegroundColor Yellow
}

$action = New-ScheduledTaskAction `
    -Execute          $pythonExe `
    -Argument         "`"$WorkerDir\worker.py`"" `
    -WorkingDirectory $WorkerDir

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -Priority                  7 `
    -RunOnlyIfNetworkAvailable $true `
    -ExecutionTimeLimit        (New-TimeSpan -Days 365) `
    -RestartCount              3 `
    -RestartInterval           (New-TimeSpan -Minutes 2)

# Run as the current user (not SYSTEM) so the task can access network shares.
# Prompts once for password — stored securely by Task Scheduler.
$password = Read-Host -Prompt "Enter Windows password for '$TaskUser' (needed for scheduled task)" -AsSecureString
$plainPwd = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password))

Register-ScheduledTask `
    -TaskName  $taskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -User      $TaskUser `
    -Password  $plainPwd `
    -RunLevel  Highest `
    -Force | Out-Null

$plainPwd = $null  # clear from memory

Write-Host "Scheduled task '$taskName' registered (auto-starts on boot)." -ForegroundColor Green

# ── Step 7: Start worker now ──────────────────────────────────────────────────

Start-ScheduledTask -TaskName $taskName
Write-Host "Worker started." -ForegroundColor Green

# ── Step 8: Summary ───────────────────────────────────────────────────────────

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Worker ID       : $($env:COMPUTERNAME)"
Write-Host "  Head node       : $HeadIP"
Write-Host "  Storage         : $StorageUNC  (also mapped as Z:\)"
Write-Host "  Worker dir      : $WorkerDir"
Write-Host "  Job scripts     : $WorkerDir\jobs\"
Write-Host "  Metrics ports   : 9182 (windows_exporter)  9835 (nvidia_gpu_exporter)"
Write-Host ""
Write-Host "  Storage layout (via Z:\ or UNC):"
Write-Host "    Z:\pending\   ← drop input files here"
Write-Host "    Z:\active\    ← in-progress (auto-managed)"
Write-Host "    Z:\results\   ← solved output files appear here"
Write-Host "    Z:\failed\    ← jobs that failed 3 retries"
Write-Host ""
Write-Host "  To add a job handler:"
Write-Host "    Copy your script to $WorkerDir\jobs\<type>.py"
Write-Host "    It needs:  def run(job: dict) -> dict"
Write-Host "    Worker reloads handlers every 60s — no restart needed."
Write-Host ""
Write-Host "  To check worker status:"
Write-Host "    Get-ScheduledTask -TaskName RayWorker"
Write-Host "    Get-ScheduledTaskInfo -TaskName RayWorker"
Write-Host ""
