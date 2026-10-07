# -*- coding: utf-8 -*-
"""账号 token 保活调度器（2026-10-07，照抄原项目 antigravity-tools-2.4.10 的
token_keepalive.py 全部规则——该文件又照抄 TraeWorkAssistant 的 WorkBuddy 保活机制）

规则全对齐：
- 惰性门：access_token 剩余寿命 > LAZY_HOURS(24h) 时**不发请求**（wb-renew 同款 24h）
- 刷新端点：POST https://www.codebuddy.cn/v2/plugin/auth/token/refresh 只带 X-Refresh-Token
- 失败分级（RefreshFail 枚举同款）：
    NoRefreshToken 无 RT  → 跳过，不算失败
    Network        网络/5xx → 只累计次数，不判死，下轮重试
    Auth           4xx     → 判 refresh_token 失效，标记需重新登录
    BadResponse    200 无 accessToken → 不判死
- 调度：每天 HH:MM 跑一次（默认 11:00），已过时刻且当天未跑则补跑
- 失败冷却 30 分钟（RETRY_COOLDOWN 同款），避免连打
- 连续 3 次 4xx 才置失效（STALE_FAILS_BEFORE_DEAD）
- 成功即回写 DB + 内存 + 上游 Key 池（旧 token 同步换新，Key 池无感知切换）
"""

import json
import logging
import threading
import time
from datetime import datetime

import requests

from ..utils.store import (
    load_accounts,
    load_setting,
    record_token_refresh,
    save_setting,
    update_account_tokens,
)

logger = logging.getLogger(__name__)

# ── 与 TraeWorkAssistant 对齐的常量 ──
REFRESH_URL = "https://www.codebuddy.cn/v2/plugin/auth/token/refresh"
LAZY_HOURS = 24                    # 到期前 24h 内才真正刷新
RUN_HHMM = "11:00"                 # 每天执行时刻
RETRY_COOLDOWN_SECS = 30 * 60      # 对方 scheduler RETRY_COOLDOWN_MS = 30 分钟
TICK_SECS = 60                     # 对方 scheduler：单后台线程 60s tick
STALE_FAILS_BEFORE_DEAD = 3        # 连续 3 次才置失效


class _RefreshResult:
    """刷新结果（照抄对方 RefreshFail 语义）。"""

    OK = "ok"                       # 刷新成功
    NO_REFRESH_TOKEN = "no_rt"      # 无 refresh_token，不可刷新
    NETWORK = "network"             # 网络不可达 / 5xx（不判死）
    AUTH = "auth"                   # 4xx：refresh token 已失效（判死）
    BAD_RESPONSE = "bad_response"   # 200 但无 accessToken（不判死）
    SKIP_RELOGIN = "skip_relogin"   # 已标记需重新登录：入口拦截，不发请求


def refresh_token_once(refresh_token: str, timeout: int = 30) -> tuple:
    """调 plugin refresh 端点刷新（照抄对方 refresh_token_once_ex）。

    Returns:
        (result, new_access, new_refresh)
    """
    if not refresh_token:
        return _RefreshResult.NO_REFRESH_TOKEN, "", ""
    try:
        resp = requests.post(
            REFRESH_URL,
            json={},
            headers={"X-Refresh-Token": refresh_token},
            timeout=timeout,
        )
    except (requests.Timeout, requests.ConnectionError, OSError) as e:
        logger.warning(f"[保活] 刷新网络失败: {type(e).__name__}: {e}")
        return _RefreshResult.NETWORK, "", ""

    if 400 <= resp.status_code < 500:
        # 4xx：refresh token 已失效（对方 RefreshFail::Auth）
        logger.warning(f"[保活] 刷新被拒 HTTP {resp.status_code}: {resp.text[:160]}")
        return _RefreshResult.AUTH, "", ""
    if resp.status_code != 200:
        # 5xx 等服务端故障：不判死（对方 RefreshFail::Network）
        logger.warning(f"[保活] 刷新失败 HTTP {resp.status_code}: {resp.text[:160]}")
        return _RefreshResult.NETWORK, "", ""

    try:
        data = resp.json().get("data") or {}
    except ValueError:
        return _RefreshResult.BAD_RESPONSE, "", ""
    access = data.get("accessToken", "")
    if not access:
        logger.warning(f"[保活] 刷新响应无 accessToken: {resp.text[:160]}")
        return _RefreshResult.BAD_RESPONSE, "", ""
    return _RefreshResult.OK, access, data.get("refreshToken", "") or ""


def _account_tokens(account) -> tuple:
    """从 Account 取 (access, refresh)；auth_raw 优先，兜底 auth_token。"""
    access = refresh = ""
    if account.auth_raw:
        try:
            raw = json.loads(account.auth_raw)
            access = raw.get("accessToken") or raw.get("access_token") or ""
            refresh = raw.get("refreshToken") or raw.get("refresh_token") or ""
        except ValueError:
            pass
    if not access:
        access = account.auth_token or ""
    return access, refresh


def refresh_account_once(account, lazy_hours: int = LAZY_HOURS, force: bool = False) -> tuple:
    """单账号续期（照抄对方 ensure_fresh）。

    含惰性门：剩余 > lazy_hours 直接跳过（force=True 时无视惰性门强制刷新）。
    成功即落盘（DB + Key 池同步换新）。

    Returns:
        (result, note) —— note 供日志/展示（fresh / refreshed / refresh_failed …）
    """
    access, refresh = _account_tokens(account)
    if not refresh:
        return _RefreshResult.NO_REFRESH_TOKEN, "no_refresh_token"

    # 已判定失效：入口拦截，不发网络请求（照抄对方 refresh_jwt_impl 的拦截）
    if getattr(account, "token_needs_relogin", False):
        return _RefreshResult.SKIP_RELOGIN, "skip_needs_relogin"

    # 惰性门（对方 ensure_fresh：remain_h > lazy_hours → "fresh" 跳过）
    exp_ms = None
    try:
        import base64
        parts = access.split(".")
        if len(parts) == 3:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            payload_data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            exp = payload_data.get("exp")
            if exp:
                exp_ms = int(exp) * 1000
    except Exception:
        exp_ms = None
    if exp_ms is not None and not force:
        remain_h = (exp_ms - int(time.time() * 1000)) / 3_600_000
        if remain_h > lazy_hours:
            return _RefreshResult.OK, "fresh"

    result, new_access, new_refresh = refresh_token_once(refresh)
    if result != _RefreshResult.OK:
        rejected = result == _RefreshResult.AUTH
        record_token_refresh(account.uid, False, rejected=rejected,
                             reason=f"HTTP 4xx 拒绝（{result}）" if rejected else "")
        return result, "refresh_failed"

    # 成功：落盘（照抄对方 save_token_store + 复用 update_account_tokens）
    # 先取旧 token 用于同步上游 Key 池
    old_token = access
    update_account_tokens(account.uid, new_access, new_refresh or "")
    account.auth_token = new_access
    try:
        raw = json.loads(account.auth_raw) if account.auth_raw else {}
    except ValueError:
        raw = {}
    raw["accessToken"] = new_access
    if new_refresh:
        raw["refreshToken"] = new_refresh
    account.auth_raw = json.dumps(raw)
    record_token_refresh(account.uid, True)

    # 上游 Key 池里持旧 token 的 Key 同步换新（无感切换，中转不断流）
    if old_token and old_token != new_access:
        try:
            from .proxy_server import ProxyDatabase
            pdb = ProxyDatabase.get_instance()
            synced = 0
            for k in pdb.get_upstream_keys():
                if k.get("api_key") == old_token:
                    pdb.update_upstream_key(k.get("key_id", ""), {"api_key": new_access})
                    synced += 1
            if synced:
                pdb._flush_to_disk()
                logger.info(f"[保活] 上游 Key 池已同步新 token（{synced} 个）")
        except Exception as e:
            logger.error(f"[保活] 上游 Key 池同步失败: {e}")

    return _RefreshResult.OK, "refreshed"


class TokenKeepAliveScheduler:
    """账号 token 保活调度器（照抄 TraeWorkAssistant scheduler 的单线程 tick 模型）。"""

    _STATE_KEY = "token_keepalive_state"

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    # ── 调度状态（last_run_date / last_fail_ts，对齐对方 scheduler_state.json）──

    def _load_state(self) -> dict:
        try:
            return json.loads(load_setting(self._STATE_KEY, "{}") or "{}")
        except ValueError:
            return {}

    def _save_state(self, state: dict):
        save_setting(self._STATE_KEY, json.dumps(state))

    def _enabled(self) -> bool:
        """开关（默认开，对齐对方 wb-renew 恒开）。"""
        return load_setting("token_keepalive_enabled", "True") == "True"

    def _run_hhmm(self) -> str:
        v = (load_setting("token_keepalive_hhmm", RUN_HHMM) or RUN_HHMM).strip()
        # 严格 HH:MM 校验，非法回退默认（对齐对方 effective_hhmm）
        try:
            h, m = v.split(":")
            if len(h) == 2 and len(m) == 2 and 0 <= int(h) < 24 and 0 <= int(m) < 60:
                return v
        except (ValueError, AttributeError):
            pass
        return RUN_HHMM

    def _lazy_hours(self) -> int:
        try:
            return int(load_setting("token_keepalive_lazy_hours", str(LAZY_HOURS)))
        except (ValueError, TypeError):
            return LAZY_HOURS

    # ── 线程生命周期 ──

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="token-keepalive")
        self._thread.start()
        logger.info("[保活] token 保活调度线程已启动（每天 %s，惰性门 %sh）",
                    self._run_hhmm(), self._lazy_hours())

    def stop(self):
        self._stop.set()

    def _loop(self):
        # 对方 scheduler：启动 90s 后首跑，避开启动高峰
        if self._stop.wait(90):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.error(f"[保活] tick 异常（已捕获，不影响下一轮）: {e}")
            if self._stop.wait(TICK_SECS):
                return

    def tick(self):
        """单轮：到点 + 当天未跑 + 不在失败冷却 → 执行一轮保活。"""
        if not self._enabled():
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        now_hm = now.strftime("%H:%M")
        hhmm = self._run_hhmm()
        if now_hm < hhmm:
            return  # 未到时刻
        state = self._load_state()
        if state.get("last_run_date") == today:
            return  # 当天已跑（对齐对方 last_run_date != today）
        last_fail_ts = state.get("last_fail_ts", 0)
        if last_fail_ts and (time.time() - last_fail_ts) < RETRY_COOLDOWN_SECS:
            return  # 失败 30 分钟冷却（对齐对方 RETRY_COOLDOWN_MS）
        self.run_once(today)

    def run_once(self, today: str = ""):
        """立即跑一轮全账号续期（含惰性门 / 失败分级 / 落盘）。"""
        today = today or datetime.now().strftime("%Y-%m-%d")
        accounts = load_accounts()
        lazy_hours = self._lazy_hours()
        refreshed = skipped_fresh = failed_network = failed_auth = no_rt = skipped_dead = 0
        for account in accounts:
            if not account.uid:
                continue
            try:
                result, note = refresh_account_once(account, lazy_hours=lazy_hours)
            except Exception as e:
                logger.error(f"[保活] 账号 {account.uid} 续期异常: {e}")
                failed_network += 1
                continue
            if result == _RefreshResult.OK:
                if note == "refreshed":
                    refreshed += 1
                    logger.info(f"[保活] {account.display_name} 续期成功")
                else:
                    skipped_fresh += 1
            elif result == _RefreshResult.NO_REFRESH_TOKEN:
                no_rt += 1
            elif result == _RefreshResult.SKIP_RELOGIN:
                skipped_dead += 1   # 已判失效：入口拦截，无意义请求不发
            elif result == _RefreshResult.AUTH:
                failed_auth += 1
                logger.warning(f"[保活] {account.display_name} refresh_token 失效，标记需重新登录")
            else:
                failed_network += 1

        state = self._load_state()
        state["last_run_date"] = today
        state["last_run_summary"] = (
            f"续期 {refreshed}，未到期跳过 {skipped_fresh}，无RT {no_rt}，"
            f"已失效跳过 {skipped_dead}，失效 {failed_auth}，网络失败 {failed_network}"
        )
        # 只有"有账号尝试刷新但全部失败"才记失败冷却（对齐对方：局部失败不整体失败）
        if refreshed == 0 and (failed_auth + failed_network) > 0:
            state["last_fail_ts"] = time.time()
        else:
            state.pop("last_fail_ts", None)
        self._save_state(state)
        logger.info(f"[保活] 本轮完成: {state['last_run_summary']}")


# 全局单例（应用启动时 start，退出时 stop）
keepalive_scheduler = TokenKeepAliveScheduler()
