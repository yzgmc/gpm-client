# gpm-client

Game Push Manager **Windows 客户端**（PySide6 桌面应用）。从服务端同步整合包 / 模组条目，下载并启动游戏。

> 当前已支持 Minecraft 整合包下载与启动。其他游戏只需在 `gpm-common` 注册适配器，客户端无需改动即可识别新游戏条目并下载（启动命令由对应适配器生成）。

## 功能

- **同步更新条目**：调用服务端 `/api/v1/sync`，拉取所有整合包 / 模组 / 支持的游戏列表
- **整合包管理**：浏览、下载（带进度条）、查看本地已安装状态、一键启动
- **模组管理**：浏览、下载到对应整合包的 mods 目录
- **多服务端**：可配置主服务端地址，整合包从该地址下载
- **可扩展游戏**：通过 `gpm-common` 的 `GameAdapterRegistry` 自动支持新游戏

## 安装与运行

```bat
:: 1. 安装 Python 3.10+ 与 gpm-common
pip install -e ..\gpm-common

:: 2. 安装客户端依赖
pip install -r requirements.txt

:: 3. 运行
python run.py
```

首次启动后在「设置」页配置：
- 服务端地址（默认 `http://127.0.0.1:8000`）
- 游戏安装根目录（整合包将解压到 `<根目录>/<game>/<modpack_name>/`）
- Java 可执行文件路径（启动 MC 需要，留空则用系统 PATH 中的 java）

## 配置存储

客户端配置保存在 `data/client_config.json`，包含：
- `server_url`：服务端地址
- `install_base_dir`：安装根目录
- `java_path`：Java 路径
- `jvm_args`：JVM 参数
- `last_sync_at`：上次同步时间

本地已下载条目记录在 `data/installed.json`，用于判断是否已安装、是否需要更新。

## 与服务端的交互流程

1. 启动 / 点击「同步」→ `GET /api/v1/sync` 获取全部条目
2. 客户端比对本地 `installed.json`，标记「可下载」/「已安装」/「有更新」
3. 点击「下载」→ `GET /api/v1/modpacks/{id}/download`，流式写入本地，校验 sha256
4. 解压整合包到安装目录（zip 格式）
5. 点击「启动」→ 调用 `MinecraftAdapter.build_launch_command()` 生成命令并拉起进程

## 目录结构

```
gpm-client/
├── app/
│   ├── main.py            # PySide6 入口
│   ├── config.py          # 配置读写
│   ├── api_client.py      # 服务端 HTTP 客户端
│   ├── sync_manager.py    # 同步与本地状态管理
│   ├── downloader.py      # 流式下载 + 哈希校验
│   ├── installer.py       # 整合包解压安装
│   ├── launcher.py        # 通过适配器启动游戏
│   └── ui/
│       ├── main_window.py # 主窗口（整合包/模组/设置三页）
│       └── widgets.py     # 进度对话框等
├── data/                  # 运行时配置与记录（gitignored）
├── requirements.txt
├── run.py
└── README.md
```
