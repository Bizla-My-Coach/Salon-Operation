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
    return context
