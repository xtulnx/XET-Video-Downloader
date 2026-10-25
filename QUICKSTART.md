# 快速开始指南

## 🚀 5分钟快速上手

### 步骤 1: 安装依赖

```bash
# 运行自动设置脚本
./setup.sh
```

或者手动安装：

```bash
# 1. 安装项目依赖，包含 yt-dlp 和 FFmpeg 包
uv sync --locked

# 2. 安装浏览器驱动
uv run playwright install chromium
```

### 步骤 2: 选择采集方式

**推荐：不准备 URL，直接浏览并收集**
- 运行程序选择 **3**，可选填写课程首页，也可以留空。
- 在打开的浏览器中登录，逐个打开章节并点击播放；支持多个标签页。
- 回到终端按回车、Ctrl+C 或关闭全部浏览器窗口结束采集，然后确认批量下载。
- 每次记录自动保存在 `capture_sessions/<时间_标识>/videos.json`。下次选择 **4**，输入该路径可继续下载；签名过期需重新采集。
- 只收集实际触发请求的视频，不会自动遍历未打开的章节。

**也可以使用原有 URL 模式：**

**方式 A: 单个视频**
- 直接在程序中输入URL

**方式 B: 批量下载**
- 创建 `urls.txt` 文件（可参考 `urls.txt.example`）
- 每行一个URL

```txt
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx1
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx2
```

### 步骤 3: 运行程序

```bash
uv run python main.py
```

启动后先输入**视频输出目录**，回车默认使用 `./downloads`。也可输入 `./课程视频` 或绝对路径，不存在的目录会自动创建；自由嗅探和读取历史记录下载同样使用此目录。

### 步骤 4: 首次登录

- 第一次运行选择"是"显示浏览器窗口
- 在弹出的浏览器中扫码或手机号登录
- 登录信息会保存在 `browser_session` 目录
- 后续运行可以选择"否"使用无头模式

### 步骤 5: 等待下载

- 程序会自动捕获视频地址
- 显示视频列表确认
- 确认下载后设置分片并发数（默认 10）和内存缓存上限（默认 256 MiB）
- 自动下载到指定目录，默认 `downloads`；连续分片每 8 MiB 或 3 秒提交一次
- 缺片重试失败会保留 `.mem.part` 和 `.mem.json`，同一输出目录重试可续传；缓存设为 0 可用原生磁盘模式

## 📝 使用示例

### 示例 1: 下载单个视频

```bash
$ uv run python main.py

# 输入选项
选择: 1 (手动输入)
URL: https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx
显示浏览器: 是 (首次)
```

### 示例 2: 批量下载课程

创建 `my_course.txt`:
```
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx1
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx2
https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx3
```

运行:
```bash
$ uv run python main.py

选择: 2 (从文件读取)
文件路径: my_course.txt
显示浏览器: 否 (已登录)
```

### 示例 3: 编程方式使用

查看 `examples.py` 获取更多示例：

```bash
uv run python examples.py
```

## 🔧 常见问题

### Q1: 未捕获到视频地址？

**A:** 
- 确保已登录（首次使用显示浏览器）
- 检查URL是否正确
- 增加等待时间

### Q2: 下载失败？

**A:**
- 同步依赖：`uv sync --locked`；检查版本：`uv run python -m yt_dlp --version`
- 检查网络连接
- 查看错误信息

### Q3: 视频质量不是最高清？

**A:**
- 捕获主播放列表后，由 yt-dlp 选择最高可用清晰度
- 仅有子列表时，需在播放器切换清晰度触发请求；小鹅通已知格式优先保留 `v.f421220`

### Q4: 如何删除登录信息重新登录？

**A:**
```bash
rm -rf browser_session/
```

## 📂 输出文件说明

```
fetch-video/
├── browser_session/          # 浏览器登录会话
├── downloads/                # 下载的视频文件
│   ├── 视频标题1_视频标识1.mp4
│   ├── 视频标题2_视频标识2.mp4
│   └── ...
├── capture_sessions/        # 自动保存的采集记录和 Cookie
└── captured_videos.json     # 捕获的视频信息（可选）
```

## 💡 进阶技巧

### 1. 只捕获不下载

采集结束时选择"否"跳过下载即可。自动保存的 JSON 可通过选项 4 重新载入。

### 2. 自定义文件名

修改 `main.py` 中的 `output_template`：

```python
output_template = str(self.output_dir / f"{safe_title}.%(ext)s")
```

### 3. 调整下载并发和缓存

确认下载后直接输入并发数和缓存上限，无需改代码。默认 10 个并发分片、256 MiB 缓存，每个视频单独处理。缓存还会受系统可用内存的 1/8 限制；并发增加时，单片可用的缓存槽位会相应减小。建议先用默认值观察速度，网络限速时提高并发不一定有效。

### 4. 保留续传文件

下载中断后不要删除 `.mem.part` 和 `.mem.json`。选择 4 载入采集记录，使用相同输出目录重试。若签名已过期，先重新采集；若播放列表本身已改变，程序会拒绝将不同内容混写，需要换一个输出目录重新下载。

## 🎯 最佳实践

1. **首次使用**
   - 显示浏览器完成登录
   - 测试单个视频

2. **批量下载**
   - 使用无头模式
   - 准备好URL列表文件

3. **网络优化**
   - 稳定的网络环境
   - 适当的并发数

4. **保存记录**
   - 导出 JSON 文件
   - 便于后续管理

## ⚡ 快捷命令

```bash
# 快速设置
./setup.sh

# 运行主程序
uv run python main.py

# 运行示例
uv run python examples.py

# 清理下载
rm -rf downloads/*

# 重新登录
rm -rf browser_session/
```

## 📞 需要帮助？

- 查看 [README.md](README.md) 获取完整文档
- 检查 [examples.py](examples.py) 获取代码示例
- 提交 Issue 报告问题

---

**开始您的第一次下载吧！** 🎉

