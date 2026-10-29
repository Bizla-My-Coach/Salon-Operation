from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.password_validation import validate_password
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.db.models import Q
from datetime import timedelta
from django.utils import timezone

from .models import Branch, Chair, Invoice, Profile, Service, Visit, VisitService
from .roster import working_employees


class BootstrapMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
                continue
            field.widget.attrs["class"] = "form-control"


class SelfDetailsForm(BootstrapMixin, forms.ModelForm):
    mobile = forms.CharField(max_length=30, required=False, label="Mobile number")

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['mobile'].initial = self.instance.profile.mobile
        self.fields['mobile'].widget.attrs.update({'autocomplete': 'tel', 'type': 'tel'})
        for name, autocomplete in [('first_name', 'given-name'), ('last_name', 'family-name'), ('email', 'email')]:
            self.fields[name].widget.attrs['autocomplete'] = autocomplete

    def clean_mobile(self):
        value = self.cleaned_data['mobile'].strip()
        if value and (not any(c.isdigit() for c in value) or any(c not in '+-() .0123456789' for c in value)):
            raise forms.ValidationError('Enter a valid mobile number.')
        return value

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            # Only self-editable fields may change; never role, branch or access.
            user.save(update_fields=['first_name', 'last_name', 'email'])
            user.profile.mobile = self.cleaned_data['mobile']
            user.profile.save(update_fields=['mobile', 'updated_at'])
        return user


class SelfPasswordChangeForm(BootstrapMixin, PasswordChangeForm):
    pass


class StaffPasswordResetForm(BootstrapMixin, SetPasswordForm):
    actor_password = forms.CharField(
        label='Your current password', strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}),
        help_text='Confirm your own password to authorize this reset.',
    )

    def __init__(self, user, *args, actor, **kwargs):
        self.actor = actor
        super().__init__(user, *args, **kwargs)
        self.order_fields(['actor_password', 'new_password1', 'new_password2'])

    def clean_actor_password(self):
        password = self.cleaned_data['actor_password']
        if not self.actor.check_password(password):
            raise forms.ValidationError('Your current password is incorrect.')
        return password

    def clean_new_password1(self):
        password = self.cleaned_data['new_password1']
        if self.user.check_password(password):
            raise forms.ValidationError('Choose a password different from the employee\'s current password.')
        return password


class VisitCreateForm(BootstrapMixin, forms.Form):
    customer_name = forms.CharField(max_length=120, widget=forms.TextInput(attrs={
        'autocomplete': 'name', 'placeholder': 'Customer full name',
    }))
    mobile = forms.CharField(
        max_length=10,
        required=False,
        help_text="Optional. Enter exactly 10 digits when provided.",
        widget=forms.TextInput(attrs={'type': 'tel', 'inputmode': 'numeric', 'autocomplete': 'tel-national', 'placeholder': '10-digit mobile number'}),
    )
    invoice_number = forms.CharField(
        required=False,
        disabled=True,
        label="Invoice number",
        help_text="Available after every service has been verified.",
        widget=forms.TextInput(attrs={"placeholder": "Available after verification"}),
    )

    def clean_mobile(self):
        mobile = self.cleaned_data["mobile"].strip()
        if mobile and (not mobile.isdigit() or len(mobile) != 10):
            raise forms.ValidationError("Enter a valid 10-digit mobile number.")
        return mobile


class CompleteInvoiceForm(VisitCreateForm):
    invoice_number = forms.RegexField(
        regex=r'\A[0-9]+\Z', max_length=50 - len(Invoice.NUMBER_PREFIX),
        label='Invoice number',
        help_text=f'Enter only the final digits, for example 977. Saved as {Invoice.NUMBER_PREFIX}977.',
        error_messages={'invalid': 'Enter only the digits after the final slash, for example 977.'},
        widget=forms.TextInput(attrs={'inputmode': 'numeric', 'pattern': '[0-9]+', 'placeholder': '977'}),
    )

    def __init__(self, *args, branch, invoice=None, **kwargs):
        self.branch = branch
        self.invoice = invoice
        super().__init__(*args, **kwargs)
        value = self.initial.get('invoice_number', '')
        if value.startswith(Invoice.NUMBER_PREFIX):
            self.initial['invoice_number'] = value[len(Invoice.NUMBER_PREFIX):]

    def clean_invoice_number(self):
        digits = self.cleaned_data['invoice_number'].lstrip('0') or '0'
        value = Invoice.NUMBER_PREFIX + digits
        invoices = Invoice.objects.filter(branch=self.branch, invoice_number=value)
        if self.invoice and self.invoice.pk:
            invoices = invoices.exclude(pk=self.invoice.pk)
        if invoices.exists():
            raise forms.ValidationError("This invoice number is already in use in this branch.")
        return value


class VisitServiceAssignmentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = VisitService
        fields = ["order_number", "service", "employee", "chair"]

    def __init__(self, *args, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        current_service = self.instance.service_id if self.instance.pk else None
        current_employee = self.instance.employee_id if self.instance.pk else None
        current_chair = self.instance.chair_id if self.instance.pk else None
        self.execution_locked = bool(self.instance.pk and (
            self.instance.status != 'ASSIGNED'
            or self.instance.tasks.exclude(status='PENDING').exists()
        ))
        self.fields["service"].queryset = Service.objects.filter(Q(active=True) | Q(pk=current_service)).distinct()
        self.fields["employee"].queryset = User.objects.filter(
            Q(pk=current_employee) | Q(pk__in=working_employees(branch).values("pk"))
        ).distinct()
        self.fields["chair"].queryset = Chair.objects.filter(
            Q(pk=current_chair) | Q(branch=branch, active=True)
        ).distinct()
        if self.execution_locked:
            for field in self.fields.values():
                field.disabled = True


class BaseVisitServiceFormSet(BaseInlineFormSet):
    def __init__(self, *args, branch=None, **kwargs):
        self.branch = branch
        super().__init__(*args, **kwargs)
        for form in self.forms:
            if form.execution_locked:
                form.fields["DELETE"].disabled = True

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["branch"] = self.branch
        return kwargs

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active = [
            form for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("service")
        ]
        if not active:
            raise forms.ValidationError("Add at least one service to the visit.")
        order_numbers = [form.cleaned_data["order_number"] for form in active]
        if len(order_numbers) != len(set(order_numbers)):
            raise forms.ValidationError("Every active service must have a unique order number.")
        locked_orders = [
            form.instance.order_number for form in active
            if form.execution_locked
        ]
        if locked_orders:
            locked_prefix_end = max(locked_orders)
            editable_orders = [
                form.cleaned_data["order_number"] for form in active
                if not form.execution_locked
            ]
            if any(order <= locked_prefix_end for order in editable_orders):
                raise forms.ValidationError(
                    "Upcoming services must remain after every started or completed service."
                )


VisitServiceFormSet = inlineformset_factory(
    Visit,
    VisitService,
    form=VisitServiceAssignmentForm,
    formset=BaseVisitServiceFormSet,
    fields=["order_number", "service", "employee", "chair"],
    extra=0,
    can_delete=True,
)

InitialVisitServiceFormSet = inlineformset_factory(
    Visit,
    VisitService,
    form=VisitServiceAssignmentForm,
    formset=BaseVisitServiceFormSet,
    fields=["order_number", "service", "employee", "chair"],
    extra=1,
    can_delete=True,
)


class CancelAndReassignForm(BootstrapMixin, forms.Form):
    cancellation_reason = forms.CharField(max_length=250, widget=forms.Textarea(attrs={"rows": 3}))
    employee = forms.ModelChoiceField(queryset=User.objects.none())
    chair = forms.ModelChoiceField(queryset=Chair.objects.none(), required=False)

    def __init__(self, *args, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = working_employees(branch)
        self.fields["chair"].queryset = Chair.objects.filter(branch=branch, active=True)


class ServiceLookupForm(BootstrapMixin, forms.Form):
    service = forms.ModelChoiceField(queryset=Service.objects.filter(active=True), empty_label="Select a service")


class TaskActionForm(BootstrapMixin, forms.Form):
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    skip_reason = forms.CharField(required=False, max_length=250)


class VerifyForm(BootstrapMixin, forms.Form):
    manager_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))


class InvoiceForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["invoice_number"]


class UserCreateForm(BootstrapMixin, forms.Form):
    branch = forms.ModelChoiceField(queryset=Branch.objects.filter(active=True), label="Branch")
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, required=False, label="Last name (optional)")
    username = forms.CharField(required=False, disabled=True, label="Username", help_text="Generated automatically from the branch and name.")
    role = forms.ChoiceField(choices=Profile.ROLE_CHOICES, initial='EMPLOYEE')
    employee_code = forms.CharField(max_length=30, required=False)
    job_title = forms.CharField(max_length=80, required=False)

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        if not actor.is_superuser and actor.profile.role == 'GENERAL_MANAGER':
            self.fields['role'].choices = [('MANAGER', 'Manager'), ('EMPLOYEE', 'Employee')]

    def clean(self):
        from .staff_accounts import username_part
        cleaned = super().clean()
        for field in ('first_name', 'last_name'):
            value = cleaned.get(field)
            if value and not username_part(value):
                self.add_error(field, 'Enter a name containing letters or numbers.')
        branch = cleaned.get('branch')
        if branch and not username_part(branch.code):
            self.add_error('branch', 'This branch needs a code containing letters or numbers.')
        return cleaned


class BranchDutyForm(BootstrapMixin, forms.Form):
    user = forms.ModelChoiceField(queryset=User.objects.none(), label="Team member")
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    status = forms.ChoiceField(choices=[("WORK", "Working at branch"), ("LEAVE", "On leave")])
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.filter(active=True), required=False, label="Working branch",
        help_text="Only used for working duties. Leave overrides the person's branch for the selected dates.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(
            is_active=True, profile__active=True,
            profile__role__in=["GENERAL_MANAGER", "MANAGER", "EMPLOYEE"],
        ).select_related("profile").order_by("first_name", "username")

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and start < timezone.localdate():
            self.add_error("start_date", "Past roster dates cannot be changed.")
        if start and end and (end < start or end - start > timedelta(days=30)):
            self.add_error("end_date", "Choose up to 31 consecutive days.")
        if cleaned.get("status") == "WORK" and not cleaned.get("branch"):
            self.add_error("branch", "Choose a working branch.")
        if cleaned.get("status") == "LEAVE":
            cleaned["branch"] = None
        return cleaned
