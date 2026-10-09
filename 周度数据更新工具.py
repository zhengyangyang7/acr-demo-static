#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新嘉理财师考核数据 - 周度数据自动更新工具
功能：读取最新汇总表&明细 → 自动在传统客户数据分层中新建周度sheet并填入数据
版本：v6.2 (2026-06-08)
更新：
  v6.2 - 改用 onedir 打包解决启动闪退；添加异常日志输出到文件
  v6.1 - 修复历史月份数据被覆盖问题（retro_update_sheet 加 all_months 过滤）
  v6.0 - 全量重建模式：每次按数据源重新分组，团队排序保持不变
  v4.9 - 样式统一修复：边框按行类型区分
"""

import os
import sys
import re
import copy
import traceback
import datetime as _dt

from datetime import datetime
import threading

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    tk = None
    ttk = filedialog = messagebox = scrolledtext = None

VERSION = "v6.2"

try:
    from openpyxl import load_workbook
    from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side,
                                  numbers)
    from openpyxl.utils import get_column_letter, column_index_from_string
    from openpyxl.utils.cell import coordinate_from_string
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl"])
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter, column_index_from_string
    from openpyxl.utils.cell import coordinate_from_string


# ─────────────────────── 核心逻辑 ───────────────────────

def extract_date_from_filename(filename):
    """从文件名提取日期，如 新嘉理财师考核数据_截止20260313-汇总表&明细.xlsx → 20260313"""
    basename = os.path.basename(filename)
    m = re.search(r'截止(\d{8})', basename)
    if m:
        return m.group(1)
    return None


def parse_date_str(date_str):
    """将 20260313 解析为 datetime"""
    return datetime.strptime(date_str, "%Y%m%d")


def get_month_str(date_str):
    """20260313 → 202603"""
    return date_str[:6]


def get_year_str(date_str):
    """20260313 → 2026"""
    return date_str[:4]


def get_sheet_suffix(date_str):
    """20260313 → 0313"""
    return date_str[4:]


def load_source_data(source_file, date_str):
    """
    从汇总表&明细的'汇总-折标规模' sheet 读取数据
    返回:
      data        : dict {理财师姓名: {传统YYYYMM: v, ...}}  ← 仅当月字段
      field_cols  : dict {字段名: 列号}                      ← 当月字段列号
      all_data    : dict {理财师姓名: {传统YYYYMM: v, ...}}  ← 所有月份历史字段
      all_months  : set  {'202601', '202602', ...}           ← 源文件中有哪些月份
    """
    month_str = get_month_str(date_str)

    wb = load_workbook(source_file, data_only=True)
    if '汇总-折标规模' not in wb.sheetnames:
        raise ValueError(f"源文件中未找到'汇总-折标规模' sheet，请确认文件格式正确。")
    ws = wb['汇总-折标规模']

    # 读取表头（第1行）
    headers = {}
    for cell in ws[1]:
        if cell.value is not None:
            headers[cell.column] = str(cell.value).strip()

    # 找当月字段列
    target_fields = [f'传统{month_str}', f'保险{month_str}', f'管理费{month_str}']
    field_cols = {}
    for col_idx, col_name in headers.items():
        if col_name in target_fields:
            field_cols[col_name] = col_idx

    missing = [f for f in target_fields if f not in field_cols]
    if missing:
        raise ValueError(
            f"在'汇总-折标规模'中未找到以下字段: {missing}\n"
            f"请确认该文件确实包含 {month_str} 月份的数据。"
        )

    # 找所有历史月份字段（传统/保险/管理费 + YYYYMM）
    all_month_field_cols = {}   # {字段名: 列号}，如 {'传统202601': 23, ...}
    all_months = set()
    for col_idx, col_name in headers.items():
        m = re.match(r'^(传统|保险|管理费)(\d{6})$', col_name)
        if m:
            all_month_field_cols[col_name] = col_idx
            all_months.add(m.group(2))

    # 找考核期间已完成折标列（G 列）
    kaohefold_col = None
    for col_idx, col_name in headers.items():
        if col_name == '考核期间已完成折标':
            kaohefold_col = col_idx
            break

    # 找考核起始日、终止日、任务目标、职级、团队长列
    kaohe_start_col  = None   # 考核期起始日
    kaohe_end_col    = None   # 考核期终止日
    kaohe_target_col = None   # 考核任务目标（按实际考核月数折算）
    rank_col         = None   # 当前考核期职级
    leader_col       = None   # 团队长
    for col_idx, col_name in headers.items():
        if col_name == '考核期起始日':
            kaohe_start_col = col_idx
        elif col_name == '考核期终止日':
            kaohe_end_col = col_idx
        elif col_name == '考核任务目标（按实际考核月数折算）':
            kaohe_target_col = col_idx
        elif col_name == '当前考核期职级':
            rank_col = col_idx
        elif col_name == '团队长':
            leader_col = col_idx

    # 读取每行理财师数据（当月 + 全量历史）
    data     = {}   # 仅当月
    all_data = {}   # 全量历史（含特殊键）
    for row in ws.iter_rows(min_row=2, values_only=True):
        name = row[1]  # B列，理财师
        if not name or not isinstance(name, str):
            continue
        name = name.strip()
        # 当月数据
        row_data = {}
        for field, col_idx in field_cols.items():
            val = row[col_idx - 1]
            row_data[field] = val if val is not None else 0
        # 额外字段（职级、团队长、考核起止日、任务目标）
        row_data['当前考核期职级'] = row[rank_col - 1] if rank_col else None
        row_data['团队长'] = row[leader_col - 1] if leader_col else None
        row_data['考核任务目标（按实际考核月数折算）'] = row[kaohe_target_col - 1] if kaohe_target_col else None
        row_data['考核期起始日'] = row[kaohe_start_col - 1] if kaohe_start_col else None
        row_data['考核期终止日'] = row[kaohe_end_col - 1] if kaohe_end_col else None
        data[name] = row_data
        # 全量历史
        all_row = {}
        for field, col_idx in all_month_field_cols.items():
            val = row[col_idx - 1]
            all_row[field] = val if val is not None else 0
        # 考核期间已完成折标（最新累计值）
        if kaohefold_col:
            g_val = row[kaohefold_col - 1]
            all_row['__考核期折标__'] = g_val if g_val is not None else 0
        # 考核起始日、终止日、任务目标
        if kaohe_start_col:
            all_row['__考核起始日__'] = row[kaohe_start_col - 1]
        if kaohe_end_col:
            all_row['__考核终止日__'] = row[kaohe_end_col - 1]
        if kaohe_target_col:
            all_row['__考核任务目标__'] = row[kaohe_target_col - 1]
        all_data[name] = all_row

    return data, field_cols, all_data, all_months


def find_existing_ytd_cols(ws, row_idx, data_start_col, current_col_offset):
    """
    在已有sheet中，找到当前月份之前所有的 YYYY年M月折标 列的列索引
    用于计算YTD
    """
    # 扫描header row（row 2）找所有月折标列（形如 '2026年\n3月折标' or '202601折标'）
    ytd_cols = []
    for cell in ws[2]:
        if cell.value and isinstance(cell.value, str):
            v = cell.value.replace('\n', '')
            # 匹配 '2026年X月折标' 或 '202601折标'
            if re.search(r'20\d{2}年\d+月折标', v) or re.search(r'20\d{6}折标', v):
                ytd_cols.append(cell.column)
    return ytd_cols


def get_new_cols_insert_position(ws):
    """
    找到'考核期间已完成折标'列之后的插入位置
    返回该列的列索引
    """
    for cell in ws[2]:
        if cell.value and '考核期间已完成折标' in str(cell.value):
            return cell.column
    return None


def _norm_gi(item):
    """
    将 group_list 的单个元素标准化为 4 元组 (leader, fr, lr, sr)。
    支持输入格式：
      - 4 元组: (leader, fr, lr, sr) → 原样返回
      - 3 元组: (fr, lr, sr)    → (None, fr, lr, sr)
    """
    if len(item) == 4:
        return item
    else:
        return (None, item[0], item[1], item[2])


def _fix_data_area_borders(ws, group_list, company_total_row=None,
                            col_start=1, col_end=None, log_func=print):
    """
    修复数据区域的边框和填充，按行类型区分处理。
    只修改边框和填充，不碰字体、对齐。

    边框规则（与原始手动sheet一致）：
      - 团队负责人行：bottom=medium（分组下界标记）
      - 合计行：top=medium, bottom=medium
      - 数据行：四边 thin

    填充规则：
      - 数据行（含其他团队成员行）：白色实色填充 #FFFFFFFF
      - 合计行：保持原有填充色（粉色/黄色），不覆盖
    """
    from openpyxl.cell import MergedCell as _MC_bdr
    from openpyxl.styles import PatternFill
    import copy as _copy

    if col_end is None:
        col_end = ws.max_column

    thin = Side(style='thin')
    medium = Side(style='medium')
    white_fill = PatternFill(start_color='FFFFFFFF', end_color='FFFFFFFF', patternType='solid')

    # 构建行类型映射
    # group_list 元素格式有两种：
    #   - 4 元组: (leader_name, first_row, last_row, sum_row)
    #   - 3 元组: (first_row, last_row, sum_row)
    # fr = 团队负责人行，用 bottom=medium 标记分组下界
    leader_rows = set()   # 团队负责人行（每个 fr）
    sum_rows = set()      # 合计行
    data_rows = set()     # 所有数据行（含负责人）
    for item in group_list:
        if len(item) == 4:
            _leader, fr, lr, sr = item
        else:
            fr, lr, sr = item
        leader_rows.add(fr)
        if sr:
            sum_rows.add(sr)
        for r in range(fr, lr + 1):
            data_rows.add(r)
    if company_total_row:
        sum_rows.add(company_total_row)

    def _has_real_fill(cell):
        """判断单元格是否有真实填充色（theme 或 rgb）"""
        f = cell.fill
        if f.patternType != 'solid':
            return False
        sc = f.start_color
        if sc is None:
            return False
        if sc.type == 'theme':
            return True
        if sc.type == 'rgb':
            return sc.rgb not in ('00000000', None, '')
        return False

    # 对每行每列修复边框和填充
    for r in range(4, ws.max_row + 1):
        if r not in data_rows and r not in sum_rows:
            continue
        for c in range(col_start, col_end + 1):
            cell = ws.cell(r, c)
            if isinstance(cell, _MC_bdr):
                continue

            old = cell.border
            old_top = old.top
            old_bottom = old.bottom
            old_left = old.left
            old_right = old.right

            if r in leader_rows:
                # 负责人行：top/left/right=thin, bottom=medium
                cell.border = Border(
                    top=thin,
                    bottom=medium,
                    left=old_left if old_left and old_left.style else thin,
                    right=old_right if old_right and old_right.style else thin,
                )
            elif r in sum_rows:
                # 合计行：top=medium, bottom=medium
                cell.border = Border(
                    top=medium,
                    bottom=medium,
                    left=old_left if old_left and old_left.style in ('thin', 'medium') else thin,
                    right=old_right if old_right and old_right.style in ('thin', 'medium') else thin,
                )
            else:
                # 普通数据行：四边 thin + 白色填充
                has_any = any(s and s.style for s in [old_top, old_bottom, old_left, old_right])
                if not has_any:
                    cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)
                # 确保数据行有白色填充（修复 insert_rows 后无填充的问题）
                if not _has_real_fill(cell):
                    cell.fill = _copy.copy(white_fill)


def copy_cell_style(src_cell, dst_cell):
    """复制单元格样式（字体、填充、边框、对齐、数字格式）"""
    dst_cell.font      = copy.copy(src_cell.font)
    dst_cell.fill      = copy.copy(src_cell.fill)
    dst_cell.border    = copy.copy(src_cell.border)
    dst_cell.alignment = copy.copy(src_cell.alignment)
    dst_cell.number_format = src_cell.number_format


def apply_data_cell_style(ws, row_idx, col_idx, ref_col_idx):
    """
    将 ref_col_idx 列（row=2 表头行对应的数据格式）复制到 col_idx
    直接用 ref_col_idx 的同行单元格；若无有效样式则向左扫描
    """
    ref_cell = ws.cell(row=row_idx, column=ref_col_idx)
    # 判断是否有真实样式（边框不为 None 且非 00000000 填充）
    def _has_real_style(cell):
        if not cell.has_style:
            return False
        if cell.border.top.border_style is not None:
            return True
        return False

    if not _has_real_style(ref_cell):
        for c in range(ref_col_idx - 1, 0, -1):
            candidate = ws.cell(row=row_idx, column=c)
            if _has_real_style(candidate):
                ref_cell = candidate
                break
    copy_cell_style(ref_cell, ws.cell(row=row_idx, column=col_idx))


def retro_update_sheet(ws, all_data, log_func=print, skip_month=None, all_months=None):
    """
    回溯更新：
    ① 对 sheet 中展开了 传统YYYYMM/保险YYYYMM/管理费YYYYMM 三列的月份：
       用 all_data 最新值覆盖三列，折标列写 SUM 公式。
    ② 对 sheet 中只有 YYYYMM折标（单列）的历史月份：
       直接用 all_data[理财师][传统YYYYMM] + 保险YYYYMM + 管理费YYYYMM 求和写入折标列。

    all_data:   {理财师姓名: {传统YYYYMM: v, ..., __考核起始日__: dt, ...}}
    skip_month: str | None，跳过当月（已由 update_sheet 处理）
    all_months: set | None，源文件中实际存在的月份集合（如 {'202601','202602'}）
                为 None 时（兼容旧调用），不限制月份，全部更新
    """
    from openpyxl.cell import MergedCell as _MC_rtr

    # 收集 all_months 中实际出现在 all_data 里的月份（用于安全过滤）
    valid_months = all_months if all_months else None

    def _safe_set(row, col, value):
        """安全写值：若目标格是 MergedCell 从属格，写入合并首格"""
        c = ws.cell(row, col)
        if isinstance(c, _MC_rtr):
            # 找到包含此格的合并区域，写入首格
            for mr in ws.merged_cells.ranges:
                if mr.min_row <= row <= mr.max_row and mr.min_col <= col <= mr.max_col:
                    ws.cell(mr.min_row, mr.min_col).value = value
                    return
        c.value = value

    # ── 扫描第2行，建立月份→列映射 ──
    # month_col_map[YYYYMM] = {
    #   '传统': col | None,
    #   '保险': col | None,
    #   '管理费': col | None,
    #   '折标': col | None,   ← 单列折标（历史月份，或当月折标列）
    # }
    month_col_map = {}

    def _ensure(mstr):
        if mstr not in month_col_map:
            month_col_map[mstr] = {'传统': None, '保险': None, '管理费': None, '折标': None}

    for cell in ws[2]:
        if isinstance(cell, _MC_rtr):
            continue
        if not cell.value:
            continue
        v = str(cell.value).replace('\n', '').strip()

        # 展开列：传统YYYYMM / 保险YYYYMM / 管理费YYYYMM
        m = re.match(r'^(传统|保险|管理费)(\d{6})$', v)
        if m:
            kind, mstr = m.group(1), m.group(2)
            _ensure(mstr)
            month_col_map[mstr][kind] = cell.column
            continue

        # 折标列格式1：YYYY年N月折标
        m2 = re.match(r'^(\d{4})年(\d+)月折标$', v)
        if m2:
            yr, mn = m2.group(1), m2.group(2).zfill(2)
            mstr = f'{yr}{mn}'
            _ensure(mstr)
            month_col_map[mstr]['折标'] = cell.column
            continue

        # 折标列格式2：202601折标
        m3 = re.match(r'^(\d{6})折标$', v)
        if m3:
            mstr = m3.group(1)
            _ensure(mstr)
            month_col_map[mstr]['折标'] = cell.column
            continue

    if not month_col_map:
        log_func(f"  该sheet无月份数据列，跳过回溯更新")
        return

    # ── 扫描数据行（理财师行）和合计行 ──
    data_rows = []
    group_list = []   # (first_data_row, last_data_row, sum_row)
    cur_start = None
    cur_end   = None
    for row_idx in range(4, ws.max_row + 1):
        a_val = ws.cell(row_idx, 1).value
        b_val = ws.cell(row_idx, 2).value
        is_sum = a_val and '合计' in str(a_val)
        has_name = b_val and isinstance(b_val, str) and b_val.strip() and not is_sum

        if is_sum:
            if cur_start is not None:
                group_list.append((cur_start, cur_end, row_idx))
            cur_start = None
            cur_end   = None
        elif has_name:
            if cur_start is None:
                cur_start = row_idx
            cur_end = row_idx
            data_rows.append((row_idx, b_val.strip()))

    updated_months = []
    for mstr, cols in month_col_map.items():
        # 跳过当月（已由 update_sheet 处理）
        if skip_month and mstr == skip_month:
            continue
        # ✅ 关键修复：只回溯更新源文件中实际存在的月份
        #    源文件没有的月份，保持 sheet 原值不动，避免被覆盖成 0
        if valid_months is not None and mstr not in valid_months:
            log_func(f"  跳过月份 {mstr}（源文件中无此月份数据）")
            continue

        ct_col = cols['传统']
        bx_col = cols['保险']
        gl_col = cols['管理费']
        zb_col = cols['折标']

        # 判断模式
        has_expanded = ct_col and bx_col and gl_col   # 三列展开模式
        has_zb_only  = zb_col and not has_expanded    # 仅折标列模式（历史单列）

        if not has_expanded and not has_zb_only:
            continue  # 没有可更新的列

        updated_months.append(mstr)

        if has_expanded:
            # ── 模式一：展开三列 → 更新三列，折标列写 SUM ──
            for row_idx, name in data_rows:
                if name not in all_data:
                    continue
                person = all_data[name]
                ct_val = person.get(f'传统{mstr}', 0) or 0
                bx_val = person.get(f'保险{mstr}', 0) or 0
                gl_val = person.get(f'管理费{mstr}', 0) or 0
                _safe_set(row_idx, ct_col, ct_val)
                _safe_set(row_idx, bx_col, bx_val)
                _safe_set(row_idx, gl_col, gl_val)
                if zb_col:
                    lc = get_column_letter(ct_col)
                    lg = get_column_letter(gl_col)
                    _safe_set(row_idx, zb_col, f'=SUM({lc}{row_idx}:{lg}{row_idx})')
            for (fr, lr, sr) in group_list:
                _safe_set(sr, ct_col, f'=SUM({get_column_letter(ct_col)}{fr}:{get_column_letter(ct_col)}{lr})')
                _safe_set(sr, bx_col, f'=SUM({get_column_letter(bx_col)}{fr}:{get_column_letter(bx_col)}{lr})')
                _safe_set(sr, gl_col, f'=SUM({get_column_letter(gl_col)}{fr}:{get_column_letter(gl_col)}{lr})')
                if zb_col:
                    _safe_set(sr, zb_col, f'=SUM({get_column_letter(zb_col)}{fr}:{get_column_letter(zb_col)}{lr})')

        else:
            # ── 模式二：仅折标单列 → 直接用源文件三科目求和 ──
            lz = get_column_letter(zb_col)
            for row_idx, name in data_rows:
                if name not in all_data:
                    continue
                person = all_data[name]
                ct_val = person.get(f'传统{mstr}', 0) or 0
                bx_val = person.get(f'保险{mstr}', 0) or 0
                gl_val = person.get(f'管理费{mstr}', 0) or 0
                _safe_set(row_idx, zb_col, ct_val + bx_val + gl_val)
            for (fr, lr, sr) in group_list:
                _safe_set(sr, zb_col, f'=SUM({lz}{fr}:{lz}{lr})')

    if updated_months:
        log_func(f"  回溯更新月份: {sorted(updated_months)}")
    else:
        log_func(f"  无需回溯更新")

    # ── 回溯更新 G 列（考核期间已完成折标）用最新累计值覆盖 ──
    g_col = None
    for cell in ws[2]:
        if cell.value and '考核期间已完成折标' in str(cell.value):
            g_col = cell.column
            break
    if g_col:
        for row_idx, name in data_rows:
            if name in all_data:
                g_val = all_data[name].get('__考核期折标__', None)
                if g_val is not None:
                    _safe_set(row_idx, g_col, g_val)
        # 合计行 SUM
        for (fr, lr, sr) in group_list:
            _safe_set(sr, g_col, f'=SUM({get_column_letter(g_col)}{fr}:{get_column_letter(g_col)}{lr})')
        log_func(f"  回溯更新 G 列（考核期间已完成折标）")

    # ── 回溯更新 YTD 折标列（2026年YTD折标）──
    from openpyxl.cell import MergedCell as _MC_rb
    ytd_col = None
    year_zb_cols = []
    for cell in ws[2]:
        if isinstance(cell, _MC_rb):
            continue
        if not cell.value:
            continue
        v = str(cell.value).replace('\n', '').strip()
        # 找当年所有月折标列（精确匹配，排除 YTD）
        if re.search(r'^20\d{2}年\d+月折标$', v):
            year_zb_cols.append(cell.column)
        # 找 YTD 列
        if re.search(r'20\d{2}年YTD折标', v):
            ytd_col = cell.column

    if ytd_col and year_zb_cols:
        for row_idx, name in data_rows:
            refs = '+'.join([f'{get_column_letter(c)}{row_idx}' for c in year_zb_cols])
            _safe_set(row_idx, ytd_col, f'=SUM({refs})')
        # 合计行
        for (fr, lr, sr) in group_list:
            refs = '+'.join([f'{get_column_letter(c)}{sr}' for c in year_zb_cols])
            _safe_set(sr, ytd_col, f'=SUM({refs})')
        log_func(f"  回溯更新 YTD 折标列（含{len(year_zb_cols)}个月份）")

    # ── 回溯更新 D/E/F 列（考核起止日+任务目标）+ 月完成率公式 ──
    # 月折标列：取列号最大（最新月份）的月折标列作为分子
    # 用 _find_latest_month_zb_col 而非 year_zb_cols[-1]，避免合并单元格漏扫
    latest_month_zb_col = _find_latest_month_zb_col(ws)
    if latest_month_zb_col is None and year_zb_cols:
        latest_month_zb_col = min(year_zb_cols)   # 兜底：新月在左，取列号最小
    if all_data and latest_month_zb_col:
        _update_kaohe_def_cols(ws, data_rows, all_data, latest_month_zb_col, log_func,
                               group_list=group_list)

    # ── 回溯更新财富中心相关列（考核期差距，完成率、任务差距）──
    _update_fc_cols(ws, group_list, ytd_col, log_func)

    # ── 回溯更新后：重新合并数据行的关键列（在取消合并写入公式后重新合并）──
    # 注意：月度完成率、考核期完成率、考核期差距列需要逐行独立显示，不应合并
    # 只有任务差距、财富中心上半年任务列需要合并（引用合计行的值）
    def _remerge_data_cols(ws, group_list):
        """重新合并数据行的关键列"""
        from openpyxl.cell import MergedCell as _MC_rm
        merge_target_cols = []
        for cell in ws[2]:
            if cell.value is None:
                continue
            v = str(cell.value).replace('\n', '').strip()
            # 只合并H1模块差距列、财富中心上半年任务列；月度完成率、考核期完成率、考核期差距不合并
            # （H2模块差距列由 _add_h2_module 自行合并，此处须排除避免重复合并）
            if v in ('任务差距', '上半年任务差距', '财富中心上半年任务差距'):
                merge_target_cols.append(cell.column)
            elif ('上半年任务' in v or '上半年年任务' in v) and '完成率' not in v and '差距' not in v:
                merge_target_cols.append(cell.column)
            # 注意：月度完成率、考核期完成率、考核期差距列不合并
        # 执行合并
        for col in merge_target_cols:
            for (fr, lr, sr) in group_list:
                if sr is None:
                    continue
                if fr < lr:
                    ws.merge_cells(start_row=fr, start_column=col, end_row=lr, end_column=col)
    _remerge_data_cols(ws, group_list)
    log_func(f"  回溯更新后已重新合并数据行关键列（仅任务差距/财富中心上半年任务）")

    # ── 回溯更新公司合计行（标黄行）汇总公式 ──
    _update_company_total_row(ws, group_list, ytd_col, log_func)

    # ── 修复 Row2:Row3 表头合并（新字段补合并）──
    _fix_header_row23_merges(ws, log_func)

    # ── 修复合计行颜色（新增列补填充色）──
    _fix_sum_row_fill(ws, group_list, log_func)

    # ── 重新修复数据区域边框（retro_update 中的取消合并/重新合并会破坏边框）──
    company_total_row = _find_company_total_row(ws, group_list)
    max_data_col = 1
    for cell in ws[2]:
        if cell.value is not None:
            max_data_col = max(max_data_col, cell.column)
    _fix_data_area_borders(ws, group_list, company_total_row,
                           col_start=1, col_end=max_data_col,
                           log_func=log_func)
    log_func(f"  回溯更新后已重新修复数据区域边框")


def _fix_header_row23_merges(ws, log_func=print):
    """
    扫描 Row 2 所有有值的单元格，若该列 Row2:Row3 还未合并，则补合并并居中。
    用于修复 U/V/W/X 等新字段表头未合并的问题。
    """
    from openpyxl.cell import MergedCell as _MC
    fixed = []
    for cell in ws[2]:
        if isinstance(cell, _MC) or cell.value is None:
            continue
        col_idx = cell.column
        # 检查 Row2:Row3 是否已合并
        already = any(
            mr.min_col <= col_idx <= mr.max_col and mr.min_row <= 2 and mr.max_row >= 3
            for mr in ws.merged_cells.ranges
        )
        if not already:
            lc = get_column_letter(col_idx)
            # 若 row3 也在某合并范围里，先移除
            to_rm = [mr for mr in ws.merged_cells.ranges
                     if mr.min_col <= col_idx <= mr.max_col and 3 in range(mr.min_row, mr.max_row + 1)]
            for mr in to_rm:
                ws.merged_cells.remove(mr)
            ws.merge_cells(f'{lc}2:{lc}3')
            cell.alignment = Alignment(
                horizontal='center', vertical='center',
                wrap_text=getattr(cell.alignment, 'wrap_text', True)
            )
            fixed.append(lc)
    if fixed:
        log_func(f"  已补合并 Row2:Row3 表头列: {fixed}")


def _hide_old_month_data_cols(ws, current_month_str, log_func=print):
    r"""
    隐藏往月的传统/保险/管理费列，只显示当月（current_month_str，如 '202603'）三列。
    识别规则：Row2 表头匹配 ^(传统|保险|管理费)\d{6}$ 且月份后缀 != current_month_str 的列。
    隐藏方式：设置 column_dimensions[col_letter].hidden = True。
    当月三列重新设为可见（hidden = False），避免多次运行后状态混乱。
    """
    from openpyxl.cell import MergedCell as _MC
    hidden_count = 0
    shown_count = 0
    for cell in ws[2]:
        if isinstance(cell, _MC):
            continue
        if not cell.value:
            continue
        v = str(cell.value).replace('\n', '').strip()
        m = re.match(r'^(传统|保险|管理费)(\d{6})$', v)
        if not m:
            continue
        col_letter = get_column_letter(cell.column)
        month_suffix = m.group(2)
        if month_suffix == current_month_str:
            ws.column_dimensions[col_letter].hidden = False
            shown_count += 1
        else:
            ws.column_dimensions[col_letter].hidden = True
            hidden_count += 1
    if hidden_count or shown_count:
        log_func(f"  已隐藏 {hidden_count} 个往月数据列，当月 {shown_count} 列保持可见")


def _update_progress_module(ws, date_str, log_func=print):
    """
    更新全年进度模块（AA列区域）：
    布局（以 0313 sheet 为例）：
      Row4: AA4=开始日期  AB4=结束日期
      Row5: AA5=起始日   AB5=结束日
      Row6: AA6=计算日期  [AB6空]  AC6=时间进度  ← 上半年时间进度标签行
      Row7: AA7=截止日   AB7=实际天数  AC7=时间进度值  ← 数值行
      Row9: AA9=计算日期  AB9=365  AC9=全年进度  ← 全年进度标签行
      Row10: AA10=截止日  [AB10空]  AC10=全年进度值  ← 数值行
    逻辑：
      AA7 = 截止日期；AB7 = AA7 - AA5 + 1（含首末天）；AC7 = AB7/(AB5-AA5+1)
      AA10 = 截止日期；AC10 = (AA10-DATE(YEAR(AA10),1,1)+1)/AB9
    """
    from datetime import datetime as _dt
    try:
        cutoff_date = _dt.strptime(date_str, '%Y%m%d').date()
    except Exception:
        log_func(f"  全年进度模块：无法解析日期 {date_str}")
        return

    # ── 根据 cutoff_date 自动判断上半年 / 下半年 ──
    year = cutoff_date.year
    if cutoff_date.month <= 6:
        # 上半年
        period_start = _dt(year, 1, 1).date()
        period_end   = _dt(year, 6, 30).date()
        period_name  = '上半年'
    else:
        # 下半年
        period_start = _dt(year, 7, 1).date()
        period_end   = _dt(year, 12, 31).date()
        period_name  = '下半年'

    # ── 精确扫描：找"开始日期"标签、"计算日期+时间进度"标签、"计算日期+全年进度"标签 ──
    # 只在列号 >= 25（即 Y 列以右）的区域搜索，排除主表区域
    # 上限设为100以适应后续周次中模块右移的情况
    START_COL = 25
    SCAN_END = 100

    start_label_row = None   # 含"开始日期"和"结束日期"的行
    prog_label_row  = None   # 含"计算日期"+"时间进度"的行（上下半年进度）
    fullyr_label_row = None  # 含"计算日期"+"全年进度"的行

    for r in range(1, ws.max_row + 1):
        texts_with_col = {}
        for c_idx in range(START_COL, min(ws.max_column + 1, SCAN_END)):
            v = ws.cell(r, c_idx).value
            if v and isinstance(v, str):
                texts_with_col[c_idx] = v.strip()
        texts = ' '.join(texts_with_col.values())
        if '开始日期' in texts and '结束日期' in texts:
            start_label_row = r
        if '计算日期' in texts and '时间进度' in texts and '全年' not in texts:
            prog_label_row = r
        if '计算日期' in texts and '全年进度' in texts:
            fullyr_label_row = r

    if not prog_label_row:
        log_func(f"  全年进度模块：未找到「计算日期/时间进度」标签行，跳过")
        return
    if not start_label_row:
        log_func(f"  全年进度模块：未找到「开始日期/结束日期」标签行，跳过")
        return

    start_date_row = start_label_row + 1   # 开始/结束日期数值行（如 Row5）
    prog_data_row  = prog_label_row  + 1   # 时间进度数值行（如 Row7）

    # 找"计算日期"标签所在列（上下半年进度那行）
    prog_calc_col = None
    prog_rate_col = None
    for c_idx in range(START_COL, SCAN_END):
        v = ws.cell(prog_label_row, c_idx).value
        if v and '计算日期' in str(v):
            prog_calc_col = c_idx
        if v and '时间进度' in str(v):
            prog_rate_col = c_idx

    if not prog_calc_col:
        log_func(f"  全年进度模块：找不到计算日期列")
        return

    # AB（实际天数）= 计算日期列 + 1；AC（时间进度）= 找到的时间进度列
    prog_days_col = prog_calc_col + 1
    if not prog_rate_col:
        prog_rate_col = prog_calc_col + 2

    # 找"开始日期"和"结束日期"列
    start_col = end_col = None
    for c_idx in range(START_COL, SCAN_END):
        v = ws.cell(start_label_row, c_idx).value
        if v and '开始日期' in str(v):
            start_col = c_idx
        if v and '结束日期' in str(v):
            end_col = c_idx
    if not start_col:
        start_col = prog_calc_col
    if not end_col:
        end_col = prog_calc_col + 1

    # ── 更新开始日期 / 结束日期为当前周期的起止日期 ──
    ws.cell(start_date_row, start_col).value = period_start
    ws.cell(start_date_row, start_col).number_format = 'yyyy/mm/dd'
    ws.cell(start_date_row, end_col).value = period_end
    ws.cell(start_date_row, end_col).number_format = 'yyyy/mm/dd'

    lc_calc  = get_column_letter(prog_calc_col)
    lc_days  = get_column_letter(prog_days_col)
    lc_rate  = get_column_letter(prog_rate_col)
    lc_start = get_column_letter(start_col)
    lc_end   = get_column_letter(end_col)

    start_ref = f'{lc_start}{start_date_row}'   # 开始日期（如 AA5）
    end_ref   = f'{lc_end}{start_date_row}'     # 结束日期（如 AB5）
    calc_ref  = f'{lc_calc}{prog_data_row}'     # 截止日期（如 AA7）
    days_ref  = f'{lc_days}{prog_data_row}'     # 全程天数（如 AB7）

    # ── 1. 写截止日期（计算日期）= 数据源截止日期 ──
    ws.cell(prog_data_row, prog_calc_col).value = cutoff_date
    ws.cell(prog_data_row, prog_calc_col).number_format = 'yyyy/mm/dd'

    # ── 2. 写全程总天数 = 结束日 - 开始日 + 1（固定值，不随截止日变化）──
    ws.cell(prog_data_row, prog_days_col).value = f'={end_ref}-{start_ref}+1'
    ws.cell(prog_data_row, prog_days_col).number_format = '0'

    # ── 3. 写时间进度 = 已完成天数(截止-开始+1) / 全程总天数(AB7) ──
    ws.cell(prog_data_row, prog_rate_col).value = f'=({calc_ref}-{start_ref}+1)/{days_ref}'
    ws.cell(prog_data_row, prog_rate_col).number_format = '0.00%'

    log_func(f"  {period_name}进度：截止日期={cutoff_date}, "
             f"区间={period_start}~{period_end}, "
             f"全程天数公式={end_ref}-{start_ref}+1, 时间进度=({calc_ref}-{start_ref}+1)/{days_ref}")

    # ── 4. 全年进度行 ──
    if fullyr_label_row:
        fy_data_row = fullyr_label_row + 1

        fy_calc_col = fy_days_col = fy_rate_col = None
        for c_idx in range(START_COL, SCAN_END):
            v = ws.cell(fullyr_label_row, c_idx).value
            if v and '计算日期' in str(v):
                fy_calc_col = c_idx
            if isinstance(v, (int, float)) and v > 300:
                fy_days_col = c_idx   # 365 这个数在标签行（如 AB9=365）
            if v and '全年进度' in str(v):
                fy_rate_col = c_idx

        if not fy_calc_col:
            fy_calc_col = prog_calc_col
        if not fy_rate_col:
            fy_rate_col = (fy_calc_col + 2) if fy_calc_col else prog_rate_col
        if not fy_days_col:
            fy_days_col = fy_calc_col + 1

        lc_fy_calc = get_column_letter(fy_calc_col)
        lc_fy_days = get_column_letter(fy_days_col)
        lc_fy_rate = get_column_letter(fy_rate_col)

        ws.cell(fy_data_row, fy_calc_col).value = cutoff_date
        ws.cell(fy_data_row, fy_calc_col).number_format = 'yyyy/mm/dd'

        fy_calc_ref  = f'{lc_fy_calc}{fy_data_row}'
        fy_days_ref  = f'{lc_fy_days}{fullyr_label_row}'  # 365 在标签行
        ws.cell(fy_data_row, fy_rate_col).value = (
            f'=({fy_calc_ref}-DATE(YEAR({fy_calc_ref}),1,1)+1)/{fy_days_ref}'
        )
        ws.cell(fy_data_row, fy_rate_col).number_format = '0.00%'

        log_func(f"  全年进度：计算日期={cutoff_date}, "
                 f"公式=({fy_calc_ref}-DATE(YEAR,1,1)+1)/{fy_days_ref}")


def _fix_sum_row_fill(ws, group_list, log_func=print):
    """
    修复合计行中无填充颜色的单元格：
    扫描合计行所有列，若某列没有填充色，则从同行的有色列（或参考其他合计行）复制 fill。
    同时处理不在 group_list 中的全局总合计行。

    颜色检测支持 theme / rgb 两种类型：
      - theme: start_color.type == 'theme' 且 patternType == 'solid'
      - rgb:   patternType == 'solid' 且 rgb 不为 00000000/None/''

    若某合计行整行都无填充色（如 insert_rows 新增的空行），
    则从其他已有颜色的合计行借用相同列位置的 fill。
    """
    # 标准化 group_list 为 4 元组 (leader, fr, lr, sr)
    group_list = [_norm_gi(item) for item in group_list if item]
    from openpyxl.cell import MergedCell as _MC
    import copy as _copy

    def _has_real_fill(cell):
        """判断单元格是否有真实填充色（theme 或 rgb）"""
        f = cell.fill
        if f.patternType != 'solid':
            return False
        sc = f.start_color
        if sc is None:
            return False
        if sc.type == 'theme':
            return True
        if sc.type == 'rgb':
            return sc.rgb not in ('00000000', None, '')
        return False

    def _copy_fill(cell):
        """安全复制 fill 样式"""
        return _copy.copy(cell.fill)

    # 收集所有合计行（来自 group_list + 全局扫描）
    sum_rows = set(sr for (_, _, _, sr) in group_list)
    for r in range(1, ws.max_row + 1):
        a_val = ws.cell(r, 1).value
        if a_val and '合计' in str(a_val):
            sum_rows.add(r)

    if not sum_rows:
        return

    # 第一轮：收集所有合计行的填充信息
    row_fill_map = {}  # {row_num: {col_idx: fill}}
    fully_blank_rows = set()
    for sr in sorted(sum_rows):
        colored_fills = {}
        for c_idx in range(1, ws.max_column + 1):
            cell = ws.cell(sr, c_idx)
            if isinstance(cell, _MC):
                continue
            if _has_real_fill(cell):
                colored_fills[c_idx] = _copy_fill(cell)
        row_fill_map[sr] = colored_fills
        if not colored_fills:
            fully_blank_rows.add(sr)

    # 第二轮：为完全无色的合计行，从其他有色的合计行借用 fill
    # 构建参考映射：{col_idx: fill}，从所有有色合计行中取最常见的
    if fully_blank_rows:
        ref_fills_by_col = {}
        ref_fills_count = {}
        for sr, fills in row_fill_map.items():
            if sr in fully_blank_rows:
                continue
            for c_idx, fill in fills.items():
                if c_idx not in ref_fills_by_col:
                    ref_fills_by_col[c_idx] = fill
                    ref_fills_count[c_idx] = 1
                else:
                    ref_fills_count[c_idx] += 1
        # 对每个完全无色的行，用参考 fill 填充
        for sr in sorted(fully_blank_rows):
            new_fills = {}
            for c_idx in range(1, ws.max_column + 1):
                cell = ws.cell(sr, c_idx)
                if isinstance(cell, _MC):
                    continue
                if c_idx in ref_fills_by_col:
                    cell.fill = _copy.copy(ref_fills_by_col[c_idx])
                    new_fills[c_idx] = cell.fill
            row_fill_map[sr] = new_fills
            if new_fills:
                log_func(f"  已为无填充合计行 Row {sr} 补充 {len(new_fills)} 列填充色（参考其他合计行）")

    # 第三轮：修复合计行中部分列无填充的情况
    # 重要：只修补"空洞"（两个有色列之间的无色列），不向右扩散到数据区域外
    fixed_total = 0
    for sr in sorted(sum_rows):
        colored_fills = row_fill_map.get(sr, {})
        if not colored_fills:
            continue

        # 计算该行填充的右边界（最右有色列的列号）
        max_filled_col = max(colored_fills.keys())
        ref_fill = list(colored_fills.values())[0]
        for c_idx in range(1, max_filled_col + 1):
            cell = ws.cell(sr, c_idx)
            if isinstance(cell, _MC):
                continue
            if not _has_real_fill(cell):
                # 优先用同列左侧最近的有色列的 fill
                fill_to_use = ref_fill
                for lc in range(c_idx - 1, 0, -1):
                    if lc in colored_fills:
                        fill_to_use = colored_fills[lc]
                        break
                cell.fill = _copy.copy(fill_to_use)
                colored_fills[c_idx] = cell.fill  # 更新缓存，供后续列参考
                fixed_total += 1

    if fixed_total:
        log_func(f"  已修复 {fixed_total} 个合计行单元格的填充颜色")


def _find_company_total_row(ws, group_list):
    """
    识别全公司合计行（标黄那一行）：
    - A 列含"合计"，且不在 group_list 的 sum_row 集合中（即不是某个团队的合计行）
    - 如果同时满足"位于所有团队合计行之后"，则优先选最后一行
    返回行号，或 None（找不到）
    """
    team_sum_rows = set()
    for item in group_list:
        if len(item) == 4:
            _, _, _, sr = item
        else:
            _, _, sr = item
        if sr:
            team_sum_rows.add(sr)
    candidates = []
    for r in range(4, ws.max_row + 1):
        a_val = ws.cell(r, 1).value
        if a_val and '合计' in str(a_val) and r not in team_sum_rows:
            candidates.append(r)
    if not candidates:
        return None
    # 取最后一个候选（通常公司总合计行在最末）
    return candidates[-1]


def _scan_header_row2(ws):
    """
    扫描 Row2 表头，正确处理合并单元格（取合并区域首格的 value）。
    返回 {列号: 表头文字} 字典。
    """
    from openpyxl.cell import MergedCell as _MC
    # 先建一个合并首格映射：非 MergedCell 但在合并范围内的首格
    result = {}
    for cell in ws[2]:
        if isinstance(cell, _MC):
            continue
        if cell.value:
            result[cell.column] = str(cell.value).replace('\n', '').strip()
    return result


def _update_company_total_row(ws, group_list, ytd_col, log_func=print):
    """
    更新全公司合计行（标黄行）的所有关键字段：

    1. 数值列 = SUM(各团队合计行)
       包括：传统/保险/管理费月份列、各月折标列、YTD折标列、G列、F列、
             财富中心上半年任务列
    2. 考核期完成率 = G公司行 / F公司行
    3. 考核期差距   = F公司行 - G公司行
    4. 月度完成率   = 最新月折标公司行 / (F公司行 / 考核期月数)
    5. 财富中心完成率 = YTD公司行 / FC任务公司行
    6. 任务差距     = F公司行 - YTD公司行
       （= 各团队考核任务目标总和 - 各团队YTD总和）
    7. 公司目标右侧三格：
       - 第1格（FC任务）：保留原有固定数字，不修改
       - 第2格（完成率）：= YTD公司行 / 第1格固定数字
       - 第3格（差距）  ：= 第1格固定数字 - YTD公司行
    """
    if not group_list:
        return

    company_row = _find_company_total_row(ws, group_list)
    if company_row is None:
        return

    from openpyxl.cell import MergedCell as _MC

    sum_rows = []
    for item in group_list:
        if len(item) == 4:
            _, _, _, sr = item
        else:
            _, _, sr = item
        if sr:
            sum_rows.append(sr)

    def _write(row, col, formula, number_format=None):
        c = ws.cell(row, col)
        if isinstance(c, _MC):
            return
        c.value = formula
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        if number_format:
            c.number_format = number_format

    # ── 扫描表头（正确处理合并单元格）──
    header_map = _scan_header_row2(ws)   # {col: text}

    numeric_sum_cols = []
    fc_task_col    = None
    fc_rate_col    = None
    fc_gap_col     = None
    kaohe_gap_col  = None
    kaohe_rate_col = None
    f_col_target   = None
    g_col_kaohe    = None
    completion_col = None
    d_col = e_col  = None
    first_data_row = group_list[0][0]

    for col, v in header_map.items():
        is_numeric = (
            re.search(r'^(传统|保险|管理费)\d{6}$', v) or
            re.search(r'^\d{4}年\d+月折标$', v) or
            ('YTD折标' in v) or
            v == '考核期间已完成折标' or
            v == '考核任务目标（按实际考核月数折算）' or
            ('财富中心' in v and '上半年' in v and '任务' in v and '完成率' not in v)
        )
        if is_numeric:
            numeric_sum_cols.append(col)

        if v == '考核期起始日':
            d_col = col
        elif v == '考核期终止日':
            e_col = col
        if v == '考核任务目标（按实际考核月数折算）':
            f_col_target = col
        if v == '考核期间已完成折标':
            g_col_kaohe = col
        if '月度' in v and '完成率' in v:
            completion_col = col
        if '考核期完成率' in v or ('考核期' in v and '完成率' in v and '累计' in v):
            kaohe_rate_col = col
        if '考核期差距' in v:
            kaohe_gap_col = col
        # 完成率/任务扫描须排除「下半年」及「差距」列（新表头均为长名，子串会互撞）
        if '财富中心' in v and '上半年' in v and '完成率' in v:
            fc_rate_col = col
        # 精确匹配上半年模块差距列（兼容新旧表头），排除下半年模块/三档差距/考核期镜像列
        if v in ('任务差距', '上半年任务差距', '财富中心上半年任务差距'):
            fc_gap_col = col
        if ('财富中心' in v and '上半年' in v and '任务' in v
                and '完成率' not in v and '差距' not in v):
            fc_task_col = col

    # 1. 所有数值列（含 fc_task_col）：SUM(各团队合计行)
    for col in numeric_sum_cols:
        lc = get_column_letter(col)
        refs = '+'.join([f'{lc}{sr}' for sr in sum_rows])
        _write(company_row, col, f'={refs}')

    # 2. 考核期完成率 = G公司行 / F公司行
    if kaohe_rate_col and f_col_target and g_col_kaohe:
        lf = get_column_letter(f_col_target)
        lg = get_column_letter(g_col_kaohe)
        _write(company_row, kaohe_rate_col,
               f'={lg}{company_row}/{lf}{company_row}', '0.00%')

    # 3. 考核期差距 = F公司行 - G公司行
    if kaohe_gap_col and f_col_target and g_col_kaohe:
        lf = get_column_letter(f_col_target)
        lg = get_column_letter(g_col_kaohe)
        _write(company_row, kaohe_gap_col,
               f'={lf}{company_row}-{lg}{company_row}')

    # 4. 月度完成率 = 最新月折标公司行 / (F公司行 / 考核期月数)
    if completion_col and f_col_target and d_col and e_col:
        latest_zb = _find_latest_month_zb_col(ws)
        if latest_zb:
            lm  = get_column_letter(latest_zb)
            lf  = get_column_letter(f_col_target)
            ld  = get_column_letter(d_col)
            le  = get_column_letter(e_col)
            d_ref = f'{ld}{first_data_row}'
            e_ref = f'{le}{first_data_row}'
            months_f = (f'(YEAR({e_ref})*12+MONTH({e_ref}))'
                        f'-(YEAR({d_ref})*12+MONTH({d_ref}))+1')
            _write(company_row, completion_col,
                   f'={lm}{company_row}/({lf}{company_row}/({months_f}))', '0.00%')

    # 5. 财富中心完成率 = 上半年已完成折标公司行 / FC任务公司行
    #    （分子 = YTD - 下半年折标；上半年周期无下半年折标列时即 YTD）
    if fc_rate_col and ytd_col and fc_task_col:
        lfctask = get_column_letter(fc_task_col)
        h1_done = _h1_done_expr(ws, ytd_col, company_row)
        _write(company_row, fc_rate_col,
               f'={h1_done}/{lfctask}{company_row}', '0.00%')

    # 6. 任务差距 = FC任务公司行 - 上半年已完成折标公司行
    if fc_gap_col and ytd_col and fc_task_col:
        lfctask = get_column_letter(fc_task_col)
        h1_done = _h1_done_expr(ws, ytd_col, company_row)
        _write(company_row, fc_gap_col,
               f'={lfctask}{company_row}-{h1_done}')

    log_func(f"  已更新公司合计行（Row {company_row}）各字段汇总公式")

    # ── 7. 处理"公司目标"行右侧三格 ──
    # 识别规则：在主表区域之外（行号 < 公司合计行），A 列或某列含"公司目标"文字
    # 右侧第1格 = 固定FC任务数字（不修改），第2格 = YTD/第1格，第3格 = 第1格 - YTD
    _update_company_goal_cells(ws, ytd_col, company_row, log_func)


def _update_company_goal_cells(ws, ytd_col, company_row, log_func=print):
    """
    识别 sheet 中"公司目标"所在单元格，更新其右侧紧邻的3格：
      - 右+0（"公司目标"右边第1格）：财富中心上半年FC任务固定数字，保留不动
      - 右+1（第2格）：完成率 = 公司YTD折标 / 第1格固定数字
      - 右+2（第3格）：任务差距 = 第1格固定数字 - 公司YTD折标

    识别规则：扫描全表所有行，找到某格的 value 含"公司目标"字样，
    且该格不是 MergedCell（或者是合并首格）。
    """
    if ytd_col is None:
        return

    from openpyxl.cell import MergedCell as _MC

    # 上半年已完成折标 = YTD - 下半年折标（下半年周期）；上半年周期即 YTD
    h1_done = _h1_done_expr(ws, ytd_col, company_row)

    # 定位 H1 模块三列（目标行公式必须落在模块列上，不能用「标签格+1..3」的
    # 相对偏移——用户手工调整列顺序后标签与数值会脱钩，污染其他列）
    fc_fixed_col = rate_col = gap_col = None
    for cell in ws[2]:
        if not cell.value:
            continue
        v2 = str(cell.value).replace('\n', '').strip()
        if ('财富中心' in v2 and '上半年' in v2 and '任务' in v2
                and '完成率' not in v2 and '差距' not in v2):
            fc_fixed_col = cell.column
        elif '财富中心' in v2 and '上半年' in v2 and '完成率' in v2:
            rate_col = cell.column
        elif v2 in ('任务差距', '上半年任务差距', '财富中心上半年任务差距'):
            gap_col = cell.column
    if not (fc_fixed_col and rate_col and gap_col):
        log_func("  公司目标行：未定位到 H1 模块三列，跳过")
        return

    goal_found = False
    for r in range(1, ws.max_row + 1):
        for c_idx in range(1, ws.max_column + 1):
            cell = ws.cell(r, c_idx)
            if isinstance(cell, _MC):
                continue
            v = cell.value
            if v and '公司目标' in str(v):
                # 找到"公司目标"标签所在行；数值格由 H1 模块列号决定（保留手填不动）

                lc_fixed = get_column_letter(fc_fixed_col)
                lc_rate  = get_column_letter(rate_col)
                lc_gap   = get_column_letter(gap_col)

                data_row = r   # 公司目标和数值在同一行

                # 第2格：完成率 = 上半年已完成折标 / 固定数字
                c_rate = ws.cell(data_row, rate_col)
                if not isinstance(c_rate, _MC):
                    c_rate.value = (
                        f'=IF({lc_fixed}{data_row}<>0,'
                        f'{h1_done}/{lc_fixed}{data_row},"")'
                    )
                    c_rate.number_format = '0.00%'
                    c_rate.alignment = Alignment(
                        horizontal='center', vertical='center', wrap_text=True)

                # 第3格：任务差距 = 固定数字 - 上半年已完成折标
                c_gap = ws.cell(data_row, gap_col)
                if not isinstance(c_gap, _MC):
                    c_gap.value = f'={lc_fixed}{data_row}-{h1_done}'
                    c_gap.alignment = Alignment(
                        horizontal='center', vertical='center', wrap_text=True)

                log_func(
                    f"  已更新公司目标行（Row {data_row}）："
                    f"完成率={h1_done}/{lc_fixed}{data_row}（分子=上半年折标），"
                    f"差距={lc_fixed}{data_row}-{h1_done}"
                )
                goal_found = True
                break   # 只处理第一个找到的公司目标格
        if goal_found:
            break

    if not goal_found:
        log_func(f"  未找到含'公司目标'的单元格，跳过公司目标行更新")


def _find_h2_zb_col(ws):
    """
    定位「2026下半年折标」列（YTD 旁边的主数据区列）。
    返回列号；不存在（上半年周期）返回 None。
    匹配规则：表头含「下半年」+「折标」，且不含「任务」/「已完成」（排除下半年模块列）。
    """
    header_map = _scan_header_row2(ws)
    for col, v in header_map.items():
        if ('下半年' in v and '折标' in v
                and '任务' not in v and '已完成' not in v):
            return col
    return None


def _find_h1_zb_col(ws):
    """
    定位「{year}上半年折标」列（主数据区）。
    返回列号；不存在（上半年周期/未生成）返回 None。
    """
    header_map = _scan_header_row2(ws)
    for col, v in header_map.items():
        if ('上半年' in v and '折标' in v
                and '任务' not in v and '完成' not in v and '进度' not in v):
            return col
    return None


def _h1_done_expr(ws, ytd_col, row):
    """
    生成「上半年已完成折标」的表达式（优先级从高到低）：
    1. 主数据区「{year}上半年折标」列存在 → 直接引用该列
    2. 「{year}下半年折标」列存在 → (YTD行 - 下半年折标行)
    3. 都不存在（上半年周期）→ YTD（此时 YTD 即等于上半年折标）
    返回不含 '=' 的表达式字符串。
    """
    h1 = _find_h1_zb_col(ws)
    if h1:
        return f'{get_column_letter(h1)}{row}'
    lytd = get_column_letter(ytd_col)
    h2 = _find_h2_zb_col(ws)
    if h2 and h2 != ytd_col:
        lh2 = get_column_letter(h2)
        return f'({lytd}{row}-{lh2}{row})'
    return f'{lytd}{row}'


def _update_fc_cols(ws, group_list, ytd_col, log_func=print):
    """
    更新财富中心相关列（考核期差距、考核期完成率、任务差距）的公式。
    group_list : [(first_data_row, last_data_row, sum_row), ...]
    ytd_col    : YTD折标列列号（可为None）

    逻辑（数据行每人独立，不合并单元格；合计行也独立写公式）：
      - 考核期差距（每理财师行） = F行 - G行
      - 考核期差距（合计行）     = F合计 - G合计
      - 考核期完成率（每理财师行）= G行 / F行
      - 考核期完成率（合计行）    = G合计 / F合计
      - 任务差距（每理财师行）    = F行 - YTD行   (若有YTD列)
      - 任务差距（合计行）        = F合计 - YTD合计

    注：财富中心上半年任务列保持原有按团队合并居中（因是任务分配数据，非个人指标）。
    """
    if not group_list:
        return
    # 标准化 group_list 为 4 元组 (leader, fr, lr, sr)
    group_list = [_norm_gi(item) for item in group_list if item]
    from openpyxl.cell import MergedCell as _MC

    # ── 扫描各列列号 ──
    fc_task_col   = None   # 财富中心上半年任务（保持合并）
    fc_rate_col   = None   # 财富中心上半年任务完成率（取消合并，逐行）
    fc_gap_col    = None   # 任务差距（取消合并，逐行）
    kaohe_gap_col = None   # 考核期差距（取消合并，逐行）
    kaohe_rate_col= None   # 考核期完成率（累计）（取消合并，逐行）
    f_col_target  = None   # 考核任务目标（按实际考核月数折算）
    g_col_kaohe   = None   # 考核期间已完成折标

    for cell in ws[2]:
        if not cell.value:
            continue
        v = str(cell.value).replace('\n', '').strip()
        # 任务/完成率扫描须排除「下半年」及「差距」列（新表头均为长名，子串会互撞）
        if ('财富中心' in v and '上半年' in v and '任务' in v
                and '完成率' not in v and '差距' not in v):
            fc_task_col = cell.column
        if '财富中心' in v and '上半年' in v and '完成率' in v:
            fc_rate_col = cell.column
        # 精确匹配上半年模块差距列（兼容新旧表头），排除下半年模块/三档差距/考核期镜像列
        if v in ('任务差距', '上半年任务差距', '财富中心上半年任务差距'):
            fc_gap_col = cell.column
        if '考核期差距' in v:
            kaohe_gap_col = cell.column
        if '考核期完成率' in v or ('考核期' in v and '完成率' in v and '累计' in v):
            kaohe_rate_col = cell.column
        if '考核任务目标（按实际考核月数折算）' in v:
            f_col_target = cell.column
        if '考核期间已完成折标' in v:
            g_col_kaohe = cell.column

    def _unmerge_col_range(col, row_start, row_end):
        """移除某列在 row_start~row_end 范围内的所有合并，让每行独立"""
        to_remove = []
        for mr in ws.merged_cells.ranges:
            if (mr.min_col <= col <= mr.max_col and
                    not (mr.max_row < row_start or mr.min_row > row_end)):
                to_remove.append(mr)
        for mr in to_remove:
            ws.merged_cells.remove(mr)

    def _write_cell_formula(row, col, formula, number_format=None):
        """向指定单元格写入公式（跳过 MergedCell 从属格）"""
        c = ws.cell(row, col)
        if isinstance(c, _MC):
            return
        c.value = formula
        c.alignment = Alignment(
            horizontal='center', vertical='center',
            wrap_text=getattr(c.alignment, 'wrap_text', True)
        )
        if number_format:
            c.number_format = number_format

    updated = False

    if f_col_target and g_col_kaohe:
        lc_f = get_column_letter(f_col_target)
        lc_g = get_column_letter(g_col_kaohe)

        for (_, fr, lr, sr) in group_list:
            # 先取消该列在数据行范围内的合并
            if kaohe_gap_col:
                _unmerge_col_range(kaohe_gap_col, fr, sr)
            if kaohe_rate_col:
                _unmerge_col_range(kaohe_rate_col, fr, sr)

            # 数据行：每人各自写公式
            for row_idx in range(fr, lr + 1):
                if kaohe_gap_col:
                    _write_cell_formula(row_idx, kaohe_gap_col,
                                        f'={lc_f}{row_idx}-{lc_g}{row_idx}')
                if kaohe_rate_col:
                    _write_cell_formula(row_idx, kaohe_rate_col,
                                        f'={lc_g}{row_idx}/{lc_f}{row_idx}',
                                        number_format='0.00%')

            # 合计行
            if kaohe_gap_col:
                _write_cell_formula(sr, kaohe_gap_col,
                                    f'={lc_f}{sr}-{lc_g}{sr}')
            if kaohe_rate_col:
                _write_cell_formula(sr, kaohe_rate_col,
                                    f'={lc_g}{sr}/{lc_f}{sr}',
                                    number_format='0.00%')

            # 合计行
            if kaohe_gap_col:
                _write_cell_formula(sr, kaohe_gap_col,
                                    f'={lc_f}{sr}-{lc_g}{sr}')
            if kaohe_rate_col:
                _write_cell_formula(sr, kaohe_rate_col,
                                    f'={lc_g}{sr}/{lc_f}{sr}',
                                    number_format='0.00%')

        log_msgs = []
        if kaohe_gap_col:
            log_msgs.append('考核期差距')
        if kaohe_rate_col:
            log_msgs.append('考核期完成率')
        if log_msgs:
            log_func(f"  已更新 {'/'.join(log_msgs)} 列（逐行公式，无合并）")
        updated = True

    if fc_rate_col and ytd_col and fc_task_col:
        lc_ytd    = get_column_letter(ytd_col)
        lc_fctask = get_column_letter(fc_task_col)
        lc_fcrate = get_column_letter(fc_rate_col)
        lc_fcgap  = get_column_letter(fc_gap_col) if fc_gap_col else None

        for (_, fr, lr, sr) in group_list:
            # 1. 取消旧合并（数据行 + 合计行范围内）
            _unmerge_col_range(fc_rate_col, fr, sr)
            if fc_gap_col:
                _unmerge_col_range(fc_gap_col, fr, sr)

            # 2. 合计行写实际公式
            # 分母：FC任务值在 fr 行（合并单元格首行），不在合计行 sr
            # 分子：上半年已完成折标 = YTD - 下半年折标（下半年周期）或 YTD（上半年周期）
            h1_done = _h1_done_expr(ws, ytd_col, sr)
            c_rate = ws.cell(sr, fc_rate_col)
            if not isinstance(c_rate, _MC):
                c_rate.value = f'={h1_done}/{lc_fctask}{fr}'
                c_rate.number_format = '0.00%'
                c_rate.alignment = Alignment(horizontal='center', vertical='center',
                                             wrap_text=True)
            if fc_gap_col:
                c_gap = ws.cell(sr, fc_gap_col)
                if not isinstance(c_gap, _MC):
                    c_gap.value = f'={lc_fctask}{fr}-{h1_done}'
                    c_gap.alignment = Alignment(horizontal='center', vertical='center',
                                                wrap_text=True)

            # 3. 数据行（fr~lr）合并成一格，引用合计行的值（=合计行单元格地址）
            if fr < lr:
                ws.merge_cells(f'{lc_fcrate}{fr}:{lc_fcrate}{lr}')
                if fc_gap_col:
                    ws.merge_cells(f'{lc_fcgap}{fr}:{lc_fcgap}{lr}')
            # 数据行首格写引用公式
            c_dr = ws.cell(fr, fc_rate_col)
            if not isinstance(c_dr, _MC):
                c_dr.value = f'={lc_fcrate}{sr}'
                c_dr.number_format = '0.00%'
                c_dr.alignment = Alignment(horizontal='center', vertical='center',
                                           wrap_text=True)
            if fc_gap_col:
                c_dg = ws.cell(fr, fc_gap_col)
                if not isinstance(c_dg, _MC):
                    c_dg.value = f'={lc_fcgap}{sr}'
                    c_dg.alignment = Alignment(horizontal='center', vertical='center',
                                               wrap_text=True)
            c_dr.alignment = Alignment(horizontal='center', vertical='center',
                                       wrap_text=True)
        if fc_gap_col:
            c_dg = ws.cell(fr, fc_gap_col)
            if not isinstance(c_dg, _MC):
                c_dg.value = f'={lc_fcgap}{sr}'
                c_dg.alignment = Alignment(horizontal='center', vertical='center',
                                           wrap_text=True)

        log_func(f"  已更新财富中心完成率/任务差距列（仅合计行计算，数据行合并引用）")
        updated = True

    # ── 财富中心上半年任务列：保持按团队合并居中（任务分配数据，非个人指标）──
    # 团队合计行写 SUM(数据行)，聚合该团队所有理财师的 FC任务
    if fc_task_col:
        lc_task = get_column_letter(fc_task_col)
        for (_, fr, lr, sr) in group_list:
            # 取消旧合并（覆盖数据行+合计行范围）
            to_remove = []
            for mr in ws.merged_cells.ranges:
                if (mr.min_col <= fc_task_col <= mr.max_col and
                        not (mr.max_row < fr or mr.min_row > sr)):
                    to_remove.append(mr)
            for mr in to_remove:
                ws.merged_cells.remove(mr)

            # 数据行：合并居中（保持原样），格式重置为普通数字
            if fr < lr:
                ws.merge_cells(f'{lc_task}{fr}:{lc_task}{lr}')
            c = ws.cell(fr, fc_task_col)
            c.number_format = '#,##0.00'
            c.alignment = Alignment(
                horizontal='center', vertical='center',
                wrap_text=getattr(c.alignment, 'wrap_text', True)
            )

            # 合计行：写 SUM(数据行) 汇总该团队 FC任务，格式强制为普通数字
            c_sum = ws.cell(sr, fc_task_col)
            if not isinstance(c_sum, _MC):
                c_sum.value = f'={lc_task}{fr}'   # 数据行合并后首格即团队任务值
                c_sum.number_format = '#,##0.00'  # 普通数字，带两位小数（与其他数值列统一）
                c_sum.alignment = Alignment(
                    horizontal='center', vertical='center', wrap_text=True)

    if not updated:
        pass  # 该 sheet 不含相关列，正常跳过


def _calc_kaohe_months(start_dt, end_dt):
    """
    计算考核期实际月数：(终止年*12+终止月) - (起始年*12+起始月) + 1
    例：2026-01-01 ~ 2026-06-30 → 6，2026-02-01 ~ 2026-06-30 → 5
    支持 datetime / date 对象，也支持 None（返回 None）
    """
    if start_dt is None or end_dt is None:
        return None
    try:
        sy, sm = start_dt.year, start_dt.month
        ey, em = end_dt.year, end_dt.month
        return (ey * 12 + em) - (sy * 12 + sm) + 1
    except AttributeError:
        return None


def _find_latest_month_zb_col(ws):
    """
    扫描 Row2，找到最新（列号最小，因为新月份插在左边）的月折标列
    （形如 '2026年3月折标'）。
    正确处理合并单元格（跳过 MergedCell 从属格），且排除 YTD 列。
    返回列号，或 None（若找不到）。
    """
    from openpyxl.cell import MergedCell as _MC
    latest_col = None
    for cell in ws[2]:
        if isinstance(cell, _MC):
            continue
        if cell.value and isinstance(cell.value, str):
            v = cell.value.replace('\n', '').strip()
            # 只匹配 "YYYY年N月折标"，排除 YTD（含字母）
            if re.search(r'^20\d{2}年\d+月折标$', v):
                if latest_col is None or cell.column < latest_col:
                    latest_col = cell.column
    return latest_col


def _update_kaohe_def_cols(ws, data_rows, all_data, col_month_zb, log_func=print,
                           group_list=None):
    """
    更新目标 sheet 的 D（考核期起始日）、E（考核期终止日）、F（考核任务目标）列，
    以及月度完成率 = 最新月折标 / (任务目标 / 考核期月数)。
    data_rows   : [(row_idx, name), ...]
    all_data    : {name: {__考核起始日__: dt, __考核终止日__: dt, __考核任务目标__: v}}
    col_month_zb: 传入的当月折标列号（仅作备用，优先动态找最新月折标列）
    group_list  : [(fr, lr, sr), ...] 用于更新合计行月度完成率（可为 None）
    """
    from openpyxl.cell import MergedCell
    # 标准化 group_list 为 4 元组 (leader, fr, lr, sr)
    if group_list:
        group_list = [_norm_gi(item) for item in group_list]

    # ── 确定月度完成率分子列（当月折标）──
    # 优先使用调用方传入的 col_month_zb（已知的当月折标列）；
    # 若未传入（None），再动态扫描 Row2 找最新月折标列作为兜底。
    if col_month_zb is not None:
        latest_zb_col = col_month_zb
    else:
        latest_zb_col = _find_latest_month_zb_col(ws)

    # 找 D/E/F 列（考核期起始日 / 终止日 / 任务目标）
    d_col = e_col = f_col = None
    completion_col = None
    for cell in ws[2]:
        if not cell.value:
            continue
        v = str(cell.value).replace('\n', '').strip()
        if v == '考核期起始日':
            d_col = cell.column
        elif v == '考核期终止日':
            e_col = cell.column
        elif v == '考核任务目标（按实际考核月数折算）':
            f_col = cell.column
        elif '月度' in v and '完成率' in v:
            completion_col = cell.column

    # ── 取消月度完成率列的所有合并（改为每行独立）──
    if completion_col:
        to_remove = [mr for mr in ws.merged_cells.ranges
                     if mr.min_col <= completion_col <= mr.max_col]
        for mr in to_remove:
            ws.merged_cells.remove(mr)

    updated_def = 0
    for row_idx, name in data_rows:
        if name not in all_data:
            continue
        person = all_data[name]
        start_dt = person.get('__考核起始日__')
        end_dt   = person.get('__考核终止日__')
        target   = person.get('__考核任务目标__')

        # D/E/F 列：只写非合并单元格（或合并首行）
        for col, val in [(d_col, start_dt), (e_col, end_dt), (f_col, target)]:
            if not col or val is None:
                continue
            c = ws.cell(row_idx, col)
            if isinstance(c, MergedCell):
                continue  # 非首行合并格跳过
            c.value = val
            # 日期列设置 YYYY/MM/DD 格式，不显示时间
            if col in (d_col, e_col):
                c.number_format = 'yyyy/mm/dd'
        updated_def += 1

    if updated_def:
        log_func(f"  已更新 D/E/F 列（考核起止日+任务目标）共 {updated_def} 行")

    # ── 合计行 F 列 = SUM(数据行) ──
    # 考核任务目标是每人独立的，合计行应汇总所有理财师
    if f_col and group_list:
        lf_sum = get_column_letter(f_col)
        for (_, fr, lr, sr) in group_list:
            c_sum = ws.cell(sr, f_col)
            if not isinstance(c_sum, MergedCell):
                c_sum.value = f'=SUM({lf_sum}{fr}:{lf_sum}{lr})'
                c_sum.number_format = '#,##0.00'
        log_func(f"  已更新 {len(group_list)} 个团队合计行的考核任务目标 SUM")

    # ── 更新月度完成率公式（每理财师行独立写，分子=最新月折标列）──
    if completion_col and f_col and d_col and e_col:
        lm = get_column_letter(latest_zb_col)   # 最新月折标列字母
        lf = get_column_letter(f_col)
        ld = get_column_letter(d_col)
        le = get_column_letter(e_col)

        # 从 group_list 提取所有数据行的行号（不仅仅是 data_rows 中的）
        all_data_rows = set()
        for row_idx, _ in data_rows:
            all_data_rows.add(row_idx)
        if group_list:
            for (_, fr, lr, sr) in group_list:
                for r in range(fr, lr + 1):
                    all_data_rows.add(r)

        log_func(f"  [DEBUG2] _update_kaohe_def_cols: completion_col={completion_col}, all_data_rows={sorted(all_data_rows)[:20]}...")

        # 每行写公式（包括不在 data_rows 中的行，如 B 列为空但 A 列有合并的理财师）
        written_rows = []
        for row_idx in sorted(all_data_rows):
            f_val = ws.cell(row_idx, f_col).value
            d_val = ws.cell(row_idx, d_col).value
            e_val = ws.cell(row_idx, e_col).value
            if f_val and d_val and e_val:
                months_formula = (
                    f'(YEAR({le}{row_idx})*12+MONTH({le}{row_idx}))'
                    f'-(YEAR({ld}{row_idx})*12+MONTH({ld}{row_idx}))+1'
                )
                c = ws.cell(row_idx, completion_col)
                if not isinstance(c, MergedCell):
                    # 添加 IF 保护避免除0
                    c.value = (
                        f'=IF({lf}{row_idx}=0,"",{lm}{row_idx}/({lf}{row_idx}/({months_formula})))'
                    )
                    c.number_format = '0.00%'
                    written_rows.append(row_idx)
        log_func(f"  [DEBUG2] 写入月度完成率公式到: {written_rows}")

        # 合计行月度完成率：= 合计行最新月折标 / (合计行任务目标 / 考核期月数)
        # 考核期月数取该团队首个理财师行的 D/E 列（合计行 D/E 通常为空）
        if group_list:
            for (_, fr, lr, sr) in group_list:
                d_ref = f'{ld}{fr}'
                e_ref = f'{le}{fr}'
                months_f = (
                    f'(YEAR({e_ref})*12+MONTH({e_ref}))'
                    f'-(YEAR({d_ref})*12+MONTH({d_ref}))+1'
                )
                c = ws.cell(sr, completion_col)
                if not isinstance(c, MergedCell):
                    c.value = f'=IF({lf}{sr}=0,"",{lm}{sr}/({lf}{sr}/({months_f})))'
                    c.number_format = '0.00%'
                    c.alignment = Alignment(horizontal='center', vertical='center',
                                            wrap_text=True)

        log_func(f"  已更新月度完成率公式（分子=最新月折标列 {get_column_letter(latest_zb_col)}，每行独立，无合并）")


# ─────────────────────── 人员变动同步 & 标题修复 ───────────────────────

def _scan_group_structure(ws):
    """
    扫描sheet的团队分组结构
    返回: group_list = [(leader_name, first_data_row, last_data_row, sum_row), ...]

    规则：
    - 合计行（A列含"合计"）作为分组的结束标志
    - A 列有非空值且不同于当前 leader 时，收尾当前组并开始新组
      （用于正确识别「其他团队」等特殊分组）
    """
    group_list = []
    cur_leader = None
    cur_start = None
    cur_end   = None
    for row_idx in range(4, ws.max_row + 1):
        a_val = ws.cell(row_idx, 1).value
        b_val = ws.cell(row_idx, 2).value
        is_sum = a_val and '合计' in str(a_val)
        has_name = b_val and isinstance(b_val, str) and b_val.strip() and not is_sum

        if is_sum:
            if cur_start is not None:
                group_list.append((cur_leader, cur_start, cur_end, row_idx))
            cur_leader = None
            cur_start = None
            cur_end   = None
        elif has_name:
            # 如果 A 列有新的团队名（不同于当前 leader），收尾当前组并开始新组
            new_leader = str(a_val).strip() if a_val else None
            if (cur_start is not None and new_leader is not None
                    and new_leader != cur_leader):
                group_list.append((cur_leader, cur_start, cur_end, None))
                cur_leader = None
                cur_start = None
                cur_end   = None
            if cur_start is None:
                cur_leader = new_leader
                cur_start = row_idx
            cur_end = row_idx

    return group_list


def _find_header_col(ws, keyword, row=2):
    """在指定行扫描含 keyword 的列号，返回第一个匹配列号或 None"""
    for cell in ws[row]:
        if cell.value and keyword in str(cell.value):
            return cell.column
    return None


def _clean_duplicate_sum_rows(ws, log_func=print):
    """
    清理多余的连续「合计」行。
    扫描sheet，当发现连续多个「合计」行时，只保留第一个，删除其余的。
    但公司总合计行（F列公式为 =Fx+Fy+... 引用多个不连续行）不会被误删。

    多余的合计行通常是历史遗留bug（旧版工具多次创建其他团队导致的）。
    """
    from openpyxl.cell import MergedCell as _MC_c

    def _is_company_total_row(r):
        """判断是否为公司总合计行（公式引用了多个不连续的行）"""
        f_val = ws.cell(r, 6).value
        if f_val and isinstance(f_val, str) and f_val.startswith('='):
            # 公司总合计行的公式通常是 =F10+F16+F22+... 格式（多个+号连接）
            # 分组合计行的公式通常是 =SUM(Fx:Fy) 格式
            return '+' in f_val and 'SUM' not in f_val.upper()
        return False

    rows_to_delete = []
    prev_was_sum = False
    for r in range(4, ws.max_row + 1):
        a_val = ws.cell(r, 1).value
        if a_val and '合计' in str(a_val):
            if prev_was_sum and not _is_company_total_row(r):
                # 连续第二个及以后的合计行（非公司总合计行），标记为待删除
                rows_to_delete.append(r)
                log_func(f"  清理多余的合计行: 行{r}")
            else:
                prev_was_sum = True
        else:
            # 检查是否有有效数据（B列有名字或A列有团队名）
            b_val = ws.cell(r, 2).value
            has_data = False
            if b_val and isinstance(b_val, str) and b_val.strip():
                has_data = True
            elif a_val and '合计' not in str(a_val):
                has_data = True
            if has_data:
                prev_was_sum = False
            # 空行不重置prev_was_sum，避免误判

    # 从下往上删除，避免行号偏移
    for r in reversed(rows_to_delete):
        # 先解除合并
        to_rm = [mr for mr in list(ws.merged_cells.ranges)
                 if mr.min_row <= r <= mr.max_row]
        for mr in to_rm:
            ws.merged_cells.remove(mr)
        ws.delete_rows(r, 1)

    if rows_to_delete:
        log_func(f"  共清理 {len(rows_to_delete)} 个多余的合计行")


def _rebuild_team_a_merges(ws, new_data_row, sum_row, team_leader_name=None, log_func=None):
    """
    在 new_data_row（新插入的数据行）被插入到 sum_row（合计行）之前后，
    重建该团队的 A 列合并结构：
      - 团队 A 列合并（A列从团队首行到最后一行合并显示团队名）
      - 合计行 A:E 合并（显示"合计"）

    new_data_row: 新插入行的行号
    sum_row:      合计行的行号（insert_rows 后已下移 1 行，= insert_before + 1）
    team_leader_name: 团队长/团队名（若为 None 则不设 A 列值）
    """
    # ── 0. 辅助：获取 A 列实际值（处理 MergedCell 情况）──
    from openpyxl.cell import MergedCell as _MC_r

    def _get_a_value(row):
        """获取指定行 A 列的实际值（MergedCell 时取合并首格值）"""
        c = ws.cell(row, 1)
        if not isinstance(c, _MC_r):
            return c.value
        # A 列是 MergedCell 从属格，找它所属的合并首格
        for mr in ws.merged_cells.ranges:
            if mr.min_col <= 1 <= mr.max_col and mr.min_row <= row <= mr.max_row:
                return ws.cell(mr.min_row, mr.min_col).value
        return None

    # ── 1. 确定团队数据行范围（fr~lr，不含合计行）──

    # 向上扫描找团队首行
    team_fr = new_data_row
    for r in range(new_data_row - 1, 3, -1):
        a_cell = ws.cell(r, 1)
        b_cell = ws.cell(r, 2)
        is_merged_a = isinstance(a_cell, _MC_r)
        a_val = _get_a_value(r)  # 始终获取实际值（含 MergedCell 的首格值）
        b_val = None if isinstance(b_cell, _MC_r) else b_cell.value

        # 合计行：停止（上一个合计行，不跨组）
        if a_val and '合计' in str(a_val):
            break

        # 有 B 列名字：可能是团队成员
        if b_val and isinstance(b_val, str) and b_val.strip():
            # 检查 A 列是否属于不同团队（通过 _get_a_value 获取实际值）
            if a_val and team_leader_name:
                if str(a_val).strip() != str(team_leader_name).strip():
                    break  # 不同团队，停止
            team_fr = r
        # A 列有团队名（B 列为空的情况，如团队首行）
        elif a_val and isinstance(a_val, str) and str(a_val).strip():
            if team_leader_name and str(a_val).strip() != str(team_leader_name).strip():
                break  # 不同团队，停止
            team_fr = r
        else:
            break  # 空行停止

    # 向下扫描找团队末行（不含合计行）
    team_lr = new_data_row
    for r in range(new_data_row + 1, sum_row):
        a_cell = ws.cell(r, 1)
        b_cell = ws.cell(r, 2)
        a_val = _get_a_value(r)  # 始终获取实际值（含 MergedCell 的首格值）
        b_val = None if isinstance(b_cell, _MC_r) else b_cell.value
        if a_val and '合计' in str(a_val):
            break
        # 检查是否属于不同团队
        if a_val and team_leader_name:
            if str(a_val).strip() != str(team_leader_name).strip():
                break
        if b_val and isinstance(b_val, str) and b_val.strip():
            team_lr = r
        else:
            break

    # ── 2. 重建团队 A 列合并 ──
    # 先移除团队数据行范围内 A 列（col 1）的所有旧合并
    to_rm_a = [mr for mr in list(ws.merged_cells.ranges)
               if mr.min_col <= 1 <= mr.max_col and
               not (mr.max_row < team_fr or mr.min_row > team_lr)]
    for mr in to_rm_a:
        ws.merged_cells.remove(mr)

    if team_fr < team_lr:
        ws.merge_cells(start_row=team_fr, start_column=1,
                       end_row=team_lr, end_column=1)
    # 设置 A 列团队名（仅团队首行）
    if team_leader_name:
        _tf_cell = ws.cell(team_fr, 1)
        if isinstance(_tf_cell, _MC_r):
            # MergedCell 从属格，先解除包含该格的 A 列合并
            for mr in list(ws.merged_cells.ranges):
                if mr.min_col <= 1 <= mr.max_col and mr.min_row <= team_fr <= mr.max_row:
                    ws.merged_cells.remove(mr)
                    break
            _tf_cell = ws.cell(team_fr, 1)  # 重新获取
        _tf_cell.value = team_leader_name

    # ── 3. 重建合计行 A:E 合并 ──
    to_rm_sum = [mr for mr in list(ws.merged_cells.ranges)
                 if mr.min_col <= 1 <= mr.max_col and
                 mr.min_col <= 5 <= mr.max_col and
                 mr.min_row == sum_row and mr.max_row == sum_row]
    for mr in to_rm_sum:
        ws.merged_cells.remove(mr)

    # 确保合计行 A 列有"合计"值
    _sum_cell = ws.cell(sum_row, 1)
    if isinstance(_sum_cell, _MC_r):
        # MergedCell 从属格，先解除包含该格的合并再写值
        for mr in list(ws.merged_cells.ranges):
            if mr.min_col <= 1 <= mr.max_col and mr.min_row <= sum_row <= mr.max_row:
                ws.merged_cells.remove(mr)
                break
        _sum_cell = ws.cell(sum_row, 1)  # 重新获取
    if not _sum_cell.value or '合计' not in str(_sum_cell.value):
        _sum_cell.value = '合计'
    _ensure_merge(ws, sum_row, 1, sum_row, 5)


def _insert_rows_with_merge_adjust(ws, insert_at, num_rows=1):
    """
    在 insert_at 行之前插入 num_rows 行，同时手动调整合并区域。
    openpyxl 的 insert_rows 不会调整合并区域，需要先记录再重建。

    返回：无
    """
    if num_rows <= 0:
        return

    # 记录所有合并区域，分为需要下移和不需要下移两类
    merges_above = []  # insert_at 之前的合并，不变
    merges_below = []  # insert_at 及之后的合并，行号 +num_rows
    for mr in list(ws.merged_cells.ranges):
        if mr.min_row >= insert_at:
            merges_below.append((mr.min_row, mr.max_row, mr.min_col, mr.max_col))
        else:
            merges_above.append((mr.min_row, mr.max_row, mr.min_col, mr.max_col))

    # 先删除所有合并
    for mr in list(ws.merged_cells.ranges):
        ws.merged_cells.remove(mr)

    # 插入行
    ws.insert_rows(insert_at, num_rows)

    # 重建不需要移动的合并
    for (r1, r2, c1, c2) in merges_above:
        ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)

    # 重建需要下移的合并（行号+num_rows）
    for (r1, r2, c1, c2) in merges_below:
        ws.merge_cells(start_row=r1 + num_rows, start_column=c1,
                       end_row=r2 + num_rows, end_column=c2)


def sync_advisors_and_rank(ws, source_data, log_func=print, keep_other_team=True):
    """
    全量重建：根据 source_data 中的团队长分组，重新构建所有团队和理财师行。

    每次运行都从数据源重新分组，避免增量更新的复杂性和潜在bug。
    合并单元格和公式的重建由后续的 _rebuild_all_merges_and_formulas 处理。

    规则：
    1. 清除所有数据行合并（保留 Row 1-3）
    2. 找到公司总合计行
    3. 保存右侧（时间进度模块等）内容
    4. 删除所有数据行（第4行 ~ 公司总合计行-1）
    5. 按团队长分组，重新插入所有团队和理财师
    6. 每个团队后插入合计行
    7. 无团队长的理财师：keep_other_team=True 时归入「其他团队」，否则过滤掉
    8. 恢复右侧内容

    keep_other_team: True=保留其他团队，False=过滤掉不展示
    """
    from openpyxl.styles import Alignment, PatternFill
    import copy as _copy

    # ── 0. 清除所有数据行合并（让后续操作不碰到 MergedCell 问题）──
    to_remove = []
    for mr in list(ws.merged_cells.ranges):
        if mr.min_row >= 4:  # 只清除数据行合并，保留 Row 1-3
            to_remove.append(mr)
    for mr in to_remove:
        ws.merged_cells.remove(mr)

    # ── 0.5 保存参考行样式 ──
    # 找一个数据行（非合计行）作为样式参考
    ref_row_idx = None
    for row_idx in range(4, min(20, ws.max_row + 1)):
        a_val = ws.cell(row_idx, 1).value
        b_val = ws.cell(row_idx, 2).value
        if b_val and isinstance(b_val, str) and b_val.strip() and not (a_val and '合计' in str(a_val)):
            ref_row_idx = row_idx
            break

    # ── 0.6 扫描当前 sheet，记录团队顺序（用于保持排序一致）──
    # 在删除数据行之前，按 A 列出现顺序收集团队长列表
    previous_team_order = []
    seen_leaders = set()
    for row_idx in range(4, ws.max_row + 1):
        a_val = ws.cell(row_idx, 1).value
        if a_val and isinstance(a_val, str):
            a_stripped = a_val.strip()
            if a_stripped and a_stripped not in ('合计', '其他团队') and a_stripped not in seen_leaders:
                seen_leaders.add(a_stripped)
                previous_team_order.append(a_stripped)
    log_func(f"  上次团队顺序（{len(previous_team_order)} 个）：{previous_team_order}")

    # ── 0.7 保存右侧（START_COL=25以右）内容（时间进度模块等）──
    START_COL = 25  # Y列以右
    saved_right_content = {}
    max_scan_col = min(ws.max_column + 1, 100)

    # ── 1. 找到公司总合计行 ──
    # 判断依据：A 列值为"合计"，且该行不在任何团队的数据范围内
    company_total_row = None
    group_list_tmp = _scan_group_structure(ws)
    # 收集所有团队数据行范围（含合计行）
    all_team_rows = set()
    for (_ld, fr, lr, sr) in group_list_tmp:
        if fr:
            end = sr if sr else lr
            for r in range(fr, end + 1):
                all_team_rows.add(r)

    for row_idx in range(4, ws.max_row + 1):
        a_val = ws.cell(row_idx, 1).value
        if a_val and str(a_val).strip() == '合计':
            if row_idx not in all_team_rows:
                company_total_row = row_idx
                break

    if company_total_row is None and group_list_tmp:
        # fallback：用最后一个合计行的下一行
        last_sr = group_list_tmp[-1][3]
        if last_sr:
            company_total_row = last_sr + 1

    if company_total_row is None:
        # 新sheet，没找到公司总合计行，追加到末尾
        company_total_row = ws.max_row + 1
        log_func(f"  未找到公司总合计行，将在第 {company_total_row} 行创建")

    log_func(f"  公司总合计行在第 {company_total_row} 行")

    # 保存右侧内容（在删除数据行之前）
    if company_total_row and company_total_row > 4:
        for r in range(4, company_total_row):
            offset = r - 4  # 相对于第4行的偏移
            for c in range(START_COL, max_scan_col):
                cell = ws.cell(r, c)
                if cell.value is not None:
                    saved_right_content[(offset, c)] = {
                        'value': cell.value,
                        'number_format': cell.number_format,
                    }
        if saved_right_content:
            log_func(f"  已保存 {len(saved_right_content)} 个右侧单元格内容")

    # ── 2. 删除所有数据行（第4行 ~ 公司总合计行-1）──
    if company_total_row > 4:
        num_data_rows = company_total_row - 4
        ws.delete_rows(4, num_data_rows)
        log_func(f"  已删除 {num_data_rows} 行数据行（第4行~第{company_total_row-1}行）")

    # ── 3. 按团队长分组 ──
    # 分组：{团队长: [理财师名1, 理财师名2, ...]}
    teams = {}
    no_team = []  # 无团队长的理财师

    for adv_name, adv_data in source_data.items():
        team_leader = adv_data.get('团队长')
        team_leader = str(team_leader).strip() if team_leader else ''
        if team_leader:
            if team_leader not in teams:
                teams[team_leader] = []
            teams[team_leader].append(adv_name)
        else:
            no_team.append(adv_name)

    log_func(f"  共有 {len(teams)} 个团队，{len(no_team)} 位理财师无团队长")

    # ── 4. 重新插入所有团队和理财师 ──
    # 插入位置：第4行（公司总合计行之前）
    insert_pos = 4

    # 团队排序：先按上次顺序排已有团队，新团队按字母序放后面
    ordered_teams = []
    for leader in previous_team_order:
        if leader in teams:
            ordered_teams.append(leader)
    for leader in sorted(teams.keys()):
        if leader not in ordered_teams:
            ordered_teams.append(leader)

    # 先处理有团队长的
    for team_leader in ordered_teams:
        advisors = sorted(teams[team_leader])
        num_advisors = len(advisors)

        # 在 insert_pos 插入 num_advisors + 1 行（+1 是合计行）
        ws.insert_rows(insert_pos, num_advisors + 1)

        # 写入理财师行
        for i, adv_name in enumerate(advisors):
            row_idx = insert_pos + i
            adv_data = source_data[adv_name]

            # 写团队长（A列）
            ws.cell(row_idx, 1).value = team_leader
            # 写理财师名（B列）
            ws.cell(row_idx, 2).value = adv_name
            # 写职级（C列）
            rank_val = adv_data.get('当前考核期职级')
            if rank_val:
                ws.cell(row_idx, 3).value = str(rank_val).strip()
            else:
                position = adv_data.get('岗位')
                if position:
                    ws.cell(row_idx, 3).value = str(position).strip()
            # 写考核期起始日（D列）
            c_d = ws.cell(row_idx, 4)
            c_d.value = adv_data.get('考核期起始日')
            if c_d.value is not None:
                c_d.number_format = 'yyyy/mm/dd'
            # 写考核期终止日（E列）
            c_e = ws.cell(row_idx, 5)
            c_e.value = adv_data.get('考核期终止日')
            if c_e.value is not None:
                c_e.number_format = 'yyyy/mm/dd'
            # 写考核任务目标（F列）
            ws.cell(row_idx, 6).value = adv_data.get('考核任务目标（按实际考核月数折算）') or 0

            # 设置对齐方式为水平居中+垂直居中
            for c in range(1, ws.max_column + 1):
                ws.cell(row_idx, c).alignment = Alignment(horizontal='center', vertical='center')

            # 数据行强制设为白色填充（清除参考行复制过来的颜色）
            for c in range(1, ws.max_column + 1):
                ws.cell(row_idx, c).fill = PatternFill(start_color='FFFFFFFF', end_color='FFFFFFFF', patternType='solid')

        # 写合计行
        sum_row_idx = insert_pos + num_advisors
        ws.cell(sum_row_idx, 1).value = '合计'

        # 复制样式（从参考行复制，包含合计行的原有颜色等样式）
        if ref_row_idx:
            _copy_row_styles_from_neighbor(ws, insert_pos, ref_row_idx)
            # 合计行也复制样式
            _copy_row_styles_from_neighbor(ws, sum_row_idx, ref_row_idx)

        # 为合计行设置浅紫色填充 + 居中对齐
        team_sum_fill = PatternFill(start_color='FFFFCCFF', end_color='FFFFCCFF', patternType='solid')
        for c in range(1, ws.max_column + 1):
            ws.cell(sum_row_idx, c).fill = _copy.copy(team_sum_fill)
            ws.cell(sum_row_idx, c).alignment = Alignment(horizontal='center', vertical='center')

        log_func(f"  已插入团队「{team_leader}」（{num_advisors}人，行{insert_pos}~{sum_row_idx}）")

        # 设置 outline_level（支持行分组折叠）
        for i in range(num_advisors):
            ws.row_dimensions[insert_pos + i].outline_level = 1
        ws.row_dimensions[sum_row_idx].outline_level = 1

        # 更新插入位置
        insert_pos = sum_row_idx + 1

    # 处理无团队长的（其他团队）
    if keep_other_team and no_team:
        # 创建「其他团队」分组
        num_advisors = len(no_team)
        ws.insert_rows(insert_pos, num_advisors + 1)

        # 写入理财师行
        for i, adv_name in enumerate(sorted(no_team)):
            row_idx = insert_pos + i
            adv_data = source_data[adv_name]

            ws.cell(row_idx, 1).value = '其他团队'
            ws.cell(row_idx, 2).value = adv_name

            rank_val = adv_data.get('当前考核期职级')
            if rank_val:
                ws.cell(row_idx, 3).value = str(rank_val).strip()
            else:
                position = adv_data.get('岗位')
                if position:
                    ws.cell(row_idx, 3).value = str(position).strip()

            c_d = ws.cell(row_idx, 4)
            c_d.value = adv_data.get('考核期起始日')
            if c_d.value is not None:
                c_d.number_format = 'yyyy/mm/dd'

            c_e = ws.cell(row_idx, 5)
            c_e.value = adv_data.get('考核期终止日')
            if c_e.value is not None:
                c_e.number_format = 'yyyy/mm/dd'

            ws.cell(row_idx, 6).value = adv_data.get('考核任务目标（按实际考核月数折算）') or 0

            # 设置对齐方式为水平居中+垂直居中
            for c in range(1, ws.max_column + 1):
                ws.cell(row_idx, c).alignment = Alignment(horizontal='center', vertical='center')

            # 数据行强制设为白色填充
            for c in range(1, ws.max_column + 1):
                ws.cell(row_idx, c).fill = PatternFill(start_color='FFFFFFFF', end_color='FFFFFFFF', patternType='solid')

        sum_row_idx = insert_pos + num_advisors
        ws.cell(sum_row_idx, 1).value = '合计'

        # 复制样式（从参考行复制，包含合计行的原有颜色等样式）
        if ref_row_idx:
            _copy_row_styles_from_neighbor(ws, insert_pos, ref_row_idx)
            _copy_row_styles_from_neighbor(ws, sum_row_idx, ref_row_idx)

        # 为合计行设置浅紫色填充 + 居中对齐
        for c in range(1, ws.max_column + 1):
            ws.cell(sum_row_idx, c).fill = _copy.copy(team_sum_fill)
            ws.cell(sum_row_idx, c).alignment = Alignment(horizontal='center', vertical='center')

        log_func(f"  已插入「其他团队」（{num_advisors}人，行{insert_pos}~{sum_row_idx}）")

        insert_pos = sum_row_idx + 1

    # ── 5. 恢复右侧（时间进度模块等）内容 ──
    # 注意：右侧内容与主数据共享行号，所以直接按偏移恢复
    if saved_right_content:
        restored = 0
        for (offset, c), data in saved_right_content.items():
            new_row = 4 + offset
            if new_row >= ws.max_row:
                continue
            cell = ws.cell(new_row, c)
            cell.value = data['value']
            if data['number_format']:
                cell.number_format = data['number_format']
            restored += 1
        log_func(f"  已恢复 {restored} 个右侧单元格内容（时间进度模块等）")

    # ── 6. 返回 existing_rows ──
    updated_rows = {}
    for row_idx in range(4, ws.max_row + 1):
        a_val = ws.cell(row_idx, 1).value
        b_val = ws.cell(row_idx, 2).value
        if a_val and '合计' in str(a_val):
            continue
        if b_val and isinstance(b_val, str) and b_val.strip():
            updated_rows[b_val.strip()] = row_idx

    log_func(f"  全量重建完成，共 {len(updated_rows)} 位理财师")

    return updated_rows

def _copy_row_styles_from_neighbor(ws, new_row, ref_row, col_end=None):
    """
    将 ref_row 的样式复制到 new_row（整行）。
    用于 insert_rows 后新行获得正确的字体、填充、对齐样式。
    不复制值，只复制样式。
    """
    from openpyxl.cell import MergedCell as _MC_crs
    if col_end is None:
        col_end = ws.max_column
    for c in range(1, col_end + 1):
        src = ws.cell(ref_row, c)
        dst = ws.cell(new_row, c)
        if isinstance(src, _MC_crs) or isinstance(dst, _MC_crs):
            continue
        copy_cell_style(src, dst)


def _ensure_merge(ws, min_row, min_col, max_row, max_col):
    """安全合并：先拆除覆盖该范围的现有合并，再合并"""
    to_remove = []
    for mr in ws.merged_cells.ranges:
        if not (mr.max_row < min_row or mr.min_row > max_row or
                mr.max_col < min_col or mr.min_col > max_col):
            to_remove.append(mr)
    for mr in to_remove:
        ws.merged_cells.remove(mr)
    ws.merge_cells(start_row=min_row, start_column=min_col,
                   end_row=max_row,   end_column=max_col)


def _rebuild_all_merges_and_formulas(ws, date_str, log_func=print):
    """
    统一重建所有数据行的合并单元格和公式。

    在 sync_advisors_and_rank（纯数据操作）之后调用。
    负责：
    1. 扫描分组结构
    2. 重建所有合并（A列团队名、A:E合计行、R/V/Z/AA/AB列团队内合并）
    3. 重写所有公式（数据行+合计行+公司总合计行）
    4. 更新财富中心相关列

    date_str: 如 '20260508'
    """
    month_str = get_month_str(date_str)  # 202605
    year_str = get_year_str(date_str)    # 2026

    # ── 0. 扫描分组结构 ──
    group_list = []  # [(leader_name, first_data_row, last_data_row, sum_row), ...]
    company_total_row = None

    cur_leader = None
    cur_start = None
    cur_end = None

    for row_idx in range(4, ws.max_row + 1):
        a_val = ws.cell(row_idx, 1).value
        b_val = ws.cell(row_idx, 2).value
        is_sum = a_val and '合计' in str(a_val)
        has_name = b_val and isinstance(b_val, str) and b_val.strip() and not is_sum

        if is_sum:
            if cur_start is not None:
                group_list.append((cur_leader, cur_start, cur_end, row_idx))
            cur_leader = None
            cur_start = None
            cur_end = None
            # 判断是否是公司总合计行（F列公式包含多个+号，非SUM格式）
            f_val = ws.cell(row_idx, 6).value
            if f_val and isinstance(f_val, str) and f_val.startswith('='):
                if '+' in f_val and 'SUM' not in f_val.upper():
                    company_total_row = row_idx
        elif has_name:
            new_leader = str(a_val).strip() if a_val else None
            if cur_start is not None and new_leader is not None and new_leader != cur_leader:
                group_list.append((cur_leader, cur_start, cur_end, None))
                cur_leader = None
                cur_start = None
                cur_end = None
            if cur_start is None:
                cur_leader = new_leader
                cur_start = row_idx
            cur_end = row_idx

    if not group_list:
        log_func("  ⚠️ 未找到任何分组结构，跳过合并/公式重建")
        return

    # ── 0.5 合并相邻的同 leader 分组，并清理夹在中间的合计行 ──
    # sync 插入新理财师后，同一团队的成员可能被拆分为多个分组
    # 例如：某团队长A(4人) + 合计行 + 团队长A(组员1人) → 应合并为一个分组
    # 同时，夹在中间的"合计行"（F列为SUM公式）应作为合并后分组的 sum_row
    merged_groups = []
    orphan_sum_rows = set()
    i = 0
    while i < len(group_list):
        cur_leader, cur_fr, cur_lr, cur_sr = group_list[i]
        cur_sum = cur_sr  # 当前分组的合计行

        # 检查 i 和 i+1 之间是否夹着一个"合计行"（数据行之外）
        # 这需要看 cur_lr+1 是否有合计行
        # 以及 i+1 是否与当前 leader 相同
        j = i + 1
        while j < len(group_list):
            next_leader, next_fr, next_lr, next_sr = group_list[j]
            if next_leader != cur_leader or cur_leader is None:
                break

            # 检查 cur_lr 和 next_fr 之间是否有合计行
            orphan_sum_row = None
            for check_r in range(cur_lr + 1, next_fr):
                a_val = ws.cell(check_r, 1).value
                if a_val and '合计' in str(a_val):
                    f_val = ws.cell(check_r, 6).value
                    if f_val and isinstance(f_val, str) and f_val.startswith('=SUM'):
                        orphan_sum_row = check_r
                    break  # 找到第一个合计行就停

            if orphan_sum_row:
                # 合并分组，使用 next_sr 作为正确的合计行
                # orphan_sum_row 是旧的多余合计行，稍后删除
                cur_lr = next_lr
                cur_sum = next_sr if next_sr is not None else orphan_sum_row
                if cur_sum != orphan_sum_row:
                    orphan_sum_rows.add(orphan_sum_row)
                log_func(f"  合并分组: {cur_leader}（跨合计行 Row {orphan_sum_row}，使用 Row {cur_sum} 作为合计行）")
                j += 1
            else:
                break

        merged_groups.append((cur_leader, cur_fr, cur_lr, cur_sum))
        i = j

    if len(merged_groups) != len(group_list):
        group_list = merged_groups

    # 删除合并过程中发现的旧合计行，并更新行号
    if orphan_sum_rows:
        for dr in sorted(orphan_sum_rows, reverse=True):
            ws.delete_rows(dr, 1)
            log_func(f"  删除多余旧合计行: Row {dr}")
            # 更新 group_list 行号
            new_groups = []
            for (leader, fr, lr, sr) in group_list:
                nfr = fr - 1 if fr > dr else fr
                nlr = lr - 1 if lr > dr else lr
                nsr = sr - 1 if sr and sr > dr else sr
                new_groups.append((leader, nfr, nlr, nsr))
            group_list = new_groups
            if company_total_row and company_total_row > dr:
                company_total_row -= 1

    log_func(f"  扫描到 {len(group_list)} 个团队分组"
             + (f"（公司总合计行: {company_total_row}）" if company_total_row else ""))

    # ── 1. 清除所有数据行合并（保留 Row 1-3）──
    to_remove = []
    for mr in list(ws.merged_cells.ranges):
        if mr.min_row >= 4:
            to_remove.append(mr)
    for mr in to_remove:
        ws.merged_cells.remove(mr)

    # ── 1.5 补建缺失合计行 ──
    # 某些分组（如新理财师插入导致分组分裂）可能没有合计行（sr is None）
    # 需要在该组最后一行之后插入合计行
    missing_sum_groups = [(i, g) for i, g in enumerate(group_list) if g[3] is None]
    if missing_sum_groups:
        log_func(f"  发现 {len(missing_sum_groups)} 个缺少合计行的分组，正在补建...")
        # 从后往前处理，避免行号偏移干扰
        for gi, (leader, fr, lr, sr) in reversed(missing_sum_groups):
            # 确定插入位置：该组最后一行的下一行
            insert_pos = lr + 1
            # 检查插入位置是否在公司总合计行之后
            if company_total_row and insert_pos >= company_total_row:
                insert_pos = company_total_row
            ws.insert_rows(insert_pos, 1)
            ws.cell(insert_pos, 1).value = '合计'
            # 从已有合计行复制样式（insert_rows 后新行默认是 Excel 等线11号）
            for _existing_sr in group_list:
                _esr = _existing_sr[3]
                if _esr and _esr != insert_pos and _esr < insert_pos:
                    _copy_row_styles_from_neighbor(ws, insert_pos, _esr)
                    break
            log_func(f"    补建合计行: {leader or '未知团队'} Row {insert_pos}（在 Row {lr} 之后）")
            # 更新 group_list 中该分组的 sr
            group_list[gi] = (leader, fr, lr, insert_pos)
            # 更新后续分组（索引 > gi）的行号 +1
            for j in range(gi + 1, len(group_list)):
                old_g = group_list[j]
                new_fr = old_g[1] + 1 if old_g[1] >= insert_pos else old_g[1]
                new_lr = old_g[2] + 1 if old_g[2] >= insert_pos else old_g[2]
                new_sr = old_g[3] + 1 if old_g[3] and old_g[3] >= insert_pos else old_g[3]
                group_list[j] = (old_g[0], new_fr, new_lr, new_sr)
            # 更新公司总合计行行号
            if company_total_row and company_total_row >= insert_pos:
                company_total_row += 1
        log_func(f"  已补建 {len(missing_sum_groups)} 个合计行")

    # ── 2. 重建 A 列团队名合并 + A:E 合计行合并 ──
    for (leader, fr, lr, sr) in group_list:
        if sr is None:
            continue  # 无合计行的分组（不应出现）
        # A 列合并（团队名）
        if fr < lr:
            ws.merge_cells(start_row=fr, start_column=1, end_row=lr, end_column=1)
        # 写团队名到首行
        if leader:
            ws.cell(fr, 1).value = leader
        # A:E 合计行合并
        ws.cell(sr, 1).value = '合计'
        ws.merge_cells(start_row=sr, start_column=1, end_row=sr, end_column=5)

    # 公司总合计行
    if company_total_row:
        ws.cell(company_total_row, 1).value = '合计'
        ws.merge_cells(start_row=company_total_row, start_column=1,
                       end_row=company_total_row, end_column=5)
        # 公司总合计行设置黄色填充
        import copy as _copy2
        from openpyxl.styles import PatternFill as _PF
        yellow_fill = _PF(start_color='FFFFFF00', end_color='FFFFFF00', patternType='solid')
        for c in range(1, ws.max_column + 1):
            ws.cell(company_total_row, c).fill = _copy2.copy(yellow_fill)

    # ── 3. 识别关键列号 ──
    # 当月传统/保险/管理费/月折标列
    cur_trad_col = cur_ins_col = cur_mgr_col = cur_month_zb_col = None
    month_zb_cols = []  # 所有月折标列
    ytd_col = None
    monthly_groups = {}  # {YYYYMM: (trad_col, ins_col, mgr_col, zb_col)}

    for cell in ws[2]:
        if cell.value is None:
            continue
        val = str(cell.value).replace('\n', '').strip()
        m_trad = re.match(r'^传统(\d{6})$', val)
        m_ins = re.match(r'^保险(\d{6})$', val)
        m_mgr = re.match(r'^管理费(\d{6})$', val)
        m_zb = re.search(rf'{year_str}年(\d+)月折标', val)

        if m_trad:
            mm = m_trad.group(1)
            if mm not in monthly_groups:
                monthly_groups[mm] = [cell.column, None, None, None]
            monthly_groups[mm][0] = cell.column
        elif m_ins:
            mm = m_ins.group(1)
            if mm not in monthly_groups:
                monthly_groups[mm] = [None, cell.column, None, None]
            monthly_groups[mm][1] = cell.column
        elif m_mgr:
            mm = m_mgr.group(1)
            if mm not in monthly_groups:
                monthly_groups[mm] = [None, None, cell.column, None]
            monthly_groups[mm][2] = cell.column
        elif m_zb:
            mm = f"{year_str}{int(m_zb.group(1)):02d}"
            if mm not in monthly_groups:
                monthly_groups[mm] = [None, None, None, cell.column]
            monthly_groups[mm][3] = cell.column
            month_zb_cols.append(cell.column)

        if f'{year_str}年YTD折标' in val:
            ytd_col = cell.column

    if month_str in monthly_groups:
        cur_trad_col, cur_ins_col, cur_mgr_col, cur_month_zb_col = monthly_groups[month_str]

    # 按月份降序排列月折标列（最新月在最左）
    month_zb_cols.sort()

    # 月完成率列
    completion_col = None
    for cell in ws[2]:
        if cell.value and '月度' in str(cell.value) and '完成率' in str(cell.value):
            completion_col = cell.column
            break

    # 财富中心列
    fc_task_col = None
    fc_rate_col = None
    fc_gap_col = None
    for cell in ws[2]:
        if cell.value is None:
            continue
        val = str(cell.value).replace('\n', '').strip()
        if ('上半年任务' in val or '上半年年任务' in val) and '完成率' not in val and '差距' not in val:
            fc_task_col = cell.column
        elif '上半年任务完成率' in val:
            fc_rate_col = cell.column
        elif val in ('任务差距', '上半年任务差距', '财富中心上半年任务差距'):
            fc_gap_col = cell.column

    # 考核期差距列
    kaohe_gap_col = None
    for cell in ws[2]:
        if cell.value and str(cell.value).replace('\n', '').strip() == '考核期差距':
            kaohe_gap_col = cell.column
            break

    # ── 5. 重写数据行公式 ──
    from openpyxl.cell import MergedCell as _MC_r5

    for (leader, fr, lr, sr) in group_list:
        # 收集该分组内需要跳过的合计行（夹在 fr..lr 之间的合计行）
        sum_rows_in_range = set()
        if sr and fr <= sr <= lr:
            sum_rows_in_range.add(sr)
        # 也检查其他可能的合计行（A列含"合计"）
        for check_r in range(fr, lr + 1):
            a_v = ws.cell(check_r, 1).value
            if a_v and '合计' in str(a_v):
                sum_rows_in_range.add(check_r)

        for row_idx in range(fr, lr + 1):
            if row_idx in sum_rows_in_range:
                continue  # 跳过合计行
            # 月完成率公式（逐行写，不合并）
            if completion_col and cur_month_zb_col:
                kc = get_column_letter(cur_month_zb_col)
                c = ws.cell(row_idx, completion_col)
                if not isinstance(c, _MC_r5):
                    c.value = (
                        f'=IF(F{row_idx}=0,"",{kc}{row_idx}/(F{row_idx}/MAX(1,(YEAR(E{row_idx})*12+MONTH(E{row_idx}))'
                        f'-(YEAR(D{row_idx})*12+MONTH(D{row_idx}))+1)))'
                    )
                    c.number_format = '0.00%'

            # 当月折标公式（逐行写）
            if cur_month_zb_col:
                tc = get_column_letter(cur_trad_col) if cur_trad_col else ''
                ic = get_column_letter(cur_ins_col) if cur_ins_col else ''
                mc = get_column_letter(cur_mgr_col) if cur_mgr_col else ''
                if tc and ic and mc:
                    c = ws.cell(row_idx, cur_month_zb_col)
                    if not isinstance(c, _MC_r5):
                        c.value = f'=SUM({tc}{row_idx}:{mc}{row_idx})'

            # 考核期差距 = F - G（逐行写，不合并）
            if kaohe_gap_col:
                c = ws.cell(row_idx, kaohe_gap_col)
                if not isinstance(c, _MC_r5):
                    c.value = f'=F{row_idx}-G{row_idx}'

            # YTD公式（逐行写）
            if ytd_col and month_zb_cols:
                c = ws.cell(row_idx, ytd_col)
                if not isinstance(c, _MC_r5):
                    refs = '+'.join([f'{get_column_letter(col)}{row_idx}' for col in month_zb_cols])
                    c.value = f'=SUM({refs})'

    # ── 5.5. 重建其他列的团队内合并（在写入公式之后执行）──
    # 只合并"团队级指标"列（财富中心上半年任务、1月销量等），
    # 每人独立指标列（月度完成率、考核期完成率、考核期差距、任务差距）不合并，
    # 这些列将在后续 _update_fc_cols / _update_kaohe_def_cols 中逐行写公式。
    merge_cols_data = []  # 需要按团队合并的列
    for cell in ws[2]:
        if cell.value is None:
            continue
        val = str(cell.value).replace('\n', '').strip()
        # 财富中心上半年任务（团队任务，整组共享一个值，需要合并）
        if ('上半年任务' in val or '上半年年任务' in val) and '完成率' not in val and '差距' not in val:
            merge_cols_data.append((cell.column, val))
        # 1月销量列（历史遗留的团队级列）
        if '1月销量' in val:
            merge_cols_data.append((cell.column, val))
        # 注意：月度完成率、考核期完成率、考核期差距、任务差距 是每人独立指标，
        # 不在此处合并，交由 _update_fc_cols / _update_kaohe_def_cols 逐行处理

    for (col, _kw) in merge_cols_data:
        for (leader, fr, lr, sr) in group_list:
            if sr is None:
                continue
            if fr < lr:
                ws.merge_cells(start_row=fr, start_column=col, end_row=lr, end_column=col)
    # 清理引用
    del merge_cols_data

    # ── 5.8. 财富中心上半年年任务列：团队负责人行改为 =SUM(F列) ──
    # 该列的值应该是该团队每位理财师考核任务目标(F列)之和
    if fc_task_col:
        lc_f = get_column_letter(6)  # F列 = 考核任务目标
        lc_task = get_column_letter(fc_task_col)
        for (leader, fr, lr, sr) in group_list:
            # 团队负责人行的 fc_task_col 写为 =SUM(F{fr}:F{lr})
            # lr 是该组最后一个有名字的行（含负责人本身和所有成员）
            c = ws.cell(fr, fc_task_col)
            c.value = f'=SUM({lc_f}{fr}:{lc_f}{lr})'
            c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            # 该团队成员行的 fc_task_col 保持为空（已合并，不单独写入）

    # ── 6. 重写合计行公式 ──
    sum_rows = []  # 所有团队合计行（公司总合计行除外）

    for (leader, fr, lr, sr) in group_list:
        if sr is None:
            continue
        if lr >= sr:
            log_func(f"  [WARN] 跳过错误分组: {leader} lr={lr} >= sr={sr}")
            continue
        sum_rows.append(sr)

        # 所有数据列写 SUM 公式
        for row_idx in range(fr, lr + 1):
            for col_idx in range(7, ws.max_column + 1):  # 从G列开始
                cell = ws.cell(row_idx, col_idx)
                if cell.value is not None and isinstance(cell.value, (int, float)):
                    # 有数值的数据列，在合计行写SUM
                    cl = get_column_letter(col_idx)
                    ws.cell(sr, col_idx).value = f'=SUM({cl}{fr}:{cl}{lr})'
                    break  # 只对有数据的列写SUM（由第一个数据行决定）

        # 更精确地：对传统/保险/管理费/月折标列写SUM
        for mm, (tc, ic, mc, zbc) in sorted(monthly_groups.items()):
            for c in [tc, ic, mc, zbc]:
                if c is not None:
                    cl = get_column_letter(c)
                    ws.cell(sr, c).value = f'=SUM({cl}{fr}:{cl}{lr})'

        # G列（考核期间已完成折标）SUM
        ws.cell(sr, 7).value = f'=SUM(G{fr}:G{lr})'
        # F列（考核任务目标）SUM
        ws.cell(sr, 6).value = f'=SUM(F{fr}:F{lr})'

        # 月折标列SUM
        for zbc in month_zb_cols:
            cl = get_column_letter(zbc)
            ws.cell(sr, zbc).value = f'=SUM({cl}{fr}:{cl}{lr})'

        # YTD合计行公式
        if ytd_col and month_zb_cols:
            refs = '+'.join([f'{get_column_letter(c)}{sr}' for c in month_zb_cols])
            ws.cell(sr, ytd_col).value = f'=SUM({refs})'

        # 月完成率合计行公式
        if completion_col and cur_month_zb_col:
            kc = get_column_letter(cur_month_zb_col)
            ws.cell(sr, completion_col).value = (
                f'=IF(F{sr}=0,"",{kc}{sr}/(F{sr}/MAX(1,(YEAR(E{fr})*12+MONTH(E{fr}))'
                f'-(YEAR(D{fr})*12+MONTH(D{fr}))+1)))'
            )

        # 考核期完成率
        if kaohe_gap_col:
            ws.cell(sr, kaohe_gap_col).value = f'=F{sr}-G{sr}'

        # 财富中心相关列（分子用「上半年折标」口径，YTD 会被下半年月份污染）
        if fc_task_col:
            ws.cell(sr, fc_task_col).value = f'={get_column_letter(fc_task_col)}{fr}'
        if fc_rate_col and fc_task_col and ytd_col:
            tl = get_column_letter(fc_task_col)
            h1_done = _h1_done_expr(ws, ytd_col, sr)
            ws.cell(sr, fc_rate_col).value = f'={h1_done}/{tl}{fr}'
        if fc_gap_col and fc_task_col and ytd_col:
            tl = get_column_letter(fc_task_col)
            h1_done = _h1_done_expr(ws, ytd_col, sr)
            ws.cell(sr, fc_gap_col).value = f'={tl}{fr}-{h1_done}'

    # ── 7. 重写公司总合计行公式 ──
    if company_total_row and sum_rows:
        # 只写到数据有效列（表头最右有值列），避免向 AF~BE 等历史空白列写入多余公式
        max_data_col = 1
        for cell in ws[2]:
            if cell.value is not None:
                max_data_col = max(max_data_col, cell.column)
        for col_idx in range(6, max_data_col + 1):
            # 使用 =sum_row1+sum_row2+... 格式
            refs = '+'.join([f'{get_column_letter(col_idx)}{sr}' for sr in sum_rows])
            ws.cell(company_total_row, col_idx).value = f'={refs}'

        # 财富中心列也用各团队合计行相加
        if fc_task_col:
            refs = '+'.join([f'{get_column_letter(fc_task_col)}{sr}' for sr in sum_rows])
            ws.cell(company_total_row, fc_task_col).value = f'={refs}'
        if fc_rate_col and fc_task_col and ytd_col:
            yl = get_column_letter(ytd_col)
            tl = get_column_letter(fc_task_col)
            ws.cell(company_total_row, fc_rate_col).value = f'={yl}{company_total_row}/{tl}{company_total_row}'
        if fc_gap_col and fc_task_col and ytd_col:
            yl = get_column_letter(ytd_col)
            tl = get_column_letter(fc_task_col)
            ws.cell(company_total_row, fc_gap_col).value = f'={tl}{company_total_row}-{yl}{company_total_row}'

        # 清理总合计行超出数据有效列范围的遗留公式（模板历史遗留）
        _cleaned_extra = 0
        for c in range(max_data_col + 1, ws.max_column + 1):
            cell = ws.cell(company_total_row, c)
            if cell.value is not None:
                cell.value = None
                _cleaned_extra += 1
        if _cleaned_extra:
            log_func(f"  已清理总合计行 Row {company_total_row} 超出有效列范围的 {_cleaned_extra} 个遗留公式")

    log_func(f"  已重建合并单元格和公式（{len(group_list)} 个团队"
             + (f" + 公司总合计" if company_total_row else "") + "）")

    # ── 7.5 修复数据区域边框 ──
    # 在重建合并和写入公式之后，按行类型修复边框（不碰字体/填充/对齐）
    # 找到数据区域右边界（表头最右有值列）
    max_data_col = 1
    for cell in ws[2]:
        if cell.value is not None:
            max_data_col = max(max_data_col, cell.column)
    if group_list:
        _fix_data_area_borders(ws, group_list, company_total_row,
                               col_start=1, col_end=max_data_col,
                               log_func=log_func)
        log_func(f"  已修复数据区域边框（Col A~{get_column_letter(max_data_col)}）")

    # ── 清理数据区域的空行 ──
    # sync 创建"其他团队"时可能多预留了行，导致出现空行
    # 注意：公司目标行的 A/B 列为空，但有内容（如"公司目标"文字），不能误删
    empty_rows = []
    for r in range(4, ws.max_row + 1):
        a_val = ws.cell(r, 1).value
        b_val = ws.cell(r, 2).value
        is_sum = a_val and '合计' in str(a_val)
        if not is_sum and not b_val and not a_val:
            # A和B都为空，检查该行是否有任何内容
            has_content = False
            for c_idx in range(3, ws.max_column + 1):
                cv = ws.cell(r, c_idx).value
                if cv is not None:
                    has_content = True
                    break
            if not has_content:
                empty_rows.append(r)
    if empty_rows:
        for r in reversed(empty_rows):
            ws.delete_rows(r, 1)
        log_func(f"  已清理 {len(empty_rows)} 个空行")

    # 注：不再"清理非合计行填充色"——数据行的白色实色填充(#FFFFFFFF)是原始模板的有意设置

    # ── 统一行高 ──
    # 将所有数据行（除标题行、表头行、特殊行高外）统一为 14.25
    for r in range(1, ws.max_row + 1):
        h = ws.row_dimensions[r].height
        # 保留特殊行高：Row1(20.25), Row2-3(25.5), 公司总合计行(35.1), 末尾空行(24.0)
        if h is not None and h not in (20.25, 25.5, 35.1, 24.0):
            ws.row_dimensions[r].height = 14.25
    log_func(f"  已统一数据行高为 14.25")


def fix_row1_merges(ws, date_str, log_func=print):
    """
    修复 Row1 的合并居中：
    - A1 合并到「财富中心」列之前：标题「私人财富管理部折标销售进度表-截止YYYYMMDD」
    - 「上半年/下半年时间进度」所在列 合并 2 列
    - Row1 时间进度引用公式修正为指向正确时间进度单元格
    """
    from datetime import datetime as _dt

    # 判断当前是上半年还是下半年
    try:
        cutoff_date = _dt.strptime(date_str, '%Y%m%d').date()
        if cutoff_date.month <= 6:
            period_label = '上半年时间进度'
        else:
            period_label = '下半年时间进度'
    except Exception:
        period_label = '上半年时间进度'

    # 找「财富中心上半年任务」列（主标题合并到这列之前）
    fc_task_col  = _find_header_col(ws, '财富中心')
    # 找「考核期差距」列（备用结束列）
    task_gap_col = _find_header_col(ws, '考核期差距')
    title_end_col = (fc_task_col - 1) if fc_task_col else task_gap_col

    # 找「上半年时间进度」或「下半年时间进度」在哪列
    time_progress_col = None
    for cell in ws[1]:
        if cell.value and ('上半年时间进度' in str(cell.value) or '下半年时间进度' in str(cell.value) or '时间进度' in str(cell.value)):
            time_progress_col = cell.column
            break

    if title_end_col and title_end_col >= 1:
        _ensure_merge(ws, 1, 1, 1, title_end_col)
        title_cell = ws.cell(1, 1)
        if title_cell.value and isinstance(title_cell.value, str):
            title_cell.value = re.sub(r'截止\d{8}', f'截止{date_str}', title_cell.value)
        title_cell.alignment = Alignment(horizontal='center', vertical='center',
                                          wrap_text=False)
        log_func(f"  已合并主标题 A1:{get_column_letter(title_end_col)}1")

    if time_progress_col:
        # 更新标题为上半年/下半年
        ws.cell(1, time_progress_col).value = period_label
        _ensure_merge(ws, 1, time_progress_col, 1, time_progress_col + 1)
        ws.cell(1, time_progress_col).alignment = Alignment(
            horizontal='center', vertical='center')
        log_func(f"  已合并「{period_label}」"
                 f"{get_column_letter(time_progress_col)}1:{get_column_letter(time_progress_col+1)}1")

        # 修复时间进度引用公式：使用与 update_time_progress 相同的逻辑
        # 找到包含"计算日期"和"时间进度"的行，然后引用其下一行的值
        START_COL = 25
        prog_label_row = None
        prog_rate_col = None

        for r in range(1, ws.max_row + 1):
            texts = []
            for c_idx in range(START_COL, min(ws.max_column + 1, 100)):
                v = ws.cell(r, c_idx).value
                if v and isinstance(v, str):
                    texts.append(v.strip())
            text_joined = ' '.join(texts)
            if '计算日期' in text_joined and '时间进度' in text_joined and '全年' not in text_joined:
                prog_label_row = r
                break

        if prog_label_row:
            for c_idx in range(START_COL, 100):
                v = ws.cell(prog_label_row, c_idx).value
                if v and '时间进度' in str(v):
                    prog_rate_col = c_idx
                    break

        if prog_rate_col:
            prog_data_row = prog_label_row + 1
            target_ref_cell = ws.cell(1, time_progress_col + 2)
            target_ref_cell.value = f'={get_column_letter(prog_rate_col)}{prog_data_row}'
            log_func(f"  已修正时间进度引用公式: "
                     f"={get_column_letter(prog_rate_col)}{prog_data_row} → "
                     f"{get_column_letter(time_progress_col+2)}1")
        else:
            log_func(f"  未找到「计算日期/时间进度」模块，无法修正进度引用公式")


def _col_op_with_merge_shift(ws, at_col, n=1, delete=False):
    """
    插列/删列时同步修正合并单元格（openpyxl 的 insert_cols/delete_cols
    不会平移合并区，直接调用会导致合并区错位）：
    - insert: min_col>=at_col 的合并区右移 n；跨越插入点的合并区向右扩展 n
    - delete: min_col>at_col 的合并区左移 1；覆盖被删列的收窄 1；完全落在被删列的移除
    """
    ops = []
    for mr in list(ws.merged_cells.ranges):
        if delete:
            if mr.min_col > at_col:
                ops.append((mr.min_row, mr.min_col - 1, mr.max_row, mr.max_col - 1, str(mr)))
            elif mr.min_col <= at_col <= mr.max_col:
                if mr.min_col == mr.max_col:
                    ops.append((None, None, None, None, str(mr)))      # 整体被删
                else:
                    ops.append((mr.min_row, mr.min_col, mr.max_row, mr.max_col - 1, str(mr)))
        else:
            if mr.min_col >= at_col:
                ops.append((mr.min_row, mr.min_col + n, mr.max_row, mr.max_col + n, str(mr)))
            elif mr.min_col < at_col <= mr.max_col:
                ops.append((mr.min_row, mr.min_col, mr.max_row, mr.max_col + n, str(mr)))
    for *_, s in ops:
        ws.unmerge_cells(s)
    if delete:
        ws.delete_cols(at_col, 1)
    else:
        ws.insert_cols(at_col, n)
    for r1, c1, r2, c2, _ in ops:
        if r1 is not None:
            ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)


def _ensure_h2_layout(ws, date_str, log_func=print):
    """
    下半年布局改造（幂等，须在 sync 人员之后、公式重建之前调用）：
    A) 旧版四列下半年模块的「下半年已完成折标」列整列删除（折标挪到主数据区）
    B) 主数据区 YTD 列后插入「{year}下半年折标」列（7~12月折标合计，公式由
       _refresh_h2_zb_col 在月份列全部就位后写入）
    仅下半年周期（截止月份>=7）生效。
    """
    from copy import copy as _copy
    try:
        cutoff = datetime.strptime(date_str, '%Y%m%d').date()
    except Exception:
        return
    if cutoff.month < 7:
        return
    year = cutoff.year

    # ── A) 删除旧模块的「下半年已完成折标」列 ──
    old_zb_col = None
    for cell in ws[2]:
        v = str(cell.value) if cell.value else ''
        if '下半年' in v and '已完成折标' in v:
            old_zb_col = cell.column
            break
    if old_zb_col:
        _col_op_with_merge_shift(ws, old_zb_col, 1, delete=True)
        log_func(f"  已删除旧下半年模块的「已完成折标」列（原第{old_zb_col}列），"
                 f"折标统一由主数据区「{year}下半年折标」列呈现")

    # ── B) YTD 列后依次插入「{year}上半年折标」「{year}下半年折标」两列 ──
    def _find_hdr(regex):
        for cell in ws[2]:
            if cell.value and re.search(regex, str(cell.value).replace('\n', '')):
                return cell.column
        return None

    ytd_col = _find_hdr(r'20\d{2}年\s*YTD折标')
    h1_zb_col = _find_hdr(r'20\d{2}\s*上半年折标')
    h2_zb_col = _find_hdr(r'20\d{2}\s*下半年折标')

    if ytd_col and not h1_zb_col:
        at = ytd_col + 1
        _col_op_with_merge_shift(ws, at, 1, delete=False)
        ref = ws.cell(2, ytd_col)
        hdr = ws.cell(2, at)
        hdr.value = f'{year}上半年折标'
        if ref.has_style:
            hdr.font = _copy(ref.font)
            hdr.fill = _copy(ref.fill)
            hdr.border = _copy(ref.border)
            hdr.alignment = _copy(ref.alignment)
        _ensure_merge(ws, 2, at, 3, at)
        try:
            ws.column_dimensions[get_column_letter(at)].width = (
                ws.column_dimensions[get_column_letter(ytd_col)].width or 12.5)
        except Exception:
            pass
        h1_zb_col = at
        log_func(f"  已在YTD列后插入「{year}上半年折标」列（第{at}列）")

    if h1_zb_col and not h2_zb_col:
        at = h1_zb_col + 1
        _col_op_with_merge_shift(ws, at, 1, delete=False)
        ref = ws.cell(2, h1_zb_col)
        hdr = ws.cell(2, at)
        hdr.value = f'{year}下半年折标'
        if ref.has_style:
            hdr.font = _copy(ref.font)
            hdr.fill = _copy(ref.fill)
            hdr.border = _copy(ref.border)
            hdr.alignment = _copy(ref.alignment)
        _ensure_merge(ws, 2, at, 3, at)
        try:
            ws.column_dimensions[get_column_letter(at)].width = (
                ws.column_dimensions[get_column_letter(h1_zb_col)].width or 12.5)
        except Exception:
            pass
        log_func(f"  已在上半年折标列后插入「{year}下半年折标」列（第{at}列）")

    # ── C) 差距列布局规范化（幂等）──
    # 最终形态：主数据区只保留「当前考核期已完成折标」镜像列（AQ后）；
    # 差距展示统一走右侧模块区：H1模块「上半年任务差距」→ H2模块「下半年任务差距」
    # →「{year}全年任务差距」（末端列由 _add_h2_module 建表头、_refresh_gap_cols 写公式）。
    # C1. 删除旧版主数据区「{year}上半年/下半年任务差距」列（与模块差距列重复）
    for regex in (r'20\d{2}\s*上半年任务差距', r'20\d{2}\s*下半年任务差距'):
        c = _find_hdr(regex)
        if c:
            _col_op_with_merge_shift(ws, c, 1, delete=True)
            log_func(f"  已删除主数据区重复差距列（原第{c}列，与模块差距列同口径）")

    # C2a. 「当前考核期任务差距」整列退役（与「考核期差距」列同口径 F-G，重复展示），
    #      无论出现在主数据区还是模块区末端一律删除
    c = _find_hdr(r'当前考核期任务差距')
    if c:
        _col_op_with_merge_shift(ws, c, 1, delete=True)
        log_func(f"  已删除「当前考核期任务差距」列（原第{c}列，与「考核期差距」同口径，重复）")

    # C2b. 「{year}全年任务差距」若残留在主数据区中部（H1模块差距列左侧）则删除，
    #      由 _add_h2_module 在模块区末端重建
    h1m_gap = None
    for cell in ws[2]:
        v = str(cell.value).replace('\n', '').strip() if cell.value else ''
        if v in ('任务差距', '上半年任务差距', '财富中心上半年任务差距'):
            h1m_gap = cell.column
            break
    c = _find_hdr(r'20\d{2}\s*全年任务差距')
    if c and h1m_gap and c < h1m_gap:
        _col_op_with_merge_shift(ws, c, 1, delete=True)
        log_func(f"  已将「{year}全年任务差距」从主数据区移除（原第{c}列），改由模块区末端呈现")

    # C3. 「当前考核期已完成折标」镜像列保持在「下半年折标」后（不存在则插入）
    anchor_col = _find_hdr(r'20\d{2}\s*下半年折标')
    if anchor_col and not _find_hdr(r'当前考核期已完成折标'):
        at = anchor_col + 1
        _col_op_with_merge_shift(ws, at, 1, delete=False)
        ref = ws.cell(2, anchor_col)
        hdr = ws.cell(2, at)
        hdr.value = '当前考核期已完成折标'
        if ref.has_style:
            hdr.font = _copy(ref.font)
            hdr.fill = _copy(ref.fill)
            hdr.border = _copy(ref.border)
            hdr.alignment = _copy(ref.alignment)
        _ensure_merge(ws, 2, at, 3, at)
        try:
            ws.column_dimensions[get_column_letter(at)].width = (
                ws.column_dimensions[get_column_letter(anchor_col)].width or 12.5)
        except Exception:
            pass
        log_func(f"  已插入「当前考核期已完成折标」镜像列（第{at}列，=G列，"
                 f"公式由 _refresh_gap_cols 写入）")


def _normalize_module_headers(ws, log_func=print):
    """
    规范右侧任务模块表头命名（只改名不动列位置，幂等），目标为用户确认版命名：
    - 「财富中心上半年年任务」→「财富中心上半年任务」（修"年年"笔误）
    - H1模块「任务差距」/「上半年任务差距」→「财富中心上半年任务差距」（与H2对齐）
    - H2模块「下半年任务完成率」→「财富中心下半年任务完成率」
    - H2模块「下半年任务差距」→「财富中心下半年任务差距」
    注意：依赖这些表头的扫描逻辑均已兼容新旧两种写法。
    """
    from openpyxl.cell import MergedCell as _MC
    RENAMES = {
        '财富中心上半年年任务':   '财富中心\n上半年任务',
        '任务差距':              '财富中心\n上半年任务差距',
        '上半年任务差距':         '财富中心\n上半年任务差距',
        '下半年任务完成率':       '财富中心下半年\n任务完成率',
        '下半年任务差距':         '财富中心下半年\n任务差距',
    }
    renamed = []
    for cell in ws[2]:
        if isinstance(cell, _MC) or not cell.value:
            continue
        v = str(cell.value).replace('\n', '').strip()
        if v in RENAMES:
            cell.value = RENAMES[v]
            renamed.append(v)
    if renamed:
        log_func(f"  已规范模块表头命名：{'、'.join(renamed)}")


def _sanitize_double_equals(ws, log_func=print):
    """
    修复外部编辑器（WPS/腾讯文档本地编辑）保存后产生的「==」开头无效公式：
    '==G4' → '=G4'。正常公式绝不会以 == 开头，全表扫描安全。
    """
    fixed = 0
    for row in ws.iter_rows():
        for cell in row:
            v = cell.value
            if isinstance(v, str) and v.startswith('=='):
                cell.value = '=' + v[2:]
                fixed += 1
    if fixed:
        log_func(f"  已修复 {fixed} 个「==」开头的无效公式（外部编辑器残留）")


def _reorder_summary_cols(ws, date_str, log_func=print):
    """
    右侧汇总区列顺序规范化（用户确认版，幂等）：
      月度完成率 → 当前考核期已完成折标 → 考核期完成率（累计） → 考核期差距
      → {year}上半年折标 → {year}下半年折标 → {year}年YTD折标
      → 财富中心上半年任务 / 完成率 / 任务差距
      → 财富中心下半年任务 / 完成率 / 任务差距
      → {year}全年任务差距
    物理搬运整列（值+样式+列宽）；公式随后由 refresh 链按表头定位整体重写，
    故无需逐格修引用。顺序已正确则跳过。仅下半年周期（截止月份>=7）生效。
    须在 _normalize_module_headers 之后、所有公式刷新之前调用。
    """
    from copy import copy as _copy
    try:
        cutoff = datetime.strptime(date_str, '%Y%m%d').date()
    except Exception:
        return
    if cutoff.month < 7:
        return
    year = cutoff.year

    def _norm(v):
        return str(v).replace('\n', '').strip() if v else ''

    # (匹配器, 目标表头描述) —— 顺序即目标列序
    MATCHERS = [
        lambda v: v == '月度完成率',
        lambda v: v == '当前考核期已完成折标',
        lambda v: '考核期完成率' in v,
        lambda v: v == '考核期差距',
        lambda v: bool(re.fullmatch(rf'{year}\s*上半年折标', v)),
        lambda v: bool(re.fullmatch(rf'{year}\s*下半年折标', v)),
        lambda v: bool(re.fullmatch(rf'{year}年?\s*YTD折标', v)),
        lambda v: v == '财富中心上半年任务',
        lambda v: '财富中心' in v and '上半年' in v and '完成率' in v,
        lambda v: v == '财富中心上半年任务差距',
        lambda v: v == '财富中心下半年任务',
        lambda v: '财富中心' in v and '下半年' in v and '完成率' in v,
        lambda v: v == '财富中心下半年任务差距',
        lambda v: bool(re.fullmatch(rf'{year}\s*全年任务差距', v)),
    ]

    cols = []
    for matcher in MATCHERS:
        found = None
        for cell in ws[2]:
            if cell.value and matcher(_norm(cell.value)):
                found = cell.column
                break
        if not found:
            log_func("  ⚠️ 列顺序规范化：汇总区列不齐，跳过（首次布局由后续步骤建立）")
            return
        cols.append(found)

    if cols == sorted(cols):
        return  # 顺序已正确，幂等跳过

    start = min(cols)
    max_r = ws.max_row

    # 快照每列（值 + 样式 + 列宽）
    snap = []
    for c in cols:
        letter = get_column_letter(c)
        width = None
        try:
            width = ws.column_dimensions[letter].width
        except Exception:
            pass
        cells = []
        for r in range(1, max_r + 1):
            cell = ws.cell(r, c)
            cells.append((cell.value, _copy(cell._style)))
        snap.append((cells, width))

    # 解除与汇总区相交的所有合并（表头 Row2:3 / 模块数据区纵向合并 / Row1 合并），
    # 后续由 fix_row1_merges / _update_fc_cols / _add_h2_module / 本函数重建
    zone_end = start + len(cols) - 1
    for mr in list(ws.merged_cells.ranges):
        if not (mr.max_col < start or mr.min_col > zone_end):
            ws.unmerge_cells(str(mr))

    # 按目标顺序写回
    for i, (cells, width) in enumerate(snap):
        tc = start + i
        for r in range(1, max_r + 1):
            v, st = cells[r - 1]
            cell = ws.cell(r, tc)
            cell.value = v
            cell._style = _copy(st)
        letter = get_column_letter(tc)
        if width:
            try:
                ws.column_dimensions[letter].width = width
            except Exception:
                pass
        # 表头 Row2:3 纵向合并重建
        _ensure_merge(ws, 2, tc, 3, tc)

    log_func(f"  已规范汇总区列顺序（{get_column_letter(start)}~"
             f"{get_column_letter(zone_end)}：完成率/考核期 → 折标 → 任务模块 → 全年差距）")


def _unify_number_formats(ws, date_str, log_func=print):
    """
    统一数值格式（用户要求：小数点后只保留两位）：
      - 金额类（F/G、月度折标、折标汇总、YTD、任务、镜像列）→ #,##0.00
      - 差距类（考核期差距、上/下半年任务差距、全年任务差距）→ #,##0.00_);[Red](#,##0.00)
      - 完成率类（月度/考核期/上半年/下半年）→ 0.00%
    只设数据行（Row4 起），跳过表头与 MergedCell 从属格。仅下半年周期生效。
    """
    from openpyxl.cell import MergedCell as _MC
    try:
        cutoff = datetime.strptime(date_str, '%Y%m%d').date()
    except Exception:
        return
    if cutoff.month < 7:
        return
    year = cutoff.year

    FMT_NUM = '#,##0.00'
    FMT_GAP = '#,##0.00_);[Red](#,##0.00)'
    FMT_PCT = '0.00%'

    def _norm(v):
        return str(v).replace('\n', '').strip() if v else ''

    fmt_map = {}   # col -> fmt
    for cell in ws[2]:
        v = _norm(cell.value)
        if not v:
            continue
        c = cell.column
        if v in ('考核任务目标（按实际考核月数折算）', '考核期间已完成折标'):
            fmt_map[c] = FMT_NUM
        elif re.fullmatch(r'20\d{2}年\d+月折标', v):
            fmt_map[c] = FMT_NUM
        elif re.fullmatch(r'(传统|保险|管理费)20\d{4}', v):
            fmt_map[c] = FMT_NUM
        elif re.fullmatch(rf'{year}\s*(上半年|下半年)折标', v):
            fmt_map[c] = FMT_NUM
        elif re.fullmatch(rf'{year}年?\s*YTD折标', v):
            fmt_map[c] = FMT_NUM
        elif v == '当前考核期已完成折标':
            fmt_map[c] = FMT_NUM
        elif v in ('财富中心上半年任务', '财富中心下半年任务'):
            fmt_map[c] = FMT_NUM
        elif v == '考核期差距' or '任务差距' in v:
            fmt_map[c] = FMT_GAP
        elif '完成率' in v:
            fmt_map[c] = FMT_PCT

    max_r = ws.max_row
    for c, fmt in fmt_map.items():
        for r in range(4, max_r + 1):
            cell = ws.cell(r, c)
            if isinstance(cell, _MC):
                continue
            cell.number_format = fmt

    log_func(f"  已统一数值格式（金额/差距两位小数、完成率 0.00%，共 {len(fmt_map)} 列）")


def _refresh_h2_zb_col(ws, date_str, log_func=print):
    """
    刷新主数据区「{year}上半年折标」/「{year}下半年折标」两列公式
    （在月份列/团队结构全部就位后调用）：
    - 上半年列 = 当年 1~6 月各月折标列之和；下半年列 = 当年 7~12 月各月折标列之和
    - 数据行逐行公式，团队合计行 = SUM(组内数据行)；公司合计行 = 各团队合计行相加
    - 数据行浅紫高亮（参考需求方样表），合计行沿用模板紫
    仅下半年周期（截止月份>=7）生效。
    """
    try:
        cutoff = datetime.strptime(date_str, '%Y%m%d').date()
    except Exception:
        return
    if cutoff.month < 7:
        return
    year = cutoff.year

    groups = _scan_group_structure(ws)
    teams = [(fr, lr, sr) for (leader, fr, lr, sr) in groups if sr and leader]
    sum_rows = [t[2] for t in teams]
    company_row = None
    for r in range(4, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if a and '合计' in str(a) and r not in sum_rows:
            company_row = r
            break

    light_purple = PatternFill('solid', fgColor='FFE6E6FA')

    for label, m_lo, m_hi in [('上半年', 1, 6), ('下半年', 7, 12)]:
        zb_col = None
        for cell in ws[2]:
            if cell.value and re.search(rf'20\d{{2}}\s*{label}折标', str(cell.value)):
                zb_col = cell.column
                break
        if not zb_col:
            log_func(f"  ⚠️ 未找到「{year}{label}折标」列，跳过刷新")
            continue

        m_cols = []
        for cell in ws[2]:
            if not cell.value:
                continue
            m = re.search(r'(20\d{2})年\s*(\d{1,2})月折标', str(cell.value))
            if m and int(m.group(1)) == year and m_lo <= int(m.group(2)) <= m_hi:
                m_cols.append(cell.column)

        lc = get_column_letter(zb_col)
        for fr, lr, sr in teams:
            for r in range(fr, lr + 1):
                cell = ws.cell(r, zb_col)
                if m_cols:
                    cell.value = '=' + '+'.join(
                        f'{get_column_letter(c)}{r}' for c in m_cols)
                else:
                    cell.value = 0
                cell.number_format = '#,##0.00'
                cell.fill = light_purple
            s_cell = ws.cell(sr, zb_col)
            s_cell.value = f'=SUM({lc}{fr}:{lc}{lr})'
            s_cell.number_format = '#,##0.00'

        if company_row and sum_rows:
            c_cell = ws.cell(company_row, zb_col)
            c_cell.value = '=' + '+'.join(f'{lc}{sr}' for sr in sum_rows)
            c_cell.number_format = '#,##0.00'

        log_func(f"  已刷新「{year}{label}折标」列（{lc}列，含{len(m_cols)}个月份）")


def _refresh_gap_cols(ws, date_str, log_func=print):
    """
    刷新差距/镜像类列的公式（在 _refresh_h2_zb_col 和 _add_h2_module 之后调用）：
      - 「当前考核期已完成折标」（主数据区，AQ后）= G列镜像，逐行 =G{r}
      - 「{year}全年任务差距」（模块区最末）= 上半年任务差距 + 下半年任务差距
        （团队合计行/公司合计行口径，个人行留空）
    （「当前考核期任务差距」已退役：与「考核期差距」列同口径 F-G，重复展示）
    上/下半年任务差距由 H1/H2 模块自身列承担（_update_fc_cols/_add_h2_module 维护），
    本函数不再重复建列。仅下半年周期（截止月份>=7）生效。
    """
    from copy import copy as _copy
    from openpyxl.cell import MergedCell as _MC
    try:
        cutoff = datetime.strptime(date_str, '%Y%m%d').date()
    except Exception:
        return
    if cutoff.month < 7:
        return
    year = cutoff.year

    def _find(regex):
        for cell in ws[2]:
            if cell.value and re.search(regex, str(cell.value).replace('\n', '')):
                return cell.column
        return None

    kh_zb  = _find(r'当前考核期已完成折标')
    yr_gap = _find(r'20\d{2}\s*全年任务差距')
    h2_zb  = _find(r'20\d{2}\s*下半年折标')
    # H1/H2 模块自身的差距列（作为全年差距的加数）
    h1m_gap = h2m_gap = None
    for cell in ws[2]:
        if not cell.value:
            continue
        v = str(cell.value).replace('\n', '').strip()
        if v in ('任务差距', '上半年任务差距', '财富中心上半年任务差距'):
            h1m_gap = cell.column
        elif v in ('下半年任务差距', '财富中心下半年任务差距'):
            h2m_gap = cell.column

    missing = [name for name, c in [
        ('当前考核期已完成折标', kh_zb),
        (f'{year}全年任务差距', yr_gap), (f'{year}下半年折标', h2_zb),
        ('上半年任务差距(模块)', h1m_gap), ('下半年任务差距(模块)', h2m_gap)] if not c]
    if missing:
        log_func(f"  ⚠️ 差距/镜像列刷新：缺少列 {missing}，跳过")
        return

    groups = _scan_group_structure(ws)
    teams = [(fr, lr, sr) for (leader, fr, lr, sr) in groups if sr and leader]
    sum_rows = [t[2] for t in teams]
    company_row = None
    for r in range(4, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if a and '合计' in str(a) and r not in sum_rows:
            company_row = r
            break

    GAP_FMT = '#,##0.00_);[Red](#,##0.00)'
    Lg1, Lg2 = get_column_letter(h1m_gap), get_column_letter(h2m_gap)

    def _style_like(dst, ref_col, r, fmt):
        src = ws.cell(r, ref_col)
        if src.has_style:
            dst.font = _copy(src.font)
            dst.fill = _copy(src.fill)
            dst.border = _copy(src.border)
            dst.alignment = _copy(src.alignment)
        dst.number_format = fmt

    max_r = company_row if company_row else ws.max_row
    for r in range(4, max_r + 1):
        # 考核期折标镜像：=G{r}（逐行）
        c1 = ws.cell(r, kh_zb)
        if not isinstance(c1, _MC):
            _style_like(c1, h2_zb, r, '#,##0.00')
            c1.value = f'=G{r}'
        # 全年任务差距：先清空（个人行留空，团队/公司口径）
        c3 = ws.cell(r, yr_gap)
        if not isinstance(c3, _MC):
            _style_like(c3, h2_zb, r, GAP_FMT)
            c3.value = None

    # 全年任务差距：团队合计行 + 公司合计行 = 上半年差距 + 下半年差距
    for fr, lr, sr in teams:
        ws.cell(sr, yr_gap).value = f'={Lg1}{sr}+{Lg2}{sr}'
    if company_row:
        ws.cell(company_row, yr_gap).value = f'={Lg1}{company_row}+{Lg2}{company_row}'

    log_func(f"  已刷新差距/镜像列：「当前考核期已完成折标」({get_column_letter(kh_zb)}=G镜像)、"
             f"「{year}全年任务差距」({get_column_letter(yr_gap)}={Lg1}+{Lg2}，"
             f"{len(teams)}团队+公司合计)")


def _add_h2_module(ws, date_str, log_func=print):
    """
    下半年考核模块（7~12月）：
    - 在现有「上半年任务差距」列（财富中心上半年模块末列）右侧新增 4 列：
      财富中心下半年任务 / 下半年任务完成率 / 下半年任务差距 / {year}全年任务差距
      （末列为全年差距汇总，表头在此创建，公式由 _refresh_gap_cols 写入）
    - 团队任务 = SUMPRODUCT(逐人: F × MAX(0,考核期∩下半年月数) ÷ 考核期总月数)
      （月序号 = 年×12+月；考核期不完整时按月均目标折算）
    - 已完成 = 当年 7~12 月各月折标列之和（按表头动态定位）
    - 时间进度模块若与新区重叠则整体平移避让，Row1 引用同步指向新位置
    - 仅下半年周期（截止月份>=7）生成；公司目标行下半年目标留空由人工填写
    """
    from copy import copy as _copy

    try:
        cutoff = datetime.strptime(date_str, '%Y%m%d').date()
    except Exception:
        log_func("  ⚠️ 下半年模块：无法解析日期，跳过")
        return
    if cutoff.month < 7:
        log_func("  下半年模块：当前为上半年周期，跳过生成")
        return

    year = cutoff.year
    H2_S = year * 12 + 7     # 下半年起始月序号
    H2_E = year * 12 + 12    # 下半年结束月序号

    # ── 1. 定位现有模块末列（「任务差距」，排除「考核期差距」、主数据区三档差距列
    #       及「当前考核期任务差距」镜像列）──
    gap_col = None
    for cell in ws[2]:
        if (cell.value and '任务差距' in str(cell.value)
                and not re.search(r'20\d{2}|考核期', str(cell.value))):
            gap_col = cell.column   # 列序升序遍历，首个命中即原模块（再往右是本模块自身列）
            break
    if not gap_col:
        log_func("  ⚠️ 下半年模块：未找到「任务差距」列，跳过")
        return

    h0 = gap_col + 1                      # 下半年任务列
    SAFE_END = gap_col + 6                # 新模块(4列)+缓冲右边界
    NEW_MOD = gap_col + 7                 # 时间进度模块平移目标起始列

    # ── 2. 定位时间进度模块（新模块区内则平移重建）──
    prog_cells = []
    for r in range(1, 13):
        for c in range(gap_col + 1, min(ws.max_column + 1, 100)):
            v = ws.cell(r, c).value
            if isinstance(v, str) and any(k in v for k in
                    ('开始日期', '结束日期', '计算日期', '时间进度')):
                prog_cells.append((r, c))

    rate_ref = None
    if prog_cells:
        mod_col0 = min(c for _, c in prog_cells)
        mod_col1 = max(c for _, c in prog_cells)
        mod_row0 = min(r for r, _ in prog_cells)
        if mod_col0 <= SAFE_END:
            # 与新模块区重叠 → 清空旧块，平移到 NEW_MOD 重建（语义与 _update_progress_module 一致）
            for r in range(mod_row0, mod_row0 + 4):
                for c in range(mod_col0, mod_col1 + 1):
                    cell = ws.cell(r, c)
                    cell.value = None
                    cell.number_format = 'General'
            c0 = NEW_MOD
            r0 = mod_row0
            ws.cell(r0, c0).value = '开始日期'
            ws.cell(r0, c0 + 1).value = '结束日期'
            ws.cell(r0 + 1, c0).value = _dt.date(year, 7, 1)
            ws.cell(r0 + 1, c0 + 1).value = _dt.date(year, 12, 31)
            ws.cell(r0 + 1, c0).number_format = 'yyyy/mm/dd'
            ws.cell(r0 + 1, c0 + 1).number_format = 'yyyy/mm/dd'
            ws.cell(r0 + 2, c0).value = '计算日期'
            ws.cell(r0 + 2, c0 + 2).value = '时间进度'
            ws.cell(r0 + 3, c0).value = cutoff
            ws.cell(r0 + 3, c0).number_format = 'yyyy/mm/dd'
            ws.cell(r0 + 3, c0 + 1).value = (
                f'={get_column_letter(c0+1)}{r0+1}-{get_column_letter(c0)}{r0+1}+1')
            ws.cell(r0 + 3, c0 + 1).number_format = '0'
            ws.cell(r0 + 3, c0 + 2).value = (
                f'=({get_column_letter(c0)}{r0+3}-{get_column_letter(c0)}{r0+1}+1)'
                f'/{get_column_letter(c0+1)}{r0+3}')
            ws.cell(r0 + 3, c0 + 2).number_format = '0.00%'
            rate_ref = f'{get_column_letter(c0+2)}{r0+3}'
            log_func(f"  时间进度模块已平移至 {get_column_letter(c0)}~{get_column_letter(c0+2)}"
                     f"（避开下半年模块），时间进度引用={rate_ref}")
        else:
            # 位置安全，仅定位时间进度值单元格（标签下一行同列）
            for r, c in prog_cells:
                v = ws.cell(r, c).value
                if isinstance(v, str) and '时间进度' in v and '计算' not in v:
                    rate_ref = f'{get_column_letter(c)}{r+1}'
                    break
    else:
        log_func("  ⚠️ 下半年模块：未找到时间进度模块标签，跳过")
        return

    # ── 3. Row1 时间进度引用修复 ──
    for cell in ws[1]:
        if (cell.value and isinstance(cell.value, str)
                and '时间进度' in cell.value and '截止' not in cell.value):
            label_col = cell.column
            width = 2
            for mr in ws.merged_cells.ranges:
                if mr.min_row == 1 and mr.min_col == label_col:
                    width = mr.max_col - mr.min_col + 1
                    break
            ws.cell(1, label_col + width).value = f'={rate_ref}'
            break

    # ── 4. 团队结构 + 公司合计行 ──
    groups = _scan_group_structure(ws)
    teams = [(fr, lr, sr) for (leader, fr, lr, sr) in groups
             if sr is not None and leader]
    if not teams:
        log_func("  ⚠️ 下半年模块：未扫描到团队结构，跳过")
        return
    sum_rows = [t[2] for t in teams]
    company_row = None
    for r in range(4, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if a and '合计' in str(a) and r not in sum_rows:
            company_row = r
            break

    # ── 5. 定位主数据区「{year}下半年折标」列（完成率/差距公式的取数源）──
    zb_col = None
    for cell in ws[2]:
        if cell.value and re.search(r'20\d{2}\s*下半年折标', str(cell.value)):
            zb_col = cell.column
            break
    if not zb_col:
        log_func(f"  ⚠️ 下半年模块：未找到「{year}下半年折标」列，跳过")
        return
    zL = get_column_letter(zb_col)

    # ── 6. 读取任务列现有手填值（数值=人工下达的目标，重跑必须保留）──
    manual_tasks = {}
    for fr, lr, sr in teams:
        v = ws.cell(fr, h0).value
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            manual_tasks[fr] = v

    # ── 6b. 清空新区旧内容（4列+1缓冲列；不动公司目标行的人工目标）──
    # 先解新区内所有合并（含表头 Row2:3，否则重跑时清到 MergedCell 会报错）
    for mr in list(ws.merged_cells.ranges):
        if mr.min_col >= h0 and mr.max_col <= h0 + 4:
            ws.unmerge_cells(str(mr))
    clear_end = company_row if company_row else ws.max_row
    for r in range(2, clear_end + 1):
        for c in range(h0, h0 + 5):   # 4列新区 + 1列缓冲（清历史模块残留垃圾）
            cell = ws.cell(r, c)
            cell.value = None
            cell.number_format = 'General'

    # ── 7. 表头（用户确认版命名；末列 = 全年任务差距汇总，公式由 _refresh_gap_cols 写）──
    HEADERS = ['财富中心\n下半年任务', '财富中心下半年\n任务完成率',
               '财富中心下半年\n任务差距', f'{year}全年\n任务差距']
    ref_h = ws.cell(2, gap_col - 2)   # 「财富中心上半年任务」表头样式参考
    for i, text in enumerate(HEADERS):
        c = h0 + i
        cell = ws.cell(2, c)
        cell.value = text
        if ref_h.has_style:
            cell.font = _copy(ref_h.font)
            cell.fill = _copy(ref_h.fill)
            cell.border = _copy(ref_h.border)
            cell.alignment = _copy(ref_h.alignment)
        _ensure_merge(ws, 2, c, 3, c)
        ws.column_dimensions[get_column_letter(c)].width = 12.5

    # ── 8. 团队锚点 + 合计行 + 公司行 ──
    def _to_date(v):
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, _dt.date):
            return v
        if isinstance(v, str):
            for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y.%m.%d'):
                try:
                    return datetime.strptime(v.strip(), fmt).date()
                except ValueError:
                    pass
        return None

    def _prorated(fr, lr):
        """逐人：月均目标(F÷考核期月数) × 考核期落在下半年的月数，再合计（占位参考值）"""
        total = 0.0
        for r in range(fr, lr + 1):
            d = _to_date(ws.cell(r, 4).value)
            e = _to_date(ws.cell(r, 5).value)
            try:
                f = float(ws.cell(r, 6).value or 0)
            except (TypeError, ValueError):
                f = 0.0
            if f <= 0 or not d or not e:
                continue
            dm, em = d.year * 12 + d.month, e.year * 12 + e.month
            ov = min(em, H2_E) - max(dm, H2_S) + 1
            sp = em - dm + 1
            if ov > 0 and sp > 0:
                total += f * ov / sp
        return round(total, 2)

    fmts = ['#,##0.00', '0.00%', '0.00_);[Red](0.00)']
    ref_a = ws.cell(4, gap_col - 2)               # 锚点样式参考
    ref_s = ws.cell(teams[0][2], gap_col - 2)      # 合计行样式参考
    ref_c = ws.cell(company_row, gap_col - 2) if company_row else None

    def _style_from(ref, cell, number_format):
        if ref is not None and ref.has_style:
            cell.font = _copy(ref.font)
            cell.fill = _copy(ref.fill)
            cell.border = _copy(ref.border)
            cell.alignment = _copy(ref.alignment)
        cell.number_format = number_format

    h0L = get_column_letter(h0)
    n_manual = 0
    for fr, lr, sr in teams:
        # 任务：手填值优先，否则折算占位
        if fr in manual_tasks:
            ws.cell(fr, h0).value = manual_tasks[fr]
            n_manual += 1
        else:
            ws.cell(fr, h0).value = _prorated(fr, lr)
        # 完成率 = 主数据区下半年折标团队合计 ÷ 任务；差距 = 任务 - 折标合计
        ws.cell(fr, h0 + 1).value = f'=IF({h0L}{fr}=0,"",{zL}{sr}/{h0L}{fr})'
        ws.cell(fr, h0 + 2).value = f'={h0L}{fr}-{zL}{sr}'
        for i, fmt in enumerate(fmts):
            _style_from(ref_a, ws.cell(fr, h0 + i), fmt)
            if lr > fr:
                _ensure_merge(ws, fr, h0 + i, lr, h0 + i)
        # 合计行引用锚点
        for i in range(3):
            cell = ws.cell(sr, h0 + i)
            cell.value = f'={get_column_letter(h0+i)}{fr}'
            _style_from(ref_s, cell, fmts[i])

    if company_row:
        cr = company_row
        ws.cell(cr, h0).value = '=' + '+'.join(f'{h0L}{sr}' for sr in sum_rows)
        ws.cell(cr, h0 + 1).value = f'=IF({h0L}{cr}=0,"",{zL}{cr}/{h0L}{cr})'
        ws.cell(cr, h0 + 2).value = f'={h0L}{cr}-{zL}{cr}'
        for i, fmt in enumerate(fmts):
            _style_from(ref_c, ws.cell(cr, h0 + i), fmt)

        # 公司目标行（合计行下一行）：目标格保留人工填写，完成率/差距给公式
        gr = cr + 1
        ws.cell(gr, h0 + 1).value = f'=IF({h0L}{gr}>0,{zL}{cr}/{h0L}{gr},"")'
        ws.cell(gr, h0 + 2).value = f'=IF({h0L}{gr}>0,{h0L}{gr}-{zL}{cr},"")'
        ws.cell(gr, h0 + 1).number_format = '0.00%'
        ws.cell(gr, h0 + 2).number_format = '0.00_);[Red](0.00)'

    log_func(f"  下半年模块已生成（{h0L}~{get_column_letter(h0+3)}，含末端「{year}全年任务差距」列，"
             f"共 {len(teams)} 个团队）："
             f"任务列 {n_manual} 个团队保留手填值、{len(teams)-n_manual} 个团队填折算参考值；"
             f"完成率/差距取数自「{year}下半年折标」列({zL})；"
             f"Row{(company_row + 1) if company_row else '?'} 公司下半年目标请人工填写")


def insert_columns_after(ws, after_col_idx, num_cols):
    """
    在 after_col_idx 列之后插入 num_cols 列
    （openpyxl insert_cols 在指定位置之前插入）
    """
    ws.insert_cols(after_col_idx + 1, num_cols)


def update_sheet(ws, source_data, date_str, log_func=print, prev_g_data=None, all_data=None, keep_other_team=True, all_months=None):
    """
    核心：在给定的周度sheet中插入新列并填入数据
    date_str:   如 20260313
    source_data: {理财师: {传统202603: v, ...}}
    prev_g_data: {理财师: G列值} 来自上一周期sheet，可为None
    all_data:    {理财师: {__考核起始日__: dt, __考核终止日__: dt, __考核任务目标__: v, ...}} 可为None
    """
    month_str = get_month_str(date_str)  # 202603
    year_str = get_year_str(date_str)    # 2026
    month_num = int(month_str[4:])       # 3

    # ── Step 0: 同步新增/离职理财师 + 职级 ──
    log_func(f"\n  同步人员变动和职级...")
    sync_advisors_and_rank(ws, source_data, log_func, keep_other_team=keep_other_team)

    # ── Step 0a0: 修复外部编辑器残留的「==」无效公式（须在一切公式逻辑前）──
    _sanitize_double_equals(ws, log_func)

    # ── Step 0a: 下半年布局（插「下半年折标」列/删旧模块折标列，须在公式重建前）──
    _ensure_h2_layout(ws, date_str, log_func)

    # ── Step 0a2: 规范模块表头命名（用户确认版：财富中心上/下半年任务×完成率×差距）──
    _normalize_module_headers(ws, log_func)

    # ── Step 0a3: 汇总区列顺序规范化（用户确认版，公式由后续 refresh 链重写）──
    _reorder_summary_cols(ws, date_str, log_func)

    # ── Step 0b: 修复 Row1 标题行合并 ──
    fix_row1_merges(ws, date_str, log_func)

    # 检查新列是否已存在
    existing_ct_col = None
    for cell in ws[2]:
        if cell.value and str(cell.value).strip() == f'传统{month_str}':
            existing_ct_col = cell.column
            break

    if existing_ct_col is not None:
        # 列已存在 → 直接覆盖更新数据，不插入新列
        log_func(f"  列'传统{month_str}'已存在（第{existing_ct_col}列），直接更新数值...")
        col_chuantong = existing_ct_col
        col_baoxian   = existing_ct_col + 1
        col_guanli    = existing_ct_col + 2
        col_month_zb  = existing_ct_col + 3
        lc = get_column_letter(col_chuantong)
        lb = get_column_letter(col_baoxian)
        lg = get_column_letter(col_guanli)
        lm = get_column_letter(col_month_zb)

        # 找理财师数据行
        data_rows = []
        for row_idx in range(4, ws.max_row + 1):
            name_cell = ws.cell(row=row_idx, column=2)
            a_cell = ws.cell(row=row_idx, column=1)
            if a_cell.value and '合计' in str(a_cell.value):
                continue
            if name_cell.value and isinstance(name_cell.value, str) and name_cell.value.strip():
                data_rows.append((row_idx, name_cell.value.strip()))

        matched, unmatched = 0, []
        for row_idx, name in data_rows:
            if name in source_data:
                row_data = source_data[name]
                ws.cell(row=row_idx, column=col_chuantong).value = row_data.get(f'传统{month_str}', 0)
                ws.cell(row=row_idx, column=col_baoxian).value   = row_data.get(f'保险{month_str}', 0)
                ws.cell(row=row_idx, column=col_guanli).value    = row_data.get(f'管理费{month_str}', 0)
                ws.cell(row=row_idx, column=col_month_zb).value  = f'=SUM({lc}{row_idx}:{lg}{row_idx})'
                matched += 1
            else:
                unmatched.append(name)
        log_func(f"  更新完成：匹配 {matched} 人，未匹配(保持不变): {len(unmatched)} 人")
        if unmatched:
            log_func(f"  未匹配: {unmatched}")

        # ── 已存在路径也需要做的修复（与新建路径保持一致）──
        # 扫描团队分组（用于颜色/财富中心等修复）—— 必须在 _update_kaohe_def_cols 之前完成
        group_list_exist = []
        cur_s = cur_e = None
        for row_idx in range(4, ws.max_row + 1):
            a_v = ws.cell(row_idx, 1).value
            b_v = ws.cell(row_idx, 2).value
            is_sum = a_v and '合计' in str(a_v)
            has_nm = b_v and isinstance(b_v, str) and b_v.strip() and not is_sum
            if is_sum:
                if cur_s is not None:
                    group_list_exist.append((cur_s, cur_e, row_idx))
                cur_s = cur_e = None
            elif has_nm:
                if cur_s is None: cur_s = row_idx
                cur_e = row_idx
        # 找 YTD 列
        year_str_e = get_year_str(date_str)
        ytd_col_e = None
        for cell in ws[2]:
            if cell.value and f'{year_str_e}年YTD折标' in str(cell.value):
                ytd_col_e = cell.column
                break

        # ── 统一重建合并和公式 ──
        _rebuild_all_merges_and_formulas(ws, date_str, log_func)

        _fix_header_row23_merges(ws, log_func)
        _update_progress_module(ws, date_str, log_func)

        # ── 回溯更新：用最新源数据覆盖该 sheet 所有历史月份列 ──
        if all_data:
            log_func(f"\n🔁 回溯更新历史月份列（跳过当月 {month_str}）...")
            retro_update_sheet(ws, all_data, log_func, skip_month=month_str, all_months=all_months)

        _hide_old_month_data_cols(ws, month_str, log_func)

        # ── 复制模板列宽（已存在路径：从最近模板sheet复制列宽）──
        wb_target = ws.parent
        template_sheet_for_width = None
        latest_date_w = 0
        for sname in wb_target.sheetnames:
            m = re.match(r'周度团队数据-折标\+考核(\d{4})', sname)
            if m and sname != ws.title:
                d = int(m.group(1))
                if d > latest_date_w:
                    latest_date_w = d
                    template_sheet_for_width = sname
        if template_sheet_for_width:
            ws_tpl = wb_target[template_sheet_for_width]
            for col_letter, dim in ws_tpl.column_dimensions.items():
                if col_letter in ws.column_dimensions:
                    ws.column_dimensions[col_letter].width = dim.width
                else:
                    ws.column_dimensions[col_letter] = copy.copy(dim)
            log_func(f"  已复制模板 '{template_sheet_for_width}' 列宽")

        # ── 已存在路径末尾：重新修复数据区域边框（retro_update 可能破坏边框）──
        group_list_final = _scan_group_structure(ws)
        if group_list_final:
            company_total_row_final = _find_company_total_row(ws, group_list_final)
            max_data_col_final = 1
            for cell in ws[2]:
                if cell.value is not None:
                    max_data_col_final = max(max_data_col_final, cell.column)
            _fix_data_area_borders(ws, group_list_final, company_total_row_final,
                                   col_start=1, col_end=max_data_col_final,
                                   log_func=log_func)
            log_func(f"  已修复数据区域边框")

        return True

    # 找'考核期间已完成折标'列位置
    zheubiao_col = None
    for cell in ws[2]:
        if cell.value and '考核期间已完成折标' in str(cell.value):
            zheubiao_col = cell.column
            break

    if zheubiao_col is None:
        raise ValueError("在目标sheet中未找到'考核期间已完成折标'列，请检查文件格式。")

    log_func(f"  '考核期间已完成折标' 在第 {zheubiao_col} 列 ({get_column_letter(zheubiao_col)})")

    # 在 zheubiao_col 之后插入 4 列（传统、保险、管理费、本月折标）
    insert_pos = zheubiao_col + 1
    ws.insert_cols(insert_pos, 4)
    log_func(f"  已在第 {insert_pos} 列之后插入 4 列")

    # 复制相邻列（zheubiao_col）的宽度到新插入的4列
    ref_col_letter = get_column_letter(zheubiao_col)
    ref_width = ws.column_dimensions.get(ref_col_letter, None)
    if ref_width and ref_width.width:
        for offset in range(4):
            new_col_letter = get_column_letter(insert_pos + offset)
            ws.column_dimensions[new_col_letter].width = ref_width.width
        log_func(f"  已复制列宽到新插入列（参考 {ref_col_letter} 列，宽度 {ref_width.width}）")

    # 新列索引
    col_chuantong = insert_pos      # 传统YYYYMM
    col_baoxian   = insert_pos + 1  # 保险YYYYMM
    col_guanli    = insert_pos + 2  # 管理费YYYYMM
    col_month_zb  = insert_pos + 3  # YYYY年M月折标

    # 列字母
    lc = get_column_letter(col_chuantong)
    lb = get_column_letter(col_baoxian)
    lg = get_column_letter(col_guanli)
    lm = get_column_letter(col_month_zb)

    # ── 写 Header（Row 2）──
    # 参考旧的月份header样式（从旧列复制样式）
    # 先找一个已有的 传统/保险/管理费 header cell 来复制样式
    style_ref = None
    for cell in ws[2]:
        if cell.value and re.search(r'^传统20\d{4}', str(cell.value)):
            style_ref = cell
            break

    def make_header_cell(ws, row, col, value, ref_cell=None):
        c = ws.cell(row=row, column=col, value=value)
        if ref_cell:
            copy_cell_style(ref_cell, c)
        else:
            c.font = Font(bold=True)
            c.alignment = Alignment(wrap_text=True, vertical='center', horizontal='center')
        return c

    make_header_cell(ws, 2, col_chuantong, f'传统{month_str}', style_ref)
    make_header_cell(ws, 2, col_baoxian,   f'保险{month_str}', style_ref)
    make_header_cell(ws, 2, col_guanli,    f'管理费{month_str}', style_ref)

    # 本月折标header：找已有的 月折标 列样式
    month_style_ref = None
    for cell in ws[2]:
        if cell.value and re.search(r'月折标', str(cell.value)):
            month_style_ref = cell
            break
    make_header_cell(ws, 2, col_month_zb,
                     f'{year_str}年\n{month_num}月折标', month_style_ref)

    # ── 新列 Row2:Row3 合并（与其他表头列保持一致）──
    # 注意：循环内用局部变量 _lc_tmp，不覆盖外部 lc/lb/lg/lm
    for col_idx in [col_chuantong, col_baoxian, col_guanli, col_month_zb]:
        _lc_tmp = get_column_letter(col_idx)
        # 先移除该列 Row2 可能已有的合并
        to_rm = [mr for mr in ws.merged_cells.ranges
                 if mr.min_col <= col_idx <= mr.max_col and mr.min_row <= 2 <= mr.max_row]
        for mr in to_rm:
            ws.merged_cells.remove(mr)
        ws.merge_cells(f'{_lc_tmp}2:{_lc_tmp}3')
        c2 = ws.cell(2, col_idx)
        c2.alignment = Alignment(horizontal='center', vertical='center',
                                 wrap_text=getattr(c2.alignment, 'wrap_text', True))
    log_func(f"  已合并新列 Row2:Row3 表头")

    # ── 修复 Row 1（标题行）合并范围：插入新列后，原来覆盖 zheubiao_col 的合并区域
    #    需要向右扩展 4 格，以包含新插入的 4 列 ──
    row1_to_remerge = []
    for mr in list(ws.merged_cells.ranges):
        if mr.min_row == 1 and mr.max_row == 1:
            # 如果该合并区域的右端恰好是 zheubiao_col（因为插入时会自动右移），
            # 或覆盖了 insert_pos-1，则扩展到包含 col_month_zb
            if mr.min_col <= (insert_pos - 1) <= mr.max_col:
                row1_to_remerge.append((mr.min_col, mr.max_col + 4, mr.min_row, mr.max_row))
                ws.merged_cells.remove(mr)
    for (c1, c2, r1, r2) in row1_to_remerge:
        lc1 = get_column_letter(c1)
        lc2 = get_column_letter(c2)
        ws.merge_cells(f'{lc1}{r1}:{lc2}{r2}')
        # 同步 Row 1 首格的居中对齐
        c_cell = ws.cell(r1, c1)
        c_cell.alignment = Alignment(
            horizontal='center', vertical='center',
            wrap_text=getattr(c_cell.alignment, 'wrap_text', True)
        )
    if row1_to_remerge:
        log_func(f"  已扩展 Row1 标题行合并范围（包含 {len(row1_to_remerge)} 处）")

    # ── 修复 Row 2 表头：确保所有非空表头单元格（非合并从属格）居中对齐 ──
    from openpyxl.cell import MergedCell as _MergedCell
    for cell in ws[2]:
        if isinstance(cell, _MergedCell):
            continue
        if cell.value is not None:
            cell.alignment = Alignment(
                horizontal='center', vertical='center',
                wrap_text=getattr(cell.alignment, 'wrap_text', True)
            )

    # ── 找到 YTD 列（在新列插入之后，所有"月折标"列）──
    # YTD列：当年所有月折标列（包含刚插入的本月折标列）
    year_month_zb_cols = []
    for cell in ws[2]:
        if cell.value and isinstance(cell.value, str):
            v = cell.value.replace('\n', '')
            if re.search(rf'{year_str}年\d+月折标', v) or re.search(rf'{year_str}\d{{2}}折标', v):
                year_month_zb_cols.append(cell.column)

    log_func(f"  YTD 相关列: {[get_column_letter(c) for c in year_month_zb_cols]}")

    # 找 YTD 列（已有）
    ytd_col = None
    for cell in ws[2]:
        if cell.value and f'{year_str}年YTD折标' in str(cell.value):
            ytd_col = cell.column
            break

    # 找月完成率列
    completion_col = None
    for cell in ws[2]:
        if cell.value and '月度' in str(cell.value) and '完成率' in str(cell.value):
            completion_col = cell.column
            break

    # 找考核任务目标列
    task_target_col = None
    for cell in ws[2]:
        if cell.value and '考核任务目标' in str(cell.value):
            task_target_col = cell.column
            break

    log_func(f"  YTD列:{get_column_letter(ytd_col) if ytd_col else '无'}, "
             f"月完成率列:{get_column_letter(completion_col) if completion_col else '无'}, "
             f"考核任务目标列:{get_column_letter(task_target_col) if task_target_col else '无'}")

    # 找 Row 3（空行）以及数据行范围
    # 扫描所有数据行（从 row 4 开始）
    # 理财师名字在 B 列（col 2）
    # 注意：有些行 A 列是团队名、合计，B 列是理财师名
    data_rows = []
    for row_idx in range(4, ws.max_row + 1):
        name_cell = ws.cell(row=row_idx, column=2)
        a_cell = ws.cell(row=row_idx, column=1)
        # 跳过合计行、空行
        if a_cell.value and '合计' in str(a_cell.value):
            continue
        if name_cell.value and isinstance(name_cell.value, str) and name_cell.value.strip():
            name = name_cell.value.strip()
            data_rows.append((row_idx, name))

    log_func(f"  找到 {len(data_rows)} 个理财师行")

    # ── 确定样式参考列（从表头扫描，找同类已有列）──
    # 优先找已有的 传统YYYYMM 列（蓝色背景，#,##0.00 数字格式）
    ref_col_data = None    # 传统/保险/管理费 数值列的样式参考
    ref_col_month = None   # 月折标列的样式参考
    for cell in ws[2]:
        if not cell.value:
            continue
        val = str(cell.value).strip()
        # 已有的传统/保险/管理费列（排除刚插入的新列）
        if re.search(r'^(传统|保险|管理费)20\d{4}', val) and cell.column not in (
                col_chuantong, col_baoxian, col_guanli):
            ref_col_data = cell.column
        # 已有的月折标列（排除刚插入的新列）
        if re.search(r'年\d+月折标', val.replace('\n', '')) and cell.column != col_month_zb:
            ref_col_month = cell.column

    # 都找不到时降级用 zheubiao_col（G列）
    if ref_col_data is None:
        ref_col_data = zheubiao_col
    if ref_col_month is None:
        ref_col_month = zheubiao_col

    log_func(f"  样式参考列: 数值列={get_column_letter(ref_col_data)}, 月折标列={get_column_letter(ref_col_month)}")

    # ── 填入数据 + 同步样式 ──
    matched = 0
    unmatched = []
    for row_idx, name in data_rows:
        val_ct = source_data[name].get(f'传统{month_str}', 0) if name in source_data else 0
        val_bx = source_data[name].get(f'保险{month_str}', 0) if name in source_data else 0
        val_gl = source_data[name].get(f'管理费{month_str}', 0) if name in source_data else 0

        # 写入值
        ws.cell(row=row_idx, column=col_chuantong).value = val_ct
        ws.cell(row=row_idx, column=col_baoxian).value   = val_bx
        ws.cell(row=row_idx, column=col_guanli).value    = val_gl
        ws.cell(row=row_idx, column=col_month_zb).value  = f'=SUM({lc}{row_idx}:{lg}{row_idx})'

        # 复制样式（从已有同类列取格式）
        apply_data_cell_style(ws, row_idx, col_chuantong, ref_col_data)
        apply_data_cell_style(ws, row_idx, col_baoxian,   ref_col_data)
        apply_data_cell_style(ws, row_idx, col_guanli,    ref_col_data)
        apply_data_cell_style(ws, row_idx, col_month_zb,  ref_col_month)

        if name in source_data:
            matched += 1
        else:
            unmatched.append(name)

    log_func(f"  匹配成功: {matched} 人，未匹配(填0): {len(unmatched)} 人")
    if unmatched:
        log_func(f"  未匹配理财师: {unmatched}")

    # ── 扫描团队分组结构 ──
    # 结构：团队首行（A列=团队名，B列=理财师名）
    #       后续行（A列=None，B列=理财师名）
    #       合计行（A列="合计" 或合并成"合计"）
    # 我们构建 group_list = [(first_data_row, last_data_row, sum_row), ...]
    group_list = []   # (first_data_row, last_data_row, sum_row)
    cur_start = None
    cur_end   = None
    for row_idx in range(4, ws.max_row + 1):
        a_val = ws.cell(row_idx, 1).value
        b_val = ws.cell(row_idx, 2).value
        is_sum = a_val and '合计' in str(a_val)
        has_name = b_val and isinstance(b_val, str) and b_val.strip() and not is_sum

        if is_sum:
            if cur_start is not None:
                group_list.append((cur_start, cur_end, row_idx))
            cur_start = None
            cur_end   = None
        elif has_name:
            if cur_start is None:
                cur_start = row_idx
            cur_end = row_idx

    # ── 1. 合计行新增列填 SUM 公式 + 样式 ──
    lc_h = get_column_letter(col_chuantong)
    lc_i = get_column_letter(col_baoxian)
    lc_j = get_column_letter(col_guanli)
    lc_k = get_column_letter(col_month_zb)
    for (fr, lr, sr) in group_list:
        if lr >= sr:
            log_func(f"  [WARN] 跳过错误的SUM范围: fr={fr}, lr={lr}, sr={sr}, 公式范围={lc_h}{fr}:{lc_h}{lr}")
            continue
        ws.cell(sr, col_chuantong).value = f'=SUM({lc_h}{fr}:{lc_h}{lr})'
        ws.cell(sr, col_baoxian  ).value = f'=SUM({lc_i}{fr}:{lc_i}{lr})'
        ws.cell(sr, col_guanli   ).value = f'=SUM({lc_j}{fr}:{lc_j}{lr})'
        ws.cell(sr, col_month_zb ).value = f'=SUM({lc_k}{fr}:{lc_k}{lr})'
    log_func(f"  已更新 {len(group_list)} 个合计行的新列 SUM 公式")

    # ── 补齐其他所有行（合计行、空行）的新列样式 ──
    data_row_set = {r for r, _ in data_rows}
    sum_row_set  = {sr for _, _, sr in group_list}
    for row_idx in range(3, ws.max_row + 1):
        if row_idx in data_row_set:
            continue  # 数据行已处理过
        for col_idx, ref_c in [(col_chuantong, ref_col_data), (col_baoxian, ref_col_data),
                               (col_guanli, ref_col_data), (col_month_zb, ref_col_month)]:
            apply_data_cell_style(ws, row_idx, col_idx, ref_c)

    # ── 修正新增列的对齐方式（数值列：右对齐；合计行：居中或与参考一致）──
    # 读取参考列（ref_col_data）的数据行对齐方式
    ref_align_data  = None
    ref_align_month = None
    if data_rows:
        ref_r = data_rows[0][0]
        ref_cell_d = ws.cell(ref_r, ref_col_data)
        ref_cell_m = ws.cell(ref_r, ref_col_month)
        if ref_cell_d.alignment:
            ref_align_data  = copy.copy(ref_cell_d.alignment)
        if ref_cell_m.alignment:
            ref_align_month = copy.copy(ref_cell_m.alignment)

    for row_idx, _ in data_rows:
        for col_idx, ref_al in [(col_chuantong, ref_align_data),
                                (col_baoxian,   ref_align_data),
                                (col_guanli,    ref_align_data),
                                (col_month_zb,  ref_align_month)]:
            if ref_al:
                ws.cell(row_idx, col_idx).alignment = copy.copy(ref_al)

    for (fr, lr, sr) in group_list:
        for col_idx, ref_al in [(col_chuantong, ref_align_data),
                                (col_baoxian,   ref_align_data),
                                (col_guanli,    ref_align_data),
                                (col_month_zb,  ref_align_month)]:
            if ref_al:
                ws.cell(sr, col_idx).alignment = copy.copy(ref_al)

    # ── 2. 更新考核期间已完成折标（G列） ──
    # 逻辑：G列 = 上一个周度sheet中同一理财师的G值 + 本次新月折标（col_month_zb）
    # "上一个周度sheet"由调用者通过 prev_ws 传入（见 update_sheet 参数）
    # 这里用 prev_g_data（dict {姓名: G值}）实现
    if prev_g_data:
        for row_idx, name in data_rows:
            prev_g = prev_g_data.get(name)
            if prev_g is not None:
                # G列 = 上期G值（数值）+ 本期月折标（公式引用）
                prev_g_val = prev_g if isinstance(prev_g, (int, float)) else 0
                ws.cell(row_idx, zheubiao_col).value = (
                    f'={prev_g_val}+{lc_k}{row_idx}'
                )
        # 同步合计行 G 列
        for (fr, lr, sr) in group_list:
            lc_g = get_column_letter(zheubiao_col)
            ws.cell(sr, zheubiao_col).value = f'=SUM({lc_g}{fr}:{lc_g}{lr})'
        log_func(f"  已更新 G列（考核期间已完成折标）= 上期值 + 本月折标")

    # ── 更新 YTD 列公式（含合计行）──
    # 重新扫描所有本年月折标列（插入后列号可能变化）
    year_zb_cols_new = []
    for cell in ws[2]:
        if cell.value and isinstance(cell.value, str):
            v = cell.value.replace('\n', '')
            if re.search(rf'{year_str}年\d+月折标', v) or re.search(rf'{year_str}\d{{2}}折标', v):
                year_zb_cols_new.append(cell.column)

    if ytd_col:
        lc_ytd = get_column_letter(ytd_col)
        for row_idx, name in data_rows:
            if year_zb_cols_new:
                refs = '+'.join([f'{get_column_letter(c)}{row_idx}' for c in year_zb_cols_new])
                ws.cell(row_idx, ytd_col).value = f'=SUM({refs})'
        # 合计行 YTD
        for (fr, lr, sr) in group_list:
            refs = '+'.join([f'{get_column_letter(c)}{sr}' for c in year_zb_cols_new])
            ws.cell(sr, ytd_col).value = f'=SUM({refs})'
        log_func(f"  已更新 YTD 列公式（含{len(year_zb_cols_new)}个月份）")

    # ── 更新 D/E/F 列（考核起止日 + 任务目标）+ 月完成率公式（动态月数）──
    if all_data:
        _update_kaohe_def_cols(ws, data_rows, all_data, col_month_zb, log_func,
                               group_list=group_list)
    elif completion_col and task_target_col:
        # 兜底：all_data 未传入时，沿用旧逻辑（用 F 列 / 动态 D/E 计算月数）
        lm_col = get_column_letter(col_month_zb)
        lt_col = get_column_letter(task_target_col)
        # 找 D/E 列
        d_col2 = e_col2 = None
        for cell in ws[2]:
            if not cell.value:
                continue
            v = str(cell.value).replace('\n', '').strip()
            if v == '考核期起始日':
                d_col2 = cell.column
            elif v == '考核期终止日':
                e_col2 = cell.column
        for row_idx, name in data_rows:
            task_val = ws.cell(row_idx, task_target_col).value
            if not task_val:
                continue
            if d_col2 and e_col2:
                ld2 = get_column_letter(d_col2)
                le2 = get_column_letter(e_col2)
                months_f = (f'(YEAR({le2}{row_idx})*12+MONTH({le2}{row_idx}))'
                            f'-(YEAR({ld2}{row_idx})*12+MONTH({ld2}{row_idx}))+1')
                ws.cell(row_idx, completion_col).value = (
                    f'={lm_col}{row_idx}/({lt_col}{row_idx}/({months_f}))'
                )
            else:
                ws.cell(row_idx, completion_col).value = (
                    f'={lm_col}{row_idx}/({lt_col}{row_idx}/6)'
                )
        log_func(f"  已更新月完成率公式")

    # ── 3. 统一重建所有合并和公式 ──
    _rebuild_all_merges_and_formulas(ws, date_str, log_func)

    # ── 4. 修复 Row2:Row3 表头合并 + 更新进度模块 ──
    _fix_header_row23_merges(ws, log_func)
    _update_progress_module(ws, date_str, log_func)

    # ── 5. 隐藏往月传统/保险/管理费列 ──
    _hide_old_month_data_cols(ws, month_str, log_func)

    # ── 复制模板列宽（新建路径：从最近模板sheet复制列宽）──
    wb_target = ws.parent
    template_sheet_for_width = None
    latest_date_w = 0
    for sname in wb_target.sheetnames:
        m = re.match(r'周度团队数据-折标\+考核(\d{4})', sname)
        if m and sname != ws.title:
            d = int(m.group(1))
            if d > latest_date_w:
                latest_date_w = d
                template_sheet_for_width = sname
    if template_sheet_for_width:
        ws_tpl = wb_target[template_sheet_for_width]
        for col_letter, dim in ws_tpl.column_dimensions.items():
            if col_letter in ws.column_dimensions:
                ws.column_dimensions[col_letter].width = dim.width
            else:
                ws.column_dimensions[col_letter] = copy.copy(dim)
        log_func(f"  已复制模板 '{template_sheet_for_width}' 列宽")

    return True


def process_file(source_file, target_file, log_func=print, keep_other_team=True):
    """
    主处理函数：
    1. 从源文件(汇总表&明细)提取日期和数据
    2. 在目标文件(传统客户数据分层)新建/更新周度sheet
    3. 保存目标文件
    """
    # 提取日期
    date_str = extract_date_from_filename(source_file)
    if not date_str:
        raise ValueError(f"无法从文件名中提取日期: {os.path.basename(source_file)}\n"
                         f"文件名应包含 '截止YYYYMMDD' 格式，如：新嘉理财师考核数据_截止20260313-汇总表&明细.xlsx")

    log_func(f"📅 检测到数据日期: {date_str} ({date_str[:4]}年{date_str[4:6]}月{date_str[6:]}日)")

    # 读取源数据（当月 + 全量历史）
    log_func(f"\n📂 正在读取源文件: {os.path.basename(source_file)}")
    source_data, field_cols, all_data, all_months = load_source_data(source_file, date_str)
    log_func(f"✅ 读取成功，共 {len(source_data)} 位理财师，源文件含历史月份: {sorted(all_months)}")

    # 新sheet名称
    sheet_suffix = get_sheet_suffix(date_str)  # 0313
    new_sheet_name = f'周度团队数据-折标+考核{sheet_suffix}'
    log_func(f"\n📋 目标Sheet名称: {new_sheet_name}")

    # 加载目标文件
    log_func(f"📂 正在加载目标文件: {os.path.basename(target_file)}")
    wb = load_workbook(target_file)

    # 检查sheet是否已存在
    if new_sheet_name in wb.sheetnames:
        log_func(f"⚠️  Sheet '{new_sheet_name}' 已存在，将在现有sheet上更新数据...")
        ws_new = wb[new_sheet_name]
    else:
        # 找到最近的一个"周度团队数据-折标+考核"sheet作为模板
        template_sheet = None
        latest_date = 0
        for sname in wb.sheetnames:
            m = re.match(r'周度团队数据-折标\+考核(\d{4})', sname)
            if m:
                d = int(m.group(1))
                if d > latest_date:
                    latest_date = d
                    template_sheet = sname

        if template_sheet is None:
            raise ValueError("目标文件中未找到任何'周度团队数据-折标+考核'格式的sheet，无法复制模板。")

        log_func(f"📋 以 '{template_sheet}' 为模板复制新sheet...")

        # 复制sheet（深度复制）
        ws_template = wb[template_sheet]
        ws_new = wb.copy_worksheet(ws_template)
        ws_new.title = new_sheet_name

        # 复制模板的列宽到新sheet（copy_worksheet 不复制 column_dimensions）
        for col_letter, dim in ws_template.column_dimensions.items():
            if col_letter in ws_new.column_dimensions:
                ws_new.column_dimensions[col_letter].width = dim.width
            else:
                ws_new.column_dimensions[col_letter] = copy.copy(dim)
        log_func(f"  已复制模板列宽（{len(ws_template.column_dimensions)} 列）")

        # 将新sheet移动到模板sheet之后
        template_idx = wb.sheetnames.index(template_sheet)
        wb.move_sheet(new_sheet_name, offset=template_idx + 1 - wb.sheetnames.index(new_sheet_name))

        # 更新第1行标题（日期部分）
        for cell in ws_new[1]:
            if cell.value and isinstance(cell.value, str) and re.search(r'截止\d{8}', cell.value):
                cell.value = re.sub(r'截止\d{8}', f'截止{date_str}', cell.value)
                log_func(f"  已更新标题: {cell.value}")
                break

        log_func(f"✅ 新Sheet '{new_sheet_name}' 创建成功")

    # 填入数据
    log_func(f"\n🔄 正在填入数据...")

    # 读取上一个周度sheet的 G列（考核期间已完成折标）作为 prev_g_data
    prev_g_data = {}
    prev_sheets = []
    for sname in wb.sheetnames:
        m = re.match(r'周度团队数据-折标\+考核(\d{4})', sname)
        if m and sname != new_sheet_name:
            prev_sheets.append((int(m.group(1)), sname))
    if prev_sheets:
        prev_sheets.sort(key=lambda x: x[0], reverse=True)
        prev_sheet_name = prev_sheets[0][1]
        ws_prev = wb[prev_sheet_name]
        # 找G列（考核期间已完成折标）列号
        prev_g_col = None
        for cell in ws_prev[2]:
            if cell.value and '考核期间已完成折标' in str(cell.value):
                prev_g_col = cell.column
                break
        # 找B列理财师名，读G列数值
        if prev_g_col:
            for row_idx in range(4, ws_prev.max_row + 1):
                a_val = ws_prev.cell(row_idx, 1).value
                b_val = ws_prev.cell(row_idx, 2).value
                if a_val and '合计' in str(a_val):
                    continue
                if b_val and isinstance(b_val, str) and b_val.strip():
                    g_val = ws_prev.cell(row_idx, prev_g_col).value
                    if g_val is not None:
                        try:
                            prev_g_data[b_val.strip()] = float(g_val)
                        except (ValueError, TypeError):
                            prev_g_data[b_val.strip()] = 0
        log_func(f"  读取上一周期 '{prev_sheet_name}' 的考核折标数据，共 {len(prev_g_data)} 人")

    success = update_sheet(ws_new, source_data, date_str, log_func,
                           prev_g_data=prev_g_data if prev_g_data else None,
                           all_data=all_data, keep_other_team=keep_other_team,
                           all_months=all_months)

    if not success:
        log_func("⚠️  数据已存在，未作更改。")
        return False

    # ── 回溯更新：在新建 sheet 内，用最新汇总数据覆盖所有历史月份列（跳过当月，已由 update_sheet 处理）──
    month_str_current = get_month_str(date_str)
    log_func(f"\n🔁 开始回溯更新新 sheet 中的历史月份列（跳过当月 {month_str_current}）...")
    retro_update_sheet(ws_new, all_data, log_func, skip_month=month_str_current, all_months=all_months)

    # ── 下半年考核模块（7~12月目标按考核期月数比例拆分）──
    log_func(f"\n🧩 生成下半年考核模块...")
    try:
        _refresh_h2_zb_col(ws_new, date_str, log_func)
        _add_h2_module(ws_new, date_str, log_func)
        _refresh_gap_cols(ws_new, date_str, log_func)
        _unify_number_formats(ws_new, date_str, log_func)
    except Exception as e:
        import traceback
        log_func(f"  ⚠️ 下半年模块生成失败: {e}")
        log_func(traceback.format_exc())

    # 保存为新文件（不覆盖原文件）
    # 新文件名：原文件名_更新_YYYYMMDD.xlsx
    base_name_target, ext_target = os.path.splitext(target_file)
    output_file = f"{base_name_target}_更新_{date_str}{ext_target}"
    
    log_func(f"\n💾 正在保存新文件...")
    wb.save(output_file)
    log_func(f"✅ 已保存：{os.path.basename(output_file)}")
    log_func(f"\n🎉 处理完成！新文件已生成，请打开 Excel 查看 '{new_sheet_name}' sheet。")
    return True


# ─────────────────────── 考核数据拆分功能 ───────────────────────

def _copy_cell_style_split(source_cell, target_cell):
    """复制单元格样式（字体/边框/填充/对齐/数字格式）"""
    if source_cell.has_style:
        target_cell._style     = copy.copy(source_cell._style)
        target_cell.number_format = source_cell.number_format
        target_cell.font       = copy.copy(source_cell.font)
        target_cell.border     = copy.copy(source_cell.border)
        target_cell.fill       = copy.copy(source_cell.fill)
        target_cell.alignment  = copy.copy(source_cell.alignment)


def split_kaohe_by_advisor(file_path, log_func=print):
    """
    按理财师拆分考核数据文件（汇总表&明细）。

    逻辑：
    1. 以 '汇总-折标规模' sheet 的 '理财师' 列为准，枚举所有理财师。
    2. 遍历文件所有 sheet：
       - 若该 sheet 有 '理财师' 列 → 只保留该理财师的行。
       - 若没有 '理财师' 列 → 整个 sheet 原样复制（公共表）。
    3. 每个理财师生成一个 xlsx，保存在源文件同级目录的子文件夹中。
    4. 完整保留值和单元格格式（字体/颜色/边框/对齐）。
    """
    from openpyxl import load_workbook, Workbook
    from openpyxl.cell import MergedCell as _MC_sp
    from openpyxl.utils import get_column_letter

    log_func(f"  加载文件: {os.path.basename(file_path)}", "dim")

    # 加载工作簿（data_only=True 读计算后的值）
    wb = load_workbook(file_path, data_only=True)

    MAIN_SHEET = '汇总-折标规模'
    if MAIN_SHEET not in wb.sheetnames:
        raise ValueError(f"未找到工作表 '{MAIN_SHEET}'，请确认文件格式正确。")

    # ── 从主 sheet 读取理财师列表 ──
    ws_main = wb[MAIN_SHEET]

    # 找"理财师"列号（在第1行找表头）
    advisor_col_idx = None
    for cell in ws_main[1]:
        if isinstance(cell, _MC_sp):
            continue
        if cell.value and str(cell.value).strip() == '理财师':
            advisor_col_idx = cell.column
            break

    if advisor_col_idx is None:
        raise ValueError(f"在 '{MAIN_SHEET}' 第1行未找到 '理财师' 列。")

    # 收集所有唯一理财师（保序）
    seen = set()
    unique_advisors = []
    for row in ws_main.iter_rows(min_row=2, values_only=True):
        val = row[advisor_col_idx - 1]
        if val and str(val).strip() and str(val).strip() not in seen:
            seen.add(str(val).strip())
            unique_advisors.append(str(val).strip())

    if not unique_advisors:
        raise ValueError(f"'{MAIN_SHEET}' 中未找到任何理财师数据。")

    log_func(f"  发现 {len(unique_advisors)} 位理财师: {', '.join(unique_advisors)}", "info")

    # ── 预扫描每个 sheet 的"理财师"列及各行数据 ──
    # 结构：sheet_meta[sheet_name] = {'advisor_col': int|None, 'rows': [row_tuple,...], 'header': row_tuple}
    sheet_meta = {}
    for sname in wb.sheetnames:
        ws = wb[sname]
        # 读表头（第1行）
        header = [cell.value for cell in ws[1] if not isinstance(cell, _MC_sp)]
        # 但要按列号顺序读，包括 MergedCell（填 None）
        header_full = []
        for cell in ws[1]:
            header_full.append(None if isinstance(cell, _MC_sp) else cell.value)
        max_col = ws.max_column

        # 找"理财师"列
        adv_col = None
        for cidx, v in enumerate(header_full, start=1):
            if v and str(v).strip() == '理财师':
                adv_col = cidx
                break

        # 读所有数据行（保留行号）
        rows = []
        for ridx, row in enumerate(ws.iter_rows(min_row=2, values_only=False), start=2):
            row_vals = []
            for cell in row:
                row_vals.append(None if isinstance(cell, _MC_sp) else cell.value)
            rows.append((ridx, row_vals))

        sheet_meta[sname] = {
            'advisor_col': adv_col,
            'header': header_full,
            'rows': rows,
            'max_col': max_col,
        }

    # ── 输出目录 ──
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    output_dir = os.path.join(os.path.dirname(file_path), f"{base_name}_拆分结果")
    os.makedirs(output_dir, exist_ok=True)
    log_func(f"  输出目录: {output_dir}", "dim")

    # ── 日期后缀（从文件名提取，或用今天）──
    m = re.search(r'(\d{8})', os.path.basename(file_path))
    if m:
        raw = m.group(1)   # YYYYMMDD
        date_suffix = raw[4:]  # MMDD
    else:
        date_suffix = datetime.now().strftime("%m%d")

    # ── 为每位理财师生成文件 ──
    for advisor in unique_advisors:
        out_path = os.path.join(output_dir, f"{advisor}_{date_suffix}.xlsx")
        new_wb = Workbook()
        # 删除默认 Sheet
        for sname in new_wb.sheetnames:
            del new_wb[sname]

        for sname in wb.sheetnames:
            meta = sheet_meta[sname]
            ws_src = wb[sname]
            new_ws = new_wb.create_sheet(title=sname)
            max_col = meta['max_col']

            # 确定要写哪些行
            if meta['advisor_col'] is not None:
                adv_col_0 = meta['advisor_col'] - 1  # 0-based
                selected_rows = [
                    (ridx, rvals) for (ridx, rvals) in meta['rows']
                    if rvals[adv_col_0] is not None and str(rvals[adv_col_0]).strip() == advisor
                ]
            else:
                # 无理财师列，整张表复制
                selected_rows = meta['rows']

            # 写表头（第1行）+ 格式
            for cidx in range(1, max_col + 1):
                src_cell = ws_src.cell(1, cidx)
                if isinstance(src_cell, _MC_sp):
                    continue
                new_ws.cell(1, cidx, value=src_cell.value)
                _copy_cell_style_split(src_cell, new_ws.cell(1, cidx))

            # 写数据行 + 格式
            for new_ridx, (orig_ridx, rvals) in enumerate(selected_rows, start=2):
                for cidx in range(1, max_col + 1):
                    val = rvals[cidx - 1] if cidx - 1 < len(rvals) else None
                    new_ws.cell(new_ridx, cidx, value=val)
                    src_cell = ws_src.cell(orig_ridx, cidx)
                    if not isinstance(src_cell, _MC_sp):
                        _copy_cell_style_split(src_cell, new_ws.cell(new_ridx, cidx))

            # 复制列宽
            for cidx in range(1, max_col + 1):
                col_letter = get_column_letter(cidx)
                if col_letter in ws_src.column_dimensions:
                    new_ws.column_dimensions[col_letter].width = \
                        ws_src.column_dimensions[col_letter].width

        new_wb.save(out_path)
        log_func(f"  ✅ {advisor} → {os.path.basename(out_path)}", "ok")

    wb.close()
    log_func(f"  拆分完成，共 {len(unique_advisors)} 个文件 → {output_dir}", "info")
    return output_dir, len(unique_advisors)


# ─────────────────────── GUI ───────────────────────

class App((tk.Tk if tk else object)):
    def __init__(self):
        super().__init__()
        self.title(f"新嘉理财师 · 周度数据自动更新工具 {VERSION}")
        self.geometry("780x780")
        self.resizable(True, True)
        self.configure(bg="#f5f5f5")

        self._build_ui()

    def _build_ui(self):
        # ── 顶部标题 ──
        title_frame = tk.Frame(self, bg="#2c5fa8", pady=12)
        title_frame.pack(fill="x")
        tk.Label(title_frame, text="新嘉理财师  周度数据自动更新工具",
                 font=("Microsoft YaHei", 16, "bold"),
                 fg="white", bg="#2c5fa8").pack()
        tk.Label(title_frame, text="自动从汇总表&明细 提取数据 → 更新传统客户数据分层",
                 font=("Microsoft YaHei", 9),
                 fg="#c8d8f0", bg="#2c5fa8").pack()

        # ── 主内容区 ──
        main = tk.Frame(self, bg="#f5f5f5", padx=20, pady=15)
        main.pack(fill="both", expand=True)

        # 源文件
        self._make_file_row(main, "📊  汇总表&明细  (源文件):",
                            "支持多个文件，将依次按日期处理",
                            self._choose_source, "source_label",
                            hint="例：新嘉理财师考核数据_截止20260313-汇总表&明细.xlsx",
                            multi=True)

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=8)

        # 目标文件
        self._make_file_row(main, "📁  传统客户数据分层  (目标文件):",
                            "数据将写入此文件",
                            self._choose_target, "target_label",
                            hint="例：传统客户数据分层.xlsx",
                            multi=False)

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=8)

        # ── 保留其他团队 勾选项 ──
        opt_frame = tk.Frame(main, bg="#f5f5f5")
        opt_frame.pack(fill="x", pady=(0, 8))
        self.keep_other_team_var = tk.BooleanVar(value=True)
        self.keep_other_team_cb = tk.Checkbutton(
            opt_frame, text="保留其他团队（无团队长的理财师）",
            variable=self.keep_other_team_var,
            font=("Microsoft YaHei", 10),
            bg="#f5f5f5", activebackground="#f5f5f5",
            cursor="hand2"
        )
        self.keep_other_team_cb.pack(side="left")

        # ── 执行按钮（周报更新）──
        btn_frame = tk.Frame(main, bg="#f5f5f5")
        btn_frame.pack(fill="x", pady=(5, 0))

        self.run_btn = tk.Button(
            btn_frame, text="▶  开始更新",
            font=("Microsoft YaHei", 13, "bold"),
            bg="#2c5fa8", fg="white", activebackground="#1e4080",
            relief="flat", padx=30, pady=8,
            cursor="hand2",
            command=self._run
        )
        self.run_btn.pack(side="left")

        self.clear_btn = tk.Button(
            btn_frame, text="清空日志",
            font=("Microsoft YaHei", 10),
            bg="#e0e0e0", fg="#333", activebackground="#cccccc",
            relief="flat", padx=16, pady=8,
            cursor="hand2",
            command=self._clear_log
        )
        self.clear_btn.pack(side="left", padx=(10, 0))

        # ══════════════════════════════════════════════
        # ── 考核数据拆分功能区 ──
        sep2_frame = tk.Frame(main, bg="#f5f5f5")
        sep2_frame.pack(fill="x", pady=(16, 4))
        tk.Frame(sep2_frame, bg="#cccccc", height=1).pack(fill="x", side="left", expand=True)
        tk.Label(sep2_frame, text="  考核数据拆分  ",
                 font=("Microsoft YaHei", 9), bg="#f5f5f5", fg="#888").pack(side="left")
        tk.Frame(sep2_frame, bg="#cccccc", height=1).pack(fill="x", side="left", expand=True)

        self._make_file_row(main,
            "✂️  汇总表&明细  (拆分源文件):",
            "按理财师拆分，每人生成一个独立 Excel",
            self._choose_split_source, "split_source_label",
            hint="例：新嘉理财师考核数据_截止20260313-汇总表&明细.xlsx",
            multi=False)

        split_btn_frame = tk.Frame(main, bg="#f5f5f5")
        split_btn_frame.pack(fill="x", pady=(6, 0))

        self.split_btn = tk.Button(
            split_btn_frame, text="✂️  开始拆分",
            font=("Microsoft YaHei", 13, "bold"),
            bg="#1a7a4a", fg="white", activebackground="#145c38",
            relief="flat", padx=30, pady=8,
            cursor="hand2",
            command=self._run_split
        )
        self.split_btn.pack(side="left")

        # 日志区
        log_frame = tk.Frame(main, bg="#f5f5f5")
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        tk.Label(log_frame, text="执行日志", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f5f5", fg="#555").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            log_frame, font=("Consolas", 10), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", relief="flat", state="disabled",
            wrap="word"
        )
        self.log_box.pack(fill="both", expand=True)

        # 配色tag
        self.log_box.tag_config("ok",    foreground="#4ec94e")
        self.log_box.tag_config("warn",  foreground="#f0c040")
        self.log_box.tag_config("error", foreground="#f44747")
        self.log_box.tag_config("info",  foreground="#9cdcfe")
        self.log_box.tag_config("dim",   foreground="#888888")

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = tk.Label(self, textvariable=self.status_var,
                               font=("Microsoft YaHei", 9),
                               bg="#d0d0d0", fg="#333", anchor="w", padx=10)
        status_bar.pack(fill="x", side="bottom")

        # 内部变量
        self.source_files = []
        self.target_file = ""
        self.split_source_file = ""  # 考核数据拆分：源文件

    def _make_file_row(self, parent, label_text, sub_text, cmd, attr_name,
                       hint="", multi=False):
        row = tk.Frame(parent, bg="#f5f5f5")
        row.pack(fill="x", pady=4)

        tk.Label(row, text=label_text, font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f5f5", fg="#222").pack(anchor="w")
        tk.Label(row, text=sub_text, font=("Microsoft YaHei", 8),
                 bg="#f5f5f5", fg="#888").pack(anchor="w")

        inner = tk.Frame(row, bg="#f5f5f5")
        inner.pack(fill="x", pady=(3, 0))

        lbl = tk.Label(inner, text=hint if hint else "（未选择）",
                       font=("Microsoft YaHei", 9),
                       bg="white", fg="#aaa" if hint else "#333",
                       relief="solid", borderwidth=1,
                       anchor="w", padx=8, pady=5)
        lbl.pack(side="left", fill="x", expand=True)

        setattr(self, attr_name, lbl)

        btn = tk.Button(inner, text="选择文件" + ("(可多选)" if multi else ""),
                        font=("Microsoft YaHei", 9),
                        bg="#e8edf5", fg="#2c5fa8",
                        relief="flat", padx=10, pady=5,
                        cursor="hand2",
                        command=cmd)
        btn.pack(side="left", padx=(6, 0))

    def _choose_source(self):
        files = filedialog.askopenfilenames(
            title="选择汇总表&明细文件（可多选）",
            filetypes=[("Excel文件", "*.xlsx *.xls"), ("所有文件", "*.*")]
        )
        if files:
            self.source_files = list(files)
            names = [os.path.basename(f) for f in files]
            self.source_label.config(
                text="\n".join(names), fg="#222"
            )
            self._log(f"已选择 {len(files)} 个源文件: {', '.join(names)}", "info")

    def _choose_target(self):
        f = filedialog.askopenfilename(
            title="选择传统客户数据分层文件",
            filetypes=[("Excel文件", "*.xlsx *.xls"), ("所有文件", "*.*")]
        )
        if f:
            self.target_file = f
            self.target_label.config(text=os.path.basename(f), fg="#222")
            self._log(f"已选择目标文件: {os.path.basename(f)}", "info")

    def _choose_split_source(self):
        f = filedialog.askopenfilename(
            title="选择考核数据汇总表&明细文件",
            filetypes=[("Excel文件", "*.xlsx *.xls"), ("所有文件", "*.*")]
        )
        if f:
            self.split_source_file = f
            self.split_source_label.config(text=os.path.basename(f), fg="#222")
            self._log(f"已选择拆分源文件: {os.path.basename(f)}", "info")

    def _run_split(self):
        if not self.split_source_file:
            messagebox.showwarning("提示", "请先选择要拆分的汇总表&明细文件！")
            return
        self.split_btn.config(state="disabled", text="⏳  拆分中...")
        self.status_var.set("拆分处理中，请稍候...")
        threading.Thread(target=self._run_split_thread, daemon=True).start()

    def _run_split_thread(self):
        try:
            self._log(f"\n{'='*50}", "dim")
            self._log(f"开始拆分: {os.path.basename(self.split_source_file)}", "info")
            self._log(f"{'='*50}", "dim")
            output_dir, count = split_kaohe_by_advisor(self.split_source_file, self._log)
            self._log(f"{'='*50}", "dim")
            self._log(f"✅ 拆分完成！共生成 {count} 个文件", "ok")
            self._log(f"📁 输出目录: {output_dir}", "info")
            self._log(f"{'='*50}", "dim")
            self.after(0, lambda: self.status_var.set(f"拆分完成！共 {count} 个文件"))
            self.after(0, lambda: messagebox.showinfo(
                "拆分完成",
                f"✅ 拆分完成！共生成 {count} 个文件\n\n📁 输出目录:\n{output_dir}"
            ))
        except Exception as e:
            self._log(f"❌ 拆分失败: {e}", "error")
            import traceback
            self._log(traceback.format_exc(), "error")
            self.after(0, lambda: self.status_var.set("拆分失败！"))
            self.after(0, lambda: messagebox.showerror("拆分错误", str(e)))
        finally:
            self.after(0, lambda: self.split_btn.config(state="normal", text="✂️  开始拆分"))

    def _run(self):
        if not self.source_files:
            messagebox.showwarning("提示", "请先选择汇总表&明细源文件！")
            return
        if not self.target_file:
            messagebox.showwarning("提示", "请先选择传统客户数据分层目标文件！")
            return

        self.run_btn.config(state="disabled", text="⏳  处理中...")
        self.status_var.set("处理中，请稍候...")
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self):
        try:
            # 对多个源文件，按日期排序后依次处理
            files_with_dates = []
            for f in self.source_files:
                d = extract_date_from_filename(f)
                if d:
                    files_with_dates.append((d, f))
                else:
                    self._log(f"⚠️  无法识别日期，跳过: {os.path.basename(f)}", "warn")

            files_with_dates.sort(key=lambda x: x[0])
            self._log(f"\n{'='*50}", "dim")
            self._log(f"开始处理，共 {len(files_with_dates)} 个文件", "info")
            self._log(f"{'='*50}\n", "dim")

            success_count = 0
            for i, (date_str, src_file) in enumerate(files_with_dates):
                self._log(f"[{i+1}/{len(files_with_dates)}] 处理: {os.path.basename(src_file)}", "info")
                try:
                    result = process_file(src_file, self.target_file, self._log,
                                          keep_other_team=self.keep_other_team_var.get())
                    if result:
                        success_count += 1
                except Exception as e:
                    import traceback as _tb
                    self._log(f"❌ 处理失败: {e}", "error")
                    self._log(_tb.format_exc(), "error")
                self._log("", "dim")

            self._log(f"{'='*50}", "dim")
            self._log(f"✅ 全部完成！成功处理 {success_count}/{len(files_with_dates)} 个文件", "ok")
            self._log(f"{'='*50}", "dim")
            self.after(0, lambda: self.status_var.set(
                f"完成！成功处理 {success_count}/{len(files_with_dates)} 个文件"))
            self.after(0, lambda: messagebox.showinfo(
                "完成",
                f"处理完成！\n成功: {success_count}/{len(files_with_dates)} 个文件\n\n"
                f"请打开 Excel 查看结果。"
            ))
        except Exception as e:
            self._log(f"❌ 发生错误: {e}", "error")
            import traceback
            self._log(traceback.format_exc(), "error")
            self.after(0, lambda: self.status_var.set("处理失败！"))
            self.after(0, lambda: messagebox.showerror("错误", str(e)))
        finally:
            self.after(0, lambda: self.run_btn.config(state="normal", text="▶  开始更新"))

    def _log(self, text, tag="dim"):
        def _write():
            self.log_box.config(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            if tag in ("ok", "error", "warn", "info"):
                self.log_box.insert("end", f"[{ts}] {text}\n", tag)
            else:
                self.log_box.insert("end", f"{text}\n", tag)
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, _write)

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")


# ─────────────────────── 入口 ───────────────────────

if __name__ == "__main__":
    if tk is None:
        raise SystemExit("未安装 tkinter。请改用网页：python 网页服务.py")
    app = App()
    app.mainloop()
