from django.db import migrations

def migrate_ledger_groups(apps, schema_editor):
    Ledger = apps.get_model('ledger', 'Ledger')
    AccountGroup = apps.get_model('ledger', 'AccountGroup')
    Company = apps.get_model('core', 'Company')

    for company in Company.objects.all():
        groups_cache = {}
        for ledger in Ledger.objects.filter(company=company):
            if not ledger.group:
                continue
            
            group_name = ledger.group
            if group_name not in groups_cache:
                # Nature is same as group name in this stage
                group, created = AccountGroup.objects.get_or_create(
                    company=company,
                    name=group_name,
                    nature=group_name
                )
                groups_cache[group_name] = group
            
            ledger.account_group = groups_cache[group_name]
            Ledger.objects.filter(pk=ledger.pk).update(account_group=groups_cache[group_name])

class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0006_alter_ledger_group_alter_ledger_parent_accountgroup_and_more'),
    ]

    operations = [
        migrations.RunPython(migrate_ledger_groups),
    ]
