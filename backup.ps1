# =============================================================================
# backup.ps1  —  Akshaya Vistara PostgreSQL Backup Script (Windows 11 / PowerShell)
#
# Features:
#   - Creates a timestamped .sql dump from the running Docker postgres container
#   - Keeps only the last N backups (default: 7)
#   - Provides colourised status output
#
# Usage (from the akshaya_vistara folder):
#   .\backup.ps1
#   .\backup.ps1 -Keep 14        # keep 14 most recent backups
#   .\backup.ps1 -Restore        # restore from the latest backup
# =============================================================================

param(
    [int]$Keep    = 7,
    [switch]$Restore
)

# ── Config ────────────────────────────────────────────────────────────────────
$BackupDir  = ".\backups"
$Container  = "akshaya_vistara_db"
$DbName     = "akshaya_vistara_db"
$DbUser     = "akshaya_vistara_user"
$Timestamp  = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$BackupFile = "$BackupDir\backup_$Timestamp.sql"

# ── Ensure backup directory exists ──────────────────────────────────────────
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir | Out-Null
}

# ── Restore mode ─────────────────────────────────────────────────────────────
if ($Restore) {
    $LatestBackup = Get-ChildItem "$BackupDir\backup_*.sql" |
                    Sort-Object LastWriteTime -Descending |
                    Select-Object -First 1

    if (-not $LatestBackup) {
        Write-Host "ERROR: No backup files found in $BackupDir" -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host "⚠  RESTORE from: $($LatestBackup.Name)" -ForegroundColor Yellow
    Write-Host "   This will OVERWRITE all data in '$DbName'!" -ForegroundColor Yellow
    $confirm = Read-Host "   Type 'yes' to confirm"
    if ($confirm -ne "yes") {
        Write-Host "Restore cancelled." -ForegroundColor Gray
        exit 0
    }

    Write-Host "Restoring database..." -ForegroundColor Cyan
    Get-Content $LatestBackup.FullName |
        docker exec -i $Container psql -U $DbUser -d $DbName
    Write-Host "Restore complete from: $($LatestBackup.Name)" -ForegroundColor Green
    exit 0
}

# ── Backup mode ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Akshaya Vistara Database Backup" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Check Docker is running
try {
    docker ps --filter "name=$Container" --format "{{.Names}}" 2>&1 | Out-Null
} catch {
    Write-Host "ERROR: Docker is not running or the container '$Container' is not found." -ForegroundColor Red
    Write-Host "       Make sure Docker Desktop is running and the stack is up:" -ForegroundColor Red
    Write-Host "       docker-compose up -d" -ForegroundColor White
    exit 1
}

$ContainerStatus = docker ps --filter "name=$Container" --format "{{.Status}}"
if (-not $ContainerStatus) {
    Write-Host "ERROR: Container '$Container' is not running." -ForegroundColor Red
    Write-Host "       Start it with: docker-compose up -d db" -ForegroundColor White
    exit 1
}

Write-Host "[1/3] Creating PostgreSQL dump..." -ForegroundColor Yellow
docker exec $Container pg_dump -U $DbUser -F p -f /tmp/akshaya_vistara_backup.sql $DbName
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pg_dump failed (exit code $LASTEXITCODE)" -ForegroundColor Red
    exit 1
}

Write-Host "[2/3] Copying backup to host..." -ForegroundColor Yellow
docker cp "${Container}:/tmp/akshaya_vistara_backup.sql" $BackupFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: docker cp failed" -ForegroundColor Red
    exit 1
}

# Get file size
$Size = [math]::Round((Get-Item $BackupFile).Length / 1KB, 1)
Write-Host "      Saved: $BackupFile ($Size KB)" -ForegroundColor Green

Write-Host "[3/3] Pruning old backups (keeping last $Keep)..." -ForegroundColor Yellow
$OldBackups = Get-ChildItem "$BackupDir\backup_*.sql" |
              Sort-Object LastWriteTime -Descending |
              Select-Object -Skip $Keep

if ($OldBackups.Count -gt 0) {
    foreach ($old in $OldBackups) {
        Remove-Item $old.FullName -Force
        Write-Host "      Deleted: $($old.Name)" -ForegroundColor DarkGray
    }
} else {
    Write-Host "      No old backups to remove." -ForegroundColor DarkGray
}

# Summary
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "   Backup complete!" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Backup file : $BackupFile" -ForegroundColor Cyan
Write-Host "Size        : $Size KB" -ForegroundColor Cyan

$TotalBackups = (Get-ChildItem "$BackupDir\backup_*.sql").Count
Write-Host "Total kept  : $TotalBackups of $Keep" -ForegroundColor Cyan
Write-Host ""
Write-Host "To restore from this backup, run:" -ForegroundColor White
Write-Host "   .\backup.ps1 -Restore" -ForegroundColor Gray
Write-Host ""
