import datetime

from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    Branch,
    BranchDuty,
    Chair,
    Customer,
    Equipment,
    Feedback,
    FeedbackAnswer,
    FeedbackQuestion,
    InventoryItem,
    Invoice,
    InvoiceSettings,
    SalesInvoice,
    SalesReportUpload,
    OperationalTask,
    Profile,
    Service,
    ServiceDetail,
    SOPTask,
    SubService,
    TaskEquipment,
    TaskInventory,
    Visit,
    VisitService,
    VisitTask,
)


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("branch_code", "store_code", "name", "active")
    search_fields = ("code", "store_code", "name")

    @admin.display(description="Branch code", ordering="code")
    def branch_code(self, obj):
        return obj.code

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "code" in form.base_fields:
            form.base_fields["code"].label = "Branch code"
        return form


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "branch", "employee_code", "active")
    list_filter = ("role", "branch", "active")

    def get_exclude(self, request, obj=None):
        excluded = list(super().get_exclude(request, obj) or ())
        profile = getattr(request.user, 'profile', None)
        can_view_salary = request.user.is_superuser or (
            profile and profile.active and profile.role in ['ADMIN', 'GENERAL_MANAGER']
        )
        if not can_view_salary:
            excluded.append('salary')
        return excluded


@admin.register(BranchDuty)
class BranchDutyAdmin(admin.ModelAdmin):
    list_display = ("date", "user", "status", "branch", "updated_by", "updated_at")
    list_filter = ("date", "status", "branch")
    search_fields = ("user__username", "user__first_name")
    readonly_fields = ("updated_by",)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


class ServiceDetailInline(admin.TabularInline):
    model = ServiceDetail
    extra = 0


class SOPInline(admin.TabularInline):
    model = SOPTask
    extra = 0


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "customer_type", "standard_duration_minutes", "active")
    search_fields = ("code", "name")
    list_filter = ("active", "category", "customer_type")
    inlines = (ServiceDetailInline, SOPInline)


class OperationalTaskInline(admin.TabularInline):
    model = OperationalTask
    extra = 0


@admin.register(SubService)
class SubServiceAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "active")
    search_fields = ("code", "name")
    inlines = (OperationalTaskInline,)


@admin.register(OperationalTask)
class OperationalTaskAdmin(admin.ModelAdmin):
    list_display = ("external_id", "name", "sub_service", "active_labour_minutes", "passive_time_minutes", "active")
    list_filter = ("active", "sub_service")
    search_fields = ("external_id", "name")


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "base_unit", "active")
    search_fields = ("code", "name")


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "utility_type", "active")
    search_fields = ("code", "name")


@admin.register(TaskInventory)
class TaskInventoryAdmin(admin.ModelAdmin):
    list_display = ("external_id", "task", "service", "inventory", "effective_quantity", "unit", "active")
    list_filter = ("service", "active")


@admin.register(TaskEquipment)
class TaskEquipmentAdmin(admin.ModelAdmin):
    list_display = ("external_id", "task", "equipment", "equipment_usage_minutes", "utility_minutes", "active")
    list_filter = ("active", "utility_type")


@admin.register(SOPTask)
class SOPTaskAdmin(admin.ModelAdmin):
    list_display = ("service", "sequence", "phase", "task_type", "title", "required", "can_skip", "active")
    list_filter = ("service", "phase", "task_type", "active")


@admin.register(Chair)
class ChairAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "branch", "active")
    list_filter = ("branch", "active")
    search_fields = ("code", "name", "branch__name")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "mobile", "email", "created_at")
    search_fields = ("name", "mobile", "email")


class CreatedUntilFilter(admin.SimpleListFilter):
    """Visits created on or before a chosen date (in the salon's time zone)."""

    title = "created on or before"
    parameter_name = "created_until"
    template = "admin/created_until_filter.html"

    def lookups(self, request, model_admin):
        return ()

    def has_output(self):
        return True

    def chosen_date(self):
        try:
            return datetime.date.fromisoformat(self.value() or "")
        except ValueError:
            return None

    def queryset(self, request, queryset):
        chosen = self.chosen_date()
        if chosen:
            return queryset.filter(created_at__date__lte=chosen)
        return queryset

    def choices(self, changelist):
        chosen = self.chosen_date()
        other_params = {
            key: value
            for key, value in changelist.get_filters_params().items()
            if key != self.parameter_name
        }
        yield {
            "value": chosen.isoformat() if chosen else "",
            "other_params": [(key, v) for key, values in other_params.items()
                             for v in (values if isinstance(values, list) else [values])],
            "clear_url": changelist.get_query_string(remove=[self.parameter_name]),
        }


def _visit_status_action(status, label):
    def action(modeladmin, request, queryset):
        to_change = list(queryset.exclude(status=status))
        now = timezone.now()
        for visit in to_change:
            old_label = visit.get_status_display()
            visit.status = status
            update_fields = ["status", "updated_at"]
            if status == "CLOSED" and not visit.closed_at:
                visit.closed_at = now
                update_fields.append("closed_at")
            visit.save(update_fields=update_fields)
            modeladmin.log_change(request, visit, f"Bulk status change: {old_label} to {label}.")
        changed = len(to_change)
        unchanged = queryset.count() - changed
        message = f"{changed} visit(s) changed to {label}."
        if unchanged > 0:
            message += f" {unchanged} were already {label}."
        modeladmin.message_user(request, message, messages.SUCCESS)

    action.__name__ = f"mark_visits_{status.lower()}"
    action.short_description = f"Change status of selected visits to {label}"
    action.allowed_permissions = ("change",)
    return action


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "branch", "token_number", "status", "created_at")
    list_filter = ("branch", "status", CreatedUntilFilter, "created_at")
    search_fields = ("=id", "customer__name", "customer__mobile", "token_number")
    list_select_related = ("customer", "branch")
    actions = [_visit_status_action(code, label) for code, label in Visit.STATUS]


@admin.register(VisitService)
class VisitServiceAdmin(admin.ModelAdmin):
    list_display = ("visit_customer", "service", "order_number", "employee", "chair", "status", "replaces")
    list_filter = ("visit__branch", "service", "status")
    search_fields = ("visit__customer__name", "visit__customer__mobile", "service__name", "service__code")
    list_select_related = ("visit__customer", "visit__branch", "service", "employee", "chair")

    @admin.display(description="Visit / customer", ordering="visit__customer__name")
    def visit_customer(self, obj):
        return f"Visit #{obj.visit_id} - {obj.visit.customer.name}"


@admin.register(VisitTask)
class VisitTaskAdmin(admin.ModelAdmin):
    list_display = ("visit_customer", "title", "sub_service_name", "sequence", "status", "performed_by")
    list_filter = ("visit_service__visit__branch", "status", "task_type", "phase")
    search_fields = ("visit_service__visit__customer__name", "visit_service__visit__customer__mobile", "title", "sub_service_name")
    list_select_related = ("visit_service__visit__customer", "performed_by")

    @admin.display(description="Visit / customer", ordering="visit_service__visit__customer__name")
    def visit_customer(self, obj):
        return f"Visit #{obj.visit_service.visit_id} - {obj.visit_service.visit.customer.name}"


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "visit_customer", "amount", "status", "completed_at")
    list_filter = ("visit__branch", "status", "payment_method", "created_at")
    search_fields = ("invoice_number", "visit__customer__name", "visit__customer__mobile")
    list_select_related = ("visit__customer", "visit__branch", "entered_by")

    @admin.display(description="Visit / customer", ordering="visit__customer__name")
    def visit_customer(self, obj):
        return f"Visit #{obj.visit_id} - {obj.visit.customer.name}"


@admin.register(FeedbackQuestion)
class FeedbackQuestionAdmin(admin.ModelAdmin):
    list_display = ("sequence", "text", "active")
    list_filter = ("active",)
    search_fields = ("text",)


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("id", "visit_customer", "submitted_at", "suggestion")
    list_filter = ("visit__branch", "submitted_at")
    search_fields = ("visit__customer__name", "visit__customer__mobile", "suggestion")
    list_select_related = ("visit__customer", "visit__branch")

    @admin.display(description="Visit / customer", ordering="visit__customer__name")
    def visit_customer(self, obj):
        return f"Visit #{obj.visit_id} - {obj.visit.customer.name}"


@admin.register(FeedbackAnswer)
class FeedbackAnswerAdmin(admin.ModelAdmin):
    list_display = ("visit_customer", "question_summary", "rating")
    list_filter = ("rating", "feedback__visit__branch", "question")
    search_fields = ("feedback__visit__customer__name", "feedback__visit__customer__mobile", "question__text")
    list_select_related = ("feedback__visit__customer", "feedback__visit__branch", "question")

    @admin.display(description="Visit / customer", ordering="feedback__visit__customer__name")
    def visit_customer(self, obj):
        return f"Visit #{obj.feedback.visit_id} - {obj.feedback.visit.customer.name}"

    @admin.display(description="Question", ordering="question__sequence")
    def question_summary(self, obj):
        return obj.question.text.splitlines()[0]


admin.site.site_header = "HAIR SHIP Administration"
admin.site.site_title = "HAIR SHIP Admin"
admin.site.index_title = "HAIR SHIP operations"

from .models import GoogleReview, GoogleReviewEvent, GoogleReviewLocation, GoogleReplyPublication, GoogleReviewAction


@admin.register(GoogleReviewLocation)
class GoogleReviewLocationAdmin(admin.ModelAdmin):
    list_display = ('environment', 'location_name', 'branch', 'active')
    list_filter = ('environment', 'branch', 'active')
    search_fields = ('location_name', 'branch__name')


class ReceivedGoogleDataAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GoogleReview)
class GoogleReviewAdmin(ReceivedGoogleDataAdmin):
    list_display = ('review_name', 'environment', 'branch', 'star_rating', 'sentiment', 'publication_status')
    list_filter = ('environment', 'branch', 'sentiment', 'publication_status')
    search_fields = ('review_name', 'reviewer_display_name')


@admin.register(GoogleReviewEvent)
class GoogleReviewEventAdmin(ReceivedGoogleDataAdmin):
    list_display = ('event_id', 'environment', 'kind', 'status', 'received_at')
    list_filter = ('environment', 'kind', 'status')
    search_fields = ('event_id', 'review_name')


admin.site.register(GoogleReplyPublication, ReceivedGoogleDataAdmin)
admin.site.register(GoogleReviewAction, ReceivedGoogleDataAdmin)


@admin.register(InvoiceSettings)
class InvoiceSettingsAdmin(admin.ModelAdmin):
    list_display = ("invoice_prefix", "amount_tolerance", "updated_by", "updated_at")
    readonly_fields = ("updated_by", "updated_at")

    def has_add_permission(self, request):
        return not InvoiceSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SalesReportUpload)
class SalesReportUploadAdmin(admin.ModelAdmin):
    list_display = ("file_name", "branch", "first_date", "last_date", "invoice_count", "matched_count",
                    "not_in_app_count", "amount_diff_count", "format_count", "uploaded_by", "uploaded_at")
    list_filter = ("branch", "kind")
    readonly_fields = [f.name for f in SalesReportUpload._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "branch", "sale_date", "customer_name", "gross_total", "match_status", "app_amount")
    list_filter = ("branch", "match_status", "sale_date")
    search_fields = ("invoice_number", "customer_name", "phone")
    list_select_related = ("branch",)
    readonly_fields = [f.name for f in SalesInvoice._meta.fields]

    def has_add_permission(self, request):
        return False


from django import forms as _forms
from .models import ReviewTarget


class ReviewTargetForm(_forms.ModelForm):
    weekend_days = _forms.TypedMultipleChoiceField(choices=ReviewTarget.WEEKDAYS, coerce=int,
        widget=_forms.CheckboxSelectMultiple, initial=[5, 6], help_text='Which days count as the weekend.')

    class Meta:
        model = ReviewTarget
        fields = ['branch', 'month', 'target', 'weekday_weight', 'weekend_weight', 'weekend_days']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['weekend_days'] = sorted(self.instance.weekend_set)

    def clean_weekend_days(self):
        return ','.join(str(d) for d in sorted(self.cleaned_data['weekend_days']))

    def clean(self):
        data = super().clean()
        if data.get('weekday_weight') == 0 and data.get('weekend_weight') == 0:
            raise _forms.ValidationError('At least one of the weights must be above 0.')
        return data


@admin.register(ReviewTarget)
class ReviewTargetAdmin(admin.ModelAdmin):
    form = ReviewTargetForm
    list_display = ('branch', 'month_label', 'target', 'weekday_daily', 'weekend_daily')
    list_filter = ('month', 'branch')
    search_fields = ('branch__name',)
    readonly_fields = ('daily_split',)

    @admin.display(description='Month', ordering='month')
    def month_label(self, obj):
        return obj.month.strftime('%b %Y')

    def _share(self, obj, weekend):
        days = [d for d in obj.days() if (d.weekday() in obj.weekend_set) == weekend]
        return round(obj.daily_targets()[days[0]], 1) if days else '-'

    @admin.display(description='Per weekday')
    def weekday_daily(self, obj):
        return self._share(obj, False)

    @admin.display(description='Per weekend day')
    def weekend_daily(self, obj):
        return self._share(obj, True)

    @admin.display(description='Daily split')
    def daily_split(self, obj):
        if not obj.pk:
            return 'Save to see the target for each day.'
        from django.utils.html import format_html_join
        return format_html_join(', ', '{} {}: {}', ((d.strftime('%a'), d.day, round(v, 1))
            for d, v in obj.daily_targets().items()))
