"""客户端入口：启动 PySide6 应用。

启动流程：
1. 加载本地配置。
2. 初始化下载缓存（按 SHA1 内容寻址，跨 modpack 复用）。
3. 若未登录（无 username/token）→ 弹登录对话框，登录成功后持久化。
4. 登录后进入主窗口。
"""

from __future__ import annotations

# 导入 gpm_common 即触发内置适配器注册
import gpm_common  # noqa: F401
import sys

from PySide6.QtWidgets import QApplication

from app.config import ClientConfig, DATA_DIR
from app.ui.login_dialog import LoginDialog
from app.ui.main_window import MainWindow
from app.ui.theme import apply_theme


def _init_download_cache() -> None:
    """初始化内容寻址下载缓存。

    路径：<DATA_DIR>/download_cache/。8 GB 上限，自动 LRU 淘汰。
    跨 modpack 复用 + 重新安装已装 modpack 提速的核心。
    """
    try:
        from app import download_cache
        cache_dir = DATA_DIR / "download_cache"
        download_cache.set_cache_dir(cache_dir)
        # 8 GB 容量；用 os 模块读 env 可被运维调整
        import os
        max_gb = float(os.getenv("GPM_CACHE_MAX_GB", "8"))
        download_cache.set_max_bytes(int(max_gb * 1024 * 1024 * 1024))
    except Exception as e:
        # 缓存初始化失败不阻塞启动
        print(f"[warn] download cache init failed: {e}", file=sys.stderr)


def _startup_mirror_probe() -> None:
    """启动时后台测速已知镜像（不阻塞启动）。"""
    try:
        from app import mirror_speed
        mirror_speed.startup_probe()
    except Exception as e:
        print(f"[warn] mirror speed probe failed: {e}", file=sys.stderr)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Game Push Manager Client")

    # 先加载配置再应用主题——按用户上次选择的主题恢复（默认 dark）
    config = ClientConfig.load()

    # 初始化下载缓存（按 SHA1 内容寻址，跨 modpack 复用）
    _init_download_cache()

    # 启动时后台测速已知镜像（不阻塞启动）
    _startup_mirror_probe()

    apply_theme(app, config.theme)

    # 未登录 → 弹登录对话框
    if not config.username or not config.token:
        dlg = LoginDialog(default_server=config.server_url or "http://127.0.0.1:8001")
        if dlg.exec() != LoginDialog.Accepted:
            sys.exit(0)  # 用户取消登录，退出
        config.server_url = dlg.server_url
        config.admin_url = dlg.server_url  # 融合体：同步与上报同一地址
        config.username = dlg.username
        config.token = dlg.token
        config.client_name = dlg.username  # 展示名用用户名
        config.save()

    win = MainWindow(config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
