"""Versioned VAS score aggregation (Apache-2.0)."""
import math

WEIGHTS = {'Content': .25, 'Organization': .15, 'Visuals': .50, 'Delivery': .10}

VERSION = 'unified-211-signed-50-50-group-all-pass-nonlinear-v3'
QUALITY_POINTS = {-3: -50, -2: -25, -1: -10, 0: 0, 1: 10, 2: 25, 3: 50}


def quality_points(rating):
    if type(rating) is not int or rating not in QUALITY_POINTS:
        raise ValueError('Quality ratings must be integers from -3 through 3')
    return QUALITY_POINTS[rating]

def aggregate(items, results):
    groups = {}
    dimension_groups = {}
    for item in items:
        if set(item['answers']) != {'Yes', 'No'}:
            continue
        dimension = item['dimension']
        name = {'Task Fulfillment / Compliance': 'CP', 'Compliance': 'CP',
                'Task Fulfillment / Functionality': 'FN', 'Functionality': 'FN'}.get(dimension)
        if name is None:
            # Read legacy rubrics whose dimension label was simply Acceptance.
            name = next((p for p in ('CP', 'FN') if item['id'].startswith(p)), dimension)
        weight = item.get('weight', 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
            raise ValueError('Binary item weight must be finite and positive: ' + item['id'])
        memberships = item.get('compliance_groups')
        if (not isinstance(memberships, list) or not memberships
                or any(not isinstance(g, str) or not g.strip() for g in memberships)
                or len(memberships) != len(set(memberships))):
            raise ValueError('Missing or invalid compliance_groups: ' + item['id'])
        raw = results[item['id']]['raw']
        if type(raw) is not int or raw not in (0, 1):
            raise ValueError('Unresolved binary judgment: ' + item['id'])
        for group in memberships:
            entry = groups.setdefault(group, {'ids': [], 'failed_ids': [], 'dimension': name})
            if entry['dimension'] != name:
                raise ValueError('Acceptance group crosses dimensions: ' + group)
            entry['ids'].append(item['id'])
            if raw == 0:
                entry['failed_ids'].append(item['id'])
            dimension_groups.setdefault(name, set()).add(group)
    if not groups:
        raise ValueError('Missing acceptance dimensions')
    for entry in groups.values():
        entry.update(passed=not entry['failed_ids'], max_points=50 / len(groups))
        entry['points'] = entry['max_points'] if entry['passed'] else 0
        entry['deduction'] = entry['max_points'] - entry['points']
    rates = {name: sum(groups[g]['passed'] for g in names) / len(names)
             for name, names in dimension_groups.items()}
    acceptance = 50 * sum(g['passed'] for g in groups.values()) / len(groups)
    contributions = {}
    for dim, weight in WEIGHTS.items():
        selected = [i for i in items if i['dimension']==dim]
        total = sum(i.get('weight',1) for i in selected)
        if total <= 0: raise ValueError('Missing quality dimension')
        contributions[dim] = weight * sum(
            quality_points(results[i['id']]['raw']) * i.get('weight', 1)
            for i in selected) / total
    quality = sum(contributions.values())
    return dict(version=VERSION, acceptance_policy='group_all_pass', acceptance_groups=groups,
                quality_policy='map_each_rating_then_weight',
                quality_rating_points={str(k): v for k, v in QUALITY_POINTS.items()},
                acceptance=acceptance, quality=quality, quality_contributions=contributions,
                acceptance_rates=rates,total=acceptance+quality,passed=acceptance+quality>=50)
