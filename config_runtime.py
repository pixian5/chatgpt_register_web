from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable


def _deep_merge_dict(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge_dict(base[key], value)
        else:
            base[key] = value
    return base


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ[key] = value
    except Exception:
        pass


def load_dotenv_files(base_dir: str | Path) -> None:
    root = Path(base_dir)
    _load_dotenv_file(root / ".env")
    _load_dotenv_file(root / ".env.local")


def _load_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_runtime_config(defaults: dict, base_dir: str | Path) -> dict:
    root = Path(base_dir)
    load_dotenv_files(root)

    merged = copy.deepcopy(defaults)
    config_json = _load_json_if_exists(root / "config.json")
    local_json = _load_json_if_exists(root / "config.local.json")
    _deep_merge_dict(merged, config_json)
    _deep_merge_dict(merged, local_json)

    env = os.environ
    merged["duckmail_api_base"] = env.get("DUCKMAIL_API_BASE", merged.get("duckmail_api_base", ""))
    merged["duckmail_domain"] = env.get("DUCKMAIL_DOMAIN", merged.get("duckmail_domain", ""))
    merged["duckmail_bearer"] = env.get("DUCKMAIL_BEARER", merged.get("duckmail_bearer", ""))
    merged["proxy"] = env.get("PROXY", merged.get("proxy", ""))
    merged["workers"] = _parse_int(env.get("WORKERS"), int(merged.get("workers", 1)))
    merged["proxy_test_workers"] = _parse_int(
        env.get("PROXY_TEST_WORKERS"),
        int(merged.get("proxy_test_workers", 20)),
    )
    merged["total_accounts"] = _parse_int(
        env.get("TOTAL_ACCOUNTS"),
        int(merged.get("total_accounts", 3)),
    )
    merged["enable_oauth"] = _as_bool(env.get("ENABLE_OAUTH", merged.get("enable_oauth", True)))
    merged["oauth_required"] = _as_bool(env.get("OAUTH_REQUIRED", merged.get("oauth_required", True)))
    merged["oauth_issuer"] = env.get("OAUTH_ISSUER", merged.get("oauth_issuer", ""))
    merged["oauth_client_id"] = env.get("OAUTH_CLIENT_ID", merged.get("oauth_client_id", ""))
    merged["oauth_redirect_uri"] = env.get("OAUTH_REDIRECT_URI", merged.get("oauth_redirect_uri", ""))
    merged["output_file"] = env.get("OUTPUT_FILE", merged.get("output_file", ""))
    merged["ak_file"] = env.get("AK_FILE", merged.get("ak_file", ""))
    merged["rk_file"] = env.get("RK_FILE", merged.get("rk_file", ""))
    merged["token_json_dir"] = env.get("TOKEN_JSON_DIR", merged.get("token_json_dir", ""))

    pool = dict(merged.get("pool", {}) or {})
    pool["base_url"] = env.get("POOL_BASE_URL", pool.get("base_url", ""))
    pool["token"] = env.get("POOL_TOKEN", pool.get("token", ""))
    pool["target_type"] = env.get("POOL_TARGET_TYPE", pool.get("target_type", ""))
    pool["target_count"] = _parse_int(env.get("POOL_TARGET_COUNT"), int(pool.get("target_count", 0) or 0))
    pool["min_candidates"] = _parse_int(env.get("POOL_MIN_CANDIDATES"), int(pool.get("min_candidates", 100) or 100))
    pool["proxy"] = env.get("POOL_PROXY", pool.get("proxy", ""))
    pool["probe_workers"] = _parse_int(env.get("POOL_PROBE_WORKERS"), int(pool.get("probe_workers", 20) or 20))
    pool["delete_workers"] = _parse_int(env.get("POOL_DELETE_WORKERS"), int(pool.get("delete_workers", 10) or 10))
    local_pool = local_json.get("pool", {}) if isinstance(local_json.get("pool"), dict) else {}
    if "interval_min" in local_pool:
        pool["interval_min"] = _parse_int(local_pool.get("interval_min"), int(pool.get("interval_min", 1) or 1))
    else:
        pool["interval_min"] = _parse_int(env.get("POOL_INTERVAL_MIN"), int(pool.get("interval_min", 1) or 1))
    merged["pool"] = pool
    return merged


def save_runtime_config(config: dict, base_dir: str | Path) -> bool:
    root = Path(base_dir)
    path = root / "config.local.json"
    try:
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def get_secret_paths() -> Iterable[tuple[str, ...]]:
    return (
        ("duckmail_bearer",),
        ("pool", "token"),
    )


def mask_secret(value: Any) -> str:
    if not value:
        return ""
    return "********"


def mask_config_secrets(config: dict) -> dict:
    masked = copy.deepcopy(config)
    for path in get_secret_paths():
        target = masked
        for key in path[:-1]:
            next_value = target.get(key)
            if not isinstance(next_value, dict):
                target = None
                break
            target = next_value
        if isinstance(target, dict) and path[-1] in target:
            target[path[-1]] = mask_secret(target[path[-1]])
    return masked


def restore_masked_secrets(new_config: dict, current_config: dict) -> dict:
    merged = copy.deepcopy(new_config)
    for path in get_secret_paths():
        new_target = merged
        old_target = current_config
        for key in path[:-1]:
            if not isinstance(new_target.get(key), dict):
                new_target[key] = {}
            new_target = new_target[key]
            old_target = old_target.get(key, {}) if isinstance(old_target, dict) else {}

        new_value = new_target.get(path[-1])
        if new_value == "********":
            new_target[path[-1]] = old_target.get(path[-1], "")
    return merged
