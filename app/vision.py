"""多模态模型给文章配图分类：reaction/scope/mechanism/conditions/other。"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import List, Optional, Tuple

from . import llm
import logging

CATS = ("reaction", "scope", "mechanism", "conditions", "other")
PROMPT = (
    "这是一篇有机化学文章的配图。判断它最可能属于哪一类，只回答一个英文词："
    "reaction(反应式/主反应通式)、scope(底物适用范围/底物范围表)、"
    "mechanism(反应机理图)、conditions(反应条件优化/筛选)、other(其它/封面/示意图/无关)。"
)


def classify_image(path: str) -> str:
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(path)[0] or "image/png"
    url = f"data:{mime};base64," + base64.b64encode(data).decode()
    # 不同提供商可能返回空内容；允许一次重试，预算由模型配置决定。
    for attempt in range(2):
        try:
            content = llm.complete("vision", [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": url}}]}]).strip().lower()
        except ValueError:
            if attempt == 1:
                raise
            continue
        if content:
            for c in CATS:
                if c in content:
                    return c
    return "other"


def tag_images(paths: List[str]) -> List[Tuple[str, str]]:
    """对每张图分类，返回 [(path, role)]；失败时 role='other'。"""
    out: List[Tuple[str, str]] = []
    for p in paths:
        try:
            role = classify_image(p)
        except Exception as exc:
            logging.getLogger(__name__).warning("图片分类未完成 (%s)", type(exc).__name__)
            role = "other"
        out.append((p, role))
    return out
# ---------- 内容审核（多模态） ----------
_REGION_LABELS = {
    "reaction": "主反应通式/典型反应式",
    "scope": "底物适用范围（底物范围/底物范围表）",
    "mechanism": "反应机理图",
}
REVIEW_PROMPT = (
    "这是一篇有机化学论文配图中的一张图。请判断这张图是否属于“{region}”。"
    "只回答一行 JSON：{{\"match\": true 或 false, \"reason\": \"简短原因\"}}。"
    "如果无法断定，返回 {{\"match\": false, \"reason\": \"无法判断\"}}。"
)


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def review_image(path: str, region: str) -> Tuple[Optional[bool], str]:
    """审核某张候选图是否属于指定区域。返回 (match, reason)；失败时 match=None。"""
    label = _REGION_LABELS.get(region, region)
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(path)[0] or "image/png"
    url = f"data:{mime};base64," + base64.b64encode(data).decode()
    prompt = REVIEW_PROMPT.format(region=label)
    for attempt in range(2):
        try:
            content = llm.complete("vision", [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}}]}]).strip()
        except ValueError:
            if attempt == 1:
                break
            continue
        if not content:
            continue
        try:
            obj = json.loads(_extract_json(content))
            if isinstance(obj, dict) and "match" in obj:
                return bool(obj["match"]), str(obj.get("reason", ""))
        except Exception:
            low = content.lower()
            if "true" in low:
                return True, ""
            if "false" in low:
                return False, ""
    return None, "review_failed"
