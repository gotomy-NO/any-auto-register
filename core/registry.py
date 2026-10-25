"""平台插件注册表 - 自动扫描 platforms/ 目录加载插件"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Type

from .base_platform import BasePlatform

# 项目当前只维护 ChatGPT 与 iCloud 两类注册；其余插件已下线。
SUPPORTED_PLATFORMS = (
    "chatgpt", "icloud",
    # 201 fork 移植回迁（方案A，见 docs/注册机方案A移植分析.md）
    "github", "cerebras", "tavily", "cursor",
    "kiro", "trae", "grok", "zcode",
)

_registry: dict[str, Type[BasePlatform]] = {}


def normalize_platform(name: str) -> str:
    return str(name or "").strip().lower()


def is_platform_enabled(name: str) -> bool:
    return normalize_platform(name) in SUPPORTED_PLATFORMS


def register(cls: Type[BasePlatform]) -> Type[BasePlatform]:
    """装饰器：注册平台插件"""
    if is_platform_enabled(cls.name):
        _registry[cls.name] = cls
    return cls


def load_all() -> None:
    """自动扫描并加载 platforms/ 下所有受支持的插件"""
    import platforms

    for _, module_name, _ in pkgutil.iter_modules(platforms.__path__, platforms.__name__ + "."):
        if not is_platform_enabled(module_name.rsplit(".", 1)[-1]):
            continue
        try:
            importlib.import_module(f"{module_name}.plugin")
        except ModuleNotFoundError:
            pass


def get(name: str) -> Type[BasePlatform]:
    normalized = normalize_platform(name)
    if not is_platform_enabled(normalized):
        raise KeyError(f"平台 '{name}' 已下线")
    if normalized not in _registry:
        raise KeyError(f"平台 '{name}' 未注册，已注册: {list(_registry)}")
    return _registry[normalized]


def list_platforms() -> list[dict[str, str]]:
    return [
        {"name": cls.name, "display_name": cls.display_name, "version": cls.version}
        for cls in _registry.values()
    ]
