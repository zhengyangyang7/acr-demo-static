#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无头驱动示例：不启动 GUI，直接调用工具核心函数跑周度更新
用法：python scripts/无头运行示例.py
前提：样例数据/ 下有源数据与数据分层表（均已脱敏）"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TOOL = os.path.join(ROOT, '周度数据更新工具.py')
SOURCE = os.path.join(ROOT, '样例数据', '新嘉理财师考核数据_截止20260918-汇总表&明细.xlsx')
TARGET = os.path.join(ROOT, '样例数据', '数据分层表_脱敏_更新至20260918.xlsx')

spec = importlib.util.spec_from_file_location('zhoudu_tool', TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ok = mod.process_file(SOURCE, TARGET, log_func=print)
sys.exit(0 if ok else 1)
