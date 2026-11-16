# Charity Hub

一个面向自托管场景的公益站统一管理面板。它借鉴 [BalanceHub](https://github.com/NotoChen/BalanceHub) 的站点卡片、账号状态、批量操作与签到记录组织方式，并参考 [metapi](https://github.com/cita-777/metapi) 的平台适配器思路，提供 New API 及常见 Fork 的账号管理、余额刷新和自动签到。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，至少替换 CREDENTIAL_KEY
docker compose up -d --build
```

打开 <http://localhost:17842>。

首次登录使用 `.env` 中的 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`。生产环境请修改为自己的强密码，并将 `COOKIE_SECURE=true`；此时应通过 HTTPS 反向代理访问。

如果目录路径或 Compose 版本导致项目名无法自动推断，可以显式指定：

```bash
docker compose -p charity-hub up -d --build
```

数据保存在 `charity_hub_data` Docker volume 中。升级时保留该 volume 即可；`CREDENTIAL_KEY` 必须保持不变，否则已保存的 Token/Cookie 无法解密。

## 能力范围

- 站点卡片总览：状态、平台、账号数、活跃账号数。
- 账号凭据类型：Access Token、Cookie/Session、用户名密码登录换取 Token。
- New API 兼容请求：`/api/user/self`、`/api/user/login`、`/api/user/checkin`。
- 老版本签到回退：`/api/user/sign_in`。
- 兼容 `New-API-User`、`Veloera-User`、`voapi-user`、`User-id`、`X-User-Id` 等用户头。
- 单账号签到、批量签到、余额刷新、签到历史。
- 后台自动调度：默认每天凌晨 2:00 执行一次，可在面板设置每日签到时间。
- 管理面板登录保护：HttpOnly 会话 Cookie、7 天会话有效期、失败登录限流。
- Token/Cookie 使用 Fernet 加密后写入 SQLite，API 列表不会返回凭据原文。

## 最新模型探活

只探测 DeepSeek、GLM、ChatGPT（gpt-*）、Claude、Gemini 五个系列的**最新版本**，不会把公益站的所有模型都测一遍：

1. 通过 CLIProxyAPI 管理 API（`GET /v0/management/openai-compatibility`）读取每个公益站的 base-url、api-key 和模型；
2. 请求各站点 `/v1/models` 获取真实模型列表（失败时退回 CPA 中配置的模型），并忽略图像、语音、向量等非对话模型；
3. 每个系列在所有站点中取最高版本号（如 `gpt-6.1-*`、`glm-5.3*`），只对这些模型发一次 `max_tokens=16` 的请求；
4. 结果分为：可用、限流、鉴权/额度、无此模型、失败；并记录延迟、连续失败次数和最近一次可用的时间。

配置：在 `.env` 中设置 `CPA_BASE_URL` 和 `CPA_MANAGEMENT_KEY`。CLIProxyAPI 需要已设置 `remote-management.secret-key`；如果 Charity Hub 访问 CPA 时不是从 localhost 发起，还需要开启 `allow-remote`。默认每 60 分钟探测一次，可在面板「模型可用性」中修改或立即探测。

### 自动同步到 CPA

每轮测活前后会自动维护 CLIProxyAPI 的 openai-compatibility 提供商（面板「CPA 自动同步」可暂停）：

- **签到站自动接入**：启用中的签到站如果还不在 CPA 里，会用站点 access token 创建名为 `charity-hub-cpa` 的令牌，验证 `/v1/models` 可用后加入 CPA。旧版 access token 已失效、或 token 缺少 `api_key:read` 等权限的站点会在面板中标出原因。你手动从 CPA 删除的站点不会再被自动加回。
- **更新对外模型**：探测可用、但 CPA 里还没有的最新模型会被加入，alias 统一为小写并去掉厂商前缀（例如 `z-ai/GLM-5.3-Flash` → `glm-5.3-flash`），方便多个站点负载均衡；连续失败 `CPA_SYNC_FAIL_THRESHOLD`（默认 3）次的模型会从对外列表移除，恢复后按原条目加回。
- **停用/启用站点**：整体不可用（拿不到模型列表，且没有任何模型探测通过）连续达到阈值后设为 disabled，恢复后自动启用；你手动停用的站点不受影响。
- **安全措施**：每次写入前加密备份原列表（保留 30 份，可在面板一键回滚，回滚后自动暂停同步）；一轮中超过 80% 的站点同时失败时视为探测侧故障，跳过本轮写入。

命令行查询（需配置 `MODEL_STATUS_TOKEN`）：

```bash
curl -s -H "Authorization: Bearer $MODEL_STATUS_TOKEN" https://<你的域名>/api/models/available
```

## 注意事项

这是一个单用户、自托管管理台。请不要直接暴露到公网，建议通过 Nginx/Caddy 配置 HTTPS，并将 `COOKIE_SECURE=true`；同时只开放 80/443 和 SSH 端口。

部分公益站会开启 Cloudflare/Turnstile 或关闭签到接口，系统会把这类情况记录为失败或旧接口回退失败，不会尝试绕过人机校验。建议优先使用站点允许的 Token/Cookie，并遵守站点服务条款。

## 目录结构

```text
app/
  main.py       FastAPI API、SQLite 持久化与后台调度
  new_api.py    New API / Fork 适配器
  crypto.py     凭据加密
  db.py         数据库初始化
  static/       管理面板
Dockerfile
docker-compose.yml
```
