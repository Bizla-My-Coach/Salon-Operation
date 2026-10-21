import json
import os
import secrets
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Write a new receive-only AWS API token to a private JSON file; never print the token.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        path = Path(options['output']).expanduser().resolve()
        parent = path.parent
        while parent != parent.parent:
            if (parent / '.git').exists():
                raise CommandError('Write the secret outside a Git checkout.')
            parent = parent.parent
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
                json.dump({'headers': {'X-Integration-Key': secrets.token_urlsafe(48)}}, output)
                output.write('\n')
        except OSError as exc:
            raise CommandError('Could not create a new private secret file; check the directory and choose a new filename.') from exc
        self.stdout.write(f'Private secret written to {path}. Install the same token in the server environment and AWS Secrets Manager.')
