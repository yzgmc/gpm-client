"""客户端入口：启动 PySide6 应用。"""

from __future__ import annotations

# 导入 gpm_common 即触发内置适配器注册
import gpm_common  # noqa: F401
import sys

from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Game Push Manager Client")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
