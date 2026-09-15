"""Trae CN 无感换号中转（完全独立的账号/池子体系，与 API 代理、无感换号互不相通）

原理（2026-08-10 PoC 实证）：
- Trae 的 AI 域名在 product.json bootConfig 里，字节级补丁指向本地中转 + ad-hoc 重签即可接管；
- 聊天/计费请求带 x-ide-token（完整 JWT）/ Authorization: Cloud-IDE-JWT / X-Ckg-User-Id 三个身份头，
  中转只在计费路径把它们换成池子账号的，body（密文）原样透传；
- 其余请求（会话列表/配置/插件等）带真身 token 透传，客户端登录态与数据不受影响；
- 池子账号是 cockpit 导入 JSON（access_token/refresh_token/trae_auth_raw），
  独立存放在 ~/.antigravity-tools/trae_pool.json，不进 ProxyDatabase。

注意：Trae 升级会覆盖 product.json 补丁，需要重新「接入」。
"""

import datetime
import ipaddress
import json
import logging
import os
import ssl
import subprocess
import threading
import time
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

UPSTREAM_BASE = "https://trae-api-cn.mchost.guru"
# UG/账号服务域名（积分查询、签到等，不走中转，直连）
# 注意：积分接口在 .com.cn（any-auto-register 研究成果），签到在 .cn
TRAE_UG_BASE = "https://api.trae.cn"
TRAE_ENT_USAGE_URL = "https://api.trae.com.cn/trae/api/v2/pay/ide_user_ent_usage"

# 计费路径（前缀匹配）：换池子账号身份头
# 注意：/api/agent/ 不能整前缀换——query_history_state 等历史态查询
# 带真身会话 id 去池子账号查会拿到 missing:null（客户端解析报错弹网络错误），
# 只有真正创建/执行计费任务的路径才换号
SWAP_PATH_PREFIXES = (
    "/api/agent/v3/workflow/",
    "/api/cue_agent/",
)
# 计费路径（精确匹配，不含 query）
SWAP_PATHS_EXACT = {
    "/api/agent/v3/create_agent_task",
    "/api/agent/v3/llm_utils_chat",
    "/api/ide/v2/llm_raw_chat",
    "/api/ide/v1/super_completion",
    "/api/ide/v1/super_completion_query",
}

_REQ_SKIP_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "content-length", "accept-encoding",
}
_RESP_SKIP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "content-length",
    "content-encoding",
}

_MAX_ATTEMPTS = 3

DATA_DIR = os.path.expanduser("~/.antigravity-tools")
TRAE_POOL_PATH = os.path.join(DATA_DIR, "trae_pool.json")
CERT_PATH = os.path.join(DATA_DIR, "trae_relay_cert.pem")
KEY_PATH = os.path.join(DATA_DIR, "trae_relay_key.pem")

TRAE_APP_PATH = "/Applications/TRAE SOLO CN.app"
TRAE_PRODUCT_JSON = os.path.join(TRAE_APP_PATH, "Contents/Resources/app/product.json")
TRAE_USER_DIR = os.path.expanduser("~/Library/Application Support/TRAE SOLO CN")
TRAE_ARGV_JSON = os.path.join(TRAE_USER_DIR, "argv.json")
TRAE_AHANET_CONFIG = os.path.join(TRAE_USER_DIR, "ahanet", "server.json")
_ORIGIN_DOMAIN = b"https://trae-api-cn.mchost.guru"


# ═══════════ 池子（独立 JSON 存储）═══════════

def _load_pool() -> list:
    try:
        with open(TRAE_POOL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_pool(pool: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TRAE_POOL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TRAE_POOL_PATH)


def list_trae_accounts() -> list:
    """池子列表（脱敏：不带 token 全文）"""
    result = []
    for acc in _load_pool():
        result.append({k: v for k, v in acc.items()
                       if k not in ("access_token", "refresh_token", "auth_raw")})
    return result


def _iso_to_ms_timestamp(value) -> int:
    """过期时间统一转毫秒时间戳：支持 ISO 字符串（expiredAt: 2026-08-24T12:28:24.137Z）
    与数字时间戳（expires_at: 1787544206203）。解析失败返回 0。"""
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def add_trae_account(import_json: str) -> tuple:
    """从 JSON 添加账号。支持对象（单号）与数组（批量）两种容器，账号字段兼容：
    - cockpit 导入：access_token / trae_auth_raw / user_id / refresh_token / expires_at
    - 官方 OAuth 响应：token / refreshToken / userId / account / deviceInfo / expiredAt
    返回 (ok, message)"""
    try:
        data = json.loads(import_json.strip())
    except ValueError as e:
        return False, f"JSON 解析失败: {e}"
    if isinstance(data, list):
        # 数组格式：[{...}] 批量导入，逐个添加，任一失败即中断并报错
        added = []
        for item in data:
            if not isinstance(item, dict):
                return False, "数组里存在非对象元素，导入中止"
            ok, msg = _add_trae_account_one(item)
            if not ok:
                return False, msg
            added.append(msg.replace("已添加 ", ""))
        if not added:
            return False, "JSON 数组为空，没有可导入的账号"
        return True, f"已添加 {len(added)} 个账号: {', '.join(added)}"
    if not isinstance(data, dict):
        return False, "不是有效的 Trae 导入 JSON（应为 JSON 对象或数组）"
    return _add_trae_account_one(data)


def _add_trae_account_one(data: dict) -> tuple:
    """添加单个账号（对象格式，字段名双兼容）。返回 (ok, message)"""
    raw = data.get("trae_auth_raw") or {}
    token = data.get("access_token") or data.get("token") or ""
    refresh = data.get("refresh_token") or data.get("refreshToken") or ""
    uid = str(data.get("user_id") or data.get("userId") or raw.get("userId") or "")
    nickname = (data.get("nickname")
                or (raw.get("account") or {}).get("username")
                or (data.get("account") or {}).get("username") or uid)
    device_id = str((raw.get("deviceInfo") or {}).get("DeviceID", "")
                    or (data.get("deviceInfo") or {}).get("DeviceID", ""))
    expires_at = _iso_to_ms_timestamp(
        data.get("expires_at") or data.get("expiresAt") or data.get("expiredAt") or 0)
    if not token.startswith("eyJ") or not uid:
        return False, "不是有效的 Trae 导入 JSON（缺 access_token 或 userId）"
    pool = _load_pool()
    if any(a.get("uid") == uid for a in pool):
        return False, f"账号 {uid} 已在池子里"
    pool.append({
        "uid": uid,
        "nickname": nickname,
        "access_token": token,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "device_id": device_id,
        "auth_raw": raw,
        "status": "active",
        "note": "",
        "used": 0,
        "added_at": int(time.time()),
    })
    _save_pool(pool)
    return True, f"已添加 {uid}"


def remove_trae_account(uid: str):
    _save_pool([a for a in _load_pool() if a.get("uid") != uid])


def _update_account(uid: str, fields: dict):
    pool = _load_pool()
    for a in pool:
        if a.get("uid") == uid:
            a.update(fields)
    _save_pool(pool)


def _get_account_full(uid: str) -> Optional[dict]:
    for a in _load_pool():
        if a.get("uid") == uid:
            return a
    return None


# ═══════════ Trae token 刷新（ExchangeToken，复刻 cockpit trae_oauth.rs）═══════════

TRAE_EXCHANGE_TOKEN_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
TRAE_EXCHANGE_CLIENT_SECRET = "-"
# CN 候选域名（cockpit TRAE_ACCOUNT_API_ORIGIN_CN / _CN_ICUBE）
TRAE_EXCHANGE_ORIGINS = ("https://api.trae.cn", "https://api.trae.com.cn")


def _read_trae_client_id() -> str:
    """从 Trae product.json 读 client_id（iCubeApp.authConfig.SOLO.stable，SOLO CN）。"""
    try:
        with open(TRAE_PRODUCT_JSON, encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("iCubeApp", {}).get("authConfig", {})
                   .get("SOLO", {}).get("stable", ""))
    except Exception:
        return ""


def refresh_trae_token(account: dict) -> dict:
    """用 refresh_token 刷新 access_token（cockpit request_exchange_token 同款）。

    成功返回 {"access_token", "refresh_token", "expires_at"}；失败返回 {}。
    注意：refresh_token 本身可能已失效（Trae 会返回 20101 refresh token is invalid），
    此时返回 {}，调用方应禁用该账号并提示重新登录导入。
    """
    rt = str(account.get("refresh_token") or "")
    if not rt:
        return {}
    client_id = _read_trae_client_id()
    if not client_id:
        return {}  # 读不到 client_id 不盲发
    body = {
        "ClientID": client_id,
        "RefreshToken": rt,
        "ClientSecret": TRAE_EXCHANGE_CLIENT_SECRET,
        "UserID": "",
    }
    for origin in TRAE_EXCHANGE_ORIGINS:
        try:
            resp = requests.post(
                origin + TRAE_EXCHANGE_TOKEN_PATH, json=body, timeout=15,
                headers={"Accept": "application/json",
                         "Content-Type": "application/json"},
                proxies={"http": None, "https": None},
            )
        except requests.RequestException as e:
            logger.warning(f"[Trae刷新] {origin} 请求异常: {e}")
            continue
        if resp.status_code != 200:
            logger.warning(f"[Trae刷新] {origin} 返回 {resp.status_code}: {resp.text[:150]}")
            continue
        try:
            data = resp.json()
        except ValueError:
            logger.warning(f"[Trae刷新] {origin} 响应非 JSON: {resp.text[:150]}")
            continue
        result = data.get("Result") or data.get("result") or data.get("data") or {}
        if isinstance(result, dict):
            token = (result.get("accessToken") or result.get("AccessToken")
                     or result.get("token") or result.get("Token"))
            new_rt = result.get("refreshToken") or result.get("RefreshToken")
            exp = result.get("expiresAt") or result.get("ExpiresAt")
        else:
            token = data.get("accessToken") or data.get("token")
            new_rt = data.get("refreshToken")
            exp = data.get("expiresAt")
        if not token:
            logger.warning(f"[Trae刷新] {origin} 响应缺少 accessToken: {resp.text[:150]}")
            continue
        try:
            expires_at = int(exp) if exp else 0
        except (TypeError, ValueError):
            expires_at = 0
        logger.info(f"[Trae刷新] {origin} 刷新成功")
        return {
            "access_token": str(token),
            "refresh_token": str(new_rt) if new_rt else rt,
            "expires_at": expires_at,
        }
    return {}


# ═══════════ 积分 / 状态 / 签到（UG 接口，直连 api.trae.cn）═══════════

def _ug_post(path: str, token: str, device_id: str = "", timeout: int = 15,
             base: str = "") -> tuple:
    """UG 接口 POST。返回 (status_code, json_body|None)
    device_id 用账号注册时绑定的（any-auto-register 研究结论）"""
    headers = {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-device-type": "mac",
        "x-device-id": device_id or "antigravity-tools",
    }
    try:
        url = (base or TRAE_UG_BASE) + path if path else (base or TRAE_UG_BASE)
        resp = requests.post(
            url,
            headers=headers, data=b"{}", timeout=timeout,
            proxies={"http": None, "https": None},
        )
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None
    except requests.RequestException as e:
        logger.warning(f"[Trae积分] 请求异常 {path}: {e}")
        return -1, None


def query_trae_points(uid: str) -> tuple:
    """查积分+状态。返回 (ok, 显示文本或错误)，成功时写回 points 字段。
    口径（any-auto-register 研究结论）：usage.credits_amount 是「已用」，
    剩余 = credits_limit - 已用，逐包求和。"""
    acc = _get_account_full(uid)
    if not acc:
        return False, "账号不存在"
    status, body = _ug_post("", acc["access_token"], acc.get("device_id", ""),
                            base=TRAE_ENT_USAGE_URL)
    if status in (401, 403):
        _update_account(uid, {"status": "disabled", "note": f"token 失效({status})"})
        return False, f"token 失效({status})"
    if status != 200 or not isinstance(body, dict):
        return False, f"查询失败({status})"
    remaining = 0
    total = 0
    for pack in body.get("user_entitlement_pack_list") or []:
        bi = pack.get("entitlement_base_info") or {}
        limit = (bi.get("quota") or {}).get("credits_limit") or 0
        used = (pack.get("usage") or {}).get("credits_amount") or 0
        if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
            total += int(limit)
            remaining += max(0, int(limit) - int(used))
    text = f"{remaining}/{total}"
    _update_account(uid, {"points": text, "points_updated_at": int(time.time())})
    return True, text


def checkin_trae_status(uid: str) -> tuple:
    """只读签到状态。返回 (ok, True=已签/False=未签/None=未知)，并写回 checkin 字段"""
    acc = _get_account_full(uid)
    if not acc:
        return False, None
    status, body = _ug_post("/trae/api/v2/ug/checkin_credits/status",
                            acc["access_token"], acc.get("device_id", ""))
    if status in (401, 403):
        _update_account(uid, {"status": "disabled", "note": f"token 失效({status})"})
        return False, None
    if status != 200 or not isinstance(body, dict):
        return False, None
    checked = bool(body.get("checked_in"))
    _update_account(uid, {"checkin": "已签到" if checked else "未签到"})
    return True, checked


def checkin_trae_account(uid: str) -> tuple:
    """签到（已签过的直接报，不重复领取）。返回 (ok, message)"""
    acc = _get_account_full(uid)
    if not acc:
        return False, "账号不存在"
    device_id = acc.get("device_id", "")
    status, body = _ug_post("/trae/api/v2/ug/checkin_credits/status",
                            acc["access_token"], device_id)
    if status in (401, 403):
        _update_account(uid, {"status": "disabled", "note": f"token 失效({status})"})
        return False, f"token 失效({status})"
    if status != 200:
        return False, f"状态查询失败({status})"
    if isinstance(body, dict) and body.get("checked_in"):
        _update_account(uid, {"checkin": "已签到"})
        query_trae_points(uid)
        return True, "今日已签到"
    # 未签到 → 领取
    status2, body2 = _ug_post("/trae/api/v2/ug/checkin_credits/claim",
                              acc["access_token"], device_id)
    if status2 == 200 and isinstance(body2, dict):
        _update_account(uid, {"checkin": "已签到"})
        query_trae_points(uid)
        credits = body2.get("credits")
        return True, f"签到成功{f' +{credits}' if credits else ''}"
    return False, f"签到失败({status2}): {json.dumps(body2, ensure_ascii=False)[:80] if body2 else ''}"


# ═══════════ 证书（自签，客户端走 ignore-certificate-errors）═══════════

def ensure_cert() -> tuple:
    """生成/返回自签证书路径。返回 (cert_path, key_path)"""
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return CERT_PATH, KEY_PATH
    os.makedirs(DATA_DIR, exist_ok=True)
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(KEY_PATH, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
    with open(CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("[Trae中转] 已生成自签证书")
    return CERT_PATH, KEY_PATH


# ═══════════ 中转服务 ═══════════

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """TTNet 建联竞速会开多条连接、用最快的、其余的 RST 掉，
        这类连接级噪音（ConnectionResetError/ssl EOF）不打 traceback"""
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)


def _is_swap_path(path: str) -> bool:
    p = urlsplit(path).path
    if p in SWAP_PATHS_EXACT:
        return True
    return any(p.startswith(prefix) for prefix in SWAP_PATH_PREFIXES)


class TraeRelayServer:
    """Trae 透明中转（生命周期接口与 CodeBuddyRelayServer 同款）"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8005):
        self.host = host
        self.port = port
        self.base_url = f"https://{host}:{port}"
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._session = requests.Session()
        # TTNet 并发探测多，默认连接池 10 不够
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=64)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._status_lock = threading.Lock()
        self._current_uid = ""
        self._total_requests = 0
        self._swapped_requests = 0
        self._last_event = ""
        self._events: deque = deque(maxlen=300)
        # 调用完成后自动查分的限频（每号 5 分钟，见 _maybe_refresh_points）
        self._last_points_refresh: dict = {}

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        try:
            cert, key = ensure_cert()
            handler = self._make_handler()
            self._httpd = _ThreadingHTTPServer((self.host, self.port), handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
        except OSError as e:
            logger.error(f"[Trae中转] 端口 {self.port} 启动失败: {e}")
            return False
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._running = True
        logger.info(f"[Trae中转] 已启动 {self.base_url} → {UPSTREAM_BASE}")
        return True

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception as e:
            logger.error(f"[Trae中转] 停止异常: {e}")
        self._httpd = None
        with self._status_lock:
            self._current_uid = ""
            self._last_event = "已停止"
        logger.info("[Trae中转] 已停止")

    def get_status(self) -> dict:
        with self._status_lock:
            return {
                "running": self._running,
                "port": self.port,
                "base_url": self.base_url,
                "current_uid": self._current_uid,
                "total_requests": self._total_requests,
                "swapped_requests": self._swapped_requests,
                "last_event": self._last_event,
            }

    def get_events(self) -> list:
        with self._status_lock:
            return list(reversed(self._events))

    def clear_events(self):
        with self._status_lock:
            self._events.clear()
            self._total_requests = 0
            self._swapped_requests = 0

    def _record_event(self, swapped: bool, event: str):
        with self._status_lock:
            self._total_requests += 1
            if swapped:
                self._swapped_requests += 1
            self._last_event = event
            self._events.append(f"{time.strftime('%H:%M:%S')} {event}")

    # ─── 选号（sticky：当前号仍可用就一直用）───

    def _maybe_refresh_points(self, uid: str, label: str = ""):
        """计费调用完成后自动刷分：延迟 3s 查（等上游结算落地），
        每号 5 分钟限频（一次对话多个计费请求只刷一次）。"""
        now = time.time()
        with self._status_lock:
            last = self._last_points_refresh.get(uid, 0)
            if now - last < 300:
                return
            self._last_points_refresh[uid] = now

        def _job():
            time.sleep(3)
            ok, text = query_trae_points(uid)
            if ok:
                with self._status_lock:
                    self._events.append(
                        f"{time.strftime('%H:%M:%S')} 刷分[{label or uid}] {text}")

        threading.Thread(target=_job, daemon=True).start()

    def _eligible_accounts(self, exclude: set) -> list:
        now_ms = int(time.time() * 1000)
        result = []
        for a in _load_pool():
            uid = a.get("uid", "")
            if uid in exclude or a.get("status") != "active":
                continue
            exp = int(a.get("expires_at") or 0)
            if exp and exp < now_ms:
                continue  # token 过期
            result.append(a)
        return result

    def _select_account(self, exclude: set):
        accounts = self._eligible_accounts(exclude)
        if not accounts:
            return None
        with self._status_lock:
            cur = self._current_uid
        if cur and cur not in exclude:
            for a in accounts:
                if a.get("uid") == cur:
                    return a
        accounts.sort(key=lambda a: int(a.get("used", 0)))
        return accounts[0]

    # ─── handler ───

    def _make_handler(self):
        server_ref = self

        class RelayHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):     self._relay()
            def do_POST(self):    self._relay()
            def do_PUT(self):     self._relay()
            def do_DELETE(self):  self._relay()
            def do_PATCH(self):   self._relay()
            def do_OPTIONS(self): self._relay()

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length > 0 else b""

            def _build_headers(self, account: Optional[dict]) -> dict:
                headers = {}
                for name, value in self.headers.items():
                    if name.lower() in _REQ_SKIP_HEADERS:
                        continue
                    headers[name] = value
                headers["Accept-Encoding"] = "identity"
                if account:
                    # 换号三头（2026-08-10 抓包实证）：token 两处 + 账号 id 一处
                    token = account["access_token"]
                    headers["authorization"] = f"Cloud-IDE-JWT {token}"
                    headers["x-ide-token"] = token
                    headers["X-Ckg-User-Id"] = account["uid"]
                return headers

            def _send_stream(self, resp, on_done=None):
                # 204/304/HEAD 禁止携带 body 和 Transfer-Encoding（RFC 9112），
                # 否则 Chromium 判定预检响应非法 → CORS 失败 → 前端报网络错误
                no_body = resp.status_code in (204, 304) or self.command == "HEAD"
                self.send_response(resp.status_code)
                for name, value in resp.headers.items():
                    if name.lower() in _RESP_SKIP_HEADERS:
                        continue
                    self.send_header(name, value)
                if no_body:
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    if on_done:
                        on_done(b"")
                    return
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
                            if len(collected) > 131072:
                                del collected[:-65536]
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                if on_done:
                    try:
                        on_done(bytes(collected))
                    except Exception as e:
                        logger.error(f"[Trae中转] 统计回调异常: {e}")

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

            def _do_forward(self, body, headers):
                try:
                    resp = server_ref._session.request(
                        method=self.command,
                        url=UPSTREAM_BASE + self.path,
                        headers=headers,
                        data=body if body else None,
                        stream=True,
                        timeout=(10, None),
                        proxies={"http": None, "https": None},
                        allow_redirects=False,
                    )
                    return resp
                except requests.RequestException as e:
                    logger.error(f"[Trae中转] 上游请求异常: {e}")
                    err = json.dumps({"error": f"upstream request failed: {e}"}).encode()
                    try:
                        self._send_error_verbatim(502, {"Content-Type": "application/json"}, err)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return None

            def _relay(self):
                path = self.path
                swap = _is_swap_path(path)
                body = self._read_body()
                logger.info(f"[Trae中转] 收到 {self.command} {urlsplit(path).path} "
                            f"body={len(body)}B swap={swap}")

                if not swap:
                    resp = self._do_forward(body, self._build_headers(None))
                    if resp is None:
                        return
                    logger.info(f"[Trae中转] 上游返回 {resp.status_code} {self.command} {urlsplit(path).path}")
                    try:
                        self._send_stream(resp)
                    finally:
                        resp.close()
                    server_ref._record_event(False, f"透传 {urlsplit(path).path}")
                    return

                # 计费路径：池子选号换身份，失败换号重试
                exclude = set()
                last_status, last_headers, last_body = 502, {}, (
                    b'{"error":"no available trae account in pool"}')
                for _ in range(_MAX_ATTEMPTS):
                    account = server_ref._select_account(exclude)
                    if not account:
                        break
                    uid = account["uid"]
                    exclude.add(uid)
                    label = account.get("nickname", uid)
                    with server_ref._status_lock:
                        is_switch = server_ref._current_uid != uid
                    tag = f"换号[{label}]" if is_switch else f"消耗[{label}]"

                    resp = self._do_forward(body, self._build_headers(account))
                    if resp is None:
                        return
                    if resp.status_code < 400:
                        logger.info(f"[Trae中转] 上游返回 {resp.status_code} {self.command} {urlsplit(path).path} (换号)")
                        def _on_done(_buf, _uid=uid, _acc=account):
                            _update_account(_uid, {"used": int(_acc.get("used", 0)) + 1})
                            with server_ref._status_lock:
                                server_ref._current_uid = _uid
                            server_ref._maybe_refresh_points(_uid, label)
                        try:
                            self._send_stream(resp, on_done=_on_done)
                            server_ref._record_event(
                                is_switch, f"{tag} {urlsplit(path).path} → {resp.status_code}")
                        finally:
                            resp.close()
                        return
                    # 失败：401/403 token 失效 → 禁用换号；429 → 换号重试；其余原样返回
                    status = resp.status_code
                    err_body = resp.content
                    resp_headers = dict(resp.headers)
                    resp.close()
                    logger.warning(f"[Trae中转] 上游错误 {status} {self.command} {urlsplit(path).path} body={err_body[:150]!r}")
                    if status in (401, 403):
                        # 先尝试用 refresh_token 刷新：access 过期但 refresh 有效时可救回
                        refreshed = refresh_trae_token(account)
                        if refreshed:
                            _update_account(uid, {
                                "access_token": refreshed["access_token"],
                                "refresh_token": refreshed.get("refresh_token")
                                or account.get("refresh_token", ""),
                                "expires_at": refreshed.get("expires_at") or 0,
                                "status": "active",
                                "note": "",
                            })
                            account["access_token"] = refreshed["access_token"]  # 同步内存引用
                            logger.info(
                                f"[Trae中转] 账号 {label} token 刷新成功，重试本账号")
                            exclude.discard(uid)
                            continue
                        _update_account(uid, {
                            "status": "disabled",
                            "note": f"token 失效({status})，刷新失败",
                        })
                        logger.warning(
                            f"[Trae中转] 账号 {label} 返回 {status} 且刷新失败，禁用并换号")
                        last_status, last_headers, last_body = status, resp_headers, err_body
                        continue
                    if status == 429:
                        logger.warning(f"[Trae中转] 账号 {label} 限流，换号重试")
                        last_status, last_headers, last_body = status, resp_headers, err_body
                        continue
                    self._send_error_verbatim(status, resp_headers, err_body)
                    server_ref._record_event(True, f"{tag} {urlsplit(path).path} → {status}")
                    return

                logger.error("[Trae中转] 池内无可用账号或全部重试失败")
                self._send_error_verbatim(last_status, last_headers, last_body)
                server_ref._record_event(True, "无可用账号")

        return RelayHandler


# ═══════════ Trae 客户端接入 / 还原 ═══════════

def is_trae_installed() -> bool:
    return os.path.isdir(TRAE_USER_DIR) and os.path.exists(TRAE_PRODUCT_JSON)


def is_trae_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "TRAE SOLO CN"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _resign_app() -> tuple:
    r = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", TRAE_APP_PATH],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return False, f"重签名失败: {r.stderr[:200]}"
    return True, "ok"


def apply_trae_config(port: int) -> tuple:
    """把 Trae 的 AI 域名指向本地中转（字节补丁 + 重签 + argv.json + 关 QUIC）"""
    if not is_trae_installed():
        return False, "未安装 TRAE SOLO CN"
    if is_trae_running():
        return False, "请先完全退出 TRAE SOLO CN 再接入"
    try:
        # 1. product.json 字节补丁（幂等：已指向本端口则跳过）
        raw = open(TRAE_PRODUCT_JSON, "rb").read()
        target = f"https://127.0.0.1:{port}".encode()
        if target in raw:
            pass
        elif _ORIGIN_DOMAIN in raw:
            bak = TRAE_PRODUCT_JSON + ".bak-antigravity"
            if not os.path.exists(bak):
                with open(bak, "wb") as f:
                    f.write(raw)
            raw = raw.replace(_ORIGIN_DOMAIN, target)
            with open(TRAE_PRODUCT_JSON, "wb") as f:
                f.write(raw)
        else:
            return False, "product.json 里找不到原始 AI 域名（可能已被其他补丁改过）"

        # 2. argv.json 固化证书容错（VS Code 启动参数持久化机制）
        argv = {}
        if os.path.exists(TRAE_ARGV_JSON):
            try:
                argv = json.load(open(TRAE_ARGV_JSON, "r", encoding="utf-8"))
            except ValueError:
                argv = {}
        if not argv.get("ignore-certificate-errors"):
            if not os.path.exists(TRAE_ARGV_JSON + ".bak-antigravity") and os.path.exists(TRAE_ARGV_JSON):
                with open(TRAE_ARGV_JSON + ".bak-antigravity", "wb") as f:
                    f.write(open(TRAE_ARGV_JSON, "rb").read())
            argv["ignore-certificate-errors"] = True
            with open(TRAE_ARGV_JSON, "w", encoding="utf-8") as f:
                json.dump(argv, f, ensure_ascii=False, indent=2)

        # 3. 关 TTNet QUIC/HTTPDNS/调度（不关的话原生栈连不上本地 TCP/TLS）
        if os.path.exists(TRAE_AHANET_CONFIG):
            cfg = json.load(open(TRAE_AHANET_CONFIG, "r", encoding="utf-8"))
            data = cfg.get("data", cfg)
            if not os.path.exists(TRAE_AHANET_CONFIG + ".bak-antigravity"):
                with open(TRAE_AHANET_CONFIG + ".bak-antigravity", "wb") as f:
                    f.write(open(TRAE_AHANET_CONFIG, "rb").read())
            data["ttnet_quic_enabled"] = 0
            data["ttnet_http_dns_enabled"] = 0
            data["ttnet_url_dispatcher_enabled"] = 0
            json.dump(cfg, open(TRAE_AHANET_CONFIG, "w", encoding="utf-8"), ensure_ascii=False)

        # 4. ad-hoc 重签名（改了包内容必须重签，否则被系统杀）
        ok, msg = _resign_app()
        if not ok:
            return False, msg

        # 5. 自签证书加入登录钥匙串信任（Trae 不读 argv.json 的
        # ignore-certificate-errors——实测 mainProcessArgs 里仍是 false；
        # 原生 TTNet 栈只认系统钥匙串，不信任就报 -202 证书错误）
        ensure_cert()
        trust = subprocess.run(
            ["security", "add-trusted-cert", "-r", "trustRoot", "-k",
             os.path.expanduser("~/Library/Keychains/login.keychain-db"), CERT_PATH],
            capture_output=True, text=True, timeout=30)
        if trust.returncode != 0 and b"duplicate" not in (trust.stderr or b"").lower():
            logger.warning(f"[Trae配置] 证书加信任失败（可能已存在）: {trust.stderr[:120]}")

        logger.info(f"[Trae配置] 已接入 https://127.0.0.1:{port}")
        return True, "已接入，重启 TRAE SOLO CN 后生效"
    except (OSError, ValueError) as e:
        logger.error(f"[Trae配置] 接入失败: {e}")
        return False, f"接入失败: {e}"


def restore_trae_config() -> tuple:
    """还原 Trae 客户端配置（product.json / argv.json / ahanet 全部还原 + 重签）"""
    if is_trae_running():
        return False, "请先完全退出 TRAE SOLO CN 再还原"
    try:
        changed = False
        bak = TRAE_PRODUCT_JSON + ".bak-antigravity"
        if os.path.exists(bak):
            with open(bak, "rb") as f:
                raw = f.read()
            with open(TRAE_PRODUCT_JSON, "wb") as f:
                f.write(raw)
            changed = True
        argv_bak = TRAE_ARGV_JSON + ".bak-antigravity"
        if os.path.exists(argv_bak):
            with open(argv_bak, "rb") as f:
                content = f.read()
            if content:
                with open(TRAE_ARGV_JSON, "wb") as f:
                    f.write(content)
            elif os.path.exists(TRAE_ARGV_JSON):
                os.remove(TRAE_ARGV_JSON)
            changed = True
        elif os.path.exists(TRAE_ARGV_JSON):
            try:
                argv = json.load(open(TRAE_ARGV_JSON, "r", encoding="utf-8"))
                if argv.pop("ignore-certificate-errors", None) is not None:
                    json.dump(argv, open(TRAE_ARGV_JSON, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2)
                    changed = True
            except ValueError:
                pass
        aha_bak = TRAE_AHANET_CONFIG + ".bak-antigravity"
        if os.path.exists(aha_bak):
            with open(aha_bak, "rb") as f:
                content = f.read()
            with open(TRAE_AHANET_CONFIG, "wb") as f:
                f.write(content)
            changed = True
        if changed:
            ok, msg = _resign_app()
            if not ok:
                return False, msg
        # 移除中转证书信任（我们的自签证书 CN=127.0.0.1）
        if os.path.exists(CERT_PATH):
            subprocess.run(
                ["security", "delete-certificate", "-c", "127.0.0.1",
                 os.path.expanduser("~/Library/Keychains/login.keychain-db")],
                capture_output=True, timeout=30)
        logger.info("[Trae配置] 已还原官方端点")
        return True, "已还原官方端点，重启 TRAE SOLO CN 后生效"
    except (OSError, ValueError) as e:
        logger.error(f"[Trae配置] 还原失败: {e}")
        return False, f"还原失败: {e}"


def get_trae_config_state(port: int) -> dict:
    """GUI 展示用：当前接入状态"""
    pointed = False
    try:
        raw = open(TRAE_PRODUCT_JSON, "rb").read()
        pointed = f"https://127.0.0.1:{port}".encode() in raw
    except OSError:
        pass
    argv_ok = False
    try:
        argv = json.load(open(TRAE_ARGV_JSON, "r", encoding="utf-8"))
        argv_ok = bool(argv.get("ignore-certificate-errors"))
    except (OSError, ValueError):
        pass
    return {
        "installed": is_trae_installed(),
        "running": is_trae_running(),
        "pointed_to_us": pointed,
        "cert_ignore": argv_ok,
    }
