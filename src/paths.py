# -*- coding: utf-8 -*-
"""路径基座：打包成 exe 后所有数据文件落在 exe 所在目录，
源码运行时落在项目根目录——一套代码两种形态都正确。"""
import os
import sys


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)      # exe 所在目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
