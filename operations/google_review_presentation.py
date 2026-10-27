"""Display only explicit findings from the saved Lambda result."""


def findings(analysis, keys, text_key):
    if not isinstance(analysis, dict):
        return []
    result, seen = [], set()
    for key in keys:
        entries = analysis.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str) and text_key == 'reported_appreciation':
                entry = {text_key: entry}
            if not isinstance(entry, dict):
                continue
            text = entry.get(text_key)
            if not isinstance(text, str) or not text.strip():
                continue
            category = entry.get('category', '')
            category = category.strip() if isinstance(category, str) else ''
            identity = (category.casefold(), ' '.join(text.split()).casefold())
            if identity in seen:
                continue
            seen.add(identity)
            priority = entry.get('priority', '')
            priority = priority.upper() if isinstance(priority, str) else ''
            result.append({'text': text.strip(), 'category': category.replace('_', ' ').capitalize(),
                'priority': priority.title() if priority in ('LOW', 'NORMAL', 'HIGH', 'URGENT') else ''})
    return result


def review_findings(review):
    return {'review_issues': findings(review.analysis, ('issues', 'complaints'), 'reported_issue'),
        'review_appreciations': findings(review.analysis, ('appreciations',), 'reported_appreciation')}
