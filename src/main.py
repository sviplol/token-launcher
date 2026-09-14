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

from .main_window import MainWindow


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
    """强制清理所有资源（atexit 和信号处理时调用）"""
    global _main_window
    if _main_window:
        # 注意：不杀 WorkBuddy！它是独立应用，关闭本软件不应影响它
        # 停止代理服务器
        try:
            api_proxy_page = _main_window._pages.get("api_proxy")
            if api_proxy_page:
                api_proxy_page._cleanup()
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


def _check_remote_disabled() -> bool:
    """启动时检查服务器端的客户端禁用开关。

    返回 True=已禁用（应阻止启动）。
    服务器不可达/超时=放行（不能因网络问题挡住所有用户）。
    """
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
        return bool(data.get("ok")) and bool(data.get("disabled"))
    except Exception:
        return False


def main():
    """应用入口"""
    _setup_logging()

    # 自动部署到桌面"前台"文件夹（替换旧版本）
    _auto_deploy_to_desktop()

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

    # 远程禁用检查（服务器开关，最初始阶段拦截）
    if _check_remote_disabled():
        logger.warning("远程禁用开关已开启，阻止启动")
        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("无法启动")
        msg.setText("该版本已过期，请联系管理员。")
        msg.setStandardButtons(QMessageBox.Ok)
        msg.exec()
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
