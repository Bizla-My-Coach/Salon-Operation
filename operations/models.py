import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Branch(TimeStamped):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} - {self.name}"


class Profile(TimeStamped):
    ROLE_CHOICES = [("ADMIN", "Admin"), ("GENERAL_MANAGER", "General manager"), ("MANAGER", "Manager"), ("EMPLOYEE", "Employee")]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="EMPLOYEE")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True)
    employee_code = models.CharField(max_length=30, blank=True)
    job_title = models.CharField(max_length=80, blank=True)
    mobile = models.CharField(max_length=30, blank=True)
    experience = models.CharField(max_length=80, blank=True)
    salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                 validators=[MinValueValidator(0)])
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=models.Q(salary__isnull=True) | models.Q(salary__gte=0),
            name='nonnegative_staff_salary',
        )]

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class SOPWorkbookImport(models.Model):
    """Private, short-lived staging plus an audit/master-data recovery snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sop_imports")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    filename = models.CharField(max_length=160)
    sha256 = models.CharField(max_length=64)
    workbook = models.BinaryField(default=bytes, editable=False)
    master_digest = models.CharField(max_length=64)
    summary = models.CharField(max_length=2000)
    status = models.CharField(max_length=12, default="PENDING", choices=[
        ("PENDING", "Awaiting confirmation"), ("APPLIED", "Imported"),
        ("EXPIRED", "Expired"), ("CANCELLED", "Cancelled"),
    ])
    applied_at = models.DateTimeField(null=True, blank=True)
    master_snapshot = models.BinaryField(default=bytes, editable=False)

    class Meta:
        ordering = ["-created_at"]


class StaffCSVImport(models.Model):
    """Private, expiring account-creation preview; never stores plaintext passwords."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='staff_csv_imports')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    filename = models.CharField(max_length=160)
    sha256 = models.CharField(max_length=64)
    rows = models.JSONField(default=list, editable=False)
    row_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default='PENDING', choices=[
        ('PENDING', 'Awaiting confirmation'), ('APPLIED', 'Imported'),
        ('EXPIRED', 'Expired'), ('CANCELLED', 'Cancelled'),
    ])
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class BranchCSVImport(models.Model):
    """Owner-bound branch/chair preview, cleared when applied or discarded."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='branch_csv_imports')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    filename = models.CharField(max_length=160)
    sha256 = models.CharField(max_length=64)
    master_digest = models.CharField(max_length=64)
    plan = models.JSONField(default=dict, editable=False)
    summary = models.JSONField(default=dict, editable=False)
    status = models.CharField(max_length=12, default='PENDING', choices=[
        ('PENDING', 'Awaiting confirmation'), ('APPLIED', 'Imported'),
        ('EXPIRED', 'Expired'), ('CANCELLED', 'Cancelled'),
    ])
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class BranchDuty(TimeStamped):
    """One person's effective workplace for one local calendar day.

    An explicit leave record overrides the home branch. One row per person/day
    makes conflicting simultaneous branch assignments impossible at the DB level.
    """

    STATUS_CHOICES = [("WORK", "Working"), ("LEAVE", "On leave")]
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="branch_duties")
    date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="WORK")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True)
    updated_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="roster_changes")

    class Meta:
        ordering = ["date", "user__username"]
        constraints = [
            models.UniqueConstraint(fields=["user", "date"], name="one_branch_duty_per_person_day"),
            models.CheckConstraint(
                condition=(models.Q(status="WORK", branch__isnull=False) | models.Q(status="LEAVE", branch__isnull=True)),
                name="valid_branch_duty_status_branch",
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.status == "WORK" and not self.branch_id:
            raise ValidationError({"branch": "Choose a working branch."})
        if self.status == "LEAVE" and self.branch_id:
            raise ValidationError({"branch": "Leave must not have a working branch."})

    def __str__(self):
        return f"{self.user} - {self.date}: {self.branch or self.get_status_display()}"


class Chair(TimeStamped):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="chairs")
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=80)
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("branch", "code")

    def __str__(self):
        return f"{self.branch.code} - {self.name}"


class Service(TimeStamped):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=80, blank=True)
    customer_type = models.CharField(max_length=30, blank=True)
    service_type = models.CharField(max_length=60, blank=True)
    standard_duration_minutes = models.PositiveIntegerField(default=30)
    current_version = models.CharField(max_length=20, blank=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class SubService(TimeStamped):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=80, blank=True)
    standard_output_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    output_unit = models.CharField(max_length=30, blank=True)
    procedure_notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class ServiceDetail(TimeStamped):
    external_id = models.CharField(max_length=30, unique=True)
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="service_details")
    sub_service = models.ForeignKey(SubService, on_delete=models.PROTECT, related_name="service_details")
    sequence = models.PositiveIntegerField(default=10)
    required_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    mandatory = models.BooleanField(default=True)
    mapping_status = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence", "id"]
        unique_together = ("service", "sub_service", "sequence")

    def __str__(self):
        return f"{self.service.name} #{self.sequence} {self.sub_service.name}"


class OperationalTask(TimeStamped):
    external_id = models.CharField(max_length=30, unique=True)
    sub_service = models.ForeignKey(SubService, on_delete=models.CASCADE, related_name="tasks")
    sequence = models.PositiveIntegerField(default=10)
    activity_code = models.CharField(max_length=30, blank=True)
    name = models.CharField(max_length=180)
    role_code = models.CharField(max_length=30, blank=True)
    employee_role = models.CharField(max_length=100, blank=True)
    active_labour_minutes = models.PositiveIntegerField(default=0)
    passive_time_minutes = models.PositiveIntegerField(default=0)
    supports_waiting = models.BooleanField(default=False)
    staff_instructions = models.TextField(blank=True)
    required_skill = models.CharField(max_length=100, blank=True)
    allowed_staff_type = models.CharField(max_length=120, blank=True)
    default_customer_type = models.CharField(max_length=30, blank=True)
    is_skippable = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence", "id"]
        unique_together = ("sub_service", "sequence")

    def __str__(self):
        return f"{self.external_id} - {self.name}"


class InventoryItem(TimeStamped):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=80, blank=True)
    source_method = models.CharField(max_length=50, blank=True)
    base_unit = models.CharField(max_length=30, blank=True)
    purchase_pack_quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    purchase_pack_unit = models.CharField(max_length=30, blank=True)
    resource_type = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} - {self.name}"


class TaskInventory(TimeStamped):
    external_id = models.CharField(max_length=30, unique=True)
    task = models.ForeignKey(OperationalTask, on_delete=models.CASCADE, related_name="inventory_requirements")
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="task_inventory")
    inventory = models.ForeignKey(InventoryItem, on_delete=models.PROTECT)
    standard_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit = models.CharField(max_length=30, blank=True)
    waste_percent = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    effective_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    mapping_status = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["inventory__name"]


class Equipment(TimeStamped):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=80, blank=True)
    utility_type = models.CharField(max_length=50, blank=True)
    branch_code = models.CharField(max_length=30, blank=True)
    rated_power_kw = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    water_litres_per_minute = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} - {self.name}"


class TaskEquipment(TimeStamped):
    external_id = models.CharField(max_length=30, unique=True)
    task = models.ForeignKey(OperationalTask, on_delete=models.CASCADE, related_name="equipment_requirements")
    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT)
    quantity_required = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    equipment_usage_minutes = models.PositiveIntegerField(default=0)
    utility_type = models.CharField(max_length=50, blank=True)
    utility_minutes = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["equipment__name"]


# Kept for backward compatibility with the existing CSV import and demo seed.
class SOPTask(TimeStamped):
    PHASES = [("BEFORE", "Before service"), ("DURING", "During service"), ("FINISHING", "Finishing"), ("AFTER", "After service")]
    TYPES = [("SERVICE", "Service step"), ("CONSULT", "Consultation"), ("HYGIENE", "Cleaning / sanitization"), ("TOWEL", "Towel / laundry"), ("QUALITY", "Quality check"), ("RECOMMEND", "Recommendation")]
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="sop_tasks")
    sequence = models.PositiveIntegerField(default=10)
    phase = models.CharField(max_length=20, choices=PHASES, default="DURING")
    task_type = models.CharField(max_length=20, choices=TYPES, default="SERVICE")
    title = models.CharField(max_length=160)
    instructions = models.TextField(blank=True)
    required = models.BooleanField(default=True)
    can_skip = models.BooleanField(default=False)
    skip_reason_required = models.BooleanField(default=True)
    quick_action = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence", "id"]
        unique_together = ("service", "sequence")

    def __str__(self):
        return f"{self.service.name} #{self.sequence} {self.title}"


class Customer(TimeStamped):
    name = models.CharField(max_length=120)
    mobile = models.CharField(max_length=30, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Visit(TimeStamped):
    STATUS = [("WAITING", "Waiting"), ("ASSIGNED", "Assigned"), ("IN_PROGRESS", "In progress"), ("EMPLOYEE_DONE", "Employee completed"), ("VERIFIED", "Manager verified"), ("INVOICED", "Invoiced"), ("CLOSED", "Closed"), ("CANCELLED", "Cancelled")]
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="visits")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="visits")
    token_number = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=30, choices=STATUS, default="WAITING")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="visits_created")
    closed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Visit #{self.id} - {self.customer.name} ({self.branch.name})"

    @property
    def is_invoice_ready(self):
        active_services = self.services.exclude(status="CANCELLED")
        return (
            active_services.exists()
            and not active_services.exclude(status="VERIFIED").exists()
            and not Invoice.objects.filter(visit=self, status="COMPLETED").exists()
        )


class VisitService(TimeStamped):
    STATUS = [("ASSIGNED", "Assigned"), ("IN_PROGRESS", "In progress"), ("PAUSED", "Paused"), ("EMPLOYEE_DONE", "Employee completed"), ("VERIFIED", "Manager verified"), ("CANCELLED", "Cancelled")]
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="services")
    service = models.ForeignKey(Service, on_delete=models.PROTECT)
    order_number = models.PositiveIntegerField(default=1)
    employee = models.ForeignKey(User, on_delete=models.PROTECT, related_name="assigned_services")
    chair = models.ForeignKey(Chair, on_delete=models.SET_NULL, null=True, blank=True)
    assigned_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="service_assignments")
    status = models.CharField(max_length=30, choices=STATUS, default="ASSIGNED")
    assigned_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    employee_completed_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    employee_notes = models.TextField(blank=True)
    manager_notes = models.TextField(blank=True)
    verified_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="verified_services"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancelled_services"
    )
    cancellation_reason = models.CharField(max_length=250, blank=True)
    replaces = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replacements"
    )

    class Meta:
        ordering = ["visit_id", "order_number", "id"]

    def __str__(self):
        return f"Visit #{self.visit_id} - {self.visit.customer.name} - #{self.order_number} {self.service.name}"

    @property
    def progress_percent(self):
        total = getattr(self, "task_total", None)
        done = getattr(self, "task_done", None)
        if total is None:
            total = self.tasks.count()
            done = self.tasks.filter(status__in=["COMPLETED", "SKIPPED"]).count()
        return int(done * 100 / total) if total else 0


class VisitTask(TimeStamped):
    STATUS = [("PENDING", "Pending"), ("IN_PROGRESS", "In progress"), ("WAITING", "Processing / setting"), ("COMPLETED", "Completed"), ("SKIPPED", "Skipped"), ("CANCELLED", "Cancelled")]
    visit_service = models.ForeignKey(VisitService, on_delete=models.CASCADE, related_name="tasks")
    source_task = models.ForeignKey(SOPTask, on_delete=models.SET_NULL, null=True, blank=True)
    source_operational_task = models.ForeignKey(OperationalTask, on_delete=models.SET_NULL, null=True, blank=True)
    sequence = models.PositiveIntegerField()
    sub_service_name = models.CharField(max_length=160, blank=True)
    phase = models.CharField(max_length=20, choices=SOPTask.PHASES)
    task_type = models.CharField(max_length=20, choices=SOPTask.TYPES)
    title = models.CharField(max_length=180)
    instructions = models.TextField(blank=True)
    active_labour_minutes = models.PositiveIntegerField(default=0)
    passive_time_minutes = models.PositiveIntegerField(default=0)
    supports_waiting = models.BooleanField(default=False)
    staff_instructions = models.TextField(blank=True)
    required = models.BooleanField(default=True)
    can_skip = models.BooleanField(default=False)
    skip_reason_required = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    skip_reason = models.CharField(max_length=250, blank=True)
    note = models.TextField(blank=True)
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["sequence", "id"]
        unique_together = ("visit_service", "sequence")

    def __str__(self):
        return f"Visit #{self.visit_service.visit_id} - {self.visit_service.visit.customer.name} - {self.title}"

    def measured_seconds(self, kind):
        now = timezone.now()
        return int(sum(max(((s.ended_at or now) - s.started_at).total_seconds(), 0)
                       for s in self.timing_segments.all() if s.kind == kind))

    @property
    def labour_seconds(self):
        return self.measured_seconds("ACTIVE")

    @property
    def waiting_seconds(self):
        return self.measured_seconds("WAITING")


class TaskTimingSegment(models.Model):
    """Actual measured intervals, independent of workbook standard minutes."""
    task = models.ForeignKey(VisitTask, on_delete=models.CASCADE, related_name="timing_segments")
    kind = models.CharField(max_length=10, choices=[("ACTIVE", "Hands-on"), ("WAITING", "Waiting")])
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at", "id"]
        constraints = [models.UniqueConstraint(fields=["task"], condition=models.Q(ended_at__isnull=True), name="one_open_task_timing_segment")]


class Invoice(TimeStamped):
    NUMBER_PREFIX = 'INV/26-27/'
    STATUS = [("DRAFT", "Draft"), ("COMPLETED", "Completed"), ("VOID", "Void")]
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="invoice")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='invoices', editable=False)
    invoice_number = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=50, blank=True)
    entered_by = models.ForeignKey(User, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS, default="COMPLETED")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['branch', 'invoice_number'], name='unique_invoice_number_per_branch',
        )]

    def clean(self):
        super().clean()
        if self.visit_id:
            from django.core.exceptions import ValidationError

            self.branch_id = self.visit.branch_id
            if Invoice.objects.filter(branch_id=self.branch_id, invoice_number=self.invoice_number).exclude(pk=self.pk).exists():
                raise ValidationError({'invoice_number': 'This invoice number is already in use in this branch.'})

    def save(self, *args, **kwargs):
        self.branch_id = self.visit.branch_id
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'branch'}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.invoice_number} - {self.visit.customer.name} (Visit #{self.visit_id})"


class FeedbackQuestion(TimeStamped):
    text = models.CharField(max_length=220)
    sequence = models.PositiveIntegerField(default=10)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence", "id"]

    def __str__(self):
        return self.text


class Feedback(TimeStamped):
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE, related_name="feedback")
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    suggestion = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Feedback #{self.pk} - {self.visit.customer.name} (Visit #{self.visit_id})"


class FeedbackAnswer(models.Model):
    feedback = models.ForeignKey(Feedback, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(FeedbackQuestion, on_delete=models.PROTECT)
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])

    class Meta:
        unique_together = ("feedback", "question")

    def __str__(self):
        return f"{self.feedback.visit.customer.name} - {self.rating}/5 (Visit #{self.feedback.visit_id})"


def review_snapshot_expiry():
    return timezone.now() + timedelta(days=30)


class ReviewCase(TimeStamped):
    """Internal work record; source review content lives in an expiring snapshot."""
    STATUS = [('NEW', 'New'), ('ACKNOWLEDGED', 'Acknowledged'), ('IN_PROGRESS', 'In progress'),
              ('WAITING', 'Waiting'), ('READY', 'Ready to close'), ('CLOSED', 'Closed')]
    OUTCOMES = [('RESOLVED', 'Resolved'), ('PARTIAL', 'Partially resolved'),
                ('UNRESOLVED', 'Unresolved'), ('NOT_ACTIONABLE', 'Not actionable')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='review_cases')
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='review_cases')
    source_key = models.CharField(max_length=200, unique=True, default=uuid.uuid4)
    synthetic = models.BooleanField(default=False, db_index=True)
    subject = models.CharField(max_length=180)
    customer_label = models.CharField(max_length=120, blank=True)
    service_description = models.TextField(blank=True, max_length=3000)
    status = models.CharField(max_length=20, choices=STATUS, default='NEW', db_index=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    sensitive = models.BooleanField(default=False)
    case_followup_requires_human = models.BooleanField(default=True)
    requires_senior_closure = models.BooleanField(default=False)
    requires_senior_followup = models.BooleanField(default=False)
    ai_requires_human_review = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    closure_summary = models.TextField(blank=True, max_length=3000)
    closure_outcome = models.CharField(max_length=20, choices=OUTCOMES, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='review_cases_closed')

    class Meta:
        ordering = ['received_at', 'id']
        indexes = [models.Index(fields=['branch', 'synthetic', 'status'])]

    @property
    def reference(self):
        return f'RV-{str(self.pk)[:8].upper()}'

    def __str__(self):
        return f'{self.reference} · {self.subject}'


class ReviewSnapshot(models.Model):
    case = models.OneToOneField(ReviewCase, on_delete=models.CASCADE, related_name='snapshot')
    text = models.TextField(blank=True, max_length=6000)
    rating = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    fetched_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(default=review_snapshot_expiry)

    @property
    def available(self):
        return self.expires_at > timezone.now()


class ReviewIssue(TimeStamped):
    STATUS = [('NEW', 'New'), ('ACKNOWLEDGED', 'Acknowledged'), ('IN_PROGRESS', 'In progress'),
              ('WAITING', 'Waiting'), ('RESOLVED', 'Resolved'),
              ('CLOSED_UNRESOLVED', 'Closed unresolved'), ('NOT_ACTIONABLE', 'Not actionable')]
    TERMINAL = ('RESOLVED', 'CLOSED_UNRESOLVED', 'NOT_ACTIONABLE')
    PRIORITIES = [('LOW', 'Low'), ('NORMAL', 'Normal'), ('HIGH', 'High'), ('URGENT', 'Urgent')]
    CATEGORIES = [('WAITING', 'Waiting time'), ('SERVICE', 'Service quality'), ('STAFF', 'Staff communication'),
                  ('BILLING', 'Billing'), ('HYGIENE', 'Hygiene'), ('HEALTH', 'Injury / health'),
                  ('CONDUCT', 'Sensitive conduct'), ('OTHER', 'Other')]
    CONFIRMATION = [('UNKNOWN', 'Unknown'), ('CONFIRMED', 'Customer confirmed'),
                    ('NOT_CONFIRMED', 'Not confirmed'), ('NOT_CONTACTABLE', 'Not contactable')]
    CUSTOMER_FOLLOWUP = [('NOT_CONTACTED', 'Not contacted'), ('AWAITING_REPLY', 'Awaiting reply'), ('CONTACTED', 'Contacted')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(ReviewCase, on_delete=models.CASCADE, related_name='issues')
    category = models.CharField(max_length=20, choices=CATEGORIES)
    reported_issue = models.TextField(max_length=2000)
    ai_priority = models.CharField(max_length=10, choices=PRIORITIES, blank=True)
    confirmed_priority = models.CharField(max_length=10, choices=PRIORITIES, blank=True)
    assignee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='review_issues')
    status = models.CharField(max_length=24, choices=STATUS, default='NEW', db_index=True)
    action_summary = models.TextField(blank=True, max_length=3000)
    outcome = models.TextField(blank=True, max_length=3000)
    evidence_reference = models.CharField(max_length=250, blank=True)
    customer_confirmation = models.CharField(max_length=20, choices=CONFIRMATION, default='UNKNOWN')
    customer_followup = models.CharField(max_length=20, choices=CUSTOMER_FOLLOWUP, default='NOT_CONTACTED')
    followup_submitted_at = models.DateTimeField(null=True, blank=True)
    followup_submitted_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='review_followups_submitted')
    waiting_reason = models.CharField(max_length=20, blank=True, choices=[
        ('CUSTOMER', 'Waiting for customer'), ('INTERNAL', 'Internal follow-up'), ('EXTERNAL', 'External assistance')])
    next_review_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='review_issues_completed')
    cycle = models.PositiveIntegerField(default=1)
    aws_issue_id = models.CharField(max_length=120, blank=True)
    source_active = models.BooleanField(default=True)
    source_review_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [models.UniqueConstraint(fields=['case', 'aws_issue_id'],
            condition=~models.Q(aws_issue_id=''), name='unique_aws_issue_per_case')]

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL

    @property
    def overdue(self):
        return not self.is_terminal and self.due_at is not None and self.due_at < timezone.now()

    @property
    def staff_followup_label(self):
        if self.status == 'RESOLVED':
            return 'Completed'
        if self.status in ('NEW', 'ACKNOWLEDGED'):
            return 'Not started'
        if self.status in self.TERMINAL:
            return self.get_status_display()
        if self.status == 'WAITING':
            return 'Awaiting follow-up'
        return 'In progress'


class ReviewActivity(models.Model):
    case = models.ForeignKey(ReviewCase, on_delete=models.CASCADE, related_name='activities')
    issue = models.ForeignKey(ReviewIssue, on_delete=models.SET_NULL, null=True, blank=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True)
    action = models.CharField(max_length=60)
    note = models.TextField(blank=True, max_length=6000)
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-id']


class ReviewReply(TimeStamped):
    STATUS = [('NONE', 'No draft'), ('DRAFT', 'Draft'), ('AWAITING_APPROVAL', 'Awaiting approval'),
              ('APPROVED', 'Approved internally'), ('NO_REPLY', 'No public reply planned')]
    case = models.OneToOneField(ReviewCase, on_delete=models.CASCADE, related_name='reply')
    text = models.TextField(blank=True, max_length=4000)
    revision = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=24, choices=STATUS, default='NONE')
    author = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='review_reply_drafts')
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='review_replies_approved')
    approved_at = models.DateTimeField(null=True, blank=True)


class ReviewAcknowledgement(TimeStamped):
    """Trusted template intent; preview never dispatches it to Google."""
    STATUS = [('HELD', 'Pending manager assignment'), ('PREVIEW', 'Ready in preview · not sent'),
              ('QUEUED', 'Queued'), ('SUBMITTED', 'Submitted · visibility unconfirmed'),
              ('PUBLISHED', 'Publication confirmed'), ('FAILED', 'Failed'),
              ('UNKNOWN', 'Unknown · reconcile before retry'), ('NO_REPLY', 'No acknowledgement planned')]
    case = models.OneToOneField(ReviewCase, on_delete=models.CASCADE, related_name='acknowledgement')
    intent_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    template_key = models.CharField(max_length=60)
    policy_version = models.CharField(max_length=40, default='ack-preview-v1')
    text = models.TextField(blank=True, max_length=1000)
    status = models.CharField(max_length=20, choices=STATUS, default='HELD')
    assignment_confirmed_at = models.DateTimeField(null=True, blank=True)
    assigned_manager = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True)
    source_fetched_at = models.DateTimeField(null=True, blank=True)
    acknowledgement_sent_at = models.DateTimeField(null=True, blank=True)
    publication_confirmed_at = models.DateTimeField(null=True, blank=True)


class ReviewNotification(TimeStamped):
    case = models.ForeignKey(ReviewCase, on_delete=models.CASCADE, related_name='notifications')
    recipient = models.ForeignKey(User, on_delete=models.PROTECT)
    dedupe_key = models.CharField(max_length=200, unique=True)
    status = models.CharField(max_length=12, default='PENDING', choices=[
        ('PENDING', 'Pending delivery'), ('PREVIEW', 'Preview only · no delivery'), ('DELIVERED', 'Delivered')])
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['recipient', 'read_at', '-created_at'], name='review_alert_rec_read_idx')]


REVIEW_ENVIRONMENTS = [('production', 'Production'), ('sandbox', 'Sandbox')]
PUBLICATION_STATES = [(s, s.replace('_', ' ').title()) for s in
    ('pending', 'submitted', 'published', 'failed', 'uncertain', 'superseded', 'skipped', 'human_review')]


class GoogleReviewLocation(TimeStamped):
    """Trusted server-side location mapping; AWS/AI cannot choose the branch."""
    environment = models.CharField(max_length=12, choices=REVIEW_ENVIRONMENTS)
    location_name = models.CharField(max_length=300)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['environment', 'location_name'], name='unique_google_location_env')]

    def __str__(self):
        return f'{self.environment}: {self.location_name} → {self.branch}'


class GoogleReview(TimeStamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    environment = models.CharField(max_length=12, choices=REVIEW_ENVIRONMENTS, db_index=True)
    review_name = models.CharField(max_length=400)
    account_name = models.CharField(max_length=120)
    location_name = models.CharField(max_length=300)
    google_review_id = models.CharField(max_length=160)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='google_reviews')
    case = models.OneToOneField(ReviewCase, on_delete=models.PROTECT, null=True, blank=True, related_name='google_review')
    star_rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    review_text = models.TextField(blank=True, max_length=6000)
    reviewer_display_name = models.CharField(max_length=120, blank=True)
    google_created_at = models.DateTimeField()
    google_updated_at = models.DateTimeField(db_index=True)
    analysis_version = models.CharField(max_length=160)
    result_revision = models.PositiveBigIntegerField(default=0)
    analysis = models.JSONField(default=dict)
    processing = models.JSONField(default=dict)
    analyzed_at = models.DateTimeField()
    profile_key = models.CharField(max_length=120, blank=True)
    version_fingerprint = models.CharField(max_length=64)
    sentiment = models.CharField(max_length=12, choices=[(s, s.title()) for s in ('positive', 'negative', 'mixed', 'neutral', 'unknown')])
    priority = models.CharField(max_length=10, choices=ReviewIssue.PRIORITIES, default='NORMAL')
    requires_human_review = models.BooleanField(default=False)
    needs_followup = models.BooleanField(default=False)
    generated_reply = models.TextField(blank=True, max_length=4000)
    publication_status = models.CharField(max_length=16, choices=PUBLICATION_STATES, default='pending')
    published_text = models.TextField(blank=True, max_length=4000)
    published_at = models.DateTimeField(null=True, blank=True)
    publication_failure_code = models.CharField(max_length=120, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name='google_reviews_closed')
    closed_review_version = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-google_updated_at', 'id']
        constraints = [models.UniqueConstraint(fields=['environment', 'review_name'], name='unique_google_review_env')]
        indexes = [models.Index(fields=['environment', 'branch', 'sentiment'])]

    @property
    def simple_closed(self):
        return bool(self.closed_at and self.closed_review_version == self.google_updated_at)

    @property
    def handling_status(self):
        if self.case_id:
            if self.case.closure_outcome in ('UNRESOLVED', 'PARTIAL'):
                return 'Unresolved'
            if self.case.closed_at:
                return 'Resolved' if self.case.closure_outcome == 'RESOLVED' else 'Closed after follow-up'
            if self.case.issues.filter(status='CLOSED_UNRESOLVED').exists():
                return 'Unresolved'
            return {'NEW': 'Open', 'ACKNOWLEDGED': 'In progress', 'IN_PROGRESS': 'In progress',
                    'WAITING': 'Awaiting follow-up', 'READY': 'Ready to close'}.get(self.case.status, 'Open')
        return 'Closed' if self.simple_closed else 'Open'


class GoogleReviewEvent(models.Model):
    """Immutable source payload and original receipt, including quarantined events."""
    environment = models.CharField(max_length=12, choices=REVIEW_ENVIRONMENTS)
    event_id = models.CharField(max_length=160)
    kind = models.CharField(max_length=16, choices=[('review', 'Review ingestion'), ('publication', 'Publication outcome')])
    fingerprint = models.CharField(max_length=64)
    payload = models.JSONField()
    review_name = models.CharField(max_length=400)
    review = models.ForeignKey(GoogleReview, on_delete=models.PROTECT, null=True, blank=True, related_name='events')
    status = models.CharField(max_length=20, default='committed')
    receipt = models.JSONField(default=dict)
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-received_at', '-id']
        constraints = [models.UniqueConstraint(fields=['environment', 'event_id'], name='unique_google_event_env')]


class GoogleReplyPublication(models.Model):
    event = models.OneToOneField(GoogleReviewEvent, on_delete=models.PROTECT, related_name='publication')
    environment = models.CharField(max_length=12, choices=REVIEW_ENVIRONMENTS)
    review_name = models.CharField(max_length=400)
    review_updated_at = models.DateTimeField()
    analysis_version = models.CharField(max_length=160)
    result_revision = models.PositiveBigIntegerField(default=0)
    evidence_rank = models.PositiveSmallIntegerField(default=0)
    evidence_at = models.DateTimeField(null=True, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    occurred_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=PUBLICATION_STATES)
    actual_reply_text = models.TextField(blank=True, max_length=4000)
    published_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=120, blank=True)
    reply_kind = models.CharField(max_length=60, blank=True)

    class Meta:
        indexes = [models.Index(fields=['environment', 'review_name', 'review_updated_at'])]


class GoogleReviewAction(models.Model):
    review = models.ForeignKey(GoogleReview, on_delete=models.PROTECT, related_name='actions')
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    action = models.CharField(max_length=30)
    note = models.TextField(blank=True, max_length=1500)
    review_version = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)
