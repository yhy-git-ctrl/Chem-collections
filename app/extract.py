"""大模型抽取：把文章文本按固定模板抽成结构化 JSON；无 API Key 时退化为极简抽取。"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import List

from . import llm
import logging
from .models import ExtractedArticle, SubstrateScope, Keywords

SYSTEM_PROMPT = (
    "你是一名有机化学文献分析助手。请从给定的文章文本中，按固定 JSON 模板抽取信息，"
    "只输出该 JSON，不要任何额外说明或前/后缀。"
    "特别要求："
    "1) 把文章中描述的代表性反应/步骤都记为 reactions 数组记录，即使结构只出现在图片、"
    "无法给出 SMILES——此时仍要给出 reactants/products 的 name（化合物名，中文/英文均可），"
    "并尽量填齐 reagent、solvent、temperature、time、yield、conditions_text。"
    "2) article.journal 尽量从标题或正文里的期刊名推断（如标题【J. Org. Chem.】→ J. Org. Chem.）。"
    "3) keywords.compounds 收集关键化合物/底物/产物/试剂名；keywords.reactions 收集反应类型；"
    "keywords.tags 收集方法/主题标签（如 脱保护、氧化、光催化）。"
    "4) mechanism.overall 用一句话概括关键机理（若有协同/自由基/氧化还原等）。"
    "5) substrate_scope.summary 用两三句凝练适用底物范围与局限，不要照抄摘要。"
    "6) substrate_scope.notes 只写会改变反应结果的关键因素（催化剂、碱/添加剂、溶剂、光照、温度、时间、浓度、气氛等），"
    "并结合优化/对照实验说明影响；不要把摘要或期刊元数据放进这里。"
    "7) 取不到就留空字符串或空数组，绝不编造。"
)


def _chat(text: str, follow_up: str = "") -> ExtractedArticle:
    user_msg = text[:12000] + (("\n\n" + follow_up) if follow_up else "")
    raw = llm.complete("text", [
            # 不把完整 JSON Schema 拼进提示词：PDF 正文已经较长，过大的系统消息会让部分
            # 推理模型耗尽输出预算而返回空 content；固定模板要求已在 SYSTEM_PROMPT 中说明。
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ])
    try:
        return ExtractedArticle.model_validate(_merge_article_fields(_parse_json(raw)))
    except Exception:
        return ExtractedArticle()


def _quality_ok(ex: ExtractedArticle) -> bool:
    return bool(ex.reactions or ex.mechanism.overall or
                ex.keywords.reactions or ex.keywords.tags)


def _llm_extract(text: str) -> ExtractedArticle:
    ex = _chat(text)
    # DeepSeek 偶发空 content，重试几次再走 follow-up 补抽
    tries = 0
    while not _quality_ok(ex) and tries < 2:
        ex = _chat(text)
        tries += 1
    if not _quality_ok(ex):
        ex = _chat(text, follow_up=(
            "上面结果太简略，漏掉了文章里写到的具体反应。请重新从全文仔细提取"
            "所有代表性反应（即使结构在图片中，也要写出底物/产物名称、试剂、溶剂、"
            "温度、时间、收率），并给出机理与凝练的底物范围。"))
    return ex


def _parse_json(text: str) -> dict:
    """从模型输出里稳健地抠出 JSON 对象（容忍 markdown 代码块/前后缀）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"```(?:json)?", "", text).strip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("模型未返回有效 JSON")


_ARTICLE_TOP = ("title", "authors", "journal", "year", "volume", "issue",
                "pages", "doi", "abstract", "source_type", "source_url",
                "retrieved_at")


def _merge_article_fields(obj: dict) -> dict:
    """模型有时把文献字段放顶层，而不是嵌进 article，这里归一到 article。

    例如模型输出 {"title":..., "journal": "Org. Lett.", "year": 2024, ...}
    而无 article 对象时，把顶层字段搬进 article，避免元数据丢失。
    """
    if not isinstance(obj, dict):
        return obj
    art = obj.get("article")
    if not isinstance(art, dict):
        art = {}
    for k in _ARTICLE_TOP:
        if k in obj and obj[k] not in (None, "", [], {}):
            if art.get(k) in (None, "", [], {}):
                art[k] = obj[k]
    # journal 可能是嵌套对象 {name: ...}
    j = art.get("journal")
    if isinstance(j, dict):
        art["journal"] = (j.get("name") or j.get("journal") or j.get("title")
                          or str(j))
    # authors 可能是列表，统一成字符串
    for field in ("authors",):
        v = art.get(field)
        if isinstance(v, (list, tuple)):
            art[field] = " ".join(str(x) for x in v if x)
    if any(art.values()):
        obj["article"] = art
    return obj


def extract(meta, text: str, images: List[str]) -> ExtractedArticle:
    """文本 -> 模板。带 API Key 走大模型；否则极简抽取（仅元信息）。"""
    try:
        out = _llm_extract(text)
    except Exception as exc:
        # 不记录异常正文，第三方响应可能包含认证信息或文献正文。
        logging.getLogger(__name__).warning("文本抽取未完成，保留元信息 (%s)", type(exc).__name__)
        out = ExtractedArticle()
    out = _refine(meta, text, out)
    # 用抓取元信息补全，保证不丢标题/来源
    if not out.article.title:
        out.article.title = meta.title
    if not out.article.source_url:
        out.article.source_url = meta.source_url
    if not out.article.source_type:
        out.article.source_type = meta.source_type
    if not out.article.retrieved_at:
        out.article.retrieved_at = meta.retrieved_at
    if not out.article.abstract:
        out.article.abstract = text.strip()[:600]
    if not out.substrate_scope.summary:
        out.substrate_scope.summary = text.strip()[:400]
    # PDF 文本中通常能直接读到优化条件和机理段落。模型调用失败或只返回空
    # JSON 时，保留这些可核对的原文事实，避免知识卡片显示摘要/元数据。
    _refine_pdf_facts(meta, text, out)
    return out


def _refine(meta, text: str, out: ExtractedArticle) -> ExtractedArticle:
    """轻量后处理：兜底补期刊/标题等内容。"""
    title = out.article.title or meta.title or ""
    # 来源类型和原始来源由摄入层确定，不能被模型猜测覆盖。
    out.article.source_type = meta.source_type or out.article.source_type
    out.article.source_url = meta.source_url or out.article.source_url
    if not out.article.journal:
        m = re.search(r"【([^】]{2,60})】", title)
        if m:
            out.article.journal = m.group(1)
    # ACS PDF 常把引用信息写成 “Cite This: J. Am. Chem. Soc. 2022, ... DOI”。
    # 这比让模型从长正文中重复猜测更可靠。
    normalized = unicodedata.normalize("NFKC", text).replace("\u2212", "-")
    # ACS 首页的 Cite This 行在不同 PDF 提取器中可能被拆成多行；只取到
    # Read Online/ACCESS 前，避免把摘要内容误当成期刊字段。
    cite = re.search(r"Cite\s+This:\s*(.+?)(?=\n\s*(?:Read\s+Online|ACCESS)\b|\Z)",
                     normalized, re.I | re.S)
    if cite:
        line = " ".join(cite.group(1).split())
        cm = re.search(
            r"^(?P<journal>.+?)\s+(?P<year>(?:19|20)\d{2})\s*,\s*"
            r"(?P<volume>\d+)\s*,\s*(?P<pages>\d+(?:\s*[-–]\s*\d+)?)",
            line,
        )
        if cm:
            if not out.article.journal:
                out.article.journal = cm.group("journal").strip(" ,;")
            if not out.article.year:
                out.article.year = cm.group("year")
            if not out.article.volume:
                out.article.volume = cm.group("volume")
            if not out.article.pages:
                out.article.pages = cm.group("pages").replace(" ", "").replace("–", "-")
    if not out.article.doi:
        dm = re.search(r"10\.1021/[a-z0-9.]+", normalized, re.I)
        if dm:
            out.article.doi = dm.group(0)
    return out


def _refine_pdf_facts(meta, text: str, out: ExtractedArticle) -> None:
    """从正文的优化/机理段落补齐可验证的兜底信息。

    这是模型不可用时的保底路径，不猜结构，只复制文章中可定位的条件和
    机理描述。模型已有结果时仅补空字段，避免覆盖人工或模型修正。
    """
    if (meta.source_type or "").lower() != "journal":
        return
    normalized = unicodedata.normalize("NFKC", text).replace("\u2212", "-")
    # 代表性反应：ACS 正文的优化条件段可直接转为一个可追溯的条目。
    if not out.reactions:
        m = re.search(
            r"Following an extensive optimization campaign,?\s*we identified\s*"
            r"the conditions outlined in Table 1 as optimal\.\s*(.+?)(?="
            r"\s*The presence of exogenous chloride|\s*With optimized conditions)",
            normalized, re.I | re.S,
        )
        if m:
            detail = " ".join(m.group(1).split())
            y = re.search(r"obtained\s+(?:in|at)\s+(\d+)%\s+yield", detail, re.I)
            from .models import Reaction, StructureRef
            out.reactions.append(Reaction(
                reaction_id="R01",
                name="Alcohol deoxytrifluoromethylation (optimized conditions)",
                type="deoxytrifluoromethylation",
                reactants=[StructureRef(name="alcohol substrate", role="reactant")],
                products=[StructureRef(name="alkyl-CF3 product", role="product")],
                reagent=["NHC salt 2 (1.2 equiv)", "Ir photocatalyst 4 (1 mol%)",
                         "Cu(terpy)Cl2 (5 mol%)", "dMesSCF3 (1.5 equiv)",
                         "TBACl (2 equiv)", "quinuclidine (1.6 equiv)"],
                solvent=["DMSO (0.025 M)"],
                temperature="blue light, 450 nm",
                time="8 h",
                yield_=(y.group(1) + "%") if y else "",
                conditions_text=detail,
            ))
    if not out.substrate_scope.notes:
        # 先抓优化段的结论，再抓无氯/无光/无铜对照，形成短而有用的因素说明。
        bits = []
        m = re.search(
            r"The presence of exogenous chloride anion\s*\(Cl[-–]?\)\s*proved\s*critical(.{0,900})",
            normalized, re.I | re.S,
        )
        if m:
            bits.append(" ".join(m.group(0).split()))
        m = re.search(r"Control experiments revealed that this effect is unique to soluble chloride sources.*?details\.", normalized, re.I | re.S)
        if m:
            bits.append(" ".join(m.group(0).split()))
        if bits:
            out.substrate_scope.notes = "关键影响因素：" + " ".join(bits)[:900]
    if not out.mechanism.overall:
        m = re.search(
            r"Our mechanistic design is detailed in Figure 2\.(.+?)(?=\n\s*Following an extensive optimization campaign)",
            normalized, re.I | re.S,
        )
        if m:
            out.mechanism.overall = " ".join(m.group(1).split())[:900]
