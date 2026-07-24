"""游戏启动器：自动内存分配 + JVM 优化参数 + 拉起进程。

内存分配策略：
- 检测系统物理内存总量
- 默认分配可用内存的 ~60%（留 40% 给系统/其他程序），封顶 12G（MC 单机吃不下更多）
- 下限：2G（保证 MC 1.18+ 能跑），上限：12G（过大反而 GC 停顿长）
- 若用户在 jvm_args 里已显式指定 -Xmx/-Xms，则尊重用户配置不覆盖

JVM 优化参数（用户未指定时自动加）：
- -XX:+UseG1GC：G1 垃圾回收器，MC 场景综合最优（JDK 8+ 默认即 G1，显式声明保险）
- -XX:+ParallelRefProcEnabled：并行处理引用，降低 STW
- -XX:MaxGCPauseMillis=50：目标 GC 停顿 50ms，降低卡顿
- -XX:+UnlockExperimentalVMOptions -XX:G1NewSizePercent=20 -XX:G1ReservePercent=20：
  增大年轻代和保留区，MC 频繁分配对象时减少晋升失败引发的 Full GC
- -XX:MaxDirectMemorySize=2G：Netty/原生 IO 直接内存上限
- -XX:+AlwaysPreTouch：启动时预触摸堆内存页，避免运行中缺页卡顿
- -Dsun.java2d.noddraw=true：禁用 DirectDraw，避免部分显卡渲染冲突黑屏
- -Dusing.lwjgl.opengl=true：优先 OpenGL 渲染路径
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from gpm_common import GameAdapterRegistry, LaunchConfig


# JVM 优化参数模板（不含 -Xmx/-Xms，内存单独算）
# MC 实战调优常用项，对 G1GC 场景稳定有效
_JVM_OPT_FLAGS: list[str] = [
    "-XX:+UseG1GC",
    "-XX:+ParallelRefProcEnabled",
    "-XX:MaxGCPauseMillis=50",
    "-XX:+UnlockExperimentalVMOptions",
    "-XX:G1NewSizePercent=20",
    "-XX:G1ReservePercent=20",
    "-XX:MaxDirectMemorySize=2G",
    "-XX:+AlwaysPreTouch",
    "-Dsun.java2d.noddraw=true",
    "-Dusing.lwjgl.opengl=true",
]


def detect_system_memory_mb() -> int:
    """检测系统物理内存总量（MB）。失败返回 0。

    Windows 用 GlobalMemoryStatusEx；Linux 读 /proc/meminfo；其它平台返回 0。
    """
    import sys

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys / 1024 / 1024)
        except Exception:
            return 0
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # MemTotal:       16384000 kB
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) // 1024
        except Exception:
            pass
        return 0
    return 0


def auto_memory_args() -> list[str]:
    """根据系统内存自动计算 -Xmx / -Xms，返回 JVM 参数列表。

    策略：
      总内存的 60% 给 MC，封顶 12G，下限 2G。
      -Xmx（最大堆）= 计算值
      -Xms（初始堆）= -Xmx（启动即分配，避免运行中扩容卡顿；配合 AlwaysPreTouch）

    系统内存检测失败（0）时返回空列表，由调用方用适配器默认值兜底。
    """
    total = detect_system_memory_mb()
    if total <= 0:
        return []
    # 60% 给 MC，留 40% 给系统和其他程序
    alloc = int(total * 0.6)
    # 下限 2G（2048MB），上限 12G（12288MB）
    alloc = max(2048, min(alloc, 12288))
    xmx = f"{alloc}M"
    # -Xms = -Xmx：启动即申请全部堆，避免运行时扩容停顿
    return [f"-Xmx{xmx}", f"-Xms{xmx}"]


def _has_memory_args(jvm_args: list[str]) -> bool:
    """判断 jvm_args 是否已显式指定 -Xmx 或 -Xms。"""
    return any(a.startswith("-Xmx") or a.startswith("-Xms") for a in jvm_args)


def _flag_key(flag: str) -> str:
    """提取 flag 的去重键。

    -XX:MaxGCPauseMillis=50 → -XX:MaxGCPauseMillis
    -XX:+UseG1GC            → -XX:UseG1GC（去掉 +/-，按 GC 选择整体去重）
    -Dsun.java2d.noddraw=true → -Dsun.java2d.noddraw
    返回该 flag 的"类别键"，用于判断用户是否已配置同类参数。
    """
    key = flag.split("=")[0]  # 去掉 =value
    if key.startswith("-XX:+") or key.startswith("-XX:-"):
        # 布尔型：-XX:+UseG1GC / -XX:-UseG1GC → 键为 -XX:UseG1GC
        # GC 选择类整体归一（用户指定任意 GC 即跳过自动 GC）
        bare = key[5:]  # 去掉 -XX:+ / -XX:-
        if bare.startswith("Use") and bare.endswith("GC"):
            return "-XX:GC"
        return "-XX:" + bare
    return key


def build_jvm_args(user_jvm_args: Optional[list[str]] = None) -> list[str]:
    """组装最终 JVM 参数：内存参数 + 优化 flag。

    - 用户已指定 -Xmx/-Xms → 用用户的，不自动算内存
    - 用户未指定 → 自动按系统内存分配
    - 优化 flag：逐项检查，用户已指定同类参数的不重复加（避免冲突）
    """
    user = list(user_jvm_args or [])
    args: list[str] = []

    # 1. 内存参数
    if _has_memory_args(user):
        args.extend([a for a in user if a.startswith("-Xmx") or a.startswith("-Xms")])
    else:
        mem = auto_memory_args()
        if mem:
            args.extend(mem)
        else:
            args.extend(["-Xmx4G", "-Xms4G"])  # 检测失败兜底

    # 2. 优化 flag（用户已指定同类参数则跳过，不冲突）
    user_keys = {_flag_key(a) for a in user}
    for flag in _JVM_OPT_FLAGS:
        if _flag_key(flag) in user_keys:
            continue
        args.append(flag)

    # 3. 追加用户指定的其它参数（排除已处理的内存参数，避免重复）
    for a in user:
        if a.startswith("-Xmx") or a.startswith("-Xms"):
            continue
        args.append(a)

    return args


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
    java_path: 来自客户端配置，None 时适配器自检测
    jvm_args: 用户配置的 JVM 参数（可含 -Xmx/-Xms 和优化 flag）。
              为空或仅含部分时，本函数自动补齐内存分配与优化参数。
    extra_args: 额外启动参数
    """
    # 组装最终 JVM 参数：自动内存分配 + 优化 flag + 用户自定义
    final_jvm_args = build_jvm_args(jvm_args)

    adapter = GameAdapterRegistry.require(game)
    config = LaunchConfig(
        java_path=java_path or None,
        jvm_args=final_jvm_args,
        extra_args=list(extra_args or []),
    )
    cmd = adapter.build_launch_command(install_dir, config, modpack_meta)
    # 在安装目录下启动，便于相对路径定位
    return subprocess.Popen(cmd, cwd=install_dir)
