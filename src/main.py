"""Token接入器 - 多平台 IDE 工具管理器

入口文件 - 使用 python -m src.main 运行
"""

import atexit
import os
import shutil
import signal
import subprocess
import sys
import time
import logging

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from .main_window import MainWindow, VERSION
from .utils.store import save_setting, load_setting


# 桌面"前台"文件夹路径（每次启动自动部署最新版exe到此处）
DESKTOP_FRONT_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "前台")
EXE_NAME = "Token接入器.exe"


def _auto_deploy_to_desktop():
    """启动时自动把当前exe复制到桌面"前台"文件夹（替换旧版本）。
    
    只在打包模式（frozen）下执行，开发模式跳过。
    PyInstaller onefile模式下 sys.executable 指向bootloader（临时目录），
    需用 sys.argv[0] 获取真实exe路径。
    """
    if not getattr(sys, 'frozen', False):
        return
    try:
        # onefile模式：sys.argv[0] 是用户双击的exe路径
        current_exe = os.path.abspath(sys.argv[0]) if sys.argv and os.path.isfile(sys.argv[0]) else sys.executable
        if not os.path.isfile(current_exe) or not current_exe.endswith('.exe'):
            return
        os.makedirs(DESKTOP_FRONT_DIR, exist_ok=True)
        target = os.path.join(DESKTOP_FRONT_DIR, EXE_NAME)
        # 如果目标已存在且大小相同则跳过
        if os.path.isfile(target):
            if os.path.getsize(target) == os.path.getsize(current_exe):
                return
        # 尝试复制
        try:
            shutil.copy2(current_exe, target)
        except PermissionError:
            # 目标文件被占用（旧版正在运行）——杀掉再复制
            try:
                subprocess.run(['taskkill', '/F', '/IM', EXE_NAME], capture_output=True, timeout=5)
                time.sleep(1)
                shutil.copy2(current_exe, target)
            except Exception:
                return
        logging.getLogger(__name__).info(f"已自动部署到桌面前台: {target}")
    except Exception as e:
        try:
            logging.getLogger(__name__).warning(f"自动部署到桌面失败: {e}")
        except Exception:
            pass


def _is_gui_mode():
    """检测是否以 GUI 模式运行（无控制台）或 PyInstaller 打包模式"""
    if getattr(sys, 'frozen', False):
        return True
    # macOS: pythonw 不带 .exe 后缀
    exe_name = os.path.basename(sys.executable).lower()
    return exe_name == "pythonw" or exe_name == "pythonw.exe"


def _setup_logging():
    """配置日志 - pythonw 模式写文件，否则输出到控制台"""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_fmt = "%H:%M:%S"

    if _is_gui_mode():
        log_dir = os.path.join(os.path.expanduser("~"), ".flash-connector", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "app.log")
        # RotatingFileHandler: 每个 2MB，保留 3 个
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(log_file, maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(log_format, date_fmt))
        logging.basicConfig(handlers=[handler], level=logging.INFO)
    else:
        logging.basicConfig(level=logging.INFO, format=log_format, datefmt=date_fmt)


logger = logging.getLogger(__name__)

# 全局引用主窗口，用于 atexit 和信号清理
_main_window = None


def _force_cleanup():
    """强制清理所有资源（atexit 和信号处理时调用）

    ★2026-09-15修复3002错误：软件退出时如果中转在跑，必须停止中转+还原
    WorkBuddy/CodeBuddy 端点配置。否则：
    - 8003端口死了但 WorkBuddy CLI 内存里还缓存指向8003 → ECONNREFUSED
    - 下次开软件 autostart 又拉起中转 → CLI 打过来 → 扣卡密积分（用户以为没接入）
    """
    global _main_window
    if _main_window:
        # 停止代理服务器
        try:
            api_proxy_page = _main_window._pages.get("api_proxy")
            if api_proxy_page:
                api_proxy_page._cleanup()
        except Exception:
            pass
        # 停止无感换号中转 + 还原 WorkBuddy/CodeBuddy 端点
        try:
            hs = _main_window._pages.get("hotswitch")
            if hs and getattr(hs, "_relay_server", None) and hs._relay_server.is_running:
                hs._relay_server.stop()
                hs._relay_server = None
                save_setting("codebuddy_relay_enabled", "0")
                save_setting("codebuddy_relay_wb_enabled", "0")
                logger.info("[退出清理] 中转已停止，还原客户端端点配置")
                # 还原配置（不重启WorkBuddy——软件都在退了，交给下次启动检测）
                try:
                    from .modules.codebuddy_relay import restore_client_config, restore_workbuddy_config, is_codebuddy_installed, is_workbuddy_installed
                    if is_codebuddy_installed():
                        restore_client_config()
                    if is_workbuddy_installed():
                        restore_workbuddy_config(restart_wb=False)
                except Exception:
                    pass
        except Exception:
            pass


def _signal_handler(signum, frame):
    """信号处理：Ctrl+C 或系统关闭信号"""
    logger.info(f"收到信号 {signum}，正在退出...")
    _force_cleanup()
    os._exit(0)


def _check_single_instance() -> bool:
    """检查是否已有实例在运行，如有则唤醒并返回 False
    
    QLocalServer方案（清理残留socket + listen成功=第一个实例）。
    removeServer防止旧实例崩溃后socket残留导致永久卡死。
    """
    if os.environ.get("FLASH_DEV") == "1":
        logger.info("FLASH_DEV=1，跳过单实例检查")
        return True

    from PySide6.QtNetwork import QLocalSocket, QLocalServer

    # 1. 先尝试连接已有实例
    socket = QLocalSocket()
    socket.connectToServer("flash-connector-single-instance")
    socket.waitForConnected(500)

    if socket.state() == QLocalSocket.ConnectedState:
        # 已有实例——发唤醒信号
        socket.write(b"SHOW")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
        return False

    # 2. 没有已有实例——清理残留socket + 创建新server
    QLocalServer.removeServer("flash-connector-single-instance")
    global _single_instance_server
    _single_instance_server = QLocalServer()
    if not _single_instance_server.listen("flash-connector-single-instance"):
        logger.warning(f"QLocalServer listen失败: {_single_instance_server.errorString()}")
        return True  # listen失败不阻止启动（防误杀）

    return True


# 单实例服务器引用
_single_instance_server = None


def _check_remote_disabled() -> dict:
    """启动时查询服务器端客户端状态（禁用开关 + 强制更新最低版本）。

    返回 {'disabled': bool, 'min_version': str}。
    服务器不可达/超时=放行（不能因网络问题挡住所有用户）。
    """
    result = {"disabled": False, "min_version": ""}
    try:
        import json as _json
        import urllib.request as _rq
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        req = _rq.Request(
            "http://38.76.201.244:8080/?api=client_status",
            headers={"User-Agent": "AntigravityTools/2.3.5"},
        )
        with _rq.urlopen(req, timeout=3, context=ctx) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("ok"):
            result["disabled"] = bool(data.get("disabled"))
            result["min_version"] = str(data.get("min_version") or "").strip()
    except Exception:
        pass
    return result


def _version_tuple(v: str):
    parts = []
    for seg in v.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _need_force_update(current: str, min_ver: str) -> bool:
    """当前版本低于服务器要求的最低版本 → 需要强制更新"""
    if not min_ver:
        return False
    try:
        return _version_tuple(current) < _version_tuple(min_ver)
    except Exception:
        return False


UPDATE_URL = "https://2bbb.lanzout.com/b04oxedod"


def _show_force_update_dialog(min_ver: str):
    """强制更新弹窗（模态，无法关闭——必须更新才能继续）"""
    from PySide6.QtWidgets import QMessageBox, QPushButton
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning)
    msg.setWindowTitle("发现新版本")
    msg.setText(
        f"当前版本已停用，请更新到最新版本后使用。\n\n"
        f"当前版本: v{VERSION}\n"
        f"要求版本: v{min_ver} 及以上\n\n"
        f"下载地址（密码 9ed0）：\n{UPDATE_URL}"
    )
    btn_open = QPushButton("打开下载页")
    btn_copy = QPushButton("复制下载地址")
    msg.addButton(btn_open, QMessageBox.AcceptRole)
    msg.addButton(btn_copy, QMessageBox.ActionRole)
    msg.addButton(QMessageBox.Close)
    while True:
        clicked = msg.exec()
        if msg.clickedButton() is btn_open:
            import webbrowser
            webbrowser.open(UPDATE_URL)
            continue
        if msg.clickedButton() is btn_copy:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(UPDATE_URL)
            continue
        break  # Close按钮或关闭窗口 → 退出程序


def _cleanup_stale_relay_config():
    """启动时清理残留的中转配置（2026-09-15修复3002错误）。

    场景：上次退出时异常（断电/崩溃/taskkill）没走到 _force_cleanup，
    WorkBuddy/CodeBuddy 的 settings.json 还指向 127.0.0.1:8003。
    本次启动如果 autostart 开着会自动拉起中转还好；
    如果用户关了 autostart 或勾了"不自动启动"，WorkBuddy 就会一直报
    "connect ECONNREFUSED 127.0.0.1:8003"。
    这里在启动最早期检测：settings 指向8003 且 relay_enabled=0 → 清理。
    """
    try:
        if load_setting("codebuddy_relay_enabled", "0") == "1":
            return  # 用户开着自动启动，中转马上要拉起，不动
        port = load_setting("codebuddy_relay_port", "8003")
        stale_url = f"http://127.0.0.1:{port}"
        from .modules.codebuddy_relay import (
            is_workbuddy_installed, restore_workbuddy_config,
            is_codebuddy_installed, restore_client_config,
            get_workbuddy_config_state,
        )
        if is_workbuddy_installed():
            state = get_workbuddy_config_state(int(port))
            # 检查聊天端点或媒体端点任一指向本地中转
            stale = state["pointed_to_us"] or state.get("media_url", "").startswith("http://127.0.0.1:")
            if stale:
                restore_workbuddy_config(restart_wb=True)
                logging.getLogger(__name__).info(
                    "[启动清理] 检测到 WorkBuddy 残留端点配置（聊天或媒体），已清理并重启 WorkBuddy")
        if is_codebuddy_installed():
            # CodeBuddy 残余配置同样清理
            restore_client_config()
    except Exception:
        pass  # 清理失败不阻塞启动


def main():
    """应用入口"""
    _setup_logging()

    # 自动部署到桌面"前台"文件夹（替换旧版本）
    _auto_deploy_to_desktop()

    # 清理上次异常退出的残留中转配置（防 WorkBuddy 报 ECONNREFUSED 3002）
    _cleanup_stale_relay_config()

    # 注册 atexit 清理（即使异常退出也尝试清理）
    atexit.register(_force_cleanup)

    # 注册信号处理（Ctrl+C / 系统关闭）
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (OSError, ValueError):
        pass  # 某些环境不允许注册信号

    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Token接入器")
    app.setOrganizationName("Antigravity")

    # 远程状态检查（禁用开关 + 强制更新版本，最初始阶段拦截）
    remote_status = _check_remote_disabled()
    if remote_status["disabled"]:
        logger.warning("远程禁用开关已开启，阻止启动")
        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("无法启动")
        msg.setText("该版本已过期，请联系管理员。")
        msg.setStandardButtons(QMessageBox.Ok)
        msg.exec()
        sys.exit(0)
    if _need_force_update(VERSION, remote_status["min_version"]):
        logger.warning(f"版本过低被强制更新: 当前v{VERSION} < 要求v{remote_status['min_version']}")
        from PySide6.QtWidgets import QApplication as _QApp
        _show_force_update_dialog(remote_status["min_version"])
        sys.exit(0)

    # 单实例检查（文件锁 + QLocalServer唤醒）
    # 文件锁：独占打开锁文件，进程退出自动释放
    if os.environ.get("FLASH_DEV") != "1":
        lock_path = os.path.join(os.path.expanduser("~"), ".flash-connector", ".single.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        try:
            _lock_fp = open(lock_path, "w")
            import msvcrt
            msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
            # 锁成功——第一个实例
            global _single_lock_fp
            _single_lock_fp = _lock_fp
        except (OSError, IOError):
            # 锁失败——已有实例
            logger.info("已有 Token接入器 实例在运行，退出重复启动")
            from PySide6.QtWidgets import QMessageBox
            from PySide6.QtGui import QIcon
            msg = QMessageBox()
            _icon_paths = []
            if getattr(sys, 'frozen', False):
                _icon_paths.append(os.path.join(sys._MEIPASS, 'assets', 'icons', 'app.png'))
            _src_dir = os.path.dirname(os.path.abspath(__file__))
            _icon_paths.append(os.path.join(_src_dir, '..', 'assets', 'icons', 'app.png'))
            for _p in _icon_paths:
                if os.path.isfile(_p):
                    _icon = QIcon(_p)
                    if not _icon.isNull():
                        msg.setWindowIcon(_icon)
                        break
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("Token接入器 已在运行")
            msg.setText("Token接入器 已经在运行中！\n\n请检查系统托盘（右下角图标）或任务栏，\n双击图标即可恢复窗口。")
            msg.setStandardButtons(QMessageBox.Ok)
            msg.exec()
            # 尝试唤醒已有实例
            try:
                from PySide6.QtNetwork import QLocalSocket
                socket = QLocalSocket()
                socket.connectToServer("flash-connector-single-instance")
                socket.waitForConnected(500)
                if socket.state() == QLocalSocket.ConnectedState:
                    socket.write(b"SHOW")
                    socket.flush()
                    socket.waitForBytesWritten(1000)
                    socket.disconnectFromServer()
            except Exception:
                pass
            sys.exit(0)

    # QLocalServer（唤醒通道，给文件锁通过后的第一个实例用）
    if os.environ.get("FLASH_DEV") != "1":
        try:
            from PySide6.QtNetwork import QLocalServer
            QLocalServer.removeServer("flash-connector-single-instance")
            global _single_instance_server
            _single_instance_server = QLocalServer()
            _single_instance_server.listen("flash-connector-single-instance")
        except Exception:
            pass

    # 设置默认字体（跨平台）
    import platform
    if platform.system() == "Darwin":
        font = QFont("PingFang SC", 13)  # macOS 中文字体
    else:
        font = QFont("Microsoft YaHei", 10)  # Windows 中文字体
    app.setFont(font)

    # 创建主窗口
    global _main_window
    _main_window = MainWindow()
    _main_window.show()

    # 监听单实例服务器的唤醒信号（第二次启动时显示窗口）
    if _single_instance_server:
        def _on_new_connection():
            client = _single_instance_server.nextPendingConnection()
            if client:
                client.waitForReadyRead(1000)
                data = client.readAll().data()
                client.disconnectFromServer()
                if data == b"SHOW":
                    logger.info("收到唤醒信号，显示主窗口")
                    _main_window.show()
                    _main_window.activateWindow()
                    _main_window.raise_()
        _single_instance_server.newConnection.connect(_on_new_connection)

    logger.info("Token接入器 已启动")

    # 运行 Qt 事件循环
    ret = app.exec()

    # 事件循环退出后，执行清理
    _force_cleanup()

    # 给非 daemon 线程 2 秒时间退出，超时则强制终止
    import threading
    non_daemon = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive() and not t.daemon]
    if non_daemon:
        logger.info(f"等待 {len(non_daemon)} 个线程退出...")
        for t in non_daemon:
            t.join(timeout=2.0)
        still_alive = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive() and not t.daemon]
        if still_alive:
            logger.warning(f"仍有 {len(still_alive)} 个线程未退出，强制终止进程")
            os._exit(ret)

    sys.exit(ret)


if __name__ == "__main__":
    main()
