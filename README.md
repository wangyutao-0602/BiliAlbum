# BiliAlbum

Bilibili 公开视频整理工具，提供 Tk 桌面界面与命令行，将获得下载许可的视频导出为 MP4。

版本：0.1.1。依赖 Python 3.11+、tkinter、yt-dlp 和 FFmpeg。

## 功能

- 支持 HTTPS BV、av、b23.tv 短链接及 UP 主 lists 合集/系列链接。
- 同一 BV 多 P 按 P 序号排列，保留 URL 中的 `?p=N`，下载前核对分集身份。
- 跨 BV 合集读取全部分页并按发布时间排序；日期缺失时保留平台顺序。无法证明列表完整时停止。
- 支持单集、全集和自定义编号，例如 `1,3,5-8`。
- 根据匿名提取结果选择画质，支持所选画质不可用时向下降级。
- 网页优先读取完整初始状态；内嵌 DASH 的 BV/P/CID 可核对时直接使用，否则交给正式 yt-dlp 提取器。
- 输出 H.264 / AAC / yuv420p MP4，支持符合条件时直接封装，以及必要时重新编码。
- 支持进度、取消、有限重试、历史记录和媒体完整性检查。

仅适用于获得权利人许可或有权留存的免费视频。当前不提供 Cookie 登录、会员、付费或 DRM 处理功能。

## macOS 安装与启动

安装带 tkinter 的 Python 3.11+ 及 FFmpeg，例如通过 Homebrew 安装 FFmpeg：

```bash
brew install ffmpeg
```

进入项目目录后运行：

```bash
chmod +x install_macos.sh start_macos.sh 启动BiliAlbum.command
./install_macos.sh
./start_macos.sh
```

之后可以双击 `启动BiliAlbum.command`，或再次运行 `./start_macos.sh`。

## 使用界面

1. 粘贴视频链接，点击「解析网址」，等待分集与画质预检完成。
2. 核对选中分集，选择单集、整个合集或自定义编号。
3. 选择画质、保存位置及是否允许降级。
4. 点击「开始下载」并确认选择；完成后打开输出文件夹。

默认保存到当前用户的 Downloads/Bilibili 文件夹。输出文件可通过 AirDrop 等方式传送到 Apple 设备；具体设备能否导入仍需实机验证。

## 命令行

以下视频 ID 为占位符，请替换为自己的授权视频链接。

```bash
# 只解析列表，不下载
.venv/bin/python main.py --url 'https://www.bilibili.com/video/BVxxxxxxxxxx?p=3' --list

# 默认下载 URL 指定的单集
.venv/bin/python main.py --url 'https://www.bilibili.com/video/BVxxxxxxxxxx?p=3'

# 自定义编号
.venv/bin/python main.py --url 'https://www.bilibili.com/video/BVxxxxxxxxxx' --mode custom --episodes '1,3,5-8' --quality 720

# 忽略本进程的显式代理
./start_macos.sh --direct
```

默认跟随系统/环境代理。「仅本程序直连」只控制程序的显式代理设置，不更改系统代理或 VPN/TUN 路由。

## HTTP 412 与诊断

网站可能拒绝自动请求。网页、元数据 API、播放信息及媒体流的错误会按阶段报告；403/412/429 不会触发整套合集的持续重试。网页本身被拒绝时，程序无法读取内嵌播放数据。更换请求头、添加 Cookie 或启用直连均不能保证恢复访问。

本地只读设置检查：

```bash
.venv/bin/python diagnose_network.py --settings-only
```

按需检查一个具体视频；该命令只获取元数据，不下载媒体：

```bash
.venv/bin/python diagnose_network.py 'https://www.bilibili.com/video/BVxxxxxxxxxx/' --yt-dlp --output network-diagnostics.json
```

拒绝请求后避免连续执行诊断。诊断会隐藏凭据和签名参数，但仍可能包含本机路径、软件环境、代理端口及视频标识；分享前请自行审查。此仓库未包含任何个人实测诊断文件。

## 测试

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q bilialbum main.py diagnose_network.py
```

测试使用模拟网络响应及本地合成媒体，不要求 Bilibili 账号，也不下载真实视频。测试夹具中的视频 ID、账号、凭据及域名均用于测试。

## 已知限制

- 平台可能调整页面、接口或访问策略，无法保证所有链接持续可用。
- 不支持版权番剧、搜索、收藏夹及需登录权限的资源。
- 同一 BV 同时属于跨 BV 合集且具有多个 P 时，其他合集 BV 默认使用 P1；当前链接保留其指定 P。
- 取消操作在网络或 FFmpeg 回调处生效，可能需要等待当前请求结束。
- 本地编码及解码校验不能代替 Mac「照片」或 iPhone/iPad 的实机导入验收。

源码结构与隐私处理说明见 [PRIVACY.md](PRIVACY.md)。
