"""整合包安装器：将下载的 zip 解压到安装目录。"""

from __future__ import annotations

import os
import zipfile

from gpm_common import GameAdapterRegistry


def install_modpack(archive_path: str, install_dir: str) -> str:
    """解压整合包到 install_dir，返回安装目录。

    针对 CurseForge 风格（含 overrides/）会把 overrides 内容释放到 install_dir；
    否则直接解压全部内容到 install_dir。
    """
    os.makedirs(install_dir, exist_ok=True)
    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
        has_overrides = any(n.startswith("overrides/") for n in names)
        if has_overrides:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if name.startswith("overrides/"):
                    rel = name[len("overrides/"):]
                    if not rel:
                        continue
                    target = os.path.join(install_dir, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        dst.write(src.read())
        else:
            zf.extractall(install_dir)
    return install_dir


def install_mod(file_path: str, modpack_meta: dict, install_base_dir: str, target_dir: str | None = None) -> str:
    """将单个模组 jar 复制到目标目录。

    - target_dir 给定时：直接复制到该目录（用户"另存为"选择的文件夹）。
    - target_dir 为 None：复制到 modpack_meta 对应整合包的 mods/ 目录。
    返回目标路径。
    """
    import shutil

    if target_dir:
        mods_dir = target_dir
    else:
        game = modpack_meta.get("game", "minecraft")
        adapter = GameAdapterRegistry.get(game)
        if adapter:
            modpack_dir = adapter.install_dir_hint(install_base_dir, modpack_meta)
        else:
            modpack_dir = os.path.join(install_base_dir, game, modpack_meta.get("name", "default"))
        mods_dir = os.path.join(modpack_dir, "mods")
    os.makedirs(mods_dir, exist_ok=True)
    dest = os.path.join(mods_dir, os.path.basename(file_path))
    shutil.copy2(file_path, dest)
    return dest
