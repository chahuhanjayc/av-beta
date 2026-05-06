from django.db import migrations, models


def _financial_year_code(voucher_date):
    year = voucher_date.year
    if voucher_date.month >= 4:
        return f"{str(year)[2:]}{str(year + 1)[2:]}"
    return f"{str(year - 1)[2:]}{str(year)[2:]}"


def repair_duplicate_voucher_numbers(apps, schema_editor):
    Voucher = apps.get_model("vouchers", "Voucher")
    VoucherSequence = apps.get_model("vouchers", "VoucherSequence")

    seen_by_company = {}
    max_by_company_fy = {}

    vouchers = (
        Voucher.objects.select_related("company")
        .order_by("company_id", "date", "created_at", "id")
    )

    for voucher in vouchers:
        seen = seen_by_company.setdefault(voucher.company_id, set())
        number = voucher.number or ""
        fy_code = _financial_year_code(voucher.date)
        key = (voucher.company_id, fy_code)
        prefix = voucher.company.short_code or "VCH"

        if number and number not in seen:
            seen.add(number)
            suffix = number.rsplit("-", 1)[-1]
            if suffix.isdigit():
                max_by_company_fy[key] = max(max_by_company_fy.get(key, 0), int(suffix))
            continue

        next_number = max_by_company_fy.get(key, 0) + 1
        generated = f"{prefix}{fy_code}-{next_number:05d}"
        while generated in seen:
            next_number += 1
            generated = f"{prefix}{fy_code}-{next_number:05d}"

        Voucher.objects.filter(pk=voucher.pk).update(number=generated)
        voucher.number = generated
        seen.add(generated)
        max_by_company_fy[key] = next_number

    for (company_id, fy_code), last_number in max_by_company_fy.items():
        sequence, _ = VoucherSequence.objects.get_or_create(
            company_id=company_id,
            financial_year=fy_code,
            defaults={"last_number": last_number},
        )
        if sequence.last_number < last_number:
            sequence.last_number = last_number
            sequence.save(update_fields=["last_number"])


class Migration(migrations.Migration):

    dependencies = [
        ("vouchers", "0024_voucher_total_tax"),
    ]

    operations = [
        migrations.RunPython(repair_duplicate_voucher_numbers, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="voucher",
            constraint=models.UniqueConstraint(
                fields=("company", "number"),
                condition=~models.Q(number=""),
                name="uniq_voucher_number_per_company",
            ),
        ),
    ]
