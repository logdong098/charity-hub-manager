from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx


TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
COOKIE_NAMES = {"session", "token", "auth_token", "access_token", "jwt", "jwt_token", "connect.sid"}


@dataclass
class AdapterResult:
    success: bool
    message: str
    reward: str | None = None
    data: dict[str, Any] | None = None


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("站点地址不能为空")
    if not re.match(r"^https?://", value, re.I):
        value = f"https://{value}"
    return value.rstrip("/")


def _message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return payload[key].strip()
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"].strip()
    return fallback


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        text = response.text.strip()
        title = re.search(r"<title>\s*([^<]+?)\s*</title>", text, re.I)
        if title:
            return {"message": re.sub(r"\s*\|.*$", "", title.group(1)).strip()}
        return {"message": text[:240] or f"HTTP {response.status_code}"}


def _looks_like_cookie(value: str) -> bool:
    first = value.strip().split(";", 1)[0]
    name, separator, cookie_value = first.partition("=")
    return bool(separator and cookie_value and name.strip().lower() in COOKIE_NAMES)


def _credential_cookies(credential: str) -> list[str]:
    raw = credential.strip()
    if not raw:
        return []
    # Base64/JWT 类 Token 也可能以 '=' 结尾，只有形如 name=value 的内容
    # 才应被视为已经传入的 Cookie 头。
    if _looks_like_cookie(raw):
        return [raw]
    return [f"session={raw}", f"token={raw}"]


def _token_value(credential: str) -> str:
    raw = credential.strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def _jwt_user_id(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        data = json.loads(payload)
        candidate = data.get("id", data.get("sub"))
        value = int(candidate)
        return value if value > 0 else None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _user_headers(user_id: int | None) -> dict[str, str]:
    if not user_id:
        return {}
    value = str(user_id)
    return {
        "New-API-User": value,
        "Veloera-User": value,
        "voapi-user": value,
        "User-id": value,
        "X-User-Id": value,
        "Rix-Api-User": value,
        "neo-api-user": value,
    }


class NewAPIAdapter:
    """兼容 New API 及其常见 fork 的轻量适配器。

    认证策略沿用 metapi：先尝试 Bearer + 兼容用户头，再尝试 session/token Cookie，
    并在新签到接口不可用时回退到旧版 /api/user/sign_in。
    """

    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        credential: str | None = None,
        user_id: int | None = None,
        cookies: str | None = None,
        json_body: Any | None = None,
    ) -> tuple[int, Any, httpx.Headers]:
        headers = {"Accept": "application/json", "User-Agent": "CharityHub/1.0"}
        if credential and not cookies:
            headers["Authorization"] = f"Bearer {_token_value(credential)}"
            headers.update(_user_headers(user_id))
        if cookies:
            headers["Cookie"] = cookies
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers.update(_user_headers(user_id))
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.request(method, url, headers=headers, json=json_body)
        return response.status_code, _json_or_text(response), response.headers

    async def detect(self, url: str) -> AdapterResult:
        try:
            status, payload, _ = await self._request(f"{normalize_url(url)}/api/status")
            ok = status < 400 and isinstance(payload, dict) and payload.get("success") is True
            system_name = payload.get("data", {}).get("system_name") if isinstance(payload, dict) else None
            return AdapterResult(ok and isinstance(system_name, str), system_name or ("连接失败" if not ok else "未知站点"), data=payload if isinstance(payload, dict) else None)
        except Exception as exc:
            return AdapterResult(False, str(exc))

    async def login(self, url: str, username: str, password: str) -> AdapterResult:
        try:
            status, payload, headers = await self._request(
                f"{normalize_url(url)}/api/user/login",
                method="POST",
                json_body={"username": username, "password": password},
            )
            if status >= 400 or not isinstance(payload, dict) or payload.get("success") is not True:
                return AdapterResult(False, _message(payload, f"登录失败（HTTP {status}）"))
            data = payload.get("data")
            token = data if isinstance(data, str) else None
            if isinstance(data, dict):
                token = data.get("token") or data.get("access_token") or data.get("accessToken")
            token = token or payload.get("token")
            if isinstance(token, str) and token.strip():
                return AdapterResult(True, "登录成功", data={"credential": token.strip(), "credential_type": "token"})
            set_cookie = headers.get_list("set-cookie")
            pairs = [item.split(";", 1)[0].strip() for item in set_cookie if item.split(";", 1)[0].strip()]
            usable = [item for item in pairs if not item.lower().startswith(("acw_", "cdn_sec_"))]
            if usable:
                return AdapterResult(True, "登录成功（Cookie 会话）", data={"credential": "; ".join(usable), "credential_type": "cookie"})
            return AdapterResult(False, "登录成功但未获取到会话凭据")
        except Exception as exc:
            return AdapterResult(False, f"登录请求失败：{exc}")

    async def self_info(self, url: str, credential: str, user_id: int | None = None) -> AdapterResult:
        base = normalize_url(url)
        token = _token_value(credential)
        ids = [user_id, _jwt_user_id(token)]
        tried: set[tuple[str, int | None]] = set()
        for candidate in ids + [None]:
            if candidate is not None and candidate <= 0:
                continue
            cookie_header = _credential_cookies(credential) if _looks_like_cookie(credential) else [None]
            for cookie in cookie_header:
                marker = (cookie or "bearer", candidate)
                if marker in tried:
                    continue
                tried.add(marker)
                try:
                    status, payload, _ = await self._request(
                        f"{base}/api/user/self", credential=credential if cookie is None else None,
                        user_id=candidate, cookies=cookie,
                    )
                    if status < 400 and isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("data"), dict):
                        return AdapterResult(True, "连接正常", data=payload["data"])
                except Exception:
                    continue
        return AdapterResult(False, "会话无效或站点要求提供 New-API-User")

    async def checkin(self, url: str, credential: str, user_id: int | None = None) -> AdapterResult:
        base = normalize_url(url)
        resolved = user_id or _jwt_user_id(_token_value(credential))
        failures: list[str] = []
        try:
            status, payload, _ = await self._request(
                f"{base}/api/user/checkin", method="POST", credential=credential,
                user_id=resolved,
            )
            if status < 400 and isinstance(payload, dict) and payload.get("success") is True:
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                reward = data.get("reward")
                return AdapterResult(True, _message(payload, "签到成功"), str(reward) if reward is not None else None)
            failures.append(_message(payload, f"HTTP {status}"))
        except Exception as exc:
            failures.append(str(exc))

        # 兼容旧版 One-API/New API fork：Cookie + /api/user/sign_in。
        for cookie in _credential_cookies(credential):
            try:
                status, payload, _ = await self._request(
                    f"{base}/api/user/sign_in", method="POST", cookies=cookie, user_id=resolved, json_body={}
                )
                if status < 400 and isinstance(payload, dict) and payload.get("success") is True:
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    reward = data.get("reward")
                    return AdapterResult(True, _message(payload, "签到成功"), str(reward) if reward is not None else None)
                failures.append(_message(payload, f"旧版签到 HTTP {status}"))
            except Exception as exc:
                failures.append(str(exc))
        message = next((x for x in failures if x), "签到失败")
        if any("already" in x.lower() or "已签" in x or "签到过" in x for x in failures):
            return AdapterResult(True, "今日已签到")
        return AdapterResult(False, message[:500])

    async def balance(self, url: str, credential: str, user_id: int | None = None) -> AdapterResult:
        result = await self.self_info(url, credential, user_id)
        if not result.success or not result.data:
            return result
        data = result.data
        quota_raw = data.get("quota", 0) or 0
        used_raw = data.get("used_quota", 0) or 0
        try:
            quota = float(quota_raw) / 500000
            used = float(used_raw) / 500000
        except (TypeError, ValueError):
            quota, used = 0.0, 0.0
        return AdapterResult(True, "余额已刷新", data={
            "balance": quota, "used": used, "quota": quota + used,
            "username": data.get("username") or data.get("display_name") or "",
            "platform_user_id": data.get("id"),
        })
