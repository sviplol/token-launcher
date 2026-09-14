"""侧边栏 — 极简黑白设计（反色选中 + 底部主题切换）"""

import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QButtonGroup, QSpacerItem, QSizePolicy, QFrame
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QIcon

from ...ui.theme import (
    LIGHT, DARK, resolve_colors, get_theme_setting, next_mode,
    set_theme_setting, MODE_ICON, MODE_LABEL,
)


def _get_icon() -> QIcon:
    paths = [
        os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "app.png"),
        os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "app.ico"),
    ]
    for p in paths:
        if os.path.isfile(p):
            icon = QIcon(p)
            if not icon.isNull():
                return icon
    return QIcon()


NAV_ITEMS = [
    ("dashboard", "🏠", "首页"),
    ("hotswitch", "⚡", "一键接入"),
    ("checkin", "✅", "签到"),
    ("settings", "⚙️", "设置"),
]


class Sidebar(QWidget):
    """极简黑白侧边栏：白底(黑主题黑底) + 反色选中 + 底部主题切换按钮"""

    page_changed = Signal(str)
    theme_cycle_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self._current_page = "dashboard"
        self._buttons = {}
        self._setup_ui()
        self.apply_colors(resolve_colors())

    # ─── UI ───

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 品牌区
        brand = QWidget()
        brand.setObjectName("brandArea")
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(20, 20, 16, 14)
        bl.setSpacing(10)

        icon_lbl = QLabel()
        icon = _get_icon()
        if not icon.isNull():
            icon_lbl.setPixmap(icon.pixmap(30, 30))
        else:
            icon_lbl.setText("⚡")
        icon_lbl.setObjectName("brandIcon")
        icon_lbl.setFixedSize(30, 30)
        bl.addWidget(icon_lbl)

        text_box = QWidget()
        text_box.setObjectName("brandText")
        tl = QVBoxLayout(text_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        name = QLabel("Token接入器")
        name.setObjectName("brandName")
        tl.addWidget(name)
        ver = QLabel("v9.9.8")
        ver.setObjectName("brandVer")
        tl.addWidget(ver)
        bl.addWidget(text_box)
        bl.addStretch()
        layout.addWidget(brand)

        sep = QFrame()
        sep.setObjectName("navSep")
        layout.addWidget(sep)
        layout.addSpacing(6)

        # 导航按钮
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for page_id, emoji, label in NAV_ITEMS:
            btn = QPushButton(f"  {emoji}  {label}")
            btn.setObjectName("navBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setChecked(page_id == self._current_page)
            btn.clicked.connect(lambda _, pid=page_id: self._on_nav(pid))
            grp.addButton(btn)
            self._buttons[page_id] = btn
            layout.addWidget(btn)

        layout.addStretch()

        # 底部主题切换按钮
        self._theme_btn = QPushButton(f"{MODE_ICON.get(get_theme_setting(), '◐')} 主题")
        self._theme_btn.setObjectName("themeBtn")
        self._theme_btn.setCursor(Qt.PointingHandCursor)
        self._theme_btn.setToolTip(f"当前：{MODE_LABEL.get(get_theme_setting(), '自动')}\n点击切换：自动 → 极简白 → 极简黑")
        self._theme_btn.clicked.connect(self._on_theme_click)
        layout.addWidget(self._theme_btn)

        footer = QLabel("  © 2026 Token接入器")
        footer.setObjectName("navFooter")
        layout.addWidget(footer)

    def _on_nav(self, page_id):
        if page_id == self._current_page:
            return
        self._current_page = page_id
        for pid, btn in self._buttons.items():
            btn.setChecked(pid == page_id)
        self.page_changed.emit(page_id)

    def _on_theme_click(self):
        self.theme_cycle_requested.emit()

    # ─── 主题 ───

    def apply_colors(self, c: dict):
        """按颜色dict刷新侧边栏QSS（黑模式文字纯白加大，选中反色）"""
        # 黑模式侧边栏文字用高对比纯白（#F5F5F5按设计稿，但加大加粗保证可读）
        nav_color = c['sidebar_text']
        if c is not None and c.get('sidebar_bg') in ('#0A0A0A', '#141414', '#1A1725', '#141220'):
            nav_color = '#FFFFFF'  # 黑模式导航文字纯白
        # 判断是否黑模式：卡片色深
        is_dark = c.get('card') in ('#141414', '#0A0A0A')
        brand_color = c['text'] if is_dark else c['text']
        self.setStyleSheet(f"""
        QWidget#sidebar {{
            background: {c['sidebar_bg']};
            border-right: 1px solid {c['border']};
            min-width: 200px; max-width: 200px;
        }}
        QWidget#brandArea, QWidget#brandText {{ background: transparent; border: none; }}
        QLabel#brandIcon {{ background: transparent; border: none; }}
        QLabel#brandName {{
            color: {brand_color}; font-size: 16px; font-weight: 800;
            background: transparent; border: none;
        }}
        QLabel#brandVer {{
            color: {c['text3']}; font-size: 11px;
            background: transparent; border: none;
        }}
        QFrame#navSep {{
            background: {c['border']}; border: none;
            max-height: 1px; margin: 0 16px;
        }}
        QPushButton#navBtn {{
            background: transparent;
            color: {nav_color};
            border: none; border-radius: 8px;
            padding: 13px 14px; text-align: left;
            font-size: 15px; font-weight: 600;
            margin: 1px 12px;
        }}
        QPushButton#navBtn:hover {{
            background: {c['bg3']};
            color: {brand_color};
        }}
        QPushButton#navBtn:pressed {{
            background: {c['border2']};
            padding-top: 14px; padding-bottom: 12px;
        }}
        QPushButton#navBtn:checked {{
            background: {c['sidebar_active_bg']};
            color: {c['sidebar_active_text']};
            font-weight: 700;
        }}
        QPushButton#navBtn:checked:pressed {{
            padding-top: 14px; padding-bottom: 12px;
        }}
        QPushButton#themeBtn {{
            background: transparent;
            color: {nav_color};
            border: 1px solid {c['border']};
            border-radius: 8px;
            padding: 9px 14px;
            font-size: 13px; font-weight: 700;
            margin: 4px 12px 2px 12px;
        }}
        QPushButton#themeBtn:hover {{
            background: {c['bg3']};
            color: {brand_color};
        }}
        QPushButton#themeBtn:pressed {{
            background: {c['border2']};
            padding-top: 10px; padding-bottom: 8px;
        }}
        QLabel#navFooter {{
            color: {c['text3']}; font-size: 10px;
            background: transparent; border: none;
            padding: 4px 16px 10px 16px;
        }}
        """)

    def refresh_theme_button(self):
        """主题切换后更新按钮文字"""
        mode = get_theme_setting()
        self._theme_btn.setText(f"{MODE_ICON.get(mode, '◐')} 主题")
        self._theme_btn.setToolTip(f"当前：{MODE_LABEL.get(mode, '自动')}\n点击切换：自动 → 极简白 → 极简黑")

    def refresh_translations(self):
        for page_id, emoji, label in NAV_ITEMS:
            btn = self._buttons.get(page_id)
            if btn:
                btn.setText(f"  {emoji}  {label}")
