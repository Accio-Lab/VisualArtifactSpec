"""Strict result validation and explicit, reproducible score aggregation."""
from collections import defaultdict
import math
from .aggregation import aggregate as aggregate_unified, quality_points


def normalize_judgments(judgments):
    """Accept the original ID map and the data_v2 results envelope, without coercion."""
    if isinstance(judgments, dict) and "results" in judgments:
        if set(judgments) != {"results"} or not isinstance(judgments["results"], list):
            raise ValueError("Expected exactly a results list in the evaluation envelope")
        mapped = {}
        for row in judgments["results"]:
            if not isinstance(row, dict) or set(row) != {"id", "answer", "reason"}:
                raise ValueError("Each results entry must contain exactly id, answer and reason")
            key = row["id"]
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Rubric ID must be a nonempty string")
            if key in mapped:
                raise ValueError(f"Duplicate rubric ID: {key}")
            mapped[key] = {"answer": row["answer"], "reason": row["reason"]}
        return mapped
    return judgments


def score(items, judgments):
    judgments = normalize_judgments(judgments)
    expected = {item["id"] for item in items}
    if not isinstance(judgments, dict) or set(judgments) != expected:
        present = set(judgments) if isinstance(judgments, dict) else set()
        raise ValueError(f"Rubric coverage mismatch: missing={sorted(expected-present)}, extra={sorted(present-expected)}")
    dimensions = defaultdict(list)
    results = {}
    for item in items:
        key = item["id"]
        answer = judgments[key]
        if not isinstance(answer, dict) or set(answer) != {"answer", "reason"}:
            raise ValueError(f"{key}: expected exactly answer and reason")
        value = answer["answer"]
        if not isinstance(value, str) or value not in item["answers"]:
            raise ValueError(f"{key}: answer must be one of {list(item['answers'])}")
        if not isinstance(answer["reason"], str) or not answer["reason"].strip():
            raise ValueError(f"{key}: evidence/reason is required")
        binary = set(item["answers"]) == {"Yes", "No"}
        raw = int(value == "Yes") if binary else int(value)
        normalized = raw * 100 if binary else quality_points(raw) + 50
        results[key] = {**answer, "dimension": item["dimension"], "raw": raw,
                        "normalized_100": normalized}
        if not binary:
            results[key]['quality_points'] = quality_points(raw)
        weight = item.get('weight', 1)
        if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not math.isfinite(weight) or weight <= 0):
            raise ValueError(f'{key}: item weight must be finite and positive')
        dimensions[item["dimension"]].append((raw, normalized, binary, weight))
    aggregate = {}
    for dimension, values in dimensions.items():
        if len({v[2] for v in values}) != 1:
            raise ValueError(f"Mixed scales within dimension: {dimension}")
        binary = values[0][2]
        total_weight = sum(v[3] for v in values)
        aggregate[dimension] = {"count": len(values), "scale": "0/1" if binary else "-3..3",
                                "weight_sum": total_weight,
                                "aggregation": "equal" if binary and all(v[3] == 1 for v in values) else "rubric_weighted",
                                "raw_mean": sum(v[0] * v[3] for v in values) / total_weight,
                                "normalized_100": sum(v[1] * v[3] for v in values) / total_weight}
        if binary:
            aggregate[dimension]["passed"] = sum(v[0] for v in values)
        else:
            aggregate[dimension]['quality_points_mean'] = aggregate[dimension]['normalized_100'] - 50
    value = {"schema_version": 2, "method": "binary pass-rate diagnostics; quality normalized_100 = mapped quality points + 50; unified contains the overall formula",
            "item_count": len(items), "dimensions": aggregate, "items": results}
    if any("benchmark_source" in i for i in items):
        value["unified"] = aggregate_unified(items, results)
    return value
