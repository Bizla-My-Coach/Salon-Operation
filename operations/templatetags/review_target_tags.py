from django import template

from operations.review_targets import month_targets

register = template.Library()


@register.inclusion_tag('includes/review_target_bars.html')
def review_target_bars(branch=None, all_branches=False):
    """Bars for this month's Google review targets: one branch, or every branch."""
    if branch is None and not all_branches:
        return {'rows': []}
    rows = month_targets(None if all_branches else branch)
    return {'rows': rows, 'single': not all_branches}
