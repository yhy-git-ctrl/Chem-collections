"""重新抽取一篇已有文章；先由调用方完成数据库备份。"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from app.classify import classify
from app.extract import extract
from app.models import ArticleMeta
from app.db import _storable_dicts


def main(article_id: int) -> None:
    root = Path(__file__).resolve().parents[1]
    db_path = root / "data" / "library.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT title,source_type,source_url,text_path FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        raise SystemExit(f"article {article_id} not found")
    title, source_type, source_url, text_path = row
    text = Path(text_path).read_text(encoding="utf-8", errors="ignore")
    meta = ArticleMeta(title=title, source_type=source_type, source_url=source_url,
                       retrieved_at=datetime.now().isoformat(timespec="seconds"))
    ex = extract(meta, text, [])
    category = classify(ex.to_json())
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE articles SET title=?,source_type=?,source_url=?,journal=?,year=?,volume=?,issue=?,pages=?,doi=?,authors=?,abstract=?,extracted_json=?,category=? WHERE id=?",
                     (ex.article.title or title, ex.article.source_type or source_type,
                      ex.article.source_url or source_url, ex.article.journal, ex.article.year,
                      ex.article.volume, ex.article.issue, ex.article.pages, ex.article.doi,
                      ex.article.authors, ex.article.abstract, ex.to_json(), category, article_id))
        # 保持关系表与 extracted_json 同步；旧脚本只更新 articles，
        # 会导致详情接口仍显示上一轮的 reactions。
        conn.execute("DELETE FROM reactions WHERE article_id=?", (article_id,))
        for r in ex.reactions:
            conn.execute(
                """INSERT INTO reactions(article_id,reaction_id,name,type,reactant_json,
                   product_json,reagent,solvent,temperature,time,yield_text,conditions_text,
                   mechanism,notes,source_image) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article_id, r.reaction_id, r.name, r.type,
                 json.dumps(_storable_dicts(r.reactants), ensure_ascii=False),
                 json.dumps(_storable_dicts(r.products), ensure_ascii=False),
                 "|".join(r.reagent), "|".join(r.solvent), r.temperature, r.time,
                 r.yield_, r.conditions_text, r.mechanism, r.notes, r.source_image),
            )
        conn.commit()
    print(json.dumps({"id": article_id, "journal": ex.article.journal, "year": ex.article.year,
                      "doi": ex.article.doi, "reactions": len(ex.reactions),
                      "mechanism": bool(ex.mechanism.overall), "category": category}, ensure_ascii=False))


if __name__ == "__main__":
    main(int(sys.argv[1]))
