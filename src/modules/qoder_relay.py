# -*- coding: utf-8 -*-
"""Qoder / VSCode CodeBuddy 接入模块（2026-09-17新增）

目标客户端：
1. Qoder（Electron，D:\\Qoder CN / ~/AppData/Roaming/com.qodercn.app.stable）
   - BYOK openai-compatible 协议：自定义 provider 指向本地中转
   - 模型映射（qwen→hunyuan 等）在中转层完成（codebuddy_relay.QODER_MODEL_MAP）
2. VSCode CodeBuddy 扩展（tencent-cloud.coding-copilot）
   - user settings.json 写 codingcopilot.endpoint 指向本地中转

两个都是"开接入即接管、停接入即还原"，与 WorkBuddy/CodeBuddy CN 同一套生命周期。
"""
import json
import logging
import os
import shutil
import sqlite3
import sys

logger = logging.getLogger(__name__)


def _dpapi_encrypt(plaintext: str) -> bytes:
    """Windows DPAPI 加密（Electron safeStorage 在 Windows 的底层实现）。

    Qoder 的 byok_model_credentials.encrypted_payload 用 safeStorage.encryptString
    加密，Windows 底层是 DPAPI CryptProtectData（当前用户作用域）。
    Python 用 ctypes.windll.crypt32 复刻——同一用户进程能解密。
    """
    if sys.platform != "win32":
        return plaintext.encode("utf-8")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    data = plaintext.encode("utf-8")
    blob_in = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, len(data)),
        ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData failed")
    enc = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return enc


def _seal_credential(payload: dict) -> bytes:
    """模拟 Electron safeStorage.encryptString(JSON.stringify(payload))"""
    return _dpapi_encrypt(json.dumps(payload, ensure_ascii=False))

# ============ Qoder 路径 ============
_QODER_CANDIDATES = [
    os.path.expandvars(r"%APPDATA%\com.qodercn.app.stable"),   # Qoder CN（国内版）
    os.path.expandvars(r"%APPDATA%\com.qoder.app.stable"),     # Qoder 国际版
]
QODER_DATA_DIR = next((p for p in _QODER_CANDIDATES if os.path.isdir(p)), "")
QODER_DB = os.path.join(QODER_DATA_DIR, "main.sqlite") if QODER_DATA_DIR else ""

# ============ VSCode CodeBuddy 路径 ============
_VSCODE_SETTINGS_CANDIDATES = [
    os.path.expandvars(r"%APPDATA%\Code\User\settings.json"),          # VSCode 稳定版
    os.path.expandvars(r"%APPDATA%\VSCodium\User\settings.json"),      # VSCodium
]
VSCODE_SETTINGS_PATH = next(
    (p for p in _VSCODE_SETTINGS_CANDIDATES if os.path.isfile(p)), "")

# VSCode CodeBuddy 扩展的端点覆盖键（package.json: codingcopilot.endpoint）
VSCODE_CB_ENDPOINT_KEY = "codingcopilot.endpoint"


def is_qoder_installed() -> bool:
    """Qoder 是否安装（配置数据库存在）"""
    return bool(QODER_DB) and os.path.isfile(QODER_DB)


def is_vscode_codebuddy_installed() -> bool:
    """VSCode + CodeBuddy 扩展是否安装（settings.json 存在即认为 VSCode 在）"""
    return bool(VSCODE_SETTINGS_PATH)


# ================================================================
# Qoder BYOK 写入
# ================================================================
# BYOK profile 写库需要的字段（对 byok_model_profiles 的实测表结构）：
# account_id / profile_id / configuration_kind / provider_key / type_key /
# protocol_style / model_key / model_display_name / provider_display_name /
# endpoint_url / api_key_ref / is_vision / is_reasoning / max_input_tokens /
# available_context_windows_json / default_context_window / efforts_json /
# supports_disabled / updated_at
# account_id 是登录账户哈希——必须从库里读现有的（登录后 account_profiles 有值），
# 未登录时写不了 BYOK（Qoder 也没法用，天然前置）。

# 我们注入的 BYOK 模型清单（1:1 复刻 Qoder 官方列表——老账户官方模型被冻结
# 不可选时，BYOK 同名自定义模型不受 freezeTurnPolicy 限制（includeByok 通道），
# 全部走中转映射到上游真实模型，扣 Key 池积分）
QODER_BYOK_MODELS = [
    # (model_key, 显示名, 是否推理模型)
    ("qwen3.8-max", "Qwen3.8-Max（→hy4）", True),
    ("qwen3.7-max", "Qwen3.7-Max（→hy4）", True),
    ("qwen3.7-plus", "Qwen3.7-Plus（→hy3）", False),
    ("glm-5.2", "GLM-5.2", False),
    ("glm-5.3", "GLM-5.3", False),
    ("kimi-k3", "Kimi-K3", False),
    ("kimi-k2.7-code", "Kimi-K2.7-Code（→K2.7）", False),
    ("deepseek-v4-pro", "DeepSeek-V4-Pro", True),
    ("deepseek-v4-flash", "DeepSeek-V4-Flash（→4.1）", False),
    ("minimax-m3", "MiniMax-M3", False),
]

_QODER_PROVIDER_KEY = "antigravity-relay"
_QODER_PROFILE_PREFIX = "ag-relay-"


def _qoder_get_account_id() -> str:
    """从 account_profiles 表读当前登录账户 id（BYOK 行的 account_id 外键）"""
    conn = sqlite3.connect(QODER_DB)
    try:
        row = conn.execute(
            "SELECT account_id FROM account_profiles ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        # 表不存在（未登录）时兜底：BYOK 表里已有行的 account_id
        try:
            row = conn.execute(
                "SELECT account_id FROM byok_model_profiles LIMIT 1").fetchone()
            return str(row[0]) if row else ""
        except sqlite3.Error:
            return ""
    finally:
        conn.close()


def get_qoder_config_state(port: int) -> dict:
    """Qoder BYOK 当前状态（GUI 展示用）"""
    state = {
        "installed": is_qoder_installed(),
        "byok_count": 0,
        "pointed_to_us": False,
    }
    if not is_qoder_installed():
        return state
    try:
        conn = sqlite3.connect(QODER_DB)
        rows = conn.execute(
            "SELECT endpoint_url FROM byok_model_profiles WHERE provider_key=?",
            (_QODER_PROVIDER_KEY,)).fetchall()
        state["byok_count"] = len(rows)
        target = f"http://127.0.0.1:{port}"
        state["pointed_to_us"] = bool(rows) and all(
            r[0] == target for r in rows)
        conn.close()
    except sqlite3.Error as e:
        logger.warning(f"[Qoder] 读 BYOK 状态失败: {e}")
    return state


def apply_qoder_config(port: int, api_key: str = "antigravity-local") -> tuple:
    """写入 Qoder BYOK 自定义 provider（openai-compatible 指向本地中转）

    幂等：已有同 provider 同 model 的行先删再插（防重复）。
    api_key：中转对计费路径会强制换 Key 池 token，这里只做占位鉴权。

    未登录（account_id 空）时也写入（account_id=""）：Qoder 登录后
    重新点一次"启动接入"即重写为正确 account_id 生效。
    """
    if not is_qoder_installed():
        return False, "未检测到 Qoder（需先安装）"
    account_id = _qoder_get_account_id()
    endpoint = f"http://127.0.0.1:{port}/v1"
    # 先备份
    bak = QODER_DB + ".bak-antigravity"
    if not os.path.exists(bak):
        shutil.copy2(QODER_DB, bak)
    try:
        conn = sqlite3.connect(QODER_DB)
        conn.execute("BEGIN")
        # 幂等：删旧注入行（含 credentials）
        conn.execute(
            "DELETE FROM byok_model_profiles WHERE provider_key=?",
            (_QODER_PROVIDER_KEY,))
        conn.execute(
            "DELETE FROM byok_model_credentials WHERE profile_id LIKE ?",
            (_QODER_PROFILE_PREFIX.replace("_", "\\_") + "%",).replace("\\%", "%") if False else
            (_QODER_PROFILE_PREFIX + "%",))
        # DPAPI 加密 credential payload（Qoder safeStorage 底层 = DPAPI）
        try:
            enc_payload = _seal_credential({"apiKey": api_key})
            dpapi_ok = True
        except Exception as e:
            logger.warning(f"[Qoder] DPAPI加密失败(非Windows?)，credential用明文兜底: {e}")
            enc_payload = json.dumps({"apiKey": api_key}).encode("utf-8")
            dpapi_ok = False
        now_ms = int(__import__("time").time() * 1000)
        for idx, (model_key, display, is_reasoning) in enumerate(QODER_BYOK_MODELS):
            profile_id = f"{_QODER_PROFILE_PREFIX}{model_key}"
            conn.execute(
                """INSERT INTO byok_model_profiles
                (account_id, profile_id, configuration_kind, provider_key,
                 provider_display_name, type_key, type_display_name,
                 protocol_style, model_key, model_display_name,
                 endpoint_url, is_vision, is_reasoning, max_input_tokens,
                 available_context_windows_json, default_context_window, efforts_json,
                 supports_disabled, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, profile_id, "custom-endpoint", _QODER_PROVIDER_KEY,
                 "本地中转（Token接入器）", "custom", "自定义",
                 "openai-compatible", model_key, display,
                 endpoint, 0, 1 if is_reasoning else 0, 1000000,
                 json.dumps([200000, 400000, 1000000]), 1000000,
                 json.dumps(["minimal", "low", "medium", "high", "xhigh"]),
                 1, now_ms, now_ms))
            # 写 credential（payload_version=1 = safeStorage/DPAPI 加密）
            conn.execute(
                """INSERT OR REPLACE INTO byok_model_credentials
                (profile_id, account_id, payload_version, encrypted_payload,
                 generation, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)""",
                (profile_id, account_id, 1, enc_payload, 1, now_ms, now_ms))
        conn.commit()
        conn.close()
        logger.info(f"[Qoder] 已写入 {len(QODER_BYOK_MODELS)} 个 BYOK 模型+credentials → {endpoint}"
                    f"（account_id={account_id or '空(未登录)'}, DPAPI={'OK' if dpapi_ok else '明文兜底'}）")
        if not account_id:
            return True, (f"已写入 {len(QODER_BYOK_MODELS)} 个 BYOK 模型；"
                          "Qoder 尚未登录——登录后请重新点一次「启动接入」使其关联账户生效")
        return True, f"已写入 {len(QODER_BYOK_MODELS)} 个 BYOK 模型+凭据（openai-compatible → {endpoint}）"
    except sqlite3.Error as e:
        logger.error(f"[Qoder] BYOK 写入失败: {e}")
        return False, f"BYOK 写入失败: {e}"


def restore_qoder_config() -> tuple:
    """删除我们注入的 BYOK 行（还原 Qoder 为官方模型）"""
    if not is_qoder_installed():
        return True, "Qoder 未安装，无需还原"
    try:
        conn = sqlite3.connect(QODER_DB)
        conn.execute(
            "DELETE FROM byok_model_credentials WHERE profile_id LIKE ?",
            (_QODER_PROFILE_PREFIX + "%",))
        cur = conn.execute(
            "DELETE FROM byok_model_profiles WHERE provider_key=?",
            (_QODER_PROVIDER_KEY,))
        n = cur.rowcount
        conn.commit()
        conn.close()
        logger.info(f"[Qoder] 已还原（删除 {n} 个 BYOK 模型+credentials）")
        return True, f"已还原（移除 {n} 个 BYOK 模型）"
    except sqlite3.Error as e:
        logger.error(f"[Qoder] BYOK 还原失败: {e}")
        return False, f"BYOK 还原失败: {e}"


# ================================================================
# VSCode CodeBuddy 接入
# ================================================================
def _load_vscode_settings() -> dict:
    try:
        with open(VSCODE_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_vscode_settings(data: dict):
    """原子写 VSCode settings.json（先备份，带 JSONC 注释容忍）"""
    bak = VSCODE_SETTINGS_PATH + ".bak-antigravity"
    if not os.path.exists(bak):
        try:
            shutil.copy2(VSCODE_SETTINGS_PATH, bak)
        except OSError:
            pass
    tmp = VSCODE_SETTINGS_PATH + ".tmp-antigravity"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, VSCODE_SETTINGS_PATH)


def get_vscode_config_state(port: int) -> dict:
    """VSCode CodeBuddy 端点当前状态（GUI 展示用）"""
    settings = _load_vscode_settings()
    endpoint = settings.get(VSCODE_CB_ENDPOINT_KEY, "")
    return {
        "installed": is_vscode_codebuddy_installed(),
        "endpoint": endpoint,
        "pointed_to_us": endpoint == f"http://127.0.0.1:{port}",
    }


def apply_vscode_config(port: int) -> tuple:
    """把 VSCode CodeBuddy 扩展端点指向本地中转

    写 user settings 的 codingcopilot.endpoint 后，自动调用
    `code --command "workbench.action.reloadWindow"` 热加载——
    不需要手动重启 VSCode（CodeBuddy 扩展的 language client 会重新连接端点）。
    """
    if not is_vscode_codebuddy_installed():
        return False, "未检测到 VSCode settings.json"
    try:
        settings = _load_vscode_settings()
        settings[VSCODE_CB_ENDPOINT_KEY] = f"http://127.0.0.1:{port}"
        _save_vscode_settings(settings)
        logger.info(f"[VSCode CodeBuddy] 端点已指向 http://127.0.0.1:{port}")
        # 尝试热加载（VSCode 在跑就 reload，没跑就跳过）
        _try_vscode_reload()
        return True, "已写入并尝试热加载（VSCode 窗口会自动刷新）"
    except OSError as e:
        logger.error(f"[VSCode CodeBuddy] 写入失败: {e}")
        return False, f"写入失败: {e}"


def _try_vscode_reload():
    """通过 VSCode CLI 发 Reload Window 命令（热加载 endpoint 变更）"""
    import subprocess
    import shutil as _sh
    code_exe = _sh.which("code")
    if not code_exe:
        return  # VSCode 不在 PATH——跳过热加载（用户手动重启）
    try:
        subprocess.Popen(
            [code_exe, "--command", "workbench.action.reloadWindow"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if sys.platform == "win32" else 0)
        logger.info("[VSCode CodeBuddy] 已发送 Reload Window 命令（热加载）")
    except Exception as e:
        logger.warning(f"[VSCode CodeBuddy] 热加载命令失败（用户需手动重启VSCode）: {e}")


def restore_vscode_config() -> tuple:
    """还原 VSCode CodeBuddy 端点（删键回官方）"""
    if not is_vscode_codebuddy_installed():
        return True, "VSCode 未安装，无需还原"
    try:
        settings = _load_vscode_settings()
        if VSCODE_CB_ENDPOINT_KEY in settings:
            del settings[VSCODE_CB_ENDPOINT_KEY]
            _save_vscode_settings(settings)
        logger.info("[VSCode CodeBuddy] 端点已还原官方")
        return True, "已还原官方端点（重启 VSCode 生效）"
    except OSError as e:
        return False, f"还原失败: {e}"
