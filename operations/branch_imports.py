"""Atomic branch and chair CSV updates identified by stable codes, not names."""
import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import timedelta

from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import Branch, BranchCSVImport, Chair
from .staff_imports import cell, normalized

MAX_BRANCH_UPLOAD = 512 * 1024
MAX_BRANCH_ROWS = 500
HEADERS = {
    'BRANCHCODE': 'branch_code', 'BRANCHNAME': 'branch_name', 'BRANCH': 'branch_name',
    'NAME': 'branch_name', 'LOCATION': 'address', 'BRANCHLOCATION': 'address', 'ADDRESS': 'address',
    'PHONE': 'phone', 'PHNO': 'phone', 'CHAIRCODE': 'chair_code', 'CHAIRNAME': 'chair_name',
    'STORECODE': 'store_code',
}


class BranchImportProblem(Exception):
    pass


def branch_master_digest():
    state = {
        'branches': list(Branch.objects.order_by('pk').values('id', 'code', 'store_code', 'name', 'address', 'phone', 'active', 'updated_at')),
        'chairs': list(Chair.objects.order_by('pk').values('id', 'branch_id', 'code', 'name', 'active', 'updated_at')),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def parse_branch_csv(payload):
    if not payload or len(payload) > MAX_BRANCH_UPLOAD:
        raise BranchImportProblem('Choose a CSV no larger than 512 KB.')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise BranchImportProblem('Export CSV UTF-8 from Excel and upload again.') from exc
    if '\x00' in text:
        raise BranchImportProblem('Upload a text CSV, not an Excel workbook.')
    existing = defaultdict(list)
    names = defaultdict(set)
    chairs = defaultdict(list)
    store_codes = {}
    for branch in Branch.objects.all():
        existing[normalized(branch.code)].append(branch)
        names[normalized(branch.name)].add(normalized(branch.code))
        if branch.store_code:
            store_codes[branch.store_code] = normalized(branch.code)
    for chair in Chair.objects.select_related('branch'):
        chairs[(normalized(chair.branch.code), normalized(chair.code))].append(chair)
    planned_branches, planned_chairs, errors = {}, {}, []
    try:
        first = text.splitlines()[0] if text.splitlines() else ''
        delimiter = ';' if first.count(';') > first.count(',') else ','
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
        header = next(reader, [])
        keys = [HEADERS.get(re.sub(r'[^A-Z0-9]', '', h.upper())) for h in header]
        if not keys or len(keys) > 7 or None in keys or len(set(keys)) != len(keys):
            raise BranchImportProblem('Use the downloadable template. CSV has unsupported or duplicate headers.')
        if not {'branch_code', 'branch_name'}.issubset(keys):
            raise BranchImportProblem('BRANCH_CODE and BRANCH_NAME columns are required.')
        for line, values in enumerate(reader, 2):
            if line > MAX_BRANCH_ROWS + 1:
                raise BranchImportProblem('CSV exceeds 500 rows. Split the upload into smaller files.')
            if not any(cell(v) for v in values):
                continue
            if len(values) != len(keys):
                errors.append(f'Row {line}: column count does not match the header.')
                continue
            data = {key: cell(v) for key, v in zip(keys, values)}
            code, name = data['branch_code'], data['branch_name']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,19}', code):
                errors.append(f'Row {line}: BRANCH_CODE must be 1–20 letters, numbers, underscores or hyphens.')
                continue
            if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
                errors.append(f'Row {line}: enter a branch name up to 120 characters without control characters.')
                continue
            address, phone = data.get('address', ''), data.get('phone', '')
            if len(address) > 1000 or any(ord(c) < 32 and c not in '\r\n' for c in address):
                errors.append(f'Row {line}: LOCATION must be up to 1,000 characters without unsupported control characters.')
                continue
            phone = re.sub(r'[\s().-]', '', phone)
            if phone and not re.fullmatch(r'\+?[0-9]{7,15}', phone):
                errors.append(f'Row {line}: PHONE must contain 7–15 digits, optionally prefixed by +, or be blank.')
                continue
            matches = existing[normalized(code)]
            if len(matches) > 1:
                errors.append(f'Row {line}: existing branch codes differ only by letter case; resolve them in administration first.')
                continue
            old = matches[0] if matches else None
            if old and not old.active:
                errors.append(f'Row {line}: branch is inactive. An administrator must review it before importing.')
                continue
            if (not old or normalized(old.name) != normalized(name)) and names[normalized(name)] - {normalized(code)}:
                errors.append(f'Row {line}: this branch name belongs to another code. Use its existing branch code.')
                continue
            store_code = data.get('store_code', '') or (old.store_code if old else None)
            if store_code and (len(store_code) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in store_code)):
                errors.append(f'Row {line}: STORE_CODE must be up to 120 characters without control characters.')
                continue
            if store_code and store_codes.get(store_code, normalized(code)) != normalized(code):
                errors.append(f'Row {line}: STORE_CODE belongs to another branch. Each store code must be unique.')
                continue
            row = {
                'pk': old.pk if old else None, 'code': old.code if old else code.upper(), 'name': name,
                'address': address or (old.address if old else ''), 'phone': phone or (old.phone if old else ''),
                'store_code': store_code,
            }
            row['action'] = 'Create' if not old else ('Update' if any(
                getattr(old, field) != row[field] for field in ['name', 'address', 'phone', 'store_code']) else 'Unchanged')
            row['changed_fields'] = ['name', 'address', 'phone', 'store_code'] if not old else [
                field for field in ['name', 'address', 'phone', 'store_code'] if getattr(old, field) != row[field]]
            key = normalized(code)
            if key in planned_branches and planned_branches[key] != row:
                errors.append(f'Row {line}: repeated branch details conflict. Repeat the same name, location, phone and store code for each chair.')
                continue
            planned_branches[key] = row
            if store_code:
                store_codes[store_code] = key
            names[normalized(name)].add(key)
            chair_code, chair_name = data.get('chair_code', ''), data.get('chair_name', '')
            if not chair_code and not chair_name:
                continue
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,19}', chair_code) or not chair_name or len(chair_name) > 80 or any(ord(c) < 32 for c in chair_name):
                errors.append(f'Row {line}: supply both CHAIR_CODE (up to 20 letters/numbers/_/-) and CHAIR_NAME (up to 80 characters).')
                continue
            chair_key = (key, normalized(chair_code))
            if chair_key in planned_chairs:
                errors.append(f'Row {line}: duplicate chair code within this branch.')
                continue
            matches = chairs[chair_key]
            if len(matches) > 1 or (matches and not matches[0].active):
                errors.append(f'Row {line}: existing chair is inactive or has ambiguous codes. Review it in administration first.')
                continue
            old_chair = matches[0] if matches else None
            planned_chairs[chair_key] = {
                'pk': old_chair.pk if old_chair else None, 'branch_code': row['code'],
                'code': old_chair.code if old_chair else chair_code.upper(), 'name': chair_name,
                'action': 'Create' if not old_chair else ('Update' if old_chair.name != chair_name else 'Unchanged'),
            }
    except csv.Error as exc:
        raise BranchImportProblem('CSV quoting or field format is invalid. Export CSV UTF-8 from Excel.') from exc
    if errors:
        raise BranchImportProblem('\n'.join(errors[:20]) + ('\nMore errors exist; correct the CSV and validate again.' if len(errors) > 20 else ''))
    if not planned_branches:
        raise BranchImportProblem('CSV contains no branch rows.')
    return {'branches': list(planned_branches.values()), 'chairs': list(planned_chairs.values())}


def expire_branch_imports():
    BranchCSVImport.objects.filter(status='PENDING', expires_at__lte=timezone.now()).update(status='EXPIRED', plan={})


def stage_branch_import(actor, filename, payload):
    before = branch_master_digest()
    plan = parse_branch_csv(payload)
    if branch_master_digest() != before:
        raise BranchImportProblem('Branches or chairs changed during validation. Validate again.')
    summary = {kind: {action.lower(): sum(row['action'] == action for row in plan[kind])
                     for action in ['Create', 'Update', 'Unchanged']} for kind in ['branches', 'chairs']}
    with transaction.atomic():
        BranchCSVImport.objects.filter(created_by=actor, status='PENDING').update(status='CANCELLED', plan={})
        return BranchCSVImport.objects.create(created_by=actor,
            filename=filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1][:160],
            sha256=hashlib.sha256(payload).hexdigest(), master_digest=before, plan=plan, summary=summary,
            expires_at=timezone.now() + timedelta(minutes=30))


def apply_branch_import(identifier, actor):
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [73810425])
            stage = BranchCSVImport.objects.select_for_update().get(pk=identifier, created_by=actor)
            if stage.status != 'PENDING' or stage.expires_at <= timezone.now():
                raise BranchImportProblem('This preview expired or was already used. Validate the CSV again.')
            with connection.cursor() as cursor:
                # Prevent concurrent manual changes between digest check and writes.
                cursor.execute('LOCK TABLE operations_branch, operations_chair IN SHARE ROW EXCLUSIVE MODE')
            if branch_master_digest() != stage.master_digest:
                raise BranchImportProblem('Branches or chairs changed after preview. Nothing was imported; validate again.')
            created_branches = {}
            for row in stage.plan['branches']:
                branch = Branch.objects.get(pk=row['pk']) if row['pk'] else Branch(code=row['code'])
                for field in ['name', 'address', 'phone', 'store_code']:
                    setattr(branch, field, row[field])
                if row['action'] != 'Unchanged':
                    branch.save()
                    audit(actor, branch, row['action'])
                created_branches[row['code']] = branch
            for row in stage.plan['chairs']:
                chair = Chair.objects.get(pk=row['pk']) if row['pk'] else Chair(branch=created_branches[row['branch_code']], code=row['code'])
                chair.name = row['name']
                if row['action'] != 'Unchanged':
                    chair.save()
                    audit(actor, chair, row['action'])
            stage.status, stage.applied_at, stage.plan = 'APPLIED', timezone.now(), {}
            stage.save(update_fields=['status', 'applied_at', 'plan'])
            return stage.summary
    except IntegrityError as exc:
        raise BranchImportProblem('The import could not complete. All changes were rolled back; validate again.') from exc


def audit(actor, obj, action):
    LogEntry.objects.create(user=actor, content_type=ContentType.objects.get_for_model(obj),
        object_id=str(obj.pk), object_repr=str(obj)[:200], action_flag=ADDITION if action == 'Create' else CHANGE,
        change_message='Created or updated through confirmed branch/chair CSV import.')
