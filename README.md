# 小鹅通视频批量下载工具 (XET Video Downloader)

一个基于 Playwright 和 yt-dlp 的自动化视频下载工具，专门用于批量下载小鹅通平台的视频课程。

## ✨ 特性

- 🎯 **自动捕获** - 使用 Playwright 自动拦截并捕获 m3u8 视频地址
- 🔐 **登录保持** - 使用持久化浏览器会话，首次登录后自动保存状态
- 📦 **批量下载** - 支持批量处理多个视频URL
- 🌐 **自由嗅探** - 打开浏览器后自由浏览，自动收集多个标签页、弹窗、iframe 中的视频
- 🎬 **清晰度归并** - 优先下载 HLS 主列表；仅遇到小鹅通转码子列表时优先保留 f421220
- ♻️ **采集复用** - 自动保存每次采集记录，可重新载入继续下载
- 📊 **友好界面** - 使用 Rich 库提供美观的终端界面
- 💾 **信息保存** - 可选保存捕获的视频信息为 JSON 文件
- ⚡ **并发下载** - 可配置分片并发，默认 10；视频任务依次执行
- 🧠 **内存分片缓存** - 默认 256 MiB 预算，连续数据批量写盘，缺片不跳过
- 📌 **统一依赖** - yt-dlp、FFmpeg 包、内存检测和浏览器 Python 库由 uv 管理并锁定

## 📋 系统要求

- Python 3.10 或更高版本
- macOS / Linux / Windows
- [uv](https://docs.astral.sh/uv/) - Python 包管理工具
- yt-dlp、psutil、Playwright、Rich 和 imageio-ffmpeg 由 `uv sync` 安装
- imageio-ffmpeg 在支持的平台提供 FFmpeg 二进制；无需单独安装全局 yt-dlp 或 FFmpeg。特殊平台可通过 `IMAGEIO_FFMPEG_EXE` 指定兼容的 FFmpeg 路径。

## 🚀 快速开始

### 1. 安装 uv

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 安装项目依赖

```bash
# 克隆或进入项目目录
cd XET-Video-Downloader

# 使用 uv 安装依赖
uv sync --locked

# 安装 Playwright 浏览器驱动
uv run playwright install chromium
```

### 3. 运行程序

```bash
uv run python main.py
```

## 📖 使用指南

### 交互式使用

运行程序后，按照提示操作：

首先输入**视频输出目录**，直接回车使用 `./downloads`。支持相对路径、绝对路径和 `~/`，不存在的多级目录会自动创建；路径包含空格时直接输入即可，无需加引号。所有来源模式（包括自由嗅探、读取采集记录）都使用本次指定的目录。

1. **输入 URL**
   - 选项 1: 手动输入单个或多个URL（用逗号分隔）
   - 选项 2: 从文本文件读取URL列表
   - 选项 3: 打开浏览器，自由浏览并收集视频（无需预先整理 URL）
   - 选项 4: 读取已保存的采集 JSON，继续下载

2. **选择浏览器模式**
   - 首次运行建议显示浏览器窗口，以便完成登录
   - 后续可以使用无头模式（后台运行）

3. **等待捕获**
   - 程序会自动访问每个URL并捕获 m3u8 地址
   - 捕获成功后会显示视频列表

4. **下载视频**
   - 确认下载后，设置分片并发数（默认 10）和缓存上限（默认 256 MiB，0 使用磁盘分片）
   - 程序依次下载所有视频
   - 视频保存在本次指定的输出目录，默认 `downloads`

### 浏览器自由嗅探（推荐用于批量课程）

运行 `uv run python main.py`，选择 **3**：

1. 起始页面可填课程首页，也可留空后在浏览器地址栏手动访问。
2. 在程序打开的浏览器中登录、切换章节并点击播放；可以打开多个标签页和弹窗。
3. 终端实时显示新增/归并的视频数量。按回车、Ctrl+C 或关闭全部浏览器窗口结束收集。
4. 查看列表，选择是否另外导出 JSON、是否开始批量下载。

监听范围是本次程序启动的浏览器会话，不会接管其他已打开的浏览器窗口。支持 HLS/m3u8 和直接 MP4 请求；不会自动遍历未打开的课程，通常需要点击播放才能触发媒体请求。`blob:` 本身不是下载地址，程序收集的是播放器实际请求的媒体资源。暂不支持 DASH/MPD 或 DRM 视频。

同一视频更换已知签名参数时会更新地址；HLS 主列表和它引用的子列表、音轨会归并，分片不会作为独立视频下载。无法确认关系的资源会分别保留，避免误删不同视频；并非所有平台的清晰度或签名规则都能自动识别。

### 自动保存和复用采集记录

每次采集会创建独立目录，不覆盖上一次记录：

```text
capture_sessions/20261024_153000_ab12cd34/
├── videos.json    # 视频地址、来源页面、标题、捕获时间和必要请求头
└── cookies.txt    # 提供给 yt-dlp 的登录 Cookie
```

有新增或更新时自动保存，正常结束时再保存一次。下次选择 **4**，输入该 `videos.json` 路径，即可继续下载；也兼容原来的 `captured_videos.json`。记录中的 Cookie 文件路径是绝对路径，移动项目后需要相应更新路径或重新采集。

这些本地文件包含签名地址和登录信息，已加入 `.gitignore`，请保留在本机。中间文件可以复用，但无法延长平台签名的有效期；地址过期时需重新打开来源页面播放并采集。依赖分区 Cookie 或其他特殊鉴权的网站可能仍无法直接下载。

文件名使用 `标题_视频标识.mp4`，避免同名章节互相覆盖。最高质量依赖实际捕获到的主列表或清晰度请求，建议在播放器中选择所需清晰度后再结束采集。

### 从文件批量下载

创建 `urls.txt` 文件，每行一个URL：

```text
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx1
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx2
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx3
```

然后运行程序，选择选项 2，输入文件路径。

## 🔧 配置说明

### 目录结构

```
fetch-video/
├── main.py                    # 交互入口、浏览器生命周期和下载
├── capture.py                 # 媒体识别、去重、会话监听和快照
├── download_worker.py         # uv 环境中的独立下载进程
├── memory_hls.py              # 有界内存分片和可恢复写入
├── runtime_tools.py           # FFmpeg 二进制定位
├── uv.lock                    # 可复现的依赖锁定
├── tests/test_capture.py      # 单元与浏览器集成测试
├── pyproject.toml            # 项目配置（uv 管理）
├── README.md                 # 说明文档
├── .gitignore               # Git 忽略文件
├── browser_session/         # 浏览器会话数据（自动生成）
├── downloads/               # 视频下载目录（自动生成）
├── capture_sessions/        # 每次采集的可复用记录（自动生成）
└── captured_videos.json     # 捕获的视频信息（可选生成）
```

### 自定义配置

您可以在代码中修改以下参数：

```python
# main.py 中的 VideoDownloader 类初始化
downloader = VideoDownloader(
    user_data_dir="./browser_session",  # 浏览器会话目录
    output_dir="./downloads"             # 视频输出目录
)
```

## 🛠️ 高级用法

### 仅捕获不下载

程序支持先捕获视频信息，保存为 JSON，稍后再下载：

```python
from main import VideoDownloader

downloader = VideoDownloader()
urls = ["https://..."]
downloader.capture_video_urls(urls, headless=True)
downloader.save_captured_info("videos.json")
```

### 有界内存下载与续传

默认使用内存缓存下载 HLS 点播视频：

- 分片直接读入内存，不创建逐片的 `-Frag` 小文件。
- 默认并发 **10**，最多领先 **20** 片；前面的片段缺失时，后面的任务不会无限排队。
- 缓存预算默认 **256 MiB**，实际不超过下载子进程启动时系统可用内存的 **1/8**。该值是媒体缓存预算，不是整个 Python 进程的 RSS 上限。
- 连续数据通过文件缓冲器顺序追加，每 **8 MiB** 或 **3 秒**提交一次。先同步数据，再原子保存续传记录。
- 每片默认最多重试 5 次；重试后仍失败会停止当前视频并保留连续进度，**不跳过缺片生成不完整视频**。
- 文件预计大小不作为硬性条件，因此估算不准或视频超过 1 GiB 也不会导致缓存随视频大小增长。

下载中只需保留同名的两个主要文件：

```text
视频标题_标识.mp4.mem.part   # 已按顺序写入的视频数据
视频标题_标识.mp4.mem.json   # 列表指纹、已提交分片数和字节偏移
```

同一视频使用同一输出目录重试即可续传。恢复时会截去尚未提交的尾部；签名参数更新仍可恢复，但清晰度/分片列表变化或记录损坏时会拒绝混写并保留文件，请改用其他输出目录重新下载。完成后生成最终视频并删除这两个文件；必要的容器封装仍由 FFmpeg 完成。

低于 32 MiB 的有效预算、单片超出缓存槽位时回退到原生磁盘分片模式。单片槽位会按预算和并发数计算，预留解密/读取副本空间；大分片回退时可能重新下载部分内容。已有旧版 `.part` 下载进度也沿用原生方式恢复，不与新的 `.mem.part` 混写。直接 MP4、直播或 yt-dlp 不支持原生处理的格式沿用其下载路径，内存缓存仅用于受支持的 HLS 点播。

程序中也可配置：

```python
from main import VideoDownloader

downloader = VideoDownloader(
    output_dir="./my_videos",
    fragment_concurrency=10,  # 1～32
    memory_cache_mib=256,     # 0 关闭内存模式
)
```

批量任务仍逐个执行，避免同时给多个视频分配缓存。网络限速时增加并发未必提速，优先观察实际吞吐量和重试次数。

### 依赖锁定与独立下载进程

`uv.lock` 纳入版本管理。下载始终通过当前 Python 启动 `download_worker.py`，使用项目环境中的 yt-dlp，不读取全局 yt-dlp CLI 配置，也不会改变当前已经运行的下载进程。

内存下载器复用了 yt-dlp 的内部 HLS 接口，因此将版本固定为 `2026.10.21`。升级 yt-dlp 时需同步验证接口兼容性、AES-128、字节范围、续传和 MP4 容器修复。

## ⚠️ 注意事项

1. **首次登录**
   - 首次运行请选择"显示浏览器窗口"
   - 在弹出的浏览器中完成微信/手机号登录
   - 登录后会自动保存在 `browser_session` 目录

2. **多清晰度**
   - 不再要求地址包含 `sign=` 或 `v.f421220`，普通 m3u8 也能采集
   - 优先保留主列表，由 yt-dlp 选择最高可用清晰度；只有子列表时保留实际捕获的版本

3. **网络问题**
   - 确保网络稳定
   - 下载失败可以重新运行程序，已下载的视频不会重复下载

4. **法律声明**
   - 仅用于下载您已购买/有权访问的课程
   - 请勿用于侵权目的

## 🐛 故障排除

### 问题: 未捕获到视频地址

**解决方案:**
- 确保已登录（使用显示浏览器模式）
- 在 URL 列表模式中增加每页等待时间，或使用自由嗅探模式手动播放
- 检查 URL 是否正确

### 问题: 下载失败

**解决方案:**
- 执行 `uv sync --locked`，确认通过 `uv run python main.py` 启动
- 查看错误信息，可能是网络问题
- 缺片失败可读取采集记录重试；签名过期需重新采集；内存模式不适用时将缓存设置为 0

### 问题: Playwright 浏览器未安装

**解决方案:**
```bash
uv run playwright install chromium
```

## 📝 示例

### 示例 1: 下载单个视频

```bash
$ uv run python main.py
# 选择选项 1: 手动输入
# 输入 URL: https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx
# 选择显示浏览器: 是
# 等待捕获...
# 确认下载: 是
```

### 示例 2: 批量下载

创建 `urls.txt`:
```
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx1
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx2
```

运行:
```bash
$ uv run python main.py
# 选择选项 2: 从文件读取
# 输入文件路径: urls.txt
# 选择显示浏览器: 否（如已登录）
# 等待捕获...
# 确认下载: 是
```

## 🧪 验证

执行 `uv sync --locked` 并安装 Chromium 后运行（无需全局 yt-dlp/ffmpeg/ffprobe）：

```bash
uv run python -m unittest discover -s tests -v
```

测试使用本地 HTTP 服务和无头 Chromium，覆盖多标签页/弹窗/iframe、章节切换、去重、回车/Ctrl+C/关闭浏览器、旧模式和 JSON 复用；并生成短视频，验证携带 Cookie、Referer、User-Agent 的真实 yt-dlp 下载。另外验证缺片重试、乱序窗口上限、AES-128 隐式 IV、字节范围、崩溃尾部回滚和超大分片回退。临时数据仅写入项目内 `.test-artifacts/`，测试结束自动清理，不访问真实课程账号。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License

## 🙏 致谢

- [Playwright](https://playwright.dev/) - 浏览器自动化
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - 视频下载
- [Rich](https://github.com/Textualize/rich) - 终端美化

## 📧 联系方式

如有问题或建议，请提交 Issue。

---

**Happy Downloading! 🎉**

