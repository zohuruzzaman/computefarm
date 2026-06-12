# connect_drive.ps1 - map the ComputeFarm worker share as Z: on a new worker PC.
# Run once; /persistent:yes survives reboots.

$share_host = "<STORAGE_PC>"
$share_ip   = "<STORAGE_PC_IP>"
$share_name = "ComputeFarm"
$drive      = "Z:"

$existing = Get-PSDrive -Name Z -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Z: is already mapped to $($existing.DisplayRoot)" -ForegroundColor Yellow
    Write-Host "Run 'net use Z: /delete' first if you want to remap."
    exit 0
}

foreach ($target in @($share_host, $share_ip)) {
    Write-Host "Trying \\$target\$share_name ..."
    net use $drive "\\$target\$share_name" /persistent:yes 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Mapped $drive -> \\$target\$share_name" -ForegroundColor Green
        Write-Host ""
        Write-Host "Next steps:"
        Write-Host "  cd Z:\framework"
        Write-Host "  .\setup_check.bat"
        Write-Host "  .\start_worker_cpu_only.bat"
        exit 0
    }
}

Write-Host "ERROR: could not map share. Check that <STORAGE_PC> is online and the share exists." -ForegroundColor Red
Write-Host "  Verify with: net view \\$share_ip"
exit 1
