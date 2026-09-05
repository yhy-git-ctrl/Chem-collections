"""合并重复文献：#5 -> #6（B1 方案）。

策略：
- 保留 #6 的主信息（DOI/年份/英文标题等元数据）。
- reactions 以 #6 为基底，补入 #5 独有的反应（去重），重排 reaction_id。
- 图片保留 #6 的一套（id 15-28），删除 #5 的图（id 1-14）及其磁盘文件。
- tokens 用合并后的文章重新生成（保留 #6 并补 #5 独有分词）。
- 删除 #5 记录及其 reactions/tokens/images 记录、对应源文件。

用 --apply 才真正执行；否则仅 dry-run 打印预览。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List

import jieba

from . import config
from .db import _get_conn

KEEP = 6
DROP = 5


import re


def _norm_product_name(name: str) -> str:
    """归一化产物名：去掉括号内注释、空格，统一"脱保护/脱苄基"等措辞。"""
    if not name:
        return ""
    s = str(name)
    # 去掉括号内的修饰（含中文括号）
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"（[^）]*）", "", s)
    # 统一同义词
    s = s.replace("脱苄基", "脱保护").replace("脱苯甲酰基", "脱保护")
    s = s.replace("相应醇", "对应醇")
    s = re.sub(r"\s+", "", s)
    return s


def _norm_reaction(r: Dict[str, Any]) -> str:
    """生成反应去重键：归一化产物名 + 收率。"""
    products = (r.get("products") or [])
    pd = "＋".join(sorted(_norm_product_name(x.get("name", ""))
                          for x in products if x.get("name")))
    yield_ = re.sub(r"\s+", "", str(r.get("yield") or r.get("yield_") or ""))
    return f"{pd}>>{yield_}"


def _reaction_has_detail(r: Dict[str, Any]) -> int:
    """打分：信息越全分越高，用于去重时保留信息更全的一条。"""
    score = 0
    if r.get("name"):
        score += 2
    if r.get("conditions_text"):
        score += 2
    if r.get("reagent"):
        score += 1
    if r.get("yield"):
        score += 1
    if r.get("solvent"):
        score += 1
    return score


def _load(article_id: int) -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT extracted_json, title, journal, year FROM articles WHERE id=?",
            (article_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"未找到文章 #{article_id}")
    return {"json": json.loads(row["extracted_json"]),
            "meta": {"title": row["title"], "journal": row["journal"],
                     "year": row["year"]}}


def _merge_reactions(keepex: Dict[str, Any], dropex: Dict[str, Any]) -> List[Dict[str, Any]]:
    keep = keepex.get("reactions") or []
    drop = dropex.get("reactions") or []
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for r in keep + drop:
        key = _norm_reaction(r)
        if not key:
            continue
        if key not in best:
            best[key] = r
            order.append(key)
        elif _reaction_has_detail(r) > _reaction_has_detail(best[key]):
            best[key] = r
    # 保留原始先后顺序（先 keep 后 drop补充），并重排 reaction_id
    merged = [dict(best[k]) for k in order]
    for i, r in enumerate(merged):
        r["reaction_id"] = f"R{i+1:02d}"
        r["name"] = r.get("name") or ""
        r["type"] = r.get("type") or ""
    return merged


def _regen_tokens(ex: Dict[str, Any]) -> List[str]:
    """从合并后的 extracted 平文重新分词。"""
    parts = []
    art = ex.get("article") or {}
    for k in ("title", "authors", "abstract"):
        if art.get(k):
            parts.append(art[k])
    for r in ex.get("reactions") or []:
        for k in ("name", "reagent", "solvent", "temperature", "time",
                  "conditions_text", "mechanism", "notes", "yield"):
            v = r.get(k)
            if v:
                parts.append(str(v))
        for arr in ("reactants", "products"):
            for x in r.get(arr) or []:
                if x.get("name"):
                    parts.append(x["name"])
    mech = ex.get("mechanism") or {}
    if mech.get("overall"):
        parts.append(mech["overall"])
    scope = ex.get("substrate_scope") or {}
    if scope.get("summary"):
        parts.append(scope["summary"])
    kw = ex.get("keywords") or {}
    if kw.get("reactions"):
        parts.extend(kw["reactions"])
    if kw.get("tags"):
        parts.extend(kw["tags"])
    text = " ".join(parts)
    return list(dict.fromkeys(t for t in jieba.cut(text) if t.strip()))


@dataclass
class Plan:
    merged: List[Dict[str, Any]]
    tokens: List[str]
    drop_image_ids: List[int]
    drop_image_files: List[str]


def build_plan() -> Plan:
    keepex = _load(KEEP)["json"]
    dropex = _load(DROP)["json"]
    merged = _merge_reactions(keepex, dropex)
    # 合并后的完整 extracted（主信息用 #6）
    merged_json = dict(keepex)
    merged_json["reactions"] = merged
    tokens = _regen_tokens(merged_json)

    drop_image_ids: List[int] = []
    drop_image_files: List[str] = []
    with _get_conn() as conn:
        for row in conn.execute(
            "SELECT id, path FROM images WHERE article_id=? ORDER BY id", (DROP,)
        ):
            drop_image_ids.append(row["id"])
            drop_image_files.append(row["path"])
    return Plan(merged=merged, tokens=tokens,
                drop_image_ids=drop_image_ids, drop_image_files=drop_image_files)


def apply() -> None:
    plan = build_plan()
    keepex = _load(KEEP)["json"]
    merged_json = dict(keepex)
    merged_json["reactions"] = plan.merged

    with _get_conn() as conn:
        # 1) 更新 #6 extracted_json
        conn.execute(
            "UPDATE articles SET extracted_json=? WHERE id=?",
            (json.dumps(merged_json, ensure_ascii=False, indent=2), KEEP),
        )
        # 2) 清空并重写 #6 的 reactions
        conn.execute("DELETE FROM reactions WHERE article_id=?", (KEEP,))
        for r in plan.merged:
            _insert_reaction(conn, KEEP, r)
        # 3) 重建 #6 tokens
        conn.execute("DELETE FROM tokens WHERE article_id=?", (KEEP,))
        for t in plan.tokens:
            conn.execute("INSERT INTO tokens(term, article_id) VALUES(?,?)",
                         (t, KEEP))
        # 4) 删除 #5 的所有记录（reactions/tokens/images/整行）
        conn.execute("DELETE FROM reactions WHERE article_id=?", (DROP,))
        conn.execute("DELETE FROM tokens WHERE article_id=?", (DROP,))
        conn.execute("DELETE FROM images WHERE article_id=?", (DROP,))
        conn.execute("DELETE FROM compounds WHERE article_id=?", (DROP,))
        conn.execute("DELETE FROM articles WHERE id=?", (DROP,))
        conn.commit()

    # 5) 删除 #5 的图片文件
    for name in plan.drop_image_files:
        p = config.IMAGES_DIR / name
        if p.exists():
            p.unlink()
    # 注：#5 的源文件（wechat html / text）保留在磁盘作为存档，不删除。
    print("合并完成：删除 #5，保留 #6。")


def _insert_reaction(conn, article_id: int, r: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO reactions(article_id, reaction_id, name, type,
           reactant_json, product_json, reagent, solvent, temperature,
           time, yield_text, conditions_text, mechanism, notes, source_image)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            article_id, r.get("reaction_id", ""), r.get("name", ""),
            r.get("type", ""),
            json.dumps(r.get("reactants") or [], ensure_ascii=False),
            json.dumps(r.get("products") or [], ensure_ascii=False),
            "|".join(r.get("reagent") or []) if isinstance(r.get("reagent"), list)
            else (r.get("reagent") or ""),
            "|".join(r.get("solvent") or []) if isinstance(r.get("solvent"), list)
            else (r.get("solvent") or ""),
            r.get("temperature", ""), r.get("time", ""), r.get("yield", ""),
            r.get("conditions_text", ""), r.get("mechanism", ""),
            r.get("notes", ""), r.get("source_image", ""),
        ),
    )


def main() -> None:
    plan = build_plan()
    print("===== 预演：合并后 reactions（去重后）=====")
    for r in plan.merged:
        names = lambda arr: "+".join(x.get("name", "") for x in (arr or []))
        print(f"- {r.get('name') or '(无命名)'}: {names(r.get('reactants'))} -> {names(r.get('products'))}")
    print(f"\ntokens 数量（合并后）: {len(plan.tokens)}")
    print(f"删除的 #5 图片记录 id: {plan.drop_image_ids}")
    print(f"删除的 #5 图片文件: {plan.drop_image_files}")


if __name__ == "__main__":
    if "--apply" in sys.argv:
        apply()
    else:
        main()
