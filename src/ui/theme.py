"""主题系统 — 极简黑白 + 三档切换（auto/light/dark）+ 北京日落自动切换"""

import os
from PySide6.QtCore import QTimer, QSettings
from PySide6.QtGui import QIcon

THEME_KEY = "ui_theme"

# ============ 极简白 ============
LIGHT = {
    "bg": "#FAFAFA", "bg2": "#F4F4F5", "bg3": "#E8E8EA", "card": "#FFFFFF",
    "text": "#0A0A0A", "text2": "#404040", "text3": "#8A8A8E",
    "accent": "#0A0A0A", "accent_hover": "#2A2A2A", "accent_text": "#FFFFFF",
    "ok": "#0A7D33", "ok_bg": "#E8F5EC",
    "warn": "#9A6B00", "warn_bg": "#F5EEDC",
    "err": "#C0271D", "err_bg": "#F9E8E7",
    "border": "#E4E4E7", "border2": "#EFEFF1",
    "sidebar_bg": "#FFFFFF", "sidebar_text": "#71717A",
    "sidebar_active_bg": "#0A0A0A", "sidebar_active_text": "#FFFFFF",
    "shadow": "rgba(0,0,0,0.05)",
    "table_alt": "#F9F9FA",
}

# ============ 极简黑 ============
DARK = {
    "bg": "#0A0A0A", "bg2": "#141414", "bg3": "#1E1E1E", "card": "#141414",
    "text": "#F5F5F5", "text2": "#A1A1AA", "text3": "#6B6B70",
    "accent": "#F5F5F5", "accent_hover": "#FFFFFF", "accent_text": "#0A0A0A",
    "ok": "#4ADE80", "ok_bg": "#12291A",
    "warn": "#FACC15", "warn_bg": "#2A250D",
    "err": "#F87171", "err_bg": "#2D1414",
    "border": "#262626", "border2": "#1C1C1C",
    "sidebar_bg": "#0A0A0A", "sidebar_text": "#8A8A8E",
    "sidebar_active_bg": "#F5F5F5", "sidebar_active_text": "#0A0A0A",
    "shadow": "rgba(0,0,0,0.4)",
    "table_alt": "#181818",
}

# 北京日落时间表（月: [时, 分]）
BJ_SUNSET = {
    1: (17, 18), 2: (18, 7), 3: (18, 29), 4: (19, 1), 5: (19, 21), 6: (19, 39),
    7: (19, 44), 8: (19, 26), 9: (18, 49), 10: (18, 10), 11: (17, 36), 12: (17, 23),
}
BJ_SUNRISE = (6, 20)  # 全年近似


def bj_now():
    """北京时间（UTC+8）"""
    import datetime
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def bj_is_night(t=None) -> bool:
    """北京时间是否为夜间（日落后~日出前）"""
    t = t or bj_now()
    m = t.month
    sh, sm = BJ_SUNSET[m]
    sunset = sh * 60 + sm
    sunrise = BJ_SUNRISE[0] * 60 + BJ_SUNRISE[1]
    now = t.hour * 60 + t.minute
    return now >= sunset or now < sunrise


def get_theme_setting() -> str:
    """读取主题设置（auto/light/dark），默认极简白"""
    from ..utils.store import load_setting
    return load_setting(THEME_KEY, "light") or "light"


def set_theme_setting(v: str):
    from ..utils.store import save_setting
    save_setting(THEME_KEY, v)


def resolve_colors(mode: str = None) -> dict:
    """mode(auto/light/dark) → 实际颜色dict"""
    mode = mode or get_theme_setting()
    if mode == "light":
        return LIGHT
    if mode == "dark":
        return DARK
    return DARK if bj_is_night() else LIGHT


def next_mode(mode: str) -> str:
    """三档循环: auto → light → dark → auto"""
    order = {"auto": "light", "light": "dark", "dark": "auto"}
    return order.get(mode, "auto")


MODE_ICON = {"auto": "◐", "light": "☀", "dark": "🌙"}
MODE_LABEL = {"auto": "自动（跟随日落）", "light": "极简白", "dark": "极简黑"}


def build_qss(c: dict) -> str:
    """按颜色dict生成完整QSS"""
    return f"""
QMainWindow, QWidget#centralWidget, QWidget#pageStack {{
    background: {c['bg']};
    color: {c['text']};
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
}}
QWidget#topBar {{
    background: {c['card']};
    border-bottom: 1px solid {c['border']};
    min-height: 52px; max-height: 52px;
}}
QLabel {{ background: transparent; border: none; color: {c['text']}; }}
QLabel#topTitle {{ font-size: 16px; font-weight: 700; }}
QLabel#topVersion {{ font-size: 11px; color: {c['text3']}; }}
QLabel#page_title {{ font-size: 20px; font-weight: 800; color: {c['text']};
    padding: 20px 24px 4px 24px; background: transparent; }}
QLabel#page_subtitle {{ font-size: 13px; color: {c['text2']};
    padding: 0 24px 12px 24px; background: transparent; }}
QFrame {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 10px; }}
QFrame#card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 10px; }}
QWidget#content_area {{ background: {c['bg']}; border: none; }}
QWidget#sidebar {{ background: {c['sidebar_bg']}; border-right: 1px solid {c['border']}; }}
QStackedWidget {{ background: {c['bg']}; border: none; }}
QScrollArea {{ background: transparent; border: none; }}
QWidget#settings_scroll_area {{ background: transparent; border: none; }}

QPushButton {{
    background: {c['bg3']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 7px;
    padding: 8px 14px; font-weight: 500;
}}
QPushButton:hover {{ background: {c['border2']}; border-color: {c['text3']}; }}
QPushButton:pressed {{ background: {c['border']}; padding-top: 9px; padding-bottom: 7px; }}
QPushButton:disabled {{ color: {c['text3']}; background: {c['bg2']}; border-color: {c['border2']}; }}
QPushButton#primary {{
    background: {c['accent']}; color: {c['accent_text']};
    border: 1px solid {c['accent']}; border-radius: 7px;
    font-weight: 700; padding: 9px 18px;
}}
QPushButton#primary:hover {{ background: {c['accent_hover']}; }}
QPushButton#primary:pressed {{ background: {c['accent']}; padding-top: 10px; padding-bottom: 8px; }}
QPushButton#primary:disabled {{ background: {c['bg2']}; color: {c['text3']}; border-color: {c['border2']}; }}
QPushButton#primary_btn {{
    background: {c['accent']}; color: {c['accent_text']};
    border: 1px solid {c['accent']}; border-radius: 7px;
    font-weight: 700; padding: 9px 18px;
}}
QPushButton#primary_btn:hover {{ background: {c['accent_hover']}; }}
QPushButton#primary_btn:pressed {{ padding-top: 10px; padding-bottom: 8px; }}
QPushButton#primary_btn:disabled {{ background: {c['bg2']}; color: {c['text3']}; border-color: {c['border2']}; }}
QPushButton#secondary_btn {{
    background: {c['bg3']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 7px; font-weight: 600;
}}
QPushButton#secondary_btn:hover {{ background: {c['border2']}; border-color: {c['text3']}; }}
QPushButton#secondary_btn:pressed {{ background: {c['border']}; padding-top: 9px; padding-bottom: 7px; }}
QPushButton#secondary_btn:disabled {{ color: {c['text3']}; background: {c['bg2']}; border-color: {c['border2']}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {c['text2']}; }}
QPushButton#ghost:hover {{ color: {c['text']}; background: {c['bg3']}; }}
QPushButton#ghost:pressed {{ background: {c['border2']}; }}
QPushButton#bigPrimary {{
    background: {c['accent']}; color: {c['accent_text']};
    border: 1px solid {c['accent']}; border-radius: 12px;
    font-size: 18px; font-weight: 800; padding: 16px 40px;
}}
QPushButton#bigPrimary:hover {{ background: {c['accent_hover']}; }}
QPushButton#bigPrimary:pressed {{ padding-top: 18px; padding-bottom: 14px; }}
QPushButton#bigPrimary:disabled {{ background: {c['bg2']}; color: {c['text3']}; border-color: {c['border2']}; }}
QPushButton#bigDanger {{
    background: {c['err']}; color: #FFFFFF;
    border: 1px solid {c['err']}; border-radius: 12px;
    font-size: 18px; font-weight: 800; padding: 16px 40px;
}}
QPushButton#bigDanger:hover {{ background: {c['err']}; opacity: 0.85; }}
QPushButton#bigDanger:pressed {{ padding-top: 18px; padding-bottom: 14px; }}
QPushButton#bigDanger:disabled {{ background: {c['bg2']}; color: {c['text3']}; border-color: {c['border2']}; }}

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {c['card']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 7px;
    padding: 7px 10px;
    selection-background-color: {c['accent']};
    selection-color: {c['accent_text']};
}}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {c['text2']};
}}
QComboBox QAbstractItemView {{
    background: {c['card']}; color: {c['text']};
    border: 1px solid {c['border']};
    selection-background-color: {c['bg3']};
}}

QTableWidget {{
    background: {c['card']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 10px;
    gridline-color: {c['border2']};
    selection-background-color: {c['bg3']}; selection-color: {c['text']};
    alternate-background-color: {c['table_alt']};
}}
QHeaderView::section {{
    background: {c['bg2']}; color: {c['text2']};
    border: none; border-bottom: 1px solid {c['border']};
    padding: 9px 8px; font-weight: 600; font-size: 12px;
}}
QTableWidget::item {{ padding: 7px; border-bottom: 1px solid {c['border2']}; }}

QScrollBar:vertical {{ background: transparent; width: 8px; border: none; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {c['text3']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QMenu {{ background: {c['card']}; border: 1px solid {c['border']};
    border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 6px; color: {c['text']}; }}
QMenu::item:selected {{ background: {c['bg3']}; }}
QToolTip {{ background: {c['text']}; color: {c['card']};
    border: none; border-radius: 5px; padding: 5px 9px; }}
QCheckBox, QRadioButton {{ color: {c['text']}; spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1.5px solid {c['border']};
    border-radius: 4px; background: {c['card']};
}}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
QProgressBar {{ background: {c['bg3']}; border: none; border-radius: 5px; height: 7px; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 5px; }}
QGroupBox {{ background: {c['card']}; border: 1px solid {c['border']};
    border-radius: 10px; margin-top: 14px; font-weight: 700; padding-top: 14px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 16px; padding: 0 8px; color: {c['text']}; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 10px; background: {c['card']}; }}
QTabBar::tab {{ background: transparent; color: {c['text2']}; padding: 7px 18px;
    border: none; font-weight: 600; font-size: 13px; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['text']}; }}
QStatusBar {{ background: transparent; color: {c['text3']}; }}
"""


class ThemeManager:
    """主题管理器 — 三档循环 + 日落自动检查"""

    def __init__(self, main_window):
        self._mw = main_window
        self._mode = get_theme_setting()
        self._timer = QTimer(main_window)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(60000)  # 每分钟
        self._last_dark = None

    @property
    def mode(self) -> str:
        return self._mode

    def mode_icon(self) -> str:
        return MODE_ICON.get(self._mode, "◐")

    def mode_label(self) -> str:
        return MODE_LABEL.get(self._mode, "自动")

    def colors(self) -> dict:
        return resolve_colors(self._mode)

    def apply(self):
        """应用当前主题到主窗口"""
        from PySide6.QtWidgets import QApplication
        c = self.colors()
        qss = build_qss(c)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(qss)
        self._mw.setStyleSheet(qss)
        # 通知侧边栏/页面更新自己的动态样式
        if hasattr(self._mw, '_sidebar') and hasattr(self._mw._sidebar, 'apply_colors'):
            self._mw._sidebar.apply_colors(c)
        for page in getattr(self._mw, '_pages', {}).values():
            if hasattr(page, 'apply_theme'):
                try:
                    page.apply_theme()
                except Exception:
                    pass
        self._last_dark = (c is DARK)

    def cycle(self):
        """点击切换按钮：auto → light → dark → auto"""
        self._mode = next_mode(self._mode)
        set_theme_setting(self._mode)
        self.apply()

    def _on_tick(self):
        """auto模式下按日落切换"""
        if self._mode != "auto":
            return
        dark_now = bj_is_night()
        if dark_now != self._last_dark:
            self.apply()
