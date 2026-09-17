"""账号管理页面"""

import secrets
import json
import logging
import urllib.request
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QLineEdit,
    QDialog, QFormLayout, QTextEdit, QFileDialog, QMessageBox,
    QMenu, QSizePolicy, QAbstractItemView, QSpinBox, QProgressBar,
    QTreeWidget, QTreeWidgetItem
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QAction, QCursor

from ...i18n import t
from ...models import Account, Platform, AccountStatus, ResourcePackage
from ...utils.store import load_accounts, save_account, delete_account, save_setting, load_setting
from ...modules.oauth import WorkBuddyAuth
from ...modules.api_client import ApiClient, check_api_key_chat_status

logger = logging.getLogger(__name__)

PAGE_SIZE = 100  # 每页显示条数

# CK 服务器配置（与积分查询项目共用）
CK_SERVER_URL = "http://124.222.75.216:9658"
CK_API_KEY = "ck_client_2026ok"


class AddAccountDialog(QDialog):
    """添加账号对话框"""

    account_added = Signal(Account)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("accounts.add_account"))
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self):
        layout = QFormLayout(self)
        layout.setSpacing(12)

        self._platform_combo = QComboBox()
        for p in Platform:
            self._platform_combo.addItem(p.value, p)
        self._platform_combo.setVisible(False)

        self._uid_input = QLineEdit()
        self._uid_input.setPlaceholderText("UID (自动检测)")
        layout.addRow("UID:", self._uid_input)

        self._nickname_input = QLineEdit()
        self._nickname_input.setPlaceholderText("昵称 (自动检测)")
        layout.addRow("昵称:", self._nickname_input)

        self._token_input = QLineEdit()
        self._token_input.setPlaceholderText("JWT Token (自动检测)")
        layout.addRow("Token:", self._token_input)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        layout.addRow(self._status_label)

        # 第一行按钮：提取 + 从备份导入
        btn_row1 = QHBoxLayout()

        btn_extract = QPushButton("📥 提取当前账号")
        btn_extract.setObjectName("secondary_btn")
        btn_extract.setToolTip("从已登录的 WorkBuddy 中提取当前账号")
        btn_extract.clicked.connect(self._extract_current)
        btn_row1.addWidget(btn_extract)

        btn_backup = QPushButton("📦 从备份导入")
        btn_backup.setObjectName("secondary_btn")
        btn_backup.setToolTip("从 WorkBuddy 账号管理器的备份中导入账号")
        btn_backup.clicked.connect(self._import_from_backup)
        btn_row1.addWidget(btn_backup)

        layout.addRow(btn_row1)

        # 第二行按钮：登录新账号 + 从服务器获取
        btn_row2 = QHBoxLayout()

        btn_login = QPushButton("🔐 登录新账号")
        btn_login.setObjectName("secondary_btn")
        btn_login.setToolTip("关闭WB → 注销SSO → 清除登录态 → 重启WB → 浏览器登录新账号")
        btn_login.clicked.connect(self._login_new)
        btn_row2.addWidget(btn_login)

        btn_server = QPushButton("🌐 从服务器获取")
        btn_server.setObjectName("secondary_btn")
        btn_server.setToolTip("输入卡密从远程服务器获取账号 Token 和 API Key")
        btn_server.clicked.connect(self._fetch_from_server)
        btn_row2.addWidget(btn_server)

        layout.addRow(btn_row2)

        # 第三行按钮：Token导入 + 卡密导入
        btn_row3 = QHBoxLayout()

        btn_api = QPushButton("🎫 Token导入")
        btn_api.setObjectName("secondary_btn")
        btn_api.setToolTip("粘贴 JWT Token 或 手机号----access----refresh 整行导入账号")
        btn_api.clicked.connect(self._import_from_token)
        btn_row3.addWidget(btn_api)

        btn_card = QPushButton("卡密导入")
        btn_card.setObjectName("secondary_btn")
        btn_card.setToolTip("粘贴格式：昵称----apikey，一行一个")
        btn_card.clicked.connect(self._import_card_keys)
        btn_row3.addWidget(btn_card)

        layout.addRow(btn_row3)

        # 卡密提取账号包（线上卡密系统）
        btn_cardpack = QPushButton("🎫 卡密提取账号包")
        btn_cardpack.setObjectName("primary_btn")
        btn_cardpack.setToolTip("输入网站购买的卡密(WK-xxxx)，自动验证下载账号包并批量导入，附带自动刷新积分")
        btn_cardpack.setMinimumHeight(38)
        btn_cardpack.clicked.connect(self._fetch_card_pack)
        layout.addRow(btn_cardpack)

        # 第四行按钮：保存 + 取消
        btn_row4 = QHBoxLayout()

        btn_save = QPushButton("💾 保存")
        btn_save.setObjectName("primary_btn")
        btn_save.clicked.connect(self._save)
        btn_row4.addWidget(btn_save)

        btn_cancel = QPushButton(t("common.cancel"))
        btn_cancel.setObjectName("secondary_btn")
        btn_cancel.clicked.connect(self.reject)
        btn_row4.addWidget(btn_cancel)

        layout.addRow(btn_row4)

    def _extract_current(self):
        """从当前 WorkBuddy 会话提取账号"""
        self._status_label.setText("⏳ 正在提取当前账号...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        result = WorkBuddyAuth.extract_current_session()
        if result:
            self._token_input.setText(result.get("neodata_token", "") or result.get("access_token", ""))
            self._uid_input.setText(result.get("uid", ""))
            self._nickname_input.setText(result.get("nickname", ""))
            source = result.get("source", "")
            phone = result.get("phone_number", "")
            status_text = f"✅ 已提取: {result.get('nickname', '未知')}"
            if phone:
                status_text += f" (手机: {phone})"
            if source:
                status_text += f"\n来源: {source}"
            self._status_label.setText(status_text)
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
        else:
            self._status_label.setText(
                "❌ 当前 WorkBuddy 未登录。\n"
                "请先在 WorkBuddy 中登录账号，或点击「从备份导入」导入已有账号，\n"
                "或点击「登录新账号」通过浏览器登录。"
            )
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _import_from_backup(self):
        """从 WorkBuddy 账号管理器的备份中导入账号"""
        import json
        import os

        self._status_label.setText("⏳ 正在扫描备份...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        from ...modules.oauth import CODEBUDDY_EXT_AUTH_DIR, WORKBUDDY_DESKTOP_INFO

        backups = []

        # === 来源1：新版 workbuddy-desktop.*.info 备份文件 ===
        auth_dir = CODEBUDDY_EXT_AUTH_DIR
        if os.path.exists(auth_dir):
            for fname in sorted(os.listdir(auth_dir), reverse=True):
                if fname.startswith("workbuddy-desktop.") and fname.endswith(".info"):
                    fpath = os.path.join(auth_dir, fname)
                    ts_str = fname.replace("workbuddy-desktop.", "").replace(".info", "")
                    label = f"📦 {ts_str}"
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            info = json.load(f)
                        access_token = info.get("auth", {}).get("accessToken", "")
                        if access_token:
                            account = info.get("account", {})
                            nickname = account.get("nickname", "")
                            if nickname:
                                label = f"📦 {ts_str} ({nickname})"
                            backups.append((label, fpath, "desktop_info"))
                    except Exception:
                        pass

        # === 来源2：旧版 account_manager/backups 目录 ===
        backup_base = os.path.expanduser("~/.workbuddy/account_manager/backups")
        if not os.path.exists(backup_base):
            backup_base = os.path.expanduser("~/.workbuddy/backup")

        if os.path.exists(backup_base):
            for name in sorted(os.listdir(backup_base), reverse=True):
                backup_dir = os.path.join(backup_base, name)
                if not os.path.isdir(backup_dir):
                    continue
                token_file = os.path.join(backup_dir, "neodata_token")
                meta_file = os.path.join(backup_dir, "_meta.json")
                label = name
                has_token = os.path.exists(token_file)

                if os.path.exists(meta_file):
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        label = meta.get("label", name)
                        created = meta.get("created_at", 0)
                        if created:
                            import time
                            label = f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(created))} - {label}"
                    except Exception:
                        pass

                if has_token:
                    backups.append((f"📁 {label}", token_file, "neodata_token"))

        if not backups:
            self._status_label.setText("❌ 未找到含有 token 的备份。请先在 WorkBuddy 中登录账号。")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
            return

        # 如果只有一个备份，直接导入
        if len(backups) == 1:
            label, path, btype = backups[0]
            if btype == "desktop_info":
                self._load_desktop_info_backup(path)
            else:
                self._load_backup_token(path)
            return

        # 多个备份，弹出选择对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("选择备份")
        dialog.setMinimumWidth(400)
        dialog_layout = QVBoxLayout(dialog)

        dialog_layout.addWidget(QLabel(f"找到 {len(backups)} 个含 token 的备份，请选择："))

        from PySide6.QtWidgets import QListWidget
        list_widget = QListWidget()
        for label, path, btype in backups:
            list_widget.addItem(label)
        list_widget.setCurrentRow(0)
        dialog_layout.addWidget(list_widget)

        btn_box = QHBoxLayout()
        btn_ok = QPushButton("导入")
        btn_ok.setObjectName("primary_btn")
        btn_ok.clicked.connect(dialog.accept)
        btn_cancel_bk = QPushButton("取消")
        btn_cancel_bk.clicked.connect(dialog.reject)
        btn_box.addWidget(btn_ok)
        btn_box.addWidget(btn_cancel_bk)
        dialog_layout.addLayout(btn_box)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            idx = list_widget.currentRow()
            if idx >= 0:
                label, path, btype = backups[idx]
                if btype == "desktop_info":
                    self._load_desktop_info_backup(path)
                else:
                    self._load_backup_token(path)

    def _load_desktop_info_backup(self, info_path: str):
        """从 workbuddy-desktop.*.info 备份文件加载账号信息"""
        import json
        import os
        import time

        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)

            account = info.get("account", {})
            auth = info.get("auth", {})
            access_token = auth.get("accessToken", "")

            if not access_token:
                self._status_label.setText("❌ 备份文件中 accessToken 为空")
                self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
                return

            from ...modules.oauth import decode_jwt
            payload = decode_jwt(access_token)
            sub = payload.get("sub", "")
            username = payload.get("preferred_username", "")
            exp = payload.get("exp", 0)

            uid = account.get("uid", sub)
            nickname = account.get("nickname", username)
            phone = account.get("phoneNumber", "")

            self._token_input.setText(access_token)
            self._uid_input.setText(uid)
            self._nickname_input.setText(nickname)

            if exp and exp < time.time():
                self._status_label.setText(
                    f"⚠️ 已导入: {nickname}（Token 已过期，需要重新登录）\n"
                    f"手机号: {phone}"
                )
                self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
            else:
                self._status_label.setText(
                    f"✅ 已导入: {nickname} (手机: {phone or '未记录'})"
                )
                self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")

        except Exception as e:
            self._status_label.setText(f"❌ 读取备份失败: {e}")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _load_backup_token(self, token_file: str):
        """从备份 token 文件加载账号信息"""
        import json
        import os

        try:
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()

            if not token:
                self._status_label.setText("❌ 备份 token 为空")
                self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
                return

            from ...modules.oauth import decode_jwt
            payload = decode_jwt(token)
            sub = payload.get("sub", "")
            username = payload.get("preferred_username", "")
            exp = payload.get("exp", 0)

            self._token_input.setText(token)
            self._uid_input.setText(sub)
            self._nickname_input.setText(username)

            import time
            if exp and exp < time.time():
                self._status_label.setText(
                    f"⚠️ 已导入: {username}（Token 已过期，需要重新登录）"
                )
                self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
            else:
                self._status_label.setText(f"✅ 已导入: {username}")
                self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")

        except Exception as e:
            self._status_label.setText(f"❌ 读取备份失败: {e}")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _fetch_from_server(self):
        """从远程服务器通过卡密批量获取账号 — 打开专用对话框"""
        dialog = ServerFetchDialog(self)
        dialog.accounts_imported.connect(self._on_batch_accounts_imported)
        dialog.exec()

    def _fetch_card_pack(self):
        """卡密提取账号包（线上卡密系统）— 打开专用对话框"""
        dialog = CardKeyFetchDialog(self)
        dialog.accounts_imported.connect(self._on_card_pack_imported)
        dialog.exec()

    def _on_card_pack_imported(self, accounts: list):
        """卡密账号包导入回调：批量入库 + Key池同步 + 自动刷新积分"""
        self._on_batch_accounts_imported(accounts)
        if accounts:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(800, self._trigger_quota_refresh)

    def _trigger_quota_refresh(self):
        """通知 AccountsPage 刷新积分"""
        try:
            parent = self.parent()
            # AddAccountDialog 的 parent 可能是 AccountsPage
            while parent and not hasattr(parent, '_query_all_quotas'):
                parent = parent.parent()
            if parent and hasattr(parent, '_query_all_quotas'):
                parent._query_all_quotas()
        except Exception:
            logger.exception("自动刷新积分触发失败（不影响已导入账号）")

    @staticmethod
    def _decode_jwt_uid(token: str):
        """解码 JWT payload → (uid, preferred_username)。失败返回 ('', '')。"""
        import base64 as _b64
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            payload = json.loads(_b64.urlsafe_b64decode(part))
            return payload.get("sub", ""), payload.get("preferred_username", "")
        except Exception:
            return "", ""

    def _import_from_token(self):
        """粘贴 JWT 批量导入（卡密同款），格式：昵称----accessToken----refreshToken。

        refreshToken 可省（省了就不能自动续期）；昵称也可省（自动取 token 里的手机号）。
        纯 JWT 账号：api_key 留空，上游 Key 池自动回退用 auth_token（裸 JWT 转发可用）。
        """
        from PySide6.QtWidgets import QDialogButtonBox, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle("🎫 Token导入")
        dialog.setMinimumSize(520, 360)
        dlg_layout = QVBoxLayout(dialog)

        hint = QLabel("每行一个账号，格式：昵称----accessToken----refreshToken（refreshToken 可省；昵称省略时自动从 token 识别手机号）")
        hint.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        hint.setWordWrap(True)
        dlg_layout.addWidget(hint)

        text_edit = QTextEdit()
        text_edit.setPlaceholderText("14797525290----eyJhbGciOi...----eyJhbGciOiJIUzUx...\n13800138000----eyJhbGciOi...")
        dlg_layout.addWidget(text_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        buttons.button(QDialogButtonBox.Ok).setText("导入")
        buttons.button(QDialogButtonBox.Cancel).setText(t("common.cancel"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        accounts = []
        invalid = []
        for line_no, raw_line in enumerate(text_edit.toPlainText().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("----")]
            access_token, refresh_token, nickname = "", "", ""
            if len(parts) == 1:
                access_token = parts[0]
            elif len(parts) == 2:
                nickname, access_token = parts
            elif len(parts) == 3:
                nickname, access_token, refresh_token = parts
            else:
                invalid.append(str(line_no))
                continue
            if not access_token.startswith("eyJ"):
                invalid.append(str(line_no))
                continue
            uid, username = self._decode_jwt_uid(access_token)
            if not uid:
                invalid.append(str(line_no))
                continue
            if not nickname:
                nickname = username or uid[:12]
            accounts.append({
                "uid": uid,
                "nickname": nickname,
                "auth_token": access_token,
                "auth_raw": json.dumps({"accessToken": access_token, "refreshToken": refresh_token}),
                "api_key": "",
                "domain": "www.codebuddy.cn",
                "ck": "",
                "platform": Platform.CODEBUDDY,
            })

        if not accounts:
            QMessageBox.warning(self, t("common.warning"), "没有可导入的 Token，请检查格式：昵称----accessToken----refreshToken")
            return

        if invalid:
            reply = QMessageBox.question(
                self,
                "格式提醒",
                f"有 {len(invalid)} 行格式不正确或 token 无法解码，将跳过这些行并继续导入吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        self._on_batch_accounts_imported(accounts)
        QMessageBox.information(self, "导入完成", f"已导入 {len(accounts)} 个账号")

    def _import_card_keys(self):
        """粘贴卡密批量导入，格式：昵称----apikey。"""
        from PySide6.QtWidgets import QDialogButtonBox, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle("卡密导入")
        dialog.setMinimumSize(520, 360)
        dlg_layout = QVBoxLayout(dialog)

        hint = QLabel("每行一个账号，格式：昵称----apikey")
        hint.setStyleSheet("color: #9BA4B0; font-size: 12px;")
        dlg_layout.addWidget(hint)

        text_edit = QTextEdit()
        text_edit.setPlaceholderText("张三----ck_xxx\n李四----ck_xxx")
        dlg_layout.addWidget(text_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        buttons.button(QDialogButtonBox.Ok).setText("导入")
        buttons.button(QDialogButtonBox.Cancel).setText(t("common.cancel"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        accounts = []
        invalid = []
        for line_no, raw_line in enumerate(text_edit.toPlainText().splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            if "----" not in line:
                invalid.append(str(line_no))
                continue
            nickname, api_key = [part.strip() for part in line.split("----", 1)]
            if not nickname or not api_key:
                invalid.append(str(line_no))
                continue
            accounts.append({
                "uid": nickname,
                "nickname": nickname,
                "auth_token": api_key,
                "api_key": api_key,
                "ck": "",
                "platform": Platform.CODEBUDDY,
            })

        if not accounts:
            QMessageBox.warning(self, t("common.warning"), "没有可导入的卡密，请检查格式：昵称----apikey")
            return

        if invalid:
            reply = QMessageBox.question(
                self,
                "格式提醒",
                f"有 {len(invalid)} 行格式不正确，将跳过这些行并继续导入吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        self._on_batch_accounts_imported(accounts)
        QMessageBox.information(self, "导入完成", f"已导入 {len(accounts)} 个账号")

    def _on_batch_accounts_imported(self, accounts: list):
        """批量导入回调：保存所有账号并通知刷新，同时自动导入到上游Key池"""
        if not accounts:
            self._status_label.setText("⚠️ 没有可导入的账号")
            self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
            return

        key_pool_count = 0
        key_pool_error = ""

        # 1. 批量导入到上游Key池（一次写磁盘）
        try:
            from ...modules.proxy_server import ProxyDatabase
            proxy_db = ProxyDatabase.get_instance()
            existing_keys = proxy_db.get_upstream_keys()
            existing_api_keys = {k.get("api_key", "") for k in existing_keys}

            for acc_data in accounts:
                api_key = acc_data.get("api_key", "") or acc_data.get("auth_token", "")
                if api_key and api_key not in existing_api_keys:
                    # 尝试从导入数据的积分信息初始化 points
                    points_str = ""
                    points_updated = ""
                    remaining = acc_data.get("credits_remaining", 0)
                    total = acc_data.get("credits_total", 0)
                    if total > 0:
                        points_str = f"{remaining:.0f}/{total:.0f}"
                        points_updated = "imported"
                    key_data = {
                        "key_id": f"ck_{secrets.token_hex(4)}",
                        "api_key": api_key,
                        "label": acc_data.get("nickname", "") or acc_data.get("uid", ""),
                        "status": "active",
                        "used_count": 0,
                        "points": points_str,
                        "points_updated_at": points_updated,
                        "created_at": datetime.now().isoformat(),
                    }
                    proxy_db.add_upstream_key(key_data)
                    existing_api_keys.add(api_key)
                    key_pool_count += 1
        except Exception as e:
            logger.exception("自动同步上游 Key 池失败")
            key_pool_error = str(e)  # 不吞错：记录日志并在下方状态栏提示

        # 2. 保存账号到数据库
        count = 0
        last_account = None
        for acc_data in accounts:
            account = Account(
                uid=acc_data.get("uid", ""),
                nickname=acc_data.get("nickname", ""),
                platform=acc_data.get("platform", Platform.CODEBUDDY),
                auth_token=acc_data.get("auth_token", ""),
                auth_raw=acc_data.get("auth_raw", ""),
                domain=acc_data.get("domain", "www.codebuddy.cn"),
                ck=acc_data.get("ck", ""),
                api_key=acc_data.get("api_key", ""),
            )
            if acc_data.get("quota"):
                account.quota = acc_data["quota"]
            save_account(account)
            last_account = account
            count += 1

        if count > 0:
            msg = f"✅ 已导入 {count} 个账号"
            if key_pool_count > 0:
                msg += f"\n🔑 已同步 {key_pool_count} 个 Key 到上游 Key 池"
            if key_pool_error:
                msg += f"\n⚠️ 同步 Key 池失败: {key_pool_error}"
                self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")
                self._status_label.setText(msg)
                self.account_added.emit(last_account)
                return
            self._status_label.setText(msg)
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
            # 通知父页面 AccountsPage 刷新表格
            self.account_added.emit(last_account)
        else:
            self._status_label.setText("⚠️ 没有可导入的账号")
            self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

    def _login_new(self):
        """通过 WorkBuddy 浏览器登录新账号"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from ...modules.oauth import WorkBuddyProcess

        if WorkBuddyProcess.is_running():
            reply = QMessageBox.question(
                self, "需要关闭 WorkBuddy",
                "登录新账号需要：\n\n"
                "1. 关闭 WorkBuddy\n"
                "2. 注销浏览器 SSO 会话\n"
                "3. 清除所有登录数据\n"
                "4. 重启 WorkBuddy 让你登录新账号\n\n"
                "WorkBuddy 关闭后会自动重启，你确定继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._status_label.setText("⏳ 正在关闭 WorkBuddy 并准备登录...")
        self._status_label.setStyleSheet("color: #D69E2E; font-size: 12px;")

        class LoginThread(QThread):
            result_ready = QSignal(object)
            status_update = QSignal(str)

            def run(self):
                result = WorkBuddyAuth.login_new_account(
                    on_status=lambda s: self.status_update.emit(s),
                    timeout=300,
                )
                self.result_ready.emit(result)

        self._login_thread = LoginThread()
        self._login_thread.result_ready.connect(self._on_login_result)
        self._login_thread.status_update.connect(self._on_status_update)
        self._login_thread.start()

    def _on_status_update(self, status_text: str):
        """登录流程状态更新"""
        self._status_label.setText(f"⏳ {status_text}")
        if "❌" in status_text:
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")
        elif "✅" in status_text:
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
        else:
            self._status_label.setStyleSheet("color: #2B6CB0; font-size: 12px;")

    def _on_login_result(self, result):
        """登录结果回调"""
        if result:
            self._token_input.setText(result.get("neodata_token", "") or result.get("access_token", ""))
            self._uid_input.setText(result.get("uid", ""))
            self._nickname_input.setText(result.get("nickname", ""))
            self._status_label.setText(f"✅ 登录成功: {result.get('nickname', '新账号')}")
            self._status_label.setStyleSheet("color: #38A169; font-size: 12px;")
        else:
            self._status_label.setText("❌ 登录超时或失败，请重试")
            self._status_label.setStyleSheet("color: #E53E3E; font-size: 12px;")

    def _save(self):
        """保存账号"""
        if not self._token_input.text() and not self._uid_input.text():
            QMessageBox.warning(self, t("common.warning"), "请先提取或登录账号")
            return

        token = self._token_input.text()
        # 如果 token 以 ck_ 开头，说明是 API Key，同时填到 api_key 字段
        api_key = token if token.startswith("ck_") else ""

        account = Account(
            uid=self._uid_input.text() or f"user_{id(self)}",
            nickname=self._nickname_input.text(),
            platform=self._platform_combo.currentData(),
            auth_token=token,
            api_key=api_key,
        )
        save_account(account)
        self.account_added.emit(account)
        self.accept()


class CreditsDetailDialog(QDialog):
    """积分明细对话框 - 显示每个积分包的详细信息"""

    def __init__(self, account: Account, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📊 积分明细")
        self.setMinimumWidth(620)
        self.setMinimumHeight(400)
        self._account = account
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 标题：手机号/UID
        header = QLabel(f"📱 {self._account.uid}")
        header.setStyleSheet("font-size: 16px; font-weight: 700; padding: 4px 0;")
        layout.addWidget(header)

        # 积分包表格
        self._pkg_table = QTableWidget()
        self._pkg_table.setColumnCount(5)
        self._pkg_table.setHorizontalHeaderLabels(["积分包", "类型", "剩余", "总量", "过期时间"])
        self._pkg_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pkg_table.setAlternatingRowColors(True)
        self._pkg_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pkg_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self._pkg_table)

        # 填充数据
        packages: list[ResourcePackage] = self._account.quota.packages
        self._pkg_table.setRowCount(len(packages))

        total_remain = 0.0
        base_remain = 0.0
        activity_remain = 0.0

        for row, pkg in enumerate(packages):
            self._pkg_table.setItem(row, 0, QTableWidgetItem(pkg.package_name))

            # 类型标签
            type_map = {"1": "基础", "2": "付费", "4": "体验"}
            type_text = type_map.get(pkg.package_type, pkg.package_type)
            self._pkg_table.setItem(row, 1, QTableWidgetItem(type_text))

            # 剩余（用 cycle_remain 周期剩余，capacity_remain 对基础包不更新）
            remain_item = QTableWidgetItem(f"{pkg.cycle_remain:.1f}")
            if pkg.cycle_remain <= 0:
                remain_item.setForeground(Qt.red)
            self._pkg_table.setItem(row, 2, remain_item)

            # 总量
            self._pkg_table.setItem(row, 3, QTableWidgetItem(f"{pkg.cycle_size:.1f}"))

            # 过期时间
            expire_text = self._format_expire(pkg.cycle_end)
            expire_item = QTableWidgetItem(expire_text)
            self._pkg_table.setItem(row, 4, expire_item)

            # 统计（用 cycle_remain 统计）
            total_remain += pkg.cycle_remain
            if pkg.package_type in ("1", "4"):
                base_remain += pkg.cycle_remain
            elif pkg.package_type == "2":
                activity_remain += pkg.cycle_remain
            else:
                activity_remain += pkg.cycle_remain

        # 如果没有积分包数据但有总量信息
        if not packages and (self._account.quota.credits_total > 0 or self._account.quota.credits_remaining > 0):
            total_remain = self._account.quota.credits_remaining
            base_remain = total_remain

        # 汇总信息
        summary_frame = QFrame()
        summary_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(43, 108, 176, 0.06);
                border: 1px solid rgba(43, 108, 176, 0.15);
                border-radius: 8px;
                padding: 10px 16px;
            }
        """)
        summary_layout = QVBoxLayout(summary_frame)
        summary_layout.setContentsMargins(16, 10, 16, 10)
        summary_layout.setSpacing(4)

        total_label = QLabel(f"<b>总剩余:</b> {total_remain:.1f}")
        total_label.setStyleSheet("font-size: 14px;")
        summary_layout.addWidget(total_label)

        detail_parts = []
        if base_remain > 0:
            detail_parts.append(f"基础: {base_remain:.1f}")
        if activity_remain > 0:
            detail_parts.append(f"活动: {activity_remain:.1f}")
        if detail_parts:
            detail_label = QLabel("　".join(detail_parts))
            detail_label.setStyleSheet("color: #5F6B7A; font-size: 12px;")
            summary_layout.addWidget(detail_label)

        layout.addWidget(summary_frame)

        # 关闭按钮
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("primary_btn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

    @staticmethod
    def _format_expire(cycle_end: str) -> str:
        """格式化过期时间，附带剩余天数"""
        if not cycle_end:
            return "-"
        try:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(cycle_end[:19] if len(cycle_end) > 19 else cycle_end, fmt)
                    break
                except ValueError:
                    continue
            else:
                return cycle_end

            now = datetime.now()
            diff = dt - now
            days = diff.days

            time_str = dt.strftime("%Y-%m-%d %H:%M")
            if days < 0:
                return f"{time_str} (已过期)"
            elif days == 0:
                return f"{time_str} (今天过期)"
            else:
                return f"{time_str} ({days}天后)"
        except Exception:
            return cycle_end


class SessionRestoreDialog(QDialog):
    """WorkBuddy 按对话恢复：列出本地全部历史对话，勾选后恢复到当前登录账号名下。

    场景：微信重登/换号后生成了新 uid，旧账号的对话在 UI 不可见（数据没丢，
    只是 sessions.user_id 隔离）。按对话粒度精准改归属，比整账号迁移更灵活。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("💬 恢复 WorkBuddy 历史对话")
        self.resize(660, 540)
        self._restore_thread = None
        self._setup_ui()
        self._load_sessions()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        from ...modules import account_switch

        self._current_uid = account_switch.read_current_workbuddy_uid()
        cur_text = f"{self._current_uid[:8]}…" if self._current_uid else "未识别（请先在 WorkBuddy 登录）"
        info = QLabel(
            f"把选中的对话恢复到当前登录账号（{cur_text}）名下；"
            "操作前自动备份数据库，恢复后需重启 WorkBuddy 客户端可见。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["对话", "最后活跃"])
        self._tree.setColumnWidth(0, 440)
        layout.addWidget(self._tree)

        btn_row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_all.setObjectName("secondary_btn")
        btn_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        btn_row.addWidget(btn_all)
        btn_none = QPushButton("全不选")
        btn_none.setObjectName("secondary_btn")
        btn_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        btn_row.addWidget(btn_none)
        btn_row.addStretch()
        self._btn_restore = QPushButton("✅ 恢复选中对话")
        self._btn_restore.setObjectName("primary_btn")
        self._btn_restore.setCursor(Qt.PointingHandCursor)
        self._btn_restore.clicked.connect(self._restore_selected)
        btn_row.addWidget(self._btn_restore)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _load_sessions(self):
        from ...modules import account_switch

        sessions = account_switch.list_workbuddy_sessions()
        # 按账号分组；当前账号的对话本就在名下，无需恢复，不列出
        groups = {}
        for s in sessions:
            if s["user_id"] == self._current_uid:
                continue
            groups.setdefault(s["user_id"], []).append(s)

        self._tree.clear()
        if not groups:
            item = QTreeWidgetItem(["没有其他账号的历史对话", ""])
            item.setFlags(Qt.NoItemFlags)
            self._tree.addTopLevelItem(item)
            self._btn_restore.setEnabled(False)
            return

        for uid, items in groups.items():
            top = QTreeWidgetItem([f"账号 {uid[:8]}…（{len(items)} 条对话）", ""])
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            top.setCheckState(0, Qt.Unchecked)
            self._tree.addTopLevelItem(top)
            for s in items:
                ts = s.get("updated_at") or 0
                try:
                    time_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    time_str = "-"
                title = s["title"] if len(s["title"]) <= 50 else s["title"][:47] + "..."
                child = QTreeWidgetItem([title, time_str])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked)
                child.setData(0, Qt.UserRole, s["id"])
                top.addChild(child)
            top.setExpanded(True)

    def _set_all(self, state):
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, state)

    def _selected_session_ids(self) -> list:
        ids = []
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.checkState(0) == Qt.Checked:
                    sid = child.data(0, Qt.UserRole)
                    if sid:
                        ids.append(sid)
        return ids

    def _restore_selected(self):
        ids = self._selected_session_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要恢复的对话")
            return
        if not self._current_uid:
            QMessageBox.warning(self, "提示", "未识别到当前登录的 WorkBuddy 账号，请先在客户端登录")
            return

        answer = QMessageBox.question(
            self,
            "恢复对话",
            f"将把选中的 {len(ids)} 条对话恢复到当前账号名下。\n\n"
            "操作前会自动备份数据库。确定继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        from PySide6.QtCore import QThread, Signal as QSignal

        class RestoreThread(QThread):
            result_ready = QSignal(bool, str)

            def __init__(self, session_ids, uid):
                super().__init__()
                self._ids = session_ids
                self._uid = uid

            def run(self):
                from ...modules import account_switch

                try:
                    msg = account_switch.restore_workbuddy_sessions(self._ids, self._uid)
                    self.result_ready.emit(True, msg)
                except Exception as exc:
                    self.result_ready.emit(False, str(exc))

        def _on_result(ok: bool, msg: str):
            self._btn_restore.setEnabled(True)
            if ok:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Information)
                box.setWindowTitle("恢复成功")
                box.setText(msg + "\n\n重启 WorkBuddy 客户端后即可在对话列表看到。")
                box.setTextInteractionFlags(Qt.TextSelectableByMouse)
                backup_path = ""
                for line in msg.splitlines():
                    if line.startswith("备份目录："):
                        backup_path = line.split("：", 1)[1].strip()
                copy_btn = None
                if backup_path:
                    copy_btn = box.addButton("📋 复制备份路径", QMessageBox.ActionRole)
                box.addButton(QMessageBox.Ok)
                box.exec()
                if copy_btn is not None and box.clickedButton() is copy_btn:
                    from PySide6.QtWidgets import QApplication

                    QApplication.clipboard().setText(backup_path)
                self._load_sessions()  # 刷新列表（已恢复的不再出现）
            else:
                QMessageBox.warning(self, "恢复失败", msg)

        self._btn_restore.setEnabled(False)
        thread = RestoreThread(ids, self._current_uid)
        thread.result_ready.connect(_on_result)
        thread.start()
        self._restore_thread = thread  # 防 GC


class AccountsPage(QWidget):
    """账号管理页面"""

    quota_updated = Signal()  # 积分更新信号，通知其他页面刷新

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content_area")
        self._accounts = []
        self._filtered_accounts = []
        self._current_page = 0
        self._sort_column = None
        self._sort_order = Qt.AscendingOrder
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题
        title = QLabel(t("accounts.title"))
        title.setObjectName("page_title")
        layout.addWidget(title)

        subtitle = QLabel("管理所有平台的账号 · 双击行查看积分明细 · 右键更多操作")
        subtitle.setObjectName("page_subtitle")
        layout.addWidget(subtitle)

        # 工具栏
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 0, 32, 32)
        content_layout.setSpacing(16)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.setContentsMargins(0, 0, 0, 0)

        # 平台筛选
        self._filter_combo = QComboBox()
        self._filter_combo.addItem("全部平台", None)
        for p in Platform:
            self._filter_combo.addItem(p.value, p)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._filter_combo.setVisible(False)

        # 按状态一键全选
        self._select_status_combo = QComboBox()
        self._select_status_combo.addItem("按状态全选")
        self._select_status_combo.addItem("· 正常")
        self._select_status_combo.addItem("· 异常")
        self._select_status_combo.addItem("· 未检测")
        self._select_status_combo.addItem("· 有API")
        self._select_status_combo.addItem("· 无API")
        self._select_status_combo.addItem("取消选择")
        self._select_status_combo.setFixedWidth(130)
        self._select_status_combo.setToolTip("按账号状态一键选中对应账号")
        self._select_status_combo.currentIndexChanged.connect(self._on_select_by_status)
        toolbar.addWidget(self._select_status_combo)

        # 搜索框
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 搜索账号昵称/手机号/UID...")
        self._search_input.textChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self._search_input)

        toolbar.addStretch()

        # 批量删除按钮
        self._btn_batch_del = QPushButton("删除")
        self._btn_batch_del.setObjectName("danger_btn")
        self._btn_batch_del.setCursor(Qt.PointingHandCursor)
        self._btn_batch_del.clicked.connect(self._batch_delete)
        self._btn_batch_del.setVisible(False)
        toolbar.addWidget(self._btn_batch_del)

        self._btn_batch_export = QPushButton("📤 导出选中账号")
        self._btn_batch_export.setObjectName("secondary_btn")
        self._btn_batch_export.setCursor(Qt.PointingHandCursor)
        self._btn_batch_export.clicked.connect(self._export_selected_accounts)
        self._btn_batch_export.setVisible(False)
        toolbar.addWidget(self._btn_batch_export)

        # 并发数设置
        toolbar.addWidget(QLabel("并发:"))
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 50)
        self._concurrency_spin.setValue(int(load_setting("account_concurrency", "5")))
        self._concurrency_spin.setToolTip("同时请求线程数，范围 1-50")
        self._concurrency_spin.valueChanged.connect(
            lambda value: save_setting("account_concurrency", str(value))
        )
        self._concurrency_spin.setFixedWidth(60)
        toolbar.addWidget(self._concurrency_spin)

        self._btn_query_all = QPushButton("💎 查询全部账号积分")
        self._btn_query_all.setObjectName("primary_btn")
        self._btn_query_all.setCursor(Qt.PointingHandCursor)
        self._btn_query_all.setToolTip("批量查询所有账号的剩余积分和用量")
        self._btn_query_all.clicked.connect(self._query_all_quotas)
        toolbar.addWidget(self._btn_query_all)

        # 检查账号状态按钮
        self._btn_check_status = QPushButton("🔍 检测账号是否可用")
        self._btn_check_status.setObjectName("secondary_btn")
        self._btn_check_status.setCursor(Qt.PointingHandCursor)
        self._btn_check_status.setToolTip("批量检测所有账号的Token是否被风控/失效，异常的自动标记")
        self._btn_check_status.clicked.connect(self._check_all_status)
        toolbar.addWidget(self._btn_check_status)

        # 停止按钮
        self._btn_stop_query = QPushButton("⏹ 停止查询")
        self._btn_stop_query.setObjectName("secondary_btn")
        self._btn_stop_query.setStyleSheet(
            "QPushButton { color: #FC8181; border: 1px solid #FC8181; }"
            "QPushButton:hover { background-color: rgba(229,62,62,0.1); }"
        )
        self._btn_stop_query.setCursor(Qt.PointingHandCursor)
        self._btn_stop_query.setVisible(False)
        self._btn_stop_query.clicked.connect(self._stop_query)
        toolbar.addWidget(self._btn_stop_query)

        content_layout.addLayout(toolbar)

        # 表格 – 列：☑、昵称、UID、积分、TK、账号状态
        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            "☑", "昵称", "UID", "积分", "TK", "账号状态"
        ])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 勾选列窄
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_sort)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.doubleClicked.connect(self._on_table_double_click)
        # 点表头"☑"列 = 全选/取消全选
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        content_layout.addWidget(self._table, 1)

        # 翻页栏
        pager_row = QHBoxLayout()
        self._btn_prev = QPushButton("◀ 上一页")
        self._btn_prev.setObjectName("secondary_btn")
        self._btn_prev.clicked.connect(self._prev_page)
        pager_row.addWidget(self._btn_prev)

        self._page_label = QLabel("0 / 0")
        self._page_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self._page_label.setAlignment(Qt.AlignCenter)
        pager_row.addWidget(self._page_label)

        self._btn_next = QPushButton("下一页 ▶")
        self._btn_next.setObjectName("secondary_btn")
        self._btn_next.clicked.connect(self._next_page)
        pager_row.addWidget(self._btn_next)

        pager_row.addStretch()

        pager_row.addWidget(QLabel("跳到:"))
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 1)
        self._page_spin.setFixedWidth(70)
        self._page_spin.valueChanged.connect(self._goto_page)
        pager_row.addWidget(self._page_spin)

        content_layout.addLayout(pager_row)

        # 进度条
        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        content_layout.addWidget(self._progress_bar)

        # 查询日志
        self._log_edit = QTextEdit()
        self._log_edit.setObjectName("log_edit")
        self._log_edit.setReadOnly(True)
        self._log_edit.setMaximumHeight(120)
        self._log_edit.setVisible(False)
        content_layout.addWidget(self._log_edit)

        layout.addWidget(content)

    # === 分页逻辑 ===

    @property
    def _total_pages(self) -> int:
        return max(1, (len(self._filtered_accounts) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _get_page_accounts(self) -> list:
        start = self._current_page * PAGE_SIZE
        end = start + PAGE_SIZE
        return self._filtered_accounts[start:end]

    def _update_pager(self):
        total = self._total_pages
        self._page_label.setText(f"{self._current_page + 1} / {total}")
        self._btn_prev.setEnabled(self._current_page > 0)
        self._btn_next.setEnabled(self._current_page < total - 1)
        self._page_spin.setRange(1, total)
        self._page_spin.blockSignals(True)
        self._page_spin.setValue(self._current_page + 1)
        self._page_spin.blockSignals(False)

    def _prev_page(self):
        if self._current_page > 0:
            self._current_page -= 1
            self._render_page()

    def _next_page(self):
        if self._current_page < self._total_pages - 1:
            self._current_page += 1
            self._render_page()

    def _goto_page(self, page: int):
        if page >= 1 and page <= self._total_pages:
            self._current_page = page - 1
            self._render_page()

    # === 数据 & 渲染 ===

    def _load_accounts(self):
        self._accounts = load_accounts()

    def _apply_filter(self):
        search = self._search_input.text().lower()

        filtered = self._accounts
        if search:
            filtered = [a for a in filtered if
                       search in a.nickname.lower() or
                       search in a.uid.lower() or
                       search in a.platform.value or
                       search in a.ck.lower() or
                       search in a.api_key.lower()]

        self._filtered_accounts = filtered
        self._apply_sort()

    def _account_sort_value(self, account: Account, column: int):
        if column == 1:
            return account.display_name.lower()
        if column == 2:
            return account.uid.lower()
        if column == 3:
            return account.quota.credits_remaining
        if column == 4:
            return account.auth_token.lower()
        if column == 5:
            return (
                0 if account.status == AccountStatus.ACTIVE else 1,
                0 if account.api_key else 1,
                account.status.value,
            )
        return ""

    def _apply_sort(self):
        if self._sort_column is None:
            return
        reverse = self._sort_order == Qt.DescendingOrder
        self._filtered_accounts.sort(
            key=lambda account: self._account_sort_value(account, self._sort_column),
            reverse=reverse,
        )

    def _on_header_sort(self, section: int):
        if self._sort_column == section:
            self._sort_order = Qt.DescendingOrder if self._sort_order == Qt.AscendingOrder else Qt.AscendingOrder
        else:
            self._sort_column = section
            self._sort_order = Qt.AscendingOrder
        self._table.horizontalHeader().setSortIndicator(section, self._sort_order)
        self._apply_sort()
        self._current_page = 0
        self._render_page()

    def _render_page(self):
        """只渲染当前页"""
        page_accounts = self._get_page_accounts()
        self._table.setRowCount(len(page_accounts))

        for row, account in enumerate(page_accounts):
            # 勾选框（第0列）
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk_item.setCheckState(Qt.Unchecked)
            self._table.setItem(row, 0, chk_item)

            # 昵称
            self._table.setItem(row, 1, QTableWidgetItem(account.display_name))

            # UID
            self._table.setItem(row, 2, QTableWidgetItem(account.uid))

            # 积分列（未查询的可点击刷新）
            if account.quota.credits_total > 0:
                credits_text = f"{account.quota.credits_remaining:.0f}/{account.quota.credits_total:.0f}"
                credits_item = QTableWidgetItem(credits_text)
                if account.quota.credits_total > 0 and (account.quota.credits_remaining / account.quota.credits_total) < 0.2:
                    credits_item.setForeground(Qt.red)
                elif account.quota.credits_total > 0:
                    credits_item.setForeground(Qt.darkGreen)
                credits_item.setToolTip("双击刷新该账号积分")
            elif account.auth_token:
                credits_item = QTableWidgetItem("🔄 未查询（双击刷新）")
                credits_item.setForeground(Qt.gray)
                credits_item.setToolTip("双击此单元格查询该账号最新积分")
            else:
                credits_item = QTableWidgetItem("无Token")
                credits_item.setForeground(Qt.gray)
            self._table.setItem(row, 3, credits_item)

            # TK列 (auth_token 截断显示)
            tk_text = account.auth_token
            if tk_text:
                tk_display = tk_text[:20] + "..." if len(tk_text) > 20 else tk_text
            else:
                tk_display = ""
            tk_item = QTableWidgetItem(tk_display)
            tk_item.setToolTip(tk_text if tk_text else "")  # 悬停显示完整值
            if not tk_text:
                tk_item.setForeground(Qt.gray)
            self._table.setItem(row, 4, tk_item)

            # 账号状态列：未检测 / 正常 / 异常
            is_normal = account.status == AccountStatus.ACTIVE
            has_quota = account.quota.credits_total > 0
            if not has_quota:
                status_text = "未检测"
                status_color = Qt.gray
            elif is_normal:
                status_text = "正常"
                status_color = Qt.darkGreen
            else:
                status_text = "异常"
                status_color = Qt.red
            api_status_item = QTableWidgetItem(status_text)
            api_status_item.setForeground(status_color)
            if account.status_reason:
                api_status_item.setToolTip(account.status_reason)
            self._table.setItem(row, 5, api_status_item)

        self._update_pager()

    def _refresh_table(self):
        """全量刷新（重新加载+渲染）"""
        self._load_accounts()
        self._apply_filter()
        self._current_page = 0
        self._render_page()

    def _on_filter_changed(self):
        """筛选变化时重置到第一页"""
        self._apply_filter()
        self._current_page = 0
        self._render_page()

    # === 双击/右键操作 ===

    def _on_table_double_click(self, index):
        """双击：积分列/账号状态列（未检测）= 刷新该号积分；其他列 = 查看积分明细"""
        page_accounts = self._get_page_accounts()
        row = index.row()
        if row >= len(page_accounts):
            return
        account = page_accounts[row]
        col = index.column()
        if col == 3:  # 积分列：任意双击都刷新积分
            self._query_single_quota(account)
            return
        if col == 5:  # 账号状态列：未检测的双击=检测（查积分）
            if account.quota.credits_total <= 0:
                self._query_single_quota(account)
                return
        self._show_credits_detail(account)

    def _get_selected_accounts(self) -> list[Account]:
        """获取当前选中的账号列表"""
        page_accounts = self._get_page_accounts()
        selected_rows = set()
        for item in self._table.selectedItems():
            selected_rows.add(item.row())
        accounts = []
        for row in sorted(selected_rows):
            if row < len(page_accounts):
                accounts.append(page_accounts[row])
        return accounts

    def _on_header_clicked(self, section: int):
        """点表头第0列(☑) = 当前页全选/取消全选"""
        if section != 0:
            return
        page_accounts = self._get_page_accounts()
        # 判断当前是否已全勾选
        all_checked = all(
            self._table.item(row, 0) is not None and self._table.item(row, 0).checkState() == Qt.Checked
            for row in range(len(page_accounts))
        ) if page_accounts else False
        new_state = Qt.Unchecked if all_checked else Qt.Checked
        for row in range(len(page_accounts)):
            item = self._table.item(row, 0)
            if item is not None:
                item.setCheckState(new_state)

    def _get_checked_accounts(self) -> list:
        """获取当前页勾选的账号列表"""
        page_accounts = self._get_page_accounts()
        checked = []
        for row, acc in enumerate(page_accounts):
            item = self._table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                checked.append(acc)
        return checked

    def _on_selection_changed(self):
        selected = self._get_selected_accounts()
        # 合并勾选框选中的（勾选也算"选中"）
        checked = self._get_checked_accounts()
        merged = selected + [a for a in checked if a not in selected]
        self._btn_batch_export.setVisible(bool(merged))
        if merged:
            self._btn_batch_export.setText(f"📤 导出选中({len(merged)})")
        if len(merged) > 0:
            self._btn_batch_del.setVisible(True)
            self._btn_batch_del.setText(f"🗑️ 删除勾选({len(merged)})")
        else:
            self._btn_batch_del.setVisible(False)

    def _on_select_by_status(self, index: int):
        """按账号状态一键选中当前页账号"""
        if index == 0:
            return  # 占位项，不触发

        page_accounts = self._get_page_accounts()
        self._table.clearSelection()

        for row, account in enumerate(page_accounts):
            has_api = bool(account.api_key)
            is_normal = account.status == AccountStatus.ACTIVE
            has_quota = account.quota.credits_total > 0
            select = False
            if index == 1:  # 正常
                select = is_normal and has_quota
            elif index == 2:  # 异常
                select = not is_normal
            elif index == 3:  # 未检测
                select = not has_quota
            elif index == 4:  # 有API
                select = has_api
            elif index == 5:  # 无API
                select = not has_api
            elif index == 6:  # 取消选择
                pass  # select remains False, clearSelection already done

            if select:
                for col in range(self._table.columnCount()):
                    self._table.item(row, col).setSelected(True)

        # 复位下拉框
        self._select_status_combo.blockSignals(True)
        self._select_status_combo.setCurrentIndex(0)
        self._select_status_combo.blockSignals(False)

        self._on_selection_changed()

    def _show_context_menu(self, pos):
        """右键菜单"""
        selected = self._get_selected_accounts()
        if not selected:
            return

        menu = QMenu(self)

        if len(selected) == 1:
            account = selected[0]
            action_detail = menu.addAction("📊 查看积分明细")
            action_detail.triggered.connect(lambda: self._show_credits_detail(account))
            menu.addSeparator()
            action_query = menu.addAction("💎 查询积分")
            action_query.triggered.connect(lambda: self._query_single_quota(account))
            menu.addSeparator()
            action_del = menu.addAction("🗑️ 删除账号")
            action_del.triggered.connect(lambda: self._delete_account(account))
        else:
            action_batch = menu.addAction(f"🗑️ 批量删除 ({len(selected)} 个账号)")
            action_batch.triggered.connect(self._batch_delete)

        menu.exec(QCursor.pos())

    def _switch_client_account(self, account: Account, client: str):
        """一键切号：把账号登录态写入 CodeBuddy CN / WorkBuddy 客户端并重启它"""
        client_name = "CodeBuddy CN" if client == "codebuddy_cn" else "WorkBuddy"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("切换账号")
        box.setText(
            f"将把 {client_name} 客户端切换到账号「{account.display_name}」。\n\n"
            f"操作会先退出正在运行的 {client_name}（未保存内容可能丢失），写入登录态后自动重启。\n\n"
            "确定继续吗？"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        keep_cb = None
        if client == "workbuddy":
            from PySide6.QtWidgets import QCheckBox

            keep_cb = QCheckBox("保留当前账号的对话记录（跟随迁移到新账号，自动备份）", box)
            keep_cb.setChecked(True)
            box.setCheckBox(keep_cb)
        box.exec()
        if box.standardButton(box.clickedButton()) != QMessageBox.Yes:
            return
        keep_sessions = bool(keep_cb and keep_cb.isChecked())
        self._run_client_switch(account, client, keep_sessions)

    def _run_client_switch(self, account: Account, client: str, keep_sessions: bool):
        """启动切号线程。确认框之后与「手动指定路径」后复用（后者不再弹确认框）。"""

        from PySide6.QtCore import QThread, Signal as QSignal

        class SwitchThread(QThread):
            result_ready = QSignal(bool, str)

            def __init__(self, acc, which, keep):
                super().__init__()
                self._acc = acc
                self._which = which
                self._keep = keep

            def run(self):
                from ...modules import account_switch

                try:
                    if self._which == "codebuddy_cn":
                        msg = account_switch.switch_to_codebuddy_cn(self._acc)
                    else:
                        msg = account_switch.switch_to_workbuddy(self._acc, keep_sessions=self._keep)
                    self.result_ready.emit(True, msg)
                except Exception as exc:
                    self.result_ready.emit(False, str(exc))

        def _on_result(ok: bool, msg: str):
            if ok:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Information)
                box.setWindowTitle("切换成功")
                box.setText(msg)
                box.setTextInteractionFlags(Qt.TextSelectableByMouse)
                backup_path = ""
                for line in msg.splitlines():
                    if line.startswith("备份目录："):
                        backup_path = line.split("：", 1)[1].strip()
                copy_btn = None
                if backup_path:
                    copy_btn = box.addButton("📋 复制备份路径", QMessageBox.ActionRole)
                box.addButton(QMessageBox.Ok)
                box.exec()
                if copy_btn is not None and box.clickedButton() is copy_btn:
                    self._copy_field(backup_path, "备份路径")
            else:
                from ...modules import account_switch as _as

                if msg.startswith(_as.APP_PATH_NOT_FOUND_PREFIX + "workbuddy"):
                    self._prompt_manual_workbuddy_path(msg, account, keep_sessions)
                else:
                    QMessageBox.warning(self, "切换失败", msg)

        thread = SwitchThread(account, client, keep_sessions)
        thread.result_ready.connect(_on_result)
        thread.start()
        self._switch_thread = thread  # 防 GC

    def _prompt_manual_workbuddy_path(self, msg: str, account: Account, keep_sessions: bool):
        """未找到 WorkBuddy 客户端：引导手动指定路径，保存后直接重试切号。

        复刻 cockpit：路径探测失败不硬猜，给用户手动指定入口，
        选中的路径写入 ~/.antigravity-tools/config.json 的 workbuddy_app_path，
        之后切号优先走该配置，不再报错。

        keep_sessions 从确认框透传：用户取消「保留对话记录」时，重试不得静默改回 True。
        """
        import os as _os
        import sys as _sys

        from ...modules import account_switch as _as

        # 首行是内部错误码（APP_PATH_NOT_FOUND:...），剥掉再上屏
        display = msg
        if display.startswith(_as.APP_PATH_NOT_FOUND_PREFIX + "workbuddy"):
            display = (
                display.split("\n", 1)[1]
                if "\n" in display
                else "未找到 WorkBuddy 客户端"
            )

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("未找到 WorkBuddy 客户端")
        box.setText(
            display
            + "\n\n如果 WorkBuddy 装在其它位置（如 D 盘、便携版），"
            "可以手动指定客户端路径后自动重试。"
        )
        pick_btn = box.addButton("🎯 手动指定路径并重试", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not pick_btn:
            return

        if _sys.platform == "darwin":
            path = QFileDialog.getExistingDirectory(
                self, "选择 WorkBuddy.app（如 /Applications/WorkBuddy.app）", "/Applications"
            )
        else:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "选择 WorkBuddy.exe",
                _os.path.join(_os.environ.get("LOCALAPPDATA", ""), "Programs"),
                "WorkBuddy (*.exe);;所有文件 (*)",
            )
        if not path:
            return

        _as.set_workbuddy_app_path(path)
        # 直接重试，不再重走确认弹窗；keep_sessions 沿用用户在确认框的选择
        self._run_client_switch(account, "workbuddy", keep_sessions)

    def _open_session_restore(self):
        """打开「按对话恢复」弹窗（WorkBuddy 历史对话挑选恢复）"""
        SessionRestoreDialog(self).exec()

    def _copy_inject_script(self, account: Account):
        """复制网页端注入脚本：在 workbuddy.cn / codebuddy.cn 页面按 F12 →
        控制台粘贴回车，页面即使用该账号（无需验证码；刷新页面后失效）。"""
        # 取软件里当前最新的 accessToken（与 _copy_token 同逻辑）
        access_token = account.auth_token
        if account.auth_raw:
            try:
                raw = json.loads(account.auth_raw)
                access_token = raw.get("accessToken") or raw.get("access_token") or access_token
            except Exception:
                pass
        if not access_token:
            return

        from ...modules import browser_inject

        self._copy_field(browser_inject.build_inject_js(access_token), "网页注入脚本")

    def _show_credits_detail(self, account: Account):
        """显示积分明细弹窗"""
        if not account.quota.packages and account.auth_token:
            self._query_and_show_detail(account)
            return

        dialog = CreditsDetailDialog(account, self)
        dialog.exec()

    def _query_and_show_detail(self, account: Account):
        """查询积分后显示明细弹窗"""
        if not account.auth_token:
            QMessageBox.warning(self, "提示", "该账号无 Token，无法查询积分明细")
            return

        from PySide6.QtCore import QThread, Signal as QSignal

        class DetailQueryThread(QThread):
            result_ready = QSignal(object, object)

            def __init__(self, acc):
                super().__init__()
                self._acc = acc

            def run(self):
                client = ApiClient.from_account(self._acc)
                result = client.get_user_resource()
                self.result_ready.emit(self._acc, result)

        thread = DetailQueryThread(account)
        thread.result_ready.connect(self._on_detail_query_result)
        thread.start()
        self._detail_thread = thread

    def _on_detail_query_result(self, account: Account, result: dict):
        if result.get("success"):
            packages = result.get("packages", [])
            remaining = result.get("remaining_credits", 0)
            total = result.get("total_credits", 0)

            account.quota.credits_remaining = remaining
            account.quota.credits_total = total
            account.quota.packages = packages
            account.quota.last_updated = datetime.now()
            save_account(account)

            # 联动更新上游 Key 池
            try:
                from ...modules.proxy_server import ProxyDatabase
                db = ProxyDatabase.get_instance()
                db.sync_quota_to_key(
                    api_key_or_token=getattr(account, "api_key", None) or account.auth_token,
                    remaining_credits=remaining,
                    total_credits=total,
                    packages=packages,
                )
            except Exception:
                pass

            self.quota_updated.emit()  # 通知其他页面刷新

            dialog = CreditsDetailDialog(account, self)
            dialog.exec()
            self._render_page()
        else:
            QMessageBox.warning(self, "查询失败", "无法获取积分明细，请检查 Token 是否有效")

    def _query_single_quota(self, account: Account):
        """查询单个账号的积分（右键触发）"""
        if not account.auth_token:
            QMessageBox.warning(self, "提示", "该账号无 Token，无法查询积分")
            return

        from PySide6.QtCore import QThread, Signal as QSignal

        class QuotaThread(QThread):
            result_ready = QSignal(object, object)  # (account, result_dict)

            def __init__(self, acc):
                super().__init__()
                self._acc = acc

            def run(self):
                client = ApiClient.from_account(self._acc)
                result = client.get_user_resource()
                self.result_ready.emit(self._acc, result)

        thread = QuotaThread(account)
        thread.result_ready.connect(self._on_single_quota_result)
        thread.start()
        self._quota_thread = thread

    def _on_single_quota_result(self, account: Account, result: dict):
        """单号积分查询结果"""
        if result.get("success"):
            packages = result.get("packages", [])
            remaining = result.get("remaining_credits", 0)
            total = result.get("total_credits", 0)

            # 通过 UID 匹配更新
            for acc in self._accounts:
                if acc.uid == account.uid:
                    acc.quota.credits_remaining = remaining
                    acc.quota.credits_total = total
                    acc.quota.packages = packages
                    acc.quota.last_updated = datetime.now()
                    save_account(acc)
                    # 联动更新上游 Key 池
                    try:
                        from ...modules.proxy_server import ProxyDatabase
                        db = ProxyDatabase.get_instance()
                        db.sync_quota_to_key(
                            api_key_or_token=getattr(acc, "api_key", None) or acc.auth_token,
                            remaining_credits=remaining,
                            total_credits=total,
                            packages=packages,
                        )
                    except Exception:
                        pass
                    self.quota_updated.emit()  # 通知其他页面刷新
                    break

            self._apply_filter()
            self._render_page()
        else:
            QMessageBox.warning(self, "查询失败", "无法获取积分，请检查 Token 是否有效")

    def _query_all_quotas(self):
        """批量查询所有账号积分 — 并发执行"""
        self._load_accounts()
        accounts_with_token = [a for a in self._accounts if a.auth_token]
        if not accounts_with_token:
            return

        max_workers = self._concurrency_spin.value()

        self._btn_query_all.setVisible(False)
        self._btn_stop_query.setVisible(True)

        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, len(accounts_with_token))
        self._progress_bar.setValue(0)

        self._log_edit.clear()
        self._log_edit.setVisible(True)
        self._append_log(f"🚀 开始查询 {len(accounts_with_token)} 个账号积分，并发数: {max_workers}")

        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed

        class BatchQuotaWorker(QThread):
            progress = QSignal(str, bool)  # uid, success
            finished_all = Signal()

            def __init__(self, accs, max_workers=5):
                super().__init__()
                self._accounts = accs
                self.max_workers = max_workers
                self._stop_flag = False

            def stop(self):
                self._stop_flag = True

            def _query_one(self, acc):
                try:
                    client = ApiClient.from_account(acc)
                    result = client.get_user_resource()
                    result["uid"] = acc.uid
                    return (acc.uid, result)
                except Exception as e:
                    return (acc.uid, {"success": False, "uid": acc.uid, "error": str(e)})

            def run(self):
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._query_one, acc): acc
                               for acc in self._accounts}
                    for future in as_completed(futures):
                        if self._stop_flag:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            uid, result = future.result()
                            self.progress.emit(uid, result.get("success", False))
                            # 更新数据
                            if result.get("success"):
                                for acc in self._accounts:
                                    if acc.uid == uid:
                                        remaining = result.get("remaining_credits", 0)
                                        total = result.get("total_credits", 0)
                                        acc.quota.credits_remaining = remaining
                                        acc.quota.credits_total = total
                                        acc.quota.packages = result.get("packages", [])
                                        acc.quota.last_updated = datetime.now()
                                        save_account(acc)
                                        # 联动更新上游 Key 池
                                        try:
                                            from ...modules.proxy_server import ProxyDatabase
                                            db = ProxyDatabase.get_instance()
                                            db.sync_quota_to_key(
                                                api_key_or_token=getattr(acc, "api_key", None) or acc.auth_token,
                                                remaining_credits=remaining,
                                                total_credits=total,
                                                packages=result.get("packages", []),
                                            )
                                        except Exception:
                                            pass
                                        break
                        except Exception:
                            pass
                self.finished_all.emit()

        self._batch_worker = BatchQuotaWorker(accounts_with_token, max_workers=max_workers)
        self._batch_worker.progress.connect(self._on_batch_quota_progress)
        self._batch_worker.finished_all.connect(self._on_batch_quota_done)
        self._batch_worker.start()

    def _stop_query(self):
        """停止查询/检测"""
        if hasattr(self, '_batch_worker') and self._batch_worker:
            self._batch_worker.stop()
            self._append_log("⏹ 正在停止查询...")
        if hasattr(self, '_status_check_worker') and self._status_check_worker:
            self._status_check_worker.stop()
            self._append_log("⏹ 正在停止检测...")
        self._btn_stop_query.setEnabled(False)

    def _append_log(self, text: str):
        """追加日志并自动滚到底部"""
        self._log_edit.append(text)
        scrollbar = self._log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_batch_quota_progress(self, uid: str, success: bool):
        """批量查询进度"""
        current = self._progress_bar.value() + 1
        self._progress_bar.setValue(current)
        icon = "✅" if success else "❌"
        self._append_log(f"{icon} {uid[:12]}... {'成功' if success else '失败'}")

    def _on_batch_quota_done(self):
        """批量查询完成"""
        self._progress_bar.setVisible(False)
        self._btn_query_all.setVisible(True)
        self._btn_stop_query.setVisible(False)
        self._btn_stop_query.setEnabled(True)
        self._append_log("📊 查询完成！")
        self._apply_filter()
        self._render_page()
        self.quota_updated.emit()  # 通知其他页面刷新

    def _check_all_status(self):
        """检查所有账号的凭证状态（风控/失效），ck_ 卡密与纯 JWT 账号都覆盖，同步到上游 Key 池和账号表"""
        from PySide6.QtCore import QThread, Signal as QSignal
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._load_accounts()
        # ck_ 卡密或 JWT（auth_token）任一存在即可检测：裸 JWT 在 chat 端点同样可用（实测 200）
        accounts_with_key = [a for a in self._accounts if a.api_key or a.auth_token]
        if not accounts_with_key:
            QMessageBox.information(self, "提示", "没有配置 API Key 或 Token 的账号，无需检测")
            return

        max_workers = self._concurrency_spin.value()
        self._btn_check_status.setEnabled(False)
        self._btn_query_all.setEnabled(False)
        self._btn_stop_query.setVisible(True)
        self._btn_stop_query.setEnabled(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, len(accounts_with_key))
        self._progress_bar.setValue(0)
        self._log_edit.clear()
        self._log_edit.setVisible(True)
        self._append_log(f"🔍 开始检测 {len(accounts_with_key)} 个账号状态，并发数: {max_workers}")

        class StatusCheckWorker(QThread):
            """后台并发检测凭证（卡密/JWT）风控状态线程"""
            progress = QSignal(str, bool, str)  # nickname, success, status_text
            # (正常, 异常, 失败, 异常列表, 限流列表, 正常列表)；列表元素为 (凭证, uid)
            done = QSignal(int, int, int, list, list, list)

            def __init__(self, accounts, max_workers=5):
                super().__init__()
                self._accounts = accounts
                self.max_workers = max_workers
                self._stop_flag = False

            def stop(self):
                self._stop_flag = True

            def _check_one(self, acc):
                # 纯 Token 账号（无 ck_ 卡密）回退用 auth_token（与池同步同款回退）
                api_key = acc.api_key or acc.auth_token
                nickname = acc.nickname or acc.uid
                try:
                    # 纯 JWT 账号临期/过期先续期再检测，避免把可续期账号误判成限流
                    if not acc.api_key and api_key.startswith("eyJ"):
                        from ...modules.proxy_server import ensure_fresh_jwt
                        api_key = ensure_fresh_jwt(acc)
                    result = check_api_key_chat_status(api_key, attempts=3, uid=acc.uid)
                    return (
                        nickname,
                        result.get("success", False),
                        result.get("status_text", "check_failed"),
                        api_key,
                        result.get("flag"),
                        acc.uid,
                    )
                except Exception as e:
                    return (nickname, False, f"异常: {e}", api_key, None, acc.uid)

            def run(self):
                normal = 0
                abnormal = 0
                failed = 0
                abnormal_keys = []
                rate_limited_keys = []
                ok_keys = []
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._check_one, acc): acc
                               for acc in self._accounts}
                    for future in as_completed(futures):
                        if self._stop_flag:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            nickname, success, status_text, api_key, flag, uid = future.result()
                            self.progress.emit(nickname, success, status_text)
                            # 列表元素为 (凭证, uid)：uid 用于写回账号表
                            #（JWT 续期后凭证已换新，按凭证反查 uid 会丢，必须直接带回来）
                            if flag == "abnormal":
                                abnormal += 1
                                abnormal_keys.append((api_key, uid))
                            elif flag == "rate_limited":
                                abnormal += 1
                                rate_limited_keys.append((api_key, uid))
                            elif success:
                                normal += 1
                                ok_keys.append((api_key, uid))
                            else:
                                failed += 1
                        except Exception:
                            failed += 1
                self.done.emit(normal, abnormal, failed, abnormal_keys, rate_limited_keys, ok_keys)

        worker = StatusCheckWorker(accounts_with_key, max_workers=max_workers)

        def _on_progress(nickname, success, status_text):
            current = self._progress_bar.value() + 1
            self._progress_bar.setValue(current)
            icon = "✅" if success else ("⚠️" if status_text in ("风控异常", "限流(401)") else "❌")
            self._append_log(f"{icon} {nickname} → {status_text}")

        def _on_done(normal, abnormal, failed, abnormal_keys, rate_limited_keys, ok_keys):
            self._btn_check_status.setEnabled(True)
            self._btn_query_all.setEnabled(True)
            self._btn_stop_query.setVisible(False)
            self._btn_stop_query.setEnabled(True)
            self._progress_bar.setVisible(False)

            # 同步到上游 Key 池（按凭证字符串匹配池 key）
            try:
                from ...modules.proxy_server import ProxyDatabase
                abnormal_apis = {cred for cred, _ in abnormal_keys}
                rate_limited_apis = {cred for cred, _ in rate_limited_keys}
                proxy_db = ProxyDatabase.get_instance()
                all_keys = proxy_db.get_upstream_keys()
                for k in all_keys:
                    k_api = k.get("api_key", "")
                    k_id = k.get("key_id", "")
                    if k_api in abnormal_apis and k.get("status") != "abnormal":
                        proxy_db.update_upstream_key(k_id, {"status": "abnormal"})
                    elif k_api in rate_limited_apis and k.get("status") != "rate_limited":
                        proxy_db.update_upstream_key(k_id, {"status": "rate_limited"})
                    elif (k_api not in abnormal_apis
                          and k_api not in rate_limited_apis
                          and k.get("status") in ("abnormal", "rate_limited")):
                        # 之前异常/限流，本次检测通过 → 恢复 active
                        proxy_db.update_upstream_key(k_id, {"status": "active"})
                proxy_db._dirty = True
                proxy_db._flush_to_disk()
                self._append_log("✅ 上游 Key 池已同步")
            except Exception as e:
                self._append_log(f"⚠️ 同步上游池失败: {e}")

            # 检测结果写回账号表（状态列/按状态全选 依赖它；uid 由 worker 直接带回）
            try:
                from ...utils.store import update_account_status
                for _, uid in abnormal_keys:
                    if uid:
                        update_account_status(uid, AccountStatus.ERROR, "风控异常")
                for _, uid in rate_limited_keys:
                    if uid:
                        update_account_status(uid, AccountStatus.ERROR, "限流(401)")
                for _, uid in ok_keys:
                    if uid:
                        update_account_status(uid, AccountStatus.ACTIVE, "")
                self._load_accounts()
                self._apply_filter()
                self._render_page()
            except Exception as e:
                self._append_log(f"⚠️ 写回账号状态失败: {e}")

            rate_limited_count = len(rate_limited_keys)
            msg = f"检测完成：✅ 正常 {normal} 个"
            if abnormal > 0:
                msg += f"，⚠️ 异常 {abnormal} 个（已标记到上游池）"
            if rate_limited_count > 0:
                msg += f"，⚠️ 限流 {rate_limited_count} 个（已标记限流）"
            if failed > 0:
                msg += f"，❓ 失败 {failed} 个"
            self._append_log(msg)
            QMessageBox.information(self, "检测完成", msg)

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        self._status_check_worker = worker
        worker.start()

    def _copy_field(self, value: str, label: str):
        """复制指定字段到剪贴板"""
        if not value:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(value)

    def _copy_token(self, account: Account):
        """按 Token 导入格式复制账号的最新凭证：昵称----accessToken----refreshToken。

        取的是软件里当前最新的 token（JWT 自动续期后已是新值），
        无 refreshToken 时退化为两段：昵称----accessToken。
        """
        access_token, refresh_token = account.auth_token, ""
        if account.auth_raw:
            try:
                raw = json.loads(account.auth_raw)
                access_token = raw.get("accessToken") or raw.get("access_token") or access_token
                refresh_token = raw.get("refreshToken") or raw.get("refresh_token") or ""
            except Exception:
                pass
        if not access_token:
            return
        nickname = account.nickname or account.uid or ""
        text = f"{nickname}----{access_token}----{refresh_token}" if refresh_token else f"{nickname}----{access_token}"
        self._copy_field(text, "Token")

    def _export_selected_accounts(self):
        selected = self._get_selected_accounts()
        rows = [
            f"{account.display_name}----{account.api_key}"
            for account in selected
            if account.api_key
        ]
        if not selected:
            return
        if not rows:
            QMessageBox.warning(self, t("common.warning"), "选中的账号没有可导出的 API Key")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出账号 API Key",
            "accounts_api_keys.txt",
            "Text Files (*.txt);;All Files (*)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(rows))
            QMessageBox.information(self, "导出完成", f"已导出 {len(rows)} 个 API Key")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{e}")

    def _sync_delete_key_pool(self, account: Account):
        """删除账号时同步删除 Key 池中对应的 Key"""
        try:
            from ...modules.proxy_server import ProxyDatabase
            proxy_db = ProxyDatabase.get_instance()
            keys = proxy_db.get_upstream_keys()
            # 用 api_key 或 auth_token 匹配
            tokens_to_remove = set()
            if account.api_key:
                tokens_to_remove.add(account.api_key)
            if account.auth_token:
                tokens_to_remove.add(account.auth_token)
            for k in keys:
                if k.get("api_key", "") in tokens_to_remove:
                    proxy_db.delete_upstream_key(k["key_id"])
        except Exception:
            pass  # Key池删除失败不影响账号删除

    def _delete_account(self, account: Account):
        reply = QMessageBox.question(
            self, t("common.confirm"),
            f"确定要删除账号 {account.display_name} 吗？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # 同步删除 Key 池中对应的 Key
            self._sync_delete_key_pool(account)
            delete_account(account.uid)
            self._refresh_table()

    def _batch_delete(self):
        selected = self._get_selected_accounts()
        if not selected:
            return

        names = [a.display_name for a in selected]
        if len(names) <= 10:
            name_list = "\n".join(f"  • {n}" for n in names)
        else:
            name_list = "\n".join(f"  • {n}" for n in names[:10])
            name_list += f"\n  ... 还有 {len(names) - 10} 个账号"

        reply = QMessageBox.question(
            self, "确认批量删除",
            f"确定要删除以下 {len(selected)} 个账号吗？\n\n{name_list}\n\n"
            f"此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            for account in selected:
                self._sync_delete_key_pool(account)
                delete_account(account.uid)
            self._refresh_table()

    def _add_account(self):
        """卡密激活 — 输入卡密下载账号包"""
        dialog = CardKeyFetchDialog(self)
        dialog.accounts_imported.connect(self._on_card_pack_imported_page)
        dialog.exec()

    def _on_card_pack_imported_page(self, accounts: list):
        """卡密导入到 AccountsPage（复用 _on_batch_accounts_imported_page + 自动刷新 + 自动回传）"""
        self._on_batch_accounts_imported_page(accounts)
        if accounts:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(800, self._query_all_quotas)
            QTimer.singleShot(5000, self._auto_sync_to_server)

    def _auto_sync_to_server(self):
        """积分刷新后自动回传账号状态到网页端（客户端↔网页端关联）"""
        try:
            from ...modules.card_client import auto_sync_from_local_db
            import sqlite3, os
            db_path = os.path.expanduser("~/.flash-connector/flash.db")
            if not os.path.exists(db_path):
                db_path = os.path.expanduser("~/.antigravity-tools/antigravity.db")
            if not os.path.exists(db_path):
                return
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            try:
                rows = c.execute("SELECT DISTINCT account_group FROM accounts WHERE account_group LIKE 'WK-%'").fetchall()
            except Exception:
                rows = []
            conn.close()
            for (card_key,) in rows:
                if card_key and card_key.startswith("WK-"):
                    result = auto_sync_from_local_db(card_key)
                    if result.get("ok"):
                        logger.info(f"状态回传成功: {card_key} ({result.get('updated',0)}个账号)")
        except Exception:
            logger.exception("自动回传状态失败（不影响本地使用）")

    def _on_batch_accounts_imported_page(self, accounts: list):
        """批量导入到 AccountsPage 级别（Key池同步 + 入库 + 刷新表格）"""
        if not accounts:
            return
        from ...modules.proxy_server import ProxyDatabase
        from ...utils.store import save_account
        from ...models import Account, Platform
        import secrets
        from datetime import datetime

        proxy_db = ProxyDatabase.get_instance()
        existing_keys = proxy_db.get_upstream_keys()
        existing_api_keys = {k.get("api_key", "") for k in existing_keys}

        for acc_data in accounts:
            api_key = acc_data.get("api_key", "") or acc_data.get("auth_token", "")
            if api_key and api_key not in existing_api_keys:
                # 卡密包自带服务器实时积分 → Key池points直接初始化（无积分等800ms后自动查分补）
                points_str = ""
                points_updated = ""
                cr = acc_data.get("credits_remaining", 0)
                ct = acc_data.get("credits_total", 0)
                if cr and cr > 0:
                    points_str = f"{cr}/{ct if ct > 0 else cr}"
                    points_updated = "imported"
                key_data = {
                    "key_id": f"ck_{secrets.token_hex(4)}",
                    "api_key": api_key,
                    "label": acc_data.get("nickname", "") or acc_data.get("uid", ""),
                    "status": "active",
                    "used_count": 0,
                    "points": points_str,
                    "points_updated_at": points_updated,
                    "created_at": datetime.now().isoformat(),
                }
                proxy_db.add_upstream_key(key_data)
                existing_api_keys.add(api_key)

            # platform 可能是字符串 "CODEBUDDY" 或 Platform enum，统一转换
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
                # 卡密来源标记：account_group=卡密号（供下载前本地核验/状态回传用）
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
                raise

        self._refresh_table()
        self.quota_updated.emit()

    def _import_batch(self):
        """从文件批量导入账号（支持 JSON 含 api_key / 纯 api_key 列表 / JWT Token）"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择账号文件", "",
            "JSON 文件 (*.json);;文本文件 (*.txt);;CSV 文件 (*.csv);;所有文件 (*)"
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"无法读取文件：{e}")
            return

        added = 0
        skipped = 0
        updated = 0

        # 优先尝试 JSON 格式
        try:
            import json
            data = json.loads(content)
            if isinstance(data, list):
                for item in data:
                    try:
                        # 支持字段：api_key / auth_token / access_token / uid / nickname / sub / preferred_username
                        token = item.get("auth_token", "") or item.get("access_token", "")
                        api_key = item.get("api_key", "")
                        uid = item.get("uid", "") or item.get("sub", "")
                        nickname = item.get("nickname", "") or item.get("preferred_username", "")

                        if not token and not api_key and not uid:
                            skipped += 1
                            continue

                        # 优先用 api_key（ck_xxx），其次 token
                        effective_credential = api_key or token

                        # JSON里没有uid就从token解析
                        if not uid and token and not token.startswith("ck_"):
                            from ...modules.oauth import decode_jwt
                            payload = decode_jwt(token)
                            uid = payload.get("sub", "")
                            nickname = nickname or payload.get("preferred_username", "")

                        if not uid:
                            # ck_ 开头的 api_key，uid 必填或用 api_key 前缀
                            if api_key:
                                uid = item.get("uid", "") or f"api_{api_key[3:11]}"
                                nickname = nickname or uid
                            else:
                                skipped += 1
                                continue

                        # 检查是否已存在（按uid去重）
                        existing = [a for a in load_accounts() if a.uid == uid]
                        account = Account(
                            uid=uid,
                            nickname=nickname or uid,
                            platform=Platform.CODEBUDDY,
                            auth_token=effective_credential,
                            api_key=api_key,
                        )
                        save_account(account)

                        # 同步导入到上游 Key 池
                        if api_key:
                            try:
                                from ...modules.proxy_server import ProxyDatabase
                                proxy_db = ProxyDatabase.get_instance()
                                existing_keys = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
                                if api_key not in existing_keys:
                                    import secrets as _sec
                                    proxy_db.add_upstream_key({
                                        "key_id": f"ck_{_sec.token_hex(4)}",
                                        "api_key": api_key,
                                        "label": uid,
                                        "status": "active",
                                        "points": "",
                                        "points_updated_at": "",
                                        "packages": [],
                                        "created_at": "",
                                    })
                                    proxy_db._dirty = True
                                    proxy_db._flush_to_disk()
                            except Exception:
                                pass

                        if existing:
                            updated += 1
                        else:
                            added += 1
                    except Exception:
                        skipped += 1

                self._refresh_table()
                msg = f"✅ 成功导入 {added} 个账号"
                if updated:
                    msg += f"\n🔄 更新 {updated} 个已有账号"
                if skipped:
                    msg += f"\n⚠️ 跳过 {skipped} 个（无效数据）"
                QMessageBox.information(self, "导入完成", msg)
                return
        except (json.JSONDecodeError, TypeError):
            pass  # 不是JSON，尝试文本格式

        # 文本格式：每行一个 Token 或 API Key，支持 "手机号----apikey" 格式
        tokens = []
        for line in content.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if "----" in line:
                # 格式：手机号----apikey
                parts = line.split("----")
                if len(parts) >= 2:
                    phone = parts[0].strip().strip('"').strip("'")
                    api_key = parts[1].strip().strip('"').strip("'")
                    if phone and api_key:
                        tokens.append({"phone": phone, "api_key": api_key})
                        continue
            if "," in line:
                for part in line.split(","):
                    part = part.strip().strip('"').strip("'")
                    if part:
                        tokens.append(part)
            else:
                tokens.append(line)

        if not tokens:
            QMessageBox.warning(self, "导入失败", "文件中没有找到有效的 Token 或 API Key")
            return

        from ...modules.oauth import decode_jwt

        for token in tokens:
            try:
                # 支持 "手机号----apikey" 格式（dict）
                if isinstance(token, dict):
                    phone = token["phone"]
                    api_key = token["api_key"]
                    uid = phone
                    nickname = phone
                    account = Account(
                        uid=uid,
                        nickname=nickname,
                        platform=Platform.CODEBUDDY,
                        auth_token=api_key,
                        api_key=api_key,
                    )
                    save_account(account)
                    # 导入上游池
                    try:
                        from ...modules.proxy_server import ProxyDatabase
                        proxy_db = ProxyDatabase.get_instance()
                        existing_keys = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
                        if api_key not in existing_keys:
                            import secrets as _sec
                            proxy_db.add_upstream_key({
                                "key_id": f"ck_{_sec.token_hex(4)}",
                                "api_key": api_key,
                                "label": phone,
                                "status": "active",
                                "points": "",
                                "points_updated_at": "",
                                "packages": [],
                                "created_at": "",
                            })
                            proxy_db._dirty = True
                            proxy_db._flush_to_disk()
                    except Exception:
                        pass
                    added += 1
                    continue

                # ck_ 开头按 API Key 处理
                if token.startswith("ck_"):
                    uid = f"api_{token[3:11]}"
                    nickname = uid
                    account = Account(
                        uid=uid,
                        nickname=nickname,
                        platform=Platform.CODEBUDDY,
                        auth_token=token,
                        api_key=token,
                    )
                    save_account(account)
                    # 导入上游池
                    try:
                        from ...modules.proxy_server import ProxyDatabase
                        proxy_db = ProxyDatabase.get_instance()
                        existing_keys = {k.get("api_key", "") for k in proxy_db.get_upstream_keys()}
                        if token not in existing_keys:
                            import secrets as _sec
                            proxy_db.add_upstream_key({
                                "key_id": f"ck_{_sec.token_hex(4)}",
                                "api_key": token,
                                "label": uid,
                                "status": "active",
                                "points": "",
                                "points_updated_at": "",
                                "packages": [],
                                "created_at": "",
                            })
                            proxy_db._dirty = True
                            proxy_db._flush_to_disk()
                    except Exception:
                        pass
                    added += 1
                else:
                    # JWT Token
                    payload = decode_jwt(token)
                    uid = payload.get("sub", "")
                    nickname = payload.get("preferred_username", "")
                    if not uid:
                        skipped += 1
                        continue
                    account = Account(
                        uid=uid,
                        nickname=nickname,
                        platform=Platform.CODEBUDDY,
                        auth_token=token,
                    )
                    save_account(account)
                    added += 1
            except Exception:
                skipped += 1

        self._refresh_table()
        msg = f"✅ 成功导入 {added} 个账号"
        if skipped:
            msg += f"\n⚠️ 跳过 {skipped} 个（无效 Token/API Key）"
        QMessageBox.information(self, "导入完成", msg)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_table()


class ServerFetchDialog(QDialog):
    """从服务器批量获取账号对话框 — 大输入框 + 进度条 + 防卡死
    只获取账号凭证信息（CK/TK/API Key），不查积分
    """

    accounts_imported = Signal(list)  # 传入 List[dict]

    SERVER_URL = "http://103.36.63.44:9658"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🌐 从服务器获取账号")
        self.setMinimumSize(680, 620)
        self._cancel_requested = False
        # macOS 修复：嵌套 QDialog 内 QTextEdit 无法接收键盘输入
        # 原因：macOS 上 Qt 的嵌套 QDialog 会拦截子控件的键盘事件
        # 解决：设为独立窗口，让内部控件能正常接收键盘输入
        import sys
        if sys.platform == "darwin":
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ─── 说明 ───
        hint = QLabel(
            "输入卡密批量获取账号凭证（CK/TK/API Key），每行一个。支持格式：\n"
            "• 16位数字卡密\n"
            "• 手机号----登录URL\n"
            "• 子API Key (sk_xxx)"
        )
        hint.setObjectName("inline_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ─── 大输入框 ───
        self._input = QTextEdit()
        self._input.setPlaceholderText(
            "每行一个卡密，例如：\n"
            "1234567890123456\n"
            "13800138000----https://copilot.tencent.com/login?platform=xxx&state=yyy\n"
            "sk_abc123def456"
        )
        self._input.setMinimumHeight(200)
        layout.addWidget(self._input, 1)

        # ─── 进度区域 ───
        prog_box = QVBoxLayout()
        prog_box.setSpacing(6)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #2B6CB0; font-size: 12px; font-weight: 600;")
        prog_box.addWidget(self._progress_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%v/%m (%p%)")
        self._progress_bar.setVisible(False)
        prog_box.addWidget(self._progress_bar)

        self._detail_label = QLabel("")
        self._detail_label.setStyleSheet("color: #718096; font-size: 11px;")
        self._detail_label.setWordWrap(True)
        self._detail_label.setMaximumHeight(120)
        prog_box.addWidget(self._detail_label)

        layout.addLayout(prog_box)

        # ─── 结果表格 ───
        self._result_table = QTableWidget()
        self._result_table.setColumnCount(4)
        self._result_table.setHorizontalHeaderLabels(["手机号", "API Key", "登录URL", "状态"])
        self._result_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._result_table.setAlternatingRowColors(True)
        self._result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._result_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._result_table.setMaximumHeight(200)
        self._result_table.setVisible(False)
        layout.addWidget(self._result_table)

        # ─── 按钮 ───
        btn_row = QHBoxLayout()

        self._btn_fetch = QPushButton("🚀 开始获取")
        self._btn_fetch.setObjectName("primary_btn")
        self._btn_fetch.setMinimumHeight(36)
        self._btn_fetch.clicked.connect(self._start_fetch)
        btn_row.addWidget(self._btn_fetch)

        self._btn_cancel = QPushButton("⏹ 取消")
        self._btn_cancel.setObjectName("secondary_btn")
        self._btn_cancel.setMinimumHeight(36)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._cancel_fetch)
        btn_row.addWidget(self._btn_cancel)

        self._btn_import = QPushButton("📥 导入选中")
        self._btn_import.setObjectName("primary_btn")
        self._btn_import.setMinimumHeight(36)
        self._btn_import.setEnabled(False)
        self._btn_import.clicked.connect(self._import_selected)
        btn_row.addWidget(self._btn_import)

        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("secondary_btn")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    # ─── 解析输入 ───

    def _parse_lines(self, text: str) -> list:
        """解析多行输入，每行一个卡密，自动识别格式"""
        items = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("sk_"):
                items.append({"sub_api_key": line, "_raw": line})
            elif "----" in line:
                items.append({"phone_url": line, "_raw": line})
            else:
                items.append({"card_code": line, "_raw": line})
        return items

    # ─── 启动获取 ───

    def _start_fetch(self):
        text = self._input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请输入卡密")
            return

        items = self._parse_lines(text)
        if not items:
            QMessageBox.warning(self, "提示", "未识别到有效卡密")
            return

        self._items = items
        self._results = []  # List[dict] 每个账号的结果
        self._cancel_requested = False

        # UI切换到工作状态
        self._btn_fetch.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._btn_import.setEnabled(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setMaximum(len(items))
        self._progress_bar.setValue(0)
        self._progress_label.setText(f"⏳ 准备获取 {len(items)} 个卡密的账号信息...")
        self._detail_label.setText("")
        self._result_table.setRowCount(0)
        self._result_table.setVisible(False)

        from PySide6.QtCore import QThread, Signal as QSignal

        class BatchFetchThread(QThread):
            """后台批量获取线程 — 只获取凭证，不查积分，防卡死"""
            progress = QSignal(int, str)         # current_index, status_text
            item_done = QSignal(int, dict)       # index, result_dict
            all_done = QSignal(list)             # all results

            SERVER_URL = "http://103.36.63.44:9658"

            def __init__(self, items):
                super().__init__()
                self._items = items
                self._cancelled = False

            def cancel(self):
                self._cancelled = True

            def _fetch_one(self, item: dict) -> dict:
                """获取单个卡密的账号凭证（CK/TK/API Key），不查积分"""
                import requests, json

                raw = item.get("_raw", "")
                result = {"raw": raw, "success": False, "phone": "", "api_key": "",
                          "login_url": "", "error": ""}

                # ─── 展开子API Key ───
                query_items = []
                if "sub_api_key" in item:
                    try:
                        resp = requests.post(
                            f"{self.SERVER_URL}/api/get_active_keys",
                            json={"sub_api_key": item["sub_api_key"]},
                            headers={"Content-Type": "application/json"},
                            timeout=15,
                        )
                        data = resp.json()
                        if not data.get("success"):
                            result["error"] = f"子Key验证失败: {data.get('message', '未知')}"
                            return result
                        for ak in (data.get("active_keys") or []):
                            phone = ak.get("phone", "")
                            api_url = ak.get("api_url", "")
                            if phone:
                                query_items.append({"phone_url": f"{phone}----{api_url}" if api_url else phone, "_phone": phone, "_api_url": api_url})
                        if not query_items:
                            result["error"] = "子Key无活跃主Key"
                            return result
                    except Exception as e:
                        result["error"] = f"子Key异常: {e}"
                        return result
                else:
                    query_items = [item]

                # ─── 提交 web_batch_query 获取手机号 ───
                try:
                    resp = requests.post(
                        f"{self.SERVER_URL}/api/web_batch_query",
                        json={"items": query_items},
                        headers={"Content-Type": "application/json"},
                        timeout=30,
                    )
                except requests.ConnectionError:
                    result["error"] = "连接失败"
                    return result
                except requests.Timeout:
                    result["error"] = "提交超时"
                    return result

                if not resp.ok:
                    result["error"] = f"HTTP {resp.status_code}"
                    return result

                data = resp.json()
                if not data.get("success"):
                    result["error"] = data.get("message", "查询失败")
                    return result

                # 从 results 中提取手机号和 key（不需要等SSE查分完成）
                accounts_found = []
                results_list = data.get("results", [])
                for r in results_list:
                    if r.get("success"):
                        phone = r.get("phone", "")
                        key = r.get("key", "")
                        api_key = r.get("api_key", "")  # web_batch_query 已返回 api_key，直接取
                        accounts_found.append({"phone": phone, "key": key, "api_key": api_key})
                    else:
                        # 某项失败
                        result["error"] = r.get("message", "卡密错误")
                        return result

                if not accounts_found:
                    result["error"] = "未获取到账号信息"
                    return result

                # ─── 获取 API Key ───
                api_keys_map = {}  # phone -> api_key
                try:
                    credentials = []
                    for qi in query_items:
                        if "card_code" in qi:
                            credentials.append({"type": "card_code", "value": qi["card_code"]})
                        elif "phone_url" in qi:
                            credentials.append({"type": "phone_url", "value": qi["phone_url"]})
                    if credentials:
                        kr = requests.post(
                            f"{self.SERVER_URL}/api/web_batch_get_api_keys",
                            json={"keys": credentials},
                            headers={"Content-Type": "application/json"},
                            timeout=15,
                        )
                        if kr.ok:
                            kd = kr.json()
                            if kd.get("success") and kd.get("data"):
                                for d in kd["data"]:
                                    if d.get("phone") and d.get("api_key"):
                                        api_keys_map[d["phone"]] = d["api_key"]
                except Exception:
                    pass

                # ─── 从 phone_url 中提取登录URL ───
                login_url_map = {}  # phone -> login_url
                for qi in query_items:
                    if "phone_url" in qi:
                        parts = qi["phone_url"].split("----", 1)
                        if len(parts) == 2:
                            p = parts[0].strip()
                            url = parts[1].strip()
                            login_url_map[p] = url
                    if "_phone" in qi and qi.get("_api_url"):
                        login_url_map[qi["_phone"]] = qi["_api_url"]

                # ─── 组装结果 ───
                # 优先用 web_batch_query 直接返回的 api_key，其次用 web_batch_get_api_keys 的结果
                if len(accounts_found) == 1:
                    acc = accounts_found[0]
                    phone = acc.get("phone", "")
                    direct_api_key = acc.get("api_key", "")
                    result.update({
                        "success": True,
                        "phone": phone,
                        "api_key": direct_api_key or api_keys_map.get(phone, ""),
                        "login_url": login_url_map.get(phone, ""),
                    })
                else:
                    # 多账号：第一个放主结果，其余放 extra_accounts
                    result.update({
                        "success": True,
                        "phone": "",
                        "api_key": "",
                        "login_url": "",
                        "extra_accounts": [],
                    })
                    for acc in accounts_found:
                        phone = acc.get("phone", "")
                        direct_api_key = acc.get("api_key", "")
                        sub = {
                            "success": True,
                            "phone": phone,
                            "api_key": direct_api_key or api_keys_map.get(phone, ""),
                            "login_url": login_url_map.get(phone, ""),
                            "raw": raw,
                        }
                        if not result["phone"]:
                            result.update(sub)
                        else:
                            result.setdefault("extra_accounts", []).append(sub)

                return result

            def run(self):
                for i, item in enumerate(self._items):
                    if self._cancelled:
                        break
                    raw = item.get("_raw", f"项目{i+1}")
                    self.progress.emit(i, f"正在获取第 {i+1}/{len(self._items)} 个: {raw[:20]}...")

                    try:
                        r = self._fetch_one(item)
                    except Exception as e:
                        r = {"raw": raw, "success": False, "error": str(e),
                             "phone": "", "api_key": "", "login_url": ""}

                    self.item_done.emit(i, r)

                self.all_done.emit([])

        self._thread = BatchFetchThread(items)
        self._thread.progress.connect(self._on_progress)
        self._thread.item_done.connect(self._on_item_done)
        self._thread.all_done.connect(self._on_all_done)
        self._thread.start()

    def _cancel_fetch(self):
        self._cancel_requested = True
        if hasattr(self, '_thread') and self._thread.isRunning():
            self._thread.cancel()
        self._progress_label.setText("⏹ 正在取消...")
        self._btn_cancel.setEnabled(False)

    # ─── 回调 ───

    def _on_progress(self, idx, text):
        self._progress_label.setText(f"⏳ {text}")
        self._progress_bar.setValue(idx)

    def _on_item_done(self, idx, result):
        self._progress_bar.setValue(idx + 1)
        ok = result.get("success", False)

        # 处理 extra_accounts（子Key展开的多账号）
        all_accounts = []
        if ok:
            all_accounts.append(result)
            for extra in result.get("extra_accounts", []):
                all_accounts.append(extra)

        # 更新结果表格
        self._result_table.setVisible(True)
        for acc in all_accounts:
            row = self._result_table.rowCount()
            self._result_table.insertRow(row)
            self._result_table.setItem(row, 0, QTableWidgetItem(acc.get("phone", "")))

            ak = acc.get("api_key", "")
            self._result_table.setItem(row, 1, QTableWidgetItem(ak[:30] + "..." if len(ak) > 30 else ak))

            login_url = acc.get("login_url", "")
            self._result_table.setItem(row, 2, QTableWidgetItem(login_url[:40] + "..." if len(login_url) > 40 else login_url))

            if ak:
                self._result_table.setItem(row, 3, QTableWidgetItem("✅ 有API Key"))
            elif login_url:
                self._result_table.setItem(row, 3, QTableWidgetItem("⚠️ 仅有URL"))
            else:
                self._result_table.setItem(row, 3, QTableWidgetItem("❓ 仅有手机号"))

            self._results.append(acc)

        # 失败的也显示
        if not ok:
            detail_text = self._detail_label.text()
            err_line = f"❌ {result.get('raw', '?')[:20]}: {result.get('error', '未知')}"
            self._detail_label.setText((detail_text + "\n" + err_line).strip())

            row = self._result_table.rowCount()
            self._result_table.insertRow(row)
            self._result_table.setItem(row, 0, QTableWidgetItem(result.get("raw", "")[:20]))
            self._result_table.setItem(row, 1, QTableWidgetItem(""))
            self._result_table.setItem(row, 2, QTableWidgetItem(""))
            self._result_table.setItem(row, 3, QTableWidgetItem(f"❌ {result.get('error', '未知')[:20]}"))
            for c in range(4):
                it = self._result_table.item(row, c)
                if it:
                    it.setForeground(Qt.red)

    def _on_all_done(self, results):
        self._btn_fetch.setEnabled(True)
        self._btn_cancel.setEnabled(False)

        success_count = sum(1 for r in self._results if r.get("success"))
        fail_count = sum(1 for r in self._results if not r.get("success"))
        total = len(self._results)

        if self._cancel_requested:
            self._progress_label.setText(f"⏹ 已取消 — 成功 {success_count}/{total}")
        else:
            self._progress_label.setText(f"✅ 完成 — 成功 {success_count}，失败 {fail_count}，共 {total}")

        self._progress_bar.setValue(self._progress_bar.maximum())

        if success_count > 0:
            self._btn_import.setEnabled(True)
            self._result_table.selectAll()

    # ─── 导入 ───

    def _import_selected(self):
        """导入选中的行到账号列表"""
        selected_rows = set()
        for item in self._result_table.selectedItems():
            selected_rows.add(item.row())

        if not selected_rows:
            QMessageBox.warning(self, "提示", "请先在表格中选择要导入的账号")
            return

        accounts = []
        phones_with_cookie = []  # 收集有手机号的账号，用于批量获取Cookie
        sorted_rows = sorted(selected_rows)
        for row in sorted_rows:
            phone_item = self._result_table.item(row, 0)
            if not phone_item:
                continue
            phone = phone_item.text()
            matched = [r for r in self._results if r.get("success") and r.get("phone") == phone]
            if not matched:
                continue
            r = matched[0]

            # CK 格式: phone----login_url，确保登录网页时能提取手机号和短信链接
            ck_value = ""
            login_url = r.get("login_url", "")
            if phone and login_url:
                ck_value = f"{phone}----{login_url}"
            elif login_url:
                ck_value = login_url
            elif phone:
                ck_value = phone

            acc_data = {
                "uid": phone or r.get("phone", ""),
                "nickname": phone or r.get("phone", ""),
                "auth_token": r.get("api_key", ""),
                "platform": Platform.CODEBUDDY,
                "domain": "www.codebuddy.cn",
                "ck": ck_value,
                "api_key": r.get("api_key", ""),
            }
            accounts.append(acc_data)
            if phone:
                phones_with_cookie.append(phone)

        # ── 批量从服务器获取 Cookie 并保存到本地 ──
        api_url_map = {}
        if phones_with_cookie:
            api_url_map = self._fetch_and_save_cookies(phones_with_cookie)

        # 用服务器返回的 api_url 补全缺少 login_url 的账号
        if api_url_map:
            for acc_data in accounts:
                phone = acc_data.get("uid", "")
                ck = acc_data.get("ck", "")
                # 如果 CK 里没有 URL，用服务器的 api_url 补全
                if phone and phone in api_url_map and "----" not in ck and "http" not in ck:
                    acc_data["ck"] = f"{phone}----{api_url_map[phone]}"

        if accounts:
            self.accounts_imported.emit(accounts)
            QMessageBox.information(self, "导入成功", f"已导入 {len(accounts)} 个账号")
            self.accept()
        else:
            QMessageBox.warning(self, "提示", "没有可导入的有效账号")

    def _fetch_and_save_cookies(self, phones: list):
        """从服务器批量获取 Cookie 并保存到本地文件

        调用服务器 batch_get_cookies API，把返回的 cookie_data 保存到
        ~/.antigravity-tools/cookies/cookie_{phone}.json，登录网页时可直接使用。
        同时用服务器返回的 api_url 补全缺少 login_url 的账号。
        """
        import requests, json, os
        from pathlib import Path

        try:
            resp = requests.post(
                f"{self.SERVER_URL}/api/batch_get_cookies",
                json={"phones": phones},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if not resp.ok:
                return {}

            data = resp.json()
            if not data.get("success"):
                return {}

            cookie_dir = Path(os.path.expanduser("~")) / ".flash-connector" / "cookies"
            cookie_dir.mkdir(parents=True, exist_ok=True)

            saved = 0
            api_url_map = {}  # phone -> api_url，用于补全 CK
            for acc in data.get("accounts", []):
                phone = acc.get("phone", "")
                # 优先使用 cookie_data，其次 original_cookie_data
                cookie_data = acc.get("cookie_data", "") or acc.get("original_cookie_data", "")
                api_url = acc.get("api_url", "")

                # 记录 api_url 用于补全 CK
                if phone and api_url:
                    api_url_map[phone] = api_url

                if not phone or not cookie_data:
                    continue

                # cookie_data 是 JSON 数组字符串，和 Playwright cookies 格式一致
                try:
                    cookies_list = json.loads(cookie_data) if isinstance(cookie_data, str) else cookie_data
                    if isinstance(cookies_list, list) and len(cookies_list) > 0:
                        cookie_file = cookie_dir / f"cookie_{phone}.json"
                        cookie_file.write_text(
                            json.dumps(cookies_list, ensure_ascii=False, indent=2),
                            encoding="utf-8"
                        )
                        saved += 1
                except (json.JSONDecodeError, TypeError):
                    continue

            if saved > 0:
                logger.info(f"从服务器获取并保存了 {saved} 个账号的 Cookie")

            return api_url_map
        except Exception as e:
            logger.warning(f"从服务器获取 Cookie 失败: {e}")
            return {}


class CardKeyFetchDialog(QDialog):
    """卡密提取账号包对话框 — 对接云端卡密系统

    流程：输入卡密 → 验证(显示积分/账号数) → 签名下载 → 解析入库 → 自动刷新积分
    卡密一次性核销，重复提取被拒绝。
    """

    accounts_imported = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🎫 卡密提取账号包")
        self.setMinimumSize(620, 520)
        self._cancel_requested = False
        self._card_info = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        hint = QLabel(
            "输入从网站购买的卡密（WK-XXXX-XXXX-XXXX-XXXX），自动验证并下载对应的账号包：\n"
            "  - 显示卡密包含的账号数量与积分总值（1 账号 = 2000 积分）\n"
            "  - 一键导入账号到本地（自动刷新积分 + 同步上游 Key 池 + 无感换号池）\n"
            "  - 每张卡密仅可提取一次，下载后即标记为已使用"
        )
        hint.setObjectName("inline_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        card_row = QHBoxLayout()
        self._card_input = QLineEdit()
        self._card_input.setPlaceholderText("粘贴卡密后按回车，例如：WK-ABCD-EFGH-IJKL-MNOP")
        self._card_input.setMinimumHeight(36)
        self._card_input.textChanged.connect(self._on_card_changed)
        # 回车直接验证
        self._card_input.returnPressed.connect(self._verify_card)
        card_row.addWidget(self._card_input, 1)

        self._btn_verify = QPushButton("🔍 兑换")
        self._btn_verify.setObjectName("secondary_btn")
        self._btn_verify.setMinimumHeight(36)
        self._btn_verify.clicked.connect(self._verify_card)
        card_row.addWidget(self._btn_verify)
        layout.addLayout(card_row)

        self._info_label = QLabel("先兑换卡密，确认账号数量与积分后再提取。")
        self._info_label.setStyleSheet(
            "background:#2D3748;border-radius:8px;padding:12px;color:#E2E8F0;font-size:12px;line-height:1.7;"
        )
        self._info_label.setWordWrap(True)
        layout.addWidget(self._info_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p%")
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #2B6CB0; font-size: 12px; font-weight: 600;")
        layout.addWidget(self._progress_label)

        self._log_edit = QTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setVisible(False)
        self._log_edit.setMaximumHeight(140)
        layout.addWidget(self._log_edit, 1)

        # 提取按钮居中放置
        fetch_row = QHBoxLayout()
        fetch_row.addStretch()
        self._btn_fetch = QPushButton("📥 提取并导入账号")
        self._btn_fetch.setObjectName("primary_btn")
        self._btn_fetch.setMinimumHeight(48)
        self._btn_fetch.setMinimumWidth(200)
        self._btn_fetch.setEnabled(False)
        self._btn_fetch.setCursor(Qt.PointingHandCursor)
        self._btn_fetch.clicked.connect(self._start_fetch)
        fetch_row.addWidget(self._btn_fetch)
        fetch_row.addStretch()
        layout.addLayout(fetch_row)

        # 底部只有关闭按钮
        close_row = QHBoxLayout()
        close_row.addStretch()
        self._btn_close = QPushButton("✕ 关闭")
        self._btn_close.setObjectName("secondary_btn")
        self._btn_close.setMinimumHeight(36)
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.clicked.connect(self.reject)
        close_row.addWidget(self._btn_close)
        layout.addLayout(close_row)

    def _on_card_changed(self):
        self._btn_fetch.setEnabled(False)
        self._card_info = None

    def _paste_from_clipboard(self):
        """一键从剪贴板粘贴卡密"""
        from PySide6.QtWidgets import QApplication as _App
        clipboard = _App.clipboard()
        text = (clipboard.text() or "").strip()
        if not text:
            QMessageBox.information(self, "提示", "剪贴板是空的。\n请先复制卡密（WK-XXXX-XXXX-XXXX-XXXX）再点粘贴。")
            return
        # 提取 WK- 开头的卡密（用户可能复制了整行聊天记录）
        import re
        m = re.search(r'WK-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}', text.upper())
        if m:
            self._card_input.setText(m.group(0))
            self._verify_card()  # 粘贴后自动验证
        elif text.upper().startswith("WK-"):
            self._card_input.setText(text.split()[0].strip())
            self._verify_card()
        else:
            self._card_input.setText(text)
            QMessageBox.information(self, "提示", f"剪贴板内容不是标准卡密格式（WK-XXXX-XXXX-XXXX-XXXX），已填入，请检查后点验证。")

    def _verify_card(self):
        key = self._card_input.text().strip()
        if not key:
            QMessageBox.warning(self, "提示", "请输入卡密")
            return

        self._btn_verify.setEnabled(False)
        self._info_label.setText("⏳ 正在验证卡密...")

        from PySide6.QtCore import QThread, Signal as QSignal

        class VerifyThread(QThread):
            result_ready = QSignal(dict)

            def __init__(self, card_key):
                super().__init__()
                self._key = card_key

            def run(self):
                try:
                    from ...modules.card_client import verify_card
                    data = verify_card(self._key)
                    self.result_ready.emit(data)
                except Exception as e:
                    self.result_ready.emit({"ok": False, "error": str(e)})

        self._verify_thread = VerifyThread(key)
        self._verify_thread.result_ready.connect(self._on_verify_result)
        self._verify_thread.start()

    def _on_verify_result(self, result: dict):
        self._btn_verify.setEnabled(True)
        if not result.get("ok"):
            err = result.get("error", "网络错误")
            self._info_label.setText(f"❌ 验证失败：{err}")
            self._info_label.setStyleSheet(
                "background:#742A2A;border-radius:8px;padding:12px;color:#FEB2B2;font-size:12px;line-height:1.7;"
            )
            return

        card = result["card"]
        self._card_info = card
        dl_remain = card.get("download_remaining", "")
        dl_text = f"\n剩余下载次数：{dl_remain}/5 次" if dl_remain != "" else ""
        extract_count = card.get("extract_count", 0)
        status_text = card.get("status_text", card.get("status", ""))
        self._info_label.setText(
            f"✅ 卡密有效（{status_text}）\n"
            f"账号数量：{card['account_count']} 个\n"
            f"积分总值：{card['points']} 积分（单价 {card['price_per_account']} 积分/个）\n"
            f"有效期：{card.get('expires_at', '永久有效')}\n"
            f"已下载次数：{extract_count} 次{dl_text}"
        )
        self._info_label.setStyleSheet(
            "background:#22543D;border-radius:8px;padding:12px;color:#C6F6D5;font-size:12px;line-height:1.7;"
        )
        self._btn_fetch.setEnabled(True)

    def _start_fetch(self):
        if not self._card_info:
            QMessageBox.warning(self, "提示", "请先验证卡密")
            return
        key = self._card_input.text().strip()

        # ===== 下载前本地核验：该卡密的账号是否已在本地 =====
        # 本地 accounts 表用 account_group=卡密号 标记来源
        # ★2026-09-17修复：本地有账号 ≠ 无需下载——还要看Key池有没有这些号！
        # 用户清空Key池后重新下载是合法恢复场景（服务器端绑定号完整时免计次下发），
        # 只有"本地有且Key池也有"才真拦（避免浪费下载次数）
        try:
            import sqlite3 as _sql2
            from ...utils.store import _get_db_path as _dbp2
            conn2 = _sql2.connect(str(_dbp2()))
            c2 = conn2.cursor()
            try:
                row = c2.execute(
                    "SELECT COUNT(*) FROM accounts WHERE account_group=?", (key,)
                ).fetchone()
                local_count = int(row[0]) if row else 0
            except Exception:
                local_count = 0
            conn2.close()
        except Exception:
            local_count = 0

        # 查Key池是否已有该卡密的账号（account_group=卡密号的uid在Key池中出现）
        pool_has_card_accounts = False
        if local_count > 0:
            try:
                import sqlite3 as _sql3b
                from ...utils.store import _get_db_path as _dbp3b
                conn3 = _sql3b.connect(str(_dbp3b()))
                c3 = conn3.cursor()
                local_uids = [r[0] for r in c3.execute(
                    "SELECT uid FROM accounts WHERE account_group=?", (key,)).fetchall()]
                conn3.close()
                if local_uids:
                    from ...modules.proxy_server import ProxyDatabase as _PDB2
                    _pdb2 = _PDB2.get_instance()
                    pool_uids = set()
                    for _pk in _pdb2.get_upstream_keys():
                        _plbl = str(_pk.get("label", ""))
                        _papi = str(_pk.get("api_key", ""))
                        # Key池条目带uid信息有限——用api_key与accounts表api_key对比
                        pool_uids.add(_papi)
                    c3b = _sql3b.connect(str(_dbp3b()))
                    c3b.row_factory = _sql3b.Row
                    placeholders3 = ",".join("?" * len(local_uids))
                    rows3 = c3b.execute(
                        f"SELECT api_key, auth_token, ck FROM accounts WHERE uid IN ({placeholders3})",
                        local_uids).fetchall()
                    c3b.close()
                    for r3 in rows3:
                        ak = (r3["api_key"] or r3["auth_token"] or "") if "api_key" in r3.keys() else ""
                        if ak and ak in pool_uids:
                            pool_has_card_accounts = True
                            break
            except Exception:
                pool_has_card_accounts = False  # 查不了Key池→按无池处理，放行下载

        need_count = int(self._card_info.get("account_count", 0) or 0)
        if local_count > 0 and local_count >= need_count and pool_has_card_accounts:
            # 本地已完整存在 且 Key池也有 → 真拦（省下载次数）
            self._progress_bar.setVisible(False)
            self._log_edit.setVisible(True)
            self._append_log(f"✅ 该卡密的 {local_count} 个账号已完整在本地且Key池可用，无需重新下载")
            self._append_log("💡 如需恢复账号，请勿清空Key池；清空后重新下载即可免费恢复（不计次）")
            self._progress_label.setText(f"✅ 账号已在本地且在Key池（{local_count} 个），无需重新下载")
            from PySide6.QtWidgets import QMessageBox as _MB
            _MB.information(
                self, "无需下载",
                f"该卡密的 {local_count} 个账号已完整在本地且Key池可用，无需重新下载。\n\n"
                "下载会消耗卡密的提取次数（共5次），已导入过的请直接使用。\n"
                "提示：若清空了Key池，重新下载可免费恢复（不消耗次数）。"
            )
            self._btn_fetch.setEnabled(False)
            self._btn_verify.setEnabled(False)
            self._card_input.setEnabled(False)
            self._btn_close.setEnabled(True)
            return
        if local_count > 0:
            # 本地有部分（少于应得数量）——提示但不阻断（可能是上次导入中断）
            self._append_log(f"⚠️ 本地已有该卡密的部分账号（{local_count}/{need_count} 个），将继续下载补齐")

        self._btn_fetch.setEnabled(False)
        self._btn_verify.setEnabled(False)
        self._card_input.setEnabled(False)
        self._btn_close.setEnabled(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(20)
        self._progress_label.setText("⏳ 正在下载账号包...")
        self._log_edit.setVisible(True)
        self._append_log(f"🔑 卡密：{key}")

        from PySide6.QtCore import QThread, Signal as QSignal

        class FetchThread(QThread):
            result_ready = QSignal(dict)

            def __init__(self, card_key, account_count=1):
                super().__init__()
                self._key = card_key
                self._count = max(1, account_count)

            def run(self):
                try:
                    from ...modules.card_client import download_card_pack
                    # 超时按号数动态放大：多号卡服务端逐号实时验证耗时≈10秒/号
                    data = download_card_pack(self._key, account_count=self._count)
                    self.result_ready.emit({"ok": True, "data": data})
                except Exception as e:
                    self.result_ready.emit({"ok": False, "error": str(e)})

        need_count = int(self._card_info.get("account_count", 1) or 1) if self._card_info else 1
        self._fetch_thread = FetchThread(key, need_count)
        self._fetch_thread.result_ready.connect(self._on_fetch_result)
        self._fetch_thread.start()

    def _on_fetch_result(self, result: dict):
        if not result.get("ok"):
            err = result.get("error", "下载失败")
            # 友好化服务器错误消息
            if "下载次数已用完" in err:
                err = "该卡密下载次数已用完（最多5次），无法再下载"
            elif err == "请求无效":
                err = "卡密无效、已作废或下载次数已用完"
            elif "已过期" in err:
                err = "卡密已过期"
            elif "已作废" in err:
                err = "卡密已被封卡"
            elif "号池积分不足" in err or "账号暂时不足" in err:
                # 号池无匹配号：把服务器给的详细原因直接透传（含面额/池内积分/解决方案）
                pass
            self._progress_label.setText(f"❌ 提取失败：{err}")
            self._append_log(f"❌ 提取失败：{err}")
            if "号池积分不足" in err or "账号暂时不足" in err:
                self._append_log("💡 解决方案：这是商家号池暂无符合要求的账号（号还在养号中），请联系商家补货后重试")
            self._reset_ui()
            return

        data = result["data"]
        # ★2026-09-17修复：先查服务器业务结果（ok字段）——
        # 服务器业务失败（如号池积分不足）时旧客户端会把error响应当成功解析，
        # accounts为空 → 误报"服务器未返回账号"掩盖真实原因
        if data.get("ok") is False:
            err = str(data.get("error", "服务器拒绝下载"))
            self._progress_label.setText(f"❌ 提取失败：{err}")
            self._append_log(f"❌ 提取失败：{err}")
            if "号池积分不足" in err:
                self._append_log("💡 服务器原样透传：绑定号的积分可能已被消耗低于面额，且号池暂无符合档位的号可补位")
                self._append_log("💡 解决方案：等号池补货后重试，或联系商家核查卡密绑定号状态")
            self._reset_ui()
            return
        accounts_raw = data.get("accounts", [])
        if not accounts_raw:
            # 防御：服务器返回ok但账号为空（正常不会发生），给出完整诊断
            self._progress_label.setText("❌ 提取失败：服务器未返回账号")
            self._append_log("❌ 提取失败：服务器未返回账号")
            self._append_log("💡 可能原因：1)号池暂无符合要求的账号（养号中）2)卡密绑定的账号已失效")
            self._append_log("💡 解决方案：联系商家检查号池库存或更换卡密")
            self._reset_ui()
            return

        self._progress_bar.setValue(50)
        self._progress_label.setText(f"⏳ 正在解析 {len(accounts_raw)} 个账号...")
        self._append_log(f"📦 下载成功：{len(accounts_raw)} 个账号")

        # 显示下载剩余次数
        dl_remain = data.get("card", {}).get("download_remaining")
        if dl_remain is not None:
            self._append_log(f"📊 还可下载 {dl_remain} 次")

        key = self._card_input.text().strip()

        # 本地缓存
        try:
            from ...modules.card_client import cache_card_pack
            cache_card_pack(key, accounts_raw)
            self._append_log("💾 已缓存账号包到本地")
        except Exception:
            pass

        # 解析为底座兼容格式
        from ...modules.card_client import parse_accounts_for_import
        accounts = parse_accounts_for_import(accounts_raw, key)

        if not accounts:
            self._progress_label.setText("❌ 无可导入账号（可能token字段为空）")
            self._append_log("❌ 解析后无可导入账号")
            self._reset_ui()
            return

        # 账号去重：只看Key池（唯一"活跃账号"事实源）
        # ★2026-09-17定案（用户）：账号管理已与一键接入融合，Key池(upstream_keys)是唯一入口。
        # accounts表退化为签到/积分等功能的历史数据底座，不参与下载去重——
        # 否则清空Key池后重新下载会被accounts表历史uid拦截（v9.10.6已修的双判定问题），
        # 现在彻底简化：Key在池=真重复跳过；Key不在池=放行（入库侧按api_key去重插入，无重复风险）。
        key_pool_tokens = set()
        pool_ok = False
        try:
            from ...modules.proxy_server import ProxyDatabase as _PDB
            _pdb = _PDB.get_instance()
            key_pool_tokens = {k.get("api_key", "") for k in _pdb.get_upstream_keys()}
            pool_ok = True
        except Exception:
            pool_ok = False  # Key池不可用→全部放行（宁可重复入池也不拦下载）

        if pool_ok:
            dup_count = 0
            new_accounts = []
            for a in accounts:
                ak = a.get("api_key", "") or a.get("auth_token", "")
                if ak and ak in key_pool_tokens:
                    dup_count += 1  # Key已在池=真重复
                else:
                    new_accounts.append(a)
            if dup_count > 0:
                self._append_log(f"⚠️ {dup_count} 个账号已在Key池（跳过）")
            if not new_accounts:
                self._progress_label.setText(f"❌ {dup_count} 个账号已在Key池，无需重复导入")
                self._append_log(f"❌ 该卡密的账号已全部在Key池中，无需重复下载")
                self._reset_ui()
                return
            accounts = new_accounts

        self._progress_bar.setValue(80)
        self._progress_label.setText(f"📥 正在保存 {len(accounts)} 个账号...")

        # 发射信号 → 父页面批量入库（含 Key 池同步）
        self.accounts_imported.emit(accounts)

        self._progress_bar.setValue(90)
        self._progress_label.setText("🔍 正在核验本地数据库...")

        # 核验：检查这些账号是否真的都入库了
        verify_ok = 0
        verify_fail = []
        try:
            import sqlite3 as _sql
            conn = _sql.connect(db_path)
            c = conn.cursor()
            for acc in accounts:
                uid = acc.get("uid", "")
                if not uid:
                    continue
                row = c.execute("SELECT uid FROM accounts WHERE uid=?", (uid,)).fetchone()
                if row:
                    verify_ok += 1
                else:
                    verify_fail.append(uid)
            conn.close()
        except Exception as e:
            self._append_log(f"⚠️ 核验数据库异常: {e}")

        if verify_fail:
            self._append_log(f"❌ 核验失败：{len(verify_fail)} 个账号未入库: {', '.join(verify_fail[:3])}")
            self._progress_label.setText(f"❌ 核验失败：{len(verify_fail)} 个账号未入库")
            self._reset_ui()
            return

        self._progress_bar.setValue(100)
        self._append_log(f"✅ 核验通过：{verify_ok}/{len(accounts)} 个账号已确认入库")

        # ★2026-09-17修复：清理无效缓存——之前token为空bug导入的空token记录
        # 这些记录auth_token为空=无效垃圾数据，显示假账号数但Key池没有
        try:
            import sqlite3 as _sql_clean
            from ...utils.store import _get_db_path as _dbp_clean
            _c_clean = _sql_clean.connect(str(_dbp_clean()))
            _c_clean.execute(
                "DELETE FROM accounts WHERE auth_token='' OR auth_token IS NULL")
            _c_clean.commit()
            _c_clean.close()
        except Exception:
            pass  # 清理失败不影响导入
        self._append_log("📊 积分刷新已自动触发（见账号管理页日志）")

        # ===== 客户告知：获取账号数 + 真实积分总和 + 赠送/波动提示 =====
        card_points = self._card_info.get('points', 0)  # 卡面额
        # 从服务器下发的原始数据拿单号积分（服务器在激活时实时查过）
        live_credits_list = [a.get('credits') for a in accounts_raw if a.get('credits') is not None]
        if live_credits_list:
            live_sum = sum(live_credits_list)
        else:
            live_sum = card_points  # 服务器没带就退回面额
        dl_remain = data.get("card", {}).get("download_remaining")

        self._append_log(f"🎁 获取账号 {len(accounts)} 个，账号价值积分总计 {live_sum} 分")
        # 逐号积分明细
        for acc, lc in zip(accounts, live_credits_list or [None] * len(accounts)):
            if lc is not None:
                self._append_log(f"　· {acc.get('nickname', '?')}：{lc} 分")
        if live_sum > card_points:
            extra = live_sum - card_points
            self._append_log(f"🎊 总共 {live_sum} 积分，超出部分（{extra} 分）为免费赠送您，祝您 AI 愉快！")
        elif live_sum < card_points and card_points - live_sum <= 200:
            gap = card_points - live_sum
            self._append_log(f"ℹ️ 当前账号实际积分 {live_sum} 分，与面额相差 {gap} 分，50-200 积分差距波动属正常范围，可自行每日签到领回，请正常使用，感谢理解")
        elif live_sum < card_points:
            self._append_log(f"ℹ️ 当前账号实际积分 {live_sum} 分（面额 {card_points} 分），积分存在正常波动，可自行每日签到领回，请放心使用")
        # 账号有效期提醒（两种情况都加）
        self._append_log("📅 账号有效期：30-60 天，请及时使用")

        if dl_remain is not None:
            self._append_log(f"📊 该卡密还可下载 {dl_remain} 次")
            self._progress_label.setText(
                f"✅ 激活成功！获取 {len(accounts)} 个账号，账号积分总计 {live_sum} 分（还可下载{dl_remain}次）"
            )
        else:
            self._progress_label.setText(
                f"✅ 激活成功！获取 {len(accounts)} 个账号，账号积分总计 {live_sum} 分"
            )
        try:
            from ...modules.toast_notify import show_toast
            show_toast(
                "Token接入器 · 激活成功",
                f"获取 {len(accounts)} 个账号，积分总计 {live_sum} 分",
            )
        except Exception:
            pass

        # 成功后锁定按钮为初始灰色不可点击状态
        self._btn_fetch.setEnabled(False)
        self._btn_verify.setEnabled(False)
        self._card_input.setEnabled(False)
        self._btn_close.setEnabled(True)

    def _reset_ui(self):
        self._btn_fetch.setEnabled(False)
        self._btn_verify.setEnabled(True)
        self._card_input.setEnabled(True)
        self._btn_close.setEnabled(True)

    def _append_log(self, text: str):
        self._log_edit.append(text)
        sb = self._log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())
