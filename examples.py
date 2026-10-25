#!/usr/bin/env python3
"""
使用示例脚本
演示如何编程方式使用视频下载器
"""

from main import VideoDownloader

# 示例 1: 下载单个视频
def example_single_video():
    """下载单个视频的示例"""
    print("示例 1: 下载单个视频")
    print("-" * 50)
    
    downloader = VideoDownloader()
    
    # 检查依赖
    if not downloader.check_dependencies():
        return
    
    # 视频URL
    urls = [
        "https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_688f0341e4b0694ca0f5034b"
    ]
    
    # 捕获视频（首次运行设置 headless=False 以完成登录）
    downloader.capture_video_urls(urls, headless=False)
    
    # 显示捕获结果
    downloader.show_captured_videos()
    
    # 下载视频
    downloader.download_all()


# 示例 2: 批量下载多个视频
def example_batch_download():
    """批量下载多个视频的示例"""
    print("示例 2: 批量下载多个视频")
    print("-" * 50)
    
    downloader = VideoDownloader()
    
    # 检查依赖
    if not downloader.check_dependencies():
        return
    
    # 多个视频URL
    urls = [
        "https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx1",
        "https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx2",
        "https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx3",
    ]
    
    # 捕获视频（已登录后可使用 headless=True）
    downloader.capture_video_urls(urls, headless=True)
    
    # 保存捕获信息
    downloader.save_captured_info("my_videos.json")
    
    # 下载所有视频
    success, fail = downloader.download_all()
    print(f"\n完成！成功: {success}, 失败: {fail}")


# 示例 3: 仅捕获视频信息，不下载
def example_capture_only():
    """仅捕获视频信息的示例"""
    print("示例 3: 仅捕获视频信息")
    print("-" * 50)
    
    downloader = VideoDownloader()
    
    if not downloader.check_dependencies():
        return
    
    urls = ["https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx"]
    
    # 捕获但不下载
    downloader.capture_video_urls(urls, headless=True)
    
    # 保存为 JSON
    downloader.save_captured_info("captured.json")
    
    print("\n视频信息已保存，稍后可以手动下载")


# 示例 4: 自定义输出目录
def example_custom_output():
    """自定义输出目录的示例"""
    print("示例 4: 自定义输出目录")
    print("-" * 50)
    
    # 自定义输出目录
    downloader = VideoDownloader(
        user_data_dir="./my_browser_data",
        output_dir="./my_videos",
        fragment_concurrency=10,
        memory_cache_mib=256,  # 0 使用原生磁盘分片；默认连续数据每 8 MiB 或 3 秒提交
    )
    
    if not downloader.check_dependencies():
        return
    
    urls = ["https://appezrn4igg1968.h5.xet.citv.cn/p/course/video/v_xxx"]
    
    downloader.capture_video_urls(urls, headless=True)
    downloader.download_all()


def example_interactive_capture():
    """自由浏览、收集多个标签页中的视频，结束后批量下载。"""
    downloader = VideoDownloader()
    if not downloader.check_dependencies():
        return
    downloader.capture_interactive()  # 也可以传入课程首页 URL
    downloader.show_captured_videos()
    # 自动快照路径：downloader.last_capture_file
    downloader.download_all()


def example_resume_capture():
    """复用一次采集记录；签名地址过期时需重新浏览采集。"""
    downloader = VideoDownloader()
    if not downloader.check_dependencies():
        return
    filename = input("输入 capture_sessions 中的 videos.json 路径: ").strip()
    downloader.load_captured_info(filename)
    downloader.show_captured_videos()
    downloader.download_all()


if __name__ == "__main__":
    print("=" * 50)
    print("小鹅通视频下载器 - 使用示例")
    print("=" * 50)
    print("\n请选择要运行的示例:")
    print("1. 下载单个视频")
    print("2. 批量下载多个视频")
    print("3. 仅捕获视频信息")
    print("4. 自定义输出目录")
    print("5. 浏览器自由嗅探并下载")
    print("6. 复用采集记录下载")
    print()
    
    choice = input("请输入选项 (1-6): ").strip()
    
    if choice == "1":
        example_single_video()
    elif choice == "2":
        example_batch_download()
    elif choice == "3":
        example_capture_only()
    elif choice == "4":
        example_custom_output()
    elif choice == "5":
        example_interactive_capture()
    elif choice == "6":
        example_resume_capture()
    else:
        print("无效选项")

