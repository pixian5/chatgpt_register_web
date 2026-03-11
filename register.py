"""
register.py - ChatGPT 注册机 Web UI 后端库

将 chatgpt_register.py 核心逻辑以 library 形式暴露：
- log_callback: 替代 print，将日志路由到调用方
- stop_event: threading.Event，支持中止批量任务
- progress_callback: (success, fail, total) 进度回调
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import os
import sys
import threading
import time
from collections import Counter
from typing import Callable, Dict, List, Optional, Any
from urllib.parse import unquote, quote

import requests as _requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

urllib3.disable_warnings(InsecureRequestWarning)

# ============================================================
# 基础路径
# ============================================================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_token_dir(config: Optional[dict] = None) -> str:
    token_dir = (config or {}).get("token_json_dir", "codex_tokens")
    if not os.path.isabs(token_dir):
        token_dir = os.path.join(_BASE_DIR, token_dir)
    return token_dir


def _normalize_token_name(raw_name: str) -> str:
    base_name = os.path.basename(str(raw_name or ""))
    base_name = unquote(base_name)
    if base_name.endswith(".json"):
        base_name = base_name[:-5]
    return base_name.strip()


# ============================================================
# stdout 捕获 → log_callback
# ============================================================

class _LogCapture(io.RawIOBase):
    """将 sys.stdout 写入重定向到 callback"""

    def __init__(self, callback: Callable[[str], None]):
        self._callback = callback
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:  # type: ignore[override]
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.rstrip("\r")
                if line and self._callback:
                    try:
                        self._callback(line)
                    except Exception:
                        pass
        return len(text)

    def flush(self):
        pass

    def readable(self):
        return False

    def writable(self):
        return True


class _LoggingHandler(logging.Handler):
    """将 logging 模块输出路由到 callback"""

    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self._callback = callback

    def emit(self, record):
        try:
            msg = self.format(record)
            if self._callback:
                self._callback(msg)
        except Exception:
            pass


# ============================================================
# 延迟导入 chatgpt_register（抑制初始化输出）
# ============================================================

_cr_lock = threading.Lock()
_cr = None


def _get_cr():
    """获取 chatgpt_register 模块（懒加载，抑制导入输出）"""
    global _cr
    if _cr is not None:
        return _cr
    with _cr_lock:
        if _cr is not None:
            return _cr
        devnull = open(os.devnull, "w")
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "chatgpt_register",
                os.path.join(_BASE_DIR, "chatgpt_register.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            _cr = mod
        finally:
            sys.stdout = old_stdout
            devnull.close()
    return _cr


def _apply_config(mod, config: dict):
    """将 config dict 覆盖到 chatgpt_register 模块全局变量"""
    if not config:
        return
    if "duckmail_bearer" in config:
        mod.DUCKMAIL_BEARER = config["duckmail_bearer"]
    if "duckmail_api_base" in config:
        mod.DUCKMAIL_API_BASE = config["duckmail_api_base"].rstrip("/")
    if "duckmail_domain" in config:
        mod.DUCKMAIL_DOMAIN = str(config["duckmail_domain"]).strip().lstrip("@")
    if "enable_oauth" in config:
        mod.ENABLE_OAUTH = mod._as_bool(config["enable_oauth"])
    if "oauth_required" in config:
        mod.OAUTH_REQUIRED = mod._as_bool(config["oauth_required"])
    if "oauth_issuer" in config:
        mod.OAUTH_ISSUER = config["oauth_issuer"].rstrip("/")
    if "oauth_client_id" in config:
        mod.OAUTH_CLIENT_ID = config["oauth_client_id"]
    if "oauth_redirect_uri" in config:
        mod.OAUTH_REDIRECT_URI = config["oauth_redirect_uri"]
    if "ak_file" in config:
        ak = config["ak_file"]
        mod.AK_FILE = ak if os.path.isabs(ak) else os.path.join(_BASE_DIR, ak)
    if "rk_file" in config:
        rk = config["rk_file"]
        mod.RK_FILE = rk if os.path.isabs(rk) else os.path.join(_BASE_DIR, rk)
    if "token_json_dir" in config:
        td = config["token_json_dir"]
        mod.TOKEN_JSON_DIR = td if os.path.isabs(td) else os.path.join(_BASE_DIR, td)


# ============================================================
# 批量注册
# ============================================================

def run_batch_register(
    count: int,
    workers: int,
    proxy: str,
    stop_event: threading.Event,
    log_cb: Callable[[str], None],
    progress_cb: Callable[[int, int, int], None],
    config: Optional[dict] = None,
) -> dict:
    """
    批量注册主函数（在线程中运行，通过回调输出日志）

    Returns:
        {"success": int, "fail": int, "total": int}
    """
    mod = _get_cr()
    _apply_config(mod, config or {})

    output_file = (config or {}).get("output_file", "registered_accounts.txt")
    if not os.path.isabs(output_file):
        output_file = os.path.join(_BASE_DIR, output_file)

    effective_proxy = proxy or (config or {}).get("proxy", "") or mod.DEFAULT_PROXY

    success_count = 0
    fail_count = 0
    total = count
    _counter_lock = threading.Lock()

    def register_one(idx: int):
        nonlocal success_count, fail_count

        if stop_event and stop_event.is_set():
            return False, None, "已停止"

        capture = _LogCapture(log_cb)
        old_stdout = sys.stdout
        sys.stdout = capture
        try:
            ok, email, err = mod._register_one(idx, total, effective_proxy, output_file)
        except Exception as e:
            ok, email, err = False, None, str(e)
        finally:
            sys.stdout = old_stdout

        with _counter_lock:
            if ok:
                success_count += 1
            else:
                fail_count += 1
            if progress_cb:
                try:
                    progress_cb(success_count, fail_count, total)
                except Exception:
                    pass

        return ok, email, err

    actual_workers = min(workers, count)
    if log_cb:
        log_cb(f"[注册] 开始批量注册: 数量={count}, 并发={actual_workers}, 代理={effective_proxy or '无'}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = []
        for i in range(1, count + 1):
            if stop_event and stop_event.is_set():
                break
            futures.append(executor.submit(register_one, i))

        for fut in concurrent.futures.as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            try:
                fut.result()
            except Exception as e:
                if log_cb:
                    log_cb(f"[FAIL] 线程异常: {e}")

    if log_cb:
        log_cb(f"[注册] 完成: 成功={success_count}, 失败={fail_count}, 总计={total}")

    return {"success": success_count, "fail": fail_count, "total": total}


# ============================================================
# 账号池管理（直接 HTTP 调用）
# ============================================================

def _pool_session(proxy: str = "", timeout: int = 10) -> _requests.Session:
    s = _requests.Session()
    if proxy:
        p = proxy if "://" in proxy else f"http://{proxy}"
        s.proxies = {"http": p, "https": p}
    s.verify = False
    return s


def get_pool_status(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
) -> dict:
    """获取账号池状态（总数、目标类型数）"""
    url = f"{base_url.rstrip('/')}/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    session = _pool_session(proxy, timeout)
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
        files = raw.get("files", []) if isinstance(raw, dict) else []
        total = len(files)
        target_count = sum(
            1 for f in files
            if (f.get("type") or f.get("typo") or "").lower() == target_type.lower()
        )
        return {"ok": True, "total": total, "target": target_count, "target_type": target_type}
    except Exception as e:
        return {"ok": False, "error": str(e), "total": 0, "target": 0}


def get_pool_accounts(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
) -> dict:
    """返回 CliProxyAPI 上指定类型的账号列表（名称）"""
    url = f"{base_url.rstrip('/')}/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    session = _pool_session(proxy, timeout)
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
        files = raw.get("files", []) if isinstance(raw, dict) else []
        accounts = [f for f in files
                    if (f.get("type") or f.get("typo") or "").lower() == target_type.lower()]
        return {"ok": True, "accounts": accounts, "total": len(accounts)}
    except Exception as e:
        return {"ok": False, "error": str(e), "accounts": []}


def get_sync_status(
    base_url: str,
    pool_token: str,
    target_type: str = "codex",
    config: Optional[dict] = None,
    proxy: str = "",
) -> dict:
    """只读：对比本地文件与远程账号，返回每个账号的同步状态"""
    tokens_dir = os.path.join(_BASE_DIR, "codex_tokens")
    uploaded_dir = os.path.join(tokens_dir, "uploaded")

    # 枚举本地文件
    root_names: set = set()
    if os.path.isdir(tokens_dir):
        for f in os.listdir(tokens_dir):
            if f.endswith(".json") and os.path.isfile(os.path.join(tokens_dir, f)):
                root_names.add(f[:-5])  # 去掉 .json 后缀

    uploaded_names: set = set()
    if os.path.isdir(uploaded_dir):
        for f in os.listdir(uploaded_dir):
            if f.endswith(".json") and os.path.isfile(os.path.join(uploaded_dir, f)):
                uploaded_names.add(f[:-5])

    # 获取远程账号
    remote_result = get_pool_accounts(base_url, pool_token, target_type, proxy)
    if not remote_result.get("ok"):
        return {"ok": False, "error": remote_result.get("error", "获取远程账号失败")}

    remote_accounts_raw = remote_result.get("accounts", [])

    def _strip_json(n: str) -> str:
        return n[:-5] if n.endswith(".json") else n

    remote_names: set = {_strip_json(a["name"]) for a in remote_accounts_raw if a.get("name")}
    remote_by_name: dict = {_strip_json(a["name"]): a for a in remote_accounts_raw if a.get("name")}

    all_names = root_names | uploaded_names | remote_names

    STATUS_ORDER = {"pending_move": 0, "pending_upload": 1, "remote_only": 2, "synced": 3, "local_only": 4}

    accounts = []
    summary = {"synced": 0, "pending_upload": 0, "pending_move": 0, "remote_only": 0, "local_only": 0}

    for name in all_names:
        in_root = name in root_names
        in_uploaded = name in uploaded_names
        in_remote = name in remote_names

        if in_uploaded and in_remote:
            status = "synced"
            location = "uploaded"
        elif in_root and not in_remote:
            status = "pending_upload"
            location = "root"
        elif in_root and in_remote:
            status = "pending_move"
            location = "root"
        elif in_remote and not in_uploaded and not in_root:
            status = "remote_only"
            location = "remote"
        else:  # in_uploaded and not in_remote
            status = "local_only"
            location = "uploaded"

        summary[status] = summary.get(status, 0) + 1
        accounts.append({
            "name": name,
            "status": status,
            "type": target_type,
            "location": location,
        })

    accounts.sort(key=lambda x: STATUS_ORDER.get(x["status"], 99))
    return {"ok": True, "accounts": accounts, "summary": summary}


def sync_local_remote(
    base_url: str,
    pool_token: str,
    target_type: str = "codex",
    config: Optional[dict] = None,
    proxy: str = "",
    log_cb: Optional[Callable[[str], None]] = None,
    target_count: int = 0,
    upload_only: bool = False,
) -> dict:
    """同步本地与远程：移动根目录中远程已有文件，按目标数补齐上传/下载账号数据
    upload_only=True 时跳过 remote_only 下载，仅上传本地存量（补号场景使用）"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    tokens_dir = os.path.join(_BASE_DIR, "codex_tokens")
    uploaded_dir = os.path.join(tokens_dir, "uploaded")
    os.makedirs(uploaded_dir, exist_ok=True)

    sync_result = get_sync_status(base_url, pool_token, target_type, config, proxy)
    if not sync_result.get("ok"):
        return {"ok": False, "error": sync_result.get("error", "获取同步状态失败"), "moved": 0, "downloaded": 0, "uploaded": 0, "errors": []}

    summary = sync_result.get("summary", {})
    current_remote = summary.get("synced", 0) + summary.get("pending_move", 0) + summary.get("remote_only", 0)
    gap = max(0, target_count - current_remote) if target_count > 0 else None
    log(f"[同步] 远程当前 {current_remote} 个，目标 {target_count}，待上传缺口 {gap if gap is not None else '无限制'}")

    session = _pool_session(proxy)
    headers = {"Authorization": f"Bearer {pool_token}", "Content-Type": "application/json", "Accept": "application/json"}
    base = base_url.rstrip("/")

    moved = 0
    downloaded = 0
    uploaded = 0
    errors = []

    for acc in sync_result.get("accounts", []):
        name = acc["name"]
        status = acc["status"]

        if status == "pending_move":
            # 根目录已在远程 → 仅移到 uploaded/
            src = os.path.join(tokens_dir, f"{name}.json")
            dst = os.path.join(uploaded_dir, f"{name}.json")
            try:
                os.replace(src, dst)
                log(f"[同步] 移动: {name}.json → uploaded/")
                moved += 1
            except Exception as e:
                msg = f"[同步] 移动 {name} 失败: {e}"
                log(msg)
                errors.append(msg)

        elif status in ("pending_upload", "local_only"):
            # 本地有但远程无 → 上传（受目标数限制）
            if gap is not None and uploaded >= gap:
                continue
            fpath = (os.path.join(tokens_dir, f"{name}.json") if status == "pending_upload"
                     else os.path.join(uploaded_dir, f"{name}.json"))
            fname = f"{name}.json"
            try:
                with open(fpath, "rb") as f:
                    file_bytes = f.read()
                upload_headers = {"Authorization": f"Bearer {pool_token}"}
                r = session.post(
                    f"{base}/v0/management/auth-files",
                    files={"file": (fname, file_bytes, "application/json")},
                    headers=upload_headers,
                    timeout=10,
                )
                if r.status_code in (200, 201):
                    log(f"[同步] 上传成功: {name}")
                    uploaded += 1
                    if status == "pending_upload":
                        dst = os.path.join(uploaded_dir, fname)
                        os.replace(fpath, dst)
                elif r.status_code == 409:
                    log(f"[同步] 已存在跳过: {name}")
                    if status == "pending_upload":
                        dst = os.path.join(uploaded_dir, fname)
                        os.replace(fpath, dst)
                else:
                    try:
                        detail = r.json()
                    except Exception:
                        detail = r.text[:200]
                    msg = f"[同步] 上传失败: {name} ({r.status_code}) {detail}"
                    log(msg)
                    errors.append(msg)
            except Exception as e:
                msg = f"[同步] 上传异常: {name} - {e}"
                log(msg)
                errors.append(msg)

        elif status == "remote_only":
            if upload_only:
                continue
            # 远程有本地无 → 下载完整数据
            try:
                resp = session.get(f"{base}/v0/management/auth-files/download",
                                   params={"name": f"{name}.json"},
                                   headers={"Authorization": f"Bearer {pool_token}", "Accept": "application/json"},
                                   timeout=15)
                resp.raise_for_status()
                acc_data = resp.json()
                dst = os.path.join(uploaded_dir, f"{name}.json")
                with open(dst, "w", encoding="utf-8") as f:
                    json.dump(acc_data, f, ensure_ascii=False, indent=2)
                log(f"[同步] 下载: {name} → uploaded/{name}.json")
                downloaded += 1
            except Exception as e:
                msg = f"[同步] 下载 {name} 失败: {e}"
                log(msg)
                errors.append(msg)

    log(f"[同步] 完成：上传 {uploaded} 个，移动 {moved} 个，下载 {downloaded} 个，错误 {len(errors)} 个")
    return {"ok": True, "moved": moved, "downloaded": downloaded, "uploaded": uploaded, "errors": errors}


def run_pool_probe(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
    config: Optional[dict] = None,
    max_workers: Optional[int] = None,
) -> dict:
    """探测账号池，找出 401 失效账号"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    url = f"{base_url.rstrip('/')}/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    session = _pool_session(proxy, timeout)

    log("[Pool] 获取账号列表...")
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
        files = raw.get("files", []) if isinstance(raw, dict) else []
    except Exception as e:
        log(f"[Pool] 获取失败: {e}")
        return {"ok": False, "error": str(e), "invalid_401": [], "total": 0, "target": 0}

    total = len(files)
    target_files = [f for f in files if (f.get("type") or f.get("typo") or "").lower() == target_type.lower()]
    log(f"[Pool] 总账号: {total}, {target_type} 账号: {len(target_files)}")

    invalid_401 = []
    probe_lock = threading.Lock()
    checked = 0
    checked_lock = threading.Lock()

    def extract_chatgpt_account_id(item: dict) -> str:
        id_token = item.get("id_token")
        if not isinstance(id_token, dict):
            return ""
        v = id_token.get("chatgpt_account_id")
        return str(v) if v else ""

    def build_probe_payload(auth_index: str, chatgpt_account_id: str) -> dict:
        call_header = {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
        }
        if chatgpt_account_id:
            call_header["Chatgpt-Account-Id"] = chatgpt_account_id
        return {
            "authIndex": auth_index,
            "method": "GET",
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "header": call_header,
        }

    def probe_one(f):
        nonlocal checked
        name = f.get("name") or ""
        file_id = f.get("id") or ""
        display_name = name or file_id or ""
        auth_index = f.get("auth_index")
        if not auth_index:
            log(f"[Pool] 跳过(缺少 auth_index): {display_name}")
            with checked_lock:
                checked += 1
                if checked == 1 or checked % 20 == 0 or checked == len(target_files):
                    log(f"[Pool] 探测进度: {checked}/{len(target_files)}, 401={len(invalid_401)}")
            return
        try:
            payload = build_probe_payload(str(auth_index), extract_chatgpt_account_id(f))
            r = session.post(
                f"{base_url.rstrip('/')}/v0/management/api-call",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
            status_code = data.get("status_code") if isinstance(data, dict) else None
            if status_code == 401:
                with probe_lock:
                    invalid_401.append({"name": name, "id": file_id, "status": 401})
                log(f"[Pool] 401: {display_name}")
            elif status_code is None:
                log(f"[Pool] 探测返回缺少 status_code: {display_name}")
        except Exception as e:
            log(f"[Pool] 探测异常: {display_name} - {e}")
        finally:
            with checked_lock:
                checked += 1
                if checked == 1 or checked % 20 == 0 or checked == len(target_files):
                    log(f"[Pool] 探测进度: {checked}/{len(target_files)}, 401={len(invalid_401)}")

    if max_workers is None:
        cfg = config or load_config()
        max_workers = int((cfg.get("pool") or {}).get("probe_workers", 20))
    max_workers = max(1, min(int(max_workers), max(1, len(target_files))))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(probe_one, target_files))

    log(f"[Pool] 探测完成: 401 失效 {len(invalid_401)} 个")
    return {
        "ok": True,
        "total": total,
        "target": len(target_files),
        "invalid_401": invalid_401,
        "invalid_count": len(invalid_401),
    }


def run_pool_clean(
    base_url: str,
    token: str,
    target_type: str = "codex",
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
    config: Optional[dict] = None,
) -> dict:
    """清理 401 失效账号（探测 + 删除）"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    probe_result = run_pool_probe(
        base_url,
        token,
        target_type,
        proxy,
        timeout,
        log_cb,
        config=config,
    )
    if not probe_result.get("ok"):
        return probe_result

    return run_pool_clean_with_probe_result(
        base_url=base_url,
        token=token,
        probe_result=probe_result,
        proxy=proxy,
        timeout=timeout,
        log_cb=log_cb,
        config=config,
    )


def _delete_invalid_accounts(
    base_url: str,
    token: str,
    invalid_401: list,
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
    config: Optional[dict] = None,
) -> dict:
    """根据 invalid_401 列表执行删除，返回删除统计"""
    def log(msg):
        if log_cb:
            log_cb(msg)

    if not invalid_401:
        log("[Pool] 无需清理，没有 401 账号")
        return {"deleted": 0, "delete_fail": 0}

    log(f"[Pool] 开始删除 {len(invalid_401)} 个失效账号...")
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    session = _pool_session(proxy, timeout)
    deleted = 0
    delete_fail = 0
    del_lock = threading.Lock()
    fail_stats = Counter()
    token_dir = _resolve_token_dir(config or load_config())
    uploaded_dir = os.path.join(token_dir, "uploaded")

    def _resp_detail(resp: Optional[_requests.Response]) -> str:
        if not resp:
            return ""
        try:
            data = resp.json()
            return str(data)[:200]
        except Exception:
            text = resp.text if hasattr(resp, "text") else ""
        return text[:200] if text else ""

    def _build_attempts(file_id: str, name: str) -> list:
        attempts = []
        seen = set()

        def add_attempt(label: str, url: str, params: Optional[dict] = None, json_body: Optional[dict] = None):
            key = (
                url,
                tuple(sorted(params.items())) if isinstance(params, dict) else None,
                tuple(sorted(json_body.items())) if isinstance(json_body, dict) else None,
            )
            if key in seen:
                return
            seen.add(key)
            attempts.append((label, url, params, json_body))

        if file_id:
            fid = str(file_id)
            add_attempt("id_path", f"{base}/v0/management/auth-files/{quote(fid, safe='')}")
            add_attempt("id_query", f"{base}/v0/management/auth-files", {"id": fid})
            add_attempt("id_json", f"{base}/v0/management/auth-files", json_body={"id": fid})

        if name:
            nm = str(name)
            candidates = [(nm, "name")]
            clean_name = _normalize_token_name(nm)
            if clean_name and clean_name != nm:
                candidates.append((clean_name, "name_no_ext"))

            for val, tag in candidates:
                add_attempt(f"{tag}_path", f"{base}/v0/management/auth-files/{quote(val, safe='')}")
                add_attempt(f"{tag}_query", f"{base}/v0/management/auth-files", {"name": val})
                add_attempt(f"{tag}_filename_query", f"{base}/v0/management/auth-files", {"filename": val})
                add_attempt(f"{tag}_json", f"{base}/v0/management/auth-files", json_body={"name": val})
                add_attempt(f"{tag}_filename_json", f"{base}/v0/management/auth-files", json_body={"filename": val})

        return attempts

    def delete_one(item):
        nonlocal deleted, delete_fail
        name = item.get("name", "")
        file_id = item.get("id") or ""
        display_name = name or file_id or ""
        if not display_name:
            with del_lock:
                delete_fail += 1
            log("[Pool] 删除失败: 空文件名")
            return
        try:
            attempts = _build_attempts(file_id, name)
            if not attempts:
                with del_lock:
                    delete_fail += 1
                    fail_stats["EMPTY:attempts"] += 1
                log(f"[Pool] 删除失败: {display_name} (无可用删除方式)")
                return

            last_status: Optional[int] = None
            last_label = ""
            best_status: Optional[int] = None
            best_label = ""
            best_detail = ""

            for label, url, params, json_body in attempts:
                r = session.delete(url, headers=headers, timeout=timeout, params=params, json=json_body)
                last_status = r.status_code
                last_label = label
                if r.status_code in (200, 204):
                    with del_lock:
                        deleted += 1
                    log(f"[Pool] 删除成功: {display_name} ({label})")
                    # 同步删除本地副本（根目录和 uploaded/）
                    clean_name = _normalize_token_name(name or display_name)
                    if clean_name:
                        for local_path in [
                            os.path.join(token_dir, f"{clean_name}.json"),
                            os.path.join(uploaded_dir, f"{clean_name}.json"),
                        ]:
                            if os.path.isfile(local_path):
                                try:
                                    os.remove(local_path)
                                    log(f"[Pool] 本地删除: {clean_name}.json")
                                except Exception as ex:
                                    log(f"[Pool] 本地删除失败: {clean_name} - {ex}")
                            else:
                                log(f"[Pool] 本地不存在: {local_path}")
                    return

                detail = _resp_detail(r)
                if best_status is None:
                    best_status = r.status_code
                    best_label = label
                    best_detail = detail
                elif best_status in (404, 405) and r.status_code not in (404, 405):
                    best_status = r.status_code
                    best_label = label
                    best_detail = detail

            final_status = best_status if best_status is not None else last_status
            final_label = best_label or last_label
            final_detail = best_detail
            with del_lock:
                delete_fail += 1
                fail_stats[f"{final_status}:{final_label}"] += 1
            detail_suffix = f" {final_detail}" if final_detail else ""
            log(f"[Pool] 删除失败: {display_name} ({final_status}, {final_label}){detail_suffix}")
        except Exception as e:
            with del_lock:
                delete_fail += 1
                fail_stats["EXC:request"] += 1
            log(f"[Pool] 删除异常: {display_name} - {e}")

    cfg = config or load_config()
    max_workers = int((cfg.get("pool") or {}).get("delete_workers", 10))
    max_workers = max(1, min(max_workers, max(1, len(invalid_401))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(delete_one, invalid_401))

    if fail_stats:
        stats_str = ", ".join([f"{k}={v}" for k, v in fail_stats.most_common(5)])
        log(f"[Pool] 删除失败统计(前5): {stats_str}")
    log(f"[Pool] 清理完成: 删除成功={deleted}, 失败={delete_fail}")
    return {"deleted": deleted, "delete_fail": delete_fail}


def run_pool_clean_with_probe_result(
    base_url: str,
    token: str,
    probe_result: Dict[str, Any],
    proxy: str = "",
    timeout: int = 10,
    log_cb: Optional[Callable[[str], None]] = None,
    config: Optional[dict] = None,
) -> dict:
    """使用现成探测结果清理 401 账号（不重复探测）"""
    if not isinstance(probe_result, dict):
        return {"ok": False, "error": "invalid probe_result", "invalid_401": [], "total": 0, "target": 0}

    invalid_401 = probe_result.get("invalid_401", [])
    if not isinstance(invalid_401, list):
        return {"ok": False, "error": "invalid probe_result.invalid_401", "invalid_401": [], "total": 0, "target": 0}

    delete_result = _delete_invalid_accounts(
        base_url=base_url,
        token=token,
        invalid_401=invalid_401,
        proxy=proxy,
        timeout=timeout,
        log_cb=log_cb,
        config=config,
    )
    return {**probe_result, **delete_result, "ok": True}


def _upload_tokens_to_pool(
    base_url: str,
    pool_token: str,
    config: Optional[dict] = None,
    proxy: str = "",
    log_cb: Optional[Callable[[str], None]] = None,
) -> int:
    """将 token_json_dir 下的 JSON 文件上传到账号池，返回上传成功数"""
    def log(msg):
        if log_cb:
            log_cb(msg)

    token_dir = (config or {}).get("token_json_dir", "codex_tokens")
    if not os.path.isabs(token_dir):
        token_dir = os.path.join(_BASE_DIR, token_dir)

    if not os.path.isdir(token_dir):
        log(f"[Pool] token 目录不存在: {token_dir}")
        return 0

    uploaded = 0
    base = base_url.rstrip("/")
    upload_headers = {"Authorization": f"Bearer {pool_token}"}
    session = _pool_session(proxy, 10)
    for fname in os.listdir(token_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(token_dir, fname)
        try:
            with open(fpath, "rb") as f:
                file_bytes = f.read()
            r = session.post(
                f"{base}/v0/management/auth-files",
                files={"file": (fname, file_bytes, "application/json")},
                headers=upload_headers,
                timeout=10,
            )
            if r.status_code in (200, 201):
                uploaded += 1
                log(f"[Pool] 上传成功: {fname}")
                uploaded_dir = os.path.join(token_dir, "uploaded")
                os.makedirs(uploaded_dir, exist_ok=True)
                os.replace(fpath, os.path.join(uploaded_dir, fname))
            elif r.status_code == 409:
                log(f"[Pool] 已存在跳过: {fname}")
                uploaded_dir = os.path.join(token_dir, "uploaded")
                os.makedirs(uploaded_dir, exist_ok=True)
                os.replace(fpath, os.path.join(uploaded_dir, fname))
            else:
                log(f"[Pool] 上传失败: {fname} ({r.status_code})")
        except Exception as e:
            log(f"[Pool] 上传异常: {fname} - {e}")

    log(f"[Pool] 上传完成: {uploaded} 个 token")
    return uploaded


def run_pool_fill(
    fill_count: int,
    base_url: str,
    pool_token: str,
    stop_event: threading.Event,
    log_cb: Callable[[str], None],
    progress_cb: Callable[[int, int, int], None],
    config: Optional[dict] = None,
    proxy: str = "",
    target_count: int = 0,
    target_type: str = "codex",
) -> dict:
    """补号：注册新账号并尝试上传到账号池"""

    def log(msg):
        if log_cb:
            log_cb(msg)

    log(f"[Pool] 开始补号: 目标数量={fill_count}")

    # 先同步本地存量到远程，减少实际需要注册的数量（跳过 remote_only 下载）
    pre_uploaded = 0
    if base_url and pool_token:
        log("[Pool] 先同步本地存量到远程...")
        sync_r = sync_local_remote(base_url, pool_token, target_type, config, proxy, log_cb,
                                   target_count=target_count, upload_only=True)
        pre_uploaded = sync_r.get("uploaded", 0)
        if pre_uploaded > 0:
            fill_count = max(0, fill_count - pre_uploaded)
            log(f"[Pool] 存量上传 {pre_uploaded} 个，剩余需注册 {fill_count} 个")

    if fill_count == 0:
        return {"success": 0, "fail": 0, "total": 0, "uploaded": pre_uploaded}

    cfg_workers = int((config or {}).get("workers") or 3)
    result = run_batch_register(
        count=fill_count,
        workers=min(cfg_workers, fill_count),
        proxy=proxy or (config or {}).get("proxy", ""),
        stop_event=stop_event,
        log_cb=log_cb,
        progress_cb=progress_cb,
        config=config,
    )

    registered = result.get("success", 0)
    if registered > 0 and base_url and pool_token:
        log("[Pool] 尝试上传新 token 到账号池...")
        uploaded = _upload_tokens_to_pool(base_url, pool_token, config, proxy, log_cb)
        result["uploaded"] = uploaded + pre_uploaded
    else:
        result["uploaded"] = pre_uploaded

    return result


# ============================================================
# 免费代理工具
# ============================================================

_PROXY_SOURCES = [
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]


def fetch_free_proxies(timeout: int = 10, proxy: str = "") -> List[str]:
    """从公开代理源获取免费代理列表，proxy 用于访问 GitHub（国内环境需要）"""
    session = _requests.Session()
    session.verify = False
    if proxy:
        p = proxy if "://" in proxy else f"http://{proxy}"
        session.proxies = {"http": p, "https": p}

    proxies = set()
    for url in _PROXY_SOURCES:
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if ":" in line and not line.startswith("http"):
                            proxies.add(f"http://{line}")
                        elif line.startswith("http"):
                            proxies.add(line)
        except Exception:
            pass
    return sorted(proxies)[:200]  # 最多返回 200 个


def test_proxy(
    proxy: str,
    target_url: str = "https://httpbin.org/ip",
    timeout: int = 5,
) -> dict:
    """测试代理可用性，返回延迟和状态"""
    if not proxy:
        return {"proxy": proxy, "ok": False, "error": "空代理"}

    proxy_url = proxy if "://" in proxy else f"http://{proxy}"
    proxies = {"http": proxy_url, "https": proxy_url}

    start = time.time()
    try:
        resp = _requests.get(target_url, proxies=proxies, timeout=timeout, verify=False)
        elapsed = int((time.time() - start) * 1000)
        if resp.status_code == 200:
            return {"proxy": proxy, "ok": True, "latency_ms": elapsed, "status": resp.status_code}
        return {"proxy": proxy, "ok": False, "latency_ms": elapsed, "status": resp.status_code}
    except _requests.exceptions.Timeout:
        return {"proxy": proxy, "ok": False, "error": "超时", "latency_ms": timeout * 1000}
    except Exception as e:
        return {"proxy": proxy, "ok": False, "error": str(e)[:80]}


def test_proxies_concurrent(
    proxy_list: List[str],
    target_url: str = "https://httpbin.org/ip",
    timeout: int = 5,
    max_workers: int = 20,
) -> List[dict]:
    """并发测试多个代理"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(lambda p: test_proxy(p, target_url, timeout), proxy_list))
    return results


# ============================================================
# 配置文件读写
# ============================================================

def load_config() -> dict:
    """读取 config.json"""
    cfg_path = os.path.join(_BASE_DIR, "config.json")
    defaults = {
        "total_accounts": 3,
        "duckmail_api_base": "https://api.duckmail.sbs",
        "duckmail_domain": "duckmail.sbs",
        "duckmail_bearer": "",
        "proxy": "",
        "workers": 3,
        "output_file": "registered_accounts.txt",
        "enable_oauth": True,
        "oauth_required": True,
        "oauth_issuer": "https://auth.openai.com",
        "oauth_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "oauth_redirect_uri": "http://localhost:1455/auth/callback",
        "ak_file": "ak.txt",
        "rk_file": "rk.txt",
        "token_json_dir": "codex_tokens",
        "proxy_test_workers": 20,
        "pool": {
            "base_url": "",
            "token": "",
            "target_type": "codex",
            "min_candidates": 100,
            "proxy": "",
            "probe_workers": 20,
            "delete_workers": 10,
        },
    }
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                defaults.update(file_cfg)
        except Exception:
            pass
    return defaults


def save_config(config: dict) -> bool:
    """保存 config.json"""
    cfg_path = os.path.join(_BASE_DIR, "config.json")
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ============================================================
# 结果文件读取
# ============================================================

def read_registered_accounts(config: Optional[dict] = None) -> List[dict]:
    """解析 registered_accounts.txt"""
    output_file = (config or {}).get("output_file", "registered_accounts.txt")
    if not os.path.isabs(output_file):
        output_file = os.path.join(_BASE_DIR, output_file)

    accounts = []
    if not os.path.exists(output_file):
        return accounts

    try:
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("----")
                if len(parts) >= 2:
                    accounts.append({
                        "email": parts[0],
                        "password": parts[1],
                        "email_password": parts[2] if len(parts) > 2 else "",
                        "oauth": parts[3] if len(parts) > 3 else "",
                    })
    except Exception:
        pass
    return accounts


def read_token_file(filename: str, config: Optional[dict] = None) -> str:
    """读取 ak.txt 或 rk.txt 内容"""
    key = "ak_file" if "ak" in filename.lower() else "rk_file"
    path = (config or {}).get(key, filename)
    if not os.path.isabs(path):
        path = os.path.join(_BASE_DIR, path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def list_codex_tokens(config: Optional[dict] = None) -> List[dict]:
    """列出 codex_tokens/ 目录下的所有 JSON token"""
    token_dir = (config or {}).get("token_json_dir", "codex_tokens")
    if not os.path.isabs(token_dir):
        token_dir = os.path.join(_BASE_DIR, token_dir)

    tokens = []
    if not os.path.isdir(token_dir):
        return tokens

    for fname in os.listdir(token_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(token_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                tokens.append(data)
        except Exception:
            pass

    tokens.sort(key=lambda x: x.get("last_refresh", ""), reverse=True)
    return tokens


# ============================================================
# 代理池单例
# ============================================================

class _ProxyPool:
    """免费代理池单例，存储最近测试结果，提供最优代理"""

    def __init__(self):
        self._proxies: List[dict] = []  # [{proxy, ok, latency_ms}, ...]
        self._lock = threading.Lock()

    def update(self, results: List[dict]):
        """更新测试结果，按延迟排序（可用的排前面）"""
        with self._lock:
            working = sorted(
                [r for r in results if r.get("ok")],
                key=lambda x: x.get("latency_ms", 99999),
            )
            failed = [r for r in results if not r.get("ok")]
            self._proxies = working + failed

    def get_best(self, fallback: str = "") -> str:
        """返回延迟最低的可用免费代理，若无则返回 fallback"""
        with self._lock:
            working = [p for p in self._proxies if p.get("ok")]
            if working:
                return working[0]["proxy"]
        return fallback

    def get_all(self) -> List[dict]:
        """返回所有代理的副本"""
        with self._lock:
            return list(self._proxies)


_proxy_pool = _ProxyPool()


# ============================================================
# 号池自动维护周期
# ============================================================

def run_pool_maintain_cycle(
    base_url: str,
    token: str,
    target_type: str,
    target_count: int,
    stop_event: threading.Event,
    log_cb: Callable[[str], None],
    config: Optional[dict] = None,
    proxy: str = "",
) -> dict:
    """
    号池维护一次完整周期：
    1. 获取当前池状态
    2. 清理 401 失效账号
    3. 计算缺口，若缺口 > 0 则注册新账号并上传到池
    4. 返回统计结果
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    log(f"[Daemon] 开始维护周期: 目标类型={target_type}, 目标数量={target_count}")

    # 1. 获取当前状态
    status = get_pool_status(base_url, token, target_type, proxy)
    if not status.get("ok"):
        log(f"[Daemon] 获取池状态失败: {status.get('error')}")
        return {"ok": False, "error": status.get("error")}

    log(f"[Daemon] 当前 {target_type} 账号数: {status['target']}")

    # 2. 清理 401 账号
    clean_result = run_pool_clean(base_url, token, target_type, proxy, log_cb=log_cb, config=config)
    if not clean_result.get("ok"):
        log(f"[Daemon] 清理失败，跳过补号: {clean_result.get('error')}")
        return {"ok": False, "error": clean_result.get("error")}

    deleted = clean_result.get("deleted", 0)
    log(f"[Daemon] 清理完成: 删除 {deleted} 个失效账号")

    # 3. 重新获取有效数量
    status_after = get_pool_status(base_url, token, target_type, proxy)
    valid_count = status_after.get("target", 0) if status_after.get("ok") else (status["target"] - deleted)
    gap = target_count - valid_count

    log(f"[Daemon] 清理后有效账号: {valid_count}, 目标: {target_count}, 缺口: {gap}")

    # 4. 若有缺口则先同步本地存量，再注册补充
    registered = 0
    uploaded = 0
    if gap > 0:
        if stop_event and stop_event.is_set():
            log("[Daemon] 任务已停止，跳过补号")
        else:
            log("[Daemon] 先同步本地存量到远程...")
            sync_r = sync_local_remote(base_url, token, target_type, config, proxy, log_cb, target_count)
            pre_uploaded = sync_r.get("uploaded", 0)
            if pre_uploaded > 0:
                status_synced = get_pool_status(base_url, token, target_type, proxy)
                valid_count = status_synced.get("target", valid_count) if status_synced.get("ok") else valid_count + pre_uploaded
                gap = max(0, target_count - valid_count)
                log(f"[Daemon] 存量上传 {pre_uploaded} 个，同步后有效账号: {valid_count}，剩余缺口: {gap}")

            if gap > 0:
                log(f"[Daemon] 开始注册 {gap} 个账号...")
                cfg_workers = int((config or {}).get("workers") or 3)
                reg_result = run_batch_register(
                    count=gap,
                    workers=min(cfg_workers, gap),
                    proxy=proxy,
                    stop_event=stop_event,
                    log_cb=log_cb,
                    progress_cb=lambda s, f, t: None,
                    config=config,
                )
                registered = reg_result.get("success", 0)
                log(f"[Daemon] 注册完成: 成功={registered}, 失败={reg_result.get('fail', 0)}")

                if registered > 0 and base_url and token:
                    log("[Daemon] 上传新 token 到账号池...")
                    uploaded = _upload_tokens_to_pool(base_url, token, config, proxy, log_cb)
            else:
                log("[Daemon] 存量补齐，无需注册新账号")
    else:
        log("[Daemon] 账号数量充足，无需补号")

    log(f"[Daemon] 维护周期完成: 删除={deleted}, 注册={registered}, 上传={uploaded}")
    return {
        "ok": True,
        "valid_before": status["target"],
        "valid_after": valid_count,
        "deleted": deleted,
        "registered": registered,
        "uploaded": uploaded,
        "gap": gap,
    }
