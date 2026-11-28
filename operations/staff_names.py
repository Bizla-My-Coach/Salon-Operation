"""Short, unambiguous staff names for busy screens.

First name only; when two people share it, add the last name; when that is
still not enough (or there is no last name), add the number at the end of the
username, e.g. br002.shahin.2 -> "Shahin #2".
"""
import re


def _number(user):
    """The trailing ".2" style number of a username, or "" (br002.shahin.2 -> "2")."""
    match = re.search(r'\.(\d+)$', user.username)
    return match.group(1) if match else ''


def display_names(users):
    users = list(users)
    first = {u.pk: (u.first_name.strip() or u.username) for u in users}
    names = {}
    by_first = {}
    for u in users:
        by_first.setdefault(first[u.pk].lower(), []).append(u)
    for group in by_first.values():
        if len(group) == 1:
            names[group[0].pk] = first[group[0].pk]
            continue
        full = {u.pk: f"{first[u.pk]} {u.last_name.strip()}".strip() for u in group}
        counts = {}
        for value in full.values():
            counts[value.lower()] = counts.get(value.lower(), 0) + 1
        labels = {}
        for u in group:
            label = full[u.pk]
            if counts[label.lower()] > 1 and _number(u):
                label = f"{label} #{_number(u)}"
            labels[u.pk] = label
        # Anything still clashing falls back to the username, which is unique.
        seen = {}
        for label in labels.values():
            seen[label.lower()] = seen.get(label.lower(), 0) + 1
        for u in group:
            names[u.pk] = labels[u.pk] if seen[labels[u.pk].lower()] == 1 else u.username
    return names
