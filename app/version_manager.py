"""版本管理器：HMCL 风格的多版本隔离管理。

在共享游戏根目录 `<install_base_dir>/minecraft/` 下集中管理多个版本：
  minecraft/
    versions/
      1.20.1/                      # 原版版本（Mojang 标准）
        1.20.1.json
        1.20.1.jar
        gpm_instance.json          # GPM 每版本独立配置
      fabric-loader-0.15.7-1.20.1/
        fabric-loader-0.15.7-1.20.1.json
        gpm_instance.json
    libraries/                     # 跨版本共享（省磁盘）
    assets/                        # 跨版本共享

每个版本可单独启动、互不干扰：
- classpath/libraries/assets 从共享根解析（install_dir = 游戏根目录）
- 存档/模组/配置落在 game_dir：隔离模式 → versions/<id>/，共享模式 → 游戏根目录
- gpm_instance.json 记录该版本的显示名、Java、JVM 参数、隔离开关等独立配置

version_id 即 Mojang 标准版本目录名（原版=mc版本，加载器=安装器生成的标准 id，
如 fabric-loader-0.15.7-1.20.1）。显示名只是友好标签，不参与路径定位。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import httpx
from app.downloader import get_sync_client


# 进度回调签名: (stage, detail, percent)，与 loader_installer / minecraft_installer 一致
ProgressCb = Callable[[str, str, int], None]

# 每版本独立配置文件名
INSTANCE_CONFIG_FILE = "gpm_instance.json"

# 国内镜像版本清单（与 minecraft_installer 同源）
_MANIFEST_URL = "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json"
_HTTP_TIMEOUT = httpx.Timeout(15.0, read=30.0)


@dataclass
class VersionInstance:
    """一个版本实例的完整描述（列表展示与启动都用它）。"""

    version_id: str               # Mojang 版本目录名（如 fabric-loader-0.15.7-1.20.1）
    display_name: str = ""        # 友好显示名，空时用 version_id
    game_version: str = ""        # MC 版本（如 1.20.1）
    mod_loader: str = "vanilla"   # vanilla/fabric/forge/neoforge/quilt
    mod_loader_version: Optional[str] = ""  # 加载器版本（None 会规范化为 "" 避免写损坏 config）
    java_path: str = ""           # 空=继承全局 ClientConfig
    jvm_args: list[str] = field(default_factory=list)  # 空=继承全局
    isolated: bool = True         # True=存档/模组/配置隔离到 versions/<id>/，False=共享根
    last_played: str = ""         # ISO 时间，空=从未启动
    created_at: str = ""          # ISO 时间
    ready: bool = False           # client jar 是否就绪（可启动）
    version_dir: str = ""         # 绝对路径，便于打开目录

    @property
    def effective_display_name(self) -> str:
        return self.display_name or self.version_id


def game_root(install_base_dir: str) -> str:
    """共享游戏根目录（.minecraft 等价物）。

    与整合包安装目录 <install_base_dir>/minecraft/<modpack>/ 同级但互不干扰：
    版本管理只触及 minecraft/ 下的 versions/、libraries/、assets/。
    """
    return os.path.join(install_base_dir, "minecraft")


def version_dir(game_root_dir: str, version_id: str) -> str:
    """版本目录绝对路径。"""
    return os.path.join(game_root_dir, "versions", version_id)


# ============ 每版本独立配置读写 ============

def _default_instance_config(version_id: str, game_version: str, loader: str,
                             loader_version: str, display_name: str = "") -> dict:
    return {
        "version_id": version_id,
        "display_name": display_name,
        "game_version": game_version,
        "mod_loader": loader,
        "mod_loader_version": loader_version or "",
        "java_path": "",
        "jvm_args": [],
        "isolated": True,
        "last_played": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_instance_config(v_dir: str, version_id: str = "") -> dict:
    """读取版本目录下的 gpm_instance.json。不存在则返回空 dict。

    version_id 仅用于在缺失时回填，不影响已存内容。

    注意：旧 config 中 mod_loader_version 可能是 null 或缺失 —— 我们统一规范化为
    ""（避免下游 int / str 处理时崩溃）。
    """
    path = os.path.join(v_dir, INSTANCE_CONFIG_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(_read_text(path))
        if not isinstance(data, dict):
            return {}
        if version_id and not data.get("version_id"):
            data["version_id"] = version_id
        # 规范化 loader_version：None / 缺失 / 非字符串 → ""
        v = data.get("mod_loader_version")
        if v is None or not isinstance(v, str):
            data["mod_loader_version"] = ""
        # 规范化 jvm_args：非 list → []
        if not isinstance(data.get("jvm_args"), list):
            data["jvm_args"] = []
        # 规范化 isolated：非 bool → True
        if not isinstance(data.get("isolated"), bool):
            data["isolated"] = True
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_instance_config(v_dir: str, config: dict) -> None:
    """写入 gpm_instance.json（原子写）。"""
    os.makedirs(v_dir, exist_ok=True)
    path = os.path.join(v_dir, INSTANCE_CONFIG_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update_instance_config(v_dir: str, **updates) -> dict:
    """局部更新 gpm_instance.json：读出现有 → 合并 updates → 写回。返回合并后配置。"""
    cfg = load_instance_config(v_dir)
    cfg.update(updates)
    save_instance_config(v_dir, cfg)
    return cfg


# ============ 扫描版本列表 ============

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _parse_loader_from_version_id(version_id: str, version_json: Optional[dict]) -> tuple[str, str]:
    """从 version_id 与版本 JSON 推断 (loader, loader_version)。

    version_id 典型形式：
      - 1.20.1                                  → (vanilla, "")
      - fabric-loader-0.15.7-1.20.1             → (fabric, "0.15.7")
      - fabric-loader-0.15.7                    → (fabric, "0.15.7")  # 无 mc 段
      - quilt-loader-0.20.0-1.20.4              → (quilt, "0.20.0")
      - 1.20.1-forge-47.2.0                     → (forge, "47.2.0")
      - neoforge-21.0.143                       → (neoforge, "21.0.143")

    旧版用正则 r"fabric-loader-([^/]+?)-(?:\d)" 在没有第三段（无 mc 后缀）的
    version_id 上匹配不到（如 `fabric-loader-0.16.0`），导致 loader_ver 永远
    空串。改用 split 取固定位置段，更稳健。
    """
    vid = (version_id or "").lower().strip()
    if not vid:
        return "vanilla", ""

    # ---- fabric / quilt：prefix-loader-VERSION[-MC] ----
    for loader, prefix in (("fabric", "fabric-loader-"), ("quilt", "quilt-loader-")):
        if vid.startswith(prefix):
            rest = vid[len(prefix):]
            # rest 形如 "0.15.7-1.20.1" 或 "0.15.7"
            parts = rest.split("-")
            if parts and parts[0]:
                return loader, parts[0]
            return loader, ""

    # ---- neoforge：可能是 neoforge-VERSION 或 1.20.1-neoforge-VERSION ----
    if "neoforge" in vid:
        # 取 neoforge- 后第一段作为版本号
        idx = vid.find("neoforge")
        after = vid[idx + len("neoforge"):].lstrip("-")
        if after:
            return "neoforge", after.split("-")[0]
        return "neoforge", ""

    # ---- forge：1.20.1-forge-47.2.0 或 forge-47.2.0 ----
    if "forge" in vid:
        idx = vid.find("forge")
        after = vid[idx + len("forge"):].lstrip("-")
        if after:
            return "forge", after.split("-")[0]
        return "forge", ""

    return "vanilla", ""


def _extract_game_version(version_id: str, version_json: Optional[dict]) -> str:
    """从版本 JSON 的 inheritsFrom 或 jar 元数据推断 MC 版本。"""
    if version_json:
        # inheritsFrom 指向原版版本 id（通常就是 mc 版本）
        inh = version_json.get("inheritsFrom") or ""
        if inh:
            return inh
        # 部分原版 JSON 有 releaseTime/inheritsFrom 缺失时，从 id 取
    vid = version_id or ""
    # fabric-loader-0.15.7-1.20.1 → 1.20.1（取最后一段版本号）
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)$", vid)
    return m.group(1) if m else ""


def list_versions(game_root_dir: str) -> list[VersionInstance]:
    """扫描游戏根目录下的 versions/，返回所有版本实例。

    判定规则：versions/<id>/<id>.json 存在即为有效版本。
    gpm_instance.json 不存在时用默认配置填充（兼容手动放入的原版/加载器版本）。
    """
    versions_dir = os.path.join(game_root_dir, "versions")
    if not os.path.isdir(versions_dir):
        return []
    result: list[VersionInstance] = []
    for name in sorted(os.listdir(versions_dir)):
        v_dir = os.path.join(versions_dir, name)
        if not os.path.isdir(v_dir):
            continue
        json_path = os.path.join(v_dir, f"{name}.json")
        if not os.path.isfile(json_path):
            continue
        # 读取版本 JSON 以推断 loader / game_version
        version_json: Optional[dict] = None
        try:
            version_json = json.loads(_read_text(json_path))
            if not isinstance(version_json, dict):
                version_json = None
        except (OSError, json.JSONDecodeError):
            version_json = None

        loader, loader_ver = _parse_loader_from_version_id(name, version_json)
        game_ver = _extract_game_version(name, version_json)

        cfg = load_instance_config(v_dir, name)
        # gpm_instance.json 缺失或字段不全时回填推断值
        display = cfg.get("display_name", "")
        inst = VersionInstance(
            version_id=cfg.get("version_id", name),
            display_name=display,
            game_version=cfg.get("game_version") or game_ver,
            mod_loader=cfg.get("mod_loader", loader),
            mod_loader_version=cfg.get("mod_loader_version", loader_ver),
            java_path=cfg.get("java_path", ""),
            jvm_args=list(cfg.get("jvm_args", [])),
            isolated=bool(cfg.get("isolated", True)),
            last_played=cfg.get("last_played", ""),
            created_at=cfg.get("created_at", ""),
            ready=_is_version_ready(v_dir, name, loader, version_json),
            version_dir=v_dir,
        )
        result.append(inst)
    return result


def _is_version_ready(v_dir: str, version_id: str, loader: str,
                      version_json: Optional[dict]) -> bool:
    """判断版本是否可启动：原版需 client jar；加载器版本需原版 jar 存在（inheritsFrom）。

    简化：原版 jar = versions/<mc_version>/<mc_version>.jar 存在即可。
    """
    # 加载器版本通过 inheritsFrom 找原版 jar
    if version_json:
        inh = version_json.get("inheritsFrom")
        if inh:
            parent_jar = os.path.join(os.path.dirname(v_dir), inh, f"{inh}.jar")
            if os.path.isfile(parent_jar):
                return True
    # 自身就是原版：检查自己的 jar
    own_jar = os.path.join(v_dir, f"{version_id}.jar")
    return os.path.isfile(own_jar)


def is_version_ready(game_root_dir: str, version_id: str) -> bool:
    """对外快速检查接口。"""
    v_dir = version_dir(game_root_dir, version_id)
    json_path = os.path.join(v_dir, f"{version_id}.json")
    if not os.path.isfile(json_path):
        return False
    try:
        vj = json.loads(_read_text(json_path))
    except (OSError, json.JSONDecodeError):
        vj = None
    return _is_version_ready(v_dir, version_id, "", vj)


# ============ 获取可选 MC 版本列表（新建版本对话框用） ============

def fetch_mc_version_ids(release_only: bool = True) -> list[str]:
    """从版本清单拉取可用 MC 版本 id 列表（release 优先）。失败返回空列表。"""
    try:
        with get_sync_client() as c:
            r = c.get(_MANIFEST_URL)
            r.raise_for_status()
            data = r.json()
        versions = data.get("versions", []) if isinstance(data, dict) else []
        if release_only:
            latest_release = (data.get("latest") or {}).get("release", "")
            ids = [v.get("id", "") for v in versions if v.get("type") == "release"]
        else:
            ids = [v.get("id", "") for v in versions]
        ids = [i for i in ids if i]
        # 最新正式版排最前
        if latest_release in ids:
            ids.remove(latest_release)
            ids.insert(0, latest_release)
        return ids
    except Exception:
        return []


# ============ 创建版本 ============

def create_version(
    install_base_dir: str,
    game_version: str,
    loader: str,
    loader_version: str = "",
    display_name: str = "",
    java_path: str = "",
    isolated: bool = True,
    progress: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """创建一个新版本并安装到共享游戏根目录。返回 version_id。

    流程：
      1. 确保原版 MC 文件齐全（client jar + libraries + assets）
      2. 非 vanilla：安装模组加载器（生成标准 version_id 目录）
      3. 写 gpm_instance.json

    version_id 规则：
      - vanilla → game_version（如 1.20.1）
      - fabric  → fabric-loader-<loader_version>-<mc_version>
      - quilt   → quilt-loader-<loader_version>-<mc_version>
      - forge   → <mc_version>-forge-<forge_version>
      - neoforge→ neoforge-<version>（由安装器决定，这里用约定 id 占位）

    注意：实际 version_id 以加载器安装器在 versions/ 下生成的目录名为准，
    安装后扫描得到真实 id 再写配置。
    """
    from app.minecraft_installer import ensure_vanilla_version
    from app.loader_installer import install_loader, SUPPORTED_LOADERS

    root = game_root(install_base_dir)
    os.makedirs(root, exist_ok=True)
    loader = (loader or "vanilla").lower()

    # 1. 确保原版文件
    if progress:
        progress("download", f"准备原版 Minecraft {game_version} 文件…", 5)
    ensure_vanilla_version(
        install_dir=root,
        mc_version=game_version,
        progress=lambda stage, detail, pct: progress("download", detail, 5 + int(pct * 0.4)) if progress else None,
        cancel_event=cancel_event,
    )

    # 2. 安装加载器（vanilla 跳过）
    if loader != "vanilla":
        if loader not in SUPPORTED_LOADERS:
            raise RuntimeError(f"不支持的加载器: {loader}")
        if not java_path:
            raise RuntimeError("安装模组加载器需要 Java 路径，请在设置中配置或新建版本时指定")
        if progress:
            progress("install", f"安装 {loader.capitalize()} 加载器…", 50)
        install_loader(
            loader=loader,
            install_dir=root,
            mc_version=game_version,
            loader_version=loader_version or None,
            install_base_dir=install_base_dir,
            java_path=java_path,
            progress=lambda stage, detail, pct: progress("install", detail, 50 + int(pct * 0.45)) if progress else None,
            cancel_event=cancel_event,
        )

    # 3. 扫描得到真实 version_id（加载器安装器生成的目录名）
    version_id = _detect_created_version_id(root, game_version, loader, loader_version)

    v_dir = version_dir(root, version_id)
    cfg = _default_instance_config(
        version_id=version_id,
        game_version=game_version,
        loader=loader,
        loader_version=loader_version,
        display_name=display_name,
    )
    cfg["isolated"] = isolated
    save_instance_config(v_dir, cfg)

    if progress:
        progress("done", f"版本 {version_id} 已创建", 100)
    return version_id


def _detect_created_version_id(root: str, game_version: str, loader: str,
                               loader_version: str) -> str:
    """安装后扫描 versions/ 定位新建版本的 id。

    优先按命名约定精确匹配，找不到则取最近修改的匹配前缀目录。
    """
    versions_dir = os.path.join(root, "versions")
    if not os.path.isdir(versions_dir):
        return game_version

    # 命名约定候选
    candidates: list[str] = []
    lv = (loader_version or "").strip()
    if loader == "vanilla":
        candidates = [game_version]
    elif loader == "fabric":
        if lv:
            candidates = [f"fabric-loader-{lv}-{game_version}"]
        candidates += [f"fabric-loader-{game_version}"]  # 兜底
    elif loader == "quilt":
        if lv:
            candidates = [f"quilt-loader-{lv}-{game_version}"]
        candidates += [f"quilt-loader-{game_version}"]
    elif loader == "forge":
        if lv:
            candidates = [f"{game_version}-forge-{lv}"]
        candidates += [f"{game_version}-forge"]
    elif loader == "neoforge":
        if lv:
            candidates = [f"neoforge-{lv}", f"{game_version}-neoforge-{lv}"]
        candidates += ["neoforge"]

    # 精确匹配
    for cand in candidates:
        if os.path.isfile(os.path.join(versions_dir, cand, f"{cand}.json")):
            return cand

    # 前缀模糊匹配，取最近修改的
    prefix_map = {
        "fabric": "fabric-loader",
        "quilt": "quilt-loader",
        "forge": f"{game_version}-forge",
        "neoforge": "neoforge",
        "vanilla": game_version,
    }
    prefix = prefix_map.get(loader, game_version)
    matched = []
    for name in os.listdir(versions_dir):
        if name == game_version and loader == "vanilla":
            return name
        if name.startswith(prefix) and os.path.isfile(
            os.path.join(versions_dir, name, f"{name}.json")
        ):
            try:
                mtime = os.path.getmtime(os.path.join(versions_dir, name))
            except OSError:
                mtime = 0
            matched.append((mtime, name))
    if matched:
        matched.sort(reverse=True)
        return matched[0][1]
    return game_version


# ============ 删除版本 ============

def delete_version(game_root_dir: str, version_id: str) -> None:
    """删除一个版本目录（含 gpm_instance.json、jar、json）。

    仅删 versions/<id>/ 本身，不影响共享的 libraries/assets/其它版本。
    原版版本（被其它加载器版本 inheritsFrom）删除后，依赖它的加载器版本将无法启动。
    """
    import shutil

    v_dir = version_dir(game_root_dir, version_id)
    # 安全检查：只删 versions/ 下的子目录，避免误删/路径穿越（先于存在性检查）
    norm = os.path.normpath(v_dir)
    expected_parent = os.path.normpath(os.path.join(game_root_dir, "versions"))
    if os.path.dirname(norm) != expected_parent:
        raise RuntimeError(f"拒绝删除：路径不在 versions/ 下: {v_dir}")
    if os.path.basename(norm) in ("", ".", "..", "natives"):
        raise RuntimeError(f"拒绝删除非法版本目录名: {version_id}")
    if not os.path.isdir(v_dir):
        return
    shutil.rmtree(v_dir, ignore_errors=False)


# ============ 启动辅助 ============

def resolve_game_dir(root: str, inst: VersionInstance) -> str:
    """根据隔离开关返回该版本启动用的 game_dir。"""
    if inst.isolated:
        return inst.version_dir
    return root


def touch_last_played(v_dir: str) -> None:
    """记录上次启动时间。"""
    update_instance_config(v_dir, last_played=datetime.now().isoformat(timespec="seconds"))
