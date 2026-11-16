"""最新模型探活。

数据来源：CLIProxyAPI 管理 API 的 openai-compatibility 配置（每个公益站的 base-url、api-key、模型）。
只关心 DeepSeek / GLM / GPT / Claude / Gemini 五个系列；每个系列在所有站点中取最高版本号，
只对这些"最新版本"模型发一次最小的 chat/completions 请求。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


CPA_BASE_URL = os.getenv("CPA_BASE_URL", "").rstrip("/")
CPA_MANAGEMENT_KEY = os.getenv("CPA_MANAGEMENT_KEY", "")
PROBE_TIMEOUT = float(os.getenv("MODEL_PROBE_TIMEOUT_SECONDS", "40"))
PROBE_CONCURRENCY = int(os.getenv("MODEL_PROBE_CONCURRENCY", "4"))

FAMILIES: dict[str, str] = {
    "deepseek": "DeepSeek",
    "glm": "GLM",
    "gpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "other": "其他",
}
# 非对话模型：图像、语音、向量等，不参与"最新版本"比较。
NON_CHAT = re.compile(r"(image|imagen|embed|tts|audio|realtime|transcribe|whisper|dall-e|moderation|vision-preview|ocr|rerank|-img\b|-img-)", re.I)
DATE_SUFFIX = re.compile(r"[-_@]?(20\d{2}-?\d{2}-?\d{2})")


def family_of(model: str) -> str | None:
    name = model.lower().rsplit("/", 1)[-1]
    if NON_CHAT.search(name):
        return None
    if "deepseek" in name:
        return "deepseek"
    if "glm" in name:
        return "glm"
    if "claude" in name:
        return "claude"
    if "gemini" in name:
        return "gemini"
    if re.match(r"^(chat)?gpt-?\d", name):
        return "gpt"
    return None


def version_of(model: str) -> tuple[int, ...]:
    """从模型名提取版本号：gpt-6.1-sol → (6,1)；claude-sonnet-4-6 → (4,6)；deepseek-v4.1 → (4,1)。"""
    name = DATE_SUFFIX.sub("", model.lower().rsplit("/", 1)[-1])
    # 版本号必须紧跟在系列名之后（Claude 允许中间夹 opus/sonnet/haiku），
    # 避免把 deepseek-r1-0528-qwen3-8b 的 "8b"、glm-z1-9b 的 "9b" 这类参数量当成版本。
    match = re.search(
        r"(?:deepseek|glm|gpt|gemini|claude(?:-(?:opus|sonnet|haiku))?)[-_ ]?v?"
        r"(\d{1,2}(?:\.\d{1,2})?(?:-\d{1,2})?)(?![0-9.]|b\b|b-|k\b)",
        name,
    )
    if not match:
        return ()
    parts = re.split(r"[.\-]", match.group(1))
    return tuple(int(p) for p in parts[:2])


@dataclass
class Provider:
    name: str
    base_url: str
    api_keys: list[str]
    models: list[dict[str, str]] = field(default_factory=list)  # [{"name": upstream, "alias": exposed}]
    disabled: bool = False


def chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if re.search(r"/v\d+[a-z]*$", base):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def models_url(base_url: str) -> str:
    return chat_url(base_url).rsplit("/chat/completions", 1)[0] + "/models"


def cpa_configured() -> bool:
    return bool(CPA_BASE_URL and CPA_MANAGEMENT_KEY)


async def fetch_cpa_list() -> list[dict[str, Any]]:
    """读取 CPA 的 openai-compatibility 原始列表（v8 管理 API，失败时回退 v0）。"""
    if not cpa_configured():
        raise RuntimeError("未配置 CPA_BASE_URL / CPA_MANAGEMENT_KEY，无法读取公益站上游配置")
    headers = {"Authorization": f"Bearer {CPA_MANAGEMENT_KEY}"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{CPA_BASE_URL}/v8/management/config", headers=headers)
        if response.status_code < 400:
            return list(((response.json().get("api-keys") or {}).get("openai-compatibility")) or [])
        legacy = await client.get(f"{CPA_BASE_URL}/v0/management/openai-compatibility", headers=headers)
    if legacy.status_code >= 400:
        raise RuntimeError(f"读取 CPA 配置失败（HTTP {response.status_code}）：{response.text[:200]}")
    payload = legacy.json()
    items = payload.get("openai-compatibility", payload) if isinstance(payload, dict) else payload
    # v0 用 api-key-entries，统一成 v8 的 keys
    return [{**it, "keys": it.get("keys") or it.get("api-key-entries") or [{"api-key": k} for k in it.get("api-keys") or []]} for it in items or [] if isinstance(it, dict)]


def to_provider(item: dict[str, Any]) -> Provider | None:
    if not isinstance(item, dict) or not item.get("base-url"):
        return None
    keys = [str(e["api-key"]) for e in item.get("keys") or [] if isinstance(e, dict) and e.get("api-key")]
    models = [{"name": str(m["name"]), "alias": str(m.get("alias") or m["name"])} for m in item.get("models") or [] if isinstance(m, dict) and m.get("name")]
    return Provider(name=str(item.get("name") or item["base-url"]), base_url=str(item["base-url"]), api_keys=keys, models=models, disabled=bool(item.get("disabled")))


async def load_providers() -> list[Provider]:
    return [p for p in (to_provider(it) for it in await fetch_cpa_list()) if p]


async def upstream_models(client: httpx.AsyncClient, provider: Provider) -> tuple[list[str], str | None]:
    """返回站点 /v1/models 实际提供的模型；失败时返回 ([], 错误信息)。"""
    if not provider.api_keys:
        return [], "CPA 中没有配置 api-key"
    try:
        response = await client.get(models_url(provider.base_url), headers={"Authorization": f"Bearer {provider.api_keys[0]}"}, timeout=20)
    except Exception as exc:
        return [], f"站点不可达：{_exc_text(exc)}"[:300]
    if response.status_code >= 400:
        return [], f"/v1/models HTTP {response.status_code}：{_error_text(response)}"
    try:
        data = response.json().get("data") or []
        return [str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")], None
    except Exception:
        return [], "/v1/models 返回内容不是 JSON"


def _exc_text(exc: Exception) -> str:
    detail = str(exc).strip() or str(getattr(exc, "__cause__", "") or "").strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if isinstance(error, str):
            return error[:300]
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])[:300]
    except ValueError:
        pass
    text = response.text.strip()
    title = re.search(r"<title>\s*([^<]+?)\s*</title>", text, re.I)
    if title:
        return title.group(1)[:300]
    if text.startswith("<"):
        return "返回了 HTML 页面"
    return text[:300] or f"HTTP {response.status_code}"


def classify(status_code: int | None, message: str) -> str:
    text = message.lower()
    if status_code is not None and status_code < 400:
        return "ok"
    if status_code == 429 or "rate limit" in text or "too many" in text or "限流" in text or "频率" in text:
        return "rate_limited"
    if status_code in (401, 402, 403) or "quota" in text or "配额" in text or "balance" in text or "充值" in text or "余额" in text or "额度" in text or "invalid token" in text or "令牌" in text:
        return "auth"
    if "渠道" in text or "channel" in text or "model_not_found" in text or "does not exist" in text or (status_code in (400, 404) and ("model" in text or "模型" in text)):
        return "unavailable"
    return "error"


async def probe_one(client: httpx.AsyncClient, provider: Provider, model: str) -> dict[str, Any]:
    url = chat_url(provider.base_url)
    headers = {"Authorization": f"Bearer {provider.api_keys[0]}", "Content-Type": "application/json"}
    body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16, "stream": False}
    started = time.monotonic()
    status_code: int | None = None
    message = ""
    try:
        response = await client.post(url, headers=headers, json=body, timeout=PROBE_TIMEOUT)
        status_code = response.status_code
        if status_code == 400 and "max_tokens" in response.text and "max_completion_tokens" in response.text:
            body.pop("max_tokens")
            body["max_completion_tokens"] = 16
            response = await client.post(url, headers=headers, json=body, timeout=PROBE_TIMEOUT)
            status_code = response.status_code
        if status_code < 400:
            try:
                data = response.json()
                ok = isinstance(data, dict) and isinstance(data.get("choices"), list)
            except ValueError:
                ok = False
            if not ok:
                status_code, message = 502, "响应不是有效的 chat/completions 结果"
            else:
                message = "可用"
        else:
            message = _error_text(response)
    except httpx.TimeoutException:
        message = f"超时（>{PROBE_TIMEOUT:.0f}s）"
    except Exception as exc:
        message = _exc_text(exc)[:300]
    return {
        "status": classify(status_code, message) if status_code is not None else "error",
        "http_status": status_code,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "message": message[:300],
    }


def is_chat_model(model: str) -> bool:
    return not NON_CHAT.search(model.lower().rsplit("/", 1)[-1])


async def run_probe(probe_disabled: set[str] | None = None) -> dict[str, Any]:
    """完整探测一轮，返回 {latest, provider_errors, results}。

    探测范围：CPA 当前对外的全部对话模型 + 五个系列在上游新出现的最新版本模型。
    已停用的提供商只在 probe_disabled 中时才探测（用于判断由系统停用的站点是否恢复）。
    """
    providers = [p for p in await load_providers() if not p.disabled or p.name in (probe_disabled or set())]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        listings = await asyncio.gather(*(upstream_models(client, p) for p in providers))

        # 每个站点可用于探测的模型：优先站点真实返回的 /v1/models，取不到时退回 CPA 里配置的上游名
        candidates: list[tuple[Provider, str, str]] = []  # (provider, upstream model, alias)
        provider_errors: list[dict[str, str]] = []
        for provider, (listed, error) in zip(providers, listings):
            alias_by_name = {m["name"]: m["alias"] for m in provider.models}
            if error:
                provider_errors.append({"provider": provider.name, "base_url": provider.base_url, "message": error})
            names = listed + [m["name"] for m in provider.models]
            for name in dict.fromkeys(names):
                candidates.append((provider, name, alias_by_name.get(name, name)))

        latest: dict[str, tuple[int, ...]] = {}
        for _, name, _ in candidates:
            family = family_of(name)
            version = version_of(name)
            if family and version and version > latest.get(family, ()):
                latest[family] = version

        targets: list[tuple[Provider, str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for p, name, alias in candidates:
            exposed = any(m["name"] == name for m in p.models)
            family = family_of(name)
            is_latest = bool(family) and version_of(name) == latest.get(family)
            # 对外模型全部探测（图像/向量等非对话模型无法用 chat 请求判断，跳过）；上游里只额外探测最新版本
            if (exposed and is_chat_model(name)) or is_latest:
                if (p.name, name) not in seen:
                    seen.add((p.name, name))
                    targets.append((p, name, alias, family or "other"))
        # 站点不可达的直接记为失败，不再逐个模型发请求
        unreachable = {e["provider"] for e in provider_errors if e["message"].startswith("站点不可达")}
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def guarded(provider: Provider, name: str, alias: str, family: str) -> dict[str, Any]:
            if provider.name in unreachable or not provider.api_keys:
                error = next((e["message"] for e in provider_errors if e["provider"] == provider.name), "站点不可达")
                outcome = {"status": "error", "http_status": None, "latency_ms": None, "message": error}
            else:
                async with semaphore:
                    outcome = await probe_one(client, provider, name)
            return {"provider": provider.name, "base_url": provider.base_url, "model": name, "alias": alias,
                    "family": family, "version": ".".join(map(str, version_of(name))), **outcome}

        results = await asyncio.gather(*(guarded(*t) for t in targets))
    return {
        "latest": {f: ".".join(map(str, v)) for f, v in latest.items()},
        "provider_count": len(providers),
        "provider_errors": provider_errors,
        "results": list(results),
    }
