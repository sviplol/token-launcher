"""全局 UI 对齐补丁 — 所有表格单元格/表头/信息标签默认居中

导入即生效（main.py 里最先 import）：
- QTableWidget：单元格文字 + 横向表头全部居中（QStyledItemDelegate 统一渲染）
- QLabel：默认水平+垂直居中（代码里显式 setAlignment 的按代码为准）
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidget, QStyledItemDelegate, QLabel


class _CenterDelegate(QStyledItemDelegate):
    """让所有单元格显示文字居中"""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignCenter


def _patch_table():
    orig_init = QTableWidget.__init__

    def patched(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        try:
            self.setItemDelegate(_CenterDelegate(self))
            self.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        except Exception:
            pass

    QTableWidget.__init__ = patched


def _patch_label():
    orig_init = QLabel.__init__

    def patched(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        try:
            self.setAlignment(Qt.AlignCenter)
        except Exception:
            pass

    QLabel.__init__ = patched


_patch_table()
_patch_label()
