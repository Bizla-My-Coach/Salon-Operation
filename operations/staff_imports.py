"""Bounded, create-only staff CSV imports with an owner-bound confirmation stage."""
import csv
import hashlib
import io
import re
import secrets
import string
import unicodedata
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import Branch, StaffCSVImport

MAX_STAFF_UPLOAD = 512 * 1024
MAX_STAFF_ROWS = 200
HEADERS = {
    'BRANCH': 'branch', 'NAME': 'name', 'PHNO': 'mobile',
    'PHONE': 'mobile', 'MOBILE': 'mobile', 'PHONENUMBER': 'mobile',
    'SECTION': 'section', 'EXPERIENCE': 'experience', 'SALARY': 'salary',
}


class StaffImportProblem(Exception):
    pass


def generate_staff_password():
    """Generate eight unpredictable characters, including each character type."""
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits, '!#$%&*?')
    alphabet = ''.join(groups)
    while True:
        password = ''.join(secrets.choice(alphabet) for _ in range(8))
        if all(any(char in group for char in password) for group in groups):
            return password


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).split()).casefold()


def cell(value):
    value = str(value).strip()
    return '' if value.casefold() in ('null', 'none', 'nan') else value


def existing_people():
    return {
        (person.profile.branch_id, normalized(person.get_full_name()))
        for person in User.objects.filter(profile__branch__isnull=False).select_related('profile')
    }


def parse_staff_csv(payload):
    if not payload or len(payload) > MAX_STAFF_UPLOAD:
        raise StaffImportProblem('Choose a CSV no larger than 512 KB.')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise StaffImportProblem('Save the file as CSV UTF-8 in Excel and upload again.') from exc
    if '\x00' in text:
        raise StaffImportProblem('This is not a readable text CSV. Export CSV UTF-8, not an Excel workbook.')
    try:
        # Regional Excel exports may use a semicolon instead of a comma.
        first_line = text.splitlines()[0] if text.splitlines() else ''
        delimiter = ';' if first_line.count(';') > first_line.count(',') else ','
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
        header = next(reader, [])
        if not header or len(header) > 8:
            raise StaffImportProblem('Use headers BRANCH, NAME, PH.NO, SECTION, EXPERIENCE, SALARY.')
        keys = []
        for title in header:
            key = HEADERS.get(re.sub(r'[^A-Z0-9]', '', title.upper()))
            if not key or key in keys:
                raise StaffImportProblem('CSV has unsupported or duplicate columns. Use the downloadable template.')
            keys.append(key)
        if not {'branch', 'name', 'section'}.issubset(keys):
            raise StaffImportProblem('BRANCH, NAME and SECTION are required columns; PH.NO may be blank.')
        branches = defaultdict(set)
        branch_by_id = {}
        for branch in Branch.objects.filter(active=True):
            branches[normalized(branch.code)].add(branch.pk)
            branches[normalized(branch.name)].add(branch.pk)
            branch_by_id[branch.pk] = branch
        seen = existing_people()
        used_usernames = {normalized(value) for value in User.objects.values_list('username', flat=True)}
        result, errors = [], []
        for line, values in enumerate(reader, 2):
            if line > MAX_STAFF_ROWS + 1:
                raise StaffImportProblem('CSV exceeds 200 rows. Split it into smaller uploads.')
            if not any(cell(value) for value in values):
                continue
            if len(values) != len(keys):
                errors.append(f'Row {line}: column count does not match the header.')
                continue
            data = {key: cell(value) for key, value in zip(keys, values)}
            matches = branches.get(normalized(data['branch']), set())
            if len(matches) != 1:
                errors.append(f'Row {line}: branch is missing, unknown, inactive or ambiguous. Use an existing branch code.')
                continue
            branch = branch_by_id[next(iter(matches))]
            name, section = data['name'], data['section']
            if not name or len(name) > 150 or not section or len(section) > 80:
                errors.append(f'Row {line}: enter a name (up to 150 characters) and section (up to 80 characters).')
                continue
            if any(ord(char) < 32 for char in name + section):
                errors.append(f'Row {line}: name and section must not contain control characters.')
                continue
            experience = data.get('experience', '')
            if len(experience) > 80 or any(ord(char) < 32 for char in experience):
                errors.append(f'Row {line}: EXPERIENCE must be up to 80 characters without control characters.')
                continue
            salary = None
            if data.get('salary'):
                try:
                    amount = re.sub(r'^(?:₹|Rs\.?\s*)', '', data['salary'], flags=re.I).replace(',', '').strip()
                    value = Decimal(amount)
                    if not value.is_finite() or value < 0 or value > Decimal('9999999999.99') or value != value.quantize(Decimal('.01')):
                        raise InvalidOperation
                    salary = str(value.quantize(Decimal('.01')))
                except (InvalidOperation, ValueError):
                    errors.append(f'Row {line}: SALARY must be a nonnegative amount with at most two decimal places, or blank/null.')
                    continue
            identity = (branch.pk, normalized(name))
            if identity in seen:
                errors.append(f'Row {line}: this name already exists at the branch or is duplicated in this CSV. No account will be overwritten.')
                continue
            mobile = data.get('mobile', '')
            if re.fullmatch(r'\d{10}\.0', mobile):
                mobile = mobile[:-2]
            mobile = re.sub(r'[\s()-]', '', mobile)
            if mobile and not re.fullmatch(r'(?:\d{10}|\+91\d{10}|91\d{10})', mobile):
                errors.append(f'Row {line}: PH.NO must be a 10-digit phone number, optionally prefixed by +91, or blank/null. Export phone numbers as text.')
                continue
            base = (slugify(branch.code).replace('-', '.') + '.' + slugify(name).replace('-', '.')).strip('.')[:135] or 'staff'
            username, suffix = base, 2
            while normalized(username) in used_usernames:
                username = f'{base}.{suffix}'
                suffix += 1
            used_usernames.add(normalized(username))
            seen.add(identity)
            result.append({
                'line': line, 'branch_id': branch.pk, 'branch_name': branch.name, 'branch_code': branch.code,
                'name': name, 'mobile': mobile, 'section': section, 'username': username,
                'experience': experience, 'salary': salary,
                'role': 'MANAGER' if re.match(r'^MANAGER(?:$|[\s/\-])', section, re.I) else 'EMPLOYEE',
            })
        if errors:
            raise StaffImportProblem('\n'.join(errors[:20]) + ('\nMore errors exist; correct the CSV and validate again.' if len(errors) > 20 else ''))
        if not result:
            raise StaffImportProblem('CSV contains no staff rows.')
        return result
    except csv.Error as exc:
        raise StaffImportProblem('CSV quoting or column format is invalid. Export CSV UTF-8 from Excel.') from exc


def expire_staff_imports():
    StaffCSVImport.objects.filter(status='PENDING', expires_at__lte=timezone.now()).update(status='EXPIRED', rows=[])


def stage_staff_import(actor, filename, payload):
    digest = hashlib.sha256(payload).hexdigest()
    if StaffCSVImport.objects.filter(sha256=digest, status='APPLIED').exists():
        raise StaffImportProblem('This exact CSV was already imported. Existing accounts will not be imported again.')
    rows = parse_staff_csv(payload)
    # Retain at most one private pending preview for this operator.
    with transaction.atomic():
        StaffCSVImport.objects.filter(created_by=actor, status='PENDING').update(status='CANCELLED', rows=[])
        return StaffCSVImport.objects.create(
            created_by=actor, filename=filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1][:160],
            sha256=digest, rows=rows, row_count=len(rows),
            expires_at=timezone.now() + timedelta(minutes=30),
        )


def apply_staff_import(identifier, actor):
    try:
        with transaction.atomic():
            # Serialize confirmations so duplicate-name and username checks cannot race
            # against another bulk importer. Existing accounts are never updated.
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [73614209])
            stage = StaffCSVImport.objects.select_for_update().get(pk=identifier, created_by=actor)
            if stage.status != 'PENDING' or stage.expires_at <= timezone.now():
                raise StaffImportProblem('This preview expired or was already used. No accounts were created. If already imported, recover credentials with Reset password.')
            if StaffCSVImport.objects.filter(sha256=stage.sha256, status='APPLIED').exists():
                raise StaffImportProblem('This CSV was already imported. No additional accounts were created.')
            branches = {branch.pk: branch for branch in Branch.objects.filter(active=True, pk__in=[r['branch_id'] for r in stage.rows])}
            seen = existing_people()
            usernames = {normalized(value) for value in User.objects.values_list('username', flat=True)}
            for row in stage.rows:
                branch = branches.get(row['branch_id'])
                if not branch or branch.name != row['branch_name'] or branch.code != row['branch_code']:
                    raise StaffImportProblem('A branch changed after preview. Validate the CSV again; nothing was imported.')
                if (branch.pk, normalized(row['name'])) in seen or normalized(row['username']) in usernames:
                    raise StaffImportProblem('A name or username was added after preview. Validate the CSV again; nothing was imported.')
                if row['role'] not in ['MANAGER', 'EMPLOYEE']:
                    raise StaffImportProblem('Only manager and employee accounts can be imported.')
            credentials = []
            content_type = ContentType.objects.get_for_model(User)
            for row in stage.rows:
                password = generate_staff_password()
                person = User(username=row['username'], first_name=row['name'])
                validate_password(password, user=person)
                person.set_password(password)
                person.save()
                profile = person.profile
                profile.role, profile.branch_id = row['role'], row['branch_id']
                profile.mobile, profile.job_title = row['mobile'], row['section']
                profile.experience, profile.salary = row['experience'], row['salary']
                profile.employee_code = f'STF-{person.pk:05d}'
                profile.save()
                LogEntry.objects.create(user=actor, content_type=content_type,
                    object_id=str(person.pk), object_repr=person.username[:200], action_flag=ADDITION,
                    change_message='Account created via confirmed staff CSV import.')
                credentials.append([row['branch_name'], row['name'], person.username, password, row['role']])
            stage.status, stage.applied_at, stage.rows = 'APPLIED', timezone.now(), []
            stage.save(update_fields=['status', 'applied_at', 'rows'])
            return credentials
    except (IntegrityError, ValidationError) as exc:
        raise StaffImportProblem('Account creation could not complete. Nothing was imported; validate the CSV again.') from exc


def credentials_csv(rows):
    """Quote cells and neutralize formula-leading values for Excel CSV consumers."""
    output = io.StringIO(newline='')
    output.write('\ufeff')
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(['BRANCH', 'NAME', 'USERNAME', 'PASSWORD', 'ROLE'])
    for row in rows:
        safe = []
        for value in row:
            leading = unicodedata.normalize('NFKC', value).lstrip()
            dangerous = leading.startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n'))
            # Keep the tab inside a quoted cell. Unlike an apostrophe alone,
            # Excel preserves this guard when a CSV is saved and reopened.
            safe.append('\t' + value if dangerous else value)
        writer.writerow(safe)
    return output.getvalue()
