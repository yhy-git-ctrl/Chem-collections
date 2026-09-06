"""FastAPI 本地 Web 服务：供桌面与手机（PWA）访问。"""

from __future__ import annotations

from io import BytesIO
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rdkit import Chem
from rdkit.Chem import AllChem, Draw

from . import config
from .classify import classify
from .db import (
    find_duplicate, get_article, get_card_info, get_compound, init_db,
    get_stats, insert_images, keyword_search, list_articles, set_image_role,
    store_article, structure_search,
)
from .ppt import export_all_ppt, export_single_ppt
from .extract import extract
from .ingest import ingest, archive_pdf

config.ensure_dirs()
init_db()

app = FastAPI(title="化学文献库")
app.mount("/images", StaticFiles(directory=str(config.IMAGES_DIR)), name="images")
STATIC_HTML = str(config.PROJECT_ROOT / "app" / "static" / "index.html")


class AddPayload(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    force: bool = False


class StructureQuery(BaseModel):
    smiles: str


class ImageRole(BaseModel):
    role: str


class CropPayload(BaseModel):
    article_id: int
    name: str
    x0: float
    y0: float
    x1: float
    y1: float
    role: str = "reaction"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    with open(STATIC_HTML, encoding="utf-8") as f:
        return HTMLResponse(
            content=f.read(),
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )


@app.get("/api/compounds/{compound_id}/image")
def compound_image(compound_id: int) -> Response:
    c = get_compound(compound_id)
    molblock = c.get("molblock", "")
    if not molblock:
        raise HTTPException(404, "该结构未提供二维坐标")
    mol = Chem.MolFromMolBlock(molblock)
    if mol is None:
        raise HTTPException(404, "结构无法渲染")
    img = Draw.MolToImage(mol, size=(260, 200))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/structure-preview")
def structure_preview(smiles: str, w: int = 260, h: int = 200) -> Response:
    """按 SMILES 在线渲染结构图（用于详情/搜索结果里的结构缩略图）。"""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise HTTPException(404, "无法解析 SMILES")
    AllChem.Compute2DCoords(mol)
    img = Draw.MolToImage(mol, size=(w, h))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/api/articles/add")
def add_article(payload: AddPayload):
    if not (payload.url or payload.text):
        raise HTTPException(400, "提供 url 或 text")
    try:
        data = ingest(payload.url or payload.text)
        ex = extract(data["meta"], data["text"], data["images"])
        dup = find_duplicate(source_url=ex.article.source_url,
                             doi=ex.article.doi,
                             title=ex.article.title,
                             journal=ex.article.journal,
                             year=ex.article.year,
                             volume=ex.article.volume,
                             issue=ex.article.issue,
                             pages=ex.article.pages)
        if dup and not payload.force:
            return {"duplicate": True, "existing": dup}
        category = classify(ex.to_json())
        article_id = store_article(ex, data["raw_path"], data["text_path"],
                                   category=category)
        insert_images(article_id, [i["path"] for i in data["images"]],
                      [i["role"] for i in data["images"]], [i.get("review_status", "") for i in data["images"]])
        info = get_card_info(article_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, detail=f"处理失败：{e}")
    return {"id": article_id, "card_no": info.get("card_no"),
            "category": info.get("category"),
            "extracted": ex.model_dump(by_alias=True, exclude_none=True)}


@app.post("/api/articles/upload")
def upload_article(file: UploadFile = File(...)):
    import tempfile
    from pathlib import Path

    suffix = Path(file.filename or "up.pdf").suffix or ".pdf"
    if suffix.lower() not in (".pdf", ".txt", ".html", ".md"):
        raise HTTPException(400, "请选择 PDF、TXT、HTML 或 Markdown 文件")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        f.write(file.file.read())
        tmp = f.name
    try:
        if suffix.lower() == ".pdf":
            saved = archive_pdf(Path(tmp))
            dup = find_duplicate(source_url=saved)
            if dup:
                return {"duplicate": True, "existing": dup}
        data = ingest(tmp)
    except Exception as e:
        raise HTTPException(400, detail=f"无法读取文件：{type(e).__name__}")
    finally:
        Path(tmp).unlink(missing_ok=True)
    try:
        ex = extract(data["meta"], data["text"], data["images"])
        dup = find_duplicate(source_url=ex.article.source_url, doi=ex.article.doi,
                             title=ex.article.title, journal=ex.article.journal,
                             year=ex.article.year)
        if dup:
            return {"duplicate": True, "existing": dup}
        category = classify(ex.to_json())
        article_id = store_article(ex, data["raw_path"], data["text_path"],
                                   category=category)
        insert_images(article_id, [i["path"] for i in data["images"]],
                      [i["role"] for i in data["images"]], [i.get("review_status", "") for i in data["images"]])
        info = get_card_info(article_id)
    except Exception as e:
        raise HTTPException(502, detail=f"处理失败：{e}")
    return {"id": article_id, "card_no": info.get("card_no"),
            "category": info.get("category"),
            "extracted": ex.model_dump(by_alias=True, exclude_none=True)}


@app.get("/api/articles")
def articles():
    return list_articles()


@app.get("/api/stats")
def stats():
    return get_stats()


@app.get("/api/open/articles")
def open_articles_dir():
    """用系统文件管理器打开存放原文/PDF 的文件夹（仅本机生效）。"""
    import os

    d = config.ARTICLES_DIR
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(d))  # Windows 专用
    except Exception as e:
        raise HTTPException(500, detail=f"打开文件夹失败：{e}")
    return {"ok": True, "path": str(d)}


@app.get("/api/export/ppt")
def export_ppt(article_id: Optional[int] = None):
    """导出知识卡片为 PPT；不传 article_id 则导出全库。"""
    from fastapi.responses import FileResponse
    import tempfile
    from pathlib import Path

    try:
        if article_id:
            buf = export_single_ppt(article_id)
            label = f"KC-{str(article_id).zfill(4)}"
        else:
            buf = export_all_ppt()
            label = "全部"
        tmp = Path(tempfile.mkstemp(suffix=".pptx")[1])
        tmp.write_bytes(buf.getvalue())
        return FileResponse(
            path=str(tmp), filename=f"化学文献库_{label}.pptx",
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    except Exception as e:
        raise HTTPException(500, detail=f"导出失败：{e}")


@app.get("/api/articles/{article_id}")
def article_detail(article_id: int):
    art = get_article(article_id)
    if not art:
        raise HTTPException(404, "未找到")
    return art


@app.get("/api/search")
def search_keywords(q: str = ""):
    return keyword_search(q)


@app.post("/api/search/structure")
def search_structure(payload: StructureQuery):
    return structure_search(payload.smiles)


@app.post("/api/images/{image_id}/role")
def image_role(image_id: int, payload: ImageRole):
    role = payload.role if payload.role in ("scope", "reaction", "mechanism", "conditions", "other") else "other"
    set_image_role(image_id, role)
    return {"ok": True, "image_id": image_id, "role": role}


@app.post("/api/images/crop")
def image_crop(payload: CropPayload):
    """从一张整页/原图里按归一化坐标裁出反应式图，存为新图片并入库。"""
    import time
    from PIL import Image

    src = config.IMAGES_DIR / payload.name
    if not src.exists():
        raise HTTPException(404, "源图不存在")
    role = payload.role if payload.role in ("scope", "reaction", "mechanism",
                                            "conditions", "other") else "reaction"
    try:
        im = Image.open(src).convert("RGB")
        w, h = im.size
        box = (max(0, int(round(payload.x0 * w))), max(0, int(round(payload.y0 * h))),
               min(w, int(round(payload.x1 * w))), min(h, int(round(payload.y1 * h))))
        if box[2] - box[0] < 30 or box[3] - box[1] < 30:
            raise HTTPException(400, "裁剪区域过小")
        crop = im.crop(box)
        # 放大到可读尺寸，避免小图模糊
        if crop.width < 300 or crop.height < 180:
            scale = max(1, int(300 / crop.width), int(180 / crop.height))
            if scale > 1:
                crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
        name = f"crop_{int(time.time() * 1000)}_{payload.article_id}.png"
        crop.save(config.IMAGES_DIR / name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"裁剪失败：{e}")

    insert_images(payload.article_id, [name], [role])
    return {"ok": True, "path": name, "role": role}
