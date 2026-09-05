"""项目配置：读取 .env 与路径。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str) -> str:
    """返回环境变量；空白视为未设置（避免 .env 空值覆盖默认值）。"""
    val = os.getenv(name, "").strip()
    return val if val else default


DATA_DIR = Path(_env("DATA_DIR", str(PROJECT_ROOT / "data")))
DB_PATH = Path(_env("DB_PATH", str(DATA_DIR / "library.db")))
ARTICLES_DIR = DATA_DIR / "articles"
IMAGES_DIR = DATA_DIR / "images"

OPENAI_API_KEY = _env("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "")  # 兼容 DeepSeek 等
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = int(_env("LLM_TIMEOUT", "90"))

# 抓取公众号文章时使用的浏览器 UA
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def ensure_dirs() -> None:
    for d in (DATA_DIR, ARTICLES_DIR, IMAGES_DIR):
        d.mkdir(parents=True, exist_ok=True)
