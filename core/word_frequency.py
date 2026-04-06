"""
BNC/COCA 词频数据统一加载模块

将 BNC_COCA_lists.csv 词频表加载为两种视图：
1. word_to_level: 所有词形 → 等级字符串 (如 "abilities" → "1k")
   供 web_app 的 /api/word-levels 接口使用
2. word_to_level_num: 所有词形 → 等级数字 (如 "abilities" → 1)
   供 step2 的词频过滤使用
3. level_to_num_map: "1k" → 1, "25k" → 25 用于前端展示

整个应用共享同一份数据，避免重复加载和实现差异。
"""
import csv
import os
import re

from core import config

# 模块级缓存
_word_to_level: dict = {}         # word_form -> "1k", "3k", ...
_word_to_level_num: dict = {}     # word_form -> 1, 3, ...
_level_to_num_map: dict = {}      # "1k" -> 1, "25k" -> 25
_loaded = False


def _load():
    """加载 BNC/COCA 词频表（仅执行一次）"""
    global _word_to_level, _word_to_level_num, _level_to_num_map, _loaded
    if _loaded:
        return

    csv_path = os.path.join(config.BASE_DIR, "BNC_COCA_lists.csv")
    if not os.path.exists(csv_path):
        print(f"[WordFreq] 警告: BNC_COCA_lists.csv 未找到: {csv_path}")
        _loaded = True
        return

    headword_count = 0
    form_count = 0

    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # 跳过表头
            for row in reader:
                if len(row) < 3:
                    continue
                level_str = row[0].strip()       # e.g. "1k", "25k"
                headword = row[1].strip().lower()
                related_forms_str = row[2]        # e.g. "a (2186984), an (338269)"

                if not level_str or not headword:
                    continue

                # 解析等级编号
                num_match = re.match(r"(\d+)k", level_str)
                if not num_match:
                    continue
                level_num = int(num_match.group(1))
                _level_to_num_map[level_str] = level_num

                # 提取所有词形
                forms = re.findall(r"([a-zA-Z'-]+)\s*\(\d+\)", related_forms_str)
                all_forms = {f.lower().strip() for f in forms}
                all_forms.add(headword)

                for form in all_forms:
                    if not form:
                        continue
                    # 如果同一词形出现多次，保留最低（最常见）的级别
                    if form not in _word_to_level_num or level_num < _word_to_level_num[form]:
                        _word_to_level[form] = level_str
                        _word_to_level_num[form] = level_num
                        form_count += 1

                headword_count += 1

        print(f"📖 BNC/COCA 词频表已加载: {form_count} 个词形, "
              f"{headword_count} 个词头, {len(_level_to_num_map)} 个等级")
    except Exception as e:
        print(f"[WordFreq] 加载 BNC_COCA_lists.csv 失败: {e}")

    _loaded = True


def get_word_level_str(word: str) -> str | None:
    """
    获取词的等级字符串，如 "1k", "3k"。
    不在词表中返回 None。
    """
    _load()
    return _word_to_level.get(word.lower().strip())


def get_word_level_num(word: str) -> int:
    """
    获取词的等级数字，如 1, 3。
    不在词表中返回 999。
    """
    _load()
    return _word_to_level_num.get(word.lower().strip(), 999)


def get_word_to_level() -> dict:
    """获取完整的 word -> level_str 映射（供 web API 使用）"""
    _load()
    return _word_to_level


def get_level_to_num_map() -> dict:
    """获取 level_str -> num 映射（供前端展示使用）"""
    _load()
    return _level_to_num_map
