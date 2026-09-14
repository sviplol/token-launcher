"""Token接入器 PyInstaller 入口 — 通过 runpy 启动 src.main"""
import runpy
import sys
import os

# 确保 src 在 path 里
if getattr(sys, 'frozen', False):
    base = sys._MEIPASS
    src_dir = os.path.join(base, 'src')
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    # 也把 base 加入（资源文件在根目录）
    if base not in sys.path:
        sys.path.insert(0, base)
else:
    src_dir = os.path.dirname(os.path.abspath(__file__))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

runpy.run_module('src.main', run_name='__main__')
