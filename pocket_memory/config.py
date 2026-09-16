from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    """Keep release configuration writable even when the app folder is read-only."""
    if not getattr(sys, "frozen", False):
        return app_directory() / "config.json"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "PocketMemory" / "config.json"
    return Path.home() / ".config" / "pocket-memory" / "config.json"


@dataclass
class AppConfig:
    path: Path
    data_dir: Path | None = None
    theme: str = "aqua-memory"
    ai_auto_apply: bool = True  # True=自动应用 AI 建议, False=仅整理不自动应用
    llm_model_path: str | None = None  # 选中的 LLM 模型相对路径（相对 models/），None=默认

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        config_path = path or default_config_path()
        if not config_path.exists():
            return cls(path=config_path)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("config.json 根节点必须是对象")
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.warning("config.json 解析失败，使用默认配置: %s", exc)
            return cls(path=config_path)
        raw_data_dir = payload.get("data_dir")
        return cls(
            path=config_path,
            data_dir=Path(raw_data_dir).resolve() if raw_data_dir else None,
            theme=payload.get("theme", "aqua-memory"),
            ai_auto_apply=bool(payload.get("ai_auto_apply", True)),
            llm_model_path=payload.get("llm_model_path"),
        )

    def _save(self) -> None:
        # 原子写：先写临时文件再 rename，避免异常中断写出半截 JSON
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            {
                "data_dir": str(self.data_dir) if self.data_dir else None,
                "theme": self.theme,
                "ai_auto_apply": self.ai_auto_apply,
                "llm_model_path": self.llm_model_path,
            },
            ensure_ascii=False,
            indent=2,
        )
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def set_data_dir(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self._save()

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self._save()

    def set_ai_auto_apply(self, enabled: bool) -> None:
        self.ai_auto_apply = enabled
        self._save()

    def set_llm_model_path(self, model_path: str | None) -> None:
        self.llm_model_path = model_path
        self._save()
