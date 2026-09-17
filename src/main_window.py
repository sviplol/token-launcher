"""Token接入器 主窗口 v9.9.8 — 极简黑白主题 + 日落自动切换"""

import logging, os, sys
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QStackedWidget,
    QSystemTrayIcon, QMenu, QApplication, QLabel, QPushButton
)
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PySide6.QtCore import Qt, QSize, QTimer

from .ui import Sidebar, ThemeManager, resolve_colors, get_theme_setting, MODE_LABEL
from .ui.pages import (
    DashboardPage, CheckinPage,
    SettingsPage, HotSwitchPage,
)
from .utils.store import init_db, load_setting

logger = logging.getLogger(__name__)

VERSION = "9.10.7"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Token接入器 v{VERSION}")
        self.setMinimumSize(QSize(1000, 650))
        self.resize(1280, 800)
        init_db()
        self._setup_ui()
        self._setup_tray()
        # 主题管理（极简白/黑 + 日落自动）
        self._theme_mgr = ThemeManager(self)
        self._sidebar.theme_cycle_requested.connect(self._on_theme_cycle)
        self._theme_mgr.apply()

    def _load_icon(self) -> QIcon:
        paths = []
        if getattr(sys, 'frozen', False):
            paths.append(os.path.join(sys._MEIPASS, 'assets', 'icons', 'app.png'))
            paths.append(os.path.join(sys._MEIPASS, 'assets', 'icons', 'app.ico'))
        src_dir = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(src_dir)
        paths.append(os.path.join(root, 'assets', 'icons', 'app.png'))
        paths.append(os.path.join(root, 'assets', 'icons', 'app.ico'))
        for p in paths:
            if os.path.isfile(p):
                icon = QIcon(p)
                if not icon.isNull():
                    return icon
        pix = QPixmap(64, 64)
        pix.fill(QColor(0, 0, 0, 0))
        pt = QPainter(pix)
        pt.setRenderHint(QPainter.Antialiasing)
        pt.setBrush(QColor("#0A0A0A"))
        pt.setPen(Qt.PenStyle.NoPen)
        pt.drawRoundedRect(4, 4, 56, 56, 12, 12)
        pt.setPen(QColor("#FFFFFF"))
        f = QFont("Segoe UI", 30, QFont.Bold)
        pt.setFont(f)
        from PySide6.QtCore import QRect
        pt.drawText(QRect(0, 0, 64, 64), Qt.AlignmentFlag.AlignCenter, "F")
        pt.end()
        return QIcon(pix)

    def _setup_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._sidebar = Sidebar()
        self._sidebar.page_changed.connect(self._switch_page)
        main_layout.addWidget(self._sidebar)

        right = QWidget()
        right.setObjectName("rightPane")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 顶部栏 52px
        topbar = QWidget()
        topbar.setObjectName("topBar")
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(20, 0, 20, 0)
        topbar_layout.setSpacing(8)

        icon_lbl = QLabel()
        icon = self._load_icon()
        if not icon.isNull():
            icon_lbl.setPixmap(icon.pixmap(28, 28))
        icon_lbl.setFixedSize(28, 28)
        topbar_layout.addWidget(icon_lbl)

        title_lbl = QLabel("Token接入器")
        title_lbl.setObjectName("topTitle")
        topbar_layout.addWidget(title_lbl)

        ver_lbl = QLabel(f"v{VERSION}")
        ver_lbl.setObjectName("topVersion")
        topbar_layout.addWidget(ver_lbl)

        topbar_layout.addStretch()

        # 全局卡密按钮
        self._global_card_btn = QPushButton("🎫 输入卡密添加账号")
        self._global_card_btn.setObjectName("primary")
        self._global_card_btn.setCursor(Qt.PointingHandCursor)
        self._global_card_btn.setMinimumHeight(36)
        self._global_card_btn.clicked.connect(self._global_card_activate)
        topbar_layout.addWidget(self._global_card_btn)

        right_layout.addWidget(topbar)

        # 页面堆栈（账号管理已合并进一键接入，闭源不再独立展示）
        self._stack = QStackedWidget()
        self._stack.setObjectName("pageStack")
        self._pages = {
            "dashboard": DashboardPage(),
            "checkin": CheckinPage(),
            "hotswitch": HotSwitchPage(),
            "settings": SettingsPage(),
        }
        for page in self._pages.values():
            self._stack.addWidget(page)

        self._pages["settings"].set_main_window(self)

        right_layout.addWidget(self._stack, 1)
        main_layout.addWidget(right, 1)
        self._stack.setCurrentWidget(self._pages["dashboard"])

        # 启动时卡密校验（防白嫖）：本地全部卡密号的账号，逐卡对服务器校验
        # 封禁/删除的卡 → 自动回收本地对应账号数据
        self._verify_thread = None
        QTimer.singleShot(5000, self._startup_card_verify)
        # 在线实时收回：每60秒轮询服务器（后台删卡/释放号 → 不重启软件即时回收本地账号）
        self._card_verify_timer = QTimer(self)
        self._card_verify_timer.setInterval(60 * 1000)
        self._card_verify_timer.timeout.connect(self._periodic_card_verify)
        self._card_verify_timer.start()

    def _periodic_card_verify(self):
        """在线轮询：后台删卡/释放号后，客户端1分钟内自动收回（无需重启）"""
        self._startup_card_verify(periodic=True)

    def _startup_card_verify(self, periodic: bool = False):
        """后台校验本地卡密有效性，回收封禁/删除卡的本地账号（启动+定时轮询共用）"""
        def _worker():
            try:
                import sqlite3
                from .utils.store import _get_db_path
                db_path = str(_get_db_path())
                if not os.path.exists(db_path):
                    return
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                try:
                    rows = c.execute(
                        "SELECT DISTINCT account_group FROM accounts WHERE account_group LIKE 'WK-%'"
                    ).fetchall()
                except Exception:
                    conn.close()
                    return
                keys = [r[0] for r in rows if r[0]]
                conn.close()
                if not keys:
                    return  # 本地没有卡密来源的账号，跳过
                from .modules.card_client import verify_local_cards
                result = verify_local_cards(keys)
                if not result.get("ok"):
                    return  # 网络失败/服务器不可达：放行（不误杀真实用户）

                bad_keys = list(result.get("revoked") or []) + list(result.get("notfound") or [])

                # 释放回收：服务器返回每张卡当前绑定的uid列表（bound），
                # 本地账号 group=卡密 但 uid 不在绑定列表 = 后台已释放/换号 → 自动回收
                released_uids = []
                rel_tokens = set()
                bound_map = result.get("bound") or {}
                if isinstance(bound_map, dict) and bound_map:
                    conn_r = sqlite3.connect(db_path)
                    c_r = conn_r.cursor()
                    try:
                        for bk, bound_uids in bound_map.items():
                            if not bk:
                                continue
                            bound_set = {str(u) for u in (bound_uids or [])}
                            rows_r = c_r.execute(
                                "SELECT uid, auth_token, api_key FROM accounts WHERE account_group=?", (bk,)
                            ).fetchall()
                            for uid_r, at_r, ak_r in rows_r:
                                if uid_r not in bound_set:
                                    released_uids.append((bk, uid_r))
                                    if at_r:
                                        rel_tokens.add(at_r)
                                    if ak_r:
                                        rel_tokens.add(ak_r)
                        # 删除被释放的本地账号
                        for bk, lu in released_uids:
                            c_r.execute("DELETE FROM accounts WHERE uid=? AND account_group=?", (lu, bk))
                        conn_r.commit()
                    except Exception:
                        pass
                    finally:
                        conn_r.close()
                    if released_uids:
                        logger.info(f"[卡密校验] 回收 {len(released_uids)} 个已释放账号（后台释放/换号）: {[u for _, u in released_uids[:10]]}")
                if not bad_keys and not released_uids:
                    return  # 全部有效

                # 回收：删除封禁/删除卡的本地账号（token删除前先收集，供Key池清理）
                import logging
                logger = logging.getLogger(__name__)
                conn2 = sqlite3.connect(db_path)
                c2 = conn2.cursor()
                removed_uids = []
                for bk in bad_keys:
                    uids_rows = c2.execute(
                        "SELECT uid, auth_token, api_key FROM accounts WHERE account_group=?", (bk,)
                    ).fetchall()
                    for u_r, at_r, ak_r in uids_rows:
                        removed_uids.append(u_r)
                        if at_r:
                            rel_tokens.add(at_r)
                        if ak_r:
                            rel_tokens.add(ak_r)
                    c2.execute("DELETE FROM accounts WHERE account_group=?", (bk,))
                conn2.commit()
                conn2.close()
                # 同步清理上游Key池（按token精确匹配）
                if rel_tokens:
                    try:
                        from .modules.proxy_server import ProxyDatabase
                        pdb = ProxyDatabase.get_instance()
                        for k in pdb.get_upstream_keys():
                            if k.get("api_key", "") in rel_tokens:
                                pdb.delete_upstream_key(k.get("key_id", ""))
                    except Exception:
                        pass
                if bad_keys:
                    logger.info(f"[卡密校验] 回收 {len(bad_keys)} 张失效卡密的 {len(removed_uids)} 个本地账号: {bad_keys}")

                # UI刷新：本地库被改后，通知账号页/接入页重新加载（回到主线程执行）
                if released_uids or bad_keys:
                    from PySide6.QtCore import QTimer as _QTimer
                    _QTimer.singleShot(0, self._refresh_after_reclaim)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("卡密校验异常（不影响使用）")

        from PySide6.QtCore import QThread
        if self._verify_thread is not None and self._verify_thread.isRunning():
            return  # 上一轮校验还在跑，跳过本轮（防重叠）
        class _VerifyThread(QThread):
            def run(self):
                _worker()
        self._verify_thread = _VerifyThread()
        self._verify_thread.start()

    def _refresh_after_reclaim(self):
        """账号被收回后刷新相关页面（主线程执行）"""
        try:
            hs = self._pages.get("hotswitch")
            if hs and hasattr(hs, "_refresh_upstream_keys"):
                hs._refresh_upstream_keys(reload_from_disk=True)
        except Exception:
            pass

    def _global_card_activate(self):
        self._switch_page("hotswitch")
        hs = self._pages.get("hotswitch")
        if hs and hasattr(hs, '_card_key_fetch'):
            hs._card_key_fetch()

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip(f"Token接入器 v{VERSION}")
        icon = self._load_icon()
        self._tray.setIcon(icon)
        self.setWindowIcon(icon)

        menu = QMenu()
        show_act = menu.addAction("显示主窗口")
        show_act.triggered.connect(self._show_window)
        card_act = menu.addAction("🎫 输入卡密添加账号")
        card_act.triggered.connect(self._global_card_activate)
        menu.addSeparator()
        quit_act = menu.addAction("退出")
        quit_act.triggered.connect(self._quit_app)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)

        if load_setting("close_behavior", "minimize") == "minimize":
            self._tray.show()

    def _on_theme_cycle(self):
        """侧边栏主题按钮点击 → 三档循环"""
        self._theme_mgr.cycle()
        self._sidebar.refresh_theme_button()
        mode = get_theme_setting()
        self._tray.showMessage("主题切换", f"已切换为：{MODE_LABEL.get(mode, '自动')}")

    def apply_theme(self, theme=None):
        """供 ThemeManager 调用"""
        self._theme_mgr.apply()
        self._sidebar.refresh_theme_button()

    def _switch_page(self, page_id):
        page = self._pages.get(page_id)
        if page:
            self._stack.setCurrentWidget(page)

    def _show_window(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._show_window()

    def _quit_app(self):
        for page in self._pages.values():
            try:
                if hasattr(page, '_worker') and page._worker:
                    page._worker.stop()
                if hasattr(page, '_status_worker') and page._status_worker:
                    page._status_worker.stop()
                if hasattr(page, '_batch_worker') and page._batch_worker:
                    page._batch_worker.stop()
            except Exception:
                pass
        try:
            self._tray.hide()
        except Exception:
            pass
        try:
            QApplication.instance().quit()
        except Exception:
            pass
        import threading
        for t in threading.enumerate():
            if t is not threading.main_thread() and t.is_alive():
                try:
                    t.join(timeout=1.0)
                except Exception:
                    pass
        still = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
        if still:
            os._exit(0)

    def closeEvent(self, event):
        if load_setting("close_behavior", "minimize") == "minimize":
            event.ignore()
            self.hide()
            self._tray.show()
            self._tray.showMessage("Token接入器", "已最小化到系统托盘")
        else:
            event.accept()
            self._quit_app()
