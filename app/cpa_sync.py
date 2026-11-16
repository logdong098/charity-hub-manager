"""Charity Hub ↔ CLIProxyAPI 同步。

1. ensure_sites_in_cpa：签到站还没接入 CPA 时，用站点 access token 在站点上创建
   charity-hub-cpa 令牌，验证可用后作为 openai-compatibility 提供商写入 CPA。
2. apply_probe_results：根据测活结果更新 CPA 对外模型
   - 最新系列模型探测可用、但 CPA 里没有 → 加入（alias 统一为小写、去掉厂商前缀）
   - 模型连续失败 >= 阈值 → 从 models 移除，并记住原条目，恢复后原样加回
   - 站点整体连续失败 >= 阈值 → disabled=true；恢复后只重新启用由本系统停用的站点
每次写入前把 CPA 原列表加密备份到数据库，可回滚。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from . import model_probe
from .crypto import decrypt, encrypt
from .db import connection, rows_dict
from .new_api import NewAPIAdapter, _jwt_user_id, _looks_like_cookie, _message, _token_value

FAIL_THRESHOLD = int(os.getenv("CPA_SYNC_FAIL_THRESHOLD", "3"))
TOKEN_NAME = "charity-hub-cpa"
# 一轮里失败的站点比例超过它，就认为是探测侧（网络等）出了问题，本轮不改 CPA
MAX_FAILING_RATIO = 0.8
adapter = NewAPIAdapter()
CONNECTED = "已接入 CPA"
REMOVED = "已从 CPA 中删除，不再自动接入"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def host_of(url: str) -> str:
    return (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()


def endpoint_key(url: str) -> str:
    """站点标识：host + 去掉 /v1 后的路径，用于判断签到站是否已在 CPA 中。"""
    parsed = urlparse(site_root(url if "://" in url else f"https://{url}"))
    return f"{(parsed.hostname or '').lower()}{parsed.path.rstrip('/').lower()}"


def site_root(url: str) -> str:
    return re.sub(r"/v1/?$", "", url.rstrip("/"))


def norm_alias(model: str) -> str:
    return model.rsplit("/", 1)[-1].lower()


def log(action: str, provider: str, detail: str) -> None:
    with connection() as conn:
        conn.execute("INSERT INTO cpa_sync_log(action, provider, detail, created_at) VALUES (?, ?, ?, ?)", (action, provider, detail[:500], now_iso()))


async def put_cpa_list(items: list[dict[str, Any]], reason: str) -> None:
    """备份当前列表后整体写回（与 CPA 管理面板相同的写法）。"""
    current = await model_probe.fetch_cpa_list()
    with connection() as conn:
        conn.execute("INSERT INTO cpa_backups(reason, payload, created_at) VALUES (?, ?, ?)", (reason[:200], encrypt(json.dumps(current, ensure_ascii=False)), now_iso()))
        conn.execute("DELETE FROM cpa_backups WHERE id NOT IN (SELECT id FROM cpa_backups ORDER BY id DESC LIMIT 30)")
    body = [{**it, "keys": [{k: v for k, v in e.items() if k not in ("auth_index", "auth-index")} for e in it.get("keys") or []]} for it in items]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.put(
            f"{model_probe.CPA_BASE_URL}/v8/management/config/api-keys/openai-compatibility",
            headers={"Authorization": f"Bearer {model_probe.CPA_MANAGEMENT_KEY}"}, json=body,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"写入 CPA 失败（HTTP {response.status_code}）：{response.text[:200]}")


# ---------------------------------------------------------------- 站点 → CPA

async def _site_request(base: str, path: str, credential: str, user_id: int | None, method: str = "GET", json_body: Any = None) -> tuple[int, Any]:
    kwargs: dict[str, Any] = {"cookies": credential} if _looks_like_cookie(credential) else {"credential": credential}
    status, payload, _ = await adapter._request(f"{base}{path}", method=method, user_id=user_id, json_body=json_body, **kwargs)
    return status, payload


def _token_items(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items", data.get("data", [])) if isinstance(data, dict) else data
    return [t for t in items or [] if isinstance(t, dict)]


def _usable_key(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("key") or value.get("token")
    if not isinstance(value, str) or not value.strip() or "*" in value:
        return None
    value = value.strip()
    return value if value.startswith("sk-") else f"sk-{value}"


async def obtain_site_key(site_url: str, credential: str, user_id: int | None) -> tuple[str | None, str]:
    """在站点上找到或创建 charity-hub-cpa 令牌，返回 (sk key, 说明)。"""
    base = site_root(site_url)
    uid = user_id or _jwt_user_id(_token_value(credential))
    status, payload = await _site_request(base, "/api/token/?p=0&size=100", credential, uid)
    if status >= 400 or not (isinstance(payload, dict) and payload.get("success")):
        return None, f"无法读取令牌列表：{_message(payload, f'HTTP {status}')}"
    token = next((t for t in _token_items(payload) if t.get("name") == TOKEN_NAME), None)
    if not token:
        body = {"name": TOKEN_NAME, "remain_quota": 0, "unlimited_quota": True, "expired_time": -1, "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": ""}
        status, created = await _site_request(base, "/api/token/", credential, uid, method="POST", json_body=body)
        if status >= 400 or not (isinstance(created, dict) and created.get("success")):
            return None, f"创建令牌失败：{_message(created, f'HTTP {status}')}"
        key = _usable_key(created.get("data"))
        if key:
            return key, "已创建令牌"
        status, payload = await _site_request(base, "/api/token/?p=0&size=100", credential, uid)
        token = next((t for t in _token_items(payload) if t.get("name") == TOKEN_NAME), None)
        if not token:
            return None, "令牌已创建但在列表中找不到"
    key = _usable_key(token.get("key"))
    if key:
        return key, "使用已有令牌"
    # 新版 New API 列表里的 key 是脱敏的，需要单独取完整 key
    for method, path in (("POST", f"/api/token/{token['id']}/key"), ("GET", f"/api/token/{token['id']}/key"), ("GET", f"/api/token/{token['id']}")):
        try:
            status, detail = await _site_request(base, path, credential, uid, method=method)
        except Exception:
            continue
        if status < 400 and isinstance(detail, dict) and detail.get("success"):
            key = _usable_key(detail.get("data"))
            if key:
                return key, "已取得令牌完整 key"
    return None, "站点只返回脱敏后的 key，无法自动获取"


async def ensure_sites_in_cpa() -> dict[str, Any]:
    items = await model_probe.fetch_cpa_list()
    cpa_hosts = {endpoint_key(str(it.get("base-url", ""))) for it in items}
    names = {str(it.get("name")) for it in items}
    with connection() as conn:
        sites = rows_dict(conn.execute(
            """SELECT s.id, s.name, s.url, s.cpa_key, s.cpa_status, a.credential, a.platform_user_id FROM sites s
               JOIN accounts a ON a.site_id = s.id AND a.status = 'active'
               WHERE s.status = 'active' GROUP BY s.id"""
        ).fetchall())
    added: list[str] = []
    for site in sites:
        if endpoint_key(site["url"]) in cpa_hosts:
            with connection() as conn:
                conn.execute("UPDATE sites SET cpa_status = ? WHERE id = ?", (CONNECTED, site["id"]))
            continue
        # 接入过、后来在 CPA 里被手动删掉的站点，尊重你的删除，不再自动加回
        if (site.get("cpa_status") or "") in (CONNECTED, REMOVED):
            if site.get("cpa_status") == CONNECTED:
                with connection() as conn:
                    conn.execute("UPDATE sites SET cpa_status = ? WHERE id = ?", (REMOVED, site["id"]))
                log("skip_site", site["name"], "已在 CPA 中被删除，不再自动接入")
            continue
        try:
            key = decrypt(site["cpa_key"]) if site.get("cpa_key") else None
            note = "使用已保存的 key"
            if not key:
                key, note = await obtain_site_key(site["url"], decrypt(site["credential"]), site["platform_user_id"])
            if key:
                base_url = site_root(site["url"]) + "/v1"
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    check = await client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
                if check.status_code >= 400:
                    key, note = None, f"令牌验证失败（HTTP {check.status_code}）"
        except Exception as exc:
            key, note = None, f"接入失败：{model_probe._exc_text(exc)}"
        with connection() as conn:
            conn.execute("UPDATE sites SET cpa_status = ?, cpa_key = COALESCE(?, cpa_key), cpa_synced_at = ? WHERE id = ?",
                         (note if key else f"未接入：{note}", encrypt(key) if key else None, now_iso(), site["id"]))
        if not key:
            continue
        name = site["name"] if site["name"] not in names else f"{site['name']}-{host_of(site['url'])}"
        items.append({"name": name, "base-url": site_root(site["url"]) + "/v1", "keys": [{"api-key": key}], "models": [], "disabled": False})
        names.add(name)
        cpa_hosts.add(endpoint_key(site["url"]))
        added.append(name)
    if added:
        await put_cpa_list(items, f"接入签到站：{', '.join(added)}")
        for name in added:
            log("add_provider", name, "签到站自动接入 CPA（模型将在测活通过后加入）")
    return {"added": added}


# ---------------------------------------------------------------- 测活 → CPA

def _provider_state() -> dict[str, dict[str, Any]]:
    with connection() as conn:
        return {r["provider"]: dict(r) for r in conn.execute("SELECT * FROM cpa_provider_state").fetchall()}


def _save_provider_state(name: str, failures: int, disabled_by_hub: bool, removed: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO cpa_provider_state(provider, consecutive_failures, disabled_by_hub, removed_models, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET consecutive_failures = excluded.consecutive_failures, disabled_by_hub = excluded.disabled_by_hub,
               removed_models = excluded.removed_models, updated_at = excluded.updated_at""",
            (name, failures, int(disabled_by_hub), json.dumps(removed, ensure_ascii=False), now_iso()),
        )


async def apply_probe_results(report: dict[str, Any]) -> dict[str, Any]:
    with connection() as conn:
        rows = rows_dict(conn.execute("SELECT provider, model, status, consecutive_failures FROM model_probe_results").fetchall())
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_provider.setdefault(row["provider"], []).append(row)
    provider_errors = {e["provider"] for e in report.get("provider_errors", [])}

    items = await model_probe.fetch_cpa_list()
    # 站点整体失败：/v1/models 拿不到，且没有任何模型探测通过
    failing = {it["name"] for it in items if it.get("name") in provider_errors and not any(r["status"] == "ok" for r in by_provider.get(it["name"], []))}
    if items and len(failing) / len(items) > MAX_FAILING_RATIO:
        log("skip", "*", f"{len(failing)}/{len(items)} 个站点同时失败，疑似探测侧网络问题，本轮不修改 CPA")
        return {"changed": False, "skipped": True}

    state = _provider_state()
    actions: list[tuple[str, str, str]] = []
    for item in items:
        name = str(item.get("name"))
        st = state.get(name, {})
        failures = int(st.get("consecutive_failures") or 0)
        disabled_by_hub = bool(st.get("disabled_by_hub"))
        removed: dict[str, Any] = json.loads(st.get("removed_models") or "{}")

        failures = failures + 1 if name in failing else 0
        if failures >= FAIL_THRESHOLD and not item.get("disabled"):
            item["disabled"] = True
            disabled_by_hub = True
            actions.append(("disable_provider", name, f"连续 {failures} 次整体不可用，已停用"))
        elif failures == 0 and disabled_by_hub and item.get("disabled"):
            item["disabled"] = False
            disabled_by_hub = False
            actions.append(("enable_provider", name, "已恢复可用，重新启用"))
        elif not item.get("disabled"):
            disabled_by_hub = False

        # 站点整体失败时不动模型列表，停用/启用即可，恢复后模型原样可用；你手动停用的站点也不动
        manually_disabled = bool(item.get("disabled")) and not disabled_by_hub
        if name not in failing and not manually_disabled:
            models: list[dict[str, Any]] = list(item.get("models") or [])
            present = {str(m.get("name")) for m in models if isinstance(m, dict)}
            for row in by_provider.get(name, []):
                model = row["model"]
                if row["status"] == "ok" and model not in present:
                    entry = removed.pop(model, None) or ({"name": model, "alias": norm_alias(model)} if norm_alias(model) != model else {"name": model})
                    models.append(entry)
                    present.add(model)
                    actions.append(("add_model", name, f"{model} 探测可用，已加入对外模型" + (f"（alias {entry['alias']}）" if entry.get("alias") else "")))
                elif row["status"] != "ok" and int(row["consecutive_failures"] or 0) >= FAIL_THRESHOLD and model in present:
                    entry = next(m for m in models if m.get("name") == model)
                    models = [m for m in models if m.get("name") != model]
                    present.discard(model)
                    removed[model] = entry
                    actions.append(("remove_model", name, f"{model} 连续 {row['consecutive_failures']} 次不可用，已从对外模型移除"))
            item["models"] = models
        _save_provider_state(name, failures, disabled_by_hub, removed)

    if actions:
        await put_cpa_list(items, "测活后更新：" + "；".join(f"{a[1]} {a[0]}" for a in actions)[:180])
        for action in actions:
            log(*action)
    return {"changed": bool(actions), "actions": len(actions)}


async def rollback(backup_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT payload FROM cpa_backups WHERE id = ?", (backup_id,)).fetchone()
    if not row:
        raise ValueError("备份不存在")
    items = json.loads(decrypt(row["payload"]))
    await put_cpa_list(items, f"回滚到备份 #{backup_id}")
    with connection() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES('cpa_sync_enabled', 'false') ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        conn.execute("DELETE FROM cpa_provider_state")
    log("rollback", "*", f"已回滚到备份 #{backup_id}，并暂停自动同步")
    return {"ok": True, "providers": len(items)}
