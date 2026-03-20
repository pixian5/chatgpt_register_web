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

## 启动流程

### 1. 进入项目目录

```bash
cd /Applications/chatgpt_register_web
```

### 2. 激活虚拟环境

```bash
source .venv/bin/activate
```

### 3. 安装依赖

```bash
python -m pip install -r requirements.txt
```

### 4. 启动服务（推荐方式）

```bash
python -m uvicorn web_app:app --host 0.0.0.0 --port 52789 --reload
```

启动成功后浏览器访问：

```
http://127.0.0.1:52789
```

## 停止服务

在运行的终端里按 `Ctrl + C` 即可停止。
