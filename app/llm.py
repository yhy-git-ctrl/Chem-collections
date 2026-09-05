"""共享模型入口：每次调用读取配置；切换不改变数据库或输出格式。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import dotenv_values

from . import config

SETTINGS_PATH = config.PROJECT_ROOT / "models.local.json"


def read_settings(path: Path = SETTINGS_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    # 保留旧安装的端点和密钥，不根据模型名猜测第三方地址。
    return {
        "active_provider": "legacy",
        "providers": {"legacy": {
            "base_url": config.OPENAI_BASE_URL or "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "text_model": config.OPENAI_MODEL,
            "vision_model": os.getenv("OPENAI_VISION_MODEL", ""),
            "text_options": {"max_tokens": 4000, "temperature": 0},
            "vision_options": {"max_tokens": 4096, "temperature": 0},
        }},
    }


def resolve(role: str, settings: dict | None = None) -> tuple[str, dict, str]:
    if role not in ("text", "vision"):
        raise ValueError("模型用途只能是 text 或 vision")
    settings = settings if settings is not None else read_settings()
    provider = settings.get(f"{role}_provider") or settings["active_provider"]
    profile = settings["providers"][provider]
    model = profile.get(f"{role}_model", "").strip()
    if not model:
        raise ValueError(f"{provider} 尚未配置 {role}_model")
    return provider, profile, model


def api_key(profile: dict) -> str:
    name = profile["api_key_env"]
    # config.load_dotenv 会保留进程环境；以实际进程环境优先。
    return (os.getenv(name) or dotenv_values(config.PROJECT_ROOT / ".env").get(name) or "").strip()


def complete(role: str, messages: list) -> str:
    from openai import OpenAI

    provider, profile, model = resolve(role)
    key = api_key(profile)
    if not key:
        raise ValueError(f"{provider} 缺少 {profile['api_key_env']}")
    options = dict(profile.get(f"{role}_options", {}))
    allowed = {"temperature", "max_tokens", "max_completion_tokens", "reasoning_effort", "extra_body"}
    if set(options) - allowed:
        raise ValueError(f"{provider} 包含不支持的请求选项")
    if "max_tokens" in options and "max_completion_tokens" in options:
        raise ValueError("不能同时配置 max_tokens 和 max_completion_tokens")
    with OpenAI(api_key=key, base_url=profile["base_url"],
                timeout=config.LLM_TIMEOUT) as client:
        response = client.chat.completions.create(model=model, messages=messages, **options)
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ValueError(f"{provider}/{model} 返回空内容")
    return content


def status(settings: dict | None = None) -> dict:
    settings = settings if settings is not None else read_settings()
    result = {}
    for role in ("text", "vision"):
        try:
            provider, profile, model = resolve(role, settings)
            result[role] = {"provider": provider, "model": model,
                            "key_configured": bool(api_key(profile))}
        except (KeyError, ValueError) as exc:
            result[role] = {"error": str(exc)}
    return result


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="切换文献库模型（不会重处理已有文献）")
    parser.add_argument("command", choices=["status", "switch"])
    parser.add_argument("provider", nargs="?")
    parser.add_argument("--role", choices=["all", "text", "vision"], default="all")
    args = parser.parse_args()
    settings = read_settings()
    if args.command == "switch":
        if args.provider not in settings["providers"]:
            parser.error("请选择 models.local.json 中已配置的提供商")
        roles = ("text", "vision") if args.role == "all" else (args.role,)
        candidate = dict(settings)
        for role in roles:
            candidate[f"{role}_provider"] = args.provider
            try:
                _, profile, _ = resolve(role, candidate)
                if not api_key(profile):
                    parser.error(f"请先在 .env 配置 {profile['api_key_env']}")
            except (ValueError, KeyError) as exc:
                parser.error(str(exc))
        if args.role == "all":
            candidate["active_provider"] = args.provider
        temp = SETTINGS_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(SETTINGS_PATH)
        settings = candidate
    print(json.dumps(status(settings), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
