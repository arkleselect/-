#!/usr/bin/env python3
import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


PRESETS = {
    "1080p balanced": {
        "height": 1080,
        "crf": 26,
        "audio": "96k",
        "description": "Recommended for learning platforms.",
    },
    "720p small": {
        "height": 720,
        "crf": 27,
        "audio": "96k",
        "description": "Smaller file, good for screen recordings.",
    },
    "Keep resolution": {
        "height": None,
        "crf": 26,
        "audio": "128k",
        "description": "Compress without resizing.",
    },
}


class VideoCompressorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Compressor")
        self.geometry("720x420")
        self.minsize(640, 380)

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.preset_name = tk.StringVar(value="1080p balanced")
        self.status = tk.StringVar(value="Choose a video to compress.")
        self.queue = queue.Queue()
        self.process = None

        self._build_ui()
        self.after(200, self._poll_queue)

    def _build_ui(self):
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(5, weight=1)

        ttk.Label(root, text="Input video").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(root, textvariable=self.input_path).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(root, text="Browse", command=self.choose_input).grid(row=0, column=2)

        ttk.Label(root, text="Output file").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(root, textvariable=self.output_path).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(root, text="Save as", command=self.choose_output).grid(row=1, column=2)

        ttk.Label(root, text="Preset").grid(row=2, column=0, sticky="w", pady=6)
        preset = ttk.Combobox(
            root,
            textvariable=self.preset_name,
            values=list(PRESETS.keys()),
            state="readonly",
        )
        preset.grid(row=2, column=1, sticky="ew", padx=8)
        preset.bind("<<ComboboxSelected>>", lambda _event: self._update_preset_text())

        self.preset_text = ttk.Label(root, text=PRESETS[self.preset_name.get()]["description"])
        self.preset_text.grid(row=3, column=1, sticky="w", padx=8, pady=(0, 8))

        controls = ttk.Frame(root)
        controls.grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
        controls.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(controls, text="Start compression", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(controls, mode="indeterminate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=12)

        log_frame = ttk.LabelFrame(root, text="Log")
        log_frame.grid(row=5, column=0, columnspan=3, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=10, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        ttk.Label(root, textvariable=self.status).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def choose_input(self):
        path = filedialog.askopenfilename(
            title="Choose video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.input_path.set(path)
        source = Path(path)
        self.output_path.set(str(source.with_name(f"{source.stem}_compressed.mp4")))

    def choose_output(self):
        path = filedialog.asksaveasfilename(
            title="Save compressed video as",
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if path:
            self.output_path.set(path)

    def _update_preset_text(self):
        self.preset_text.configure(text=PRESETS[self.preset_name.get()]["description"])

    def start(self):
        if not shutil.which("ffmpeg"):
            messagebox.showerror("ffmpeg not found", "Install ffmpeg first: brew install ffmpeg")
            return

        input_file = self.input_path.get().strip()
        output_file = self.output_path.get().strip()
        if not input_file or not os.path.exists(input_file):
            messagebox.showerror("Missing input", "Please choose a valid input video.")
            return
        if not output_file:
            messagebox.showerror("Missing output", "Please choose an output file.")
            return

        self.start_button.configure(state="disabled")
        self.progress.start(10)
        self.log.delete("1.0", "end")
        self.status.set("Compressing...")

        worker = threading.Thread(target=self._compress, args=(input_file, output_file), daemon=True)
        worker.start()

    def _compress(self, input_file, output_file):
        preset = PRESETS[self.preset_name.get()]
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_file,
        ]

        if preset["height"]:
            cmd.extend(["-vf", f"scale=-2:{preset['height']}"])

        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-crf",
                str(preset["crf"]),
                "-preset",
                "slow",
                "-c:a",
                "aac",
                "-b:a",
                preset["audio"],
                output_file,
            ]
        )

        self.queue.put(("log", "Running:\n" + " ".join(cmd) + "\n\n"))
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for line in self.process.stdout:
            self.queue.put(("log", line))

        code = self.process.wait()
        if code == 0:
            self.queue.put(("done", output_file))
        else:
            self.queue.put(("error", f"ffmpeg exited with code {code}"))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log.insert("end", payload)
                    self.log.see("end")
                elif kind == "done":
                    self.progress.stop()
                    self.start_button.configure(state="normal")
                    self.status.set(f"Done: {payload}")
                    messagebox.showinfo("Compression complete", f"Saved to:\n{payload}")
                elif kind == "error":
                    self.progress.stop()
                    self.start_button.configure(state="normal")
                    self.status.set(payload)
                    messagebox.showerror("Compression failed", payload)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)


if __name__ == "__main__":
    app = VideoCompressorApp()
    app.mainloop()
