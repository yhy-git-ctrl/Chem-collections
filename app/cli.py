"""命令行入口：add / list / show / keywords / structure / selftest。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config
from .db import (
    get_article, init_db, keyword_search, list_articles,
    store_article, structure_search,
)
from .extract import extract
from .ingest import ingest
from .models import (
    ArticleMeta, ExtractedArticle, Keywords, Mechanism, Reaction,
    StructureRef, SubstrateScope,
)


def _print_article_table() -> None:
    arts = list_articles()
    if not arts:
        print("（库为空）")
        return
    print(f"{'ID':<4} {'来源':<6} {'标题'}")
    for a in arts:
        print(f"{a['id']:<4} {a['source_type']:<6} {a['title'][:60]}")


def cmd_add(source: str) -> None:
    init_db()
    data = ingest(source)
    ex = extract(data["meta"], data["text"], data["images"])
    article_id = store_article(ex, data["raw_path"], data["text_path"])
    print(f"已入库 #{article_id}：{ex.article.title}")
    print(ex.to_json())


def cmd_show(article_id: int) -> None:
    art = get_article(article_id)
    if not art:
        print("未找到")
        return
    print(art["extracted"].to_json())


def cmd_keywords(query: str) -> None:
    for r in keyword_search(query):
        print(f"#{r['id']} 命中{r['hits']} | {r['title'][:60]}")


def cmd_structure(smiles: str) -> None:
    for r in structure_search(smiles):
        print(f"文章#{r['article_id']} 结构#{r['compound_id']} "
              f"sim={r['similarity']} sub={r['substructure_match']} | {r['name']}")


def _selftest_html() -> str:
    return """<!DOCTYPE html><html><head><title>有机化学速递：Diels-Alder 反应示例</title></head>
<body><article><h1>有机化学速递：一个经典的 Diels-Alder 反应</h1>
<div id="js_content">
<p>本文介绍一个代表性环加成反应。二烯与亲双烯体在二氯甲烷中回流，
得到目标桥环产物。相关结构 SMILES：c1ccccc1 与 CC(=O)c1ccccc1。</p>
<p>适用底物范围：多种取代烯烃与共轭二烯；反应时间 12 小时，温度 40 摄氏度。</p>
</div></article></body></html>"""


def _demo_extracted() -> ExtractedArticle:
    return ExtractedArticle(
        article=ArticleMeta(
            title="示例：Diels-Alder 反应",
            source_type="wechat", source_url="https://mp.weixin.qq.com/s/demo",
            journal="示例期刊", year="2026",
        ),
        reactions=[
            Reaction(
                name="苯乙烯与环戊二烯环加成",
                type="cycloaddition",
                reactants=[StructureRef(name="苯乙烯", smiles="C=Cc1ccccc1",
                                        role="reactant")],
                products=[StructureRef(name="桥环产物", smiles="C1=CC2CC1CC2")],
                solvent=["二氯甲烷"], temperature="40℃", time="12 h",
                conditions_text="DCM, 40℃, 12 h",
            )
        ],
        mechanism=Mechanism(overall="经协同 [4+2] 环加成形成桥环骨架。"),
        substrate_scope=SubstrateScope(summary="适用于多种共轭二烯与亲双烯体。"),
        keywords=Keywords(compounds=["苯乙烯", "环戊二烯"], reactions=["Diels-Alder"],
                          tags=["环加成"]),
    )


def cmd_selftest() -> None:
    init_db()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "demo.html"
        p.write_text(_selftest_html(), encoding="utf-8")
        data = ingest(str(p))
        ex = extract(data["meta"], data["text"], data["images"])
        aid = store_article(ex, data["raw_path"], data["text_path"])
        print(f"[1] 文件解析入库：# {aid} 标题={ex.article.title}")

    ex2 = _demo_extracted()
    aid2 = store_article(ex2)
    print(f"[2] 含结构入库：# {aid2} 反应数={len(ex2.reactions)}")

    print("\n[3] 关键词检索 'Diels':")
    cmd_keywords("Diels")
    print("\n[4] 结构检索 'c1ccccc1'（苯环）:")
    cmd_structure("c1ccccc1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="文献收集")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("source", help="网址 / PDF 路径 / 文本")

    sub.add_parser("list")
    p_show = sub.add_parser("show")
    p_show.add_argument("id", type=int)

    p_kw = sub.add_parser("keywords")
    p_kw.add_argument("query")
    p_st = sub.add_parser("structure")
    p_st.add_argument("smiles")

    sub.add_parser("selftest")
    return parser


def main(argv=None) -> None:
    config.ensure_dirs()
    args = build_parser().parse_args(argv)
    if args.cmd == "add":
        cmd_add(args.source)
    elif args.cmd == "list":
        _print_article_table()
    elif args.cmd == "show":
        cmd_show(args.id)
    elif args.cmd == "keywords":
        cmd_keywords(args.query)
    elif args.cmd == "structure":
        cmd_structure(args.smiles)
    elif args.cmd == "selftest":
        cmd_selftest()


if __name__ == "__main__":
    main()
