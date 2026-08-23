from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .crypto import decrypt, encrypt, fingerprint
from .db import connection, init_db, row_dict, rows_dict
from .new_api import NewAPIAdapter, _looks_like_cookie, normalize_url
from .auth import COOKIE_NAME, ADMIN_USERNAME, check_password, clear_failed_logins, create_session, hash_password, login_allowed, record_failed_login, validate_runtime_config, verify_session


ROOT = os.path.dirname(__file__)
adapter = NewAPIAdapter()
checkin_lock = asyncio.Lock()
scheduler_task: asyncio.Task[None] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_account(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("credential", None)
    row["checkin_enabled"] = bool(row.get("checkin_enabled"))
    row["credential_hint"] = fingerprint(row.pop("credential_hash", "")) if row.get("credential_hash") else ""
    return row


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=3, max_length=500)
    platform: str = "new-api"
    note: str = Field(default="", max_length=500)

    @field_validator("url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        return normalize_url(value)


class AccountIn(BaseModel):
    site_id: int = Field(gt=0)
    username: str = Field(default="", max_length=200)
    credential: str = Field(min_length=1, max_length=10000)
    credential_type: Literal["token", "cookie", "password"] = "token"
    platform_user_id: int | None = Field(default=None, gt=0)
    checkin_enabled: bool = True


class AccountUpdate(BaseModel):
    username: str | None = Field(default=None, max_length=200)
    credential: str | None = Field(default=None, min_length=1, max_length=10000)
    credential_type: Literal["token", "cookie", "password"] | None = None
    platform_user_id: int | None = Field(default=None, gt=0)
    checkin_enabled: bool | None = None
    status: Literal["active", "disabled"] | None = None


class ScheduleIn(BaseModel):
    enabled: bool
    checkin_time: str = "02:00"

    @field_validator("checkin_time")
    @classmethod
    def validate_checkin_time(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("签到时间必须是 HH:MM 格式") from exc
        return parsed.strftime("%H:%M")


class ConfigImportIn(BaseModel):
    data: dict[str, Any]
    mode: Literal["merge"] = "merge"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=500)
    new_password: str = Field(min_length=12, max_length=500)


def site_view(row: dict[str, Any]) -> dict[str, Any]:
    row["account_count"] = int(row.get("account_count") or 0)
    row["healthy_count"] = int(row.get("healthy_count") or 0)
    row["last_probe_ok"] = None if row.get("last_probe_ok") is None else bool(row["last_probe_ok"])
    return row


def stored_admin_password_hash() -> str:
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'admin_password_hash'").fetchone()
    return str(row["value"]) if row and row["value"] else ""


def _backup_root(data: dict[str, Any]) -> dict[str, Any]:
    nested = data.get("accounts")
    return nested if isinstance(nested, dict) else data


def _import_records(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    root = _backup_root(data)
    raw_sites = root.get("sites", []) if isinstance(root.get("sites", []), list) else []
    raw_accounts = root.get("accounts", []) if isinstance(root.get("accounts", []), list) else []
    raw_tokens = root.get("accountTokens", []) if isinstance(root.get("accountTokens", []), list) else []
    tokens_by_account: dict[Any, str] = {}
    for token in raw_tokens:
        if not isinstance(token, dict) or token.get("enabled") is False:
            continue
        value = token.get("token")
        if value and token.get("isDefault", False):
            tokens_by_account[token.get("accountId")] = str(value)
    sites: list[dict[str, Any]] = []
    site_keys: set[tuple[str, str]] = set()
    warnings: list[str] = []
    for item in raw_sites:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        try:
            url = normalize_url(str(item["url"]))
        except ValueError:
            continue
        platform = str(item.get("platform") or "new-api").strip() or "new-api"
        key = (platform.lower(), url.lower())
        if key in site_keys:
            continue
        site_keys.add(key)
        sites.append({
            "source_id": item.get("id"), "key": key, "name": str(item.get("name") or url)[:120],
            "url": url, "platform": platform[:40], "status": "disabled" if item.get("status") == "disabled" else "active",
            "note": str(item.get("note") or "")[:500],
        })
    accounts: list[dict[str, Any]] = []
    for item in raw_accounts:
        if not isinstance(item, dict):
            continue
        source_site_id = item.get("siteId", item.get("site_id"))
        credential = item.get("credential") or item.get("accessToken") or tokens_by_account.get(item.get("id")) or item.get("apiToken")
        if not credential or not source_site_id:
            warnings.append(f"账号 {item.get('username') or item.get('id') or '未知'} 缺少可用凭据，已跳过")
            continue
        extra = item.get("extraConfig")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                extra = {}
        extra = extra if isinstance(extra, dict) else {}
        platform_user_id = item.get("platformUserId") or extra.get("platformUserId")
        try:
            platform_user_id = int(platform_user_id) if platform_user_id else None
        except (TypeError, ValueError):
            platform_user_id = None
        accounts.append({
            "source_site_id": source_site_id, "username": str(item.get("username") or "")[:200],
            "credential": str(credential), "credential_type": "cookie" if _looks_like_cookie(str(credential)) else "token",
            "platform_user_id": platform_user_id, "checkin_enabled": bool(item.get("checkinEnabled", True)),
            "status": "disabled" if item.get("status") in {"disabled", "expired"} else "active",
        })
    ignored = [key for key in ("siteApiEndpoints", "siteDisabledModels", "manualModels", "routeChannels", "routeGroupSources", "tokenRoutes", "downstreamApiKeys") if root.get(key)]
    if ignored:
        warnings.append("当前版本只导入站点、账号和签到相关字段；以下 metapi 配置未导入：" + ", ".join(ignored))
    return sites, accounts, warnings


def account_with_site(account_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT a.*, s.name AS site_name, s.url AS site_url, s.platform
               FROM accounts a JOIN sites s ON s.id = a.site_id WHERE a.id = ?""", (account_id,)
        ).fetchone()
    return row_dict(row)


async def execute_checkin(account_id: int, source: str = "manual") -> dict[str, Any]:
    account = account_with_site(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    if account["status"] != "active":
        return {"account_id": account_id, "site_id": account["site_id"], "status": "skipped", "message": "账号已停用"}
    with connection() as conn:
        site = conn.execute("SELECT status FROM sites WHERE id = ?", (account["site_id"],)).fetchone()
    if not site or site["status"] != "active":
        return {"account_id": account_id, "site_id": account["site_id"], "status": "skipped", "message": "站点已停用"}
    try:
        credential = decrypt(account["credential"])
    except ValueError as exc:
        result = {"account_id": account_id, "site_id": account["site_id"], "status": "failed", "message": str(exc)}
    else:
        try:
            result_obj = await adapter.checkin(account["site_url"], credential, account["platform_user_id"])
            result = {
                "account_id": account_id,
                "site_id": account["site_id"],
                "status": "success" if result_obj.success else "failed",
                "message": result_obj.message,
                "reward": result_obj.reward,
            }
        except Exception as exc:
            result = {"account_id": account_id, "site_id": account["site_id"], "status": "failed", "message": f"请求异常：{exc}"}
    timestamp = now_iso()
    with connection() as conn:
        conn.execute(
            "INSERT INTO checkin_logs(account_id, status, message, reward, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, result["status"], result["message"][:500], result.get("reward"), timestamp),
        )
        if result["status"] == "success":
            conn.execute("UPDATE accounts SET last_checkin_at = ?, status = 'active', updated_at = ? WHERE id = ?", (timestamp, timestamp, account_id))
        elif "过期" in result["message"] or "expired" in result["message"].lower() or "未登录" in result["message"]:
            conn.execute("UPDATE accounts SET status = 'disabled', updated_at = ? WHERE id = ?", (timestamp, account_id))
    result["source"] = source
    return result


async def refresh_account(account_id: int) -> dict[str, Any]:
    account = account_with_site(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    credential = decrypt(account["credential"])
    result = await adapter.balance(account["site_url"], credential, account["platform_user_id"])
    if not result.success or not result.data:
        raise HTTPException(502, result.message)
    data = result.data
    timestamp = now_iso()
    with connection() as conn:
        conn.execute(
            """UPDATE accounts SET username = COALESCE(NULLIF(?, ''), username), platform_user_id = COALESCE(?, platform_user_id),
               balance = ?, used = ?, quota = ?, last_balance_at = ?, updated_at = ? WHERE id = ?""",
            (data.get("username", ""), data.get("platform_user_id"), data.get("balance"), data.get("used"), data.get("quota"), timestamp, timestamp, account_id),
        )
    return {"account_id": account_id, **data, "last_balance_at": timestamp}


async def scheduler_loop() -> None:
    while True:
        try:
            with connection() as conn:
                setting = conn.execute("SELECT key, value FROM settings WHERE key IN ('checkin_enabled', 'checkin_time', 'last_scheduler_day')").fetchall()
                values = {row["key"]: row["value"] for row in setting}
                enabled = values.get("checkin_enabled", "true").lower() == "true"
                accounts = conn.execute(
                    """SELECT a.id, a.last_checkin_at FROM accounts a JOIN sites s ON s.id = a.site_id
                       WHERE a.status = 'active' AND a.checkin_enabled = 1 AND s.status = 'active'"""
                ).fetchall()
            local_now = datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Shanghai")))
            configured_time = values.get("checkin_time", "02:00")
            try:
                hour, minute = (int(part) for part in configured_time.split(":", 1))
                due_time = time(hour=hour, minute=minute)
            except (ValueError, TypeError):
                due_time = time(hour=2, minute=0)
            today = local_now.date().isoformat()
            if enabled and local_now.time() >= due_time and values.get("last_scheduler_day", "") != today:
                if accounts:
                    async with checkin_lock:
                        for item in accounts:
                            try:
                                await execute_checkin(item["id"], "scheduler")
                            except Exception as exc:
                                print(f"[scheduler] account={item['id']} error={exc}")
                    with connection() as conn:
                        conn.execute("UPDATE settings SET value = ? WHERE key = 'last_scheduler_run'", (now_iso(),))
                        conn.execute("UPDATE settings SET value = ? WHERE key = 'last_scheduler_day'", (today,))
        except Exception as exc:
            print(f"[scheduler] error={exc}")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global scheduler_task
    validate_runtime_config()
    init_db()
    scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    if scheduler_task:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Charity Hub", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")


@app.middleware("http")
async def protect_management_api(request: Request, call_next):
    path = request.url.path
    public_api = {"/api/health", "/api/auth/login", "/api/auth/status", "/api/auth/logout"}
    authenticated = verify_session(request.cookies.get(COOKIE_NAME))
    if path.startswith("/api/") and path not in public_api and not authenticated:
        return JSONResponse(status_code=401, content={"detail": "请先登录管理面板"})
    if path.startswith("/api/") and authenticated and not stored_admin_password_hash() and path not in {"/api/auth/change-password", "/api/auth/logout", "/api/auth/status"}:
        return JSONResponse(status_code=428, content={"detail": "首次登录必须先修改管理密码"})
    return await call_next(request)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(os.path.join(ROOT, "static", "index.html"))


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "time": now_iso()}


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    authenticated = verify_session(request.cookies.get(COOKIE_NAME))
    return {"authenticated": authenticated, "username": ADMIN_USERNAME if authenticated else None, "must_change_password": authenticated and not stored_admin_password_hash()}


@app.post("/api/auth/login")
async def auth_login(payload: LoginIn, request: Request) -> JSONResponse:
    client_id = request.client.host if request.client else "unknown"
    if not login_allowed(client_id):
        raise HTTPException(429, "登录失败次数过多，请 10 分钟后重试")
    if not check_password(payload.username, payload.password, stored_admin_password_hash() or None):
        record_failed_login(client_id)
        raise HTTPException(401, "用户名或密码错误")
    clear_failed_logins(client_id)
    response = JSONResponse({"authenticated": True, "username": ADMIN_USERNAME, "must_change_password": not stored_admin_password_hash()})
    response.set_cookie(COOKIE_NAME, create_session(ADMIN_USERNAME), max_age=7 * 24 * 60 * 60, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", path="/")
    return response


@app.post("/api/auth/logout")
async def auth_logout() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.post("/api/auth/change-password")
async def auth_change_password(payload: ChangePasswordIn, request: Request) -> JSONResponse:
    if not verify_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(401, "请先登录")
    if payload.new_password == payload.current_password:
        raise HTTPException(422, "新密码不能与当前密码相同")
    if not check_password(ADMIN_USERNAME, payload.current_password, stored_admin_password_hash() or None):
        raise HTTPException(401, "当前密码错误")
    with connection() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES('admin_password_hash', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (hash_password(payload.new_password),))
    return JSONResponse({"ok": True, "message": "管理密码已修改"})


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    with connection() as conn:
        sites = rows_dict(conn.execute(
            """SELECT s.*, COUNT(a.id) account_count,
                      SUM(CASE WHEN a.status = 'active' THEN 1 ELSE 0 END) healthy_count
               FROM sites s LEFT JOIN accounts a ON a.site_id = s.id GROUP BY s.id ORDER BY s.status DESC, s.id DESC"""
        ).fetchall())
        total_accounts = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        active_accounts = conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE status = 'active'").fetchone()["n"]
        checked_today = conn.execute("SELECT COUNT(*) AS n FROM checkin_logs WHERE status = 'success' AND date(created_at) = date('now')").fetchone()["n"]
        failures_today = conn.execute("SELECT COUNT(*) AS n FROM checkin_logs WHERE status = 'failed' AND date(created_at) = date('now')").fetchone()["n"]
        settings = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    return {
        "sites": [site_view(site) for site in sites],
        "stats": {"sites": len(sites), "accounts": total_accounts, "active_accounts": active_accounts, "checked_today": checked_today, "failures_today": failures_today},
        "schedule": {"enabled": settings.get("checkin_enabled", "true") == "true", "checkin_time": settings.get("checkin_time", "02:00"), "last_run": settings.get("last_scheduler_run") or None},
    }


@app.post("/api/sites")
async def create_site(payload: SiteIn) -> dict[str, Any]:
    with connection() as conn:
        try:
            cursor = conn.execute("INSERT INTO sites(name, url, platform, note) VALUES (?, ?, ?, ?)", (payload.name.strip(), payload.url, payload.platform, payload.note.strip()))
        except Exception as exc:
            raise HTTPException(409, f"站点地址已存在或保存失败：{exc}") from exc
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return row_dict(row) or {}


@app.post("/api/sites/probe")
async def probe_site(payload: SiteIn) -> dict[str, Any]:
    result = await adapter.detect(payload.url)
    with connection() as conn:
        conn.execute("UPDATE sites SET last_probe_at = ?, last_probe_ok = ?, updated_at = ? WHERE url = ?", (now_iso(), int(result.success), now_iso(), payload.url))
    return {"ok": result.success, "message": result.message}


@app.patch("/api/sites/{site_id}")
async def update_site(site_id: int, payload: SiteIn) -> dict[str, Any]:
    with connection() as conn:
        result = conn.execute("UPDATE sites SET name = ?, url = ?, platform = ?, note = ?, updated_at = ? WHERE id = ?", (payload.name.strip(), payload.url, payload.platform, payload.note.strip(), now_iso(), site_id))
        if result.rowcount == 0:
            raise HTTPException(404, "站点不存在")
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    return row_dict(row) or {}


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: int) -> dict[str, bool]:
    with connection() as conn:
        result = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    if result.rowcount == 0:
        raise HTTPException(404, "站点不存在")
    return {"ok": True}


@app.post("/api/sites/{site_id}/toggle")
async def toggle_site(site_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT status FROM sites WHERE id = ?", (site_id,)).fetchone()
        if not row:
            raise HTTPException(404, "站点不存在")
        status = "disabled" if row["status"] == "active" else "active"
        conn.execute("UPDATE sites SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), site_id))
    return {"id": site_id, "status": status}


@app.get("/api/sites/{site_id}/accounts")
async def list_accounts(site_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = rows_dict(conn.execute("SELECT id, site_id, username, credential_type, platform_user_id, balance, used, quota, status, checkin_enabled, last_checkin_at, last_balance_at, created_at FROM accounts WHERE site_id = ? ORDER BY id DESC", (site_id,)).fetchall())
    return [public_account(row) for row in rows]


@app.post("/api/accounts")
async def create_account(payload: AccountIn) -> dict[str, Any]:
    with connection() as conn:
        site = conn.execute("SELECT * FROM sites WHERE id = ?", (payload.site_id,)).fetchone()
    if not site:
        raise HTTPException(404, "站点不存在")
    credential = payload.credential.strip()
    credential_type = payload.credential_type
    if credential_type == "password":
        login = await adapter.login(site["url"], payload.username, credential)
        if not login.success or not login.data:
            raise HTTPException(422, login.message)
        credential = login.data["credential"]
        credential_type = login.data["credential_type"]
    timestamp = now_iso()
    with connection() as conn:
        cursor = conn.execute("""INSERT INTO accounts(site_id, username, credential, credential_type, platform_user_id, checkin_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (payload.site_id, payload.username.strip(), encrypt(credential), credential_type, payload.platform_user_id, int(payload.checkin_enabled), timestamp, timestamp))
        row = conn.execute("SELECT id, site_id, username, credential_type, platform_user_id, balance, used, quota, status, checkin_enabled, last_checkin_at, last_balance_at, created_at FROM accounts WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return public_account(row_dict(row) or {})


@app.patch("/api/accounts/{account_id}")
async def update_account(account_id: int, payload: AccountUpdate) -> dict[str, Any]:
    account = account_with_site(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    values: dict[str, Any] = {}
    for key in ("username", "platform_user_id", "status"):
        value = getattr(payload, key)
        if value is not None:
            values[key] = value
    if payload.checkin_enabled is not None:
        values["checkin_enabled"] = int(payload.checkin_enabled)
    if payload.credential is not None:
        credential = payload.credential.strip()
        credential_type = payload.credential_type or account["credential_type"]
        if credential_type == "password":
            login = await adapter.login(account["site_url"], payload.username or account["username"], credential)
            if not login.success or not login.data:
                raise HTTPException(422, login.message)
            credential, credential_type = login.data["credential"], login.data["credential_type"]
        values["credential"] = encrypt(credential)
        values["credential_type"] = credential_type
    if payload.credential_type is not None and payload.credential is None:
        values["credential_type"] = payload.credential_type
    values["updated_at"] = now_iso()
    if len(values) > 1:
        assignment = ", ".join(f"{key} = ?" for key in values)
        with connection() as conn:
            conn.execute(f"UPDATE accounts SET {assignment} WHERE id = ?", (*values.values(), account_id))
    return public_account(account_with_site(account_id) or {})


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int) -> dict[str, bool]:
    with connection() as conn:
        result = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    if result.rowcount == 0:
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/checkin")
async def checkin_one(account_id: int) -> dict[str, Any]:
    async with checkin_lock:
        return await execute_checkin(account_id)


@app.post("/api/checkin/all")
async def checkin_all() -> dict[str, Any]:
    with connection() as conn:
        ids = [row["id"] for row in conn.execute("SELECT a.id FROM accounts a JOIN sites s ON s.id = a.site_id WHERE a.status = 'active' AND a.checkin_enabled = 1 AND s.status = 'active'").fetchall()]
    async with checkin_lock:
        results = [await execute_checkin(account_id) for account_id in ids]
    return {"total": len(results), "success": sum(item["status"] == "success" for item in results), "results": results}


@app.post("/api/accounts/{account_id}/refresh")
async def refresh_one(account_id: int) -> dict[str, Any]:
    return await refresh_account(account_id)


@app.get("/api/logs")
async def logs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    with connection() as conn:
        return rows_dict(conn.execute("""SELECT l.*, a.username, s.name AS site_name FROM checkin_logs l
          JOIN accounts a ON a.id = l.account_id JOIN sites s ON s.id = a.site_id ORDER BY l.id DESC LIMIT ?""", (limit,)).fetchall())


@app.get("/api/config/export")
async def export_config() -> JSONResponse:
    with connection() as conn:
        sites = rows_dict(conn.execute("SELECT id, name, url, platform, status, note, created_at, updated_at FROM sites ORDER BY id").fetchall())
        accounts = rows_dict(conn.execute("SELECT id, site_id, username, credential, credential_type, platform_user_id, balance, used, quota, status, checkin_enabled, last_checkin_at, last_balance_at, created_at, updated_at FROM accounts ORDER BY id").fetchall())
        settings = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    exported_accounts: list[dict[str, Any]] = []
    for item in accounts:
        credential = decrypt(item.pop("credential"))
        item["credential"] = credential
        item["site_id"] = item.pop("site_id")
        item["checkin_enabled"] = bool(item.get("checkin_enabled"))
        exported_accounts.append(item)
    payload = {
        "format": "charity-hub",
        "version": 1,
        "exported_at": now_iso(),
        "warning": "此文件包含站点账号凭据，请妥善保管。",
        "sites": sites,
        "accounts": exported_accounts,
        "schedule": {"enabled": settings.get("checkin_enabled", "true") == "true", "checkin_time": settings.get("checkin_time", "02:00")},
    }
    return JSONResponse(content=payload, headers={"Content-Disposition": "attachment; filename=charity-hub-config.json"})


@app.post("/api/config/import/preview")
async def preview_config(payload: ConfigImportIn) -> dict[str, Any]:
    sites, accounts, warnings = _import_records(payload.data)
    return {
        "ok": bool(sites), "sites": len(sites), "accounts": len(accounts),
        "site_names": [site["name"] for site in sites[:12]], "warnings": warnings[:20],
        "message": "可导入预览已生成" if sites else "未找到可导入的站点数据",
    }


@app.post("/api/config/import")
async def import_config(payload: ConfigImportIn) -> dict[str, Any]:
    sites, accounts, warnings = _import_records(payload.data)
    if not sites:
        raise HTTPException(422, "JSON 中没有可导入的站点数据")
    timestamp = now_iso()
    site_id_by_key: dict[tuple[str, str], int] = {}
    source_site_id_map: dict[Any, int] = {}
    inserted_sites = 0
    updated_sites = 0
    inserted_accounts = 0
    skipped_accounts = 0
    with connection() as conn:
        for site in sites:
            existing = conn.execute("SELECT id FROM sites WHERE platform = ? AND url = ?", (site["platform"], site["url"])).fetchone()
            if existing:
                site_id = existing["id"]
                conn.execute("UPDATE sites SET name = ?, status = ?, note = ?, updated_at = ? WHERE id = ?", (site["name"], site["status"], site["note"], timestamp, site_id))
                updated_sites += 1
            else:
                cursor = conn.execute("INSERT INTO sites(name, url, platform, status, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (site["name"], site["url"], site["platform"], site["status"], site["note"], timestamp, timestamp))
                site_id = cursor.lastrowid
                inserted_sites += 1
            site_id_by_key[site["key"]] = int(site_id)
            source_site_id_map[site.get("source_id")] = int(site_id)
        for item in accounts:
            site_id = source_site_id_map.get(item["source_site_id"])
            if not site_id:
                skipped_accounts += 1
                continue
            duplicate = False
            existing_accounts = conn.execute("SELECT credential FROM accounts WHERE site_id = ?", (site_id,)).fetchall()
            for existing in existing_accounts:
                try:
                    if decrypt(existing["credential"]) == item["credential"]:
                        duplicate = True
                        break
                except ValueError:
                    continue
            if duplicate:
                skipped_accounts += 1
                continue
            conn.execute("""INSERT INTO accounts(site_id, username, credential, credential_type, platform_user_id, status, checkin_enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (site_id, item["username"], encrypt(item["credential"]), item["credential_type"], item["platform_user_id"], item["status"], int(item["checkin_enabled"]), timestamp, timestamp))
            inserted_accounts += 1
        imported_schedule = payload.data.get("schedule") if isinstance(payload.data.get("schedule"), dict) else None
        if imported_schedule and isinstance(imported_schedule.get("checkin_time"), str):
            try:
                checkin_time = datetime.strptime(imported_schedule["checkin_time"], "%H:%M").strftime("%H:%M")
                conn.execute("INSERT INTO settings(key, value) VALUES('checkin_time', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (checkin_time,))
                conn.execute("INSERT INTO settings(key, value) VALUES('checkin_enabled', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("true" if imported_schedule.get("enabled", True) else "false",))
            except ValueError:
                warnings.append("导入的自动签到时间格式无效，已保留当前设置")
    return {"ok": True, "inserted_sites": inserted_sites, "updated_sites": updated_sites, "inserted_accounts": inserted_accounts, "skipped_accounts": skipped_accounts, "warnings": warnings[:20]}


@app.get("/api/schedule")
async def get_schedule() -> dict[str, Any]:
    with connection() as conn:
        rows = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    return {"enabled": rows.get("checkin_enabled", "true") == "true", "checkin_time": rows.get("checkin_time", "02:00"), "last_run": rows.get("last_scheduler_run") or None}


@app.put("/api/schedule")
async def set_schedule(payload: ScheduleIn) -> dict[str, Any]:
    with connection() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES('checkin_enabled', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("true" if payload.enabled else "false",))
        conn.execute("INSERT INTO settings(key, value) VALUES('checkin_time', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (payload.checkin_time,))
    return {"enabled": payload.enabled, "checkin_time": payload.checkin_time}
