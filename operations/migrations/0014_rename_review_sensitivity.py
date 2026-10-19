from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('operations', '0013_reviewcase_reviewissue_reviewactivity_reviewreply_and_more')]
    operations = [migrations.RenameField(model_name='reviewcase', old_name='requires_senior_review', new_name='sensitive')]
