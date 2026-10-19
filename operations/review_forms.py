from django import forms
from django.utils import timezone

from .forms import BootstrapMixin
from .models import Branch, ReviewIssue
from .review_workflow import branch_managers, is_review_senior
from .roster import working_branch


class VersionForm(BootstrapMixin, forms.Form):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class FollowupUpdateForm(VersionForm):
    customer_followup = forms.ChoiceField(choices=ReviewIssue.CUSTOMER_FOLLOWUP, label='Customer follow-up')
    staff_followup = forms.ChoiceField(choices=[('NEW', 'Not started'), ('IN_PROGRESS', 'In progress'), ('RESOLVED', 'Completed')], label='Staff follow-up')
    action_summary = forms.CharField(max_length=3000, label='Action / service note',
        widget=forms.Textarea(attrs={'rows': 3}), help_text='Briefly describe what was done or what happens next.')

    def __init__(self, *args, case, issue, **kwargs):
        status = 'NEW' if issue.status in ['NEW', 'ACKNOWLEDGED'] else 'RESOLVED' if issue.status == 'RESOLVED' else 'IN_PROGRESS'
        choices = list(self.base_fields['staff_followup'].choices)
        if issue.status in ['CLOSED_UNRESOLVED', 'NOT_ACTIONABLE']:
            choices.append((issue.status, issue.get_status_display()))
            status = issue.status
        super().__init__(*args, initial={'version': case.version, 'customer_followup': issue.customer_followup,
            'staff_followup': status, 'action_summary': issue.action_summary}, auto_id=f'followup-{issue.pk}-%s', **kwargs)
        self.fields['staff_followup'].choices = choices


class IssueUpdateForm(VersionForm):
    status = forms.ChoiceField(choices=ReviewIssue.STATUS, label='Work status')
    confirmed_priority = forms.ChoiceField(choices=[('', 'Not triaged yet')] + ReviewIssue.PRIORITIES, required=False,
                                          label='Manager-confirmed priority')
    assignee = forms.ModelChoiceField(queryset=branch_managers(None).none(), required=False, label='Assigned manager')
    action_summary = forms.CharField(max_length=3000, label='Action taken / next action', widget=forms.Textarea(attrs={'rows': 3}))
    outcome = forms.CharField(max_length=3000, required=False, label='Outcome / reason for closure', widget=forms.Textarea(attrs={'rows': 2}))
    customer_confirmation = forms.ChoiceField(choices=ReviewIssue.CONFIRMATION, label='Customer confirmation')
    evidence_reference = forms.CharField(max_length=250, required=False, label='Internal evidence reference',
                                         help_text='Use a reference only; keep private details out of public replies.')
    waiting_reason = forms.ChoiceField(choices=[('', 'Choose when waiting')] + list(ReviewIssue._meta.get_field('waiting_reason').choices), required=False)
    next_review_at = forms.DateTimeField(required=False, label='Review again at (India time)',
                                         widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}))

    def __init__(self, *args, case, issue, **kwargs):
        self.issue = issue
        initial = {name: getattr(issue, name) for name in self.base_fields if name != 'version'}
        initial.update(version=case.version)
        super().__init__(*args, initial=initial, auto_id=f'issue-{issue.pk}-%s', **kwargs)
        self.fields['assignee'].queryset = branch_managers(case.branch)

    def clean(self):
        data = super().clean()
        if data.get('status') in ReviewIssue.TERMINAL and not data.get('outcome'):
            self.add_error('outcome', 'Explain the result, or why the issue could not be resolved / is not actionable.')
        if data.get('status') == 'WAITING':
            if not data.get('waiting_reason'):
                self.add_error('waiting_reason', 'Choose what you are waiting for.')
            when = data.get('next_review_at')
            if not when or when <= timezone.now():
                self.add_error('next_review_at', 'Choose a future date and time to review this issue.')
        return data


class ClosureForm(VersionForm):
    summary = forms.CharField(max_length=3000, label='Resolution summary / triage decision', widget=forms.Textarea(attrs={'rows': 3}))
    service_description = forms.CharField(max_length=3000, label='Service provided / corrective service description', widget=forms.Textarea(attrs={'rows': 3}))


class ReasonForm(VersionForm):
    reason = forms.CharField(max_length=1500, widget=forms.Textarea(attrs={'rows': 2}))


class AssignmentForm(ReasonForm):
    owner = forms.ModelChoiceField(queryset=branch_managers(None).none(), required=False, label='Branch manager', empty_label='Unassigned — HQ follow-up')

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['owner'].queryset = branch_managers(case.branch)


class ReplyForm(VersionForm):
    text = forms.CharField(max_length=4000, label='Public reply draft', required=False, widget=forms.Textarea(attrs={'rows': 5}),
                           help_text='Do not include internal notes, private contact details or investigation findings.')
    note = forms.CharField(max_length=1500, required=False, label='Communication decision / approval note', widget=forms.Textarea(attrs={'rows': 2}))


class ReviewIntakeForm(BootstrapMixin, forms.Form):
    branch = forms.ModelChoiceField(queryset=Branch.objects.none())
    subject = forms.CharField(max_length=180, label='Ticket subject', help_text='A short internal label for this review.')
    customer_label = forms.CharField(max_length=120, required=False, label='Customer / reviewer display name', help_text='Optional. Do not match a Google reviewer to a customer by name alone.')
    rating = forms.TypedChoiceField(choices=[('', 'Not provided')] + [(n, f'{n} / 5') for n in range(1, 6)], coerce=int, required=False, empty_value=None)
    text = forms.CharField(max_length=6000, label='Customer review / reported concern', widget=forms.Textarea(attrs={'rows': 4}), required=False)
    service_description = forms.CharField(max_length=3000, label='Service description', widget=forms.Textarea(attrs={'rows': 2}))
    category = forms.ChoiceField(choices=[('', 'No specific issue — review / triage only')] + ReviewIssue.CATEGORIES, required=False, label='Reported issue category')
    reported_issue = forms.CharField(max_length=2000, required=False, label='Reported issue', widget=forms.Textarea(attrs={'rows': 2}), help_text='Record only what the customer reported; low stars alone do not identify an issue.')

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        if is_review_senior(user):
            self.fields['branch'].queryset = Branch.objects.filter(active=True)
        else:
            branch = working_branch(user)
            self.fields['branch'].queryset = Branch.objects.filter(pk=branch.pk) if branch else Branch.objects.none()
            self.fields['branch'].initial = branch

    def clean(self):
        data = super().clean()
        if bool(data.get('category')) != bool(data.get('reported_issue')):
            self.add_error('reported_issue', 'Supply both a category and reported issue, or leave both blank for review-only triage.')
        return data


class AddIssueForm(VersionForm):
    category = forms.ChoiceField(choices=ReviewIssue.CATEGORIES)
    reported_issue = forms.CharField(max_length=2000, label='Customer-reported issue', widget=forms.Textarea(attrs={'rows': 3}))
