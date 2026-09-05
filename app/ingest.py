"""收集与解析：抓取公众号/网页，或读取 PDF / 文本，输出干净文本与元信息。"""

from __future__ import annotations

import re
import base64
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from . import config
from . import vision
from .models import ArticleMeta

JINA = "https://r.jina.ai/"


def _save_image_bytes(data: bytes, ext: str, idx: int) -> str:
    config.ensure_dirs()
    name = f"img_{int(time.time() * 1000)}_{idx}{ext}"
    (config.IMAGES_DIR / name).write_bytes(data)
    return name


def download_images(urls: List[str], referer: str = "") -> Dict[str, str]:
    """从文章里下载图片到本地。返回 {原始外链: 本地文件名}。
    只有下载成功的才会进入返回，便于按原文顺序内嵌回 HTML。
    """
    saved: Dict[str, str] = {}
    headers = {"User-Agent": config.UA}
    if referer:
        headers["Referer"] = referer
    for i, u in enumerate(urls[:40]):
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        try:
            r = requests.get(u, headers=headers, timeout=18)
            if r.status_code != 200 or len(r.content) < 2000:
                continue
            ct = (r.headers.get("content-type") or "").lower()
            ext = ".png" if "png" in ct else ".jpg" if ("jpeg" in ct or "jpg" in ct) \
                else ".webp" if "webp" in ct else ".png"
            pext = str(Path(urlparse(u).path).suffix or "").lower()
            if pext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                ext = pext
            saved[u] = _save_image_bytes(r.content, ext, i)
        except requests.RequestException:
            continue
    return saved


def _mime_from_name(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/png"


def embed_images_self_contained(html: str, title: str,
                                url2file: Dict[str, str]) -> str:
    """把下载好的图片以 base64 内嵌回正文，生成双击即可看图的完整 HTML。

    做法：用 BeautifulSoup 解析，保留正文区（#js_content / article / body），
    去掉 script/style/noscript 等干扰；把每个 <img> 的懒加载外链替换成本地
    图片的 data: 内嵌地址，最终输出一份自包含的单文件 HTML。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.select_one("#js_content") or soup.select_one("article") or soup.body
    if body is None:
        return html

    for img in body.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        local = url2file.get(src)
        if not local:
            # 没有对应本地图的（如表情、无法下载的）直接去掉图占位
            img.decompose()
            continue
        path = config.IMAGES_DIR / local
        try:
            data = path.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            mime = _mime_from_name(local)
            img["src"] = f"data:{mime};base64,{b64}"
            img["data-src"] = ""
            # 清理微信懒加载/data 属性，避免脚本或样式干扰
            for key in list(img.attrs):
                if key.startswith("data-") and key != "data-src":
                    del img[key]
        except OSError:
            img.decompose()

    style = (
        "body{font-family:'Microsoft YaHei',sans-serif;max-width:960px;margin:0 auto;"
        "padding:24px;line-height:1.8;color:#222}"
        "img{max-width:100%;height:auto;display:block;margin:12px auto;border-radius:6px}"
        "h1{font-size:1.4em} p{margin:.6em 0}"
    )
    title_txt = (title or "").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f"<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title_txt}</title><style>{style}</style></head>"
        f"<body><h1>{title_txt}</h1>{body}</body></html>"
    )


def extract_pdf_images(path: Path) -> List[str]:
    """从 PDF 提取内嵌图片到本地，返回本地文件名列表。"""
    import pymupdf

    saved: List[str] = []
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return []
    i = 0
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                pix = pymupdf.Pixmap(doc, xref)
                if pix.n > 4:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                w, h = pix.width, pix.height
                # 过滤期刊 Logo / 页眉水印等小图（多为"宽短条"或极小图标）
                if w < 100 or h < 80:
                    continue
                if w / max(h, 1) > 3.5 and min(w, h) < 200:
                    continue
                data = pix.tobytes("png")
                if len(data) < 3000:
                    continue
                saved.append(_save_image_bytes(data, ".png", i))
                i += 1
            except Exception:
                continue
    return saved


def render_pdf_pages(path: Path, dpi: int = 110) -> List[str]:
    """把 PDF 每页渲染成整页图片，捕捉矢量绘制的反应式/机理/底物图。"""
    import pymupdf

    saved: List[str] = []
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return []
    for pno, page in enumerate(doc):
        try:
            pix = page.get_pixmap(dpi=dpi)
            if pix.width < 300 or pix.height < 150:
                continue
            saved.append(_save_image_bytes(pix.tobytes("png"), ".png", pno))
        except Exception:
            continue
    return saved


def crop_pdf_figures(path: Path, dpi: int = 150) -> List[str]:
    """按图注裁剪图，并返回图片文件名（兼容旧调用方）。"""
    return [p for p, _ in crop_pdf_figures_with_roles(path, dpi=dpi)]


def _caption_role(caption: str, width: float | None = None,
                  height: float | None = None) -> str:
    """根据图注的编号/关键词给出稳定的初始用途，随后仍可在界面手动调整。"""
    s = unicodedata.normalize("NFKC", caption or "").lower()
    ratio = (width / height) if width and height else None
    if re.match(r"\s*table\s*1\b|\s*表\s*1\b", s):
        # ACS Table 1 often has a reaction equation above the control rows. A
        # short, wide crop is that equation and belongs in the main-reaction
        # slot; a taller crop is the actual optimization/control table.
        return "reaction" if ratio and ratio >= 3.0 else "conditions"
    if re.match(r"\s*table\s*[2-9]\b|\s*表\s*[2-9]\b", s):
        return "scope"
    if "mechanism" in s or "机理" in s or re.match(r"\s*(figure|fig\.?|图)\s*2\b", s):
        return "mechanism"
    if re.match(r"\s*(scheme|figure|fig\.?|图)\s*1\b", s):
        # Figure 1 can be a general concept/overview. Only a wide equation
        # crop is treated as a representative reaction.
        return "reaction" if ratio and ratio >= 2.0 else ("reaction" if ratio is None else "other")
    return "other"


def crop_pdf_figures_with_roles(path: Path, dpi: int = 150) -> List[Tuple[str, str]]:
    """按 Scheme/Figure/Fig./Table/图注裁剪，并返回 (文件名, 初始用途)。

    ACS 图注有的在图上方、有的在图下方；反应式是密集矢量图形，文字段少。
    因此在图注上下各取一个候选窗口，保留矢量对象更密集的一侧，从而稳定抓到图。
    """
    import re
    import pymupdf

    saved: List[Tuple[str, str]] = []
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return []
    cap_re = re.compile(r"^\s*(Scheme|Figure|Fig\.?|Table|图|表)\s*\d", re.I)

    def _draw_in(page, rect) -> int:
        n = 0
        for dr in page.get_drawings():
            try:
                if dr["rect"].intersects(rect):
                    n += 1
            except Exception:
                pass
        return n

    idx = 0
    for page in doc:
        pw, ph = page.rect.width, page.rect.height
        blocks = page.get_text("blocks")
        caps = sorted([b for b in blocks if cap_re.match((b[4] or "").strip())],
                      key=lambda b: b[1])
        used_images = set()
        for b in caps:
            x0, y0, x1, y1 = b[0], b[1], b[2], b[3]
            caption = (b[4] or "").strip()
            role = _caption_role(caption)
            # 很多 ACS PDF 的完整反应图本身就是高分辨率位图。
            # 优先选紧邻图注的位图边界，避免用稀疏的装饰矢量裁成半页正文。
            candidates = []
            for im in page.get_image_info(xrefs=True):
                r = pymupdf.Rect(im["bbox"])
                if im["xref"] in used_images or not im["xref"] or r.width < 100 or r.height < 40:
                    continue
                overlap = max(0, min(x1, r.x1) - max(x0, r.x0))
                if overlap < 0.6 * min(x1-x0, r.width):
                    continue
                gap = min(abs(y0-r.y1), abs(r.y0-y1))
                if gap < 55:
                    candidates.append((gap, im))
            if candidates:
                _, im = min(candidates, key=lambda candidate: candidate[0])
                pix = pymupdf.Pixmap(doc, im["xref"])
                if pix.n - pix.alpha > 3:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                sel_rect = pymupdf.Rect(im["bbox"])
                role2 = _caption_role(caption, sel_rect.width, sel_rect.height)
                saved.append((_save_image_bytes(pix.tobytes("png"), ".png", 1000 + idx), role2))
                used_images.add(im["xref"])
                idx += 1
                continue
            # 图注较宽视为通栏图，否则按列宽
            wide = (x1 - x0) > 0.5 * pw
            col_x0, col_x1 = (0, pw) if wide else (max(0, x0 - 4), min(pw, x1 + 6))
            win = 0.34 * ph
            up_rect = pymupdf.Rect(col_x0, max(0.13 * ph, y0 - win), col_x1, y0)
            low_rect = pymupdf.Rect(col_x0, y1, col_x1, min(ph, y1 + win))
            up = _draw_in(page, up_rect)
            low = _draw_in(page, low_rect)
            best = up_rect if up >= low else low_rect
            # 收紧到"构件图形"外接框（排除超长页眉/分隔线等装饰），去掉四周文字
            minx = best.x0; miny = best.y0; maxx = best.x1; maxy = best.y1
            found = False
            for dr in page.get_drawings():
                r = dr["rect"]
                if r.intersects(best):
                    try:
                        w = r.x1 - r.x0; h = r.y1 - r.y0
                        if r.width > 0.8 * pw or r.height > 0.8 * ph:
                            continue
                        if w < 3 and h < 3:
                            continue
                        if found:
                            minx = min(minx, r.x0); miny = min(miny, r.y0)
                            maxx = max(maxx, r.x1); maxy = max(maxy, r.y1)
                        else:
                            minx, miny, maxx, maxy = r.x0, r.y0, r.x1, r.y1
                            found = True
                    except Exception:
                        pass
            if not found:
                continue
            # 留出结构中的原子标签与边缘文字，避免精确贴着键线截断。
            rect = pymupdf.Rect(minx-10, miny-10, maxx+10, maxy+10) & page.rect
            pix = page.get_pixmap(dpi=dpi, clip=rect)
            if pix.width < 300 or pix.height < 160:
                continue
            role2 = _caption_role(caption, rect.width, rect.height)
            saved.append((_save_image_bytes(pix.tobytes("png"), ".png", 1000 + idx), role2))
            idx += 1
    return saved


def _safe_name(name: str, suffix: str = ".txt") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:80].strip() or "untitled"
    return cleaned + suffix


def _save_raw(name: str, content: str) -> str:
    config.ensure_dirs()
    path = config.ARTICLES_DIR / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _save_text(name: str, content: str) -> str:
    config.ensure_dirs()
    path = config.ARTICLES_DIR / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def fetch_url(url: str) -> Tuple[str, str]:
    """返回 (内容, 格式)。格式为 'html' 或 'markdown'。优先直接抓，失败回退 jina。"""
    try:
        r = requests.get(url, headers={"User-Agent": config.UA}, timeout=30)
        if r.status_code == 200 and r.text and len(r.text) > 500:
            return r.text, "html"
    except requests.RequestException:
        pass
    try:
        r = requests.get(JINA + url, headers={"User-Agent": config.UA}, timeout=20)
        if r.status_code == 200 and r.text:
            return r.text, "markdown"
    except requests.RequestException:
        pass
    return "", ""


def _title_from_soup(soup: BeautifulSoup) -> str:
    for sel in ("meta[property='og:title']", "meta[name='twitter:title']"):
        el = soup.select_one(sel)
        if el and el.get("content"):
            return el["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def html_to_text_and_images(html: str) -> Tuple[str, str, List[str]]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = _title_from_soup(soup)
    # 微信公众号正文区
    body = soup.select_one("#js_content") or soup.select_one("article") or soup.body
    text = (body.get_text("\n", strip=True) if body else
            " ".join(soup.stripped_strings))
    text = re.sub(r"\n{3,}", "\n\n", text)
    images = [el.get("data-src") or el.get("src")
              for el in soup.find_all("img")]
    images = [i for i in images if i]
    return title, text, images


def parse_pdf(path: Path) -> Tuple[str, str]:
    import pymupdf  # 兼容 import fitz 的较新版本
    doc = pymupdf.open(str(path))
    pages = [p.get_text() for p in doc]
    text = unicodedata.normalize("NFKC", "\n".join(pages))
    first = (unicodedata.normalize("NFKC", pages[0]).strip().splitlines()[0]
             if pages and pages[0].strip() else "")
    title = first if first else path.stem
    return title, text


def _guess_source_type(url: str) -> str:
    if "mp.weixin.qq.com" in url:
        return "wechat"
    if url.lower().endswith((".pdf", ".txt", ".html")):
        return "journal"
    return "web"


def ingest(source: str) -> Dict:
    """摄入来源：URL / PDF 文件 / 文本|文本文件。返回 meta 与文本等。"""
    now = datetime.now().isoformat(timespec="seconds")
    src = source.strip()
    page_images = []
    preset_roles: Dict[str, str] = {}
    if src.startswith("http://") or src.startswith("https://"):
        content, fmt = fetch_url(src)
        if not content:
            raise RuntimeError("抓取失败：请在 .env 配置后重试，或改用微信复制链接。")
        if fmt == "html":
            title, text, images = html_to_text_and_images(content)
            url2file = download_images(images, referer=src)
            image_files = list(url2file.values())
            # 生成"双击即可看图文"的自包含源文件（图片 base64 内嵌）
            full_html = embed_images_self_contained(content, title, url2file)
            raw_path = _save_raw(f"wechat_{int(datetime.now().timestamp())}.html", full_html)
            images = image_files
        else:  # markdown
            title = (content.splitlines()[0].strip("# ").strip()
                     if content.splitlines() else "")
            text = content
            images = []
            raw_path = _save_raw(f"wechat_{int(datetime.now().timestamp())}.md", content)
        source_type = _guess_source_type(src)
        meta = ArticleMeta(title=title, source_type=source_type, source_url=src,
                           retrieved_at=now)
    else:
        p = Path(src)
        if p.suffix.lower() == ".pdf":
            title, text = parse_pdf(p)
            # 优先用图注裁剪出的反应/机理/底物图；整页渲染兜底+作为手动裁剪源。
            cropped = crop_pdf_figures_with_roles(p)
            if cropped:
                images = [name for name, _ in cropped]
                # 记录所有图注规则结果（包括 other），避免视觉模型把概览图
                # 或 Table 1 再次误判为第二张主反应式。
                preset_roles = {name: role for name, role in cropped}
            else:
                images = extract_pdf_images(p)
            page_images = render_pdf_pages(p)
            raw_path = archive_pdf(p)
            src = raw_path
        elif p.exists() and p.suffix.lower() in (".txt", ".md", ".html"):
            raw = p.read_text(encoding="utf-8", errors="ignore")
            if p.suffix.lower() == ".html":
                title, text, images = html_to_text_and_images(raw)
                url2file = download_images(images)
                images = list(url2file.values())
            else:
                title = raw.strip().splitlines()[0] if raw.strip() else p.stem
                text = raw
                images = []
            raw_path = _save_raw(f"file_{p.stem}.{p.suffix.lstrip('.')}", raw)
        else:
            # 视为直接文本
            text = src
            title = text.strip().splitlines()[0] if text.strip() else ""
            images = []
            raw_path = _save_raw(f"text_{int(datetime.now().timestamp())}.txt", text)
        source_type = "journal" if p.suffix.lower() == ".pdf" else "web"
        meta = ArticleMeta(title=title, source_type=source_type, source_url=src,
                           retrieved_at=now)

    text_path = _save_text(
        f"text_{_safe_name(meta.title[:40], '')}_{int(datetime.now().timestamp())}.txt",
        text,
    )
    img_objs = []
    if images:
        full = [str(config.IMAGES_DIR / n) for n in images]
        # 有图注编号时优先使用可解释的规则（Figure 1=反应式、Figure 2=机理、
        # Table 1=条件优化、Table 2+=底物范围），避免视觉模型把 Table 1 当反应式。
        # 未能判定的图片才交给视觉模型分类。
        uncertain = [i for i, n in enumerate(images) if n not in preset_roles]
        tagged = vision.tag_images([full[i] for i in uncertain]) if uncertain else []
        tag_by_index = {i: role for i, (_, role) in zip(uncertain, tagged)}
        img_objs = [{"path": images[i], "role": preset_roles.get(images[i], tag_by_index.get(i, "other"))}
                    for i in range(len(images))]
    # 整页图仅供人工裁剪，不能被自动分到反应式/机理槽。
    img_objs.extend({"path": name, "role": "other"} for name in page_images)
    return {"meta": meta, "text": text, "images": img_objs,
            "raw_path": raw_path, "text_path": text_path}


def archive_pdf(path: Path) -> str:
    """按内容摘要保存原始字节，避免临时文件删除后源 PDF 丢失。"""
    import hashlib
    content = path.read_bytes()
    if not content.startswith(b"%PDF-"):
        raise ValueError("文件不是有效的 PDF")
    config.ensure_dirs()
    target = config.ARTICLES_DIR / ("pdf_" + hashlib.sha256(content).hexdigest() + ".pdf")
    if not target.exists():
        target.write_bytes(content)
    return str(target)
