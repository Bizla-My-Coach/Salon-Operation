from django.db import migrations, models


def preserve_existing_followups(apps, schema_editor):
    Issue = apps.get_model('operations', 'ReviewIssue')
    Issue.objects.filter(customer_confirmation='CONFIRMED').update(customer_followup='CONTACTED')
    Issue.objects.filter(status='WAITING', waiting_reason='CUSTOMER').exclude(
        customer_confirmation='CONFIRMED').update(customer_followup='AWAITING_REPLY')
    Issue.objects.exclude(action_summary='').update(
        followup_submitted_at=models.F('updated_at'),
        followup_submitted_by=models.F('completed_by'))


class Migration(migrations.Migration):
    # Keep updates after the FK/index schema transaction has committed on PostgreSQL.
    dependencies = [('operations', '0016_reviewissue_customer_followup_and_more')]
    operations = [migrations.RunPython(preserve_existing_followups, migrations.RunPython.noop)]
