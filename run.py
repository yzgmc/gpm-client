"""客户端启动入口。

支持两种启动模式：
1. 默认模式：启动 PySide6 GUI 客户端
2. --gpm-updater 模式：作为单 exe 自升级辅助进程运行（无 GUI）

单 exe 设计：主 exe 启动时若 argv 含 --gpm-updater，跳过 GUI，转交
app._updater_helper.run_updater() 执行"等主进程退出 → 原子替换 → 重启"。
Nuitka 打包时通过 --include-module=app._updater_helper 确保该模块被嵌入。
"""

from __future__ import annotations

import sys


def main() -> None:
    # 升级模式：跳过 GUI，直接转交升级逻辑
    if "--gpm-updater" in sys.argv:
        from app._updater_helper import run_updater
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--new-exe", required=True)
        parser.add_argument("--current-exe", required=True)
        # 跳过 "--gpm-updater" 自身，避免 argparse 报错
        args = parser.parse_args([a for a in sys.argv[1:] if a != "--gpm-updater"])
        sys.exit(run_updater(args.new_exe, args.current_exe))

    # 正常模式：启动 GUI
    from app.main import main as _main
    _main()


if __name__ == "__main__":
    main()
