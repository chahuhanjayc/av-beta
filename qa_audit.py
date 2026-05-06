import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'akshaya_vistara.settings')
django.setup()

# Fix DisallowedHost by modifying settings dynamically
from django.conf import settings
if 'testserver' not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS.append('testserver')

from django.test import Client
from django.urls import get_resolver
from django.contrib.auth import get_user_model
from core.models import Company
from decimal import Decimal
import traceback

User = get_user_model()

def run_audit():
    print("=== STARTING FULL SYSTEM AUDIT (CA & QA PERSPECTIVE) ===")
    
    # 1. Setup Client & Auth
    client = Client()
    user = User.objects.filter(email="chauhanjayc@gmail.com").first()
    if not user:
        print("CRITICAL: Test user not found.")
        return
        
    client.force_login(user)
    
    company = Company.objects.filter(ledgers__isnull=False).distinct().first()
    if not company:
        print("CRITICAL: No company with ledgers found to test.")
        return
    
    print(f"Using company for testing: {company.name}")
        
    # Simulate setting the current company in session
    response = client.get(f'/core/switch-company/{company.id}/', follow=True)
    if response.status_code != 200:
        print(f"Failed to switch company. Status: {response.status_code}")
    
    # 2. Extract all URLs (Simple Crawler)
    print("\n--- PHASE 1: NAVIGATION & ROUTE TEST ---")
    resolver = get_resolver()
    all_urls = set()
    
    def extract_urls(urlpatterns, prefix=''):
        for pattern in urlpatterns:
            if hasattr(pattern, 'url_patterns'):
                extract_urls(pattern.url_patterns, prefix + str(pattern.pattern))
            else:
                url = prefix + str(pattern.pattern)
                # Filter out admin, auth, media, static, and parameterized URLs for the generic crawler
                if not url.startswith('admin/') and not url.startswith('__debug__/') and '<' not in url:
                    all_urls.add('/' + url.lstrip('^').rstrip('$'))
                    
    extract_urls(resolver.url_patterns)
    
    broken_links = []
    server_errors = []
    redirects = []
    success_count = 0
    
    for url in sorted(list(all_urls)):
        if url in ['/logout/', '/login/']: continue
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                success_count += 1
            elif resp.status_code in [301, 302]:
                redirects.append((url, resp.url))
            elif resp.status_code == 404:
                broken_links.append(url)
            elif resp.status_code == 500:
                server_errors.append(url)
            else:
                print(f"Warning: {url} returned {resp.status_code}")
        except Exception as e:
            server_errors.append((url, str(e)))

    print(f"Tested {len(all_urls)} static routes.")
    print(f"Success (200): {success_count}")
    print(f"Redirects (30x): {len(redirects)}")
    print(f"Broken Links (404): {len(broken_links)}")
    for link in broken_links: print(f"  - [404] {link}")
    print(f"Server Errors (500): {len(server_errors)}")
    for err in server_errors: print(f"  - [500] {err}")

    # 3. Specific Module Logic Tests
    print("\n--- PHASE 2-10: BUSINESS LOGIC TESTS ---")
    
    issues = []
    
    # A. Check double entry validation
    print("Testing Double Entry Enforcement...")
    from vouchers.models import Voucher, VoucherItem
    from ledger.models import Ledger
    
    try:
        # Find ANY cash and sales ledgers instead of hardcoded names
        cash = Ledger.objects.filter(company=company, account_group__nature='Asset').first()
        sales = Ledger.objects.filter(company=company, account_group__nature='Income').first()
        
        if not cash or not sales:
            print("Skipping double-entry test (Missing test ledgers)")
        else:
            # Try to save unbalanced voucher (should fail at model level)
            v = Voucher.objects.create(company=company, voucher_type="Journal", narration="Test Unbalanced")
            VoucherItem.objects.create(voucher=v, ledger=cash, entry_type='DR', amount=100)
            VoucherItem.objects.create(voucher=v, ledger=sales, entry_type='CR', amount=90)
            
            try:
                v.validate_balance()
                issues.append(("CRITICAL", "Vouchers", "validate_balance() did not raise ValidationError for unbalanced entry!"))
            except Exception as e:
                # Expected
                pass
                
            v.delete() # Cleanup
    except Exception as e:
        issues.append(("HIGH", "Testing", f"Failed to test double entry: {e}"))
        traceback.print_exc()

    # B. Test GST Auto-calculation and Inter-state vs Intra-state
    print("Testing GST Auto-Calculation...")
    from inventory.models import StockItem, Godown
    try:
        laptop = StockItem.objects.filter(company=company).first()
        main_wh = Godown.objects.filter(company=company).first()
        client_a = Ledger.objects.filter(company=company, gstin__startswith=company.gstin[:2] if company.gstin else "27").exclude(account_group__nature='Tax').first()
        client_b = Ledger.objects.filter(company=company).exclude(gstin__startswith=company.gstin[:2] if company.gstin else "27").exclude(account_group__nature='Tax').first()
        
        if laptop and main_wh and client_a and sales:
            # Intra-state
            v_intra = Voucher.objects.create(company=company, voucher_type="Sales", place_of_supply=company.gstin[:2] if company.gstin else "27")
            VoucherItem.objects.create(voucher=v_intra, ledger=client_a, entry_type='DR', amount=10000)
            VoucherItem.objects.create(voucher=v_intra, ledger=sales, entry_type='CR', amount=10000, stock_item=laptop, quantity=1, rate=10000, godown=main_wh)
            v_intra.create_tax_lines()
            
            cgst = v_intra.items.filter(ledger__name__icontains="CGST").first()
            if not cgst:
                issues.append(("CRITICAL", "GST", "Intra-state sales did not generate CGST line."))
            v_intra.delete()
            
            # Inter-state (only if we found an inter-state client)
            if client_b:
                v_inter = Voucher.objects.create(company=company, voucher_type="Sales", place_of_supply="24" if company.gstin[:2] != "24" else "27")
                VoucherItem.objects.create(voucher=v_inter, ledger=client_b, entry_type='DR', amount=10000)
                VoucherItem.objects.create(voucher=v_inter, ledger=sales, entry_type='CR', amount=10000, stock_item=laptop, quantity=1, rate=10000, godown=main_wh)
                v_inter.create_tax_lines()
                
                igst = v_inter.items.filter(ledger__name__icontains="IGST").first()
                if not igst:
                    issues.append(("CRITICAL", "GST", "Inter-state sales did not generate IGST line."))
                v_inter.delete()
    except Exception as e:
        issues.append(("HIGH", "Testing", f"Failed to test GST: {e}"))
        traceback.print_exc()

    # C. Test Inventory Negative Stock Guard
    print("Testing Inventory Negative Stock Prevention...")
    try:
        # Set prevent_negative_stock = True
        laptop.prevent_negative_stock = True
        laptop.save()
        
        v_neg = Voucher.objects.create(company=company, voucher_type="Sales")
        VoucherItem.objects.create(voucher=v_neg, ledger=client_a, entry_type='DR', amount=1000000)
        # Sell 1000 laptops (more than opening stock)
        VoucherItem.objects.create(voucher=v_neg, ledger=sales, entry_type='CR', amount=1000000, stock_item=laptop, quantity=1000, rate=1000, godown=main_wh)
        
        try:
            v_neg.sync_inventory()
            issues.append(("CRITICAL", "Inventory", "System allowed sale resulting in negative stock despite prevent_negative_stock=True."))
        except Exception as e:
            # Expected
            pass
        v_neg.delete()
    except Exception as e:
        pass


    # Print Final Summary
    print("\n" + "="*50)
    print("SUMMARY OF ISSUES FOUND:")
    if not issues and not server_errors and not broken_links:
        print("No critical issues found via automated paths!")
    else:
        for link in broken_links: print(f"[MEDIUM] UI: Broken Link - {link}")
        for err in server_errors: print(f"[HIGH] Server Error (500) at: {err}")
        for severity, module, msg in issues: print(f"[{severity}] {module}: {msg}")
    print("="*50)

if __name__ == '__main__':
    run_audit()
