"""
web_app.py - pam管理 Web UI 后端

FastAPI 主程序：
- REST API：配置、注册、账号池、代理、结果查看
- WebSocket：注册日志、池管理日志实时推送

启动：
    uv run uvicorn web_app:app --host 0.0.0.0 --port 52789 --reload
    或 python web_app.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from config_runtime import mask_config_secrets, restore_masked_secrets
import register as reg

# ============================================================
# 应用初始化
# ============================================================

app = FastAPI(title="pam管理 Web UI", version="1.0.4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _BASE_DIR / "templates"

# ============================================================
# 全局任务状态
# ============================================================

# 注册任务
_reg_state: Dict[str, Any] = {
    "running": False,
    "success": 0,
    "fail": 0,
    "total": 0,
    "stop_event": None,
    "start_time": None,
}
_reg_log_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
_reg_ws_clients: List[WebSocket] = []

# 账号池任务
_pool_state: Dict[str, Any] = {
    "running": False,
    "task": "",
    "stop_event": None,
    "stop_requested": False,
}
_shared_reg_state: Dict[str, Any] = {
    "mode": "",
    "running": False,
    "success": 0,
    "fail": 0,
    "total": 0,
}
_pool_log_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
_pool_ws_clients: List[WebSocket] = []
_POOL_LOG_HISTORY_MAX = 500
_pool_log_history: List[Dict[str, Any]] = []
_pool_log_seq = 0
_pool_log_lock = threading.Lock()

_PROBE_RESULT_TTL_SEC = 120

# 事件循环引用
_event_loop: Optional[asyncio.AbstractEventLoop] = None

_DEFAULT_POOL_RUNTIME_CONFIG: Dict[str, Any] = {
    "base_url": "",
    "token": "",
    "target_type": reg.DEFAULT_POOL_TARGET_TYPE,
    "target_count": reg.DEFAULT_POOL_TARGET_COUNT,
    "proxy": "",
}
_MASKED_SECRET = "********"


@app.on_event("startup")
async def _startup():
    global _event_loop
    _event_loop = asyncio.get_event_loop()
    config = reg.load_config()
    pool = config.get("pool", {})
    base_url = str(pool.get("base_url", "")).strip()
    token = str(pool.get("token", "")).strip()
    if base_url and token:
        _start_pool_daemon(pool)


def _to_int(value: Any, fallback: int, minimum: Optional[int] = None) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = fallback
    if minimum is not None:
        num = max(minimum, num)
    return num


def _normalize_pool_runtime_config(
    source: Optional[dict] = None,
    fallback: Optional[dict] = None,
) -> Dict[str, Any]:
    merged = dict(_DEFAULT_POOL_RUNTIME_CONFIG)
    if fallback:
        merged.update(fallback)
    if source:
        merged.update(source)
    return {
        "base_url": str(merged.get("base_url", "")).strip(),
        "token": str(merged.get("token", "")).strip(),
        "target_type": str(merged.get("target_type") or reg.DEFAULT_POOL_TARGET_TYPE).strip() or reg.DEFAULT_POOL_TARGET_TYPE,
        "target_count": _to_int(merged.get("target_count"), reg.DEFAULT_POOL_TARGET_COUNT, minimum=0),
        "proxy": str(merged.get("proxy", "")).strip(),
    }


def _pick_runtime_value(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text or text == _MASKED_SECRET:
        return str(fallback or "").strip()
    return text


def _resolve_pool_request_config(source: Optional[dict] = None) -> Dict[str, Any]:
    cfg = reg.load_config()
    pool = cfg.get("pool", {}) if isinstance(cfg.get("pool"), dict) else {}
    source = source or {}
    return {
        "base_url": _pick_runtime_value(source.get("base_url"), pool.get("base_url", "")),
        "token": _pick_runtime_value(source.get("token"), pool.get("token", "")),
        "target_type": _pick_runtime_value(source.get("target_type"), pool.get("target_type", reg.DEFAULT_POOL_TARGET_TYPE)) or reg.DEFAULT_POOL_TARGET_TYPE,
        "target_count": _to_int(source.get("target_count", pool.get("target_count", reg.DEFAULT_POOL_TARGET_COUNT)), reg.DEFAULT_POOL_TARGET_COUNT, minimum=0),
        "proxy": _pick_runtime_value(source.get("proxy"), pool.get("proxy", "")),
    }


# ============================================================
# 辅助：线程安全地推送日志到队列
# ============================================================

def _push_log_sync(queue: asyncio.Queue, msg: str):
    """从同步线程将日志推入 asyncio Queue"""
    if _event_loop and not _event_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(queue.put(msg), _event_loop)
        except Exception:
            pass


def _make_reg_log_cb():
    return lambda msg: _push_log_sync(_reg_log_queue, msg)


def _push_pool_log_sync(msg: str):
    global _pool_log_seq
    with _pool_log_lock:
        _pool_log_seq += 1
        entry = {"seq": _pool_log_seq, "msg": str(msg)}
        _pool_log_history.append(entry)
        if len(_pool_log_history) > _POOL_LOG_HISTORY_MAX:
            del _pool_log_history[:-_POOL_LOG_HISTORY_MAX]
    _push_log_sync(_pool_log_queue, json.dumps(entry, ensure_ascii=False))


def _make_pool_log_cb():
    return _push_pool_log_sync


def _reset_pool_logs() -> None:
    global _pool_log_seq
    with _pool_log_lock:
        _pool_log_history.clear()
        _pool_log_seq = 0
    try:
        while True:
            _pool_log_queue.get_nowait()
    except asyncio.QueueEmpty:
        pass


def _run_post_stop_reconcile(
    *,
    base_url: str,
    token: str,
    target_type: str,
    target_count: int,
    proxy: str,
    log_cb,
) -> Dict[str, Any]:
    """停止补号后执行一次收尾：校验401、清理并双向同步。"""
    if not base_url or not token:
        log_cb("[Pool] 停止后收尾跳过：base_url 或 token 为空")
        return {"ok": False, "error": "missing base_url/token"}

    cfg = reg.load_config()
    log_cb("[Pool] 停止补号后开始自动校验 401 并双向同步有效账号...")

    refresh_result = reg.run_pool_refresh_status(
        base_url=base_url,
        token=token,
        target_type=target_type,
        target_count=target_count,
        proxy=proxy,
        log_cb=log_cb,
        config=cfg,
    )
    if not refresh_result.get("ok"):
        log_cb(f"[Pool] 停止后校验失败: {refresh_result.get('error', '未知错误')}")
        return refresh_result

    sync_result = reg.sync_local_remote(
        base_url=base_url,
        token=token,
        target_type=target_type,
        config=cfg,
        proxy=proxy,
        log_cb=log_cb,
        target_count=target_count,
    )
    if sync_result.get("ok"):
        log_cb(
            f"[Pool] 停止后同步完成: 上传={sync_result.get('uploaded', 0)}, "
            f"移动={sync_result.get('moved', 0)}, 下载={sync_result.get('downloaded', 0)}"
        )
    else:
        log_cb(f"[Pool] 停止后同步失败: {sync_result.get('error', '未知错误')}")

    return {
        "ok": bool(sync_result.get("ok")),
        "refresh": refresh_result,
        "sync": sync_result,
    }


def _token_fingerprint(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _build_probe_signature(base_url: str, token: str, target_type: str, proxy: str) -> str:
    raw = "|".join([
        str(base_url or "").strip().rstrip("/"),
        str(target_type or "").strip().lower(),
        _token_fingerprint(str(token or "").strip()),
        str(proxy or "").strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_reg_progress_cb():
    def cb(success: int, fail: int, total: int):
        _reg_state["success"] = success
        _reg_state["fail"] = fail
        _reg_state["total"] = total
    return cb


def _set_shared_reg_state(mode: str = "", running: bool = False, success: int = 0, fail: int = 0, total: int = 0):
    _shared_reg_state.update({
        "mode": str(mode or ""),
        "running": bool(running),
        "success": int(success),
        "fail": int(fail),
        "total": int(total),
    })


def _make_shared_reg_progress_cb(mode: str):
    def cb(success: int, fail: int, total: int):
        _set_shared_reg_state(mode=mode, running=True, success=success, fail=fail, total=total)
    return cb


def _finish_shared_reg_state(mode: str, result: Optional[dict] = None):
    result = result or {}
    _set_shared_reg_state(
        mode=mode,
        running=False,
        success=result.get("success", _shared_reg_state.get("success", 0)),
        fail=result.get("fail", _shared_reg_state.get("fail", 0)),
        total=result.get("total", _shared_reg_state.get("total", 0)),
    )


# ============================================================
# 静态文件 & 首页
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = _TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html 不存在")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ============================================================
# 配置 API
# ============================================================

@app.get("/api/config")
async def get_config():
    return mask_config_secrets(reg.load_config())


@app.post("/api/config")
async def save_config(config: dict = Body(...)):
    # 移除 _comment 字段保护
    config.pop("_comment", None)
    current = reg.load_config()
    merged = restore_masked_secrets(config, current)
    ok = reg.save_config(merged)
    if not ok:
        raise HTTPException(status_code=500, detail="保存失败")

    # 如果守护进程已启用，同步更新其运行时配置（下次周期生效）
    pool = merged.get("pool", {})
    if _pool_daemon["enabled"] and pool:
        _pool_daemon["config"] = _normalize_pool_runtime_config(pool, _pool_daemon["config"])
        _pool_daemon["interval_min"] = _to_int(
            pool.get("interval_min"),
            _pool_daemon["interval_min"],
            minimum=1,
        )
        _reschedule_pool_daemon_timer()

    return {"ok": True}


# ============================================================
# 注册任务 API
# ============================================================

@app.post("/api/register/start")
async def register_start(body: dict = Body(...)):
    if _reg_state["running"]:
        raise HTTPException(status_code=409, detail="已有注册任务运行中")

    config = reg.load_config()
    count = _to_int(body.get("count", 1), 1)
    workers = _to_int(body.get("workers") or config.get("workers") or reg.DEFAULT_WORKERS, reg.DEFAULT_WORKERS)
    proxy = str(body.get("proxy", "")).strip()

    if count <= 0:
        raise HTTPException(status_code=400, detail="count 必须大于 0")
    if workers <= 0:
        raise HTTPException(status_code=400, detail="workers 必须大于 0")

    # 重置状态
    _reg_state.update({
        "running": True,
        "success": 0,
        "fail": 0,
        "total": count,
        "start_time": time.time(),
    })
    stop_event = threading.Event()
    _reg_state["stop_event"] = stop_event

    log_cb = _make_reg_log_cb()
    progress_cb = _make_reg_progress_cb()

    def run_task():
        try:
            result = reg.run_batch_register(
                count=count,
                workers=workers,
                proxy=proxy,
                stop_event=stop_event,
                log_cb=log_cb,
                progress_cb=progress_cb,
                config=config,
            )
            _reg_state.update(result)
        except Exception as e:
            log_cb(f"[ERROR] 注册任务异常: {e}")
        finally:
            _reg_state["running"] = False
            _reg_state["stop_event"] = None
            log_cb("[注册] 任务结束")

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()

    return {"ok": True, "count": count, "workers": workers}


@app.post("/api/register/stop")
async def register_stop():
    stop_event = _reg_state.get("stop_event")
    if stop_event:
        stop_event.set()
    return {"ok": True}


@app.get("/api/register/status")
async def register_status():
    elapsed = None
    if _reg_state.get("start_time"):
        elapsed = int(time.time() - _reg_state["start_time"])
    return {
        "running": _reg_state["running"],
        "success": _reg_state["success"],
        "fail": _reg_state["fail"],
        "total": _reg_state["total"],
        "elapsed": elapsed,
    }


# ============================================================
# 注册日志 WebSocket
# ============================================================

@app.websocket("/ws/register/logs")
async def ws_register_logs(ws: WebSocket):
    await ws.accept()
    _reg_ws_clients.append(ws)
    try:
        while True:
            # 从队列取日志并推送
            try:
                msg = await asyncio.wait_for(_reg_log_queue.get(), timeout=1.0)
                for client in list(_reg_ws_clients):
                    try:
                        await client.send_text(json.dumps({"type": "log", "msg": msg}))
                    except Exception:
                        _reg_ws_clients.discard(client) if hasattr(_reg_ws_clients, 'discard') else None
            except asyncio.TimeoutError:
                # 发送心跳
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _reg_ws_clients:
            _reg_ws_clients.remove(ws)


# ============================================================
# 账号池 API
# ============================================================

@app.post("/api/pool/probe")
async def pool_probe(body: dict = Body(...)):
    if _pool_state["running"]:
        raise HTTPException(status_code=409, detail="已有池任务运行中")

    pool_req = _resolve_pool_request_config(body)
    base_url = pool_req["base_url"]
    token = pool_req["token"]
    target_type = pool_req["target_type"]
    proxy = pool_req["proxy"]

    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")

    _reset_pool_logs()
    _pool_state["running"] = True
    _pool_state["task"] = "probe"
    log_cb = _make_pool_log_cb()

    def run_task():
        try:
            cfg = reg.load_config()
            result = reg.run_pool_probe(base_url, token, target_type, proxy, log_cb=log_cb, config=cfg)
            log_cb(
                f"[Pool] 探测结果: 远程总={result.get('total')}, 远程目标={result.get('target')}, "
                f"本地目标={result.get('local_total', 0)}, 远程401={result.get('remote_invalid_count', 0)}, "
                f"本地401={result.get('local_invalid_count', 0)}, 合并401={result.get('invalid_count')}"
            )
        except Exception as e:
            log_cb(f"[ERROR] 探测异常: {e}")
        finally:
            _pool_state["running"] = False

    threading.Thread(target=run_task, daemon=True).start()
    return {"ok": True, "task": "probe"}


@app.post("/api/pool/clean")
async def pool_clean(body: dict = Body(...)):
    if _pool_state["running"]:
        raise HTTPException(status_code=409, detail="已有池任务运行中")

    pool_req = _resolve_pool_request_config(body)
    base_url = pool_req["base_url"]
    token = pool_req["token"]
    target_type = pool_req["target_type"]
    proxy = pool_req["proxy"]
    probe_result = body.get("probe_result")
    probe_signature = str(body.get("probe_signature", "") or "")
    probe_ts_raw = body.get("probe_ts")

    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")

    _reset_pool_logs()
    _pool_state["running"] = True
    _pool_state["task"] = "clean"
    log_cb = _make_pool_log_cb()

    def run_task():
        try:
            cfg = reg.load_config()
            now_ts = int(time.time())
            expected_signature = _build_probe_signature(base_url, token, target_type, proxy)
            use_cached_probe = False
            if isinstance(probe_result, dict):
                try:
                    probe_ts = int(probe_ts_raw)
                except Exception:
                    probe_ts = 0
                is_fresh = probe_ts > 0 and (now_ts - probe_ts) <= _PROBE_RESULT_TTL_SEC
                signature_ok = bool(probe_signature) and probe_signature == expected_signature
                invalid_list_ok = isinstance(probe_result.get("invalid_401"), list)
                use_cached_probe = is_fresh and signature_ok and invalid_list_ok

            if use_cached_probe:
                log_cb("[Pool] 复用检查结果执行清理，跳过重复探测")
                result = reg.run_pool_clean_with_probe_result(
                    base_url=base_url,
                    token=token,
                    probe_result=probe_result,
                    proxy=proxy,
                    log_cb=log_cb,
                    config=cfg,
                )
            else:
                if isinstance(probe_result, dict):
                    log_cb("[Pool] 检查结果不可用或已过期，回退为重新探测后清理")
                result = reg.run_pool_clean(base_url, token, target_type, proxy, log_cb=log_cb, config=cfg)
            log_cb(
                f"[Pool] 清理完成: 远端删除={result.get('deleted')}, 本地删除={result.get('local_deleted', 0)}, "
                f"远端失败={result.get('delete_fail')}, 本地失败={result.get('local_delete_fail', 0)}"
            )
        except Exception as e:
            log_cb(f"[ERROR] 清理异常: {e}")
        finally:
            _pool_state["running"] = False

    threading.Thread(target=run_task, daemon=True).start()
    return {"ok": True, "task": "clean"}


@app.post("/api/pool/fill")
async def pool_fill(body: dict = Body(...)):
    if _pool_state["running"]:
        raise HTTPException(status_code=409, detail="已有池任务运行中")

    count = _to_int(body.get("count", 1), 1)
    pool_req = _resolve_pool_request_config(body)
    base_url = pool_req["base_url"]
    pool_token = pool_req["token"]
    proxy = pool_req["proxy"]
    target_type = pool_req["target_type"]
    target_count = _to_int(body.get("target_count", 0), 0, minimum=0)
    config = reg.load_config()

    if count <= 0:
        raise HTTPException(status_code=400, detail="count 必须大于 0")

    _reset_pool_logs()
    _pool_state["running"] = True
    _pool_state["task"] = "fill"
    _pool_state["stop_requested"] = False
    _set_shared_reg_state(mode="manual_fill", running=True, success=0, fail=0, total=count)
    log_cb = _make_pool_log_cb()
    stop_event = threading.Event()
    _pool_state["stop_event"] = stop_event

    progress_cb = _make_shared_reg_progress_cb("manual_fill")

    def run_task():
        stop_requested = False
        try:
            result = reg.run_pool_fill(
                fill_count=count,
                base_url=base_url,
                pool_token=pool_token,
                stop_event=stop_event,
                log_cb=log_cb,
                progress_cb=progress_cb,
                config=config,
                proxy=proxy,
                target_count=target_count,
                target_type=target_type,
            )
            _set_shared_reg_state(
                mode="manual_fill",
                running=False,
                success=result.get("success", 0),
                fail=result.get("fail", 0),
                total=result.get("total", count),
            )
            log_cb(f"[Pool] 补号完成: 成功={result.get('success')}, 失败={result.get('fail')}")
        except Exception as e:
            _set_shared_reg_state(mode="manual_fill", running=False)
            log_cb(f"[ERROR] 补号异常: {e}")
        finally:
            stop_requested = bool(_pool_state.get("stop_requested"))
            if stop_requested:
                try:
                    _run_post_stop_reconcile(
                        base_url=base_url,
                        token=pool_token,
                        target_type=target_type,
                        target_count=target_count,
                        proxy=proxy,
                        log_cb=log_cb,
                    )
                except Exception as reconcile_error:
                    log_cb(f"[Pool] 停止后自动收尾异常: {reconcile_error}")
            _pool_state["running"] = False
            _pool_state["task"] = ""
            _pool_state["stop_requested"] = False
            _pool_state.pop("stop_event", None)

    threading.Thread(target=run_task, daemon=True).start()
    return {"ok": True, "task": "fill", "count": count}


@app.post("/api/pool/status")
async def pool_status_api(body: dict = Body(...)):
    pool_req = _resolve_pool_request_config(body)
    base_url = pool_req["base_url"]
    token = pool_req["token"]
    target_type = pool_req["target_type"]
    target_count = _to_int(body.get("target_count", pool_req["target_count"]), reg.DEFAULT_POOL_TARGET_COUNT, minimum=0)
    proxy = pool_req["proxy"]

    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")

    cfg = reg.load_config()
    log_cb = _make_pool_log_cb()
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: reg.run_pool_refresh_status(
            base_url=base_url,
            token=token,
            target_type=target_type,
            target_count=target_count,
            proxy=proxy,
            log_cb=log_cb,
            config=cfg,
        ),
    )
    return result


@app.get("/api/pool/accounts")
async def pool_accounts(base_url: str, token: str,
                        target_type: str = reg.DEFAULT_POOL_TARGET_TYPE, proxy: str = ""):
    pool_req = _resolve_pool_request_config({
        "base_url": base_url,
        "token": token,
        "target_type": target_type,
        "proxy": proxy,
    })
    base_url = pool_req["base_url"]
    token = pool_req["token"]
    target_type = pool_req["target_type"]
    proxy = pool_req["proxy"]
    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: reg.get_pool_accounts(base_url, token, target_type, proxy)
    )
    return result


@app.get("/api/pool/sync-status")
async def pool_sync_status(base_url: str, token: str,
                           target_type: str = reg.DEFAULT_POOL_TARGET_TYPE, proxy: str = ""):
    pool_req = _resolve_pool_request_config({
        "base_url": base_url,
        "token": token,
        "target_type": target_type,
        "proxy": proxy,
    })
    base_url = pool_req["base_url"]
    token = pool_req["token"]
    target_type = pool_req["target_type"]
    proxy = pool_req["proxy"]
    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: reg.get_sync_status(base_url, token, target_type, proxy=proxy)
    )
    return result


@app.post("/api/pool/sync")
async def pool_sync(body: dict = Body(...)):
    pool_req = _resolve_pool_request_config(body)
    base_url = pool_req["base_url"]
    token = pool_req["token"]
    target_type = pool_req["target_type"]
    proxy = pool_req["proxy"]
    target_count = int(body.get("target_count", 0))

    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")

    _reset_pool_logs()
    log_cb = _make_pool_log_cb()
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: reg.sync_local_remote(base_url, token, target_type, proxy=proxy, log_cb=log_cb, target_count=target_count)
    )
    return result


@app.get("/api/pool/task-status")
async def pool_task_status():
    return {
        "running": _pool_state["running"],
        "task": _pool_state.get("task", ""),
        "stop_requested": _pool_state.get("stop_requested", False),
    }


@app.get("/api/pool/reg-stats")
async def pool_reg_stats():
    return {
        "mode": _shared_reg_state.get("mode", ""),
        "running": _shared_reg_state.get("running", False),
        "success": _shared_reg_state.get("success", 0),
        "fail": _shared_reg_state.get("fail", 0),
        "total": _shared_reg_state.get("total", 0),
    }


@app.post("/api/pool/stop")
async def pool_stop():
    manual_stopped = False
    daemon_stopped = False

    manual_stop_event = _pool_state.get("stop_event")
    if _pool_state.get("running") and _pool_state.get("task") == "fill" and manual_stop_event:
        manual_stop_event.set()
        _pool_state["stop_requested"] = True
        manual_stopped = True
        _make_pool_log_cb()("[Pool] 已请求停止当前补号任务")

    daemon_stop_event = _pool_daemon.get("stop_event")
    if _pool_daemon.get("running_now") and daemon_stop_event:
        daemon_stop_event.set()
        _pool_daemon["stop_requested"] = True
        daemon_stopped = True
        _make_pool_log_cb()("[Daemon] 已请求停止当前补号周期")

    return {"ok": True, "manual_stopped": manual_stopped, "daemon_stopped": daemon_stopped}


@app.post("/api/pool/inspect")
async def pool_inspect(body: dict = Body(...)):
    """执行一轮完整校验与401清理，返回最新状态（供前端渲染确认卡）"""
    pool_req = _resolve_pool_request_config(body)
    base_url = pool_req["base_url"]
    token = pool_req["token"]
    target_type = pool_req["target_type"]
    target_count = _to_int(body.get("target_count", pool_req["target_count"]), reg.DEFAULT_POOL_TARGET_COUNT, minimum=0)
    proxy = pool_req["proxy"]

    if not base_url or not token:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")

    _reset_pool_logs()
    log_cb = _make_pool_log_cb()
    loop = asyncio.get_event_loop()
    cfg = reg.load_config()
    result = await loop.run_in_executor(
        None,
        lambda: reg.run_pool_refresh_status(
            base_url=base_url,
            token=token,
            target_type=target_type,
            target_count=target_count,
            proxy=proxy,
            log_cb=log_cb,
            config=cfg,
        ),
    )

    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "校验失败"))

    return result


# ============================================================
# 池管理日志 WebSocket
# ============================================================

@app.websocket("/ws/pool/logs")
async def ws_pool_logs(ws: WebSocket):
    await ws.accept()
    _pool_ws_clients.append(ws)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(_pool_log_queue.get(), timeout=1.0)
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"seq": None, "msg": str(raw)}
                for client in list(_pool_ws_clients):
                    try:
                        await client.send_text(json.dumps({"type": "log", **payload}, ensure_ascii=False))
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _pool_ws_clients:
            _pool_ws_clients.remove(ws)


@app.get("/api/pool/logs")
async def pool_logs(after: int = 0, limit: int = 200):
    limit = max(1, min(int(limit), _POOL_LOG_HISTORY_MAX))
    after = max(0, int(after))
    with _pool_log_lock:
        items = [dict(item) for item in _pool_log_history if int(item.get("seq", 0)) > after]
        if len(items) > limit:
            items = items[-limit:]
        last_seq = _pool_log_seq
    return {"ok": True, "items": items, "last_seq": last_seq}


@app.get("/api/pool/logs/cursor")
async def pool_logs_cursor():
    with _pool_log_lock:
        last_seq = _pool_log_seq
    return {"ok": True, "last_seq": last_seq}


# ============================================================
# 代理管理 API
# ============================================================

@app.get("/api/proxy/fetch")
async def proxy_fetch():
    config = reg.load_config()
    fallback_proxy = config.get("proxy", "")
    proxies = await asyncio.get_event_loop().run_in_executor(
        None, lambda: reg.fetch_free_proxies(proxy=fallback_proxy)
    )
    return {"ok": True, "proxies": proxies, "count": len(proxies)}


@app.post("/api/proxy/test")
async def proxy_test(body: dict = Body(...)):
    proxies = body.get("proxies", [])
    target_url = body.get("target_url", "https://httpbin.org/ip")
    timeout = int(body.get("timeout", 5))
    config = reg.load_config()
    max_workers = int(body.get("workers") or config.get("proxy_test_workers") or reg.DEFAULT_PROXY_TEST_WORKERS)

    if not proxies:
        raise HTTPException(status_code=400, detail="proxies 不能为空")
    if len(proxies) > 50:
        proxies = proxies[:50]

    results = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: reg.test_proxies_concurrent(proxies, target_url, timeout, max_workers=max_workers),
    )
    return {"ok": True, "results": results}


# ============================================================
# 结果查看 API
# ============================================================

@app.get("/api/results")
async def get_results():
    config = reg.load_config()
    accounts = reg.read_registered_accounts(config)
    return {"ok": True, "accounts": accounts, "count": len(accounts)}


@app.get("/api/tokens")
async def get_tokens():
    config = reg.load_config()
    tokens = reg.list_codex_tokens(config)
    return {"ok": True, "tokens": tokens, "count": len(tokens)}


@app.get("/api/tokens/ak")
async def get_ak():
    config = reg.load_config()
    content = reg.read_token_file("ak.txt", config)
    return PlainTextResponse(content=content)


@app.get("/api/tokens/rk")
async def get_rk():
    config = reg.load_config()
    content = reg.read_token_file("rk.txt", config)
    return PlainTextResponse(content=content)


@app.get("/api/tokens/download/ak")
async def download_ak():
    config = reg.load_config()
    ak_file = config.get("ak_file", "ak.txt")
    if not os.path.isabs(ak_file):
        ak_file = str(_BASE_DIR / ak_file)
    if not os.path.exists(ak_file):
        raise HTTPException(status_code=404, detail="ak.txt 不存在")
    return FileResponse(ak_file, filename="ak.txt", media_type="text/plain")


@app.get("/api/tokens/download/rk")
async def download_rk():
    config = reg.load_config()
    rk_file = config.get("rk_file", "rk.txt")
    if not os.path.isabs(rk_file):
        rk_file = str(_BASE_DIR / rk_file)
    if not os.path.exists(rk_file):
        raise HTTPException(status_code=404, detail="rk.txt 不存在")
    return FileResponse(rk_file, filename="rk.txt", media_type="text/plain")


# ============================================================
# 号池守护进程
# ============================================================

_pool_daemon: Dict[str, Any] = {
    "enabled": False,
    "interval_min": reg.DEFAULT_POOL_INTERVAL_MIN,
    "next_run_ts": None,
    "last_run_ts": None,
    "running_now": False,
    "stop_event": None,
    "stop_requested": False,
    "config": dict(_DEFAULT_POOL_RUNTIME_CONFIG),  # {base_url, token, target_type, target_count, proxy}
}
_pool_daemon_timer: Optional[threading.Timer] = None


def _persist_pool_interval(interval_min: int):
    cfg = reg.load_config()
    pool = cfg.get("pool")
    if not isinstance(pool, dict):
        pool = {}
        cfg["pool"] = pool
    pool["interval_min"] = max(1, int(interval_min))
    reg.save_config(cfg)


def _reschedule_pool_daemon_timer():
    global _pool_daemon_timer

    if _pool_daemon_timer and _pool_daemon_timer.is_alive():
        _pool_daemon_timer.cancel()
        _pool_daemon_timer = None

    if not _pool_daemon["enabled"] or _pool_daemon.get("running_now"):
        return

    interval_sec = max(1, int(_pool_daemon["interval_min"])) * 60
    _pool_daemon["next_run_ts"] = time.time() + interval_sec
    _pool_daemon_timer = threading.Timer(interval_sec, _run_daemon_once)
    _pool_daemon_timer.daemon = True
    _pool_daemon_timer.start()


def _start_pool_daemon(source: Optional[dict] = None):
    global _pool_daemon_timer

    cfg = _normalize_pool_runtime_config(source, _pool_daemon["config"])
    if not cfg["base_url"] or not cfg["token"]:
        raise HTTPException(status_code=400, detail="base_url 和 token 不能为空")

    # 停止旧 timer
    if _pool_daemon_timer and _pool_daemon_timer.is_alive():
        _pool_daemon_timer.cancel()
        _pool_daemon_timer = None

    interval_min = _to_int(
        (source or {}).get("interval_min"),
        _pool_daemon["interval_min"],
        minimum=1,
    )
    _pool_daemon.update({
        "enabled": True,
        "interval_min": interval_min,
        "last_run_ts": time.time(),
        "stop_requested": False,
        "config": cfg,
    })

    # 立即执行一次（在后台线程）
    _pool_daemon["next_run_ts"] = None
    t = threading.Thread(target=_run_daemon_once, daemon=True)
    t.start()
    return interval_min


def _run_daemon_once():
    """守护进程单次执行"""
    global _pool_daemon_timer

    if not _pool_daemon["enabled"]:
        return

    _pool_daemon["running_now"] = True
    _pool_daemon["last_run_ts"] = time.time()
    _pool_daemon["stop_requested"] = False
    _set_shared_reg_state()
    log_cb = _make_pool_log_cb()
    reg_result: Optional[dict] = None
    cfg = _normalize_pool_runtime_config(_pool_daemon["config"])
    stop_requested = False
    try:
        proxy = reg._proxy_pool.get_best(cfg["proxy"])
        stop_event = threading.Event()
        _pool_daemon["stop_event"] = stop_event
        reg_result = reg.run_pool_maintain_cycle(
            base_url=cfg["base_url"],
            token=cfg["token"],
            target_type=cfg["target_type"],
            target_count=cfg["target_count"],
            stop_event=stop_event,
            log_cb=log_cb,
            progress_cb=_make_shared_reg_progress_cb("daemon"),
            config=reg.load_config(),
            proxy=proxy,
        )
    except Exception as e:
        log_cb(f"[Daemon] 执行异常: {e}")
    finally:
        stop_requested = bool(_pool_daemon.get("stop_requested"))
        if stop_requested:
            try:
                _run_post_stop_reconcile(
                    base_url=cfg["base_url"],
                    token=cfg["token"],
                    target_type=cfg["target_type"],
                    target_count=cfg["target_count"],
                    proxy=cfg["proxy"],
                    log_cb=log_cb,
                )
            except Exception as reconcile_error:
                log_cb(f"[Daemon] 停止后自动收尾异常: {reconcile_error}")
        _finish_shared_reg_state("daemon", reg_result)
        _pool_daemon["running_now"] = False
        _pool_daemon["stop_event"] = None
        _pool_daemon["stop_requested"] = False
        if _pool_daemon["enabled"]:
            interval_sec = _pool_daemon["interval_min"] * 60
            _pool_daemon["next_run_ts"] = time.time() + interval_sec
            _pool_daemon_timer = threading.Timer(interval_sec, _run_daemon_once)
            _pool_daemon_timer.daemon = True
            _pool_daemon_timer.start()


@app.post("/api/pool/daemon/start")
async def pool_daemon_start(body: dict = Body(...)):
    daemon_body = dict(body or {})
    daemon_body.update(_resolve_pool_request_config(body))
    _reset_pool_logs()
    interval_min = _start_pool_daemon(daemon_body)
    _persist_pool_interval(interval_min)
    return {"ok": True, "interval_min": interval_min}


@app.post("/api/pool/daemon/stop")
async def pool_daemon_stop():
    global _pool_daemon_timer

    _pool_daemon["enabled"] = False
    _pool_daemon["next_run_ts"] = None
    _pool_daemon["stop_requested"] = bool(_pool_daemon.get("running_now"))
    if _pool_daemon_timer and _pool_daemon_timer.is_alive():
        _pool_daemon_timer.cancel()
        _pool_daemon_timer = None
    stop_event = _pool_daemon.get("stop_event")
    if stop_event:
        stop_event.set()

    return {"ok": True}


@app.get("/api/pool/daemon/status")
async def pool_daemon_status():
    remaining = None
    if _pool_daemon["next_run_ts"]:
        remaining = max(0, int(_pool_daemon["next_run_ts"] - time.time()))
    return {
        "enabled": _pool_daemon["enabled"],
        "running_now": _pool_daemon["running_now"],
        "interval_min": _pool_daemon["interval_min"],
        "next_run_ts": _pool_daemon["next_run_ts"],
        "last_run_ts": _pool_daemon["last_run_ts"],
        "remaining_sec": remaining,
        "stop_requested": _pool_daemon["stop_requested"],
        "config": _pool_daemon["config"],
    }


@app.post("/api/pool/daemon/run-once")
async def pool_daemon_run_once(body: dict = Body(default={})):
    """立即触发一次维护周期（不影响守护进程定时器）"""
    cfg = _normalize_pool_runtime_config(_pool_daemon["config"])
    # 允许临时覆盖配置
    if body.get("base_url"):
        cfg = _normalize_pool_runtime_config(body, cfg)

    base_url = cfg["base_url"]
    token = cfg["token"]
    if not base_url or not token:
        raise HTTPException(status_code=400, detail="请先配置 base_url 和 token")

    if _pool_daemon["running_now"]:
        raise HTTPException(status_code=409, detail="守护进程正在运行中")

    _reset_pool_logs()
    log_cb = _make_pool_log_cb()

    def run_task():
        _pool_daemon["running_now"] = True
        _pool_daemon["last_run_ts"] = time.time()
        _pool_daemon["stop_requested"] = False
        _set_shared_reg_state()
        reg_result: Optional[dict] = None
        try:
            proxy = reg._proxy_pool.get_best(cfg["proxy"])
            stop_event = threading.Event()
            _pool_daemon["stop_event"] = stop_event
            reg_result = reg.run_pool_maintain_cycle(
                base_url=cfg["base_url"],
                token=cfg["token"],
                target_type=cfg["target_type"],
                target_count=cfg["target_count"],
                stop_event=stop_event,
                log_cb=log_cb,
                progress_cb=_make_shared_reg_progress_cb("daemon"),
                config=reg.load_config(),
                proxy=proxy,
            )
        except Exception as e:
            log_cb(f"[Daemon] 执行异常: {e}")
        finally:
            _finish_shared_reg_state("daemon", reg_result)
            _pool_daemon["running_now"] = False
            _pool_daemon["stop_event"] = None
            _pool_daemon["stop_requested"] = False

    threading.Thread(target=run_task, daemon=True).start()
    return {"ok": True}


# ============================================================
# 代理池端点
# ============================================================

@app.post("/api/proxy/pool/update")
async def proxy_pool_update(body: dict = Body(...)):
    """接收前端测试结果，更新内存代理池"""
    results = body.get("results", [])
    if not isinstance(results, list):
        raise HTTPException(status_code=400, detail="results 必须是列表")
    reg._proxy_pool.update(results)
    best = reg._proxy_pool.get_best()
    return {"ok": True, "count": len(results), "best": best}


@app.get("/api/proxy/active")
async def proxy_active():
    """返回当前最优代理"""
    config = reg.load_config()
    fallback = config.get("proxy", "")
    best = reg._proxy_pool.get_best(fallback)
    source = "free_pool" if reg._proxy_pool.get_best() else ("user_config" if fallback else "direct")
    return {
        "proxy": best,
        "source": source,
        "pool_size": len(reg._proxy_pool.get_all()),
    }


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        "web_app:app",
        host="0.0.0.0",
        port=52789,
        reload=False,
        log_level="info",
    )
