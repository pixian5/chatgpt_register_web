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

这是一个基于 FastAPI 的管理面板，用来统一处理以下几类工作：

- 批量注册账号
- 查看注册日志与结果
- 管理本地 token 文件
- 对接 CliProxyAPI 账号池
- 清理失效账号、补号、守护补号
- 通过 Web UI 持续观察运行状态

项目当前同时用于：

- GitHub 仓库维护
- Hugging Face Space 部署
- Ubuntu 服务器部署

README 下面的内容按“实际维护流程”编写，不是模板文档。

## 目录结构

项目核心文件如下：

- `web_app.py`
  FastAPI 主程序，提供页面、REST API、WebSocket、守护进程、补号状态管理等能力。

- `register.py`
  项目业务库，负责封装注册、清理、同步、补号、守护补号等核心逻辑。

- `chatgpt_register.py`
  账号注册底层实现。

- `templates/index.html`
  单页前端界面，包含设置、日志、账号列表、手动补号、守护进程等全部 UI 逻辑。

- `config.json`
  默认配置文件。

- `requirements.txt`
  Python 依赖清单。

- `Dockerfile`
  Hugging Face Space 的 Docker 部署入口。

## 运行环境

推荐环境：

- Python `3.11+`
- Linux / macOS
- Ubuntu 服务器用于线上运行
- Hugging Face Space 用于公网页面部署

当前依赖：

```txt
requests>=2.32.5
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
curl-cffi>=0.7.0
python-multipart>=0.0.9
```

## 配置说明

配置文件为 `config.json`，常用字段如下：

- `duckmail_api_base`
  临时邮箱 API 地址。

- `duckmail_domain`
  注册邮箱后缀。

- `duckmail_bearer`
  临时邮箱 API 鉴权。

- `proxy`
  默认代理。

- `workers`
  注册并发数。

- `proxy_test_workers`
  代理测试并发数。

- `enable_oauth`
  是否启用 OAuth 流程。

- `oauth_required`
  是否要求 OAuth 成功。

- `token_json_dir`
  本地 token 保存目录。

- `pool.base_url`
  CliProxyAPI 地址。

- `pool.token`
  CliProxyAPI Token。

- `pool.target_type`
  目标账号类型，当前默认 `codex`。

- `pool.target_count`
  目标数量，当前默认值是 `666`。

- `pool.probe_workers`
  池探测并发数。

- `pool.delete_workers`
  池清理并发数。

- `pool.interval_min`
  守护进程间隔分钟数。

## 本地开发

如果只是修改代码、校验语法、提交发布，使用下面流程即可。

### 1. 进入项目目录

```bash
cd /Users/x/code/code_register_web
```

### 2. 创建虚拟环境

如果还没有 `.venv`：

```bash
python3 -m venv .venv
```

### 3. 安装依赖

```bash
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
```

### 4. 做语法检查

```bash
python3 -m py_compile web_app.py register.py chatgpt_register.py
```

说明：

- 当前维护约定是“本机默认不启动项目”。
- 本机主要用于改代码、校验、提交、发布。
- 真正运行以 Hugging Face 和 Ubuntu 服务器为准。

## 本地手动启动方式

如果你确实需要手动临时运行：

```bash
.venv/bin/python -m uvicorn web_app:app --host 127.0.0.1 --port 52789
```

访问：

```txt
http://127.0.0.1:52789
```

停止：

```bash
Ctrl + C
```

但默认维护流程中不需要本机启动。

## Hugging Face Space 部署

当前 Space 信息：

- Space: `xbzsb/bz`
- 运行方式：Docker

Space 部署入口来自仓库顶部的 Front Matter 和 `Dockerfile`。

### Docker 启动命令

容器内最终启动的是：

```bash
python -m uvicorn web_app:app --host 0.0.0.0 --port 7860
```

### 当前发布方式

为了避免把本地运行态文件和敏感文件一起推上去，发布 HF 时采用“导出净化副本再强推”的方式：

1. 从当前 Git 提交导出干净副本。
2. 删除不应该上传到 HF 的本地文件。
3. 在临时目录重新初始化 Git。
4. 强推到 HF Space 仓库。

典型流程：

```bash
tmpdir=$(mktemp -d)
git archive HEAD | tar -x -C "$tmpdir"
rm -rf "$tmpdir/.agents" "$tmpdir/.codex" "$tmpdir/.venv" "$tmpdir/codex_tokens" "$tmpdir/__pycache__"
rm -f "$tmpdir/ak.txt" "$tmpdir/rk.txt" "$tmpdir/registered_accounts.txt" "$tmpdir/.DS_Store" "$tmpdir/uvicorn.log"
cd "$tmpdir"
git init
git config user.name "pixian5"
git config user.email "pixian5@users.noreply.github.com"
git add .
git commit -m "deploy <commit>"
git remote add origin "https://<hf-user>:<hf-token>@huggingface.co/spaces/xbzsb/bz"
git push -f origin HEAD:main
```

### 不应上传到 HF 的内容

这些内容属于本地运行态或敏感文件，不应该进 Space：

- `ak.txt`
- `rk.txt`
- `registered_accounts.txt`
- `.agents/`
- `.codex/`
- `.venv/`
- `codex_tokens/`
- `uvicorn.log`

## Ubuntu 服务器部署

当前线上服务器：

- 域名：`or.sbbz.tech`
- 运行目录：`/root/c/chatgpt_register_web`
- 服务端口：`52789`

### 服务器部署原则

服务器更新代码时，不应该覆盖这些运行态数据：

- `ak.txt`
- `rk.txt`
- `registered_accounts.txt`
- `config.json`
- `codex_tokens/`

因为这些文件记录的是实际运行状态、账号、配置和 token。

### 服务器更新流程

当前稳定流程如下：

1. 备份运行态文件。
2. `git stash` 暂存运行态修改。
3. `git fetch` + `git pull --ff-only` 更新代码。
4. 恢复运行态文件。
5. 检查 `.venv`，没有就创建。
6. 安装依赖。
7. 执行语法检查。
8. 杀掉旧的 `uvicorn` 进程。
9. 启动新的 `uvicorn` 进程。

### 服务器启动命令

```bash
/root/c/chatgpt_register_web/.venv/bin/python -m uvicorn web_app:app --host 0.0.0.0 --port 52789
```

常见后台启动方式：

```bash
nohup /root/c/chatgpt_register_web/.venv/bin/python -m uvicorn web_app:app --host 0.0.0.0 --port 52789 >/root/c/chatgpt_register_web/uvicorn.log 2>&1 &
```

### 访问地址

```txt
http://or.sbbz.tech:52789
```

## Git 提交流程

每次代码修改完成后，推荐固定步骤：

1. 修改代码。
2. 运行语法检查。
3. 提交 Git。
4. 推送 GitHub。
5. 部署 HF。
6. 部署服务器。

### 提交示例

```bash
git add .
git commit -m "中文提交信息"
git push origin main
```

当前远程仓库：

```txt
origin = https://github.com/pixian5/chatgpt_register_web.git
```

## 页面功能概览

当前 Web UI 主要包含这些模块：

- 设置区
  管理邮箱、代理、OAuth、账号池、守护进程参数。

- 手动补号
  检查账号池、清理失效账号、执行补号。

- 守护进程
  定时维护账号池。

- 仅注册
  只做注册，不处理池同步。

- 账号列表
  查看当前账号状态。

- 实时日志
  显示池维护、补号、注册日志。

## 实时统计说明

当前补号统计机制分成两层：

### 1. 后端主统计

后端维护实时统计接口：

```txt
/api/pool/reg-stats
```

这个接口会记录：

- `mode`
- `running`
- `success`
- `fail`
- `total`

在手动补号或守护补号过程中，注册每完成一次，后端就会更新一次统计。

### 2. 前端兜底覆盖

前端除了在任务状态轮询时读取后端统计外，还额外做了每 `10` 秒一次的兜底覆盖：

- 每隔 10 秒重新请求 `/api/pool/reg-stats`
- 用服务端结果覆盖当前页面显示

这样做是为了避免：

- 页面刷新后统计丢失
- WebSocket 或前端本地计数发生漂移
- 多标签页显示不一致

## 常见维护注意事项

### 1. 不要把运行态文件提交进 Git

尤其不要把下面这些文件当作代码变更一起提交：

- `ak.txt`
- `rk.txt`
- `registered_accounts.txt`
- `codex_tokens/`

### 2. 服务器配置和仓库默认值不是一回事

例如：

- 仓库默认 `pool.target_count` 可以是 `666`
- 服务器运行中的 `config.json` 可能还是别的值

部署服务器时如果恢复了旧 `config.json`，运行值会继续沿用服务器原配置。

### 3. 修改默认值时要分清两层

如果要改“默认值”，通常要同时考虑：

- 代码里的默认值
- 仓库里的 `config.json`
- 服务器当前实际运行的 `config.json`

### 4. 本机不是主运行环境

当前维护约定：

- 本机默认不启动
- 本机只负责改代码、提交、发布、校验
- 真正运行环境是 HF 和 Ubuntu 服务器

## 后续建议

如果后面继续维护这个项目，建议 README 长期保持同步更新以下内容：

- 当前默认值
- 当前部署地址
- 当前服务器路径
- 当前发布方式
- 当前不提交的敏感文件列表

这样别人接手时，不需要再重新摸索部署链路。
