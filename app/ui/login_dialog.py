"""登录对话框：客户端启动时要求用户登录服务端账号。

登录成功后返回 (server_url, username, token)，由调用方持久化到 config。
不分 admin / 普通用户，所有账号均可登录客户端。
"""

from __future__ import annotations

import httpx
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class LoginDialog(QDialog):
    """登录对话框，返回 (server_url, username, token)。"""

    def __init__(self, default_server: str = "http://127.0.0.1:8001", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GPM 客户端 · 登录")
        self.setModal(True)
        self.setMinimumWidth(380)

        self._server_url = ""
        self._username = ""
        self._token = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("登录到 GPM 服务端")
        title.setObjectName("title")
        layout.addWidget(title)

        hint = QLabel("使用服务端账号登录，同一账号在后台显示为一个客户端。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)
        self._edit_server = QLineEdit(default_server)
        self._edit_server.setPlaceholderText("http://服务器IP:8001")
        self._edit_user = QLineEdit()
        self._edit_user.setPlaceholderText("admin")
        self._edit_pass = QLineEdit()
        self._edit_pass.setPlaceholderText("密码")
        self._edit_pass.setEchoMode(QLineEdit.Password)
        form.addRow("服务端地址", self._edit_server)
        form.addRow("用户名", self._edit_user)
        form.addRow("密码", self._edit_pass)
        layout.addLayout(form)

        self._msg = QLabel("")
        self._msg.setObjectName("error")
        self._msg.setWordWrap(True)
        layout.addWidget(self._msg)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self._btn_login = QPushButton("登录")
        self._btn_login.setObjectName("primary")
        self._btn_login.setMinimumHeight(38)
        self._btn_login.clicked.connect(self._on_login)
        btn_row.addWidget(self._btn_login)
        layout.addLayout(btn_row)

        # 回车触发登录
        self._edit_pass.returnPressed.connect(self._on_login)
        self._edit_user.returnPressed.connect(lambda: self._edit_pass.setFocus())

    def _on_login(self) -> None:
        server = self._edit_server.text().strip().rstrip("/")
        user = self._edit_user.text().strip()
        pwd = self._edit_pass.text()
        if not server or not user or not pwd:
            self._msg.setText("请填写完整：服务端地址、用户名、密码")
            return

        self._btn_login.setEnabled(False)
        self._btn_login.setText("登录中…")
        self._msg.setText("")

        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.post(
                    f"{server}/api/v1/auth/login",
                    json={"username": user, "password": pwd},
                )
            if r.status_code == 401:
                self._msg.setText("用户名或密码错误")
                return
            if r.status_code >= 400:
                self._msg.setText(f"登录失败 (HTTP {r.status_code})")
                return
            data = r.json()
            token = data.get("token", "")
            if not token:
                self._msg.setText("服务端未返回 token")
                return
            self._server_url = server
            self._username = user
            self._token = token
            self.accept()
        except httpx.ConnectError:
            self._msg.setText(f"无法连接服务器：{server}，请检查地址")
        except Exception as e:  # noqa: BLE001
            self._msg.setText(f"登录出错：{type(e).__name__}: {e}")
        finally:
            self._btn_login.setEnabled(True)
            self._btn_login.setText("登录")

    @property
    def server_url(self) -> str:
        return self._server_url

    @property
    def username(self) -> str:
        return self._username

    @property
    def token(self) -> str:
        return self._token
