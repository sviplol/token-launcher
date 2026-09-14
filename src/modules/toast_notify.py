# -*- coding: utf-8 -*-
"""Windows Toast 通知工具 — 用程序图标（解决 Qt showMessage 图标限制）"""
import os
import logging

logger = logging.getLogger(__name__)


def show_toast(title: str, message: str, duration: str = "short"):
    """发送 Windows 原生 Toast 通知（用程序图标）

    Qt 的 QSystemTrayIcon.showMessage 在 Win10/11 传 QIcon 会被系统忽略，
    只能用 MessageIcon 枚举（蓝i/黄!/红x）。改用 winotify 发原生 Toast，
    自动关联 exe 图标。

    Args:
        title: 通知标题
        message: 通知内容
        duration: "short" 或 "long"
    """
    try:
        from winotify import Notification
        # 图标路径：frozen 模式从 _MEIPASS 取，开发模式从 assets 取
        icon_path = None
        import sys
        if getattr(sys, 'frozen', False):
            _icon = os.path.join(sys._MEIPASS, 'assets', 'icons', 'app.ico')
            if os.path.exists(_icon):
                icon_path = _icon
        else:
            _base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            _icon = os.path.join(_base, 'assets', 'icons', 'app.ico')
            if os.path.exists(_icon):
                icon_path = _icon

        toast = Notification(
            app_id="flash.flashconnector.flash",
            title=title,
            msg=message,
            duration=duration,
            icon=icon_path,
        )
        toast.show()
        logger.info(f"[Toast] {title}: {message[:50]}")
    except ImportError:
        # winotify 不可用则降级到 Qt 托盘
        logger.warning("[Toast] winotify 不可用，降级到 Qt 托盘通知")
        try:
            from PySide6.QtWidgets import QSystemTrayIcon, QApplication
            app = QApplication.instance()
            if app:
                # 找到主窗口的托盘
                for w in app.topLevelWidgets():
                    if hasattr(w, '_tray') and w._tray:
                        w._tray.showMessage(title, message)
                        break
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[Toast] 发送失败: {e}")

