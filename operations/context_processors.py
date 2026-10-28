from .roster import working_branch
from .review_workflow import is_review_admin


def acting_branch(request):
    user = request.user
    profile = getattr(user, "profile", None) if user.is_authenticated else None
    context = {'review_admin': bool(user.is_authenticated and is_review_admin(user)),
        'can_manage_records': bool(user.is_authenticated and user.is_staff and (
        user.is_superuser or user.has_module_perms('operations')
    )), 'acting_branch': None}
    if profile and profile.role == "GENERAL_MANAGER":
        context['acting_branch'] = working_branch(user)
    match = getattr(request, 'resolver_match', None)
    view = match.url_name if match else ''
    fast = {
        'dashboard', 'manager_dashboard', 'general_manager_dashboard', 'admin_dashboard',
        'employee_dashboard', 'execute_service', 'admin_visits', 'visit_detail',
        'manager_staff', 'general_manager_staff', 'admin_staff',
        'review_inbox', 'review_detail', 'google_review_inbox', 'google_review_detail',
    }
    full = fast | {
        'admin_reports', 'service_catalog', 'review_reports', 'google_review_analytics',
        'google_review_assignments', 'import_data',
    }
    partial = {
        'branch_roster', 'my_profile', 'change_password', 'new_visit', 'edit_visit_services',
        'verify_service', 'verify_visit', 'add_invoice', 'cancel_and_reassign_service',
        'create_user', 'staff_password_reset', 'staff_csv_import', 'branch_csv_import',
        'workbook_import',
    }
    context.update({
        'live_enabled': bool(user.is_authenticated and view in full | partial),
        'live_interval': 5000 if view in fast else 30000,
        'live_full_page': view in full,
    })
    return context
