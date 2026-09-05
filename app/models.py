"""固定抽取模板（默认）。可整体替换成你的模板，只要保持 JSON 字段一致即可。"""

from __future__ import annotations

from typing import Annotated, List, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _to_str(v):
    """把数字、None、列表等归一成字符串（大模型输出常不按类型来）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return str(v)


def _to_str_list(v):
    """把单个字符串/None 等归成字符串列表（大模型常给单个值）。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            s = _to_str(x).strip()
            if s:
                out.append(s)
        return out
    return [_to_str(v).strip()] if str(v).strip() else []


def _to_struct_list(v):
    """把字符串元素转成 StructureRef（即当作 name），或保留字典。"""
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(x)
            else:
                out.append({"name": _to_str(x), "smiles": "", "role": ""})
        return out
    return [{"name": _to_str(v), "smiles": "", "role": ""}]


def _to_step_list(v):
    """把字符串元素转成 MechanismStep（即当作 step）。"""
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(x)
            else:
                out.append({"step": _to_str(x), "note": ""})
        return out
    return [{"step": _to_str(v), "note": ""}]


Str = Annotated[str, BeforeValidator(_to_str)]
StrList = Annotated[List[str], BeforeValidator(_to_str_list)]
StructureList = Annotated[List["StructureRef"], BeforeValidator(_to_struct_list)]
StepList = Annotated[List["MechanismStep"], BeforeValidator(_to_step_list)]


class StructureRef(BaseModel):
    """一个结构引用：名称 + 可选 SMILES + 角色。"""

    name: Str = ""
    smiles: Str = ""
    role: Str = ""  # reactant / product / reagent


class ArticleMeta(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    title: Str = ""
    authors: Str = ""
    journal: Str = ""
    year: Str = ""
    volume: Str = ""
    issue: Str = ""
    pages: Str = ""
    doi: Str = ""
    source_type: Str = "web"  # wechat | journal | web
    source_url: Str = ""
    abstract: Str = ""
    retrieved_at: Str = ""


class Reaction(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    reaction_id: Str = ""
    name: Str = ""
    type: Str = ""
    reactants: StructureList = []
    products: StructureList = []
    reagent: StrList = []
    solvent: StrList = []
    temperature: Str = ""
    time: Str = ""
    yield_: Str = Field(default="", alias="yield")
    conditions_text: Str = ""
    mechanism: Str = ""
    notes: Str = ""
    source_image: Str = ""


class MechanismStep(BaseModel):
    step: Str = ""
    note: Str = ""


class Mechanism(BaseModel):
    overall: Str = ""
    steps: StepList = []


class SubstrateScope(BaseModel):
    summary: Str = ""
    notes: Str = ""


class Keywords(BaseModel):
    compounds: StrList = []
    reactions: StrList = []
    tags: StrList = []


class ExtractedArticle(BaseModel):
    article: ArticleMeta = ArticleMeta()
    reactions: List[Reaction] = []
    mechanism: Mechanism = Mechanism()
    substrate_scope: SubstrateScope = SubstrateScope()
    keywords: Keywords = Keywords()

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, exclude_none=True)

    @classmethod
    def from_json(cls, data: str) -> "ExtractedArticle":
        return cls.model_validate_json(data)
