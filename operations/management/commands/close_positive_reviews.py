from django.core.management.base import BaseCommand

from operations.google_review_workflow import auto_close_positive


class Command(BaseCommand):
    help = 'Close positive Google reviews that have been open for 24 hours. Pages also do this when opened.'

    def handle(self, *args, **options):
        self.stdout.write(f'Closed {auto_close_positive()} positive review(s).')
