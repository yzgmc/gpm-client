"""应用版本与更新源配置。

版本号由 CI 在打包时通过环境变量 GPM_APP_VERSION 注入（如 `0.1.0+20260727`）。
本地开发态默认使用 `0.0.0+dev`，方便区分。
"""

from __future__ import annotations

import os

# 应用版本：CI 注入或开发态默认
APP_VERSION: str = os.environ.get("GPM_APP_VERSION", "0.0.0+dev")

# 升级源：GitHub 仓库 owner/repo
GITHUB_OWNER: str = os.environ.get("GPM_GITHUB_OWNER", "yzgmc")
GITHUB_REPO: str = os.environ.get("GPM_GITHUB_REPO", "gpm-client")

# Release 资产名（与 build-release.yml 中的 gpm-client.exe 对应）
UPDATE_ASSET_NAME: str = os.environ.get("GPM_UPDATE_ASSET_NAME", "gpm-client.exe")
