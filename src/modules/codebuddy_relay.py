"""CodeBuddy 无感换号中转服务

原理：
- 本地透明反向代理，原样转发到 https://copilot.tencent.com（请求头/请求体/响应全部透传）；
- 仅在「计费路径」（/v2/chat/completions、/v2/completions、/v2/chat/queue/* 等）
  用上游 Key 池里的 Key 替换 Authorization，实现无感换号；
- 其余路径（登录态、token 刷新、历史会话、产品配置等）带客户端原始 token 透传，
  客户端登录账号、历史会话、UI 完全不受影响；
- 429 临时限流 → mark_key_cooldown 自动换下一个 Key 重试；
  额度耗尽(code 14018) → mark_key_exhausted；风控(code 11140) → mark_key_abnormal；
- JWT 临期自动续期（复用 ProxyRouter.maybe_refresh_jwt_key）。

配合 CodeBuddy 扩展的自定义端点能力使用：
  settings.json: codingcopilot.envRouteMode="custom" + codingcopilot.endpoint=http://127.0.0.1:<port>
"""

import gzip
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlsplit

import requests

from ..utils.store import load_setting
from .proxy_server import ProxyDatabase, ProxyRouter, _decode_jwt_sub

logger = logging.getLogger(__name__)

UPSTREAM_BASE = "https://copilot.tencent.com"

# 需要替换 token 的计费路径（精确匹配，不含 query）
SWAP_PATHS_EXACT = {
    "/v2/chat/completions",
    "/v2/completions",
    "/v2/agents",
    "/v2/embeddings",
}
# 需要替换 token 的计费路径（前缀匹配）
SWAP_PATHS_PREFIX = (
    "/v2/chat/queue/",
)

# WorkBuddy 内嵌 CLI 的 OpenAI 客户端把 CODEBUDDY_BASE_URL 原样当 baseURL
# （不像内部 v2 客户端会补 /v2），所以它的请求是不带版本前缀的裸路径，
# 直接打上游会 302 到别的域名导致 CLI 失败。这里统一补上 /v2 再转发。
_BARE_OPENAI_PREFIXES = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/audio/",
)


def _normalize_upstream_path(path: str) -> str:
    """裸 OpenAI 路径补 /v2 前缀，其余原样"""
    p = urlsplit(path).path
    if p.startswith(_BARE_OPENAI_PREFIXES):
        return "/v2" + path
    return path

# 请求侧需要剥掉的 hop-by-hop 头
_REQ_SKIP_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "content-length", "accept-encoding",
}
# 响应侧需要剥掉的头（长度/编码由本服务重新组织）
_RESP_SKIP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "content-length",
    "content-encoding",
}

# 单次请求最多尝试的 Key 数量
_MAX_KEY_ATTEMPTS = 5

# 换号请求上行的 acp-connection-id：进程级随机值（不用真身的，防会话维度关联）
_RELAY_ACP_CONNECTION_ID = str(uuid.uuid4())


def _scrub_body_identity(body: bytes, out_headers: dict, in_headers) -> bytes:
    """换号请求的 body 脱敏：把真身会话/ACP id 在 body 里的所有出现
    同步替换为请求头改写后的值（growthEvent 埋点等位置，引用一致性不破）。
    gzip 体先解压再压回；任何异常都原样返回，不影响转发。
    """
    if not body:
        return body
    repl = {}
    for name, real in (("x-conversation-id", in_headers.get("X-Conversation-ID")),
                       ("acp-connection-id", in_headers.get("acp-connection-id"))):
        if not real:
            continue
        new = next((v for k, v in out_headers.items() if k.lower() == name), "")
        if new and new != real:
            repl[real] = new
    if not repl:
        return body
    gz = "gzip" in (next((v for k, v in out_headers.items()
                          if k.lower() == "content-encoding"), "") or "").lower()
    try:
        text = gzip.decompress(body).decode("utf-8") if gz else body.decode("utf-8")
    except Exception:
        return body
    for old, new in repl.items():
        text = text.replace(old, new)
    data = text.encode("utf-8")
    return gzip.compress(data) if gz else data


def _parse_usage(buf: bytes) -> dict:
    """从响应体里提取 token 用量（SSE 流的 data: 行 / 整体 JSON 都兼容）"""
    text = buf.decode("utf-8", "ignore")
    found = {}
    # SSE：逐行找 data: {..."usage":{...}...}，取最后一个
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        u = obj.get("usage") if isinstance(obj, dict) else None
        if isinstance(u, dict) and u.get("total_tokens"):
            found = u
    # 非流式：整体就是 JSON
    if not found:
        try:
            obj = json.loads(text)
            u = obj.get("usage") if isinstance(obj, dict) else None
            if isinstance(u, dict) and u.get("total_tokens"):
                found = u
        except ValueError:
            pass
    if not found:
        return {}
    details = found.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": int(found.get("prompt_tokens") or 0),
        "completion_tokens": int(found.get("completion_tokens") or 0),
        "total_tokens": int(found.get("total_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "credit": float(found.get("credit") or 0.0),
    }


def _extract_model(body: bytes) -> str:
    """从请求 body 解析 model 字段（兼容 gzip 压缩体），失败返回空字符串。"""
    if not body:
        return ""
    try:
        raw = body
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8", "ignore"))
        return str(data.get("model") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _err_preview(body: bytes, limit: int = 200) -> str:
    """上游错误响应体截断预览（照 API 代理 error 字段带原始返回，方便定位原因）。"""
    try:
        return body.decode("utf-8", "ignore")[:limit]
    except Exception:
        return ""


def _is_swap_path(path: str) -> bool:
    """判断请求路径是否为计费路径（需要换 token）"""
    p = urlsplit(path).path
    if p in SWAP_PATHS_EXACT:
        return True
    return any(p.startswith(prefix) for prefix in SWAP_PATHS_PREFIX)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class CodeBuddyRelayServer:
    """CodeBuddy 透明中转服务（与 ProxyServer 同款生命周期接口）"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8003):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.db = ProxyDatabase.get_instance()
        self.router = ProxyRouter(self.db)
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # 上游连接池
        self._session = requests.Session()
        # 状态展示（GUI 轮询）
        self._status_lock = threading.Lock()
        self._current_key: dict = {}      # 当前消耗中的 Key {key_id, label, points}
        self._total_requests = 0
        self._swapped_requests = 0
        self._last_event = ""
        # 事件日志（使用日志 tab 读取），独立于 API 代理的日志
        self._events: deque = deque(maxlen=300)
        # 进行中的换号请求 {key_id: 并发数}（Key 池「使用中」状态展示）
        self._inflight: dict = {}

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        try:
            handler = self._make_handler()
            self._httpd = _ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            logger.error(f"[CodeBuddy中转] 端口 {self.port} 启动失败: {e}")
            return False
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._running = True
        logger.info(f"[CodeBuddy中转] 已启动 {self.base_url} → {UPSTREAM_BASE}")
        return True

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception as e:
            logger.error(f"[CodeBuddy中转] 停止异常: {e}")
        self._httpd = None
        with self._status_lock:
            self._current_key = {}
            self._last_event = "已停止"
        logger.info("[CodeBuddy中转] 已停止")

    def get_status(self) -> dict:
        """GUI 状态轮询用"""
        with self._status_lock:
            return {
                "running": self._running,
                "port": self.port,
                "base_url": self.base_url,
                "current_key": dict(self._current_key),
                "total_requests": self._total_requests,
                "swapped_requests": self._swapped_requests,
                "last_event": self._last_event,
                "inflight": dict(self._inflight),
            }

    def _inflight_inc(self, key_id: str):
        with self._status_lock:
            self._inflight[key_id] = self._inflight.get(key_id, 0) + 1

    def _inflight_dec(self, key_id: str):
        with self._status_lock:
            n = self._inflight.get(key_id, 0) - 1
            if n > 0:
                self._inflight[key_id] = n
            else:
                self._inflight.pop(key_id, None)

    # ─── 内部 ───

    def _set_current_key(self, key: dict):
        with self._status_lock:
            self._current_key = {
                "key_id": key.get("key_id", ""),
                "label": key.get("label", key.get("key_id", "")[:8]),
                "points": key.get("points", ""),
            }

    def set_forced_key(self, key_id: str) -> bool:
        """手动指定当前消耗的 Key（上游 Key 池右键「使用此账号」）。

        专一模式下 _select_relay_key 优先用 _current_key，下一个请求即切到该号；
        若该号不可用（禁用/冷却/积分不足）会被 _eligible_keys 过滤，自动换别的。
        """
        key = None
        for k in self.db.get_upstream_keys():
            if k.get("key_id") == key_id:
                key = k
                break
        if not key:
            return False
        label = key.get("label", key_id[:8])
        with self._status_lock:
            self._current_key = {
                "key_id": key_id,
                "label": label,
                "points": key.get("points", ""),
            }
            self._last_event = f"手动指定使用 {label}"
        self._record_event(True, f"手动指定使用 {label}")
        return True

    def _record_event(self, swapped: bool, event: str):
        with self._status_lock:
            self._total_requests += 1
            if swapped:
                self._swapped_requests += 1
            self._last_event = event
            self._events.append(
                f"{time.strftime('%H:%M:%S')} {event}")
            # 只保留最近 500 条，防止长跑内存无限增长
            if len(self._events) > 500:
                del self._events[:-500]

    def get_events(self) -> list:
        """使用日志 tab 读取（新的在前）"""
        with self._status_lock:
            return list(reversed(self._events))

    def clear_events(self):
        with self._status_lock:
            self._events.clear()
            self._total_requests = 0
            self._swapped_requests = 0

    # ─── 独立 Key 状态（relay_* 字段，与 API 代理的 status 互不影响）───

    @staticmethod
    def _points_remaining(key: dict) -> float:
        """解析 points "剩余/总量"，返回剩余积分；无数据返回 -1"""
        points_str = key.get("points", "")
        if not points_str or "/" not in points_str:
            return -1
        try:
            return float(points_str.split("/")[0])
        except (ValueError, IndexError):
            return -1

    def _eligible_keys(self, exclude: set) -> list:
        """池子里可用于无感换号的 Key

        条件：仅账号 JWT；relay 侧未禁用/未冷却；且已知积分未触阈值
        （阈值预判在选 Key 时做，不依赖 GUI 页面的定时刷新，
        避免拿 0 积分的死号去打必败的请求）。
        独立池子：不看也不动 API 代理页的 status，两边互不相通。
        """
        now = time.time()
        try:
            min_credits = float(load_setting("hotswitch_min_credits", "0") or 0)
        except (ValueError, TypeError):
            min_credits = 0
        try:
            auto_enable = float(load_setting("hotswitch_auto_enable", "100") or 100)
        except (ValueError, TypeError):
            auto_enable = 100
        result = []
        for k in self.db.get_upstream_keys():
            kid = k.get("key_id", "")
            if kid in exclude:
                continue
            if not k.get("api_key", "").startswith("eyJ"):
                continue
            relay_status = k.get("relay_status", "active")
            if relay_status != "active":
                # 「积分不足」类自动禁用的：积分已恢复（查分/估算）则自动启用，
                # 本页侧自闭环，与 API 代理页互不相通；风控/手动禁用不动
                if relay_status == "disabled" and \
                        str(k.get("relay_note", "")).startswith("积分不足"):
                    pts_back = self._points_remaining(k)
                    if pts_back > max(auto_enable, 0):
                        self.db.update_upstream_key(kid, {
                            "relay_status": "active",
                            "relay_note": "",
                        })
                        logger.info(
                            f"[CodeBuddy中转] Key {k.get('label', kid)} 积分恢复 {pts_back:.0f}，自动启用")
                    else:
                        continue
                else:
                    continue
            if float(k.get("relay_cooldown_until") or 0) > now:
                continue
            # 积分阈值预判：pts <= 阈值（含 0 积分，阈值默认 0）本页侧禁用并跳过
            pts = self._points_remaining(k)
            if pts >= 0 and pts <= max(min_credits, 0):
                self.db.update_upstream_key(kid, {
                    "relay_status": "disabled",
                    "relay_note": f"积分不足({pts:.0f}<={max(min_credits, 0):.0f})，自动禁用",
                })
                logger.warning(
                    f"[CodeBuddy中转] Key {k.get('label', kid)} 积分 {pts:.0f} 触阈值，禁用并跳过")
                continue
            result.append(k)
        return result

    def _select_relay_key(self, exclude: set):
        """专一模式：当前 Key 仍可用就继续用，只有它失效（禁用/冷却/耗尽）才换下一个"""
        keys = self._eligible_keys(exclude)
        with self._status_lock:
            cur_id = self._current_key.get("key_id", "")
        if cur_id and cur_id not in exclude:
            for k in keys:
                if k.get("key_id", "") == cur_id:
                    return k
            # 当前 Key 被过滤（禁用/冷却/积分不足）→ 记录请求间换号的原因
            reason = self._why_key_unavailable(cur_id)
            if reason:
                self._record_event(True, f"换号[{cur_id[:8]}] 不可用：{reason}")
        if not keys:
            return None
        # 没有当前 Key（首次/刚被处置）：取使用最少的，分摊消耗
        keys.sort(key=lambda k: int(k.get("relay_used", 0) or 0))
        return keys[0]

    def _why_key_unavailable(self, key_id: str) -> str:
        """当前 Key 没进候选时的原因（对照 _eligible_keys 的过滤条件），找不到返回空。"""
        now = time.time()
        try:
            min_credits = float(load_setting("hotswitch_min_credits", "0") or 0)
        except (ValueError, TypeError):
            min_credits = 0
        for k in self.db.get_upstream_keys():
            if k.get("key_id", "") != key_id:
                continue
            label = k.get("label", key_id[:8])
            if k.get("relay_status", "active") != "active":
                note = str(k.get("relay_note", "") or "")
                return f"{label} {note or '已禁用'}"
            if float(k.get("relay_cooldown_until") or 0) > now:
                remain = int(float(k.get("relay_cooldown_until")) - now)
                return f"{label} 冷却中（剩 {remain}s）"
            pts = self._points_remaining(k)
            if pts >= 0 and pts <= max(min_credits, 0):
                return f"{label} 积分不足({pts:.0f}≤{max(min_credits, 0):.0f})"
            return f"{label} 不可用"
        return ""

    def _is_current_key(self, key_id: str) -> bool:
        """该 Key 是否就是当前正在消耗的 Key（用于日志区分 消耗/换号）"""
        with self._status_lock:
            return bool(key_id) and self._current_key.get("key_id", "") == key_id

    def _punish_key(self, key_id: str, action: str) -> str:
        """独立处置：只写 relay_* 字段，不动主池 status。返回用户可读的处置原因。"""
        label = ""
        if action == "cooldown":
            try:
                secs = int(load_setting("cooldown_seconds", "10") or "10")
            except (ValueError, TypeError):
                secs = 10
            secs = max(1, min(secs, 3600))
            self.db.update_upstream_key(key_id, {
                "relay_cooldown_until": time.time() + secs,
            })
            reason = f"429 限流，冷却 {secs}s"
            logger.warning(f"[CodeBuddy中转] Key {key_id} 限流，中转侧冷却 {secs} 秒")
            return reason
        if action == "exhausted":
            self.db.update_upstream_key(key_id, {
                "relay_status": "disabled",
                # 备注以「积分不足」开头：本页积分恢复时只自动恢复这类
                "relay_note": "积分不足(14018 额度耗尽)，自动禁用",
            })
            reason = "14018 积分耗尽，已禁用"
            logger.warning(f"[CodeBuddy中转] Key {key_id} 积分耗尽，本页侧禁用")
            return reason
        if action == "abnormal":
            self.db.update_upstream_key(key_id, {
                "relay_status": "disabled",
                "relay_note": "上游风控(11140)，中转侧自动禁用",
            })
            reason = "11140 上游风控，已禁用"
            logger.warning(f"[CodeBuddy中转] Key {key_id} 被风控，本页侧禁用")
            return reason
        return ""

    def _classify_error(self, status_code: int, body: bytes) -> str:
        """根据上游错误分类，返回对 Key 的处置: cooldown / exhausted / abnormal / retry / passthrough

        与 API 代理（proxy_server）同口径：
        - 429 + body code 14018/14019 → 额度耗尽（永久禁用，不是临时限流！）
        - 402/400 + code 14018 → 额度耗尽
        - 401/403 + code 11140 → 风控异常
        - 400 空 body → 上游临时问题，换 Key 重试但不罚 Key
        - 其余 429/401/403 → 临时冷却，轮转重试
        """
        code = None
        try:
            err = json.loads(body.decode("utf-8", "ignore"))
            if isinstance(err, dict):
                err_obj = err.get("error")
                data_obj = err_obj.get("data") if isinstance(err_obj, dict) else None
                code = (data_obj.get("code", 0) if isinstance(data_obj, dict) else 0) \
                    or err.get("code", 0) or None
        except ValueError:
            code = None
        if status_code == 429:
            # 429 不一定是临时限流：14018=额度已用尽, 14019=额度不足，必须判死
            if code in (14018, 14019):
                return "exhausted"
            return "cooldown"
        if status_code in (402, 400):
            if code == 14018:
                return "exhausted"
            # 400 空 body：上游临时问题（照 API 代理口径），换 Key 重试不罚 Key
            if status_code == 400 and not body.strip():
                return "retry"
            return "passthrough"  # 参数错误等非 Key 问题，原样返回客户端
        if status_code in (401, 403):
            if code == 11140:
                return "abnormal"
            # token 失效等，临时冷却让池自动轮转，不当场打死
            return "cooldown"
        return "passthrough"

    def _make_handler(self):
        server_ref = self

        class RelayHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # 静音默认访问日志
                pass

            # 所有方法统一走 relay
            def do_GET(self):     self._relay()
            def do_POST(self):    self._relay()
            def do_PUT(self):     self._relay()
            def do_DELETE(self):  self._relay()
            def do_PATCH(self):   self._relay()
            def do_OPTIONS(self): self._relay()

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    return self.rfile.read(length)
                return b""

            def _build_upstream_headers(self, auth_override: str = "") -> dict:
                headers = {}
                for name, value in self.headers.items():
                    if name.lower() in _REQ_SKIP_HEADERS:
                        continue
                    headers[name] = value
                # 统一 identity 编码，避免 gzip 与流式转发打架
                headers["Accept-Encoding"] = "identity"
                if auth_override:
                    headers["Authorization"] = auth_override
                    # 换 token 必须同步换身份：X-User-Id 改成池子账号的 uid
                    # （2026-08-09 抓包实锤 CLI 会带真身 X-User-Id，不同步则
                    # 单请求内 token 与 X-User-Id 身份矛盾，等于自曝）
                    uid = _decode_jwt_sub(auth_override[7:] if auth_override.startswith("Bearer ") else auth_override)
                    if uid:
                        headers["X-User-Id"] = uid
                    else:
                        headers.pop("X-User-Id", None)
                    # 会话级身份同步（2026-08-09 抓包实锤真身会话 id 泄漏）：
                    # X-Conversation-ID 按 池子uid+真身会话id 派生稳定新 id——
                    # 同会话不变、跨会话不同、与真身不可逆关联；
                    # acp-connection-id 换成本进程级随机值
                    for k in list(headers):
                        lk = k.lower()
                        if lk == "x-conversation-id":
                            headers[k] = str(uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"antigravity-relay:{uid}:{headers[k]}"))
                        elif lk == "acp-connection-id":
                            headers[k] = _RELAY_ACP_CONNECTION_ID
                return headers

            def _send_error_verbatim(self, status: int, resp_headers, body: bytes):
                self.send_response(status)
                for name, value in resp_headers.items():
                    if name.lower() in _RESP_SKIP_HEADERS:
                        continue
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _send_stream(self, status: int, resp_headers, resp, on_done=None):
                """chunked 流式回传（SSE 友好）

                on_done: 流结束（或客户端断开）后回调，参数为收集到的响应字节
                （用于解析 token 用量做统计）；不传则不收集。
                """
                self.send_response(status)
                for name, value in resp_headers.items():
                    if name.lower() in _RESP_SKIP_HEADERS:
                        continue
                    self.send_header(name, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                collected = bytearray() if on_done else None
                try:
                    for chunk in resp.iter_content(chunk_size=4096):
                        if not chunk:
                            continue
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                        if collected is not None:
                            collected.extend(chunk)
                            # 只留尾部 256KB，usage 在流末尾，防止长流占内存
                            if len(collected) > 262144:
                                del collected[:-131072]
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    logger.info(
                        f"[CodeBuddy中转] 流式回传完成 {self.command} {self.path}")
                except (BrokenPipeError, ConnectionResetError):
                    logger.warning(
                        f"[CodeBuddy中转] 客户端提前断开连接 {self.command} {self.path}")
                if on_done:
                    try:
                        on_done(bytes(collected))
                    except Exception as e:
                        logger.error(f"[CodeBuddy中转] 统计回调异常: {e}")

            def _relay(self):
                path = self.path
                upstream_path = _normalize_upstream_path(path)
                swap = _is_swap_path(upstream_path)
                body = self._read_body()
                model = _extract_model(body) or "-"
                t0 = time.time()
                url = UPSTREAM_BASE + upstream_path
                logger.info(
                    f"[CodeBuddy中转] 收到请求 {self.command} {path} "
                    f"body={len(body)}B swap={swap}"
                    + (f"（裸路径→{urlsplit(upstream_path).path}）"
                       if upstream_path != path else ""))

                if not swap:
                    # 非计费路径：原始 token 透传
                    self._do_forward(url, body, self._build_upstream_headers(), tag="透传")
                    server_ref._record_event(False, f"透传 {urlsplit(path).path}")
                    return

                # 计费路径：从中转独立 Key 池选 Key 换 token，失败自动轮转重试
                exclude: set = set()
                last_status, last_headers, last_body = 502, {}, (
                    b'{"error":"no available upstream key",'
                    b'"hint":"no active JWT (account-token) key in pool - '
                    b'restore one in the key-pool tab"}'
                )

                for attempt in range(_MAX_KEY_ATTEMPTS):
                    key = server_ref._select_relay_key(exclude)
                    if not key:
                        break
                    key_id = key.get("key_id", "")
                    exclude.add(key_id)
                    api_key = server_ref.router.maybe_refresh_jwt_key(key)
                    label = key.get("label", key_id[:8])
                    # 专一模式：还是当前 Key 记「消耗」，真换了才记「换号」
                    is_switch = not server_ref._is_current_key(key_id)
                    tag = f"换号[{label}]" if is_switch else f"消耗[{label}]"
                    headers = self._build_upstream_headers(
                        auth_override=f"Bearer {api_key}")
                    # body 里的真身会话/ACP id 同步替换为改写后的值
                    fwd_body = _scrub_body_identity(body, headers, self.headers)

                    # 流结束后统计用量（有无 usage 都计一次调用）
                    def _on_done(buf: bytes, _kid=key_id, _key=key, _tag=tag, _model=model, _path=urlsplit(upstream_path).path):
                        server_ref._inflight_dec(_kid)
                        usage = _parse_usage(buf)
                        credit = usage.pop("credit", 0.0)
                        try:
                            server_ref.db.increment_relay_key_stats(_kid, credits=credit, **usage)
                        except Exception as e:
                            logger.error(f"[CodeBuddy中转] 统计写入失败: {e}")
                        # 与 API 代理同一套统计管线：进仪表盘汇总、
                        # used_count/累计 token、daily_stats["upstream"]
                        try:
                            server_ref.db.increment_upstream_key_stats(
                                _kid, credits=credit, **usage)
                            if credit > 0:
                                server_ref.db.deduct_key_points(_kid, credit)
                        except Exception as e:
                            logger.error(f"[CodeBuddy中转] 标准统计写入失败: {e}")
                        server_ref._set_current_key(_key)
                        # 记录每条请求的 token/积分消耗到使用日志
                        pt = usage.get("prompt_tokens", 0)
                        ct = usage.get("completion_tokens", 0)
                        tt = usage.get("total_tokens", 0)
                        if tt > 0 or credit > 0:
                            server_ref._record_event(
                                False,
                                f"{_tag} {_path} 计费 "
                                f"输入{pt}tok+输出{ct}tok=共{tt}tok "
                                f"消耗{credit:g}积分（model={_model}）")
                        # 调用完成后异步查分刷新积分（内部 5 分钟限频，照 API 代理）
                        try:
                            server_ref.db.refresh_key_points_if_needed(_kid)
                        except Exception:
                            pass

                    server_ref._inflight_inc(key_id)
                    result = self._do_forward(
                        url, fwd_body, headers, tag=tag,
                        capture_error=True, on_stream_done=_on_done)
                    if result is None:
                        # 上游网络异常，已尽量回 502
                        server_ref._inflight_dec(key_id)
                        server_ref._record_event(
                            is_switch,
                            f"第{attempt+1}个 {tag} {urlsplit(path).path} 网络异常（model={model}）")
                        return
                    if result[0] == "ok":
                        # 成功：流式已回给客户端，统计在 _on_done 里落
                        server_ref._record_event(
                            is_switch,
                            f"{tag} {urlsplit(path).path} → {result[1]}"
                            f"（model={model}，{int((time.time()-t0)*1000)}ms）")
                        return
                    status, resp_headers, resp_body = result
                    server_ref._inflight_dec(key_id)
                    action = server_ref._classify_error(status, resp_body)
                    if action == "passthrough":
                        # 非 Key 类错误（参数错误等），原样返回给客户端
                        self._send_error_verbatim(status, resp_headers, resp_body)
                        server_ref._record_event(
                            True,
                            f"第{attempt+1}个 {tag} {urlsplit(path).path} → {status}"
                            f"（model={model}，resp={_err_preview(resp_body)}）")
                        return
                    if action == "retry":
                        # 上游临时问题（如 400 空 body），不罚 Key 直接换下一个
                        logger.warning(
                            f"[CodeBuddy中转] Key {label} 返回 {status} 空 body，换下一个 Key 重试")
                        server_ref._record_event(
                            True,
                            f"第{attempt+1}个 {tag} {urlsplit(path).path} → {status} "
                            f"400 空 body，换下一个 Key（model={model}，resp={_err_preview(resp_body)}）")
                        last_status, last_headers, last_body = status, resp_headers, resp_body
                        continue
                    reason = server_ref._punish_key(key_id, action)
                    logger.warning(
                        f"[CodeBuddy中转] Key {label} 返回 {status}，处置={action}，换下一个 Key 重试")
                    server_ref._record_event(
                        True,
                        f"第{attempt+1}个 {tag} {urlsplit(path).path} → {status} "
                        f"{reason}，换下一个 Key（model={model}，resp={_err_preview(resp_body)}）")
                    last_status, last_headers, last_body = status, resp_headers, resp_body

                # 所有 Key 都失败：把最后一次错误原样返回
                logger.error("[CodeBuddy中转] 池内无可用 Key 或全部重试失败")
                self._send_error_verbatim(last_status, last_headers, last_body)
                server_ref._record_event(
                    True, f"无可用 Key（池内全部禁用/冷却/积分不足，model={model}）")

            def _do_forward(self, url, body, headers, tag, capture_error=False,
                            on_stream_done=None):
                """转发一次请求。

                返回：
                - None: 上游请求异常（已尽量回 502 给客户端）
                - ("ok", status): 已流式回传完成（含 2xx/3xx 或非 capture 的任意状态）
                - (status, headers, body): capture_error=True 且 >=400，交调用方决策
                """
                try:
                    resp = server_ref._session.request(
                        method=self.command,
                        url=url,
                        headers=headers,
                        data=body if body else None,
                        stream=True,
                        timeout=(10, None),
                        proxies={"http": None, "https": None},
                        allow_redirects=False,
                    )
                except requests.RequestException as e:
                    logger.error(f"[CodeBuddy中转] {tag} 上游请求异常: {e}")
                    err = json.dumps({"error": f"upstream request failed: {e}"}).encode()
                    try:
                        self._send_error_verbatim(502, {"Content-Type": "application/json"}, err)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return None

                if capture_error and resp.status_code >= 400:
                    try:
                        err_body = resp.content
                    finally:
                        resp.close()
                    logger.warning(
                        f"[CodeBuddy中转] {tag} 上游返回 {resp.status_code} "
                        f"body={err_body[:200]!r}")
                    return resp.status_code, dict(resp.headers), err_body

                logger.info(
                    f"[CodeBuddy中转] {tag} 上游返回 {resp.status_code}，开始回传 "
                    f"{self.command} {self.path}")
                try:
                    self._send_stream(resp.status_code, dict(resp.headers), resp,
                                      on_done=on_stream_done)
                finally:
                    resp.close()
                return ("ok", resp.status_code)

        return RelayHandler


# ═══════════ CodeBuddy 客户端配置（settings.json + 开发者模式）═══════════

# CodeBuddy 客户端配置（settings.json + 开发者模式）— 跨平台路径
import sys as _sys
if _sys.platform == "win32":
    _appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    CODEBUDDY_USER_DIR = os.path.join(_appdata, "CodeBuddy CN", "User")
elif _sys.platform == "darwin":
    CODEBUDDY_USER_DIR = os.path.expanduser("~/Library/Application Support/CodeBuddy CN/User")
else:
    CODEBUDDY_USER_DIR = os.path.expanduser("~/.config/CodeBuddy CN/User")
CODEBUDDY_SETTINGS_PATH = os.path.join(CODEBUDDY_USER_DIR, "settings.json")
CODEBUDDY_STATE_DB = os.path.join(CODEBUDDY_USER_DIR, "globalStorage", "state.vscdb")
CODEBUDDY_MEMENTO_KEY = "Tencent-Cloud.coding-copilot"

# 本功能写入 settings.json 的两个键（还原时只删这两个，不动其他配置）
SETTING_ROUTE_MODE = "codingcopilot.envRouteMode"
SETTING_ENDPOINT = "codingcopilot.endpoint"


def is_codebuddy_running() -> bool:
    """CodeBuddy CN 是否在运行"""
    try:
        if _sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq CodeBuddy CN.exe"],
                capture_output=True, timeout=5, text=True,
            )
            return "CodeBuddy CN.exe" in r.stdout
        else:
            r = subprocess.run(
                ["pgrep", "-f", "CodeBuddy CN"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
    except Exception:
        return False


def is_codebuddy_installed() -> bool:
    """CodeBuddy CN 是否安装过（配置目录存在即视为安装）"""
    return os.path.isdir(CODEBUDDY_USER_DIR)


def _load_client_settings() -> dict:
    try:
        with open(CODEBUDDY_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_client_settings(data: dict):
    """原子写 settings.json（首次先备份）"""
    bak = CODEBUDDY_SETTINGS_PATH + ".bak-antigravity"
    if not os.path.exists(bak) and os.path.exists(CODEBUDDY_SETTINGS_PATH):
        try:
            with open(CODEBUDDY_SETTINGS_PATH, "r", encoding="utf-8") as f:
                raw = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(raw)
        except OSError as e:
            logger.warning(f"[CodeBuddy配置] 备份 settings.json 失败: {e}")
    tmp = CODEBUDDY_SETTINGS_PATH + ".tmp-antigravity"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, CODEBUDDY_SETTINGS_PATH)


def get_client_config_state(port: int) -> dict:
    """读取客户端当前配置状态（GUI 展示用）"""
    settings = _load_client_settings()
    endpoint = settings.get(SETTING_ENDPOINT, "")
    return {
        "route_mode": settings.get(SETTING_ROUTE_MODE, ""),
        "endpoint": endpoint,
        "pointed_to_us": endpoint == f"http://127.0.0.1:{port}",
        "dev_mode": is_dev_mode_enabled(),
    }


def apply_client_config(port: int) -> tuple:
    """把 CodeBuddy 的 API 端点指向本地中转（settings.json 热加载，即时生效）"""
    try:
        settings = _load_client_settings()
        settings[SETTING_ROUTE_MODE] = "custom"
        settings[SETTING_ENDPOINT] = f"http://127.0.0.1:{port}"
        _save_client_settings(settings)
        logger.info(f"[CodeBuddy配置] 端点已指向 http://127.0.0.1:{port}")
        return True, f"已写入端点 http://127.0.0.1:{port}"
    except OSError as e:
        logger.error(f"[CodeBuddy配置] 写入 settings.json 失败: {e}")
        return False, f"写入失败: {e}"


def restore_client_config() -> tuple:
    """还原 CodeBuddy 端点配置（只删本功能写入的两个键）"""
    try:
        settings = _load_client_settings()
        changed = False
        for key in (SETTING_ROUTE_MODE, SETTING_ENDPOINT):
            if key in settings:
                del settings[key]
                changed = True
        if changed:
            _save_client_settings(settings)
        logger.info("[CodeBuddy配置] 端点配置已还原")
        return True, "已还原官方端点"
    except OSError as e:
        logger.error(f"[CodeBuddy配置] 还原 settings.json 失败: {e}")
        return False, f"还原失败: {e}"


def is_dev_mode_enabled() -> bool:
    """读取扩展 globalState，判断开发者模式是否已开启"""
    if not os.path.exists(CODEBUDDY_STATE_DB):
        return False
    try:
        con = sqlite3.connect(f"file:{CODEBUDDY_STATE_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key=?",
                (CODEBUDDY_MEMENTO_KEY,),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return False
        data = json.loads(row[0])
        return bool(data.get("state.developer-mode", {}).get("enable"))
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"[CodeBuddy配置] 读取开发者模式状态失败: {e}")
        return False


def enable_dev_mode() -> tuple:
    """开启扩展开发者模式（自定义端点的前置条件，一次性）。

    注意：运行中的 CodeBuddy 会把内存里的 globalState 在退出时刷盘覆盖，
    所以必须在 CodeBuddy 完全退出后调用。
    """
    if is_codebuddy_running():
        return False, "CodeBuddy 正在运行，请先完全退出再开启（退出后点一次即可，永久生效）"
    if not os.path.exists(CODEBUDDY_STATE_DB):
        return False, f"找不到 {CODEBUDDY_STATE_DB}"
    try:
        con = sqlite3.connect(CODEBUDDY_STATE_DB)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key=?",
                (CODEBUDDY_MEMENTO_KEY,),
            ).fetchone()
            data = json.loads(row[0]) if row else {}
            data["state.developer-mode"] = {"enable": True}
            con.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                (CODEBUDDY_MEMENTO_KEY, json.dumps(data, ensure_ascii=False)),
            )
            con.commit()
        finally:
            con.close()
        logger.info("[CodeBuddy配置] 开发者模式已开启")
        return True, "开发者模式已开启"
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"[CodeBuddy配置] 开启开发者模式失败: {e}")
        return False, f"写入失败: {e}"


# ═══════════ WorkBuddy 客户端配置（CLI settings.json env 覆写）═══════════
#
# WorkBuddy 的 AI 调用由内置 CodeBuddy CLI 发出，CLI 的设置链包含
# ~/.workbuddy/settings.json，其中 env.CODEBUDDY_BASE_URL 可覆写 API 根地址
# （官方机制，settings env 与进程环境变量等价，CLI 新会话启动时读取）。
# 指向本地中转后，计费路径的 token 由 Key 池替换，其余透传。

WORKBUDDY_SETTINGS_PATH = os.path.expanduser("~/.workbuddy/settings.json")
WB_ENV_KEY = "CODEBUDDY_BASE_URL"


def is_workbuddy_installed() -> bool:
    """WorkBuddy 是否安装过（配置目录存在即视为安装）"""
    return os.path.isdir(os.path.dirname(WORKBUDDY_SETTINGS_PATH))


def _load_wb_settings() -> dict:
    try:
        with open(WORKBUDDY_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_wb_settings(data: dict):
    """原子写 settings.json（首次先备份）"""
    bak = WORKBUDDY_SETTINGS_PATH + ".bak-antigravity"
    if not os.path.exists(bak) and os.path.exists(WORKBUDDY_SETTINGS_PATH):
        try:
            with open(WORKBUDDY_SETTINGS_PATH, "r", encoding="utf-8") as f:
                raw = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(raw)
        except OSError as e:
            logger.warning(f"[WorkBuddy配置] 备份 settings.json 失败: {e}")
    tmp = WORKBUDDY_SETTINGS_PATH + ".tmp-antigravity"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WORKBUDDY_SETTINGS_PATH)


def get_workbuddy_config_state(port: int) -> dict:
    """读取 WorkBuddy 当前配置状态（GUI 展示用）"""
    settings = _load_wb_settings()
    base_url = (settings.get("env") or {}).get(WB_ENV_KEY, "")
    return {
        "base_url": base_url,
        "pointed_to_us": base_url == f"http://127.0.0.1:{port}",
        "settings_exists": os.path.exists(WORKBUDDY_SETTINGS_PATH),
    }


def apply_workbuddy_config(port: int) -> tuple:
    """把 WorkBuddy 的 CLI API 根地址指向本地中转（新会话生效，不用重启）"""
    try:
        settings = _load_wb_settings()
        env = settings.get("env")
        if not isinstance(env, dict):
            env = {}
        env[WB_ENV_KEY] = f"http://127.0.0.1:{port}"
        settings["env"] = env
        _save_wb_settings(settings)
        logger.info(f"[WorkBuddy配置] CODEBUDDY_BASE_URL 已指向 http://127.0.0.1:{port}")
        return True, "已写入，WorkBuddy 新会话生效"
    except OSError as e:
        logger.error(f"[WorkBuddy配置] 写入 settings.json 失败: {e}")
        return False, f"写入失败: {e}"


def restore_workbuddy_config() -> tuple:
    """还原 WorkBuddy 端点配置（只删本功能写入的 env 键）"""
    try:
        settings = _load_wb_settings()
        env = settings.get("env")
        if isinstance(env, dict) and WB_ENV_KEY in env:
            del env[WB_ENV_KEY]
            if not env:
                del settings["env"]
            _save_wb_settings(settings)
        logger.info("[WorkBuddy配置] 端点配置已还原")
        return True, "已还原官方端点"
    except OSError as e:
        logger.error(f"[WorkBuddy配置] 还原 settings.json 失败: {e}")
        return False, f"还原失败: {e}"
