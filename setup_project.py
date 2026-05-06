"""
setup_project.py
One-time setup script for Akshaya Vistara (AV).
Prepares the environment, database, and initial data.
"""

import os
import sys
import subprocess

def run_command(command, description):
    """Utility to run shell commands and show status."""
    print(f"[*] {description}...")
    try:
        subprocess.check_call([sys.executable, 'manage.py'] + command)
        print(f"[OK] {description} successful.")
    except subprocess.CalledProcessError:
        print(f"[ERROR] {description} failed.")
        return False
    return True

def main():
    print("="*40)
    print("   Akshaya Vistara (AV) - Setup   ")
    print("="*40)

    # 1. ENV CHECK
    if not os.path.exists('.env'):
        print("[!] Missing .env file.")
        print("    Please create it from .env.example or set environment variables manually.")
        # Proceeding anyway as some might use system env vars, but warning the user.
    else:
        print("[OK] .env file found.")

    # Initialize Django to access ORM and Settings
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'akshaya_vistara.settings')
    try:
        import django
        django.setup()
    except Exception as e:
        print(f"[CRITICAL] Could not initialize Django: {e}")
        sys.exit(1)

    # 2. DATABASE CHECK
    from django.db import connections
    from django.db.utils import OperationalError
    db_conn = connections['default']
    try:
        db_conn.cursor()
        print("[OK] Database connection established.")
    except OperationalError:
        print("[ERROR] Database connection failed. Check your DB settings in .env")
        sys.exit(1)

    # 3. MIGRATIONS
    if not run_command(['makemigrations'], "Creating migrations"):
        sys.exit(1)
    if not run_command(['migrate'], "Applying migrations"):
        sys.exit(1)

    # 4. SUPERUSER CHECK
    from django.contrib.auth import get_user_model
    User = get_user_model()
    if not User.objects.filter(is_superuser=True).exists():
        choice = input("[?] No superuser found. Create one now? (y/n): ").lower()
        if choice == 'y':
            print("[*] Launching createsuperuser...")
            subprocess.call([sys.executable, 'manage.py', 'createsuperuser'])
    else:
        print("[OK] Superuser already exists.")

    # 5. LOAD DEFAULT DATA
    run_command(['setup_dev_data'], "Loading default/sample data")

    # 6. STATIC FILES
    from django.conf import settings
    if not settings.DEBUG:
        run_command(['collectstatic', '--noinput'], "Collecting static files")
    else:
        print("[*] DEBUG is True, skipping collectstatic.")

    print("\n" + "="*40)
    print("   Setup Complete.   ")
    print("="*40)
    print("You can now run:")
    print("python manage.py runserver")
    print("="*40)

if __name__ == "__main__":
    main()
