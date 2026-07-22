"""游戏启动器：通过 gpm-common 的适配器生成启动命令并拉起进程。"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from gpm_common import GameAdapterRegistry, LaunchConfig


def launch(
    game: str,
    install_dir: str,
    modpack_meta: dict,
    java_path: Optional[str] = None,
    jvm_args: Optional[list[str]] = None,
    extra_args: Optional[list[str]] = None,
) -> subprocess.Popen:
    """启动游戏。返回子进程对象。

    game: 游戏标识（如 minecraft）
    install_dir: 整合包安装目录
    modpack_meta: 整合包元数据（含 mod_loader / game_version 等）
    java_path / jvm_args: 来自客户端配置
    """
    adapter = GameAdapterRegistry.require(game)
    config = LaunchConfig(
        java_path=java_path or None,
        jvm_args=list(jvm_args or []),
        extra_args=list(extra_args or []),
    )
    cmd = adapter.build_launch_command(install_dir, config, modpack_meta)
    # 在安装目录下启动，便于相对路径定位
    return subprocess.Popen(cmd, cwd=install_dir)
