# 局域网视频压缩系统

这是一个面向学习平台上传场景的小型 Web 压缩系统，默认按 `78.8%` 的压缩强度工作，也就是接近 `208M -> 44M` 这一档的体积目标。

系统特点：

- 支持同一局域网内多人同时访问
- 每个浏览器自动分配专属用户 ID，上传、任务列表和下载结果彼此隔离
- 默认最多 `3` 个压缩任务并发执行
- 支持批量上传，自动排队
- 支持 `课件录屏` 和 `通用视频` 两种压缩策略
- 输出固定为 `MP4 / H.264 / AAC`，方便学习平台兼容

## 启动方式

### macOS

- 双击 [start_web.command](/Users/mortysmith/Documents/VideoCompressorSystem/start_web.command)
- 如果要让同事访问，双击 [start_web_lan.command](/Users/mortysmith/Documents/VideoCompressorSystem/start_web_lan.command)

### Windows

- 双击 [start_windows.bat](/Users/mortysmith/Documents/VideoCompressorSystem/start_windows.bat)

## 环境要求

- Python 3
- ffmpeg
- ffprobe

macOS 安装：

```bash
brew install ffmpeg
```

Windows 安装：

```bash
winget install Gyan.FFmpeg
```

## 手动启动

```bash
cd /Users/mortysmith/Documents/VideoCompressorSystem
MAX_CONCURRENT_JOBS=3 python3 web_app.py
```

Windows 上如果没有 `python3`，改成：

```bash
set MAX_CONCURRENT_JOBS=3
python web_app.py
```

## 页面说明

- `目标压缩比例`：默认 `78.8%`，数值越高，体积越小，画质也越容易下降
- `课件录屏`：适合 PPT、录屏、静态讲解视频
- `通用视频`：适合真人、运动画面更多的视频

## 使用建议

- 如果主要是课程录屏、PPT 讲解，优先用 `课件录屏`
- 如果视频有人像、镜头运动，建议用 `通用视频`
- 同事一起用时，不需要分开部署，直接访问同一个局域网地址即可；系统会按浏览器 Cookie 自动隔离用户环境

## 目录

- `static/`：前端页面
- `uploads/<用户ID>/`：当前用户上传原视频
- `outputs/<用户ID>/`：当前用户压缩结果
- `web_app.py`：后端服务
