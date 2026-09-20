# -*- coding: utf-8 -*-
"""Qoder 官方模型无感接入（2026-09-19，零文件修改方案）

原理（逆向实证）：Qoder daemon 支持 QODER_MODEL_TRANSPORT=http 模式——
chat 请求用标准 OpenAI 协议发往 https://{QODER_MODEL_SERVER_HOST}/model/v1/chat/completions。
两个环境变量经 buildEnv() 100% 透传进 daemon（不在删除列表）。

方案：
1. 中转加 TLS 监听（自签证书 + 信任导入——一次性，管理员权限）
2. setx 用户级环境变量指向本地中转
3. 用户重启 Qoder → 官方模型选中即走中转（OpenAI协议→换token→腾讯上游）

零文件修改：不改 app.asar、不 patch、Qoder 升级无影响（env 是系统级的）。
"""
import json
import logging
import os
import shutil
import subprocess
import ssl
import sys

logger = logging.getLogger(__name__)

# 环境变量（Qoder daemon 透传链已实证）
QODER_TRANSPORT_ENV = "QODER_MODEL_TRANSPORT"
QODER_HOST_ENV = "QODER_MODEL_SERVER_HOST"

# 证书目录（%USERPROFILE%\.token-relay\tls\）
CERT_DIR = os.path.join(os.path.expanduser("~"), ".token-relay", "tls")
CERT_FILE = os.path.join(CERT_DIR, "server.pem")
KEY_FILE = os.path.join(CERT_DIR, "server.key")


def _gen_self_signed_cert() -> tuple:
    """生成自签证书（CN=127.0.0.1 + SAN），返回 (cert_path, key_path)"""
    os.makedirs(CERT_DIR, exist_ok=True)
    if os.path.isfile(CERT_FILE) and os.path.isfile(KEY_FILE):
        return CERT_FILE, KEY_FILE
    # 用 cryptography 生成（程序内置依赖，无openssl依赖）
    import ipaddress
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"127.0.0.1")])
    san = x509.SubjectAlternativeName([
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.DNSName(u"localhost"),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(KEY_FILE, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    logger.info("[Qoder无感] 自签证书已生成（127.0.0.1, 10年有效期）")
    return CERT_FILE, KEY_FILE


def trust_cert_in_windows() -> tuple:
    """把自签证书导入 Windows 信任存储（CurrentUser\\CA——无确认弹窗）"""
    _gen_self_signed_cert()
    ps_cmd = (
        "Import-Certificate -FilePath "
        f"'{CERT_FILE}' -CertStoreLocation Cert:\\CurrentUser\\CA"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            logger.info("[Qoder无感] 证书已导入当前用户CA信任存储（无弹窗）")
            return True, "证书已信任（当前用户CA）"
        # CA失败兜底LocalMachine Root（需管理员）
        r2 = subprocess.run(
            ["certutil", "-addstore", "Root", CERT_FILE],
            capture_output=True, text=True, timeout=20)
        if r2.returncode == 0:
            return True, "证书已信任（本机Root，管理员）"
        return False, f"证书导入失败: {r.stderr or r2.stderr}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"导入异常: {e}"


def apply_qoder_seamless(port: int) -> tuple:
    """开启 Qoder 官方模型无感接入（零文件修改）

    1. 确保 TLS 证书存在且被信任
    2. setx 两个环境变量（用户级，永久）
    3. 提示重启 Qoder 生效
    """
    # 证书
    ok, msg = trust_cert_in_windows()
    if not ok:
        logger.warning(f"[Qoder无感] 证书信任失败: {msg}")
        return False, f"证书信任失败: {msg}（尝试右键以管理员运行本程序）"
    # 环境变量（用户级永久；NODE_EXTRA_CA_CERTS让daemon的Node信任自签证书——
    # Electron AS_NODE 模式不读 Windows store，必须用 Node 原生 CA 扩展变量，
    # 该变量经 buildEnv() 透传实证不在删除列表）
    try:
        subprocess.run(["setx", QODER_TRANSPORT_ENV, "http"],
                       capture_output=True, timeout=15, check=True)
        subprocess.run(["setx", QODER_HOST_ENV, f"127.0.0.1:{port + 1}"],
                       capture_output=True, timeout=15, check=True)
        subprocess.run(["setx", "NODE_EXTRA_CA_CERTS", CERT_FILE],
                       capture_output=True, timeout=15, check=True)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        return False, f"环境变量写入失败: {e}"
    logger.info(f"[Qoder无感] 环境变量已设置: {QODER_TRANSPORT_ENV}=http, "
                f"{QODER_HOST_ENV}=127.0.0.1:{port}, NODE_EXTRA_CA_CERTS={CERT_FILE}")
    return True, (f"无感接入已配置——重启 Qoder 后，官方模型选中即走中转"
                  f"（TLS 已信任 + daemon 协议已切换 http 模式）")


def restore_qoder_seamless() -> tuple:
    """关闭无感接入（删除三个环境变量）"""
    try:
        for var in (QODER_TRANSPORT_ENV, QODER_HOST_ENV, "NODE_EXTRA_CA_CERTS"):
            subprocess.run(
                ["reg", "delete", "HKCU\\Environment", "/v", var, "/f"],
                capture_output=True, timeout=15)
        logger.info("[Qoder无感] 环境变量已删除（Qoder 重启后回官方直连）")
        return True, "已还原（重启 Qoder 后恢复官方直连）"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"还原失败: {e}"


def get_qoder_seamless_state(port: int) -> dict:
    """无感接入当前状态"""
    try:
        r = subprocess.run(
            ["reg", "query", "HKCU\\Environment", "/v", QODER_HOST_ENV],
            capture_output=True, text=True, timeout=10)
        enabled = QODER_HOST_ENV in r.stdout and f"127.0.0.1:{port + 1}" in r.stdout
        return {"enabled": enabled,
                "cert_trusted": os.path.isfile(CERT_FILE)}
    except Exception:
        return {"enabled": False, "cert_trusted": os.path.isfile(CERT_FILE)}
