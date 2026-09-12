#!/usr/bin/env python3
"""Validates that the Prism capstone pack data is parseable and internally consistent."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def read_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
    return rows


def main():
    pricing = read_json(DATA_DIR / "model_pricing.json")
    seed = read_json(DATA_DIR / "seed_keys.json")
    config = read_json(DATA_DIR / "gateway_config.sample.json")
    requests = read_jsonl(DATA_DIR / "sample_requests.jsonl")
    routing_cases = read_jsonl(DATA_DIR / "routing_eval.jsonl")

    priced_models = {m for m in pricing if not m.startswith("_")}
    for model, price in pricing.items():
        if model.startswith("_"):
            continue
        for field in ("input_per_1m", "output_per_1m"):
            if not isinstance(price.get(field), (int, float)) or price[field] < 0:
                raise ValueError(f"Price for {model} has invalid {field}")

    provider_names = set()
    for provider in config["providers"]:
        for field in ("name", "base_url", "api_key"):
            if not provider.get(field):
                raise ValueError(f"Provider entry missing {field}: {provider}")
        if provider["name"] in provider_names:
            raise ValueError(f"Duplicate provider name: {provider['name']}")
        provider_names.add(provider["name"])

    aliases = config["model_aliases"]
    for alias, route in aliases.items():
        if "route_by_difficulty" in route:
            for tier, target in route["route_by_difficulty"].items():
                if target not in aliases or "primary" not in aliases.get(target, {}):
                    raise ValueError(f"Alias '{alias}' tier '{tier}' routes to unknown alias '{target}'")
            continue
        chain = [route["primary"]] + list(route.get("fallbacks", []))
        for model in chain:
            if model not in priced_models:
                raise ValueError(f"Alias '{alias}' references unpriced model '{model}'")
            provider = model.rsplit("-", 1)[0]
            if provider not in provider_names:
                raise ValueError(f"Alias '{alias}' model '{model}' has no registered provider '{provider}'")

    keys_seen = set()
    for tenant in seed["tenants"]:
        for field in ("team", "virtual_key", "monthly_budget_usd", "rate_limit", "model_allowlist", "semantic_cache"):
            if field not in tenant:
                raise ValueError(f"Tenant '{tenant.get('team')}' missing {field}")
        if tenant["virtual_key"] in keys_seen:
            raise ValueError(f"Duplicate virtual key: {tenant['virtual_key']}")
        keys_seen.add(tenant["virtual_key"])
        for allowed in tenant["model_allowlist"]:
            if allowed not in aliases and allowed not in priced_models:
                raise ValueError(f"Tenant '{tenant['team']}' allowlists unknown model/alias '{allowed}'")
        cache = tenant["semantic_cache"]
        if cache.get("enabled") and not 0 < cache.get("similarity_threshold", 0) <= 1:
            raise ValueError(f"Tenant '{tenant['team']}' has invalid similarity_threshold")

    ids_seen = set()
    for row in requests:
        for field in ("id", "note", "body"):
            if field not in row:
                raise ValueError(f"Sample request missing {field}: {row}")
        if row["id"] in ids_seen:
            raise ValueError(f"Duplicate sample request id: {row['id']}")
        ids_seen.add(row["id"])
        body = row["body"]
        if "model" not in body or not isinstance(body.get("messages"), list):
            raise ValueError(f"Sample request {row['id']} body is not a chat completion request")
        # negative cases are allowed to reference unknown models/empty messages
        if "negative case" not in row["note"]:
            if body["model"] not in aliases and body["model"] not in priced_models:
                raise ValueError(f"Sample request {row['id']} uses unknown model '{body['model']}'")
            if not body["messages"]:
                raise ValueError(f"Sample request {row['id']} has empty messages")

    route_ids = set()
    for case in routing_cases:
        for field in ("id", "expected_tier", "note", "prompt"):
            if not case.get(field):
                raise ValueError(f"Routing case missing {field}: {case}")
        if case["id"] in route_ids:
            raise ValueError(f"Duplicate routing case id: {case['id']}")
        route_ids.add(case["id"])
        if case["expected_tier"] not in aliases or case["expected_tier"] == "auto":
            raise ValueError(f"Routing case {case['id']} expects unknown tier '{case['expected_tier']}'")

    print("Pack OK:")
    print(f"  {len(priced_models)} priced models, {len(provider_names)} providers, {len(aliases)} aliases")
    print(f"  {len(seed['tenants'])} seed tenants, {len(requests)} sample requests, {len(routing_cases)} routing eval cases")


if __name__ == "__main__":
    main()
