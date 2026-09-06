"""SQLite 存储：文章、反应、结构、图片、分词索引；含关键词与结构检索。"""

from __future__ import annotations

import json
import pickle
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import jieba
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem

from . import config
from .models import ExtractedArticle, Reaction, StructureRef

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT, source_type TEXT, source_url TEXT,
  journal TEXT, year TEXT, volume TEXT, issue TEXT, pages TEXT, doi TEXT,
  authors TEXT, abstract TEXT,
  raw_path TEXT, text_path TEXT, extracted_json TEXT,
  created_at TEXT, card_no TEXT, category TEXT
);
CREATE TABLE IF NOT EXISTS reactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER, reaction_id TEXT, name TEXT, type TEXT,
  reactant_json TEXT, product_json TEXT,
  reagent TEXT, solvent TEXT, temperature TEXT, time TEXT, yield_text TEXT,
  conditions_text TEXT, mechanism TEXT, notes TEXT, source_image TEXT,
  FOREIGN KEY(article_id) REFERENCES articles(id)
);
CREATE TABLE IF NOT EXISTS compounds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER, name TEXT, smiles TEXT, canonical_smiles TEXT, role TEXT,
  morgan_fp BLOB, molblock TEXT,
  FOREIGN KEY(article_id) REFERENCES articles(id)
);
CREATE TABLE IF NOT EXISTS images(
  id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER, path TEXT, caption TEXT,
  role TEXT,
  review_status TEXT,
  FOREIGN KEY(article_id) REFERENCES articles(id)
);
CREATE TABLE IF NOT EXISTS tokens(
  term TEXT, article_id INTEGER
);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens ON tokens(term, article_id);
CREATE INDEX IF NOT EXISTS idx_compounds_article ON compounds(article_id);
CREATE INDEX IF NOT EXISTS idx_compounds_smiles ON compounds(canonical_smiles);
"""


def _get_conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """为已存在的库补列（card_no / category）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    for col in ("card_no", "category"):
        if col not in cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} TEXT")
    img_cols = {r[1] for r in conn.execute("PRAGMA table_info(images)")}
    if "role" not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN role TEXT")
    if "review_status" not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN review_status TEXT")
    # 旧数据回填：没有编号/分类的文章按 id 顺序补
    rows = conn.execute(
        "SELECT id, extracted_json FROM articles "
        "WHERE card_no IS NULL OR card_no='' ORDER BY id").fetchall()
    for r in rows:
        card_no = "KC-" + str(_next_card_seq(conn)).zfill(4)
        cat = ""
        if r["extracted_json"]:
            try:
                from .classify import classify
                cat = classify(r["extracted_json"])
            except Exception:
                cat = ""
        conn.execute("UPDATE articles SET card_no=?, category=? WHERE id=?",
                     (card_no, cat, r["id"]))
    conn.commit()


def _next_card_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='card_seq'").fetchone()
    n = (int(row["value"]) if row and row["value"] else 0) + 1
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('card_seq',?) "
        "ON CONFLICT(key) DO UPDATE SET value=?",
        (str(n), str(n)),
    )
    return n


def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000，。；：、（）()【】\[\]/·—\-]+", "", (s or "").lower())


def find_duplicate(source_url: str = "", doi: str = "", title: str = "",
                   journal: str = "", year: str = "", volume: str = "",
                   issue: str = "", pages: str = "") -> Dict[str, Any]:
    """判断是否重复收录，返回已存在文章信息或空。

    判定优先级（命中即判重）：
    1) DOI 相同（文献唯一标识，最可靠）
    2) 无 DOI 时，按期刊信息判重：期刊 + 年份 相同；
       若年份缺失，则期刊 + (卷/期/页 至少一个) 相同。
    """
    with _get_conn() as conn:
        # 1) DOI
        do_ = (doi or "").strip()
        if do_:
            r = conn.execute(
                "SELECT id, card_no, title, journal, year, doi, source_url, created_at "
                "FROM articles WHERE doi=? AND doi<>'' ORDER BY id DESC LIMIT 1",
                (do_,),
            ).fetchone()
            if r:
                return dict(r)

        j = (journal or "").strip()
        y = (year or "").strip()
        v = (volume or "").strip()
        i = (issue or "").strip()
        p = (pages or "").strip()
        if j:
            rows = conn.execute(
                "SELECT id, card_no, title, journal, year, volume, issue, pages, "
                "doi, source_url, created_at FROM articles WHERE journal=? AND journal<>''",
                (j,),
            ).fetchall()
            for r in rows:
                ry = (r["year"] or "").strip()
                rv = (r["volume"] or "").strip()
                ri = (r["issue"] or "").strip()
                rp = (r["pages"] or "").strip()
                # 期刊相同前提下：年份相同，或年份缺失但卷/期/页至少一个相同
                if (y and ry and y == ry) or (
                    y and not ry and ((v and v == rv) or (i and i == ri) or (p and p == rp))
                ):
                    return dict(r)

        # 3) 兜底：标题规范化后互相包含（避免漏判同文异链）
        nt = _norm(title)
        if nt:
            rows = conn.execute(
                "SELECT id, card_no, title, journal, year, doi, source_url, created_at "
                "FROM articles WHERE title<>''").fetchall()
            for r in rows:
                rt = _norm(r["title"])
                if rt and (rt in nt or nt in rt):
                    return dict(r)
    return {}


def _canonical(smiles: str) -> Tuple[str, str]:
    """返回 (canonical_smiles, molblock)。无效 SMILES 返回 ('','')。"""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return "", ""
    canon = Chem.MolToSmiles(mol)
    block = Chem.MolToMolBlock(mol)
    return canon, block


def _storable_dicts(objs: Iterable[StructureRef]) -> List[Dict[str, str]]:
    return [o.model_dump(exclude_none=True) for o in objs]


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens = [t.strip().lower() for t in jieba.cut_for_search(text)]
    return [t for t in tokens if t and len(t) > 1]


def _flat_text(extracted: ExtractedArticle) -> str:
    parts = [
        extracted.article.title,
        extracted.article.authors,
        extracted.article.journal,
        extracted.article.year,
        extracted.article.volume,
        extracted.article.issue,
        extracted.article.pages,
        extracted.article.doi,
        extracted.article.source_url,
        extracted.article.abstract,
        " ".join(extracted.keywords.compounds),
        " ".join(extracted.keywords.reactions),
        " ".join(extracted.keywords.tags),
        extracted.mechanism.overall,
        extracted.substrate_scope.summary,
    ]
    for r in extracted.reactions:
        parts += [r.name, r.type, r.conditions_text, " ".join(r.reagent),
                  " ".join(r.solvent), r.temperature, r.time, r.mechanism, r.notes]
    return " ".join(p for p in parts if p)


def store_article(extracted: ExtractedArticle, raw_path: str = "",
                  text_path: str = "", card_no: str = "", category: str = "") -> int:
    """把抽取结果写库，返回文章 id。"""
    init_db()
    art = extracted.article
    now = datetime.now().isoformat(timespec="seconds")
    with _get_conn() as conn:
        if not card_no:
            card_no = "KC-" + str(_next_card_seq(conn)).zfill(4)
        cur = conn.execute(
            """INSERT INTO articles(title, source_type, source_url, journal, year,
               volume, issue, pages, doi, authors, abstract, raw_path, text_path,
               extracted_json, created_at, card_no, category)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                art.title, art.source_type, art.source_url, art.journal,
                art.year, art.volume, art.issue, art.pages, art.doi,
                art.authors, art.abstract, raw_path, text_path,
                extracted.to_json(), now, card_no, category,
            ),
        )
        article_id = cur.lastrowid

        for r in extracted.reactions:
            conn.execute(
                """INSERT INTO reactions(article_id, reaction_id, name, type,
                   reactant_json, product_json, reagent, solvent, temperature,
                   time, yield_text, conditions_text, mechanism, notes, source_image)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    article_id, r.reaction_id, r.name, r.type,
                    json.dumps(_storable_dicts(r.reactants), ensure_ascii=False),
                    json.dumps(_storable_dicts(r.products), ensure_ascii=False),
                    "|".join(r.reagent), "|".join(r.solvent), r.temperature,
                    r.time, r.yield_, r.conditions_text, r.mechanism, r.notes,
                    r.source_image,
                ),
            )

        comps = collect_compounds(extracted)
        for c in comps:
            canon, block = _canonical(c["smiles"]) if c.get("smiles") else ("", "")
            if not canon:
                continue
            fp = AllChem.GetMorganFingerprintAsBitVect(
                Chem.MolFromSmiles(canon), 2, nBits=2048
            )
            conn.execute(
                """INSERT INTO compounds(article_id, name, smiles, canonical_smiles,
                   role, morgan_fp, molblock) VALUES(?,?,?,?,?,?,?)""",
                (article_id, c["name"], c.get("smiles", ""), canon, c.get("role", ""),
                 pickle.dumps(fp), block),
            )

        text = _flat_text(extracted)
        for term in set(_tokenize(text)):
            conn.execute("INSERT INTO tokens(term, article_id) VALUES(?,?)",
                         (term, article_id))
        return article_id


def collect_compounds(extracted: ExtractedArticle) -> List[Dict[str, str]]:
    comps: List[Dict[str, str]] = []
    for r in extracted.reactions:
        for s in r.reactants:
            comps.append({"name": s.name, "smiles": s.smiles, "role": "reactant"})
        for s in r.products:
            comps.append({"name": s.name, "smiles": s.smiles, "role": "product"})
    for name in extracted.keywords.compounds:
        comps.append({"name": name, "smiles": "", "role": "keyword"})
    return comps


def insert_images(article_id: int, image_paths: List[str],
                  roles: List[str] | None = None,
                  review_status: List[str] | None = None) -> None:
    with _get_conn() as conn:
        for i, p in enumerate(image_paths):
            role = (roles[i] if roles and i < len(roles) else "") or ""
            rv = (review_status[i] if review_status and i < len(review_status) else "") or ""
            conn.execute("INSERT INTO images(article_id, path, role, review_status) VALUES(?,?,?,?)",
                         (article_id, p, role, rv))


def set_image_role(image_id: int, role: str) -> None:
    with _get_conn() as conn:
        conn.execute("UPDATE images SET role=?, review_status='manual' WHERE id=?", (role, image_id))
        conn.commit()


def list_articles() -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id,title,journal,year,source_type,source_url,created_at,card_no,category "
            "FROM articles ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_article(article_id: int) -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if row is None:
            return {}
        art = dict(row)
        art["extracted"] = ExtractedArticle.from_json(art.pop("extracted_json"))
        art["reactions"] = [dict(r) for r in conn.execute(
            "SELECT * FROM reactions WHERE article_id=? ORDER BY id", (article_id,))]
        art["compounds"] = [dict(c) for c in conn.execute(
            "SELECT id,name,smiles,canonical_smiles,role,molblock FROM compounds "
            "WHERE article_id=? ORDER BY id", (article_id,))]
        art["images"] = [dict(i) for i in conn.execute(
            "SELECT id,path,caption,role,review_status FROM images WHERE article_id=? ORDER BY id",
            (article_id,))]
    return art


def get_compound(compound_id: int) -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, article_id, name, smiles, canonical_smiles, role, molblock "
            "FROM compounds WHERE id=?", (compound_id,)).fetchone()
    return dict(row) if row else {}


def get_card_info(article_id: int) -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, card_no, category, title, journal, created_at "
            "FROM articles WHERE id=?", (article_id,)).fetchone()
    return dict(row) if row else {}


def keyword_search(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """用 jieba 分词后在 tokens 索引里匹配，按命中数排序。"""
    terms = _tokenize(query)
    if not terms:
        return []
    placeholders = ",".join("?" for _ in terms)
    with _get_conn() as conn:
        rows = conn.execute(
            f"""SELECT a.id, a.title, a.journal, a.year, a.source_type, a.source_url,
                       a.created_at, COUNT(t.term) AS hits
                FROM tokens t JOIN articles a ON a.id=t.article_id
                WHERE t.term IN ({placeholders})
                GROUP BY a.id ORDER BY hits DESC LIMIT ?""",
            (*terms, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def structure_search(smiles: str, limit: int = 50) -> List[Dict[str, Any]]:
    """子结构检索 + Tanimoto 相似度排序。"""
    qmol = Chem.MolFromSmiles(smiles)
    if qmol is None:
        return []
    qfp = AllChem.GetMorganFingerprintAsBitVect(qmol, 2, nBits=2048)
    results: List[Dict[str, Any]] = []
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, article_id, name, canonical_smiles, role, morgan_fp "
            "FROM compounds").fetchall()
        for r in rows:
            canon = r["canonical_smiles"]
            mol = Chem.MolFromSmiles(canon)
            if mol is None:
                continue
            fp = pickle.loads(r["morgan_fp"])
            sim = DataStructs.TanimotoSimilarity(qfp, fp)
            if mol.HasSubstructMatch(qmol) or sim >= 0.5:
                results.append({
                    "compound_id": r["id"],
                    "article_id": r["article_id"],
                    "name": r["name"],
                    "canonical_smiles": canon,
                    "role": r["role"],
                    "similarity": round(sim, 3),
                    "substructure_match": mol.HasSubstructMatch(qmol),
                })
    results.sort(key=lambda x: (not x["substructure_match"], -x["similarity"]))
    return results[:limit]


def get_stats() -> Dict[str, Any]:
    """返回仪表盘所需的聚合统计。"""
    from pathlib import Path

    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        reactions = conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]
        images = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        compounds = conn.execute("SELECT COUNT(*) FROM compounds").fetchone()[0]
        categories = [
            {"category": r["category"], "count": r["n"]}
            for r in conn.execute(
                "SELECT category, COUNT(*) n FROM articles GROUP BY category ORDER BY n DESC"
            )
        ]
        source_types = [
            {"source_type": r["source_type"], "count": r["n"]}
            for r in conn.execute(
                "SELECT source_type, COUNT(*) n FROM articles GROUP BY source_type ORDER BY n DESC"
            )
        ]
        latest = [
            {"id": r["id"], "card_no": r["card_no"], "title": r["title"],
             "category": r["category"], "created_at": r["created_at"]}
            for r in conn.execute(
                "SELECT id, card_no, title, category, created_at FROM articles "
                "ORDER BY id DESC LIMIT 5"
            )
        ]

    def _count_files(d: Path) -> int:
        try:
            return sum(1 for _ in d.glob("*") if _.is_file())
        except OSError:
            return 0

    def _dir_size(d: Path) -> int:
        try:
            return sum(p.stat().st_size for p in d.glob("*") if p.is_file())
        except OSError:
            return 0

    return {
        "total": total,
        "reactions": reactions,
        "images": images,
        "compounds": compounds,
        "categories": categories,
        "source_types": source_types,
        "latest": latest,
        "dirs": {
            "data_dir": str(config.DATA_DIR),
            "articles_dir": str(config.ARTICLES_DIR),
            "images_dir": str(config.IMAGES_DIR),
            "db_path": str(config.DB_PATH),
            "articles_file_count": _count_files(config.ARTICLES_DIR),
            "images_file_count": _count_files(config.IMAGES_DIR),
            "articles_size": _dir_size(config.ARTICLES_DIR),
            "images_size": _dir_size(config.IMAGES_DIR),
            "db_size": _dir_size(config.DATA_DIR),
        },
    }
    return results
