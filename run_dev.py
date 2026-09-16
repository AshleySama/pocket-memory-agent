"""Pocket Memory 开发模式启动器。

用 Python 脚本替代 .bat，减少被终端防护误判为批处理启动器的概率。
启动方式（任选其一）：
  1. 终端：python run_dev.py
  2. 终端（先激活虚拟环境）：
     .venv\\Scripts\\activate && python run_dev.py
  3. 直接用虚拟环境 Python：.venv\\Scripts\\python run_dev.py

启动器会自动检测 .venv：若存在且当前不是 venv 的 Python，则用 venv 的
Python 重新启动自身，确保 run_dev.py 与 app.py 都在虚拟环境下运行。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
APP_SCRIPT = ROOT / "app.py"


def venv_python_is_usable() -> bool:
    """A copied project may carry a venv tied to a removed Python install."""
    if not VENV_PYTHON.is_file():
        return False
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), "--version"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def main() -> int:
    # 若 venv 存在且当前解释器不是 venv 的 Python，则用 venv Python 重启自身
    venv_usable = venv_python_is_usable()
    if venv_usable and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        print(f"[启动器] 检测到虚拟环境，切换到: {VENV_PYTHON}")
        cmd = [str(VENV_PYTHON), str(Path(__file__).resolve())] + sys.argv[1:]
        try:
            return subprocess.call(cmd, cwd=str(ROOT))
        except KeyboardInterrupt:
            return 0

    # 此时已运行在 venv Python 下（或 venv 不存在回退到全局 Python）
    if venv_usable:
        print(f"[启动器] 已在虚拟环境中运行: {sys.executable}")
    else:
        print(f"[启动器] 使用全局 Python: {sys.executable}")
        if VENV_PYTHON.is_file():
            print(f"[启动器] 提示: .venv 已失效，请运行 setup_venv.bat 重建环境")
        else:
            print(f"[启动器] 提示: 未找到 .venv，如需隔离环境请先运行 setup_venv.bat")

    if not APP_SCRIPT.is_file():
        print(f"[错误] 找不到 app.py: {APP_SCRIPT}")
        return 1

    # 启动 app.py，透传所有命令行参数（如 --no-browser --port 8000 等）
    cmd = [sys.executable, str(APP_SCRIPT)] + sys.argv[1:]
    try:
        return subprocess.call(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
