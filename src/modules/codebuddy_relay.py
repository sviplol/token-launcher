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
import shutil
import sqlite3
import subprocess
import sys
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
    # 媒体链路（2026-09-15新增：图片/视频生成走中转，CLI的WB_MEDIA_URL patch指向本地8003）
    "/v2/images/generations",
    "/v2/images/edits",
    "/v2/videos/generations",
}
# 需要替换 token 的计费路径（前缀匹配）
SWAP_PATHS_PREFIX = (
    "/v2/chat/queue/",
    # 视频任务轮询（高频POST，也计费）
    "/v2/videos/tasks",
)

# Qoder daemon 私有头（转发腾讯上游会触发安全拦截——request illegal）
_QODER_STRIP_HEADERS = {
    "x-request-id", "x-session-id", "x-machine-id", "x-client-type",
    "x-task-id", "x-qcs-request-id", "x-feature-gate",
}

# WorkBuddy 内嵌 CLI 的 OpenAI 客户端把 CODEBUDDY_BASE_URL 原样当 baseURL
# （不像内部 v2 客户端会补 /v2），所以它的请求是不带版本前缀的裸路径，
# 直接打上游会 302 到别的域名导致 CLI 失败。这里统一补上 /v2 再转发。
_BARE_OPENAI_PREFIXES = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/audio/",
)
# OpenAI 标准的 /v1 前缀（Qoder BYOK / 通用 OpenAI 客户端用）也归一到 /v2
_V1_OPENAI_PREFIXES = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/models",
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/videos/generations",
)

# Qoder daemon（http transport）的模型前缀——model_server 的全部路径
# chat=/model/v1/chat/completions，模型目录=/model/v1/models（目录请求
# 不能转发腾讯上游——需本地返回我们的模型列表，见 handler 内处理）
_QODER_MODEL_PREFIX = "/model/v1/"

# ============ Qoder 模型映射（2026-09-17，基于本机 WorkBuddy models.json 实测ID） ============
# Qoder 用户在 BYOK 里填原生模型名，中转改写为上游实际模型 ID 再转发——
# 计费统一走 Key 池积分。
# 上游真实模型（~/.workbuddy/models.json 实测）：hy4-preview / hy3 /
# deepseek-v4-pro / deepseek-v4.1-flash / glm-5.3 / glm-5.3-flash / glm-5.2 /
# glm-5.1 / glm-5v-turbo / minimax-m3 / kimi-k3 / kimi-k2.7 / kimi-k2.6
# 用户定案：Qoder 的 qwen 系列全部用 hy4/hy3 代替。
QODER_MODEL_MAP = {
    # Qoder 千问系列 → 混元 hy4/hy3（用户定案：qwen 全部用 hy3/4 代替）
    "qwen3.8-max": "hy4-preview",
    "qwen3.7-max": "hy4-preview",
    "qwen3.7-plus": "hy3",
    # 重名直接透传（上游真实存在）
    "glm-5.2": "glm-5.2",
    "glm-5.3": "glm-5.3",
    "kimi-k3": "kimi-k3",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4.1-flash",
    # 同家族就近替代（上游真实ID）
    "kimi-k2.7-code": "kimi-k2.7",
    "minimax-m3": "minimax-m3",
}


def _map_qoder_model(body: bytes) -> bytes:
    """Qoder daemon 请求体清洗：模型映射 + 剥离 Qoder 私有扩展字段。

    http transport 模式的请求带 metadata/custom_model/patches 等 Qoder
    私有字段——腾讯上游安全策略对未知字段返回 request illegal，
    转发前剥掉（研究实证：剥后即标准 OpenAI Chat Completions）。
    """
    if not body:
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict):
        return body
    changed = False
    for k in ("metadata", "custom_model", "patches"):
        if k in data:
            del data[k]
            changed = True
    model = data.get("model")
    if isinstance(model, str):
        mapped = QODER_MODEL_MAP.get(model)
        if mapped and mapped != model:
            data["model"] = mapped
            changed = True
            logger.info(f"[CodeBuddy中转] 模型映射: {model} → {mapped}（Qoder）")
    if not changed:
        return body
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError):
        return body


def _normalize_upstream_path(path: str) -> str:
    """裸 OpenAI 路径补 /v2 前缀；/v1 前缀归一到 /v2；其余原样

    Qoder daemon（QODER_MODEL_TRANSPORT=http 模式）发的是
    /model/v1/chat/completions——归一到 /v2/chat/completions 走计费换token
    （2026-09-19：Qoder官方模型无感接入通道）。
    """
    p = urlsplit(path).path
    if p.startswith(_BARE_OPENAI_PREFIXES):
        return "/v2" + path
    if p in _V1_OPENAI_PREFIXES:
        return path.replace("/v1/", "/v2/", 1)
    if p.startswith("/model/v1/"):
        return path.replace("/model/v1/", "/v2/", 1)
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


class _ThreadingHTTPSServer(ThreadingMixIn, HTTPServer):
    """HTTPS 中转（Qoder daemon 无感通道：https://127.0.0.1:8003/model/v1/...）

    自签证书经 qoder_seamless 生成并导入 Windows 信任后，
    daemon 的 fetch 信任本机中转——零文件修改接管官方模型 chat。
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, certfile, keyfile):
        import ssl as _ssl
        self._ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(certfile, keyfile)
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = super().get_request()
        try:
            return self._ctx.wrap_socket(sock, server_side=True), addr
        except Exception:
            # TLS 握手失败（如健康探测发来明文 HTTP）——静默丢弃
            try:
                sock.close()
            except OSError:
                pass
            raise


class CodeBuddyRelayServer:
    """CodeBuddy 透明中转服务（与 ProxyServer 同款生命周期接口）"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8003):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.db = ProxyDatabase.get_instance()
        self.router = ProxyRouter(self.db)
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._httpsd = None
        self._https_thread = None
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
            # 8003 保持纯 HTTP（WorkBuddy/CodeBuddy CN 已验证链路——绝不能动）
            self._httpd = _ThreadingHTTPServer((self.host, self.port), handler)
            # HTTPS 旁路端口（+1）：Qoder daemon 无感通道专用
            # （单端口无法同时听明文HTTP和TLS——分端口最稳）
            self._httpsd = None
            cert = os.path.join(os.path.expanduser("~"), ".token-relay", "tls", "server.pem")
            key = os.path.join(os.path.expanduser("~"), ".token-relay", "tls", "server.key")
            if os.path.isfile(cert) and os.path.isfile(key):
                try:
                    self._httpsd = _ThreadingHTTPSServer(
                        (self.host, self.port + 1), handler, cert, key)
                    self._https_thread = threading.Thread(
                        target=self._httpsd.serve_forever, daemon=True)
                    self._https_thread.start()
                    logger.info(f"[CodeBuddy中转] HTTPS 旁路已启动 "
                                f"https://127.0.0.1:{self.port + 1}（Qoder 无感通道）")
                except OSError as e:
                    logger.warning(f"[CodeBuddy中转] HTTPS 旁路启动失败: {e}")
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
        if getattr(self, "_httpsd", None):
            try:
                self._httpsd.shutdown()
                self._httpsd.server_close()
            except Exception as e:
                logger.error(f"[CodeBuddy中转] HTTPS旁路停止异常: {e}")
            self._httpsd = None
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

                # Qoder daemon（http transport）的模型目录请求——本地返回
                # 模型列表（OpenAI /v1/models 格式），不转发腾讯上游。
                # 目录内容 = QODER_MODEL_MAP 的全部映射源模型（Qoder 原生名）。
                if path.startswith(_QODER_MODEL_PREFIX) and "models" in path:
                    models_list = [
                        {"id": mk, "object": "model", "owned_by": "local-relay"}
                        for mk in QODER_MODEL_MAP.keys()
                    ]
                    body = json.dumps(
                        {"object": "list", "data": models_list},
                        ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    server_ref._record_event(False, "Qoder模型目录响应")
                    return

                swap = _is_swap_path(upstream_path)
                body = self._read_body()
                # Qoder BYOK 模型映射（qwen→hunyuan 等，命中映射表才改写）
                if swap and body:
                    body = _map_qoder_model(body)
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
                    # 伪装真实客户端 UA（python-requests UA 会被腾讯上游
                    # 风控层拦截 request illegal——2026-09-19实测）
                    headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                             "CodeBuddy/5.5.6 Chrome/128.0.0.0 Safari/537.36")
                    # 剥离 Qoder daemon 私有头（X-Request-ID/X-Session-ID 等——
                    # 腾讯上游安全策略对非标头返回 request illegal）
                    for _qh in list(headers.keys()):
                        if _qh.lower() in _QODER_STRIP_HEADERS:
                            del headers[_qh]
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
# .codebuddy 的 settings.json 与 .workbuddy 同构，CLI 可能读任一目录，两个都写（提示词要求）
CODEBUDDY_DOT_SETTINGS_PATH = os.path.expanduser("~/.codebuddy/settings.json")
WB_ENV_KEY = "CODEBUDDY_BASE_URL"

# ============ 媒体链路 CLI patch（2026-09-15，照抄一键部署工具验证过的方案） ============
# WorkBuddy 图片/视频生成走独立链路（ImageServiceImpl/VideoServiceImpl），
# 不看 models.json（聊天才看）。需要 patch CLI 双文件的 prepareRequest，
# 让媒体请求读 WB_MEDIA_URL env（幂等开关：env存在走中转，不存在走官方）。

# CLI 文件路径（5.6.2 起 codebuddy.js 已移除，只 patch 存在的文件）
if _sys.platform == "win32":
    _pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    CLI_FILES = [
        os.path.join(_pf, "WorkBuddy", "resources", "app.asar.unpacked", "cli", "dist", "codebuddy-headless.js"),
        os.path.join(_pf, "WorkBuddy", "resources", "app.asar.unpacked", "cli", "dist", "codebuddy.js"),
        os.path.join(_pf, "WorkBuddy", "resources", "app.asar.unpacked", "cli", "dist", "codebuddy-lite-wb.mjs"),
    ]
elif _sys.platform == "darwin":
    CLI_FILES = [
        "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/dist/codebuddy-headless.js",
        "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/dist/codebuddy.js",
    ]
else:
    CLI_FILES = []

# ===== 5.6.2 版 prepareRequest patch 串（2026-09-22：WorkBuddy 5.6.2 重构——
# 混淆变量变化+新增(0,ln.Gp)()全局auth。旧串失效导致更新后媒体链路丢失。
# 文件在 unpacked 目录=独立散文件，非等长替换安全）=====
CLI_PATCH_PAIRS_V2 = [
    # (原生串, patch串)——1号（带identityHeaders：媒体/上传链路）
    ('async prepareRequest(){let L=(0,ln.Gp)(),ei=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'ea=L?.auth?.accessToken??ei?.accessToken;if(!ea)throw Error("Authentication required. Please login first.");'
     'let es={...ei,accessToken:ea},el=(0,ln.I2)(),ec=(await this.productManager.waitConfiguration()).endpoint;'
     'if(!ec)throw Error("Base endpoint not configured.");return{auth:es,endpoint:ec,identityHeaders:el}}',
     'async prepareRequest(){let eN=process.env.WB_MEDIA_URL||"",L=(0,ln.Gp)(),'
     'ei=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'ea=L?.auth?.accessToken??ei?.accessToken;'
     'if(!ea)throw Error("Authentication required. Please login first.");'
     'let es={...ei,accessToken:ea},el=(0,ln.I2)(),'
     'ec=eN||(await this.productManager.waitConfiguration()).endpoint;'
     'if(!ec)throw Error("Base endpoint not configured.");'
     'return{auth:es,endpoint:ec,identityHeaders:el}}'),
    # 2号（纯净版：其他服务链路）
    ('async prepareRequest(){let L=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'ei=(0,ln.Gp)()?.auth?.accessToken??L?.accessToken;if(!ei)throw Error("Authentication required. Please login first.");'
     'let ea={...L,accessToken:ei},es=(await this.productManager.waitConfiguration()).endpoint;'
     'if(!es)throw Error("Base endpoint not configured.");return{auth:ea,endpoint:es}}',
     'async prepareRequest(){let eN=process.env.WB_MEDIA_URL||"",'
     'L=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'ei=(0,ln.Gp)()?.auth?.accessToken??L?.accessToken;'
     'if(!ei)throw Error("Authentication required. Please login first.");'
     'let ea={...L,accessToken:ei},es=eN||(await this.productManager.waitConfiguration()).endpoint;'
     'if(!es)throw Error("Base endpoint not configured.");return{auth:ea,endpoint:es}}'),
    # 3号（lite-wb.mjs 的 1号变体：混淆名 cT 系——带identityHeaders）
    ('async prepareRequest(){let ei=(0,cT.Gp)(),ea=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'es=ei?.auth?.accessToken??ea?.accessToken;if(!es)throw Error("Authentication required. Please login first.");'
     'let el={...ea,accessToken:es},ec=(0,cT.I2)(),eu=(await this.productManager.waitConfiguration()).endpoint;',
     'async prepareRequest(){let eN=process.env.WB_MEDIA_URL||"",ei=(0,cT.Gp)(),'
     'ea=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'es=ei?.auth?.accessToken??ea?.accessToken;if(!es)throw Error("Authentication required. Please login first.");'
     'let el={...ea,accessToken:es},ec=(0,cT.I2)(),'
     'eu=eN||(await this.productManager.waitConfiguration()).endpoint;'),
    # 4号（lite-wb.mjs 的 2号变体：纯净版）
    ('async prepareRequest(){let ei=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'ea=(0,cT.Gp)()?.auth?.accessToken??ei?.accessToken;if(!ea)throw Error("Authentication required. Please login first.");',
     'async prepareRequest(){let eN=process.env.WB_MEDIA_URL||"",'
     'ei=this.authenticationManager.currentSessionSubject.getValue()?.auth,'
     'ea=(0,cT.Gp)()?.auth?.accessToken??ei?.accessToken;'
     'if(!ea)throw Error("Authentication required. Please login first.");'),
]

# 原生 prepareRequest（在 CLI 里恰好出现2次：ImageServiceImpl/VideoServiceImpl 各一处）
CLI_NATIVE_SNIPPET = (
    'async prepareRequest(){let eA=this.authenticationManager.currentSessionSubject.getValue()?.auth;'
    'if(!eA?.accessToken)throw Error("Authentication required. Please login first.");'
    'let el=(await this.productManager.waitConfiguration()).endpoint;'
    'if(!el)throw Error("Base endpoint not configured.");return{auth:eA,endpoint:el}}'
)
# patch 后版本：WB_MEDIA_URL 存在→走它+用WB_MEDIA_KEY；不存在→官方原逻辑（零影响）
CLI_PATCHED_SNIPPET = (
    'async prepareRequest(){let eN=process.env.WB_MEDIA_URL;if(eN){'
    'let eK=process.env.WB_MEDIA_KEY||"";'
    'if(!eK){try{let _s=JSON.parse(require("fs").readFileSync(require("os").homedir()+"/.workbuddy/settings.json","utf8"));'
    'eK=(_s.env&&_s.env.WB_MEDIA_KEY)||""}catch(e){}}'
    'let eA2=eK?{accessToken:eK}:this.authenticationManager.currentSessionSubject.getValue()?.auth;'
    'if(!eA2?.accessToken)throw Error("Authentication required. Please login first.");'
    'return{auth:eA2,endpoint:eN.replace(/\\/$/,"")}}'
    'let eA=this.authenticationManager.currentSessionSubject.getValue()?.auth;'
    'if(!eA?.accessToken)throw Error("Authentication required. Please login first.");'
    'let el=(await this.productManager.waitConfiguration()).endpoint;'
    'if(!el)throw Error("Base endpoint not configured.");return{auth:eA,endpoint:el}}'
)


def _load_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_json_file_atomic(path: str, data: dict):
    """原子写 JSON（首次先备份，不破坏其他字段）"""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    bak = path + ".bak-antigravity"
    if not os.path.exists(bak) and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(raw)
        except OSError as e:
            logger.warning(f"[媒体链路] 备份 {os.path.basename(path)} 失败: {e}")
    tmp = path + ".tmp-antigravity"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cli_patch_status() -> dict:
    """检测 CLI 双文件的 patch 状态（GUI展示+一键修复用）"""
    result = {"files": [], "all_patched": True, "any_exists": False}
    for fp in CLI_FILES:
        exists = os.path.exists(fp)
        patched = False
        if exists:
            result["any_exists"] = True
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    c = f.read()
                patched = "process.env.WB_MEDIA_URL" in c
            except OSError:
                pass
        if not (exists and patched):
            result["all_patched"] = False
        result["files"].append({"path": fp, "exists": exists, "patched": patched})
    return result


def patch_cli_files() -> tuple:
    """patch CLI 文件（v2：兼容5.6.2新版+旧版；幂等；每处串出现1次才patch防误伤）

    WorkBuddy 升级会还原这些文件 → 调用方可定期用 cli_patch_status() 检测，
    缺失则重新调本函数（一键修复）。
    v2逻辑（2026-09-22）：先试5.6.2新串（CLI_PATCH_PAIRS_V2，逐对独立判定），
    再试旧串（CLI_NATIVE_SNIPPET，老版本兼容）。
    """
    if not CLI_FILES:
        return False, "当前平台不支持 CLI patch"
    patched_count = 0
    for fp in CLI_FILES:
        if not os.path.exists(fp):
            continue  # 5.6.2 移除了 codebuddy.js——不存在的文件静默跳过
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError as e:
            return False, f"读取失败: {os.path.basename(fp)}: {e}"
        if "process.env.WB_MEDIA_URL" in content:
            patched_count += 1
            continue  # 已patch（幂等跳过）
        changed = False
        # v2新串（5.6.2）：逐对检查（每对出现1次）
        for native, patched in CLI_PATCH_PAIRS_V2:
            n = content.count(native)
            if n == 1:
                content = content.replace(native, patched)
                changed = True
            elif n > 1:
                return False, (f"{os.path.basename(fp)} v2串出现{n}次(应为1)，中止防误伤")
        # 旧串（5.6.2之前版本）：出现2次
        if not changed and CLI_NATIVE_SNIPPET in content:
            n_native = content.count(CLI_NATIVE_SNIPPET)
            if n_native != 2:
                return False, (f"{os.path.basename(fp)} 旧串出现{n_native}次(应为2)，"
                               f"疑似版本更新，为防误伤已中止")
            content = content.replace(CLI_NATIVE_SNIPPET, CLI_PATCHED_SNIPPET)
            changed = True
        if not changed:
            continue  # 本文件没有可patch串（可能是不含媒体逻辑的lite版）
        # 备份原文件（.orig放同目录）
        orig = fp + ".orig"
        if not os.path.exists(orig):
            try:
                shutil.copy2(fp, orig)
            except OSError as e:
                return False, f"备份失败: {e}"
        try:
            tmp = fp + ".tmp-patch"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, fp)
        except OSError as e:
            return False, f"写入失败: {os.path.basename(fp)}: {e}"
        patched_count += 1
        logger.info(f"[媒体patch] {os.path.basename(fp)} patch成功(v2多串→走WB_MEDIA_URL)")
    if patched_count == 0:
        return False, "未找到可patch的CLI文件（WorkBuddy未安装？）"
    return True, f"CLI patch完成({patched_count}文件)"


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
    env = settings.get("env") or {}
    base_url = env.get(WB_ENV_KEY, "")
    media_url = env.get(WB_MEDIA_URL_KEY, "")
    cli = cli_patch_status()
    return {
        "base_url": base_url,
        "pointed_to_us": base_url == f"http://127.0.0.1:{port}",
        "media_url": media_url,
        "media_pointed_to_us": media_url == f"http://127.0.0.1:{port}",
        "cli_patched": cli["all_patched"],
        "cli_files": cli["files"],
        "settings_exists": os.path.exists(WORKBUDDY_SETTINGS_PATH),
    }


WB_MEDIA_URL_KEY = "WB_MEDIA_URL"
WB_MEDIA_KEY_KEY = "WB_MEDIA_KEY"


def _write_media_env_to_settings(path: str, port: int) -> bool:
    """把聊天+媒体端点写进指定 settings.json（合并env段，不覆盖其他字段）"""
    data = _load_json_file(path)
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
    env[WB_ENV_KEY] = f"http://127.0.0.1:{port}"
    env[WB_MEDIA_URL_KEY] = f"http://127.0.0.1:{port}"
    data["env"] = env
    try:
        _save_json_file_atomic(path, data)
        return True
    except OSError as e:
        logger.warning(f"[媒体链路] 写入 {path} 失败: {e}")
        return False


def _clear_media_env_from_settings(path: str) -> bool:
    """从指定 settings.json 删除本功能写入的端点键（保留其他工具的键如WB_MEDIA_KEY）"""
    data = _load_json_file(path)
    env = data.get("env")
    changed = False
    if isinstance(env, dict):
        for k in (WB_ENV_KEY, WB_MEDIA_URL_KEY):
            if env.get(k) and env[k].startswith("http://127.0.0.1:"):
                del env[k]
                changed = True
        if changed:
            if not env:
                data.pop("env", None)
            else:
                data["env"] = env
            try:
                _save_json_file_atomic(path, data)
            except OSError as e:
                logger.warning(f"[媒体链路] 还原 {path} 失败: {e}")
                return False
    return changed


def apply_workbuddy_config(port: int) -> tuple:
    """把 WorkBuddy 的聊天+媒体端点全部指向本地中转（新会话生效，不用重启）

    ★2026-09-15 媒体链路完整三件套（照抄一键部署工具验证过的方案）：
    1. settings.json env段写 WB_MEDIA_URL —— .workbuddy 和 .codebuddy 两个目录都写
    2. patch CLI 双文件（codebuddy-headless.js + codebuddy.js）的 prepareRequest ——
       ImageServiceImpl/VideoServiceImpl 读 WB_MEDIA_URL 走中转（幂等开关）
    3. WB_MEDIA_KEY 不写（留空）—— patch 里 key 为空时回退官方登录态 accessToken，
       中转对计费路径会强制换 Key 池 token，所以无影响
    停止接入时删端点键（媒体回官方），CLI patch 保留（幂等无副作用，WorkBuddy升级还原后
    下次启动接入自动重打）。
    """
    # 1. 写 settings.json 双目录
    ok_wb = _write_media_env_to_settings(WORKBUDDY_SETTINGS_PATH, port)
    ok_cb = True
    if os.path.isdir(os.path.expanduser("~/.codebuddy")):
        ok_cb = _write_media_env_to_settings(CODEBUDDY_DOT_SETTINGS_PATH, port)
    if not ok_wb:
        return False, "写入 settings.json 失败"
    # 2. patch CLI 双文件（幂等）
    patch_ok, patch_msg = patch_cli_files()
    if not patch_ok:
        logger.warning(f"[媒体链路] CLI patch未完成: {patch_msg}（聊天走中转正常，媒体可能走官方）")
    else:
        logger.info(f"[媒体链路] {patch_msg}")
    logger.info(f"[WorkBuddy配置] 聊天+媒体端点已指向 http://127.0.0.1:{port}（settings双目录）")
    msg = "已写入，WorkBuddy 新会话生效（聊天+图片+视频全部走中转）"
    if not patch_ok:
        msg += f"；注意: CLI patch未完成（{patch_msg}），图片/视频可能仍走官方"
    return True, msg


def restore_workbuddy_config(restart_wb: bool = True) -> tuple:
    """还原 WorkBuddy 端点配置（只删本功能写入的 env 键）

    ★restart_wb=True（默认）时重启 WorkBuddy 进程：
    WorkBuddy 的 CLI 在启动时读取 settings.json 的 env 并缓存在内存，
    只改文件不杀进程→运行中的 CLI 继续打 8003→中转停了就报
    "connect ECONNREFUSED 127.0.0.1:8003"（3002错误）。
    杀掉 WorkBuddy 让它下次启动重读 settings 才是真正的断开。
    """
    try:
        changed = _clear_media_env_from_settings(WORKBUDDY_SETTINGS_PATH)
        if os.path.exists(CODEBUDDY_DOT_SETTINGS_PATH):
            changed2 = _clear_media_env_from_settings(CODEBUDDY_DOT_SETTINGS_PATH)
            changed = changed or changed2
        logger.info("[WorkBuddy配置] 端点配置已还原（聊天+媒体，双目录；CLI patch保留幂等无副作用）")
        # 重启 WorkBuddy（杀旧进程，让 CLI 重读 settings.json）
        if restart_wb and changed:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/IM", "WorkBuddy.exe", "/F"],
                                   capture_output=True, timeout=10)
                else:
                    subprocess.run(["pkill", "-f", "WorkBuddy"],
                                   capture_output=True, timeout=10)
                logger.info("[WorkBuddy配置] 已停止 WorkBuddy 进程（CLI 将重读官方端点）")
                # 延迟2秒后拉起 WorkBuddy（用户无感重启）
                import threading
                def _relaunch():
                    import time as _t
                    _t.sleep(2)
                    try:
                        wb_exe = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WorkBuddy", "WorkBuddy.exe")
                        if sys.platform != "win32":
                            wb_exe = "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"
                        if os.path.exists(wb_exe):
                            subprocess.Popen([wb_exe], start_new_session=True,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            logger.info("[WorkBuddy配置] WorkBuddy 已重新启动（官方端点）")
                    except OSError as e:
                        logger.warning(f"[WorkBuddy配置] 重启 WorkBuddy 失败: {e}")
                threading.Thread(target=_relaunch, daemon=True).start()
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.warning(f"[WorkBBuddy配置] 停止 WorkBuddy 进程失败: {e}")
        return True, "已还原官方端点" + ("（WorkBuddy 将自动重启）" if changed and restart_wb else "")
    except OSError as e:
        logger.error(f"[WorkBuddy配置] 还原 settings.json 失败: {e}")
        return False, f"还原失败: {e}"
