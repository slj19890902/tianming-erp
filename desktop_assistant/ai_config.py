"""Machine-local AI credential handling for the desktop assistant."""
from __future__ import annotations

from pathlib import Path

from desktop_assistant.storage import read_json, write_json
from desktop_assistant.credential_store import protect, unprotect


AI_CONFIG_FILE = "ai-provider.json"


def normalize_openai_api_key(value: str) -> str:
    key = value.strip()
    if not key.startswith("sk-") or not 20 <= len(key) <= 512:
        raise ValueError("AI 密钥格式无效，请从对应服务商复制完整密钥")
    if any(character.isspace() or ord(character) < 32 for character in key):
        raise ValueError("AI 密钥不能包含空格或换行")
    return key


def save_openai_api_key(root: Path, value: str) -> None:
    key = normalize_openai_api_key(value)
    (root / "control").mkdir(parents=True, exist_ok=True)
    write_json(
        root / "control" / AI_CONFIG_FILE,
        {
            "provider": "openai",
            "protected_api_key": protect(key, 'openai'),
        },
    )


def load_openai_api_key(root: Path) -> str | None:
    path = root / "control" / AI_CONFIG_FILE
    if not path.is_file():
        return None
    config = read_json(path)
    if config.get("provider") != "openai":
        raise ValueError("AI 服务配置类型无效")
    protected = config.get("protected_api_key")
    if not isinstance(protected, str) or not protected:
        raise ValueError("AI 密钥配置不完整")
    return normalize_openai_api_key(unprotect(protected, 'openai'))


def openai_api_key_is_configured(root: Path) -> bool:
    return load_openai_api_key(root) is not None


def save_deepseek_api_key(root: Path, value: str) -> None:
    key = normalize_openai_api_key(value)
    (root / "control").mkdir(parents=True, exist_ok=True)
    write_json(root / "control" / "deepseek-provider.json",
               {"provider": "deepseek", "protected_api_key": protect(key, 'deepseek')})


def load_deepseek_api_key(root: Path) -> str | None:
    path = root / "control" / "deepseek-provider.json"
    if not path.is_file():
        return None
    config = read_json(path)
    if config.get("provider") != "deepseek":
        raise ValueError("DeepSeek 服务配置类型无效")
    return normalize_openai_api_key(unprotect(config["protected_api_key"], 'deepseek'))
