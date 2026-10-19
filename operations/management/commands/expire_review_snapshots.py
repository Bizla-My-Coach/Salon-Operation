from django.core.management.base import BaseCommand
from django.utils import timezone

from operations.models import ReviewSnapshot


class Command(BaseCommand):
    help = 'Remove expired cached review text and ratings; internal ticket state is unchanged.'

    def handle(self, *args, **options):
        count = ReviewSnapshot.objects.filter(expires_at__lte=timezone.now()).exclude(text='', rating__isnull=True).update(text='', rating=None)
        self.stdout.write(f'Cleared {count} expired review snapshots.')
