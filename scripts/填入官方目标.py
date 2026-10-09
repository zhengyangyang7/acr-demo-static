#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把官方下半年目标写入输出表「财富中心下半年任务」列（团队锚点行 + 公司目标行）

用法（在仓库根目录）：
  python scripts/填入官方目标.py
  python scripts/填入官方目标.py 样例数据/官方下半年目标.json
  python scripts/填入官方目标.py 样例数据/官方下半年目标.json 样例数据/数据分层表_脱敏_更新至20260918.xlsx
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from official_targets import fill_official_targets

DEFAULT_TARGETS = os.path.join(ROOT, "样例数据", "官方下半年目标.json")
DEFAULT_BOOK = os.path.join(ROOT, "样例数据", "数据分层表_脱敏_更新至20260918.xlsx")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    targets = argv[0] if len(argv) >= 1 else DEFAULT_TARGETS
    book = argv[1] if len(argv) >= 2 else DEFAULT_BOOK
    if not os.path.isabs(targets):
        targets = os.path.join(ROOT, targets)
    if not os.path.isabs(book):
        book = os.path.join(ROOT, book)
    if not os.path.isfile(targets):
        raise SystemExit(f"找不到目标文件: {targets}")
    if not os.path.isfile(book):
        raise SystemExit(f"找不到分层表: {book}")
    result = fill_official_targets(book, targets)
    print(f"目标文件: {targets}")
    print(f"分层表: {book}")
    print(f"Sheet: {result.sheet}  列: {result.column}")
    print(f"已写入 {result.written_teams} 个团队锚点行；公司目标: "
          f"{'已写' if result.company_written else '未写'}")
    print("已保存")


if __name__ == "__main__":
    main()
