import django.db.models.deletion
from django.db import migrations, models


def backfill_invoice_branch(apps, schema_editor):
    alias = schema_editor.connection.alias
    Invoice = apps.get_model('operations', 'Invoice')
    Visit = apps.get_model('operations', 'Visit')
    branch = Visit.objects.using(alias).filter(pk=models.OuterRef('visit_id')).values('branch_id')[:1]
    Invoice.objects.using(alias).update(branch_id=models.Subquery(branch))


class Migration(migrations.Migration):
    dependencies = [('operations', '0011_branchcsvimport')]

    operations = [
        migrations.AddField(
            model_name='invoice', name='branch',
            field=models.ForeignKey(
                editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='invoices', to='operations.branch',
            ),
        ),
        migrations.RunPython(backfill_invoice_branch, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='invoice', name='branch',
            field=models.ForeignKey(
                editable=False, on_delete=django.db.models.deletion.PROTECT,
                related_name='invoices', to='operations.branch',
            ),
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.UniqueConstraint(
                fields=('branch', 'invoice_number'), name='unique_invoice_number_per_branch',
            ),
        ),
        migrations.AlterField(
            model_name='invoice', name='invoice_number', field=models.CharField(max_length=50),
        ),
    ]
