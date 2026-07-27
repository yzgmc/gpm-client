"""单 exe 自升级辅助模块（嵌入到主 exe，无独立 .exe 文件）。

设计：主 exe 启动时通过 `sys.argv[1] == "--gpm-updater"` 检测当前是否为升级模式。
升级模式时跳过 GUI 初始化，直接执行替换+重启，然后退出。

Nuitka 打包时通过 --include-module=app._updater_helper 确保本模块被嵌入主 exe。

调用方式（主进程 → 启动自身作为升级器）：
    cmd = [sys.executable, "--gpm-updater",
           "--new-exe", <下载的新exe路径>,
           "--current-exe", <要被替换的当前exe路径>]
    subprocess.Popen(cmd, ...)
    os._exit(0)  # 主进程立即退出，释放 exe 文件锁
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from typing import Optional


def wait_for_exit(pid: int, timeout: float = 30.0, poll: float = 0.3) -> bool:
    """等待指定 PID 进程退出。返回是否在 timeout 内退出。"""
    if sys.platform == "win32":
        import subprocess

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if str(pid) not in r.stdout:
                    return True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(poll)
        return False
    else:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass
            time.sleep(poll)
        return False


def atomic_replace(new_path: str, current_path: str, max_retries: int = 10, retry_delay: float = 0.5) -> None:
    """用新 exe 原子替换当前 exe。失败抛出 RuntimeError。"""
    if not os.path.isfile(new_path):
        raise RuntimeError(f"新 exe 不存在：{new_path}")
    if not os.path.isfile(current_path):
        raise RuntimeError(f"当前 exe 不存在：{current_path}")

    backup_path = current_path + ".bak"
    if os.path.exists(backup_path):
        try:
            os.remove(backup_path)
        except OSError:
            pass

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            shutil.copy2(current_path, backup_path)
            try:
                os.replace(new_path, current_path)
            except OSError:
                shutil.copy2(new_path, current_path)
                os.remove(new_path)
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
            return
        except (OSError, shutil.Error) as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            try:
                if os.path.exists(backup_path) and not os.path.exists(current_path):
                    shutil.copy2(backup_path, current_path)
            except OSError:
                pass
            break

    raise RuntimeError(
        f"替换 exe 失败（已重试 {max_retries} 次）：{last_err}\n"
        f"新版本仍在：{new_path}\n"
        f"当前版本（可能已损坏）已从备份恢复。"
    )


def restart_exe(current_exe: str) -> None:
    """启动新 exe（detach）。"""
    import subprocess

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    subprocess.Popen(
        [current_exe],
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_updater(new_exe: str, current_exe: str) -> int:
    """升级器入口：等主进程退出 → 原子替换 → 重启新版本。

    返回 0 = 成功；非 0 = 失败（错误已打印到 stderr）。
    """
    new_path = os.path.abspath(new_exe)
    cur_path = os.path.abspath(current_exe)

    main_pid = os.getppid()
    if not wait_for_exit(main_pid, timeout=30.0):
        print(f"[updater] 等待主进程 (PID {main_pid}) 退出超时，强制继续", file=sys.stderr)

    time.sleep(0.5)

    try:
        atomic_replace(new_path, cur_path)
    except RuntimeError as e:
        print(f"[updater] {e}", file=sys.stderr)
        return 1

    restart_exe(cur_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GPM 客户端自升级器（嵌入主 exe）")
    parser.add_argument("--new-exe", required=True, help="下载好的新 exe 路径")
    parser.add_argument("--current-exe", required=True, help="要被替换的当前 exe 路径")
    args = parser.parse_args()
    return run_updater(args.new_exe, args.current_exe)


if __name__ == "__main__":
    sys.exit(main())
