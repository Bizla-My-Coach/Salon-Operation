from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations', '0017_preserve_existing_review_followups')]
    operations = [
        migrations.AddField(model_name='reviewnotification', name='read_at',
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddIndex(model_name='reviewnotification', index=models.Index(
            fields=['recipient', 'read_at', '-created_at'], name='review_alert_rec_read_idx')),
    ]
