"""按默认化学知识分类标准自动为卡片归类。"""

from __future__ import annotations

import re
from typing import List

TAXONOMY: List[str] = [
    "氧化", "还原", "偶联", "取代与官能团转化", "加成与环加成",
    "消除与重排", "缩合与成环", "保护与脱保护", "金属有机",
    "杂元素化学", "新型反应", "机理研究", "其它",
]

RULES: List[tuple] = [
    ("偶联", r"suzuki|sonogashira|buchwald|stille|ullmann|negishi|偶联|coupling"),
    ("保护与脱保护", r"脱保护|deprotect|保护基|苄基|对甲氧基苄基|pmb|boc|cbz|fmo|benzyl"),
    ("氧化", r"氧化|氧化剂|tempo|azado|pifa|pida|ddq|can|oxidation|氧化脱"),
    ("还原", r"还原|氢化|氢解|还原胺化|reduction|hydrogenation|负氢还原"),
    ("环加成", r"环加成|diels|cycloaddition|\[4\+2\]|\[2\+2\]"),
    ("加成", r"加成|迈克尔|michael|addition|氢化"),
    ("金属有机", r"格氏|grignard|有机锂|organolithium|羰基化|转金属化|插羰|metalation"),
    ("缩合与成环", r"酰胺|酯化|缩合|多组分|ugi|passerini|amide|ester|酰化"),
    ("重排", r"重排|rearrangement|curtius|beckmann|hoffmann|lossen|cris"),
    ("取代与官能团转化", r"取代|卤化|硝化|磺化|重氮化|氟代|氯化|取代"),
    ("杂元素化学", r"氟化学|磷化学|硫化学|硼|硅|有机氟"),
    ("新型反应", r"光催化|电化学|流动化学|photoredox|electrochem|flow chem|光化学"),
]


def classify(text: str) -> str:
    """从抽取文本（JSON/平文）里识别化学知识分类。"""
    s = (text or "").lower()
    for cat, pat in RULES:
        if re.search(pat, s, re.IGNORECASE):
            return cat
    return "其它"
