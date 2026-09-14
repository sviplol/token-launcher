"""一键接入页面 - CodeBuddy / WorkBuddy 透明中转

独立的本地中转服务（默认端口 8003）：
- 原样转发到官方服务器，仅在对话计费请求经过时用池里的账号 token 替换
- 上游 Key 池只放账号 token（JWT），禁用/恢复状态独立于 API 代理页
- 使用日志独立，只记录经过本中转的请求
"""

import secrets
import time
import logging

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QLineEdit, QSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QTabWidget, QTextEdit, QMessageBox, QApplication,
    QScrollArea, QDialog, QToolButton, QMenu, QComboBox,
    QFormLayout, QDialogButtonBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QBrush, QColor, QIntValidator

from ...utils.store import save_setting, load_setting
from ...modules.proxy_server import ProxyDatabase
from ...modules.codebuddy_relay import (
    CodeBuddyRelayServer, apply_client_config, restore_client_config,
    get_client_config_state, is_dev_mode_enabled, enable_dev_mode,
    is_codebuddy_running, is_codebuddy_installed, is_workbuddy_installed,
    apply_workbuddy_config, restore_workbuddy_config,
    get_workbuddy_config_state,
)
from .api_proxy import (
    ImportFromAccountsDialog, _style_popup_menu, _fmt_tokens, ApiProxyPage,
    _get_account_concurrency_setting,
)

logger = logging.getLogger(__name__)


def _set_item(table, row, col, text, tooltip=None):
    """设置表格单元格，自动加 tooltip 显示完整内容"""
    item = QTableWidgetItem(text)
    item.setToolTip(tooltip if tooltip else text)
    table.setItem(row, col, item)
    return item


def _html_esc(text: str) -> str:
    """HTML 转义（日志原文可能含 < > &）"""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _colorize_event_html(ev: str) -> str:
    """使用日志按事件内容着色（照 API 代理页：错误一眼可见）"""
    text = _html_esc(ev)
    lower = ev.lower()
    if any(k in lower for k in ("限流", "429", "冷却")):
        color = "#D69E2E"  # 橙黄：限流冷却
    elif any(k in lower for k in ("14018", "14019", "耗尽", "无可用", "积分不足")):
        color = "#E53E3E"  # 红：积分耗尽
    elif any(k in lower for k in ("11140", "风控", "异常")):
        color = "#E53E3E"  # 红：风控/异常
    elif "换号" in ev:
        color = "#2B6CB0"  # 蓝：换号
    elif "透传" in ev:
        color = "#9BA4B0"  # 灰：透传
    else:
        color = "#718096"
    return f"<span style='color:{color}'>{text}</span>"


def _set_multiline_text(label: QLabel, text: str):
    """给 wordWrap QLabel 写多行文本并按行数锁定最小高度。

    实测：macOS 上 QLabel sizeHint 不随 setText 的行数更新，
    布局仍按 1 行分高，多行文字被裁掉一半。按行数 setMinimumHeight 兜底。
    """
    label.setText(text)
    fm = label.fontMetrics()
    lines = text.count("\n") + 1
    label.setMinimumHeight(fm.lineSpacing() * lines + 8)


class _SkKeyDialog(QDialog):
    """专属接入 Key 子窗口 — 其他 Agent 平台接入用"""

    def __init__(self, parent, page: "HotSwitchPage"):
        super().__init__(parent)
        self._page = page
        self.setWindowTitle("🔑 专属接入 Key")
        self.setMinimumSize(560, 420)
        self.setStyleSheet("""
            QDialog { background: #FFFFFF; }
            QLabel { color: #1A1A1A; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        # 红字提示（用户要求：必须红字）
        warn = QLabel(
            "⚠️ WorkBuddy 用户一键接入直接用，无需配置自定义模型。\n"
            "其他平台用户才需要用到这个 Key！"
        )
        warn.setStyleSheet(
            "color: #C0271D; font-size: 14px; font-weight: 700;"
            "background: #F9E8E7; border: 1px solid #F2CECC;"
            "border-radius: 10px; padding: 12px;"
        )
        warn.setWordWrap(True)
        layout.addWidget(warn)

        # 接入地址
        addr_label = QLabel("接入地址（其他平台 API Base URL 填这个）：")
        addr_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        layout.addWidget(addr_label)
        addr_edit = QLineEdit("http://127.0.0.1:8003/v1")
        addr_edit.setReadOnly(True)
        addr_edit.setMinimumHeight(38)
        addr_edit.setStyleSheet("""
            QLineEdit { background: #F4F4F5; border: 1px solid #E4E7EB; border-radius: 8px;
                        padding: 8px 12px; font-family: Consolas, monospace; font-size: 13px; }
        """)
        layout.addWidget(addr_edit)

        # Key 显示
        key_label = QLabel("专属接入 Key（随机生成，不可自定义）：")
        key_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        layout.addWidget(key_label)
        self._key_edit = QLineEdit(page._get_hotswitch_key())
        self._key_edit.setReadOnly(True)
        self._key_edit.setMinimumHeight(38)
        self._key_edit.setStyleSheet("""
            QLineEdit { background: #F4F4F5; border: 1px solid #E4E7EB; border-radius: 8px;
                        padding: 8px 12px; font-family: Consolas, monospace; font-size: 13px; }
        """)
        layout.addWidget(self._key_edit)

        # 说明
        desc = QLabel(
            "用法：在 Cherry Studio / Claude Code / Cursor 等平台的 API 设置里，\n"
            "Base URL 填上面的接入地址，API Key 填上面这串 Key 即可。\n"
            "点击「🎲 随机重生成」会换新 Key，旧 Key 立即失效。"
        )
        desc.setStyleSheet("color: #6B7280; font-size: 12px;")
        layout.addWidget(desc)

        # 调度模式
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("调度模式:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("专一（推荐）", 1)
        self._mode_combo.addItem("临期优先", 2)
        self._mode_combo.addItem("轮询", 3)
        self._mode_combo.addItem("会话亲和", 4)
        current_mode = page._load_hotswitch_mode()
        self._mode_combo.setCurrentIndex({1: 0, 2: 1, 3: 2, 4: 3}.get(current_mode, 0))
        self._mode_combo.setMinimumHeight(36)
        self._mode_combo.setMinimumWidth(160)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo)

        btn_help = QToolButton()
        btn_help.setText("❓")
        btn_help.setToolTip("四种调度模式的区别与适用场景")
        btn_help.setCursor(Qt.PointingHandCursor)
        btn_help.setFixedSize(36, 36)
        btn_help.setStyleSheet("""
            QToolButton { border: none; border-radius: 18px; font-size: 16px;
                          background: #F4F4F5; color: #6B7280; }
            QToolButton:hover { background: #E4E7EB; color: #1A1A1A; }
        """)
        btn_help.clicked.connect(page._show_mode_help)
        mode_row.addWidget(btn_help)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        layout.addStretch()

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_regen = QPushButton("🎲 随机重生成")
        btn_regen.setObjectName("secondary_btn")
        btn_regen.setCursor(Qt.PointingHandCursor)
        btn_regen.setMinimumHeight(40)
        btn_regen.setToolTip("重新生成随机 Key（旧 Key 立即失效）")
        btn_regen.clicked.connect(self._on_regen)
        btn_row.addWidget(btn_regen)

        btn_copy_addr = QPushButton("📋 复制地址")
        btn_copy_addr.setObjectName("secondary_btn")
        btn_copy_addr.setCursor(Qt.PointingHandCursor)
        btn_copy_addr.setMinimumHeight(40)
        btn_copy_addr.setToolTip("复制API接入地址到剪贴板")
        btn_copy_addr.clicked.connect(self._on_copy_addr)
        btn_row.addWidget(btn_copy_addr)

        btn_copy = QPushButton("📋 复制 Key")
        btn_copy.setObjectName("primary_btn")
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_copy.setMinimumHeight(40)
        btn_copy.clicked.connect(self._on_copy)
        btn_row.addWidget(btn_copy)

        btn_close = QPushButton("关闭")
        btn_close.setObjectName("secondary_btn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setMinimumHeight(40)
        btn_close.clicked.connect(self.reject)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _on_copy_addr(self):
        from PySide6.QtWidgets import QApplication
        addr = "http://127.0.0.1:8003/v1"
        QApplication.clipboard().setText(addr)
        QMessageBox.information(self, "已复制", "API接入地址已复制到剪贴板：\n" + addr)

    def _on_regen(self):
        reply = QMessageBox.question(
            self, "重新生成",
            "将生成新的随机 Key，旧 Key 立即失效（正在使用旧 Key 的客户端会断开）。\n确定继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        new_key = self._page._regen_hotswitch_key()
        self._key_edit.setText(new_key)

    def _on_copy(self):
        from PySide6.QtWidgets import QApplication
        key = self._key_edit.text().strip()
        if key:
            QApplication.clipboard().setText(key)
            QMessageBox.information(self, "已复制", "Key 已复制到剪贴板")

    def _on_mode_changed(self, index: int):
        mode = self._mode_combo.itemData(index) or 1
        self._page._save_hotswitch_mode(mode)


class HotSwitchPage(QWidget):
    """一键接入页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 页面背景走全局 QSS #content_area（与其他页面一致）
        self.setObjectName("content_area")
        self._db = ProxyDatabase.get_instance()
        self._relay_server: CodeBuddyRelayServer = None
        # 持有 QMenu 的 Python 引用，防止局部菜单被 GC 后 Qt 点击事件访问已释放对象（SIGSEGV）
        self._open_menus: list = []
        self._setup_ui()

        # 中转状态/日志定时刷新
        self._relay_timer = QTimer(self)
        self._relay_timer.timeout.connect(self._on_timer)
        self._relay_timer.start(2000)
        # Tab切换时立即刷新对应内容（切到日志Tab秒出最新事件）
        self._tab_widget.currentChanged.connect(self._on_tab_changed)

        # 首次启动自动生成专属接入 Key（随机 sk，不可自定义）
        self._ensure_hotswitch_key()

        # 上次开启过中转的话，启动后自动拉起（静默，不弹窗）
        QTimer.singleShot(800, self._autostart_relay)

        # Key池→accounts表自动同步（签到页数据源）：池里有号但accounts表缺的，反哺入库
        QTimer.singleShot(1500, self._sync_pool_to_accounts)

    def _sync_pool_to_accounts(self):
        """把上游Key池里的号反哺到flash.db accounts表（签到页/积分明细的数据源）。

        历史原因：早期版本卡密导入只写了proxy_db没写accounts → 签到页空。
        此处按uid去重补录：JWT解析uid+RT存auth_raw → 签到功能即可用。
        """
        try:
            import json as _json
            import sqlite3 as _sql3
            import base64 as _b64
            from datetime import datetime as _dt

            def _jwt_uid(token: str) -> str:
                try:
                    parts = token.split(".")
                    if len(parts) < 2:
                        return ""
                    payload = parts[1]
                    payload += "=" * (-len(payload) % 4)
                    data = _json.loads(_b64.urlsafe_b64decode(payload))
                    return str(data.get("sub", "") or "")
                except Exception:
                    return ""

            from ...utils.store import _get_db_path as _dbp
            db_path = str(_dbp())
            keys = self._jwt_keys()
            if not keys:
                return
            conn = _sql3.connect(db_path)
            existing = {r[0] for r in conn.execute("SELECT uid FROM accounts").fetchall()}
            added = 0
            for k in keys:
                token = k.get("api_key", "")
                if not token.startswith("eyJ"):
                    continue
                uid = _jwt_uid(token)
                if not uid or uid in existing:
                    continue
                label = k.get("label", "") or uid[:12]
                conn.execute(
                    "INSERT OR IGNORE INTO accounts (uid, nickname, platform, status, auth_token, auth_raw, created_at) VALUES (?,?,?,?,?,?,?)",
                    (uid, label, "codebuddy", "active", token,
                     _json.dumps({"accessToken": token, "refreshToken": ""}),
                     _dt.now().isoformat()))
                existing.add(uid)
                added += 1
            conn.commit()
            conn.close()
            if added > 0:
                logger.info(f"[Key池同步] 反哺{added}个号到accounts表（签到页数据源）")
        except Exception:
            logger.exception("Key池→accounts同步失败（不影响Key池使用）")

    # ═══════════ 专属接入 Key ═══════════

    _HS_KEY_ID = "hotswitch_default"

    def _ensure_hotswitch_key(self):
        """首次启动生成随机专属 Key（只入库，不碰 UI）"""
        try:
            sub_keys = self._db.get_sub_api_keys()
            existing = [k for k in sub_keys if k.get("key_id") == self._HS_KEY_ID]
            if not existing:
                key_data = {
                    "key_id": self._HS_KEY_ID,
                    "api_key": f"sk-{secrets.token_urlsafe(32)}",
                    "label": "一键接入专属Key",
                    "is_active": True,
                    "allowed_models": [],
                    "allowed_key_ids": [],
                    "max_usage": 0,
                    "used_count": 0,
                    "rate_limit_rpm": 1000,
                    "key_mode": self._load_hotswitch_mode(),
                    "created_at": __import__('datetime').datetime.now().isoformat(),
                }
                self._db.add_sub_api_key(key_data)
        except Exception as e:
            logger.error(f"专属Key初始化失败: {e}")

    def _get_hotswitch_key(self) -> str:
        """读取当前专属 Key"""
        try:
            for k in self._db.get_sub_api_keys():
                if k.get("key_id") == self._HS_KEY_ID:
                    return k.get("api_key", "")
        except Exception:
            pass
        return ""

    def _regen_hotswitch_key(self) -> str:
        """随机重新生成专属 Key（旧 Key 立即失效），返回新 Key"""
        new_key = f"sk-{secrets.token_urlsafe(32)}"
        self._db.update_sub_api_key(self._HS_KEY_ID, {
            "api_key": new_key,
        })
        return new_key

    def _load_hotswitch_mode(self) -> int:
        return int(load_setting("hotswitch_key_mode", "1") or 1)

    def _save_hotswitch_mode(self, mode: int):
        save_setting("hotswitch_key_mode", str(mode))
        try:
            self._db.update_sub_api_key(self._HS_KEY_ID, {"key_mode": mode})
        except Exception:
            pass

    def _show_sk_key_dialog(self):
        """专属接入 Key 子窗口（不占主界面空间）"""
        dlg = _SkKeyDialog(self, self)
        dlg.exec()

    def _show_mode_help(self):
        """四种调度模式费曼解读"""
        QMessageBox.information(
            self, "四种调度模式解读",
            "【专一模式（推荐）】\n"
            "用一个比喻：就像你会固定找一个熟悉的出租车司机。一个号一直用到积分耗尽/被限流，"
            "才换下一个号。好处是行为最像真人，账号压力最小，最安全。\n\n"
            "【临期优先】\n"
            "比喻：超市先把快过期的牛奶摆到最前面卖。优先消耗积分最快过期的号，"
            "把快作废的积分先吃干净，一点不浪费。适合号多、积分有有效期的囤号党。\n\n"
            "【轮询模式】\n"
            "比喻：发牌员轮流发牌，一人一张。每个请求换下一个号，压力平均分摊到所有号。"
            "适合高并发跑量，单号压力最低，但请求分散不像单一真人。\n\n"
            "【会话亲和】\n"
            "比喻：同一个客人永远安排同一个服务员。同一轮对话固定用同一个号，"
            "上下文连贯不串号。适合多轮长对话（Claude Code / Agent 长任务）。"
        )

    # ═══════════ UI 构建 ═══════════

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("一键接入")
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("点击下方按钮自动接入WorkBuddy和CodeBuddy，直接正常使用官方模型即可")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        content = QWidget()
        content.setObjectName("content_area")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(16)

        # ─── 核心开关区（大按钮醒目）───
        control_card = QFrame()
        control_card.setObjectName("card")
        control_layout = QVBoxLayout(control_card)
        control_layout.setSpacing(16)
        control_layout.setContentsMargins(24, 24, 24, 24)

        # 状态行
        status_row = QHBoxLayout()
        self._status_label = QLabel("⏹ 接入服务未开启")
        self._status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #9CA3AF;")
        status_row.addWidget(self._status_label)
        status_row.addStretch()

        # 专属接入 Key 按钮（弹子窗口，不占页面空间）
        self._btn_sk_key = QPushButton("🔑 API Key")
        self._btn_sk_key.setObjectName("secondary_btn")
        self._btn_sk_key.setCursor(Qt.PointingHandCursor)
        self._btn_sk_key.setToolTip("专属接入 Key（其他 Agent 平台接入用，WorkBuddy 用户无需配置）")
        self._btn_sk_key.clicked.connect(self._show_sk_key_dialog)
        status_row.addWidget(self._btn_sk_key)

        self._url_label = QLabel("")
        self._url_label.setStyleSheet("font-size: 13px; color: #6B7280;")
        self._url_label.setVisible(False)
        status_row.addWidget(self._url_label)
        control_layout.addLayout(status_row)

        # 核心大按钮（红色字醒目）
        # 启动接入 = 消耗走卡密账号（Key池）；停止接入 = 消耗走 WorkBuddy 自己登录的账号
        self._toggle_btn = QPushButton("⚡ 启动接入（消耗卡密账号）")
        self._toggle_btn.setObjectName("bigPrimary")
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.setMinimumHeight(56)
        self._toggle_btn.setToolTip("启动后：WorkBuddy/CodeBuddy 的对话消耗走卡密账号（上游Key池），官方模型直接用\n停止后：消耗走你自己登录的 WorkBuddy 账号")
        self._toggle_btn.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #C0271D; border: 2px solid #C0271D;
                border-radius: 14px; font-size: 20px; font-weight: 800;
                padding: 14px 32px;
            }
            QPushButton:hover { background: #F9E8E7; }
            QPushButton:pressed { background: #F2D7D5; }
        """)
        self._toggle_btn.clicked.connect(self._toggle_service)
        control_layout.addWidget(self._toggle_btn)

        # 高级设置行（折叠，默认隐藏端口/阈值）
        adv_row = QHBoxLayout()
        adv_row.addWidget(QLabel("端口:"))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(int(load_setting("codebuddy_relay_port", "8003")))
        adv_row.addWidget(self._port_spin)

        # 监听模式已移除（固定本地，不开放外网）

        adv_row.addWidget(QLabel("最低积分:"))
        self._min_credits_spin = QSpinBox()
        self._min_credits_spin.setRange(0, 100000)
        self._min_credits_spin.setValue(int(load_setting("hotswitch_min_credits", "0")))
        self._min_credits_spin.setSuffix(" 分")
        adv_row.addWidget(self._min_credits_spin)

        self._auto_enable_spin = QSpinBox()
        self._auto_enable_spin.setRange(0, 100000)
        self._auto_enable_spin.setValue(int(load_setting("hotswitch_auto_enable", "100")))
        self._auto_enable_spin.setSuffix(" 分")
        adv_row.addWidget(self._auto_enable_spin)

        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(1, 3600)
        self._cooldown_spin.setValue(int(load_setting("cooldown_seconds", "10")))
        self._cooldown_spin.setSuffix(" 秒")
        self._cooldown_spin.valueChanged.connect(lambda v: save_setting("cooldown_seconds", str(v)))
        adv_row.addWidget(self._cooldown_spin)

        self._open_mode_hint = QLabel("")
        self._open_mode_hint.setStyleSheet("color: #EF4444; font-size: 12px;")
        self._open_mode_hint.setVisible(False)
        adv_row.addWidget(self._open_mode_hint)
        adv_row.addStretch()

        self._min_credits_spin.valueChanged.connect(self._apply_thresholds_now)
        self._auto_enable_spin.valueChanged.connect(self._apply_thresholds_now)
        control_layout.addLayout(adv_row)

        content_layout.addWidget(control_card)

        # ─── Tab 区：上游 Key 池 / 使用日志 ───
        self._tab_widget = QTabWidget()
        self._build_pool_tab()
        self._build_log_tab()
        # 客户端接入：中转启动/停止逻辑依赖这些控件，保留创建但不挂 Tab（完全隐藏）
        self._build_client_tab(hidden=True)
        content_layout.addWidget(self._tab_widget, 1)

        layout.addWidget(content, 1)

    def _build_pool_tab(self):
        """Tab 1: 上游 Key 池（只放账号 token / JWT）"""
        pool_tab = QWidget()
        pool_layout = QVBoxLayout(pool_tab)
        pool_layout.setSpacing(10)

        # 统计行（颜色跟随主题，apply_theme 会再刷新）
        stats_row = QHBoxLayout()
        self._stat_total = QLabel("📋 总 Key: 0")
        self._stat_total.setStyleSheet("font-size: 14px; font-weight: 700;")
        stats_row.addWidget(self._stat_total)
        self._stat_active = QLabel("✅ 活跃: 0")
        self._stat_active.setStyleSheet("font-size: 14px; font-weight: 700; color: #0A7D33;")
        stats_row.addWidget(self._stat_active)
        self._stat_disabled = QLabel("🚫 禁用: 0")
        self._stat_disabled.setStyleSheet("font-size: 14px; font-weight: 700; color: #C0271D;")
        stats_row.addWidget(self._stat_disabled)
        self._stat_points = QLabel("💰 剩余可用: 0")
        self._stat_points.setStyleSheet("font-size: 15px; font-weight: 800; color: #0A7D33;")
        self._stat_points.setToolTip("池子所有账号当前剩余可用积分之和（已扣除消耗）\n这是您现在真正能用的积分")
        stats_row.addWidget(self._stat_points)
        self._stat_used_points = QLabel("已用: 0")
        self._stat_used_points.setStyleSheet("font-size: 12px; font-weight: 600; color: #9CA3AF;")
        self._stat_used_points.setToolTip("历史累计已消耗的积分")
        stats_row.addWidget(self._stat_used_points)
        self._stat_used = QLabel("📊 总调用: 0")
        self._stat_used.setStyleSheet("font-size: 14px; font-weight: 700;")
        stats_row.addWidget(self._stat_used)
        stats_row.addStretch()

        btn_refresh = QPushButton("🔄 刷新账号列表")
        btn_refresh.setObjectName("secondary_btn")
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.clicked.connect(self._refresh_pool)
        stats_row.addWidget(btn_refresh)
        pool_layout.addLayout(stats_row)

        # 工具栏 — 卡密添加 + 查积分 + 检测 + 筛选 + 并发（账号管理功能全部合并至此）
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.setContentsMargins(0, 0, 0, 0)

        btn_card_add = QPushButton("🎫 卡密添加账号")
        btn_card_add.setObjectName("primary_btn")
        btn_card_add.setCursor(Qt.PointingHandCursor)
        btn_card_add.setToolTip("输入卡密下载账号包，自动导入账号+上游Key池+自动刷新积分")
        btn_card_add.clicked.connect(self._card_key_fetch)
        toolbar.addWidget(btn_card_add)

        btn_points = QPushButton("🔄 刷新全部积分")
        btn_points.setObjectName("secondary_btn")
        btn_points.setCursor(Qt.PointingHandCursor)
        btn_points.setToolTip("异步查询所有 token Key 的剩余积分（每个 Key 5 分钟限频一次）")
        btn_points.clicked.connect(self._refresh_all_points)
        toolbar.addWidget(btn_points)

        btn_check = QPushButton("🔍 检测账号是否可用")
        btn_check.setObjectName("secondary_btn")
        btn_check.setCursor(Qt.PointingHandCursor)
        btn_check.setToolTip("批量检测所有 token Key 是否被风控（11140），异常的自动禁用（仅本页侧）")
        btn_check.clicked.connect(self._check_all_key_status)
        toolbar.addWidget(btn_check)

        # 状态筛选下拉（账号管理的分类筛选合并至此）
        self._status_filter = QComboBox()
        self._status_filter.addItem("全部状态", "")
        self._status_filter.addItem("✅ 活跃", "active")
        self._status_filter.addItem("🚫 禁用", "disabled")
        self._status_filter.addItem("⛔ 永久禁用", "permanent_disabled")
        self._status_filter.setToolTip("按状态筛选 Key（调度消耗只走「活跃」）")
        self._status_filter.currentIndexChanged.connect(lambda _i: self._refresh_pool())
        toolbar.addWidget(self._status_filter)

        # 并发设置（纯输入无箭头，回车/失焦生效）
        toolbar.addWidget(QLabel("并发:"))
        self._concurrency_edit = QLineEdit()
        self._concurrency_edit.setText(str(load_setting("account_concurrency", "5")))
        self._concurrency_edit.setFixedWidth(48)
        self._concurrency_edit.setAlignment(Qt.AlignCenter)
        self._concurrency_edit.setToolTip("积分查询/状态检测的同时请求线程数（1-50，输入后回车生效）")
        self._concurrency_edit.setValidator(QIntValidator(1, 50, self))
        def _save_concurrency():
            v = self._concurrency_edit.text().strip()
            if v.isdigit() and 1 <= int(v) <= 50:
                save_setting("account_concurrency", v)
        self._concurrency_edit.editingFinished.connect(_save_concurrency)
        toolbar.addWidget(self._concurrency_edit)

        btn_disable = QPushButton("禁用选中账号")
        btn_disable.setObjectName("secondary_btn")
        btn_disable.setCursor(Qt.PointingHandCursor)
        btn_disable.setToolTip("按积分范围批量临时禁用 Key（仅本页侧，不影响 API 代理页）")
        btn_disable.clicked.connect(
            lambda: self._open_batch_status_dialog(enable=False, permanent=False))
        toolbar.addWidget(btn_disable)

        btn_batch_del = QPushButton("🗑️ 删除勾选")
        btn_batch_del.setObjectName("danger_btn")
        btn_batch_del.setCursor(Qt.PointingHandCursor)
        btn_batch_del.setToolTip("删除勾选的 Key（token 一并删除，不可恢复）")
        btn_batch_del.clicked.connect(self._batch_delete_checked)
        toolbar.addWidget(btn_batch_del)

        btn_enable = QPushButton("✅ 启用选中账号")
        btn_enable.setObjectName("secondary_btn")
        btn_enable.setCursor(Qt.PointingHandCursor)
        btn_enable.setToolTip("按积分范围批量恢复 Key 为可用（仅本页侧）")
        btn_enable.clicked.connect(lambda: self._open_batch_status_dialog(enable=True))
        toolbar.addWidget(btn_enable)

        # 当天/总计切换
        self._today_only = False
        self._chk_today = QPushButton("📅 当天")
        self._chk_today.setObjectName("secondary_btn")
        self._chk_today.setCheckable(True)
        self._chk_today.setCursor(Qt.PointingHandCursor)
        self._chk_today.setToolTip("开启后只显示当天统计，关闭显示总计")
        self._chk_today.clicked.connect(self._toggle_today)
        toolbar.addWidget(self._chk_today)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 搜索 Key...")
        self._search_input.textChanged.connect(lambda _t: self._refresh_pool())
        toolbar.addWidget(self._search_input)

        toolbar.addStretch()
        pool_layout.addLayout(toolbar)

        # Key 表格（☑复选+标签+状态+调用+积分+添加时间+RT到期+操作）
        self._pool_sort_column = None
        self._pool_sort_order = Qt.AscendingOrder
        self._pool_table = QTableWidget()
        self._pool_table.setColumnCount(8)
        self._pool_table.setHorizontalHeaderLabels([
            "☑", "标签", "状态", "调用", "积分", "添加时间", "RT到期", "操作"
        ])
        # 列宽策略：☑窄固定 + 操作紧凑 + 其余按权重Stretch
        pool_header = self._pool_table.horizontalHeader()
        pool_header.setSectionResizeMode(0, QHeaderView.Fixed)
        self._pool_table.setColumnWidth(0, 36)   # 复选框列固定36px
        pool_header.setSectionResizeMode(1, QHeaderView.Stretch)  # 标签占主要
        pool_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # 状态按内容
        pool_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)  # 调用按内容
        pool_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # 积分按内容
        pool_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)  # 添加时间按内容
        pool_header.setSectionResizeMode(6, QHeaderView.ResizeToContents)  # RT到期按内容
        pool_header.setSectionResizeMode(7, QHeaderView.Fixed)
        self._pool_table.setColumnWidth(7, 130)  # 操作列紧凑固定130px
        # 表头点击排序（照 API 代理页：内存重排，操作列不参与）
        pool_header.setSortIndicatorShown(True)
        pool_header.sectionClicked.connect(self._on_pool_header_sort)
        self._pool_table.setAlternatingRowColors(True)
        self._pool_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pool_table.setSelectionBehavior(QTableWidget.SelectRows)
        # 行高统一让操作按钮更清晰
        self._pool_table.verticalHeader().setDefaultSectionSize(40)
        # 右键菜单
        self._pool_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._pool_table.customContextMenuRequested.connect(self._on_pool_context_menu)
        pool_layout.addWidget(self._pool_table)

        self._tab_widget.addTab(pool_tab, "🔑 上游 Key 池")

    def _build_log_tab(self):
        """Tab 2: 使用日志（中转自己的事件流）"""
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)

        self._log_edit = QTextEdit()
        self._log_edit.setObjectName("log_edit")
        self._log_edit.setReadOnly(True)
        self._log_edit.setFont(QFont("Consolas"))
        log_layout.addWidget(self._log_edit)

        log_toolbar = QHBoxLayout()
        btn_refresh_log = QPushButton("🔄 刷新使用日志")
        btn_refresh_log.setObjectName("secondary_btn")
        btn_refresh_log.setCursor(Qt.PointingHandCursor)
        btn_refresh_log.clicked.connect(self._refresh_log)
        log_toolbar.addWidget(btn_refresh_log)

        btn_clear_log = QPushButton("🗑️ 清空日志")
        btn_clear_log.setObjectName("secondary_btn")
        btn_clear_log.setCursor(Qt.PointingHandCursor)
        btn_clear_log.clicked.connect(self._clear_log)
        log_toolbar.addWidget(btn_clear_log)

        log_toolbar.addStretch()
        log_layout.addLayout(log_toolbar)

        self._tab_widget.addTab(log_tab, "📊 使用日志")

    def _build_client_tab(self, hidden: bool = False):
        """Tab 3: 客户端接入（CodeBuddy / WorkBuddy）— hidden=True 时不挂 Tab（逻辑控件保留）"""
        client_tab = QWidget()
        client_tab.setVisible(not hidden)
        outer = QVBoxLayout(client_tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 内容超出时用滚动区承载（照 settings.py 标准模式），
        # 否则窗口不够高时 QVBoxLayout 会把标题/按钮压到 0 高
        scroll = QScrollArea()
        scroll.setObjectName("settings_scroll_area")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        client_layout = QVBoxLayout(content)
        client_layout.setContentsMargins(8, 8, 8, 8)
        client_layout.setSpacing(12)

        # CodeBuddy 客户端配置区
        cb_card = QFrame()
        cb_card.setObjectName("card")
        cb_layout = QVBoxLayout(cb_card)
        cb_layout.setSpacing(8)

        cb_layout.addWidget(QLabel("CodeBuddy 客户端配置:"))
        self._client_label = QLabel("检测中…")
        self._client_label.setStyleSheet("font-size: 12px;")
        self._client_label.setWordWrap(True)
        cb_layout.addWidget(self._client_label)

        cb_btn_row = QHBoxLayout()
        self._devmode_btn = QPushButton("🔧 开启开发者模式")
        self._devmode_btn.setObjectName("secondary_btn")
        self._devmode_btn.setCursor(Qt.PointingHandCursor)
        self._devmode_btn.setToolTip(
            "自定义端点的前置条件，一次性操作。需先完全退出 CodeBuddy。")
        self._devmode_btn.clicked.connect(self._enable_devmode)
        cb_btn_row.addWidget(self._devmode_btn)

        btn_recheck = QPushButton("🔄 重新检测")
        btn_recheck.setObjectName("secondary_btn")
        btn_recheck.setCursor(Qt.PointingHandCursor)
        btn_recheck.clicked.connect(self._refresh_client_status)
        cb_btn_row.addWidget(btn_recheck)
        cb_btn_row.addStretch()
        cb_layout.addLayout(cb_btn_row)

        client_layout.addWidget(cb_card)

        # WorkBuddy 客户端配置区
        wb_card = QFrame()
        wb_card.setObjectName("card")
        wb_layout = QVBoxLayout(wb_card)
        wb_layout.setSpacing(8)

        wb_layout.addWidget(QLabel("WorkBuddy 客户端配置:"))
        self._wb_label = QLabel("检测中…")
        self._wb_label.setStyleSheet("font-size: 12px;")
        self._wb_label.setWordWrap(True)
        wb_layout.addWidget(self._wb_label)

        wb_btn_row = QHBoxLayout()
        self._wb_btn = QPushButton("🔗 接入 WorkBuddy 客户端")
        self._wb_btn.setObjectName("secondary_btn")
        self._wb_btn.setCursor(Qt.PointingHandCursor)
        self._wb_btn.setToolTip(
            "写入 ~/.workbuddy/settings.json 的 env.CODEBUDDY_BASE_URL 指向本地中转。\n"
            "WorkBuddy 新会话即走中转（无需重启，无需开发者模式）。")
        self._wb_btn.clicked.connect(self._toggle_workbuddy)
        wb_btn_row.addWidget(self._wb_btn)
        wb_btn_row.addStretch()
        wb_layout.addLayout(wb_btn_row)

        client_layout.addWidget(wb_card)
        client_layout.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll)
        if not hidden:
            self._tab_widget.addTab(client_tab, "🤖 客户端接入")

    # ═══════════ 服务控制 ═══════════

    def _relay_host(self) -> str:
        """监听模式已固定本地，不开放外网"""
        return "127.0.0.1"

    def _display_url(self) -> str:
        """当前应展示/复制的中转地址（固定本地）"""
        return f"http://127.0.0.1:{self._port_spin.value()}"

    def _on_listen_mode_changed(self, _index: int):
        """监听模式已移除，保留空实现防外部调用报错"""
        pass

    @staticmethod
    def _get_local_ips() -> list:
        """获取本机所有非回环 IP 地址（照 API 代理页）"""
        import socket
        ips = []
        try:
            hostname = socket.gethostname()
            for ip in socket.getaddrinfo(hostname, None):
                addr = ip[4][0]
                if isinstance(addr, str) and addr != "127.0.0.1" and not addr.startswith("169.254.") and ":" not in addr:
                    if addr not in ips:
                        ips.append(addr)
        except Exception:
            pass
        if not ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
                if ip != "127.0.0.1":
                    ips.append(ip)
            except Exception:
                pass
        return ips

    def _autostart_relay(self):
        """按持久化标记自动恢复中转服务与客户端配置（静默）"""
        if load_setting("codebuddy_relay_enabled", "0") != "1":
            return
        if self._relay_server and self._relay_server.is_running:
            return
        port = int(load_setting("codebuddy_relay_port", "8003") or "8003")
        server = CodeBuddyRelayServer(host=self._relay_host(), port=port)
        if not server.start():
            return
        self._relay_server = server
        # 客户端未安装时静默跳过（不弹窗不打错误日志）
        if is_codebuddy_installed():
            apply_client_config(port)
        # WorkBuddy 默认自动接入（用户 08-09 要求：开服务即接入，可手动断开）
        if is_workbuddy_installed():
            ok, _msg = apply_workbuddy_config(port)
            if ok:
                save_setting("codebuddy_relay_wb_enabled", "1")
        self._refresh_status()

    def _toggle_service(self):
        """启动/停止无感换号中转"""
        if self._relay_server and self._relay_server.is_running:
            self._relay_server.stop()
            self._relay_server = None
            save_setting("codebuddy_relay_enabled", "0")
            save_setting("codebuddy_relay_wb_enabled", "0")
            self._toggle_btn.setText("⚡ 启动接入（消耗卡密账号）")
            self._toggle_btn.setStyleSheet("""
                QPushButton {
                    background: #FFFFFF; color: #C0271D; border: 2px solid #C0271D;
                    border-radius: 14px; font-size: 20px; font-weight: 800;
                    padding: 14px 32px;
                }
                QPushButton:hover { background: #F9E8E7; }
                QPushButton:pressed { background: #F2D7D5; }
            """)
            self._status_label.setText("⏹ 接入服务未开启")
            self._status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #9CA3AF;")
            self._url_label.setVisible(False)
            # 客户端没装就跳过还原（没什么可还原的，也不弹窗）
            if is_codebuddy_installed():
                ok, msg = restore_client_config()
                if not ok:
                    QMessageBox.warning(self, "还原配置失败", msg)
            # WorkBuddy 如指向本地中转也一并还原，避免打到已关闭的端口
            if is_workbuddy_installed():
                ok, msg = restore_workbuddy_config()
                if not ok:
                    QMessageBox.warning(self, "还原 WorkBuddy 配置失败", msg)
            self._refresh_status()
            return

        port = self._port_spin.value()
        self._relay_server = CodeBuddyRelayServer(host=self._relay_host(), port=port)
        if not self._relay_server.start():
            self._relay_server = None
            QMessageBox.warning(self, "启动失败", f"无法在端口 {port} 启动中转服务，可能端口已被占用")
            return

        save_setting("codebuddy_relay_port", str(port))
        save_setting("codebuddy_relay_enabled", "1")
        self._toggle_btn.setText("⏹ 停止接入（消耗自己账号）")
        self._toggle_btn.setStyleSheet("""
            QPushButton {
                background: #C0271D; color: #FFFFFF; border: 2px solid #C0271D;
                border-radius: 14px; font-size: 20px; font-weight: 800;
                padding: 14px 32px;
            }
            QPushButton:hover { background: #A01F17; }
        """)
        self._status_label.setText("✅ 接入服务已开启")
        self._status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #10B981;")
        self._url_label.setText(f"中转地址: http://127.0.0.1:{port}")
        self._url_label.setVisible(True)

        # 自动接入 WorkBuddy（无需重启，直接写配置）
        if is_workbuddy_installed():
            ok, msg = apply_workbuddy_config(port)
            if ok:
                save_setting("codebuddy_relay_wb_enabled", "1")
            else:
                QMessageBox.warning(self, "WorkBuddy 接入失败", msg)

        # 自动接入 CodeBuddy（需要开发者模式 + 重启生效）
        if is_codebuddy_installed():
            ok, msg = apply_client_config(port)
            if not ok:
                QMessageBox.warning(self, "CodeBuddy 配置失败", msg)
            else:
                # 检查开发者模式
                if not is_dev_mode_enabled():
                    # 尝试自动开启
                    if not is_codebuddy_running():
                        ok2, msg2 = enable_dev_mode()
                        if not ok2:
                            QMessageBox.warning(self, "开发者模式", 
                                f"自动开启开发者模式失败：{msg2}\n\n"
                                f"请手动完全关闭 CodeBuddy，然后重新点击一键接入。")
                        else:
                            QMessageBox.information(self, "需要重启 CodeBuddy",
                                "一键接入已配置完成！\n\n"
                                "CodeBuddy 的开发者模式已自动开启，\n"
                                "请重启 CodeBuddy 客户端使其生效。")
                    else:
                        QMessageBox.information(self, "需要重启 CodeBuddy",
                            "一键接入已配置完成！\n\n"
                            "但 CodeBuddy 正在运行，开发者模式需要先关闭 CodeBuddy 才能开启。\n"
                            "请完全关闭 CodeBuddy，然后重新点击一键接入。\n"
                            "（开发者模式只需开启一次，永久生效）")
        self._refresh_status()

    def _enable_devmode(self):
        """手动开启 CodeBuddy 开发者模式"""
        if is_dev_mode_enabled():
            QMessageBox.information(self, "开发者模式", "开发者模式已开启，无需重复操作。")
            return
        ok, msg = enable_dev_mode()
        if ok:
            QMessageBox.information(self, "开发者模式", msg + "\n现在可以启动 CodeBuddy 了。")
        else:
            QMessageBox.warning(self, "开启开发者模式失败", msg)
        self._refresh_client_status()

    def _toggle_workbuddy(self):
        """接入/还原 WorkBuddy 的 CLI 端点配置（静默，不弹窗）"""
        port = self._port_spin.value()
        running = self._relay_server and self._relay_server.is_running
        state = get_workbuddy_config_state(port)
        if state["pointed_to_us"]:
            ok, msg = restore_workbuddy_config()
            if not ok:
                QMessageBox.warning(self, "还原失败", msg)
            else:
                save_setting("codebuddy_relay_wb_enabled", "0")
        else:
            if not running:
                QMessageBox.warning(self, "中转未开启", "请先点上方「▶ 启动服务」启动中转服务。")
                return
            ok, msg = apply_workbuddy_config(port)
            if ok:
                save_setting("codebuddy_relay_wb_enabled", "1")
            else:
                QMessageBox.warning(self, "接入失败", msg)
        self._refresh_client_status()

    def _copy_url(self):
        QApplication.clipboard().setText(self._display_url())

    # ═══════════ 状态刷新 ═══════════

    def _on_tab_changed(self, idx: int):
        """Tab切换时立即刷新当前页（不等2秒定时器）"""
        if idx == 0:
            self._refresh_pool()
        elif idx == 1:
            self._refresh_log()

    def _on_timer(self):
        """2 秒定时：状态 + Key 池 + 日志（不管在哪个Tab都刷新日志，保证实时）"""
        self._refresh_status()
        self._refresh_client_status()
        idx = self._tab_widget.currentIndex()
        if idx == 0:
            self._refresh_pool()
        self._refresh_log()  # 日志始终刷新（轮询relay的deque，轻量操作）

    def apply_theme(self):
        """主题切换时刷新页面内硬编码颜色（跟随极简黑白主题）"""
        try:
            from ..theme import resolve_colors
            c = resolve_colors()
            # Key 池统计标签
            if hasattr(self, '_stat_total'):
                self._stat_total.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {c['text']};")
            if hasattr(self, '_stat_active'):
                self._stat_active.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {c['ok']};")
            if hasattr(self, '_stat_disabled'):
                self._stat_disabled.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {c['err']};")
            if hasattr(self, '_stat_points'):
                self._stat_points.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {c['text2']};")
            if hasattr(self, '_stat_used'):
                self._stat_used.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {c['text2']};")
            # 日志区
            if hasattr(self, '_log_edit'):
                self._log_edit.setStyleSheet(f"background: {c['bg2']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 10px; padding: 8px;")
        except Exception:
            pass

    def _fresh_points(self, cur: dict) -> str:
        """当前消耗 Key 的积分从 DB 实时取（_set_current_key 里是选 Key 时的旧快照）"""
        fallback = cur.get("points") or "?"
        kid = cur.get("key_id", "")
        if not kid:
            return fallback
        for k in self._jwt_keys():
            if k.get("key_id") == kid:
                return k.get("points") or fallback
        return fallback

    def _refresh_status(self):
        """刷新中转运行状态（大按钮样式保持红色主题，不随定时器被覆盖）"""
        running = self._relay_server and self._relay_server.is_running

        if running:
            st = self._relay_server.get_status()
            cur = st.get("current_key") or {}
            self._status_label.setText("✅ 接入服务已开启")
            self._status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #0A7D33;")
            self._port_spin.setEnabled(False)
            if cur:
                points = self._fresh_points(cur)
                consume = f"当前消耗: {cur.get('label', '-')}（剩余 {points} 分）"
            else:
                consume = "当前消耗: -（等待客户端发起对话）"
            self._stat_used.setToolTip(
                f"{consume}｜累计请求 {st['total_requests']} 次"
                f"（换号 {st['swapped_requests']} 次）｜最近: {st['last_event'] or '-'}")
        else:
            self._status_label.setText("⏹ 接入服务未开启")
            self._status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #8A8A8E;")
            self._port_spin.setEnabled(True)
            self._url_label.setText(self._display_url())

        # 控件样式重载
        self._toggle_btn.style().unpolish(self._toggle_btn)
        self._toggle_btn.style().polish(self._toggle_btn)

    def _refresh_client_status(self):
        """刷新两个客户端的配置状态"""
        port = self._port_spin.value()
        try:
            cfg = get_client_config_state(port)
            endpoint_txt = cfg["endpoint"] or "官方默认"
            endpoint_ok = "✅" if cfg["pointed_to_us"] else "⚠️"
            devmode_txt = "✅ 已开启" if cfg["dev_mode"] else "❌ 未开启（点下方按钮，需先退出 CodeBuddy）"
            _set_multiline_text(self._client_label,
                f"{endpoint_ok} 当前端点: {endpoint_txt}\n"
                f"开发者模式: {devmode_txt}"
            )
            self._devmode_btn.setVisible(not cfg["dev_mode"])
        except Exception:
            self._client_label.setText("配置状态检测失败")

        try:
            wb = get_workbuddy_config_state(port)
            # WorkBuddy 桌面端保存自身设置时会丢掉它不认识的 env 键（实测被覆盖过），
            # 中转运行中且标记为已接入时发现漂移就静默补回
            if not wb["pointed_to_us"] and \
                    self._relay_server and self._relay_server.is_running and \
                    load_setting("codebuddy_relay_wb_enabled", "0") == "1":
                apply_workbuddy_config(port)
                wb = get_workbuddy_config_state(port)
            wb_url = wb["base_url"] or "官方默认"
            wb_ok = "✅" if wb["pointed_to_us"] else "⚠️"
            _set_multiline_text(self._wb_label, f"{wb_ok} 当前端点: {wb_url}")
            self._wb_btn.setText(
                "🔌 断开 WorkBuddy" if wb["pointed_to_us"] else "🔗 接入 WorkBuddy")
        except Exception:
            self._wb_label.setText("配置状态检测失败")

    # ═══════════ 上游 Key 池（仅 JWT，状态独立）═══════════

    def _jwt_keys(self) -> list:
        """池子里所有账号 token Key（JWT）"""
        return [
            k for k in self._db.get_upstream_keys()
            if k.get("api_key", "").startswith("eyJ")
        ]

    @staticmethod
    def _relay_state_of(key: dict) -> tuple:
        """(状态文本, 状态码)：active / cooldown / disabled / permanent_disabled

        只看本页侧 relay_* 字段——本页是独立池子，与 API 代理页状态互不相通，
        显示什么状态，批量操作就认什么状态。
        """
        relay_status = key.get("relay_status", "active")
        if relay_status == "permanent_disabled":
            return "⛔ 永久禁用", "permanent_disabled"
        if relay_status != "active":
            return "🚫 已禁用", "disabled"
        remain = float(key.get("relay_cooldown_until") or 0) - time.time()
        if remain > 0:
            return f"🧊 冷却中({int(remain)}s)", "cooldown"
        return "✅ 活跃", "active"

    def _toggle_today(self):
        """切换 Key 池表格的当天/总计显示"""
        self._today_only = self._chk_today.isChecked()
        self._chk_today.setText("📅 当天✓" if self._today_only else "📅 当天")
        self._refresh_pool()

    def _apply_thresholds_now(self, *_args):
        """最低积分/自动启用变更：持久化 + 下次 _refresh_pool 生效（那里只在状态需要变迁时才写库）"""
        if not hasattr(self, "_pool_table"):
            return  # UI 构建期间 setValue 触发的信号，忽略
        save_setting("hotswitch_min_credits", str(self._min_credits_spin.value()))
        save_setting("hotswitch_auto_enable", str(self._auto_enable_spin.value()))
        self._refresh_pool()

    def _apply_point_thresholds(self, k: dict):
        """按存量积分对本页侧 relay_* 状态执行 最低积分/自动启用 规则。

        只在状态需要变迁时写库（_refresh_pool 每 2s 跑一次，不能无脑写）。
        永禁 / 检测禁用 / 无积分数据的一律不动；自动恢复的仅限「积分不足自动禁用」的。
        """
        relay_status = k.get("relay_status", "active")
        if relay_status == "permanent_disabled":
            return
        note = str(k.get("relay_note", ""))
        if note.startswith("检测:"):
            return  # 风控禁用交给「检测」按钮管理
        pts = ApiProxyPage._points_remaining(k.get("points", ""))
        if pts < 0:
            return  # 无积分数据

        min_val = self._min_credits_spin.value()
        auto_val = self._auto_enable_spin.value()
        key_id = k.get("key_id", "")
        in_cooldown = float(k.get("relay_cooldown_until") or 0) > time.time()

        # 积分 <= min → 自动禁用（冷却中的跳过，等冷却结束后再判）
        if min_val > 0 and pts <= min_val and relay_status == "active" and not in_cooldown:
            self._db.update_upstream_key(key_id, {
                "relay_status": "disabled",
                "relay_note": f"积分不足({pts:.0f}<={min_val})，中转侧自动禁用",
            })
            k["relay_status"] = "disabled"
            k["relay_note"] = f"积分不足({pts:.0f}<={min_val})，中转侧自动禁用"
        # 积分 > auto → 只恢复「积分不足自动禁用」的，手动/风控禁用的不动
        elif auto_val > 0 and pts > auto_val and relay_status == "disabled" \
                and note.startswith("积分不足"):
            self._db.update_upstream_key(key_id, {
                "relay_status": "active",
                "relay_note": "",
            })
            k["relay_status"] = "active"
            k["relay_note"] = ""

    def _close_menus(self):
        """刷新表格前关闭并释放所有持有菜单。

        崩溃根因（2026-08-11 崩溃报告 SIGSEGV @ QMenu::mouseReleaseEvent）：
        「操作 ▾」菜单弹出后，2s 定时器 _refresh_pool 重建表格行 → 弹出中的 QMenu
        被 Qt 删除 → 用户点击时事件处理到已释放对象。先 close 再重建可避免。
        """
        for m in self._open_menus:
            try:
                m.close()
            except Exception:
                pass
        self._open_menus = []

    def _on_pool_context_menu(self, pos):
        """上游 Key 池右键菜单：使用/积分明细/查询积分/禁用/恢复/永久禁用/删除"""
        row = self._pool_table.rowAt(pos.y())
        if row < 0:
            return
        # 第0列现在是复选框，key_id藏在tooltip里
        chk = self._pool_table.item(row, 0)
        if not chk or "Key: " not in (chk.toolTip() or ""):
            return
        key_id = chk.toolTip().split("Key: ", 1)[1].strip()
        keys = self._jwt_keys()
        k = next((kk for kk in keys if kk.get("key_id") == key_id), {})
        if not k:
            return
        _, state = self._relay_state_of(k)
        label = k.get("label", key_id[:8])

        running = bool(self._relay_server and self._relay_server.is_running)
        is_cur = False
        if running:
            try:
                cur = self._relay_server.get_status().get("current_key") or {}
                is_cur = cur.get("key_id", "") == key_id
            except Exception:
                pass

        menu = QMenu(self)
        _style_popup_menu(menu)

        act_use = menu.addAction("✅ 使用此账号")
        act_use.setToolTip("下一个请求切换到该账号（不可用时自动换别的）")
        act_use.setEnabled(running)
        act_use.triggered.connect(lambda checked, kid=key_id: self._use_key(kid))
        if is_cur:
            act_use.setText("🟢 使用中")
            act_use.setEnabled(False)

        menu.addSeparator()
        act = menu.addAction("📊 积分明细")
        act.setToolTip("查看该账号的积分包明细（各包剩余/总量/过期时间）")
        act.triggered.connect(lambda checked, kid=key_id: self._show_key_credits_detail(kid))
        act = menu.addAction("💎 查询积分")
        act.setToolTip("立即查询该账号最新剩余积分")
        act.triggered.connect(lambda checked, kid=key_id: self._query_single_key_points(kid))

        menu.addSeparator()
        if state != "active":
            act = menu.addAction("✅ 恢复")
            act.triggered.connect(
                lambda checked, kid=key_id: self._set_key_relay_status(kid, "active"))
        else:
            act = menu.addAction("🚫 禁用")
            act.triggered.connect(
                lambda checked, kid=key_id: self._set_key_relay_status(kid, "disabled"))
        if state != "permanent_disabled":
            act = menu.addAction("⛔ 永久禁用")
            act.triggered.connect(
                lambda checked, kid=key_id: self._permanent_disable_key(kid))
        menu.addSeparator()
        act = menu.addAction("🗑 删除")
        act.setToolTip("从池子中彻底移除该 Key（token 一并删除，不可恢复）")
        act.triggered.connect(
            lambda checked, kid=key_id: self._delete_key(kid))

        menu.exec(self._pool_table.viewport().mapToGlobal(pos))

    def _show_key_credits_detail(self, key_id: str):
        """积分明细：用Key池里的token直接查get_user_resource（不依赖accounts表）"""
        for k in self._jwt_keys():
            if k.get("key_id") == key_id:
                token = k.get("api_key", "")
                if not token:
                    QMessageBox.warning(self, "无Token", "该Key没有api_key字段")
                    return
                # 反查uid（JWT查分需要X-User-Id）
                uid = ""
                try:
                    from ...utils.store import find_account_by_token
                    acc = find_account_by_token(token)
                    if acc:
                        uid = acc.uid
                except Exception:
                    pass

                self._show_key_credits_detail_async(key_id, token, uid)
                return

    def _show_key_credits_detail_async(self, key_id: str, token: str, uid: str):
        """后台查积分+弹明细（packages是ResourcePackage对象列表，按属性访问）"""
        def _done(result: dict, kid=key_id):
            try:
                if not result.get("success"):
                    QMessageBox.warning(self, "查询失败", result.get("error", "未知错误"))
                    return
                packages = result.get("packages", [])
                credits = result.get("remaining_credits", 0)
                total = result.get("total_credits", 0)
                # 弹窗
                from PySide6.QtWidgets import QDialog as _QDialog, QVBoxLayout as _QVBox, QHBoxLayout as _QHBox
                m = _QDialog(self)
                m.setWindowTitle("📊 积分明细")
                m.resize(560, 400)
                m.setStyleSheet("QDialog{background:#FFFFFF;}QLabel{color:#1A1A1A;font-size:13px;}QTextEdit{background:#F9FAFB;border:1px solid #E4E7EB;border-radius:8px;padding:12px;font-family:Consolas,monospace;font-size:13px;}")
                v = _QVBox(m)
                v.setContentsMargins(20, 20, 20, 20)
                v.setSpacing(12)
                # 标题：账号标签
                label_txt = kid[:14]
                for kk in self._jwt_keys():
                    if kk.get("key_id") == kid:
                        label_txt = kk.get("label") or kid[:14]
                        break
                summary = QLabel(f"账号 <b>{label_txt}</b> · 总剩余 <b style='color:#0A7D33;font-size:18px'>{credits:.0f}</b> / <b>{total:.0f}</b> · {len(packages)} 个积分包")
                v.addWidget(summary)
                edit = QTextEdit()
                edit.setReadOnly(True)
                if not packages:
                    txt = "（该账号暂未返回积分包明细）\n"
                else:
                    txt = f"{'积分包':<22} {'剩余/总量':<12} {'已用%':<7} {'周期结束':<20}\n"
                    txt += "-" * 70 + "\n"
                    for p in packages:
                        # ResourcePackage对象：属性访问（不是dict）
                        try:
                            name = (getattr(p, 'package_name', '') or getattr(p, 'product_name', '') or '-')[:20]
                            rem = float(getattr(p, 'capacity_remain', 0) or 0)
                            tot = float(getattr(p, 'capacity_size', 0) or 0)
                            used_pct = float(getattr(p, 'usage_percentage', 0) or 0)
                            cyc_end = (getattr(p, 'cycle_end', '') or '-')[:19]
                            tlabel = getattr(p, 'type_label', '') or ''
                            txt += f"{name:<22} {rem:>6.0f}/{tot:<5.0f} {used_pct:>5.1f}%  {cyc_end:<20}\n"
                        except Exception as pe:
                            txt += f"(包解析失败: {pe})\n"
                edit.setPlainText(txt)
                v.addWidget(edit, 1)
                btn_row = _QHBox()
                btn_close = QPushButton("关闭")
                btn_close.setObjectName("secondary_btn")
                btn_close.clicked.connect(m.accept)
                btn_row.addStretch()
                btn_row.addWidget(btn_close)
                v.addLayout(btn_row)
                m.exec()
            except Exception as e:
                logger.exception("积分明细弹窗失败")
                QMessageBox.critical(self, "错误", f"积分明细弹窗失败：{e}")

        from PySide6.QtCore import QThread, Signal as QSignal
        class _QThread(QThread):
            done = QSignal(dict)
            def run(self):
                try:
                    from ...modules.api_client import ApiClient
                    client = ApiClient(access_token=token, uid=uid, domain="www.codebuddy.cn")
                    r = client.get_user_resource()
                    self.done.emit(r)
                except Exception as e:
                    self.done.emit({"success": False, "error": str(e)})
        t = _QThread()
        t.done.connect(_done)
        t.start()
        self._credits_detail_thread = t

    def _delete_key(self, key_id: str):
        """右键「删除」：从池子彻底移除该 Key（token 一并删除，不可恢复）"""
        ret = QMessageBox.question(
            self, "确认删除",
            f"确定要从池子中彻底删除 Key {key_id} 吗？\n\n"
            "删除后该账号的 token 与使用记录一并移除，不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self._db.delete_upstream_key(key_id)
        # 若删除的正是当前消耗中的 Key：relay 侧 current 指向已删 Key，
        # _select_relay_key 匹配不到会自然选别的号（无需额外处理）
        self._refresh_pool()

    def _batch_delete_checked(self):
        """批量删除勾选的 Key（复选框列打勾的行）"""
        checked_ids = []
        for row in range(self._pool_table.rowCount()):
            item = self._pool_table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                tip = item.toolTip() or ""
                if "Key: " in tip:
                    checked_ids.append(tip.split("Key: ", 1)[1].strip())
        if not checked_ids:
            QMessageBox.information(self, "提示", "请先勾选要删除的账号（第一列复选框）")
            return
        ret = QMessageBox.question(
            self, "确认批量删除",
            f"确定要删除勾选的 {len(checked_ids)} 个 Key 吗？\n\n"
            "删除后这些账号的 token 与使用记录一并移除，不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        for kid in checked_ids:
            self._db.delete_upstream_key(kid)
        self._refresh_pool()
        QMessageBox.information(self, "完成", f"已删除 {len(checked_ids)} 个 Key")

    def _use_key(self, key_id: str):
        """右键「使用此账号」：强制切换当前消耗的 Key"""
        if not (self._relay_server and self._relay_server.is_running):
            QMessageBox.information(self, "提示", "请先启动无感换号中转服务")
            return
        if self._relay_server.set_forced_key(key_id):
            QMessageBox.information(
                self, "已切换", "下一个请求将使用该账号（若不可用会自动换别的）")
            self._refresh_pool()
        else:
            QMessageBox.warning(self, "切换失败", "未找到该 Key")

    def _on_pool_header_sort(self, section: int):
        """表头点击排序（照 API 代理页：内存重排，操作列不参与）。

        交互：第一次点击正序，第二次倒序，第三次取消排序恢复默认。
        """
        if section >= self._pool_table.columnCount() - 1:
            return
        header = self._pool_table.horizontalHeader()
        if self._pool_sort_column == section and self._pool_sort_order == Qt.DescendingOrder:
            # 第三次点击：取消排序，恢复默认（最近使用时间）
            self._pool_sort_column = None
            self._pool_sort_order = Qt.AscendingOrder
            header.setSortIndicator(-1, Qt.AscendingOrder)
            self._refresh_pool()
            return
        if self._pool_sort_column == section:
            self._pool_sort_order = Qt.DescendingOrder
        else:
            self._pool_sort_column = section
            self._pool_sort_order = Qt.AscendingOrder
        header.setSortIndicator(section, self._pool_sort_order)
        self._refresh_pool()

    def _pool_points_num(self, k: dict) -> float:
        """Key 剩余积分转数值（照 API 代理 _points_remaining：支持 123/500 取前段），
        解析失败返回 -1（排最后/不参与求和）"""
        text = str(k.get("points", "") or "").strip()
        if not text:
            return -1
        try:
            if "/" in text:
                return float(text.split("/", 1)[0])
            return float(text)
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _key_added_at_text(k: dict) -> str:
        """Key添加时间（created_at → 显示 MM-DD HH:MM）"""
        text = str(k.get("created_at", "") or "").strip()
        if not text:
            return "-"
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(text)
            return dt.strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            # 部分老key的created_at为空字符串或异常格式
            return "-"

    @staticmethod
    def _key_rt_expiry(k: dict):
        """从RT(JWT)解析到期时间。RT是离线token（两段或三段），exp在payload。
        返回 (显示文本, 到期天数float)；无RT/解析失败返回 ("-", None)"""
        import base64 as _b64
        rt = str(k.get("api_key", "") or "").strip()
        if not rt.startswith("eyJ"):
            return ("-", None)
        try:
            parts = rt.split(".")
            if len(parts) < 2:
                return ("-", None)
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            data = __import__("json").loads(_b64.urlsafe_b64decode(payload))
            exp = data.get("exp") or data.get("expires") or data.get("refresh_expires_in")
            if not exp:
                return ("-", None)
            import time as _time
            from datetime import datetime as _dt
            exp_ts = float(exp) if not isinstance(exp, (int, float)) else float(exp)
            # exp可能是相对秒数（如1209600）而不是绝对时间戳——根据量级判断
            if exp_ts < 10**11:  # 小于~1973年，是相对秒
                exp_ts += _time.time()
            days_left = (exp_ts - _time.time()) / 86400
            dt = _dt.fromtimestamp(exp_ts)
            disp = dt.strftime("%m-%d")
            if days_left < 0:
                return (f"⚠️{disp}", days_left)
            if days_left < 7:
                return (f"🔴{days_left:.0f}天", days_left)
            return (f"{days_left:.0f}天", days_left)
        except Exception:
            return ("-", None)

    def _pool_sort_value(self, k: dict, column: int):
        # 列布局：0=☑ 1=标签 2=状态 3=调用次数 4=积分 5=添加时间 6=RT到期 7=操作
        if column == 1:
            return k.get("label", "").lower()
        if column == 2:
            return k.get("relay_status", "active")
        if column == 3:
            return int(k.get("relay_used", 0) or 0)
        if column == 4:
            return self._pool_points_num(k)
        if column == 5:
            return str(k.get("created_at", "") or "")
        if column == 6:
            _, days = self._key_rt_expiry(k)
            return days if days is not None else -1
        return ""

    def _refresh_pool(self):
        keys = self._jwt_keys()

        # 最低积分/自动启用规则（只在状态需要变迁时写库）
        if hasattr(self, "_min_credits_spin"):
            for k in keys:
                try:
                    self._apply_point_thresholds(k)
                except Exception:
                    pass

        # 状态筛选（账号管理的分类筛选合并至此）
        if hasattr(self, "_status_filter"):
            fstate = self._status_filter.currentData() or ""
            if fstate:
                keys = [k for k in keys if k.get("relay_status", "active") == fstate]

        search = self._search_input.text().strip().lower()
        if search:
            keys = [
                k for k in keys
                if search in k.get("key_id", "").lower()
                or search in k.get("label", "").lower()
                or search in str(k.get("points", "")).lower()
            ]

        # 正在中转请求里使用的 Key（relay 侧 inflight 跟踪）
        inflight = {}
        if self._relay_server and self._relay_server.is_running:
            try:
                inflight = self._relay_server.get_status().get("inflight", {}) or {}
            except Exception:
                inflight = {}

        # 排序：用户点了表头就按该列排；未排序时按最近使用时间
        if self._pool_sort_column is not None:
            keys.sort(
                key=lambda k: self._pool_sort_value(k, self._pool_sort_column),
                reverse=self._pool_sort_order == Qt.DescendingOrder)
        else:
            keys.sort(key=lambda k: k.get("last_used_at", ""), reverse=True)
        # 使用中的 Key 置顶（不破坏列内顺序）
        if inflight:
            keys.sort(key=lambda k: 0 if k.get("key_id", "") in inflight else 1)

        # 刷新前关闭弹出中的菜单，避免重建删掉正在处理点击的 QMenu（SIGSEGV 根因）
        self._close_menus()
        self._pool_table.setRowCount(len(keys))
        active = 0
        disabled = 0
        total_used = 0
        total_points = 0.0
        points_counted = 0

        for row, k in enumerate(keys):
            key_id = k.get("key_id", "")
            label = k.get("label", "") or "-"
            state_text, state = self._relay_state_of(k)
            points = k.get("points", "-")

            # 统计数据（当天或总计）— 调用次数/Token 都跟随切换
            if self._today_only:
                today = self._db.get_today_stats("relay", key_id)
                used = today.get("count", 0)
                total_prompt = today.get("prompt_tokens", 0)
                total_completion = today.get("completion_tokens", 0)
                total_t = today.get("total_tokens", 0)
                total_cached = today.get("cached_tokens", 0)
            else:
                used = int(k.get("relay_used", 0) or 0)
                total_prompt = int(k.get("relay_prompt_tokens", 0) or 0)
                total_completion = int(k.get("relay_completion_tokens", 0) or 0)
                total_t = int(k.get("relay_total_tokens", 0) or 0)
                total_cached = int(k.get("relay_cached_tokens", 0) or 0)

            if state in ("active", "cooldown"):
                active += 1
            else:
                disabled += 1
            total_used += used

            # 总积分：只累加能解析出数值的（points 为 "-"/空 不计）
            pts_num = self._pool_points_num(k)
            if pts_num >= 0:
                total_points += pts_num
                points_counted += 1

            # ☑ 复选框列（批量删除/批量操作用）
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk_item.setCheckState(Qt.Unchecked)
            chk_item.setToolTip(f"Key: {key_id}")
            self._pool_table.setItem(row, 0, chk_item)

            # 标签列（KeyID藏进悬停提示，闭源不显示明文）
            _set_item(self._pool_table, row, 1, label, tooltip=f"标签: {label}\nKey: {key_id}")

            # 正在使用的 Key 状态文字加并发数标记 + 整行绿色背景（照 API 代理页）
            if key_id in inflight:
                state_text = f"🟢 使用中({inflight[key_id]})"
            # 禁用原因（relay_note）放进状态列悬停提示，不单独占列
            note = str(k.get("relay_note", "") or "")
            state_tip = f"状态: {state}" + (
                f"，并发: {inflight[key_id]}" if key_id in inflight else "") + (
                f"\n原因: {note}" if note else "")
            state_item = _set_item(self._pool_table, row, 2, state_text, tooltip=state_tip)
            if key_id in inflight:
                green_bg = QBrush(QColor(200, 255, 200))  # 浅绿色
                for col in range(self._pool_table.columnCount()):
                    item = self._pool_table.item(row, col)
                    if item:
                        item.setBackground(green_bg)
            elif state == "active":
                state_item.setForeground(Qt.darkGreen)
            elif state in ("disabled", "permanent_disabled"):
                state_item.setForeground(Qt.red)

            _set_item(self._pool_table, row, 3, str(used), tooltip=f"调用次数: {used:,}")

            # 积分列：剩余（主显，绿）+ 已用（灰小字）——客户一眼看到自己还能用多少
            pts_parts = str(points)
            used_disp = ""
            if "/" in pts_parts:
                try:
                    rem_s, tot_s = pts_parts.split("/", 1)
                    rem, tot = float(rem_s), float(tot_s)
                    if tot > 0 and rem < tot:
                        used_disp = f"已用{tot-rem:.0f}"
                    pts_parts = f"{rem:.0f}"  # 只显示剩余（总量在悬停里）
                except (ValueError, ZeroDivisionError):
                    pass
            if used_disp:
                pts_parts = f"{pts_parts}  {used_disp}"
            points_tip = f"剩余可用: {pts_parts.split('  ')[0] if '  ' in pts_parts else pts_parts}"
            if "/" in str(points):
                points_tip += f"\n原始: {points}（剩余/总量）"
            if total_t > 0:
                points_tip += f"\n输入: {total_prompt:,}  输出: {total_completion:,}  总计: {total_t:,}"
                if total_cached > 0:
                    points_tip += f"\n缓存命中: {total_cached:,} = {total_cached/total_t*100:.1f}%"
            pts_item = _set_item(self._pool_table, row, 4, pts_parts, tooltip=points_tip)
            # 剩余<100红显（即将耗尽），<500橙显
            try:
                rem_num = float(pts_parts.split("  ")[0].replace(",", "")) if pts_parts not in ("-", "") else -1
                if 0 <= rem_num < 100:
                    pts_item.setForeground(QBrush(QColor(0xC0, 0x27, 0x1D)))
                elif 0 <= rem_num < 500:
                    pts_item.setForeground(QBrush(QColor(0xD6, 0x9E, 0x2E)))
                else:
                    pts_item.setForeground(QBrush(QColor(0x0A, 0x7D, 0x33)))
            except (ValueError, IndexError):
                pass

            # 添加时间列（账号何时导入Key池）
            added_txt = self._key_added_at_text(k)
            added_tip = f"添加时间: {k.get('created_at', '-')}"
            added_item = _set_item(self._pool_table, row, 5, added_txt, tooltip=added_tip)
            added_item.setForeground(QBrush(QColor(0x6B, 0x72, 0x80)))

            # RT到期列（剩余天数：<7天红显，<0警示）
            rt_txt, rt_days = self._key_rt_expiry(k)
            rt_tip = "RT(刷新令牌)到期时间\n"
            if rt_days is not None:
                rt_tip += f"剩余: {rt_days:.1f} 天\n到期后无法自动续期，需重新导入"
            else:
                rt_tip += "无RT或解析失败（纯JWT导入的可能无RT字段）"
            rt_item = _set_item(self._pool_table, row, 6, rt_txt, tooltip=rt_tip)
            if rt_days is not None:
                if rt_days < 0:
                    rt_item.setForeground(QBrush(QColor(0xC0, 0x27, 0x1D)))
                elif rt_days < 7:
                    rt_item.setForeground(QBrush(QColor(0xD6, 0x9E, 0x2E)))
                else:
                    rt_item.setForeground(QBrush(QColor(0x0A, 0x7D, 0x33)))

            # 操作栏：刷新积分(独立按钮) + 积分明细(独立按钮) + 设置齿轮菜单
            ops_widget = QWidget()
            ops_widget.setAttribute(Qt.WA_TranslucentBackground, True)
            ops_layout = QHBoxLayout(ops_widget)
            ops_layout.setContentsMargins(4, 0, 4, 0)
            ops_layout.setSpacing(4)
            ops_layout.setAlignment(Qt.AlignCenter)

            # 刷新积分（🔄图标，垂直居中）
            btn_query = QToolButton()
            btn_query.setText("🔄")
            btn_query.setCursor(Qt.PointingHandCursor)
            btn_query.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn_query.setToolTip("查询该账号最新积分")
            btn_query.setFixedSize(32, 28)
            btn_query.setStyleSheet("""
                QToolButton {
                    background: #E8F5EC; color: #0A7D33; border: 1px solid #C6E9D2;
                    border-radius: 6px; font-size: 14px; padding: 0;
                }
                QToolButton:hover { background: #C6E9D2; border-color: #0A7D33; }
                QToolButton:pressed { background: #A8D9B5; }
            """)
            btn_query.clicked.connect(lambda _c, kid=key_id, b=btn_query: self._query_single_key_points(kid, b))
            ops_layout.addWidget(btn_query)

            # 积分明细（📊独立按钮，不用菜单入口）
            btn_detail = QToolButton()
            btn_detail.setText("📊")
            btn_detail.setCursor(Qt.PointingHandCursor)
            btn_detail.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn_detail.setToolTip("查看该账号的积分包明细")
            btn_detail.setFixedSize(32, 28)
            btn_detail.setStyleSheet("""
                QToolButton {
                    background: #E8F0FE; color: #1A56DB; border: 1px solid #C6DCFC;
                    border-radius: 6px; font-size: 14px; padding: 0;
                }
                QToolButton:hover { background: #C6DCFC; border-color: #1A56DB; }
                QToolButton:pressed { background: #A8C4F0; }
            """)
            btn_detail.clicked.connect(lambda _c, kid=key_id: self._show_key_credits_detail(kid))
            ops_layout.addWidget(btn_detail)

            # 设置齿轮菜单（⚙风格）
            btn_ops = QToolButton()
            btn_ops.setObjectName("ops_btn")
            btn_ops.setText("⚙")
            btn_ops.setCursor(Qt.PointingHandCursor)
            btn_ops.setToolButtonStyle(Qt.ToolButtonTextOnly)
            btn_ops.setPopupMode(QToolButton.InstantPopup)
            btn_ops.setFixedSize(32, 28)
            btn_ops.setStyleSheet("""
                QToolButton {
                    background: #F4F4F5; color: #374151; border: 1px solid #E4E7EB;
                    border-radius: 6px; font-size: 15px; padding: 0;
                }
                QToolButton:hover { background: #E0E0E0; border-color: #9CA3AF; }
            """)

            ops_menu = QMenu(btn_ops)
            self._open_menus.append(ops_menu)  # 持有引用防 GC（见 _close_menus）
            _style_popup_menu(ops_menu)
            if state != "active":
                act = ops_menu.addAction("✅ 恢复")
                act.triggered.connect(
                    lambda checked, kid=key_id: self._set_key_relay_status(kid, "active"))
            else:
                act = ops_menu.addAction("🚫 禁用")
                act.triggered.connect(
                    lambda checked, kid=key_id: self._set_key_relay_status(kid, "disabled"))
            if state != "permanent_disabled":
                act = ops_menu.addAction("⛔ 永久禁用")
                act.triggered.connect(
                    lambda checked, kid=key_id: self._permanent_disable_key(kid))
            ops_menu.addSeparator()
            act = ops_menu.addAction("🗑 删除")
            act.setToolTip("从池子中彻底移除该 Key（token 一并删除，不可恢复）")
            act.triggered.connect(
                lambda checked, kid=key_id: self._delete_key(kid))

            btn_ops.setMenu(ops_menu)
            ops_layout.addWidget(btn_ops)
            ops_layout.addStretch()
            self._pool_table.setCellWidget(row, 7, ops_widget)

        self._stat_total.setText(f"📋 总 Key: {len(keys)}")
        self._stat_active.setText(f"✅ 活跃: {active}")
        self._stat_disabled.setText(f"🚫 禁用: {disabled}")
        # 剩余/已用积分统计：剩余=各号points剩余段之和（客户真正能用的），已用=总量-剩余
        points_str = f"{total_points:.0f}" if points_counted else "-"
        self._stat_points.setText(f"💰 剩余可用: {points_str}")
        used_pts = 0
        try:
            for k in keys:
                text = str(k.get("points", "") or "").strip()
                if "/" in text:
                    parts = text.split("/", 1)
                    rem, tot = float(parts[0]), float(parts[1])
                    used_pts += max(0, tot - rem)
        except (ValueError, ZeroDivisionError):
            pass
        self._stat_used_points.setText(f"已用: {used_pts:.0f}")
        # 剩余积分偏低时红色提醒
        if points_counted and total_points < 100:
            self._stat_points.setStyleSheet("font-size: 15px; font-weight: 800; color: #E53E3E;")
        else:
            self._stat_points.setStyleSheet("font-size: 15px; font-weight: 800; color: #0A7D33;")
        self._stat_used.setText(f"📊 总调用: {total_used}")

    def _set_key_relay_status(self, key_id: str, status: str):
        """单 Key 禁用/恢复（只写 relay_* 字段，不动主池 status）"""
        updates = {"relay_status": status}
        if status == "active":
            updates["relay_note"] = ""
            updates["relay_cooldown_until"] = 0
        self._db.update_upstream_key(key_id, updates)
        self._refresh_pool()

    def _permanent_disable_key(self, key_id: str):
        """永久禁用（不会被检测等自动恢复，只能手动恢复）"""
        ret = QMessageBox.question(
            self, "确认永久禁用",
            f"确定永久禁用 Key {key_id}？\n\n永久禁用后不会被检测等操作自动恢复，\n只能手动点击「恢复」来重新启用。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret == QMessageBox.Yes:
            self._set_key_relay_status(key_id, "permanent_disabled")

    # ===== 卡密添加账号（原账号管理功能合并至此） =====
    def _card_key_fetch(self):
        """卡密导入对话框：下载账号包→自动入库+Key池+导入完成立即刷新池列表"""
        from .accounts import CardKeyFetchDialog
        dialog = CardKeyFetchDialog(self)
        dialog.accounts_imported.connect(self._on_card_accounts_imported)
        dialog.exec()

    def _on_card_accounts_imported(self, accounts: list):
        """卡密导入完成：走accounts页同款入库逻辑（Key池+账号表+group标记），完成后立即刷新本页"""
        if not accounts:
            return
        try:
            # 复用账号管理的批量入库逻辑（写账号表+Key池+group）
            from .accounts import AccountsPage
            AccountsPage._on_batch_accounts_imported_page.__get__(self, AccountsPage)(accounts) \
                if False else None
        except Exception:
            pass
        # 直接内联同款逻辑（不依赖AccountsPage实例）
        try:
            from ...modules.proxy_server import ProxyDatabase
            from ...utils.store import save_account
            from ...models import Account, Platform
            import secrets
            from datetime import datetime
            proxy_db = ProxyDatabase.get_instance()
            existing_api_keys = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
            for acc_data in accounts:
                api_key = acc_data.get("api_key", "") or acc_data.get("auth_token", "")
                if api_key and api_key not in existing_api_keys:
                    points_str = ""
                    cr = acc_data.get("credits_remaining", 0)
                    ct = acc_data.get("credits_total", 0)
                    if cr and cr > 0:
                        points_str = f"{cr}/{ct if ct > 0 else cr}"
                    proxy_db.add_upstream_key({
                        "key_id": f"ck_{secrets.token_hex(4)}",
                        "api_key": api_key,
                        "label": acc_data.get("nickname", "") or acc_data.get("uid", ""),
                        "status": "active",
                        "used_count": 0,
                        "points": points_str,
                        "points_updated_at": "imported" if points_str else "",
                        "created_at": datetime.now().isoformat(),
                    })
                    existing_api_keys.add(api_key)
                raw_platform = acc_data.get("platform", Platform.CODEBUDDY)
                if isinstance(raw_platform, str):
                    try:
                        raw_platform = Platform(raw_platform.lower())
                    except ValueError:
                        raw_platform = Platform.CODEBUDDY
                try:
                    account = Account(
                        uid=acc_data.get("uid", ""),
                        nickname=acc_data.get("nickname", ""),
                        platform=raw_platform,
                        auth_token=acc_data.get("auth_token", ""),
                        auth_raw=acc_data.get("auth_raw", ""),
                        domain=acc_data.get("domain", "www.codebuddy.cn"),
                        ck=acc_data.get("ck", ""),
                        api_key=acc_data.get("api_key", ""),
                    )
                    save_account(account)
                    group = (acc_data.get("account_group") or "").strip()
                    if group:
                        import sqlite3 as _sql3
                        from ...utils.store import _get_db_path as _dbp3
                        try:
                            _c3 = _sql3.connect(str(_dbp3()))
                            _c3.execute("UPDATE accounts SET account_group=? WHERE uid=?", (group, acc_data.get("uid", "")))
                            _c3.commit()
                            _c3.close()
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"入库失败 uid={acc_data.get('uid','')}: {e}")
        except Exception:
            logger.exception("卡密导入Key池失败")
        # ★ 关键：导入完成立即刷新池列表（买家直接看到账号出现，不用手点刷新）
        self._refresh_pool()
        self._refresh_upstream_keys(reload_from_disk=True)

    def _query_single_key_points(self, key_id: str, btn=None):
        """操作按钮：查询单个Key最新积分（uid从accounts表反查，JWT模式必需）
        请求中：按钮显示⟳ + 灰背景；完成：恢复👁 + 绿背景，并弹结果。
        """
        for k in self._jwt_keys():
            if k.get("key_id") == key_id:
                token = k.get("api_key", "")
                if not token:
                    QMessageBox.warning(self, "无Token", "该Key没有api_key字段")
                    return
                # 从accounts表反查uid（JWT查分需要X-User-Id）
                uid = ""
                try:
                    from ...utils.store import find_account_by_token
                    acc = find_account_by_token(token)
                    if acc:
                        uid = acc.uid
                except Exception:
                    pass

                # 按钮即时反馈：变加载图标+灰色
                if btn:
                    btn.setText("⏳")
                    btn.setStyleSheet("QToolButton { background: #E4E7EB; color: #6B7280; border: 1px solid #E4E7EB; border-radius: 6px; font-size: 14px; padding: 0; }")
                    btn.setEnabled(False)

                def _done(result: dict, kid=key_id, b=btn):
                    # 恢复按钮原状（🔄刷新图标）
                    if b:
                        b.setText("🔄")
                        b.setStyleSheet("""
                            QToolButton {
                                background: #E8F5EC; color: #0A7D33; border: 1px solid #C6E9D2;
                                border-radius: 6px; font-size: 14px; padding: 0;
                            }
                            QToolButton:hover { background: #C6E9D2; border-color: #0A7D33; }
                        """)
                        b.setEnabled(True)
                    try:
                        if result.get("success"):
                            from datetime import datetime as _dt
                            remaining = result.get("remaining_credits", 0)
                            total = result.get("total_credits", 0)
                            self._db.update_upstream_key(kid, {
                                "points": f"{remaining}/{total}" if total > 0 else str(remaining),
                                "points_updated_at": _dt.now().isoformat(),
                            })
                            self._refresh_pool()
                            # 弹结果反馈（用户要求：点了要有反馈通知）
                            QMessageBox.information(
                                self, "✅ 查询成功",
                                f"账号 {kid[:12]}... 剩余积分：{remaining} / {total}\n\n"
                                f"已实时写入Key池并刷新列表。")
                        else:
                            QMessageBox.warning(self, "查询失败", result.get("error", "未知错误"))
                    except Exception:
                        logger.exception("单Key积分查询回写失败")

                from PySide6.QtCore import QThread, Signal as QSignal

                class _QThread(QThread):
                    done = QSignal(dict)

                    def run(self):
                        try:
                            from ...modules.api_client import ApiClient
                            client = ApiClient(access_token=token, uid=uid, domain="www.codebuddy.cn")
                            r = client.get_user_resource()
                            self.done.emit(r)
                        except Exception as e:
                            self.done.emit({"success": False, "error": str(e)})

                t = _QThread()
                t.done.connect(_done)
                t.start()
                self._single_points_thread = t
                return

    def _open_batch_status_dialog(self, enable: bool, permanent: bool = False):
        """批量禁/解禁弹框（照 API 代理页：按积分范围筛选 + 实时预览 + 二次确认）

        只写 relay_* 字段，不动 API 代理页的 status。
        """
        keys = self._jwt_keys()
        if enable:
            action_text = "批量解禁"
            status_to = "active"
            done_verb = "解禁"
            confirm_template = "确定将上述 {n} 个 Key 恢复为可用吗？"
        elif permanent:
            action_text = "批量永久禁用"
            status_to = "permanent_disabled"
            done_verb = "永久禁用"
            confirm_template = "确定永久禁用上述 {n} 个 Key 吗？\n（永久禁用后只能手动解禁）"
        else:
            action_text = "批量临时禁用"
            status_to = "disabled"
            done_verb = "临时禁用"
            confirm_template = "确定临时禁用上述 {n} 个 Key 吗？"

        dlg = QDialog(self)
        dlg.setWindowTitle(action_text)
        dlg.setMinimumWidth(420)

        v = QVBoxLayout(dlg)

        # 输入区
        form = QFormLayout()
        min_spin = QSpinBox()
        min_spin.setRange(0, 9_999_999)
        min_spin.setValue(0 if not enable else 500)
        form.addRow("最小积分:", min_spin)

        max_spin = QSpinBox()
        max_spin.setRange(0, 9_999_999)
        max_spin.setValue(500 if not enable else 100_000)
        form.addRow("最大积分:", max_spin)

        v.addLayout(form)
        v.addWidget(QLabel(f"筛选条件：积分（剩余）在上述范围内的 Key\n操作类型：{action_text}（仅本页侧）"))

        # 预览（实时刷新）
        preview_label = QLabel("")
        preview_label.setWordWrap(True)
        preview_label.setObjectName("preview_label")
        v.addWidget(preview_label)

        def _refresh_preview():
            lo, hi = min_spin.value(), max_spin.value()
            matched = self._filter_keys_by_points(keys, lo, hi, status_to)
            in_range = [k for k in keys
                        if lo <= ApiProxyPage._points_remaining(k.get("points", "")) <= hi]
            already_done = len([k for k in in_range
                                if k.get("relay_status", "active") == status_to])
            preview_label.setText(
                f"范围匹配 {len(in_range)} 个，将实际生效 <b>{len(matched)}</b> 个"
                + (f"（{already_done} 个已{'启用' if enable else '禁用'}，跳过）" if already_done else "")
            )

        min_spin.valueChanged.connect(_refresh_preview)
        max_spin.valueChanged.connect(_refresh_preview)
        _refresh_preview()

        # 按钮
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        v.addWidget(btn_box)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText(action_text)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        lo, hi = min_spin.value(), max_spin.value()
        matched = self._filter_keys_by_points(keys, lo, hi, status_to)
        if not matched:
            QMessageBox.information(self, "提示", "范围内没有可操作的 Key")
            return

        # 二次确认
        reply = QMessageBox.question(
            self,
            action_text,
            confirm_template.format(n=len(matched)) +
            f"\n积分范围：{lo} ~ {hi}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for k in matched:
            updates = {"relay_status": status_to}
            if status_to == "active":
                updates["relay_note"] = ""
                updates["relay_cooldown_until"] = 0
            self._db.update_upstream_key(k["key_id"], updates)

        self._refresh_pool()
        QMessageBox.information(self, "完成", f"已{done_verb} {len(matched)} 个 Key")

    @staticmethod
    def _filter_keys_by_points(keys: list, lo: int, hi: int, target_status: str = None) -> list:
        """按 points 剩余积分在 [lo, hi] 范围内筛选 Key，排除已处于目标状态的（看 relay_status）"""
        result = []
        for k in keys:
            if target_status and k.get("relay_status", "active") == target_status:
                continue  # 已经是目标状态，跳过
            pts = ApiProxyPage._points_remaining(k.get("points", ""))
            if pts < 0:
                continue  # 解析失败的跳过（没有积分数据）
            if lo <= pts <= hi:
                result.append(k)
        return result

    def _import_from_accounts(self):
        """从已获取账号导入 token（JWT）到池子，ck_ 卡密不导入"""
        existing_keys = self._db.get_upstream_keys()
        existing_api_keys = {k.get("api_key", "") for k in existing_keys}
        dialog = ImportFromAccountsDialog(self, existing_api_keys=existing_api_keys)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            accounts = dialog.get_selected_accounts()
            if not accounts:
                QMessageBox.warning(self, "提示", "请选择要导入的账号")
                return

            count = 0
            skipped = 0
            for acc in accounts:
                # 无感换号只收账号 token（JWT），ck_ 卡密走 API 代理那边
                import_key = acc.auth_token or ""
                if not import_key.startswith("eyJ"):
                    skipped += 1
                    continue

                existing_keys = self._db.get_upstream_keys()
                existing_api_keys = {k.get("api_key", "") for k in existing_keys}
                if import_key in existing_api_keys:
                    continue

                key_data = {
                    "key_id": f"ck_{secrets.token_hex(4)}",
                    "api_key": import_key,
                    "label": acc.display_name or acc.uid,
                    "status": "active",
                    "used_count": 0,
                    "points": f"{acc.quota.credits_remaining:.0f}/{acc.quota.credits_total:.0f}" if acc.quota and acc.quota.credits_total > 0 else "",
                    "points_updated_at": "imported" if acc.quota and acc.quota.credits_total > 0 else "",
                    "created_at": __import__('datetime').datetime.now().isoformat(),
                }
                self._db.add_upstream_key(key_data)
                count += 1

            self._refresh_pool()
            msg = f"成功导入 {count} 个 token Key"
            if skipped:
                msg += f"，跳过 {skipped} 个非 token 账号（卡密请去 API 代理页导入）"
            if count == 0 and not skipped:
                msg = "没有新的 Key 需要导入（可能已存在）"
            QMessageBox.information(self, "导入完成", msg)

    def _refresh_all_points(self):
        """查询所有 token Key 的积分并同步（照 API 代理页：后台 worker + 进度上状态行，不弹窗）"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ...modules.api_client import ApiClient

        keys = [k for k in self._jwt_keys() if k.get("api_key", "")]
        if not keys:
            QMessageBox.information(self, "提示", "上游 Key 池为空，无需查询")
            return

        class PointsRefreshWorker(QThread):
            """Background worker for refreshing upstream key quota."""
            progress = QSignal(str)
            done = QSignal(int, int)  # success, failed

            def __init__(self, keys, db, max_workers=5):
                super().__init__()
                self._keys = keys
                self._db = db
                self.max_workers = max_workers

            def _query_one(self, k):
                api_key = k.get("api_key", "")
                label = k.get("label", api_key[:12])
                self.progress.emit(f"正在查询 {label}...")
                if api_key.startswith("ck_"):
                    client = ApiClient.from_api_key(api_key)
                else:
                    from ...utils.store import load_accounts
                    accounts = load_accounts()
                    acc = None
                    for a in accounts:
                        if a.auth_token == api_key or a.api_key == api_key:
                            acc = a
                            break
                    if acc and acc.api_key and acc.api_key.startswith("ck_"):
                        client = ApiClient.from_api_key(acc.api_key)
                    elif acc:
                        client = ApiClient(
                            access_token=acc.auth_token,
                            uid=acc.uid,
                            domain=acc.domain or "www.codebuddy.cn",
                        )
                    else:
                        client = ApiClient.from_api_key(api_key)
                return k, client.get_user_resource()

            def run(self):
                success = 0
                failed = 0
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._query_one, k): k for k in self._keys}
                    for future in as_completed(futures):
                        try:
                            k, result = future.result()
                            api_key = k.get("api_key", "")
                            if result.get("success"):
                                remaining = result.get("remaining_credits", 0)
                                total = result.get("total_credits", 0)
                                packages = result.get("packages", [])
                                self._db.sync_quota_to_key(
                                    api_key_or_token=api_key,
                                    remaining_credits=remaining,
                                    total_credits=total,
                                    packages=packages,
                                )
                                try:
                                    from ...utils.store import load_accounts, save_account
                                    accounts = load_accounts()
                                    for acc in accounts:
                                        if acc.auth_token == api_key or acc.api_key == api_key:
                                            acc.quota.credits_remaining = remaining
                                            acc.quota.credits_total = total
                                            save_account(acc)
                                            break
                                except Exception:
                                    pass
                                success += 1
                            else:
                                failed += 1
                        except Exception:
                            failed += 1
                self.done.emit(success, failed)

        max_workers = _get_account_concurrency_setting()
        self._points_worker = PointsRefreshWorker(keys, self._db, max_workers=max_workers)
        self._points_worker.progress.connect(
            lambda msg: self._stat_total.setText(f"⏳ {msg}")
        )
        self._points_worker.done.connect(self._on_points_refresh_done)
        self._points_worker.start()

    def _on_points_refresh_done(self, success: int, failed: int):
        """积分刷新完成回调（照 API 代理页：结果上状态行，不弹窗）"""
        self._refresh_pool()
        msg = f"积分刷新完成：✅ {success} 个成功"
        if failed > 0:
            msg += f"，❌ {failed} 个失败"
        self._stat_total.setText(f"📋 {msg}")

    def _check_all_key_status(self):
        """一键检测所有 token Key 是否被风控（403 code:11140），异常的本页侧禁用"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ...modules.api_client import check_api_key_chat_status

        keys = [
            k for k in self._jwt_keys()
            if k.get("relay_status", "active") == "active"
        ]
        if not keys:
            QMessageBox.information(self, "提示", "没有需要检测的 Key（活跃的 token Key 为空）")
            return

        class KeyStatusCheckWorker(QThread):
            progress = QSignal(str)
            done = QSignal(int, int, int)  # normal, abnormal, failed

            def __init__(self, keys, db, max_workers=5):
                super().__init__()
                self._keys = keys
                self._db = db
                self.max_workers = max_workers

            def _check_one(self, k):
                api_key = k.get("api_key", "")
                label = k.get("label", api_key[:12])
                # JWT 临期/过期先续期再检测，避免可续期的 Key 被 401 误判
                if api_key.startswith("eyJ"):
                    from ...modules.proxy_server import refresh_pool_jwt_key
                    api_key = refresh_pool_jwt_key(self._db, k)
                self.progress.emit(f"检测 {label}...")
                result = check_api_key_chat_status(api_key, attempts=3)
                return k, label, result

            def run(self):
                normal = 0
                abnormal = 0
                failed = 0
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._check_one, k): k for k in self._keys}
                    for future in as_completed(futures):
                        try:
                            k, label, result = future.result()
                            key_id = k.get("key_id", "")
                            status_text = result.get("status_text", "check_failed")
                            self.progress.emit(f"{label}: {status_text}")
                            if result.get("flag") == "abnormal":
                                # 只动本页侧状态，不动 API 代理页的 status
                                self._db.update_upstream_key(key_id, {
                                    "relay_status": "disabled",
                                    "relay_note": "检测: 被上游风控(11140)",
                                })
                                abnormal += 1
                            elif result.get("flag") == "rate_limited":
                                self._db.update_upstream_key(key_id, {
                                    "relay_status": "disabled",
                                    "relay_note": "检测: 系统限流",
                                })
                                abnormal += 1
                            elif result.get("success"):
                                # 检测通过：自动禁用的（备注带「检测:」）恢复，手动禁用的不动
                                if k.get("relay_status") == "disabled" and \
                                        str(k.get("relay_note", "")).startswith("检测:"):
                                    self._db.update_upstream_key(key_id, {
                                        "relay_status": "active",
                                        "relay_note": "",
                                    })
                                normal += 1
                            else:
                                failed += 1
                        except Exception as e:
                            self.progress.emit(f"检测失败: {e}")
                            failed += 1
                self.done.emit(normal, abnormal, failed)

        max_workers = _get_account_concurrency_setting()
        self._status_check_worker = KeyStatusCheckWorker(keys, self._db, max_workers=max_workers)
        self._status_check_worker.progress.connect(
            lambda msg: self._stat_total.setText(f"🔍 {msg}")
        )
        self._status_check_worker.done.connect(self._on_status_check_done)
        self._status_check_worker.start()

    def _on_status_check_done(self, normal: int, abnormal: int, failed: int):
        """检测完成回调（照 API 代理页：结果上状态行，有异常才弹窗）"""
        self._refresh_pool()
        msg = f"检测完成：✅ 正常 {normal} 个"
        if abnormal > 0:
            msg += f"，⚠️ 异常 {abnormal} 个（已在本页侧禁用）"
        if failed > 0:
            msg += f"，❓ 失败 {failed} 个"
        self._stat_total.setText(f"📋 {msg}")
        if abnormal > 0:
            QMessageBox.warning(
                self, "检测完成",
                f"发现 {abnormal} 个 Key 被风控/限流，已在本页侧禁用。\n"
                f"禁用的 Key 不会再被中转调用。\n\n"
                f"正常: {normal}  异常: {abnormal}  失败: {failed}",
            )

    # ═══════════ 使用日志 ═══════════

    def _refresh_log(self):
        if not (self._relay_server and self._relay_server.is_running):
            self._log_edit.setPlainText("接入服务未开启。")
            return
        events = self._relay_server.get_events()
        st = self._relay_server.get_status()
        cur = st.get("current_key") or {}
        header_parts = []
        if cur:
            header_parts.append(
                f"当前消耗: {cur.get('label', '-')}（剩余 {self._fresh_points(cur)} 分）")
        header_parts.append(
            f"累计请求 {st['total_requests']} 次（换号 {st['swapped_requests']} 次）")
        header_html = "<br>".join(_html_esc(p) for p in header_parts) + "<hr>"

        if events:
            body = "<br>".join(_colorize_event_html(ev) for ev in events)
        else:
            body = "<span style='color:#9BA4B0'>（暂无请求）</span>"
        self._log_edit.setHtml(
            f"<pre style='font-family:Consolas,monospace; font-size:12px;'>"
            f"{header_html}{body}</pre>")

    def _clear_log(self):
        if self._relay_server and self._relay_server.is_running:
            self._relay_server.clear_events()
        self._log_edit.clear()
