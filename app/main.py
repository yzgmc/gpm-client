"""客户端入口：启动 PySide6 应用。

启动流程：
1. 加载本地配置。
2. 若未登录（无 username/token）→ 弹登录对话框，登录成功后持久化。
3. 登录后进入主窗口。
"""

from __future__ import annotations

# 导入 gpm_common 即触发内置适配器注册
import gpm_common  # noqa: F401
import sys

from PySide6.QtWidgets import QApplication

from app.config import ClientConfig
from app.ui.login_dialog import LoginDialog
from app.ui.main_window import MainWindow
from app.ui.theme import apply_dark_theme


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Game Push Manager Client")
    # 应用黑色系高质感主题（全局 QSS + 字体）
    apply_dark_theme(app)

    config = ClientConfig.load()

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
