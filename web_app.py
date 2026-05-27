#!/usr/bin/env python3
import cgi
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
CLIENT_COOKIE = "vcs_client_id"

CONTENT_PROFILES = {
    "slides": {
        "name": "课件录屏",
        "description": "适合 PPT、录屏、讲解课件，优先兼顾体积和可读性。",
    },
    "general": {
        "name": "通用视频",
        "description": "适合真人讲课、演示或运动较多的视频，保守一些。",
    },
}

DEFAULT_TARGET_REDUCTION = float(os.environ.get("DEFAULT_TARGET_REDUCTION", "78.8"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))
JOB_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_JOBS)

JOBS = {}
JOBS_LOCK = threading.Lock()
PROCESSES = {}
PROCESSES_LOCK = threading.Lock()


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def parse_fraction(value):
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            numerator = float(left)
            denominator = float(right)
            return numerator / denominator if denominator else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def round_even(value):
    rounded = int(round(value))
    if rounded % 2:
        rounded += 1 if value >= rounded else -1
    return max(rounded, 2)


def safe_name(filename):
    name = Path(filename).name.strip() or "video.mp4"
    keep = []
    for char in name:
        if char.isalnum() or char in "._- ()[]":
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep)


def normalize_client_id(value):
    if not value:
        return None
    value = value.strip()
    try:
        return uuid.UUID(value).hex
    except (ValueError, AttributeError):
        return None


def client_dirs(client_id):
    return UPLOAD_DIR / client_id, OUTPUT_DIR / client_id


def json_response(handler, data, status=HTTPStatus.OK):
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def file_size(path):
    if not path.exists():
        return None
    return path.stat().st_size


def set_job(job_id, **updates):
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {})
        job.update(updates)


def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def list_jobs(client_id=None):
    with JOBS_LOCK:
        jobs = [
            dict(job)
            for job in JOBS.values()
            if client_id is None or job.get("client_id") == client_id
        ]
    jobs.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return jobs


def set_process(job_id, process):
    with PROCESSES_LOCK:
        if process is None:
            PROCESSES.pop(job_id, None)
        else:
            PROCESSES[job_id] = process


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def probe_media(path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    format_info = payload.get("format", {})
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration = float(
        format_info.get("duration")
        or video_stream.get("duration")
        or audio_stream.get("duration")
        or 0
    )
    return {
        "duration": duration,
        "bit_rate": int(float(format_info.get("bit_rate") or 0)),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "video_codec": video_stream.get("codec_name") or "",
        "audio_codec": audio_stream.get("codec_name") or "",
        "video_bitrate": int(float(video_stream.get("bit_rate") or 0)),
        "audio_bitrate": int(float(audio_stream.get("bit_rate") or 0)),
        "fps": parse_fraction(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
    }


def pick_audio_kbps(meta, target_reduction, profile_key):
    source_audio = int(round((meta.get("audio_bitrate") or 0) / 1000)) or 96
    duration = meta.get("duration") or 0

    if profile_key == "slides":
        if duration >= 5400:
            target = 24
        elif duration >= 1800:
            target = 28
        else:
            target = 32
    else:
        if target_reduction >= 78:
            target = 64
        elif target_reduction >= 70:
            target = 80
        else:
            target = 96

    return int(clamp(min(source_audio, target), 24, 128))


def minimum_video_kbps(width, height, fps, profile_key):
    area_factor = max((width * height) / float(1280 * 720), 0.35)
    effective_fps = fps or 24
    fps_factor = max(effective_fps / 24.0, 0.2 if profile_key == "slides" else 0.6)
    base = 32 if profile_key == "slides" else 280
    floor = 18 if profile_key == "slides" else 180
    return max(int(base * area_factor * fps_factor), floor)


def pick_scale(meta, target_reduction, profile_key):
    width = meta.get("width") or 0
    height = meta.get("height") or 0
    if not width or not height:
        return 0, 0, False

    if profile_key == "slides":
        if target_reduction >= 80:
            scale = 0.58
        elif target_reduction >= 76:
            scale = 0.60
        elif target_reduction >= 72:
            scale = 0.67
        elif target_reduction >= 66:
            scale = 0.75
        elif target_reduction >= 58:
            scale = 0.84
        else:
            scale = 1.0
    else:
        if target_reduction >= 80:
            scale = 0.70
        elif target_reduction >= 76:
            scale = 0.76
        elif target_reduction >= 72:
            scale = 0.82
        elif target_reduction >= 66:
            scale = 0.90
        elif target_reduction >= 58:
            scale = 0.95
        else:
            scale = 1.0

    if max(width, height) <= 1280 and target_reduction < 75:
        scale = max(scale, 0.95)

    target_width = round_even(width * scale)
    target_height = round_even(height * scale)
    resized = target_width != width or target_height != height
    return target_width, target_height, resized


def pick_output_fps(meta, profile_key):
    fps = meta.get("fps") or 0
    if profile_key != "slides":
        return None
    if 0 < fps <= 2.2:
        return 1
    if fps and fps <= 5:
        return 2
    return None


def estimate_output_size(duration, video_kbps, audio_kbps):
    if not duration:
        return 0
    return int(duration * (video_kbps + audio_kbps) * 1000 / 8)


def build_strategy(meta, input_size, target_reduction, profile_key):
    reduction = clamp(float(target_reduction or DEFAULT_TARGET_REDUCTION), 45.0, 90.0)
    duration = max(meta.get("duration") or 0, 1)
    remain_factor = max(0.08, 1 - reduction / 100.0)
    target_size = int(input_size * remain_factor)
    target_total_kbps = max(int((target_size * 8) / duration / 1000), 28)
    preferred_audio_kbps = pick_audio_kbps(meta, reduction, profile_key)
    audio_kbps = min(preferred_audio_kbps, max(target_total_kbps - 12, 16))
    video_kbps = max(target_total_kbps - audio_kbps, 12)
    width, height, resized = pick_scale(meta, reduction, profile_key)
    output_fps = pick_output_fps(meta, profile_key)

    check_fps = output_fps or meta.get("fps") or 24
    while width and height and video_kbps < minimum_video_kbps(width, height, check_fps, profile_key):
        if width <= 426 or height <= 240:
            break
        width = round_even(width * 0.92)
        height = round_even(height * 0.92)
        resized = True

    estimated_size = estimate_output_size(duration, video_kbps, audio_kbps)
    quality_floor_kbps = minimum_video_kbps(width or 640, height or 360, check_fps, profile_key)

    if profile_key == "slides":
        encoder_preset = "veryslow"
        tune = "stillimage"
    else:
        encoder_preset = "slow"
        tune = None

    return {
        "profile_key": profile_key,
        "profile_name": CONTENT_PROFILES[profile_key]["name"],
        "target_reduction": round(reduction, 1),
        "target_size": target_size,
        "estimated_size": estimated_size,
        "target_total_kbps": int(target_total_kbps),
        "width": width,
        "height": height,
        "resized": resized,
        "output_fps": output_fps,
        "video_kbps": int(video_kbps),
        "audio_kbps": int(audio_kbps),
        "quality_floor_met": video_kbps >= quality_floor_kbps,
        "encoder_preset": encoder_preset,
        "tune": tune,
    }


def build_ffmpeg_command(input_path, output_path, strategy):
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
    ]

    filters = []
    if strategy["resized"] and strategy["width"] and strategy["height"]:
        filters.append(f"scale={strategy['width']}:{strategy['height']}:flags=lanczos")
        filters.append("setsar=1")
    if filters:
        cmd.extend(["-vf", ",".join(filters)])
    if strategy["output_fps"]:
        cmd.extend(["-r", str(strategy["output_fps"])])

    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-b:v",
            f"{strategy['video_kbps']}k",
            "-maxrate",
            f"{max(int(strategy['video_kbps'] * 1.12), strategy['video_kbps'])}k",
            "-bufsize",
            f"{max(int(strategy['video_kbps'] * 2), strategy['video_kbps'])}k",
            "-preset",
            strategy["encoder_preset"],
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    )
    if strategy["tune"]:
        cmd.extend(["-tune", strategy["tune"]])

    cmd.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            f"{strategy['audio_kbps']}k",
            "-ac",
            "2",
            "-progress",
            "pipe:1",
            "-nostats",
            "-loglevel",
            "error",
            str(output_path),
        ]
    )
    return cmd


def snapshot_summary(jobs):
    running = sum(1 for job in jobs if job.get("status") == "running")
    queued = sum(1 for job in jobs if job.get("status") == "queued")
    finished = sum(1 for job in jobs if job.get("status") == "done")
    failed = sum(1 for job in jobs if job.get("status") == "failed")
    stopped = sum(1 for job in jobs if job.get("status") == "stopped")
    return {
        "total": len(jobs),
        "running": running,
        "queued": queued,
        "finished": finished,
        "failed": failed,
        "stopped": stopped,
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
    }


def completed_output_paths(client_id):
    _, client_output_dir = client_dirs(client_id)
    files = []
    for job in list_jobs(client_id):
        if job.get("status") != "done" or not job.get("output_name"):
            continue
        output_path = client_output_dir / Path(job["output_name"]).name
        if output_path.exists() and output_path.is_file():
            files.append(output_path)
    return files


def stop_job(job_id, client_id):
    job = get_job(job_id)
    if not job:
        return False, "任务不存在。"
    if job.get("client_id") != client_id:
        return False, "任务不存在。"
    if job.get("status") not in {"queued", "running", "stopping"}:
        return False, "当前任务不能停止。"

    with PROCESSES_LOCK:
        process = PROCESSES.get(job_id)

    if process is None:
        set_job(job_id, status="stopped", message="任务已从队列中移除。", progress=0)
        return True, "已取消排队任务。"

    set_job(job_id, status="stopping", message="正在停止压缩任务。")
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    set_process(job_id, None)
    set_job(job_id, status="stopped", message="压缩任务已停止。")
    return True, "已停止。"


def compress_video(job_id, input_path, output_path):
    started = time.time()
    set_job(job_id, status="queued", message="等待空闲压缩通道。", progress=0)

    with JOB_SEMAPHORE:
        latest = get_job(job_id) or {}
        if latest.get("status") == "stopped":
            return

        strategy = latest.get("strategy") or {}
        duration = max(float(latest.get("duration") or 0), 1)
        set_job(
            job_id,
            status="running",
            message="正在压缩，请保持页面打开。",
            started_at=time.time(),
            progress=1,
        )

        cmd = build_ffmpeg_command(input_path, output_path, strategy)

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            set_process(job_id, process)

            speed = ""
            if process.stdout:
                for line in process.stdout:
                    line = line.strip()
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key == "out_time_ms":
                        try:
                            current_seconds = float(value) / 1_000_000.0
                        except ValueError:
                            continue
                        progress = clamp(round((current_seconds / duration) * 100, 1), 0.0, 99.4)
                        message = "正在压缩，请保持页面打开。"
                        if speed:
                            message = f"正在压缩，速度 {speed}。"
                        set_job(job_id, progress=progress, message=message)
                    elif key == "speed":
                        speed = value
                    elif key == "progress" and value == "end":
                        set_job(job_id, progress=99.8)

            stderr_output = process.stderr.read() if process.stderr else ""
            code = process.wait()
            set_process(job_id, None)

            latest = get_job(job_id) or {}
            if latest.get("status") in {"stopping", "stopped"}:
                set_job(job_id, status="stopped", message="压缩任务已停止。")
                return
            if code != 0:
                message = stderr_output.strip().splitlines()[-1] if stderr_output.strip() else f"ffmpeg 退出码：{code}"
                set_job(job_id, status="failed", message=message, progress=0)
                return

            original = file_size(input_path) or 0
            compressed = file_size(output_path) or 0
            saved = max(original - compressed, 0)
            ratio = round(max((1 - compressed / original) * 100, 0), 1) if original else 0
            set_job(
                job_id,
                status="done",
                message="压缩完成，可以下载结果文件。",
                output_name=output_path.name,
                compressed_size=compressed,
                saved_size=saved,
                saved_ratio=ratio,
                progress=100,
                seconds=round(time.time() - started),
                finished_at=time.time(),
            )
        except Exception as exc:
            set_process(job_id, None)
            set_job(job_id, status="failed", message=f"压缩异常：{exc}", progress=0)


class VideoCompressorHandler(SimpleHTTPRequestHandler):
    def get_client_id(self):
        if hasattr(self, "_client_id"):
            return self._client_id

        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        current = normalize_client_id(cookie.get(CLIENT_COOKIE).value if CLIENT_COOKIE in cookie else None)
        self._client_id = current or uuid.uuid4().hex
        self._set_client_cookie = current != self._client_id
        return self._client_id

    def end_headers(self):
        client_id = self.get_client_id()
        if getattr(self, "_set_client_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"{CLIENT_COOKIE}={client_id}; Path=/; SameSite=Lax; Max-Age=31536000",
            )
        super().end_headers()

    def translate_path(self, path):
        parsed = urlparse(path)
        clean_path = unquote(parsed.path)
        if clean_path == "/":
            return str(STATIC_DIR / "index.html")
        if clean_path.startswith("/static/"):
            return str(STATIC_DIR / clean_path.removeprefix("/static/"))
        if clean_path.startswith("/download/"):
            _, client_output_dir = client_dirs(self.get_client_id())
            return str(client_output_dir / Path(clean_path.removeprefix("/download/")).name)
        return str(STATIC_DIR / clean_path.lstrip("/"))

    def do_GET(self):
        parsed = urlparse(self.path)
        client_id = self.get_client_id()
        if parsed.path == "/api/system":
            jobs = list_jobs(client_id)
            json_response(
                self,
                {
                    "client_id": client_id,
                    "ffmpeg": bool(shutil.which("ffmpeg")),
                    "ffprobe": bool(shutil.which("ffprobe")),
                    "default_target_reduction": DEFAULT_TARGET_REDUCTION,
                    "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
                    "content_profiles": CONTENT_PROFILES,
                    "lan_url": (
                        f"http://{get_local_ip()}:{os.environ.get('PORT', '8088')}"
                        if get_local_ip()
                        else None
                    ),
                    "summary": snapshot_summary(jobs),
                },
            )
            return

        if parsed.path == "/api/jobs":
            jobs = list_jobs(client_id)
            json_response(self, {"jobs": jobs, "summary": snapshot_summary(jobs)})
            return

        if parsed.path == "/download-all":
            output_paths = completed_output_paths(client_id)
            if not output_paths:
                json_response(self, {"message": "还没有可下载的压缩结果。"}, HTTPStatus.NOT_FOUND)
                return

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            archive_name = f"compressed_videos_{timestamp}_{client_id[:8]}.zip"
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp_file:
                archive_path = Path(temp_file.name)

            try:
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    used_names = set()
                    for output_path in output_paths:
                        arcname = output_path.name
                        if arcname in used_names:
                            arcname = f"{output_path.stem}_{uuid.uuid4().hex[:6]}{output_path.suffix}"
                        used_names.add(arcname)
                        archive.write(output_path, arcname=arcname)

                size = archive_path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{archive_name}"')
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with archive_path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)
            finally:
                archive_path.unlink(missing_ok=True)
            return

        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.removeprefix("/api/jobs/")
            job = get_job(job_id)
            if not job or job.get("client_id") != client_id:
                json_response(self, {"message": "任务不存在。"}, HTTPStatus.NOT_FOUND)
                return
            json_response(self, job)
            return

        return super().do_GET()

    def do_POST(self):
        client_id = self.get_client_id()
        if self.path.startswith("/api/jobs/") and self.path.endswith("/stop"):
            job_id = self.path.removeprefix("/api/jobs/").removesuffix("/stop")
            ok, message = stop_job(job_id, client_id)
            status = HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST
            json_response(self, {"message": message}, status)
            return

        if self.path != "/api/jobs":
            json_response(self, {"message": "接口不存在。"}, HTTPStatus.NOT_FOUND)
            return

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            json_response(
                self,
                {"message": "服务器缺少 ffmpeg 或 ffprobe，请先安装 ffmpeg。"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type"),
                "CONTENT_LENGTH": self.headers.get("Content-Length"),
            },
        )

        try:
            target_reduction = float(form.getfirst("target_reduction", str(DEFAULT_TARGET_REDUCTION)))
        except ValueError:
            json_response(self, {"message": "目标压缩比例格式不正确。"}, HTTPStatus.BAD_REQUEST)
            return

        profile_key = form.getfirst("content_profile", "slides")
        if profile_key not in CONTENT_PROFILES:
            json_response(self, {"message": "视频类型无效。"}, HTTPStatus.BAD_REQUEST)
            return

        file_items = form["video"] if "video" in form else None
        if file_items is None:
            json_response(self, {"message": "请上传视频文件。"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(file_items, list):
            file_items = [file_items]

        valid_items = [item for item in file_items if item is not None and item.filename]
        if not valid_items:
            json_response(self, {"message": "请上传视频文件。"}, HTTPStatus.BAD_REQUEST)
            return

        job_ids = []
        client_upload_dir, client_output_dir = client_dirs(client_id)
        client_upload_dir.mkdir(parents=True, exist_ok=True)
        client_output_dir.mkdir(parents=True, exist_ok=True)
        for file_item in valid_items:
            job_id = uuid.uuid4().hex
            filename = safe_name(file_item.filename)
            input_path = client_upload_dir / f"{job_id}_{filename}"
            output_path = client_output_dir / f"{Path(filename).stem}_compressed_{job_id[:8]}.mp4"

            with input_path.open("wb") as target:
                shutil.copyfileobj(file_item.file, target)

            try:
                meta = probe_media(input_path)
            except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
                json_response(self, {"message": f"分析视频失败：{exc}"}, HTTPStatus.BAD_REQUEST)
                return

            input_size = file_size(input_path) or 0
            strategy = build_strategy(meta, input_size, target_reduction, profile_key)
            set_job(
                job_id,
                id=job_id,
                client_id=client_id,
                status="queued",
                message="视频已上传，等待开始压缩。",
                filename=filename,
                input_size=input_size,
                created_at=time.time(),
                progress=0,
                duration=round(meta.get("duration") or 0, 2),
                width=meta.get("width") or 0,
                height=meta.get("height") or 0,
                fps=round(meta.get("fps") or 0, 2),
                source_video_bitrate=meta.get("video_bitrate") or 0,
                source_audio_bitrate=meta.get("audio_bitrate") or 0,
                target_reduction=strategy["target_reduction"],
                content_profile=profile_key,
                strategy=strategy,
            )

            thread = threading.Thread(
                target=compress_video,
                args=(job_id, input_path, output_path),
                daemon=True,
            )
            thread.start()
            job_ids.append(job_id)

        json_response(self, {"job_ids": job_ids}, HTTPStatus.CREATED)


class StableThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        self.socket.bind(self.server_address)
        host, port = self.server_address[:2]
        self.server_name = host or "0.0.0.0"
        self.server_port = port


def main():
    UPLOAD_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", "8088"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = StableThreadingHTTPServer((host, port), VideoCompressorHandler)
    local_ip = get_local_ip()
    print(f"视频压缩系统已启动：http://localhost:{port}")
    if local_ip:
        print(f"局域网访问地址：http://{local_ip}:{port}")
    else:
        print("未能自动识别局域网 IP，请手动查看本机地址。")
    print(f"当前最大并发压缩数：{MAX_CONCURRENT_JOBS}")
    server.serve_forever()


if __name__ == "__main__":
    main()
