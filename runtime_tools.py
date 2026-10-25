"""项目 uv 环境提供的工具；不依赖 PATH 中的全局 yt-dlp。"""

import imageio_ffmpeg


def ffmpeg_executable():
    # imageio-ffmpeg 在支持的平台随 wheel 提供二进制，也支持 IMAGEIO_FFMPEG_EXE。
    return imageio_ffmpeg.get_ffmpeg_exe()
