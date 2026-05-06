# =============================================================================
# setup.ps1  —  Akshaya Vistara one-click setup for Windows 11 (PowerShell 5.1+)
# Run this from inside the akshaya_vistara folder:
#   cd C:\path\to\akshaya_vistara
#   .\setup.ps1
# =============================================================================

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   Akshaya Vistara Setup Script" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: Check Python is installed
# ---------------------------------------------------------------------------
Write-Host "[1/8] Checking Python installation..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version 2>&1
    Write-Host "      Found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "ERROR: Python is not installed or not in PATH." -ForegroundColor Red
    Write-Host "       Download from: https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Step 2: Create virtual environment
# ---------------------------------------------------------------------------
Write-Host "[2/8] Creating virtual environment (.venv)..." -ForegroundColor Yellow
if (Test-Path ".venv") {
    Write-Host "      .venv already exists, skipping creation." -ForegroundColor DarkGray
} else {
    python -m venv .venv
    Write-Host "      Virtual environment created." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Step 3: Activate virtual environment
# ---------------------------------------------------------------------------
Write-Host "[3/8] Activating virtual environment..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1
Write-Host "      Activated." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 4: Upgrade pip and install dependencies
# ---------------------------------------------------------------------------
Write-Host "[4/8] Installing dependencies from requirements.txt..." -ForegroundColor Yellow
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
Write-Host "      Dependencies installed." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 5: Create .env file from .env.example (if not already present)
# ---------------------------------------------------------------------------
Write-Host "[5/8] Setting up .env file..." -ForegroundColor Yellow
if (Test-Path ".env") {
    Write-Host "      .env already exists, skipping." -ForegroundColor DarkGray
} else {
    Copy-Item ".env.example" ".env"
    # Generate a random SECRET_KEY
    $secretKey = -join ((65..90) + (97..122) + (48..57) + (33,35,36,37,38,42,43,45,60,62,63,64,94,95,126) |
                        Get-Random -Count 50 | ForEach-Object {[char]$_})
    (Get-Content ".env") -replace "your-secret-key-here-change-this-in-production", $secretKey |
        Set-Content ".env"
    Write-Host "      .env created with a random SECRET_KEY." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Step 6: Run migrations
# ---------------------------------------------------------------------------
Write-Host "[6/8] Running database migrations..." -ForegroundColor Yellow
python manage.py makemigrations accounts
python manage.py makemigrations core
python manage.py makemigrations ledger
python manage.py makemigrations vouchers
python manage.py migrate
Write-Host "      Migrations complete." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 7: Collect static files
# ---------------------------------------------------------------------------
Write-Host "[7/8] Collecting static files..." -ForegroundColor Yellow
python manage.py collectstatic --noinput --quiet
Write-Host "      Static files collected." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Step 8: Create superuser
# ---------------------------------------------------------------------------
Write-Host "[8/8] Creating superuser account..." -ForegroundColor Yellow
Write-Host ""
Write-Host "      You will be prompted to enter an email and password." -ForegroundColor Cyan
Write-Host "      This is your admin account — remember these credentials!" -ForegroundColor Cyan
Write-Host ""
python manage.py createsuperuser

# ---------------------------------------------------------------------------
# Done!
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "   Setup complete!" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "To start the development server, run:" -ForegroundColor Cyan
Write-Host "   .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "   python manage.py runserver" -ForegroundColor White
Write-Host ""
Write-Host "Then open: http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host ""

# Ask user if they want to start the server now
$startNow = Read-Host "Start the server now? (y/n)"
if ($startNow -eq "y" -or $startNow -eq "Y") {
    Write-Host ""
    Write-Host "Starting server... Press Ctrl+C to stop." -ForegroundColor Cyan
    Write-Host "Open http://127.0.0.1:8000 in your browser." -ForegroundColor Green
    Write-Host ""
    python manage.py runserver
}
