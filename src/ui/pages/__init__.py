"""页面模块"""

from .dashboard import DashboardPage
from .accounts import AccountsPage
from .checkin import CheckinPage
from .settings import SettingsPage
from .hotswitch import HotSwitchPage

__all__ = [
    "DashboardPage", "AccountsPage", "CheckinPage",
    "SettingsPage", "HotSwitchPage",
]
