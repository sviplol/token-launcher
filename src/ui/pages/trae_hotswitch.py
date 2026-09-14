"""Trae 换号页面 - TRAE SOLO CN 透明中转

完全独立的账号/池子体系（trae_pool.json），与 API 代理、无感换号互不相通：
- 池子账号 = cockpit 导入 JSON（access_token/refresh_token/trae_auth_raw）
- 中转只在计费路径（/api/agent/* 等）换池子账号身份头，其余透传
- 「接入」会补丁 Trae 客户端的 product.json 并重签名，可一键还原
"""

import json
import subprocess
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QMessageBox, QApplication, QScrollArea, QDialog,
    QDialogButtonBox
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal

from ...utils.store import save_setting, load_setting
from ...modules.trae_relay import (
    TraeRelayServer, list_trae_accounts, add_trae_account, remove_trae_account,
    apply_trae_config, restore_trae_config, get_trae_config_state,
    query_trae_points, checkin_trae_account, checkin_trae_status,
    is_trae_running,
)
from .hotswitch import _set_multiline_text


class _PoolWorker(QThread):
    """后台检测/刷积分/签到（照 PointsRefreshWorker 模式）"""
    progress = Signal(str)
    done = Signal(int, int)  # 成功数, 失败数

    def __init__(self, uids, mode):
        super().__init__()
        self._uids = uids
        self._mode = mode  # "check"（状态+积分+签到状态）| "checkin"（签到）

    def run(self):
        ok = fail = 0
        for uid in self._uids:
            self.progress.emit(f"正在处理 {uid}...")
            if self._mode == "check":
                success, _msg = query_trae_points(uid)
                checkin_trae_status(uid)
            else:
                success, _msg = checkin_trae_account(uid)
            ok, fail = ok + int(success), fail + int(not success)
        self.done.emit(ok, fail)


def _set_item(table, row, col, text, tooltip=None):
    item = QTableWidgetItem(text)
    item.setToolTip(tooltip if tooltip else text)
    table.setItem(row, col, item)
    return item


class TraeHotSwitchPage(QWidget):
    """Trae 换号页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 页面背景走全局 QSS #content_area（与其他页面一致，否则默认背景色不一致）
        self.setObjectName("content_area")
        self._relay_server: TraeRelayServer = None
        self._setup_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start(2000)

        QTimer.singleShot(800, self._autostart_relay)

    # ═══════════ UI ═══════════

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题/副标题也放进滚动区：否则内容最低高度把固定头部挤压到 0 高，
        # 视觉上就是「顶部一大段空白间距」（hotswitch 踩过的坑）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(16)

        title = QLabel("Trae 换号")
        title.setObjectName("page_title")
        content_layout.addWidget(title)

        subtitle = QLabel("TRAE SOLO CN 透明中转 · 独立账号池 · 只消耗池里号的积分")
        subtitle.setObjectName("page_subtitle")
        content_layout.addWidget(subtitle)

        # ─── 服务控制 ───
        control_card = QFrame()
        control_card.setObjectName("card")
        control_layout = QVBoxLayout(control_card)
        control_layout.setSpacing(10)

        row = QHBoxLayout()
        row.addWidget(QLabel("端口:"))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(int(load_setting("trae_relay_port", "8005")))
        row.addWidget(self._port_spin)

        row.addWidget(QLabel("    中转地址:"))
        self._url_label = QLabel(f"https://127.0.0.1:{self._port_spin.value()}")
        self._url_label.setStyleSheet("color: #2B6CB0; font-weight: 600; font-size: 13px;")
        self._url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self._url_label)

        btn_copy = QPushButton("📋 复制")
        btn_copy.setObjectName("secondary_btn")
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_copy.setFixedWidth(60)
        btn_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self._url_label.text()))
        row.addWidget(btn_copy)

        row.addStretch()

        self._status_label = QLabel("⏹ 已停止")
        self._status_label.setStyleSheet("font-weight: 600; color: #9BA4B0;")
        row.addWidget(self._status_label)

        self._toggle_btn = QPushButton("▶ 启动服务")
        self._toggle_btn.setObjectName("primary_btn")
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle_service)
        row.addWidget(self._toggle_btn)
        control_layout.addLayout(row)

        row2 = QHBoxLayout()
        self._current_label = QLabel("当前消耗: 无")
        self._current_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        row2.addWidget(self._current_label)
        row2.addStretch()
        self._stat_label = QLabel("")
        self._stat_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        row2.addWidget(self._stat_label)
        control_layout.addLayout(row2)

        content_layout.addWidget(control_card)

        # ─── 账号池 ───
        pool_card = QFrame()
        pool_card.setObjectName("card")
        pool_layout = QVBoxLayout(pool_card)

        pool_head = QHBoxLayout()
        pool_title = QLabel("账号池")
        pool_title.setStyleSheet("font-weight: 600;")
        pool_head.addWidget(pool_title)
        pool_head.addStretch()

        btn_add = QPushButton("➕ 添加账号")
        btn_add.setObjectName("secondary_btn")
        btn_add.setCursor(Qt.PointingHandCursor)
        btn_add.clicked.connect(self._add_account)
        pool_head.addWidget(btn_add)

        btn_refresh = QPushButton("🔍 检测状态 / 积分")
        btn_refresh.setObjectName("secondary_btn")
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.setToolTip("检测 token 是否有效 + 刷新积分 + 查签到状态；失效的号自动标记禁用")
        btn_refresh.clicked.connect(lambda: self._run_pool_worker("check"))
        pool_head.addWidget(btn_refresh)

        btn_checkin = QPushButton("✅ 一键签到")
        btn_checkin.setObjectName("secondary_btn")
        btn_checkin.setCursor(Qt.PointingHandCursor)
        btn_checkin.setToolTip("批量签到；今日已签到的号会跳过不重复领")
        btn_checkin.clicked.connect(lambda: self._run_pool_worker("checkin"))
        pool_head.addWidget(btn_checkin)

        btn_del = QPushButton("🗑 删除选中")
        btn_del.setObjectName("secondary_btn")
        btn_del.setCursor(Qt.PointingHandCursor)
        btn_del.clicked.connect(self._remove_account)
        pool_head.addWidget(btn_del)
        pool_layout.addLayout(pool_head)

        self._pool_table = QTableWidget()
        self._pool_table.setColumnCount(7)
        self._pool_table.setHorizontalHeaderLabels(
            ["昵称", "UID", "Token 过期", "积分", "签到", "状态", "消耗次数"])
        self._pool_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pool_table.verticalHeader().setVisible(False)
        self._pool_table.setAlternatingRowColors(True)  # 照其他页表格交替行色
        self._pool_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pool_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._pool_table.setMinimumHeight(160)
        pool_layout.addWidget(self._pool_table)

        content_layout.addWidget(pool_card)

        # ─── 客户端接入 ───
        client_card = QFrame()
        client_card.setObjectName("card")
        client_layout = QVBoxLayout(client_card)

        client_head = QHBoxLayout()
        client_title = QLabel("客户端接入（TRAE SOLO CN）")
        client_title.setStyleSheet("font-weight: 600;")
        client_head.addWidget(client_title)
        client_head.addStretch()

        self._apply_btn = QPushButton("🔌 接入 Trae")
        self._apply_btn.setObjectName("primary_btn")
        self._apply_btn.setCursor(Qt.PointingHandCursor)
        self._apply_btn.clicked.connect(self._apply_client)
        client_head.addWidget(self._apply_btn)

        self._restore_btn = QPushButton("♻️ 还原")
        self._restore_btn.setObjectName("secondary_btn")
        self._restore_btn.setCursor(Qt.PointingHandCursor)
        self._restore_btn.clicked.connect(self._restore_client)
        client_head.addWidget(self._restore_btn)
        client_layout.addLayout(client_head)

        self._client_state_label = QLabel("")
        self._client_state_label.setWordWrap(True)
        self._client_state_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        client_layout.addWidget(self._client_state_label)

        content_layout.addWidget(client_card)

        # ─── 使用日志 ───
        log_card = QFrame()
        log_card.setObjectName("card")
        log_layout = QVBoxLayout(log_card)

        log_head = QHBoxLayout()
        log_title = QLabel("使用日志")
        log_title.setStyleSheet("font-weight: 600;")
        log_head.addWidget(log_title)
        log_head.addStretch()
        btn_clear = QPushButton("🗑 清空")
        btn_clear.setObjectName("secondary_btn")
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.clicked.connect(self._clear_log)
        log_head.addWidget(btn_clear)
        log_layout.addLayout(log_head)

        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMinimumHeight(140)
        log_layout.addWidget(self._log_text)

        content_layout.addWidget(log_card)

    # ═══════════ 服务控制 ═══════════

    def _toggle_service(self):
        if self._relay_server and self._relay_server.is_running:
            self._relay_server.stop()
            save_setting("trae_relay_enabled", "0")
            self._refresh_status()
            return
        port = self._port_spin.value()
        save_setting("trae_relay_port", str(port))
        self._relay_server = TraeRelayServer(port=port)
        if self._relay_server.start():
            save_setting("trae_relay_enabled", "1")
        else:
            self._relay_server = None
            self._set_client_msg("❌ 启动失败：端口被占用（换个端口试试）", error=True)
        self._refresh_status()

    def _autostart_relay(self):
        if load_setting("trae_relay_enabled", "0") == "1":
            port = int(load_setting("trae_relay_port", "8005"))
            self._relay_server = TraeRelayServer(port=port)
            if not self._relay_server.start():
                self._relay_server = None
            self._refresh_status()

    def _refresh_status(self):
        running = self._relay_server and self._relay_server.is_running
        if running:
            self._status_label.setText("🟢 运行中")
            self._status_label.setStyleSheet("font-weight: 600; color: #38A169;")
            self._toggle_btn.setText("⏹ 停止服务")
        else:
            self._status_label.setText("⏹ 已停止")
            self._status_label.setStyleSheet("font-weight: 600; color: #9BA4B0;")
            self._toggle_btn.setText("▶ 启动服务")
        self._url_label.setText(f"https://127.0.0.1:{self._port_spin.value()}")

    # ═══════════ 账号池 ═══════════

    def _add_account(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("添加 Trae 账号")
        dlg.setMinimumWidth(560)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("粘贴 cockpit 导入 JSON（含 access_token / refresh_token / trae_auth_raw）："))
        edit = QTextEdit()
        edit.setPlaceholderText('{"email": "...", "user_id": "...", "access_token": "eyJ...", ...}')
        v.addWidget(edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        if dlg.exec() == QDialog.Accepted:
            text = edit.toPlainText().strip()
            if not text:
                return
            ok, msg = add_trae_account(text)
            if not ok:
                QMessageBox.warning(self, "添加失败", msg)
            self._refresh_pool()

    def _remove_account(self):
        row = self._pool_table.currentRow()
        if row < 0:
            return
        uid = self._pool_table.item(row, 1).text()
        remove_trae_account(uid)
        self._refresh_pool()

    def _refresh_pool(self):
        accounts = list_trae_accounts()
        self._pool_table.setRowCount(len(accounts))
        for i, acc in enumerate(accounts):
            _set_item(self._pool_table, i, 0, acc.get("nickname", ""))
            _set_item(self._pool_table, i, 1, acc.get("uid", ""))
            exp = int(acc.get("expires_at") or 0)
            exp_text = time.strftime("%m-%d %H:%M", time.localtime(exp / 1000)) if exp else "-"
            if exp and exp < int(time.time() * 1000):
                exp_text += "（已过期）"
            _set_item(self._pool_table, i, 2, exp_text)
            _set_item(self._pool_table, i, 3, acc.get("points", "-"))
            checkin = acc.get("checkin", "-")
            checkin_text = {"已签到": "✅ 已签到", "未签到": "☐ 未签到"}.get(checkin, checkin)
            _set_item(self._pool_table, i, 4, checkin_text)
            status = "🟢 正常" if acc.get("status") == "active" else f"🚫 {acc.get('note', '已禁用')}"
            _set_item(self._pool_table, i, 5, status)
            _set_item(self._pool_table, i, 6, str(acc.get("used", 0)))

    def _run_pool_worker(self, mode: str):
        uids = [a["uid"] for a in list_trae_accounts()]
        if not uids:
            return
        self._worker = _PoolWorker(uids, mode)
        self._worker.progress.connect(
            lambda msg: self._stat_label.setText(msg))
        self._worker.done.connect(self._on_pool_worker_done)
        self._worker.start()

    def _on_pool_worker_done(self, ok: int, fail: int):
        self._stat_label.setText(f"完成：成功 {ok} 个，失败 {fail} 个")
        self._refresh_pool()

    # ═══════════ 客户端接入 ═══════════

    def _quit_trae_and_wait(self) -> bool:
        """优雅退出 Trae 并等待，最多 15s。返回是否已完全退出"""
        subprocess.run(
            ["osascript", "-e", 'quit app "TRAE SOLO CN"'],
            capture_output=True, timeout=10)
        for _ in range(30):
            if not is_trae_running():
                return True
            time.sleep(0.5)
        return not is_trae_running()

    def _apply_client(self):
        port = self._port_spin.value()
        running = is_trae_running()
        tip = ("接入会修改 TRAE SOLO CN 的 product.json 并重签名（自动备份，可一键还原）。\n"
               + ("检测到 Trae 正在运行，需要先退出。是否现在退出并接入？"
                  if running else "是否继续？"))
        reply = QMessageBox.question(self, "接入 Trae", tip)
        if reply != QMessageBox.Yes:
            return
        if running and not self._quit_trae_and_wait():
            self._set_client_msg("❌ Trae 退出超时，请手动完全退出后再试", error=True)
            return
        ok, msg = apply_trae_config(port)
        self._set_client_msg(("✅ " if ok else "❌ ") + msg, error=not ok)
        if ok:
            self._refresh_client_state()

    def _restore_client(self):
        running = is_trae_running()
        if running:
            reply = QMessageBox.question(
                self, "还原 Trae",
                "还原需要先退出 TRAE SOLO CN。是否现在退出并还原？")
            if reply != QMessageBox.Yes:
                return
            if not self._quit_trae_and_wait():
                self._set_client_msg("❌ Trae 退出超时，请手动完全退出后再试", error=True)
                return
        ok, msg = restore_trae_config()
        self._set_client_msg(("✅ " if ok else "❌ ") + msg, error=not ok)
        if ok:
            self._refresh_client_state()

    def _set_client_msg(self, text, error=False):
        color = "#E53E3E" if error else "#38A169"
        self._client_state_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        _set_multiline_text(self._client_state_label, text)

    def _refresh_client_state(self):
        state = get_trae_config_state(self._port_spin.value())
        if not state["installed"]:
            self._set_client_msg("未检测到 TRAE SOLO CN", error=True)
            return
        parts = []
        parts.append("端点已指向中转" if state["pointed_to_us"] else "端点未接入")
        parts.append("证书容错已开" if state["cert_ignore"] else "证书容错未开")
        parts.append("Trae 运行中" if state["running"] else "Trae 未运行")
        self._client_state_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        _set_multiline_text(self._client_state_label, " · ".join(parts))

    # ═══════════ 日志 ═══════════

    def _clear_log(self):
        if self._relay_server:
            self._relay_server.clear_events()
        self._log_text.clear()

    def _on_timer(self):
        if self._relay_server and self._relay_server.is_running:
            st = self._relay_server.get_status()
            cur = st["current_uid"] or "无"
            self._current_label.setText(f"当前消耗: {cur}")
            self._stat_label.setText(
                f"总请求 {st['total_requests']} · 换号 {st['swapped_requests']} · {st['last_event']}")
            events = self._relay_server.get_events()
            text = "\n".join(events)
            if text != self._log_text.toPlainText():
                self._log_text.setPlainText(text)
        else:
            self._current_label.setText("当前消耗: 无")
        self._refresh_pool()
