# Charity Hub

一个面向自托管场景的公益站统一管理面板。它借鉴 [BalanceHub](https://github.com/NotoChen/BalanceHub) 的站点卡片、账号状态、批量操作与签到记录组织方式，并参考 [metapi](https://github.com/cita-777/metapi) 的平台适配器思路，提供 New API 及常见 Fork 的账号管理、余额刷新和自动签到。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env，至少替换 CREDENTIAL_KEY
docker compose up -d --build
```

打开 <http://localhost:3000>。

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
- Token/Cookie 使用 Fernet 加密后写入 SQLite，API 列表不会返回凭据原文。

## 注意事项

这是一个单用户、自托管管理台，默认没有额外的登录层，请不要直接暴露到公网。若需要公网访问，请在反向代理层增加认证、HTTPS 和访问控制。

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
