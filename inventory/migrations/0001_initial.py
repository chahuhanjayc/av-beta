"""
inventory/migrations/0001_initial.py

Initial migration for the inventory app.

Dependencies:
  - core.0003_company_bank_upi_fields  (Company model)
  - vouchers.0003_voucheritem_reference_voucher  (Voucher model)
"""

from decimal import Decimal
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("core",     "0003_company_bank_upi_fields"),
        ("vouchers", "0003_voucheritem_reference_voucher"),
    ]

    operations = [
        # ── HSN_SAC ───────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="HSN_SAC",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=20, unique=True)),
                ("description", models.CharField(blank=True, max_length=255)),
            ],
            options={
                "verbose_name": "HSN / SAC Code",
                "verbose_name_plural": "HSN / SAC Codes",
                "ordering": ["code"],
            },
        ),

        # ── TaxRate ───────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="TaxRate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("rate", models.DecimalField(decimal_places=2, max_digits=5)),
                ("description", models.CharField(blank=True, max_length=100)),
            ],
            options={
                "verbose_name": "Tax Rate",
                "verbose_name_plural": "Tax Rates",
                "ordering": ["rate"],
            },
        ),

        # ── StockItem ─────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="StockItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("company", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="stock_items",
                    to="core.company",
                )),
                ("unit", models.CharField(
                    choices=[
                        ("Nos",    "Nos"),
                        ("Kgs",    "Kgs"),
                        ("Boxes",  "Boxes"),
                        ("Dozen",  "Dozen"),
                        ("Meters", "Meters"),
                        ("Pieces", "Pieces"),
                    ],
                    default="Nos",
                    max_length=20,
                )),
                ("opening_quantity", models.DecimalField(
                    decimal_places=3, default=Decimal("0.000"), max_digits=15,
                    help_text="Initial stock quantity when item was set up.",
                )),
                ("purchase_price", models.DecimalField(
                    decimal_places=2, default=Decimal("0.00"), max_digits=15,
                    help_text="Default purchase price per unit (used as fallback for WAC).",
                )),
                ("selling_price", models.DecimalField(
                    decimal_places=2, default=Decimal("0.00"), max_digits=15,
                    help_text="Default selling price per unit (auto-fills voucher lines).",
                )),
                ("hsn_sac", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to="inventory.hsn_sac",
                    verbose_name="HSN / SAC Code",
                )),
                ("tax_rate", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to="inventory.taxrate",
                )),
                ("low_stock_threshold", models.DecimalField(
                    decimal_places=3, default=Decimal("0.000"), max_digits=15,
                    help_text="Show alert when closing stock falls below this level.",
                )),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Stock Item",
                "verbose_name_plural": "Stock Items",
                "ordering": ["name"],
                "unique_together": {("company", "name")},
            },
        ),

        # ── StockLedger ───────────────────────────────────────────────────────
        migrations.CreateModel(
            name="StockLedger",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stock_item", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="ledger_entries",
                    to="inventory.stockitem",
                )),
                ("voucher", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="stock_movements",
                    to="vouchers.voucher",
                )),
                ("date", models.DateField()),
                ("quantity", models.DecimalField(
                    decimal_places=3, max_digits=15,
                    help_text="Positive = inward (purchase), Negative = outward (sales).",
                )),
                ("rate", models.DecimalField(
                    decimal_places=2, max_digits=15,
                    help_text="Rate per unit at transaction time.",
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Stock Ledger Entry",
                "verbose_name_plural": "Stock Ledger Entries",
                "ordering": ["date", "created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="stockledger",
            index=models.Index(fields=["stock_item", "date"], name="inv_stockledger_item_date_idx"),
        ),
        migrations.AddIndex(
            model_name="stockledger",
            index=models.Index(fields=["voucher"], name="inv_stockledger_voucher_idx"),
        ),

        # ── VoucherStockItem ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="VoucherStockItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("voucher", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="voucher_stock_items",
                    to="vouchers.voucher",
                )),
                ("stock_item", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="voucher_lines",
                    to="inventory.stockitem",
                )),
                ("quantity", models.DecimalField(
                    decimal_places=3, max_digits=15,
                    help_text="Quantity of this stock item in the voucher.",
                )),
                ("rate", models.DecimalField(
                    decimal_places=2, max_digits=15,
                    help_text="Rate per unit at the time of transaction.",
                )),
            ],
            options={
                "verbose_name": "Voucher Stock Item",
                "verbose_name_plural": "Voucher Stock Items",
            },
        ),
    ]
