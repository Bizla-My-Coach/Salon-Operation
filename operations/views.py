from datetime import timedelta
from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import connection, IntegrityError, transaction
from django.db.models import Avg, Count, Exists, OuterRef, Prefetch, Q, Sum
from django.shortcuts import render,redirect,get_object_or_404
from django.http import JsonResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.cache import never_cache
from .models import *
from .forms import *
from .timing import add_timing_summary, adopt_legacy_active_segment, close_segment, start_segment
from .decorators import roles_required
from .roster import employee_services, working_branch
from .staff_access import resettable_employees
from .workflow import (
    CONSULTATION_CODE,
    OPENING_TASK_TYPES,
    SANITISATION_CODE,
    build_visit_tasks,
    rebuild_service_tasks,
    unfinished_verification_tasks,
)

def user_branch(user): return working_branch(user)


@never_cache
@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER', 'EMPLOYEE')
@require_POST
def save_theme_preference(request):
    theme = request.POST.get('theme')
    if theme not in dict(Profile.THEME_CHOICES):
        return JsonResponse({'error': 'Choose light or dark.'}, status=400)
    profiles = Profile.objects.filter(user=request.user)
    if request.POST.get('only_if_unset') == '1':
        # An older tab importing browser storage must not overwrite a saved choice.
        profiles = profiles.filter(theme='')
    profiles.update(theme=theme, updated_at=timezone.now())
    saved = Profile.objects.values_list('theme', flat=True).get(user=request.user)
    return JsonResponse({'theme': saved})


@sensitive_post_parameters()
@never_cache
@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER', 'EMPLOYEE')
@require_http_methods(['GET', 'POST'])
def my_profile(request):
    action = request.POST.get('action') if request.method == 'POST' else None
    details_form = SelfDetailsForm(
        request.POST if action == 'details' else None, instance=request.user,
    )
    password_form = SelfPasswordChangeForm(
        request.user, data=request.POST if action == 'password' else None,
    )
    if action == 'details' and details_form.is_valid():
        with transaction.atomic():
            details_form.save()
        messages.success(request, 'Your details have been updated.')
        return redirect('my_profile')
    if action == 'password' and password_form.is_valid():
        user = password_form.save()
        update_session_auth_hash(request, user)
        messages.success(request, 'Your password has been changed.')
        return redirect('my_profile')
    if request.method == 'POST' and action not in ['details', 'password']:
        messages.error(request, 'Choose details or password to update your profile.')
    return render(request, 'operations/profile.html', {
        'details_form': details_form, 'password_form': password_form,
    })


@sensitive_post_parameters()
@never_cache
@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER', 'EMPLOYEE')
@require_http_methods(['GET', 'POST'])
def change_password(request):
    form = SelfPasswordChangeForm(
        request.user, data=request.POST if request.method == 'POST' else None,
    )
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, 'Your password has been changed.')
        return redirect('change_password')
    return render(request, 'operations/change_password.html', {'password_form': form})


@roles_required('MANAGER')
@require_GET
def manager_staff(request):
    branch = user_branch(request.user)
    return _staff_directory(request, branch=branch)


@roles_required('ADMIN', 'GENERAL_MANAGER')
@never_cache
@require_GET
def general_manager_staff(request):
    return _staff_directory(request, business_wide=True)


def _staff_directory(request, branch=None, business_wide=False):
    today = timezone.localdate()
    duties = BranchDuty.objects.filter(date=today).select_related('branch')
    if business_wide:
        staff = User.objects.filter(profile__role__in=['EMPLOYEE', 'MANAGER', 'GENERAL_MANAGER'])
        open_filter = Q(assigned_services__status__in=['ASSIGNED', 'IN_PROGRESS', 'PAUSED'])
    else:
        staff = User.objects.filter(profile__role='EMPLOYEE').filter(
            Q(profile__branch=branch)
            | Q(pk__in=duties.filter(status='WORK', branch=branch).values('user_id'))
        ) if branch else User.objects.none()
        open_filter = Q(
            assigned_services__visit__branch=branch,
            assigned_services__status__in=['ASSIGNED', 'IN_PROGRESS', 'PAUSED'],
        )
    query = request.GET.get('q', '').strip()[:120]
    if query:
        staff = staff.filter(
            Q(username__icontains=query) | Q(first_name__icontains=query)
            | Q(last_name__icontains=query) | Q(profile__employee_code__icontains=query)
            | Q(profile__branch__name__icontains=query)
        )
    staff = staff.select_related('profile', 'profile__branch').prefetch_related(
        Prefetch('branch_duties', queryset=duties, to_attr='today_duties'),
    ).annotate(
        open_services=Count('assigned_services', filter=open_filter),
        can_reset_password=Exists(resettable_employees(request.user).filter(pk=OuterRef('pk'))),
    ).order_by('first_name', 'last_name', 'username')
    page_obj = Paginator(staff, 24).get_page(request.GET.get('page'))
    staff = list(page_obj.object_list)
    for person in staff:
        duty = person.today_duties[0] if person.today_duties else None
        person.current_branch = duty.branch if duty and duty.status == 'WORK' else (
            person.profile.branch if not duty and person.profile.role != 'GENERAL_MANAGER' else None
        )
        if not person.is_active or not person.profile.active:
            person.duty_label = 'Inactive'
        elif duty and duty.status == 'LEAVE':
            person.duty_label = 'On leave'
        elif not business_wide and duty and duty.branch_id != branch.pk:
            person.duty_label = 'Working at another branch'
        elif person.current_branch is None:
            person.duty_label = 'Not rostered today'
        else:
            person.duty_label = 'Working today'
    return render(request, 'operations/manager_staff.html', {
        'staff': staff, 'branch': branch, 'today': today,
        'business_wide': business_wide, 'page_obj': page_obj, 'query': query,
        'can_import_staff': business_wide and (
            request.user.is_superuser or request.user.profile.role in ['ADMIN', 'GENERAL_MANAGER']
        ),
    })


@roles_required('MANAGER', 'GENERAL_MANAGER')
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters()
def staff_password_reset(request, user_id):
    if request.user.profile.role == 'MANAGER' and not user_branch(request.user):
        raise PermissionDenied
    return_to = 'general_manager_staff' if request.user.profile.role == 'GENERAL_MANAGER' else 'manager_staff'
    with transaction.atomic():
        # A fresh scoped lookup on both GET and POST prevents URL/hidden-field tampering.
        employee = get_object_or_404(
            resettable_employees(request.user).select_for_update(of=('self',)).select_related('profile'),
            pk=user_id,
        )
        form = StaffPasswordResetForm(
            employee, request.POST if request.method == 'POST' else None, actor=request.user,
        )
        if request.method == 'POST' and form.is_valid():
            form.save(commit=False)
            employee.save(update_fields=['password'])
            LogEntry.objects.create(
                user=request.user, content_type=ContentType.objects.get_for_model(User),
                object_id=str(employee.pk), object_repr=employee.username[:200],
                action_flag=CHANGE, change_message='Employee password reset through staff management.',
            )
            messages.success(request, 'Password reset. Share the new password privately and ask the employee to change it in My profile.')
            return redirect(return_to)
    return render(request, 'operations/staff_password_reset.html', {
        'employee': employee, 'form': form, 'return_to': return_to,
    })

def health(request):
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return JsonResponse({'status':'ok','django':'connected','postgresql':'connected'})

def with_progress(queryset):
    return queryset.annotate(
        task_total=Count('tasks',distinct=True),
        task_done=Count('tasks',filter=Q(tasks__status__in=['COMPLETED','SKIPPED']),distinct=True),
    )
@login_required
def dashboard(request):
    p=request.user.profile
    if request.user.is_superuser or p.role=='ADMIN': return redirect('admin_dashboard')
    if p.role=='GENERAL_MANAGER': return redirect('general_manager_dashboard')
    if p.role=='MANAGER':
        return redirect('manager_dashboard') if user_branch(request.user) else render(request, 'operations/off_duty.html')
    return redirect('employee_dashboard')

def logout_view(request): logout(request); return redirect('login')

@roles_required('ADMIN')
def admin_dashboard(request):
    return render(request,'operations/admin_dashboard.html',{'branches':Branch.objects.count(),'users':User.objects.count(),'services':Service.objects.count(),'sop_tasks':OperationalTask.objects.count()})


@roles_required('GENERAL_MANAGER')
def general_manager_dashboard(request):
    return render(request, 'operations/general_manager_dashboard.html', {
        'acting_branch': user_branch(request.user),
        'branches': Branch.objects.filter(active=True).count(),
        'active_visits': Visit.objects.filter(status__in=['WAITING', 'ASSIGNED', 'IN_PROGRESS', 'EMPLOYEE_DONE']).count(),
        'services': Service.objects.filter(active=True).count(),
    })


@roles_required('ADMIN', 'GENERAL_MANAGER')
def admin_reports(request):
    """Business-wide operational reporting without exposing edit controls."""
    visits = Visit.objects.all()
    task_summary = VisitTask.objects.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='COMPLETED')),
        skipped=Count('id', filter=Q(status='SKIPPED')),
        labour_minutes=Sum(
            'active_labour_minutes',
            filter=~Q(task_type__in=OPENING_TASK_TYPES),
        ),
        passive_minutes=Sum(
            'passive_time_minutes',
            filter=~Q(task_type__in=OPENING_TASK_TYPES),
        ),
    )
    summary = {
        'visits': visits.count(),
        'active_visits': visits.filter(status__in=['WAITING', 'ASSIGNED', 'IN_PROGRESS', 'EMPLOYEE_DONE']).count(),
        'completed_visits': visits.filter(status__in=['VERIFIED', 'INVOICED', 'CLOSED']).count(),
        'customers': Customer.objects.count(),
        'invoices': Invoice.objects.count(),
        'revenue': Invoice.objects.aggregate(total=Sum('amount'))['total'] or 0,
        'feedback_requests': Feedback.objects.count(),
        'feedback_submitted': Feedback.objects.filter(submitted_at__isnull=False).count(),
        'feedback_average': FeedbackAnswer.objects.aggregate(value=Avg('rating'))['value'],
    }
    task_summary = {key: value or 0 for key, value in task_summary.items()}

    branch_rows = list(
        Branch.objects.annotate(
            visit_count=Count('visits', distinct=True),
            active_visit_count=Count(
                'visits',
                filter=Q(visits__status__in=['WAITING', 'ASSIGNED', 'IN_PROGRESS', 'EMPLOYEE_DONE']),
                distinct=True,
            ),
            invoice_count=Count('visits__invoice', distinct=True),
            revenue=Sum('visits__invoice__amount'),
        ).order_by('name')
    )
    branch_feedback = {
        row['feedback__visit__branch_id']: row['average']
        for row in FeedbackAnswer.objects.values('feedback__visit__branch_id').annotate(average=Avg('rating'))
    }
    for branch in branch_rows:
        branch.revenue = branch.revenue or 0
        branch.feedback_average = branch_feedback.get(branch.pk)

    service_rows = Service.objects.annotate(
        visit_count=Count('visitservice', distinct=True),
        completed_count=Count(
            'visitservice',
            filter=Q(visitservice__status__in=['EMPLOYEE_DONE', 'VERIFIED']),
            distinct=True,
        ),
        labour_minutes=Sum(
            'visitservice__tasks__active_labour_minutes',
            filter=~Q(visitservice__tasks__task_type__in=OPENING_TASK_TYPES),
        ),
        passive_minutes=Sum(
            'visitservice__tasks__passive_time_minutes',
            filter=~Q(visitservice__tasks__task_type__in=OPENING_TASK_TYPES),
        ),
    ).order_by('-visit_count', 'name')
    status_rows = visits.values('status').annotate(total=Count('id')).order_by('status')
    rating_rows = FeedbackAnswer.objects.values('rating').annotate(total=Count('id')).order_by('rating')
    recent_feedback = Feedback.objects.filter(submitted_at__isnull=False).select_related(
        'visit__customer', 'visit__branch'
    ).prefetch_related('answers').order_by('-submitted_at')[:10]
    for feedback in recent_feedback:
        ratings = [answer.rating for answer in feedback.answers.all()]
        feedback.average_rating = sum(ratings) / len(ratings) if ratings else None

    return render(request, 'operations/admin_reports.html', {
        'summary': summary,
        'task_summary': task_summary,
        'branch_rows': branch_rows,
        'service_rows': service_rows,
        'status_rows': status_rows,
        'rating_rows': rating_rows,
        'recent_feedback': recent_feedback,
    })


@roles_required('ADMIN', 'GENERAL_MANAGER')
def admin_visits(request):
    """Searchable, read-only visit cards for administrators."""
    query = request.GET.get('q', '').strip()
    branch_id = request.GET.get('branch', '').strip()
    service_id = request.GET.get('service', '').strip()
    status = request.GET.get('status', '').strip()
    service_queryset = with_progress(
        VisitService.objects.select_related('service', 'employee', 'chair').prefetch_related(
            Prefetch(
                'tasks',
                queryset=VisitTask.objects.only(
                    'visit_service_id',
                    'status',
                    'task_type',
                    'started_at',
                    'completed_at',
                ).prefetch_related('timing_segments'),
            )
        )
    ).order_by('order_number', 'id')
    visits = Visit.objects.select_related('branch', 'customer', 'invoice', 'feedback').prefetch_related(
        Prefetch('services', queryset=service_queryset)
    )
    if query:
        search = (
            Q(customer__name__icontains=query)
            | Q(customer__mobile__icontains=query)
            | Q(customer__email__icontains=query)
            | Q(token_number__icontains=query)
            | Q(services__service__name__icontains=query)
            | Q(services__service__code__icontains=query)
            | Q(services__employee__username__icontains=query)
            | Q(services__employee__first_name__icontains=query)
            | Q(invoice__invoice_number__icontains=query)
        )
        if query.isdigit():
            search |= Q(pk=int(query))
        visits = visits.filter(search)
    if branch_id.isdigit():
        visits = visits.filter(branch_id=int(branch_id))
    if service_id.isdigit():
        visits = visits.filter(services__service_id=int(service_id))
    valid_statuses = {choice[0] for choice in Visit.STATUS}
    if status in valid_statuses:
        visits = visits.filter(status=status)
    visits = visits.annotate(
        task_total=Count('services__tasks', distinct=True),
        task_done=Count(
            'services__tasks',
            filter=Q(services__tasks__status__in=['COMPLETED', 'SKIPPED']),
            distinct=True,
        ),
        feedback_average=Avg('feedback__answers__rating'),
    ).distinct().order_by('-created_at', '-id')
    page = Paginator(visits, 20).get_page(request.GET.get('page'))
    for visit in page.object_list:
        visit.admin_progress = int(visit.task_done * 100 / visit.task_total) if visit.task_total else 0
        visit_services = list(visit.services.all())
        add_timing_summary(visit, (
            task for item in visit_services if item.status != 'CANCELLED'
            for task in item.tasks.all()
        ))
    return render(request, 'operations/admin_visits.html', {
        'page': page,
        'branches': Branch.objects.order_by('name'),
        'service_options': Service.objects.order_by('name'),
        'status_options': Visit.STATUS,
        'filters': {'q': query, 'branch': branch_id, 'service': service_id, 'status': status},
    })

@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')
@require_GET
def visit_detail(request, visit_id):
    visits = Visit.objects.select_related('customer', 'branch', 'invoice', 'feedback')
    if not request.user.is_superuser and request.user.profile.role == 'MANAGER':
        visits = visits.filter(branch=user_branch(request.user))
    visit = get_object_or_404(visits.prefetch_related(Prefetch(
        'services', queryset=VisitService.objects.select_related('service', 'employee', 'chair')
        .prefetch_related('tasks__timing_segments').order_by('order_number', 'id'),
    )), pk=visit_id)
    services = list(visit.services.all())
    for item in services:
        add_timing_summary(item, item.tasks.all())
    add_timing_summary(visit, (t for item in services if item.status != 'CANCELLED' for t in item.tasks.all()))
    return render(request, 'operations/visit_detail.html', {'visit': visit, 'services': services})


@roles_required('ADMIN', 'GENERAL_MANAGER', 'MANAGER')
def service_catalog(request):
    form=ServiceLookupForm(request.GET or None)
    selected=None; sections=[]; totals={'labour':0,'passive':0,'equipment':0,'utility':0}
    if form.is_valid():
        selected=form.cleaned_data['service']
        mapped=list(selected.service_details.filter(active=True,sub_service__active=True).select_related('sub_service').order_by('sequence','id'))
        ordered=[]
        ordered.extend((detail.sub_service,detail.mandatory) for detail in mapped if detail.sub_service.code not in [CONSULTATION_CODE,SANITISATION_CODE])
        for sub_service,mandatory in ordered:
            task_rows=[]
            for task in sub_service.tasks.filter(active=True).order_by('sequence','id'):
                inventory=list(task.inventory_requirements.filter(active=True,service=selected).select_related('inventory'))
                equipment=list(task.equipment_requirements.filter(active=True).select_related('equipment'))
                totals['labour']+=task.active_labour_minutes; totals['passive']+=task.passive_time_minutes
                totals['equipment']+=sum(item.equipment_usage_minutes for item in equipment)
                totals['utility']+=sum(item.utility_minutes for item in equipment)
                task_rows.append({'task':task,'inventory':inventory,'equipment':equipment})
            sections.append({'sub_service':sub_service,'mandatory':mandatory,'tasks':task_rows})
    return render(request,'operations/service_catalog.html',{'form':form,'selected':selected,'sections':sections,'totals':totals})

@roles_required('ADMIN')
def create_user(request):
    form=UserCreateForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                u=User.objects.create_user(username=form.cleaned_data['username'],password=form.cleaned_data['password'],first_name=form.cleaned_data['first_name'])
                p=u.profile; p.role=form.cleaned_data['role']; p.branch=form.cleaned_data['branch']; p.employee_code=form.cleaned_data['employee_code']; p.job_title=form.cleaned_data['job_title']; p.save()
        except IntegrityError:
            if not User.objects.filter(username=form.cleaned_data['username']).exists():
                raise
            form.add_error('username', 'This username is already in use. Choose another.')
        else:
            messages.success(request,'User created.'); return redirect('create_user')
    return render(request,'operations/form.html',{'form':form,'title':'Add team member'})


@roles_required('ADMIN', 'GENERAL_MANAGER')
def branch_roster(request):
    today = timezone.localdate()
    form = BranchDutyForm(request.POST or None, initial={
        'start_date': today, 'end_date': today, 'status': 'WORK',
    })
    if request.method == 'POST' and form.is_valid():
        user = form.cleaned_data['user']
        branch = form.cleaned_data['branch']
        status = form.cleaned_data['status']
        first = form.cleaned_data['start_date']
        last = form.cleaned_data['end_date']
        with transaction.atomic():
            # Lock the person while validating and writing all daily rows.
            User.objects.select_for_update().get(pk=user.pk)
            if first == today and user.profile.role == 'EMPLOYEE' and (
                status == 'LEAVE' or user_branch(user) != branch
            ) and VisitService.objects.filter(
                employee=user, status__in=['ASSIGNED', 'IN_PROGRESS', 'PAUSED']
            ).exclude(visit__branch=branch).exists():
                form.add_error('user', 'Reassign this employee’s open services before moving them or recording leave today.')
            else:
                day = first
                while day <= last:
                    BranchDuty.objects.update_or_create(
                        user=user, date=day,
                        defaults={
                            'status': status,
                            'branch': branch if status == 'WORK' else None,
                            'updated_by': request.user,
                        },
                    )
                    day += timedelta(days=1)
        if not form.errors:
            messages.success(request, 'Branch roster updated.')
            return redirect('branch_roster')
    duties = BranchDuty.objects.filter(date__gte=today).select_related(
        'user__profile__branch', 'branch', 'updated_by'
    ).order_by('date', 'user__username')[:200]
    return render(request, 'operations/branch_roster.html', {
        'form': form, 'duties': duties, 'today': today,
    })

@roles_required('ADMIN')
@require_http_methods(['GET', 'POST'])
def csv_import(request):
    # Keep old bookmarks without bypassing the validated import workflows.
    if request.method == 'POST':
        return HttpResponseBadRequest('Direct CSV imports are no longer available. Open Import data and use validate then confirm.')
    return redirect('import_data')


@roles_required('MANAGER')
def manager_dashboard(request):
    branch=user_branch(request.user)
    # Prefetch in the visit's authoritative execution order.  Do not rely on
    # implicit model ordering after progress annotations/grouping because the
    # manager must see the same sequence used to build the employee task plan.
    services=(with_progress(
        VisitService.objects.select_related('service','employee','chair').prefetch_related('tasks__timing_segments')
    ).order_by('visit_id','order_number','id'))
    visits=(Visit.objects.filter(branch=branch).exclude(status__in=['CLOSED','CANCELLED'])
            .select_related('customer')
            .prefetch_related(Prefetch('services',queryset=services),'invoice')
            .order_by('created_at','id'))
    for visit in visits:
        active_services = [item for item in visit.services.all() if item.status != 'CANCELLED']
        visit.active_services = active_services
        visit.active_service_count = len(active_services)
        visit.cancelled_services = [item for item in visit.services.all() if item.status == 'CANCELLED']
        for item in active_services:
            item.execution_locked = item.status != 'ASSIGNED' or any(t.status != 'PENDING' for t in item.tasks.all())
            add_timing_summary(item, item.tasks.all())
        add_timing_summary(visit, (t for item in active_services for t in item.tasks.all()))
        visit.can_add_services = visit.status in ['WAITING', 'ASSIGNED', 'IN_PROGRESS', 'EMPLOYEE_DONE']
        visit.can_edit_assignments = visit.can_add_services and (
            not active_services or any(item.status == 'ASSIGNED' and not item.execution_locked for item in active_services)
        )
        visit.can_verify_all = bool(active_services) and all(
            item.status in ['EMPLOYEE_DONE', 'VERIFIED'] for item in active_services
        ) and any(item.status == 'EMPLOYEE_DONE' for item in active_services)
    return render(request,'operations/manager_dashboard.html',{'visits':visits})

@roles_required('MANAGER')
def new_visit(request):
    branch=user_branch(request.user); form=VisitCreateForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            mobile=form.cleaned_data['mobile']; customer=Customer.objects.filter(mobile=mobile).first() if mobile else None
            if customer:
                if customer.name != form.cleaned_data['customer_name']:
                    customer.name = form.cleaned_data['customer_name']
                    customer.save(update_fields=['name', 'updated_at'])
            else:
                customer=Customer.objects.create(name=form.cleaned_data['customer_name'],mobile=mobile)
            visit=Visit.objects.create(branch=branch,customer=customer,status='WAITING',created_by=request.user)
        messages.success(request,'Customer details saved. Now add and assign the services.')
        return redirect('edit_visit_services', visit_id=visit.pk)
    return render(request,'operations/visit_form.html',{'form':form,'title':'Create service visit'})

@roles_required('MANAGER')
@transaction.atomic
def edit_visit_services(request, visit_id):
    branch = user_branch(request.user)
    if request.method == 'POST':
        # Match employee task/cancellation lock order and re-read task history
        # before deciding which rows can still be edited.
        employee_ids = VisitService.objects.filter(visit_id=visit_id, visit__branch=branch).values('employee_id')
        list(User.objects.select_for_update().filter(pk__in=employee_ids).order_by('pk'))
        get_object_or_404(Visit.objects.select_for_update(), pk=visit_id, branch=branch)
    visit = get_object_or_404(Visit, pk=visit_id, branch=branch)
    if visit.status in ['VERIFIED', 'INVOICED', 'CLOSED', 'CANCELLED']:
        messages.error(request, "Assignments cannot be changed after visit verification.")
        return redirect("manager_dashboard")

    formset_class = (
        InitialVisitServiceFormSet
        if request.method == 'GET' and not visit.services.exists()
        else VisitServiceFormSet
    )
    formset = formset_class(
        request.POST or None,
        instance=visit,
        branch=branch,
        queryset=visit.services.exclude(status='CANCELLED').order_by('order_number', 'id'),
        prefix='services',
    )
    if request.method == "POST" and formset.is_valid():
        with transaction.atomic():
            changed_service_ids = set()
            for form in formset.forms:
                if not form.cleaned_data:
                    continue
                instance = form.instance
                if form.cleaned_data.get('DELETE'):
                    if instance.pk and not form.execution_locked:
                        instance.delete()
                    continue
                if not form.cleaned_data.get('service'):
                    continue
                is_new = not instance.pk
                service_changed = is_new or 'service' in form.changed_data
                item = form.save(commit=False)
                item.visit = visit
                item.assigned_by = request.user
                if is_new or 'employee' in form.changed_data:
                    item.assigned_at = timezone.now()
                item.save()
                if service_changed:
                    changed_service_ids.add(item.pk)
            active = visit.services.exclude(status='CANCELLED')
            has_started_history = VisitTask.objects.filter(visit_service__visit=visit).exclude(status='PENDING').exists()
            if not has_started_history:
                build_visit_tasks(visit)
            else:
                for item in active.filter(pk__in=changed_service_ids):
                    rebuild_service_tasks(item)
            visit.status = 'ASSIGNED' if not active.exclude(status='ASSIGNED').exists() else visit.status
            visit.save(update_fields=['status'])
        messages.success(request, "Upcoming service order and assignments updated.")
        return redirect("manager_dashboard")

    return render(
        request,
        "operations/service_assignments.html",
        {
            "formset": formset,
            "title": f"Assign services for {visit.customer.name}",
            "visit": visit,
        },
    )


@roles_required('MANAGER')
@transaction.atomic
def verify_service(request,pk):
    vs=get_object_or_404(VisitService.objects.select_related('visit__customer','service','employee','chair').prefetch_related('tasks__timing_segments'),pk=pk,visit__branch=user_branch(request.user))
    if request.method == 'POST':
        visit = Visit.objects.select_for_update().get(pk=vs.visit_id)
        vs = VisitService.objects.select_for_update().select_related('visit', 'service').get(pk=pk)
        vs.visit = visit
    if vs.visit.status in ['INVOICED', 'CLOSED', 'CANCELLED'] or vs.status not in ['EMPLOYEE_DONE', 'VERIFIED']:
        messages.error(request, 'Only submitted services in an open visit can be verified.')
        return redirect('manager_dashboard')
    add_timing_summary(vs, vs.tasks.prefetch_related('timing_segments'))
    form=VerifyForm(request.POST or None,initial={'manager_notes':vs.manager_notes})
    if request.method=='POST' and form.is_valid():
        if unfinished_verification_tasks(vs.tasks.all()).exists():
            messages.error(request,'Complete all tasks or record permitted skips before verification.'); return redirect('verify_service',pk=pk)
        vs.status='VERIFIED'; vs.manager_notes=form.cleaned_data['manager_notes']; vs.verified_at=timezone.now(); vs.verified_by=request.user; vs.save()
        if not vs.visit.services.exclude(status='CANCELLED').exclude(status='VERIFIED').exists(): vs.visit.status='VERIFIED'; vs.visit.save(update_fields=['status'])
        messages.success(request,'Service verified.'); return redirect('manager_dashboard')
    return render(request,'operations/verify.html',{'vs':vs,'form':form})


@roles_required('MANAGER')
def verify_visit(request, visit_id):
    visit = get_object_or_404(
        Visit.objects.select_related('customer').prefetch_related(
            Prefetch(
                'services',
                queryset=VisitService.objects.select_related('service', 'employee', 'chair')
                .prefetch_related('tasks__timing_segments').order_by('order_number', 'id'),
            )
        ),
        pk=visit_id,
        branch=user_branch(request.user),
    )
    if visit.status in ['INVOICED', 'CLOSED', 'CANCELLED']:
        messages.error(request, 'This visit can no longer be verified.')
        return redirect('manager_dashboard')
    active_services = [item for item in visit.services.all() if item.status != 'CANCELLED']
    for item in active_services:
        add_timing_summary(item, item.tasks.all())
    add_timing_summary(visit, (t for item in active_services for t in item.tasks.all()))
    if request.method == 'POST':
        with transaction.atomic():
            locked_visit = Visit.objects.select_for_update().get(pk=visit.pk)
            if locked_visit.status in ['INVOICED', 'CLOSED', 'CANCELLED']:
                messages.error(request, 'This visit can no longer be verified.')
                return redirect('manager_dashboard')
            services = list(
                locked_visit.services.select_for_update().exclude(status='CANCELLED').order_by('order_number', 'id')
            )
            unfinished = [item for item in services if item.status not in ['EMPLOYEE_DONE', 'VERIFIED']]
            incomplete_tasks = unfinished_verification_tasks(VisitTask.objects.filter(visit_service__in=services))
            if not services or unfinished or incomplete_tasks.exists():
                messages.error(request, 'Submit every active service and complete tasks or record permitted skips before Verify all.')
                return redirect('verify_visit', visit_id=visit.pk)
            now = timezone.now()
            for item in services:
                item.manager_notes = request.POST.get(f'manager_notes_{item.pk}', '').strip()
                item.status = 'VERIFIED'
                item.verified_at = item.verified_at or now
                item.verified_by = request.user
                item.save(update_fields=['manager_notes', 'status', 'verified_at', 'verified_by', 'updated_at'])
            locked_visit.status = 'VERIFIED'
            locked_visit.save(update_fields=['status'])
        messages.success(request, 'All services verified. The invoice can now be added.')
        return redirect('manager_dashboard')
    return render(request, 'operations/verify_visit.html', {'visit': visit, 'services': active_services})
@roles_required('MANAGER')
def cancel_and_reassign_service(request, pk):
    branch = user_branch(request.user)
    service = get_object_or_404(
        VisitService.objects.select_related('visit__customer', 'service'), pk=pk, visit__branch=branch
    )
    if service.status not in ['ASSIGNED', 'IN_PROGRESS', 'PAUSED']:
        messages.error(request, 'Only an upcoming or active service can be cancelled and reassigned.')
        return redirect('manager_dashboard')
    form = CancelAndReassignForm(
        request.POST or None,
        branch=branch,
        initial={'employee': service.employee_id, 'chair': service.chair_id},
    )
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            User.objects.select_for_update().get(pk=service.employee_id)
            locked_visit = Visit.objects.select_for_update().get(pk=service.visit_id)
            if locked_visit.status in ['VERIFIED', 'INVOICED', 'CLOSED', 'CANCELLED']:
                messages.error(request, 'This visit can no longer be reassigned.')
                return redirect('manager_dashboard')
            old = VisitService.objects.select_for_update().get(pk=service.pk)
            if old.status not in ['ASSIGNED', 'IN_PROGRESS', 'PAUSED']:
                messages.error(request, 'This service can no longer be reassigned.')
                return redirect('manager_dashboard')
            now = timezone.now()
            old.status = 'CANCELLED'
            old.cancelled_at = now
            old.cancelled_by = request.user
            old.cancellation_reason = form.cleaned_data['cancellation_reason']
            old.save(update_fields=['status', 'cancelled_at', 'cancelled_by', 'cancellation_reason', 'updated_at'])
            for old_task in old.tasks.filter(status__in=['PENDING', 'IN_PROGRESS', 'WAITING']):
                adopt_legacy_active_segment(old_task)
                close_segment(old_task, now)
                old_task.status = 'CANCELLED'
                old_task.completed_at = now
                old_task.save(update_fields=['status', 'completed_at', 'updated_at'])
            replacement = VisitService.objects.create(
                visit=old.visit,
                service=old.service,
                order_number=old.order_number,
                employee=form.cleaned_data['employee'],
                chair=form.cleaned_data['chair'],
                assigned_by=request.user,
                replaces=old,
            )
            completed_opening_types = set(VisitTask.objects.filter(
                visit_service__visit=old.visit,
                task_type__in=['HYGIENE', 'CONSULT'],
                status__in=['COMPLETED', 'SKIPPED'],
            ).values_list('task_type', flat=True))
            rebuild_service_tasks(
                replacement,
                include_sanitisation=not old.visit.services.exclude(status='CANCELLED').filter(order_number__lt=old.order_number).exists() and 'HYGIENE' not in completed_opening_types,
                include_consultation=not old.visit.services.exclude(status='CANCELLED').filter(order_number__lt=old.order_number).exists() and 'CONSULT' not in completed_opening_types,
            )
            old.visit.status = 'ASSIGNED'
            old.visit.save(update_fields=['status'])
        messages.success(request, 'The original service was cancelled and a replacement was assigned.')
        return redirect('manager_dashboard')
    return render(request, 'operations/form.html', {
        'form': form,
        'title': f'Cancel and reassign {service.service.name}',
        'form_note': 'The original task history will be preserved. A cancellation reason is required.',
    })

@roles_required('MANAGER')
def add_invoice(request,visit_id):
    visit=get_object_or_404(Visit,pk=visit_id,branch=user_branch(request.user))
    existing = Invoice.objects.filter(visit=visit).first()
    if existing and existing.status == 'COMPLETED':
        feedback, _ = Feedback.objects.get_or_create(visit=visit)
        messages.info(request, "This combined visit already has one completed invoice.")
        return redirect('manager_dashboard')
    if not visit.is_invoice_ready:
        messages.error(request,'Verify every service in the order before creating the invoice.')
        return redirect('manager_dashboard')
    form=CompleteInvoiceForm(request.POST or None, branch=visit.branch, invoice=existing, initial={
        'customer_name': visit.customer.name,
        'mobile': visit.customer.mobile,
        'invoice_number': existing.invoice_number if existing else '',
    })
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                locked = Visit.objects.select_for_update().get(pk=visit.pk)
                locked_existing = Invoice.objects.filter(visit=locked).first()
                if locked_existing and locked_existing.status == 'COMPLETED':
                    messages.info(request, 'This visit already has a completed invoice.')
                    return redirect('manager_dashboard')
                if not locked.is_invoice_ready:
                    messages.error(request, 'Verify every service before creating the invoice.')
                    return redirect('manager_dashboard')
                customer = locked.customer
                customer.name = form.cleaned_data['customer_name']
                customer.mobile = form.cleaned_data['mobile']
                customer.save(update_fields=['name', 'mobile', 'updated_at'])
                total=sum((item.service.base_price for item in locked.services.exclude(status='CANCELLED').select_related('service')),start=0)
                inv = locked_existing or Invoice(visit=locked, entered_by=request.user)
                inv.invoice_number = form.cleaned_data['invoice_number']
                inv.amount = total
                inv.status = 'COMPLETED'
                inv.completed_at = timezone.now()
                inv.save()
                locked.status='INVOICED'; locked.save(update_fields=['status'])
                Feedback.objects.get_or_create(visit=locked)
        except IntegrityError:
            # A different visit may claim the number after form validation.
            duplicates = Invoice.objects.filter(branch=visit.branch, invoice_number=form.cleaned_data['invoice_number'])
            if existing:
                duplicates = duplicates.exclude(pk=existing.pk)
            if not duplicates.exists():
                raise
            form.add_error('invoice_number', 'This invoice number is already in use in this branch.')
        else:
            messages.success(request, 'Invoice completed. Feedback can now be collected.')
            return redirect('manager_dashboard')
    total=sum((item.service.base_price for item in visit.services.exclude(status='CANCELLED').select_related('service')),start=0)
    return render(request,'operations/invoice_form.html',{'form':form,'visit':visit,'total':total})


def _save_feedback(request, feedback, questions):
    for question in questions:
        try:
            rating = int(request.POST.get(f'q_{question.id}', 0))
        except (TypeError, ValueError):
            rating = 0
        if rating not in range(1, 6):
            return False
        FeedbackAnswer.objects.update_or_create(
            feedback=feedback, question=question, defaults={'rating': rating}
        )
    feedback.suggestion=request.POST.get('suggestion','').strip()
    feedback.submitted_at=timezone.now()
    feedback.save(update_fields=['suggestion', 'submitted_at', 'updated_at'])
    feedback.visit.status='CLOSED'
    feedback.visit.closed_at=timezone.now()
    feedback.visit.save(update_fields=['status','closed_at'])
    return True


@roles_required('MANAGER')
def collect_feedback(request, visit_id):
    visit = get_object_or_404(Visit, pk=visit_id, branch=user_branch(request.user), status='INVOICED')
    feedback, _ = Feedback.objects.get_or_create(visit=visit)
    questions = FeedbackQuestion.objects.filter(active=True)
    if request.method == 'POST':
        if request.POST.get('action') == 'close_visit':
            visit.status = 'CLOSED'
            visit.closed_at = timezone.now()
            visit.save(update_fields=['status', 'closed_at', 'updated_at'])
            messages.success(request, 'Visit closed without collecting feedback.')
            return redirect('manager_dashboard')
        with transaction.atomic():
            if not _save_feedback(request, feedback, questions):
                messages.error(request, 'Please answer every feedback question.')
            else:
                messages.success(request, 'Feedback saved and the visit was closed.')
                return redirect('manager_dashboard')
    return render(request, 'operations/feedback.html', {
        'feedback': feedback, 'questions': questions, 'manager_collection': True
    })

@roles_required('EMPLOYEE')
def employee_dashboard(request):
    jobs=(with_progress(employee_services(request.user)).select_related('visit__customer','visit__branch','service','chair')
        .prefetch_related('visit__services')
        .order_by('visit__created_at','visit_id','order_number','id'))
    return render(request,'operations/employee_dashboard.html',{'jobs':jobs})


def _pending_opening_task(visit):
    return (
        VisitTask.objects.filter(
            visit_service__visit=visit,
            task_type__in=OPENING_TASK_TYPES,
        )
        .exclude(status__in=['COMPLETED', 'SKIPPED', 'CANCELLED'])
        .order_by('visit_service__order_number', 'sequence', 'id')
        .first()
    )


@roles_required('EMPLOYEE')
def execute_service(request,pk):
    vs=get_object_or_404(employee_services(request.user),pk=pk)
    current=vs.tasks.exclude(status__in=['COMPLETED','SKIPPED']).first()
    opening_tasks = vs.tasks.filter(task_type__in=OPENING_TASK_TYPES)
    opening_current = _pending_opening_task(vs.visit)
    service_tasks = vs.tasks.exclude(task_type__in=OPENING_TASK_TYPES).prefetch_related('timing_segments')
    return render(request,'operations/execute.html',{
        'vs':vs,
        'current':current,
        'opening_tasks':opening_tasks,
        'opening_current':opening_current,
        'service_tasks':service_tasks,
        'service_actions_locked':opening_current is not None,
    })

@roles_required('EMPLOYEE')
@require_POST
@transaction.atomic
def task_action(request,pk,action):
    # Serialize transitions from different tabs or phones to keep each task's
    # timing segments consistent while multiple tasks and services run together.
    User.objects.select_for_update().get(pk=request.user.pk)
    task=get_object_or_404(VisitTask.objects.select_for_update(),pk=pk,visit_service__in=employee_services(request.user)); vs=task.visit_service
    now=timezone.now(); note=request.POST.get('note','').strip()
    reason_choice=request.POST.get('skip_reason_choice','').strip()
    reason_other=request.POST.get('skip_reason_other','').strip()
    reason=reason_other if reason_choice=='OTHER' else reason_choice
    opening_current = _pending_opening_task(vs.visit)
    if action == 'confirm':
        if task.task_type not in OPENING_TASK_TYPES:
            messages.error(request, 'Only visit opening checks can be confirmed.')
            return redirect('execute_service', pk=vs.pk)
        if opening_current is None or task.pk != opening_current.pk:
            messages.error(request, 'Confirm the visit opening checks in order.')
            return redirect('execute_service', pk=vs.pk)
        task.status = 'COMPLETED'
        task.started_at = None
        task.completed_at = now
        task.note = note
        task.performed_by = request.user
        task.save(update_fields=[
            'status', 'started_at', 'completed_at', 'note', 'performed_by', 'updated_at'
        ])
        return redirect('execute_service', pk=vs.pk)
    if task.task_type in OPENING_TASK_TYPES:
        messages.error(request, 'Use Confirm for visit opening checks; they are not timed.')
        return redirect('execute_service', pk=vs.pk)
    if opening_current is not None:
        messages.error(request, 'Confirm sanitisation and consultation before starting service work.')
        return redirect('execute_service', pk=vs.pk)
    if action == 'start' and task.staff_instructions and vs.tasks.filter(sequence__lt=task.sequence).exclude(status__in=['COMPLETED', 'SKIPPED', 'CANCELLED']).exists():
        messages.error(request, 'Complete the earlier task group in this service first.')
        return redirect('execute_service', pk=vs.pk)
    if action=='start':
        if task.status!='PENDING': return redirect('execute_service',pk=vs.pk)
        task.status='IN_PROGRESS'; task.started_at=task.started_at or now
        start_segment(task, 'ACTIVE', now)
        if vs.status=='ASSIGNED': vs.status='IN_PROGRESS'; vs.started_at=vs.started_at or now; vs.visit.status='IN_PROGRESS'; vs.visit.save(update_fields=['status'])
    elif action == 'wait':
        if not task.supports_waiting or task.status != 'IN_PROGRESS':
            messages.error(request, 'Only an active processing-enabled task can enter waiting.')
            return redirect('execute_service', pk=vs.pk)
        adopt_legacy_active_segment(task)
        start_segment(task, 'WAITING', now)
        task.status = 'WAITING'
    elif action == 'resume':
        if not task.supports_waiting or task.status != 'WAITING':
            return redirect('execute_service', pk=vs.pk)
        start_segment(task, 'ACTIVE', now)
        task.status = 'IN_PROGRESS'
    elif action=='complete':
        if task.status!='IN_PROGRESS':
            messages.error(request,'Start the task before completing it.'); return redirect('execute_service',pk=vs.pk)
        adopt_legacy_active_segment(task)
        close_segment(task, now)
        task.status='COMPLETED'; task.started_at=task.started_at or now; task.completed_at=now
    elif action=='skip':
        if task.status != 'PENDING':
            messages.error(request, 'Only an unstarted task can be skipped.')
            return redirect('execute_service', pk=vs.pk)
        if not task.can_skip: messages.error(request,'This task cannot be skipped.'); return redirect('execute_service',pk=vs.pk)
        if task.skip_reason_required and not reason: messages.error(request,'Please give a short skip reason.'); return redirect('execute_service',pk=vs.pk)
        task.status='SKIPPED'; task.skip_reason=reason; task.completed_at=now
    else:
        messages.error(request, 'Unknown task action.')
        return redirect('execute_service', pk=vs.pk)
    task.note=note; task.performed_by=request.user; task.save(); vs.save()
    return redirect('execute_service',pk=vs.pk)

@roles_required('EMPLOYEE')
@require_POST
@transaction.atomic
def finish_service(request,pk):
    User.objects.select_for_update().get(pk=request.user.pk)
    vs=get_object_or_404(employee_services(request.user),pk=pk)
    # Different employees may finish services in the same visit together.
    vs.visit = Visit.objects.select_for_update().get(pk=vs.visit_id)
    if _pending_opening_task(vs.visit) is not None:
        messages.error(request, 'Confirm sanitisation and consultation before submitting service work.')
        return redirect('execute_service', pk=vs.pk)
    if unfinished_verification_tasks(vs.tasks.all()).exists(): messages.error(request,'Complete or record permitted skips for the remaining tasks first.')
    else:
        vs.status='EMPLOYEE_DONE'; vs.employee_completed_at=timezone.now(); vs.employee_notes=request.POST.get('employee_notes',''); vs.save()
        if not vs.visit.services.exclude(status__in=['EMPLOYEE_DONE','VERIFIED','CANCELLED']).exists():
            vs.visit.status='EMPLOYEE_DONE'; vs.visit.save(update_fields=['status'])
        messages.success(request,'Submitted to manager.')
    return redirect('employee_dashboard')

def feedback_form(request,token):
    fb=get_object_or_404(Feedback,public_token=token)
    questions=FeedbackQuestion.objects.filter(active=True)
    if request.method=='POST' and not fb.submitted_at:
        with transaction.atomic():
            if not _save_feedback(request, fb, questions):
                messages.error(request,'Please answer all five questions.')
                return render(request,'operations/feedback.html',{'feedback':fb,'questions':questions})
        return render(request,'operations/feedback_thanks.html')
    return render(request,'operations/feedback.html',{'feedback':fb,'questions':questions})
