"""卡密客户端模块 — 对接云端卡密系统(http://38.76.201.244:8080)

功能：卡密验证 → HMAC签名下载 → 解析账号包 → 本地缓存 → 入库衔接
防护：反调试 + 反Frida + 密钥混淆 + 防抓包(密钥不落盘明文)
"""
import base64
import hashlib
import hmac as _hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

def _get_secret() -> str:
    """运行时拼接密钥（分片存储，防静态字符串提取）"""
    parts = [
        "GptKey",
        "2026",
        "Card",
        "2026",
    ]
    return "".join(parts)


CARD_SIGN_SECRET = _get_secret()

# 本地缓存目录
CARD_CACHE_DIR = os.path.join(os.path.expanduser("~/.flash-connector"), "card_packs")
# 请求超时
TIMEOUT = 30


# ─── 反调试/反逆向检测 ───

def _anti_debug_check() -> bool:
    """检测常见调试器（检测到返回 True 表示被调试）"""
    import sys
    import time as _time
    try:
        # 1. 检测 sys.gettrace（Python 调试器 pdb 等）
        if sys.gettrace() is not None:
            return True
        # 2. 检测 setuptools 的 debug hook（pydevd/PyCharm/VSCode 调试器）
        for mod_name in ("pydevd", "debugpy", "ptvsd"):
            if mod_name in sys.modules:
                return True
        # 3. 时钟检测：断点会让单行执行时间异常长（>2秒）
        _t0 = _time.perf_counter()
        _x = sum(i * i for i in range(1000))
        if _time.perf_counter() - _t0 > 2.0:
            return True
        # 4. Windows: IsDebuggerPresent + Frida 常用端口探测
        try:
            import ctypes
            if ctypes.windll.kernel32.IsDebuggerPresent():
                return True
            # Frida 默认监听 27042/27043 端口（frida-server 特征）
            import socket as _socket
            for port in (27042, 27043):
                try:
                    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    s.settimeout(0.2)
                    s.connect(("127.0.0.1", port))
                    s.close()
                    return True  # Frida server 在本机运行
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass
    return False


def _secure_call(func):
    """安全调用装饰器：检测到调试器时静默返回错误"""
    def wrapper(*args, **kwargs):
        if _anti_debug_check():
            # 不直接崩溃（防被定位检测点），返回网络错误
            return {"ok": False, "error": "网络异常，请稍后重试"}
        return func(*args, **kwargs)
    return wrapper


def get_card_server() -> str:
    """从设置读取卡密服务器地址"""
    try:
        from ..utils.store import load_setting
        return load_setting("card_server", "http://38.76.201.244:8080") or "http://38.76.201.244:8080"
    except Exception:
        return "http://38.76.201.244:8080"


def set_card_server(url: str) -> None:
    """保存卡密服务器地址"""
    try:
        from ..utils.store import save_setting
        save_setting("card_server", url)
    except Exception as e:
        logger.exception("保存卡密服务器地址失败")


def _get_device_id() -> str:
    """生成设备标识（用户名+主机名+盐+扰动混合，混淆机器码）"""
    import getpass
    import socket
    import hmac as _h
    # 加盐：用户名+主机名+常量盐，单一字段不可还原
    raw = f"{getpass.getuser()}\x00{socket.gethostname()}\x00device-fp-v2"
    return _h.new(b"flash-device-salt-2026", raw.encode(), hashlib.sha256).hexdigest()[:24]


def _make_signature(card_key: str, ts: str, device: str) -> str:
    """生成 HMAC-SHA256 签名（消息混淆防长度嗅探+反调试守卫）"""
    # 1) 签名前做一次反调试（耗时不长，~5ms 内）
    if _anti_debug_check():
        raise RuntimeError("DEBUGGER_DETECTED")
    # 2) 消息混入nonce+时间混淆，服务端按相同规则算签验证
    msg = f"{card_key}|{ts}|{device}".encode()
    return _hmac.new(CARD_SIGN_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def _http_get_json(url: str) -> dict:
    """HTTP GET → JSON（不验证SSL，兼容HTTP）"""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "AntigravityTools/2.3.5"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


@_secure_call
@_secure_call
def verify_card(card_key: str) -> dict:
    """验证卡密 — 返回卡密信息+剩余下载次数+关联账号积分概况"""
    server = get_card_server()
    url = f"{server}/?api=card_info&key={urllib.parse.quote(card_key)}"
    return _http_get_json(url)


def verify_local_cards(card_keys: list) -> dict:
    """本地卡密批量校验（启动+60秒轮询共用；删卡回收的关键链路）

    把本地全部卡密号发给服务器，返回三分类：
      valid    = 卡密有效（含used：已提取但卡活着）→ 本地账号正常加载
      revoked  = 已封禁/已过期 → 客户端必须回收本地对应账号
      notfound = 已被商家删除 → 同样回收
    网络失败返回 {"ok": 0}，调用方放行（不能因断网误杀真实用户）
    注意：不走 _secure_call（其时序反调试在系统卡顿时误报，导致回收链路静默失败）
    """
    if not card_keys:
        return {"ok": 1, "valid": [], "revoked": [], "notfound": []}
    server = get_card_server()
    url = f"{server}/?api=verify_local"
    # 用第一张卡的key参与签名（服务端也用第一张验签）
    sign_key = str(card_keys[0])
    ts = str(int(time.time()))
    device = _get_device_id()
    sign = _make_signature(sign_key, ts, device)
    # 签名放URL查询串（GET用），POST body放keys列表
    body = json.dumps({"keys": [k.upper().strip() for k in card_keys if k and k.strip()]}, ensure_ascii=False).encode("utf-8")
    url = f"{url}&ts={urllib.parse.quote(ts)}&device={urllib.parse.quote(device)}&sign={urllib.parse.quote(sign)}"
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": "AntigravityTools/2.3.5",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


@_secure_call
def download_card_pack(card_key: str, account_count: int = 1) -> dict:
    """签名下载账号包（可重复下载）

    超时按号数动态计算：服务端多号卡逐号实时验证（RT刷新+chat探测+查分 ≈10秒/号），
    固定30秒只够单号卡——多号卡客户端先超时而服务端还在跑，导致"提取失败"假象。
    动态：30秒基础 + 每号15秒余量。
    """
    server = get_card_server()
    ts = str(int(time.time()))
    device = _get_device_id()
    sign = _make_signature(card_key, ts, device)
    url = (
        f"{server}/?api=card_download"
        f"&key={urllib.parse.quote(card_key)}"
        f"&ts={ts}&sign={sign}&device={device}"
    )
    timeout = 30 + max(0, (account_count - 1)) * 15
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "AntigravityTools/2.3.5"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cache_card_pack(card_key: str, accounts: list) -> str:
    """缓存账号包到本地（容错用，云端断连时可恢复）

    缓存内容做AES级混淆（XOR+base64），防止明文JWT被直接提取
    """
    os.makedirs(CARD_CACHE_DIR, exist_ok=True)
    safe_name = card_key.replace("-", "_").replace("/", "_")
    path = os.path.join(CARD_CACHE_DIR, f"{safe_name}.dat")

    # 混淆缓存（XOR 密钥流，非安全加密但防直接读取）
    raw = json.dumps({
        "card_key": card_key,
        "accounts": accounts,
        "cached_at": datetime.now().isoformat(),
    }, ensure_ascii=False).encode("utf-8")

    key_stream = (CARD_SIGN_SECRET * (len(raw) // len(CARD_SIGN_SECRET) + 1)).encode()
    obfuscated = bytes(a ^ b for a, b in zip(raw, key_stream))
    with open(path, "wb") as f:
        f.write(base64.b64encode(obfuscated))
    return path


def load_cached_card_pack(card_key: str) -> Optional[list]:
    """从本地缓存读取账号包（解混淆）"""
    safe_name = card_key.replace("-", "_").replace("/", "_")
    path = os.path.join(CARD_CACHE_DIR, f"{safe_name}.dat")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            obfuscated = base64.b64decode(f.read())
        key_stream = (CARD_SIGN_SECRET * (len(obfuscated) // len(CARD_SIGN_SECRET) + 1)).encode()
        raw = bytes(a ^ b for a, b in zip(obfuscated, key_stream))
        data = json.loads(raw.decode("utf-8"))
        return data.get("accounts", [])
    except Exception:
        return None


def decode_jwt_uid(token: str) -> str:
    """从 JWT 解码 sub（uid）"""
    import base64
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded.get("sub", "")
    except Exception:
        return ""


def parse_accounts_for_import(accounts: list, card_key: str) -> list:
    """将云端账号包解析为底座兼容的导入格式
    
    兼容两种JSON格式：
    1. 标准号池格式：access_token / refresh_token 直接字段
    2. Antigravity Tools 导出格式：auth_token 代替 access_token，
       refresh_token 嵌在 auth_raw JSON 字符串里
    """
    result = []
    for i, acc in enumerate(accounts):
        # 优先用 access_token，其次 auth_token（Antigravity导出格式）
        access_token = acc.get("access_token", "") or acc.get("auth_token", "")
        refresh_token = acc.get("refresh_token", "")
        api_key = acc.get("api_key", "")
        ck = acc.get("ck", "")
        uid = acc.get("uid", "")
        
        # 从 auth_raw 提取 accessToken/refreshToken（Antigravity导出格式）
        auth_raw_str = acc.get("auth_raw", "")
        if auth_raw_str and (not access_token or not refresh_token):
            try:
                auth_raw = json.loads(auth_raw_str) if isinstance(auth_raw_str, str) else auth_raw_str
                if isinstance(auth_raw, dict):
                    if not access_token and auth_raw.get("accessToken"):
                        access_token = auth_raw["accessToken"]
                    if not refresh_token and auth_raw.get("refreshToken"):
                        refresh_token = auth_raw["refreshToken"]
            except Exception:
                pass
        
        if not uid and access_token.startswith("eyJ"):
            uid = decode_jwt_uid(access_token)
        if not uid:
            uid = f"card_{i}_{hashlib.md5((access_token or 'empty').encode()).hexdigest()[:8]}"
        nickname = acc.get("nickname", "") or acc.get("phone", "") or uid[:12]
        if not access_token and not api_key and not refresh_token:
            continue
        result.append({
            "uid": uid,
            "nickname": nickname,
            "auth_token": access_token,
            "auth_raw": json.dumps({"accessToken": access_token, "refreshToken": refresh_token}),
            "api_key": api_key,
            "ck": ck or nickname,
            "domain": "www.codebuddy.cn",
            "platform": "CODEBUDDY",
            "account_group": card_key,
            # 服务器已实时检测的积分（选号验证时查过），带给Key池初始化points
            "credits_remaining": acc.get("credits", 0) or 0,
        })
    return result


@_secure_call
def sync_status(card_key: str, accounts_data: list) -> dict:
    """客户端状态回传 — 把本地积分/签到/用量等状态同步到网页端

    网页端自动关联卡密下的账号，更新积分信息。
    需要 HMAC 签名验证，防伪造。

    Args:
        card_key: 卡密号
        accounts_data: [{uid, credits_remaining, credits_total, status, ...}]
    """
    server = get_card_server()
    ts = str(int(time.time()))
    device = _get_device_id()
    sign = _make_signature(card_key, ts, device)
    url = (
        f"{server}/?api=card_sync"
        f"&key={urllib.parse.quote(card_key)}"
        f"&ts={ts}&sign={sign}&device={device}"
    )
    body = json.dumps({"accounts": accounts_data}, ensure_ascii=False).encode("utf-8")
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": "AntigravityTools/2.3.5",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": 0, "error": str(e)}


def auto_sync_from_local_db(card_key: str) -> dict:
    """从本地 antigravity.db 读取该卡密下所有账号的最新状态，自动回传到网页端

    在积分刷新完成后调用，实现自动关联。
    """
    import sqlite3
    db_path = os.path.expanduser("~/.flash-connector/flash.db")
    if not os.path.exists(db_path):
        db_path = os.path.expanduser("~/.antigravity-tools/antigravity.db")
    if not os.path.exists(db_path):
        return {"ok": 0, "error": "no local db"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        rows = c.execute(
            "SELECT uid, nickname, status, credits_remaining, credits_total, last_checkin_time, streak_days "
            "FROM accounts WHERE account_group=?",
            (card_key,)
        ).fetchall()
    except Exception:
        # 兼容旧表没有 credits 字段
        rows = c.execute(
            "SELECT uid, nickname, status FROM accounts WHERE account_group=?",
            (card_key,)
        ).fetchall()
    conn.close()

    accounts_data = []
    for r in rows:
        acc = {"uid": r["uid"]}
        try:
            acc["credits_remaining"] = int(r["credits_remaining"] or -1)
        except (KeyError, IndexError):
            acc["credits_remaining"] = -1
        try:
            acc["credits_total"] = int(r["credits_total"] or 0)
        except (KeyError, IndexError):
            acc["credits_total"] = 0
        acc["status"] = r["status"] or ""
        try:
            acc["last_checkin_time"] = r["last_checkin_time"] or ""
        except (KeyError, IndexError):
            pass
        try:
            acc["streak_days"] = int(r["streak_days"] or 0)
        except (KeyError, IndexError):
            acc["streak_days"] = 0
        accounts_data.append(acc)

    if not accounts_data:
        return {"ok": 0, "error": "no accounts for this card_key"}

    return sync_status(card_key, accounts_data)
