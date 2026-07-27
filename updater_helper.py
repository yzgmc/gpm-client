"""外部 updater 辅助进程：等待主程序退出 → 原子替换 exe → 重启新版本。

触发方式：
  python updater_helper.py --new-exe <新exe路径> --current-exe <当前exe路径>

设计原则：
- 独立进程：与主客户端解耦，不依赖 PySide6/Nuitka bundle
- 原子替换：先备份旧 exe → rename 新 exe 覆盖 → 失败回滚
- 重试机制：Windows 文件占用可能持续几秒，最多重试 10 次
- 自动重启：替换成功后启动新 exe，传递原始命令行参数

安全：
- 仅接受 --new-exe 与 --current-exe 路径参数
- 不联网（避免升级过程中再次发起请求）
- 所有操作在 Windows 上以 os.replace 实现原子性（同分区）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time


def wait_for_exit(pid: int, timeout: float = 30.0, poll: float = 0.3) -> bool:
    """等待指定 PID 进程退出。返回是否在 timeout 内退出。"""
    if sys.platform == "win32":
        # Windows：用 tasklist 查询 PID
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
        # POSIX：kill(pid, 0) 检测进程是否存在
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                # 进程存在但无权限发信号：视为存在
                pass
            time.sleep(poll)
        return False


def atomic_replace(new_path: str, current_path: str, max_retries: int = 10, retry_delay: float = 0.5) -> None:
    """用新 exe 原子替换当前 exe。失败抛出 RuntimeError。

    步骤：
    1. 备份 current -> current.bak
    2. 复制 new -> current（跨分区时 os.replace 会失败，用 shutil.copy2 兜底）
    3. 删除 new（成功后）
    4. 失败时从 backup 回滚
    """
    if not os.path.isfile(new_path):
        raise RuntimeError(f"新 exe 不存在：{new_path}")
    if not os.path.isfile(current_path):
        raise RuntimeError(f"当前 exe 不存在：{current_path}")

    backup_path = current_path + ".bak"
    # 删除旧备份（若存在）
    if os.path.exists(backup_path):
        try:
            os.remove(backup_path)
        except OSError:
            pass

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            # 1. 备份
            shutil.copy2(current_path, backup_path)
            # 2. 替换（同分区 os.replace 原子；跨分区用 copy2 + remove）
            try:
                os.replace(new_path, current_path)
            except OSError:
                # 跨分区 / ReplaceFile 失败：复制后删除
                shutil.copy2(new_path, current_path)
                os.remove(new_path)
            # 3. 删除备份
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass  # 备份残留不影响运行，下次升级会清理
            return
        except (OSError, shutil.Error) as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            # 重试耗尽：尝试回滚
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
    """启动新 exe，detach 当前进程。"""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0x08000000)
    import subprocess

    subprocess.Popen(
        [current_exe],
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="GPM 客户端外部升级器")
    parser.add_argument("--new-exe", required=True, help="下载好的新 exe 路径")
    parser.add_argument("--current-exe", required=True, help="要被替换的当前 exe 路径")
    args = parser.parse_args()

    new_path = os.path.abspath(args.new_exe)
    cur_path = os.path.abspath(args.current_exe)

    # 等主进程退出（最多 30s）
    main_pid = os.getppid()
    if not wait_for_exit(main_pid, timeout=30.0):
        print(f"[updater] 等待主进程 (PID {main_pid}) 退出超时，强制继续", file=sys.stderr)

    # 再保险 sleep 500ms：让 OS 释放文件句柄
    time.sleep(0.5)

    try:
        atomic_replace(new_path, cur_path)
    except RuntimeError as e:
        print(f"[updater] {e}", file=sys.stderr)
        return 1

    # 启动新版本
    restart_exe(cur_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
