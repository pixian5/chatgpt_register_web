---
title: bz
emoji: "🧩"
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# pam管理 Web UI

这个项目已经改成“仓库不存真实配置”的模式：

- 仓库内只保留示例配置
- 本地开发使用 `.env`
- GitHub Actions 使用 `Secrets`
- Hugging Face Space 使用 Space Secrets / Variables
- Ubuntu 服务器使用环境变量或 `config.local.json`

## 配置优先级

运行时配置加载顺序如下，越靠后优先级越高：

1. 代码内默认值
2. `config.json`（仓库示例，不应存真实 secret）
3. `config.local.json`（本地/服务器私有配置，不提交）
4. `.env`
5. `.env.local`
6. 系统环境变量

Web UI 的“保存配置”现在会写入：

```txt
config.local.json
```

不会再把真实值写回仓库里的示例配置。

## 关键文件

- `web_app.py`
  FastAPI 主程序
- `register.py`
  业务逻辑与运行配置读写
- `chatgpt_register.py`
  注册底层实现
- `config_runtime.py`
  新增的统一配置加载模块
- `templates/index.html`
  前端页面
- `.env.example`
  本地 / CI / 服务器环境变量模板
- `config.example.json`
  JSON 示例配置

## 本地开发

### 1. 创建虚拟环境

```bash
python3 -m venv .venv
```

### 2. 安装依赖

```bash
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
```

### 3. 准备本地配置

```bash
cp .env.example .env
```

然后把真实值只写进 `.env`，不要写进 `config.json`。

## 环境变量

推荐使用这些变量名：

```env
DUCKMAIL_API_BASE=https://api.duckmail.sbs
DUCKMAIL_DOMAIN=codex.sbbz.tech
DUCKMAIL_BEARER=

POOL_BASE_URL=https://or.sbbz.tech:52788/
POOL_TOKEN=
POOL_TARGET_TYPE=codex
POOL_TARGET_COUNT=666
POOL_PROXY=
POOL_PROBE_WORKERS=40
POOL_DELETE_WORKERS=10
POOL_INTERVAL_MIN=30

PROXY=
WORKERS=1
PROXY_TEST_WORKERS=20

ENABLE_OAUTH=true
OAUTH_REQUIRED=true
OAUTH_ISSUER=https://auth.openai.com
OAUTH_CLIENT_ID=app_EMoamEEZ73f0CkXaXp7hrann
OAUTH_REDIRECT_URI=http://localhost:1455/auth/callback
```

## GitHub Secrets

如果你通过 GitHub Actions 构建或部署，建议把下面这些值放进仓库 Secrets：

- `DUCKMAIL_API_BASE`
- `DUCKMAIL_DOMAIN`
- `DUCKMAIL_BEARER`
- `POOL_BASE_URL`
- `POOL_TOKEN`

如果是 Actions 运行容器或远程部署，把这些 Secrets 作为环境变量注入即可。

## Hugging Face Space

这个项目使用 Docker Space 部署。

在 Hugging Face Space 后台：

`Settings -> Variables and secrets`

建议这样放：

- 非敏感值放 `Variables`
  - `DUCKMAIL_API_BASE`
  - `DUCKMAIL_DOMAIN`
  - `POOL_BASE_URL`
- 敏感值放 `Secrets`
  - `DUCKMAIL_BEARER`
  - `POOL_TOKEN`

Docker 启动时会自动读这些环境变量，不需要再把真实值提交进仓库。

## Ubuntu 服务器部署

推荐两种方式：

### 方式一：环境变量

例如：

```bash
export DUCKMAIL_API_BASE="https://api.duckmail.sbs"
export DUCKMAIL_DOMAIN="codex.sbbz.tech"
export DUCKMAIL_BEARER="your-real-token"
export POOL_BASE_URL="https://or.sbbz.tech:52788/"
export POOL_TOKEN="your-real-token"
```

然后再启动：

```bash
python -m uvicorn web_app:app --host 0.0.0.0 --port 52789
```

### 方式二：私有配置文件

在服务器项目目录创建：

```txt
config.local.json
```

或：

```txt
.env
```

这两个文件都已被 `.gitignore` 忽略，不会进仓库。

## 安全说明

以下文件不会被提交：

- `.env`
- `.env.local`
- `config.local.json`

仓库中的这些文件只保留示例值：

- `config.json`
- `config.example.json`
- `.env.example`

另外，Web UI 返回配置时会对 secret 做掩码处理，避免前端直接拿到完整明文。

## GitHub 自动部署

仓库已支持：

- push 到 `main` 后自动部署服务器
- push 到 `main` 后自动同步到 Hugging Face Space

工作流文件：

```txt
.github/workflows/deploy.yml
```

### 服务器 Secrets

GitHub 仓库里至少需要配置这些 Secrets：

- `SERVER_HOST`
- `SERVER_USER`
- `SERVER_SSH_KEY`
- `SERVER_APP_DIR`
  建议填 `/root/c/chatgpt_register_web`
- `SERVER_APP_PORT`
  建议填 `52789`

以及运行配置：

- `DUCKMAIL_API_BASE`
- `DUCKMAIL_DOMAIN`
- `DUCKMAIL_BEARER`
- `POOL_BASE_URL`
- `POOL_TOKEN`
- `POOL_TARGET_TYPE`
- `POOL_TARGET_COUNT`
- `POOL_PROXY`
- `POOL_PROBE_WORKERS`
- `POOL_DELETE_WORKERS`
- `POOL_INTERVAL_MIN`
- `PROXY`
- `WORKERS`
- `PROXY_TEST_WORKERS`
- `ENABLE_OAUTH`
- `OAUTH_REQUIRED`
- `OAUTH_ISSUER`
- `OAUTH_CLIENT_ID`
- `OAUTH_REDIRECT_URI`

### Hugging Face Secrets

如果需要自动同步到 HF，还要配置：

- `HF_TOKEN`
- `HF_SPACE_ID`

示例：

```txt
HF_SPACE_ID=xbzsb/bz
```

### 服务器部署方式

工作流会在服务器上自动：

1. 拉取最新 GitHub 代码
2. 生成私有 `.env`
3. 创建或复用 `.venv`
4. 安装依赖
5. 生成 `systemd` 服务
6. 重启服务

服务名默认是：

```txt
chatgpt-register-web
```
