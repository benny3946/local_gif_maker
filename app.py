from __future__ import annotations

from fractions import Fraction
from io import BytesIO
import json
import math
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD


PREVIEW_WIDTH = 1280
PREVIEW_HEIGHT = 720
PLAYBACK_FPS = 15
PLAYBACK_POLL_MS = 30
DEFAULT_FRAME_SEARCH_BATCH_SIZE = 15
MAX_FRAME_SEARCH_BATCH_SIZE = 120
FRAME_SEARCH_POLL_MS = 15
FRAME_SEARCH_HOLD_DELAY_MS = 500
SEEK_DEBOUNCE_MS = 80
RULER_LABEL_SPACING = 80
TIME_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def format_time(seconds: float) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


def parse_time(value: str) -> float:
    value = value.strip()
    try:
        if ":" not in value:
            return float(value)
        parts = [float(part) for part in value.split(":")]
        if len(parts) == 2:
            minutes, seconds = parts
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        pass
    raise ValueError("Enter seconds or a time in HH:MM:SS.mmm format.")


class GifMaker:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Local GIF Maker")
        self.root.geometry("1020x790")
        self.root.minsize(820, 670)

        self.ffmpeg = shutil.which("ffmpeg")
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gif-maker")
        self.events: queue.Queue[tuple[str, int, object]] = queue.Queue()
        self.video_path: Path | None = None
        self.duration = 0.0
        self.frame_duration = 1 / PLAYBACK_FPS
        self.current_time = 0.0
        self.request_id = 0
        self.load_future: Future[tuple[float, float, bytes]] | None = None
        self.preview_future: Future[bytes] | None = None
        self.preview_pending = False
        self.frame_search_generation = 0
        self.frame_search_batch_future: Future[
            tuple[int, int, int, tuple[bytes, ...]]
        ] | None = None
        self.frame_search_cache: dict[int, tuple[bytes, int, int]] = {}
        self.frame_search_target_index: int | None = None
        self.frame_search_frame_ready = False
        self.seek_after_id: str | None = None
        self.frame_search_direction: int | None = None
        self.frame_search_hold_after_id: str | None = None
        self.frame_search_continuous = False
        self.playback_generation = 0
        self.playback_process: subprocess.Popen[bytes] | None = None
        self.playback_frame_lock = threading.Lock()
        self.latest_playback_frame: tuple[int, bytes, float, int, int] | None = None
        self.export_generation = 0
        self.output_image: Image.Image | None = None
        self.output_frame_index = 0
        self.output_after_id: str | None = None
        self.source_photo: ImageTk.PhotoImage | None = None
        self.output_photo: ImageTk.PhotoImage | None = None
        self.source_frame_image: Image.Image | None = None
        self.source_zoom = 1.0
        self.source_zoom_center = (0.5, 0.5)

        self.status_var = tk.StringVar(value="Open a video to get started.")
        self.current_var = tk.StringVar(value="00:00:00.000")
        self.current_frame_var = tk.StringVar(value="— / —")
        self.start_var = tk.StringVar(value="00:00:00.000")
        self.end_var = tk.StringVar(value="00:00:00.000")
        self.start_frame_var = tk.StringVar(value="1")
        self.end_frame_var = tk.StringVar(value="1")
        self.seek_step_var = tk.StringVar(value="1")
        self.frame_search_batch_var = tk.StringVar(
            value=str(DEFAULT_FRAME_SEARCH_BATCH_SIZE)
        )
        self.fps_var = tk.IntVar(value=15)
        self.width_var = tk.IntVar(value=480)
        self.export_progress_var = tk.IntVar(value=0)
        self.export_percent_var = tk.StringVar(value="")

        self._build_ui()
        self._register_drop_targets(self.root)
        arrow_shortcut_tag = "LocalGifMakerArrowShortcut"
        self.root.bind_class(arrow_shortcut_tag, "<Left>", self._previous_frame)
        self.root.bind_class(arrow_shortcut_tag, "<Right>", self._next_frame)
        self.root.bind_class(
            arrow_shortcut_tag, "<KeyRelease-Left>", self._end_frame_search
        )
        self.root.bind_class(
            arrow_shortcut_tag, "<KeyRelease-Right>", self._end_frame_search
        )
        self.root.bind_class(arrow_shortcut_tag, "<Up>", self._seek_forward)
        self.root.bind_class(arrow_shortcut_tag, "<Down>", self._seek_backward)
        self.root.bind_class(
            "LocalGifMakerSpaceShortcut", "<space>", self._toggle_playback
        )
        self._register_shortcut_tag(self.root, arrow_shortcut_tag)
        self._register_space_shortcut(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        poll_interval = PLAYBACK_POLL_MS if self.playback_process else 80
        self.root.after(poll_interval, self._poll_events)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=14)
        container.pack(fill="both", expand=True)

        toolbar = ttk.Frame(container)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Open video…", command=self._open_video).pack(side="left")
        self.video_label = ttk.Label(toolbar, text="No video selected", anchor="w")
        self.video_label.pack(side="left", padx=12, fill="x", expand=True)

        previews = ttk.Frame(container)
        previews.pack(fill="both", expand=True)
        previews.columnconfigure(0, weight=1)
        previews.columnconfigure(1, weight=1)
        previews.rowconfigure(0, weight=1)

        source_panel = ttk.LabelFrame(previews, text="Source video", padding=8)
        source_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        output_panel = ttk.LabelFrame(previews, text="GIF preview", padding=8)
        output_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.source_view = ttk.Label(
            source_panel,
            text="Choose a video or drag one into this window",
            anchor="center",
            relief="sunken",
        )
        self.source_view.pack(fill="both", expand=True)
        self.source_view.bind("<Button-1>", self._toggle_playback)
        self.source_view.bind("<Button-2>", self._reset_source_zoom)
        self.source_view.bind("<MouseWheel>", self._on_source_mousewheel)
        self.source_view.bind("<Configure>", self._resize_source_preview)
        self.source_view.configure(cursor="hand2")
        preview_controls = ttk.Frame(source_panel)
        preview_controls.pack(fill="x", pady=(5, 0))
        ttk.Label(
            preview_controls, text="Click the video or press Space to play/pause"
        ).pack(side="left")
        self.reset_zoom_button = ttk.Button(
            preview_controls, text="Reset zoom", command=self._reset_source_zoom
        )
        self.reset_zoom_button.pack(side="right")
        self.output_view = ttk.Label(
            output_panel,
            text="Your exported GIF will preview here",
            anchor="center",
            relief="sunken",
        )
        self.output_view.pack(fill="both", expand=True)

        timeline = ttk.LabelFrame(container, text="Select a section", padding=10)
        timeline.pack(fill="x", pady=(12, 0))
        self.timeline_ruler = tk.Canvas(
            timeline,
            height=84,
            highlightthickness=0,
            background=self.root.cget("background"),
        )
        self.timeline_ruler.pack(fill="x", pady=(0, 6))
        self.timeline_ruler.bind("<Configure>", self._draw_timeline_ruler)
        self.timeline_ruler.bind("<ButtonPress-1>", self._begin_timeline_drag)
        self.timeline_ruler.bind("<B1-Motion>", self._drag_trim_marker)
        self.timeline_ruler.bind("<ButtonRelease-1>", self._end_trim_marker_drag)
        self.trim_marker_drag: str | None = None
        self.trim_marker_drag_offset = 0.0

        trim_controls = ttk.Frame(timeline)
        trim_controls.pack(fill="x")
        trim_fields = ttk.Frame(trim_controls)
        trim_fields.pack(side="left", fill="x", expand=True)

        time_row = ttk.Frame(trim_fields)
        time_row.pack(fill="x")
        ttk.Label(time_row, text="Preview time").pack(side="left")
        self.current_entry = ttk.Entry(
            time_row, textvariable=self.current_var, width=15
        )
        self.current_entry.pack(side="left", padx=(6, 10))
        self._bind_enter_to_blur(self.current_entry, self._go_to_time)
        ttk.Label(time_row, text="Frame").pack(side="left")
        self.current_frame_entry = ttk.Entry(
            time_row, textvariable=self.current_frame_var, width=14, state="readonly"
        )
        self.current_frame_entry.pack(side="left", padx=(6, 0))
        ttk.Button(
            time_row, text="Set start here", command=self._set_start
        ).pack(side="left", padx=(12, 4))
        ttk.Button(time_row, text="Set end here", command=self._set_end).pack(side="left")

        range_row = ttk.Frame(trim_fields)
        range_row.pack(fill="x", pady=(10, 0))
        ttk.Label(range_row, text="Start").pack(side="left")
        self.start_entry = ttk.Entry(range_row, textvariable=self.start_var, width=15)
        self.start_entry.pack(side="left", padx=(6, 14))
        self._bind_enter_to_blur(self.start_entry)
        self.start_entry.bind("<FocusOut>", self._sync_frames_from_times)
        ttk.Label(range_row, text="End").pack(side="left")
        self.end_entry = ttk.Entry(range_row, textvariable=self.end_var, width=15)
        self.end_entry.pack(side="left", padx=(6, 0))
        self._bind_enter_to_blur(self.end_entry)
        self.end_entry.bind("<FocusOut>", self._sync_frames_from_times)
        ttk.Label(range_row, text="Times accept seconds or HH:MM:SS.mmm").pack(
            side="left", padx=12
        )

        frame_row = ttk.Frame(trim_fields)
        frame_row.pack(fill="x", pady=(6, 0))
        ttk.Label(frame_row, text="Start frame").pack(side="left")
        self.start_frame_entry = ttk.Entry(
            frame_row, textvariable=self.start_frame_var, width=10
        )
        self.start_frame_entry.pack(side="left", padx=(6, 14))
        self._bind_enter_to_blur(self.start_frame_entry)
        self.start_frame_entry.bind("<FocusOut>", self._sync_times_from_frames)
        ttk.Label(frame_row, text="End frame").pack(side="left")
        self.end_frame_entry = ttk.Entry(
            frame_row, textvariable=self.end_frame_var, width=10
        )
        self.end_frame_entry.pack(side="left", padx=(6, 14))
        self._bind_enter_to_blur(self.end_frame_entry)
        self.end_frame_entry.bind("<FocusOut>", self._sync_times_from_frames)
        self.frame_count_label = ttk.Label(frame_row, text="Total frames: —")
        self.frame_count_label.pack(side="left")

        seek_step_panel = ttk.LabelFrame(
            trim_controls, text="Arrow-key seek", padding=(10, 6)
        )
        seek_step_panel.pack(side="right", fill="y", padx=(12, 0))
        ttk.Label(seek_step_panel, text="Seconds per Up/Down press").pack(anchor="w")
        self.seek_step_spinbox = ttk.Spinbox(
            seek_step_panel,
            from_=0.01,
            to=3600,
            increment=0.1,
            textvariable=self.seek_step_var,
            width=10,
        )
        self.seek_step_spinbox.pack(anchor="w", pady=(4, 0))
        self._bind_enter_to_blur(self.seek_step_spinbox)
        ttk.Label(seek_step_panel, text="Frames per preload batch (1-120)").pack(
            anchor="w", pady=(8, 0)
        )
        self.frame_search_batch_spinbox = ttk.Spinbox(
            seek_step_panel,
            from_=1,
            to=MAX_FRAME_SEARCH_BATCH_SIZE,
            increment=1,
            textvariable=self.frame_search_batch_var,
            width=10,
        )
        self.frame_search_batch_spinbox.pack(anchor="w", pady=(4, 0))
        self._bind_enter_to_blur(self.frame_search_batch_spinbox)

        options = ttk.Frame(container)
        options.pack(fill="x", pady=(12, 0))
        ttk.Label(options, text="Frame rate").pack(side="left")
        self.fps_spinbox = ttk.Spinbox(
            options, from_=1, to=60, textvariable=self.fps_var, width=5
        )
        self.fps_spinbox.pack(side="left", padx=(6, 16))
        self._bind_enter_to_blur(self.fps_spinbox)
        ttk.Label(options, text="Width (px)").pack(side="left")
        self.width_spinbox = ttk.Spinbox(
            options,
            from_=100,
            to=1920,
            increment=10,
            textvariable=self.width_var,
            width=6,
        )
        self.width_spinbox.pack(side="left", padx=6)
        self._bind_enter_to_blur(self.width_spinbox)
        self.export_button = ttk.Button(
            options, text="Create GIF…", command=self._export_gif, state="disabled"
        )
        self.export_button.pack(side="right")

        status_row = ttk.Frame(container)
        status_row.pack(fill="x", pady=(10, 0))
        ttk.Label(status_row, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True
        )
        self.export_progress = ttk.Progressbar(
            status_row,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=self.export_progress_var,
            length=180,
        )
        self.export_progress.pack(side="left", padx=(8, 4))
        self.export_percent = ttk.Label(
            status_row, textvariable=self.export_percent_var, width=5, anchor="e"
        )
        self.export_percent.pack(side="left")

    def _open_video(self) -> None:
        if not self.ffmpeg:
            messagebox.showerror(
                "FFmpeg not found",
                "FFmpeg was not found on PATH. Add its bin folder to PATH, "
                "then close and reopen this app.",
            )
            return
        path = filedialog.askopenfilename(
            title="Choose a video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.wmv *.m4v *.mpeg *.mpg"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._load_video_path(Path(path))

    def _register_drop_targets(self, widget: tk.Misc) -> None:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", self._on_drop)
        for child in widget.winfo_children():
            self._register_drop_targets(child)

    def _register_space_shortcut(self, widget: tk.Misc) -> None:
        self._register_shortcut_tag(widget, "LocalGifMakerSpaceShortcut")

    def _bind_enter_to_blur(
        self,
        widget: tk.Entry | ttk.Entry | ttk.Spinbox,
        commit: Callable[[tk.Event], object] | None = None,
    ) -> None:
        widget.bind(
            "<Return>",
            lambda event: self._commit_and_blur(event, commit),
        )

    def _commit_and_blur(
        self,
        event: tk.Event,
        commit: Callable[[tk.Event], object] | None,
    ) -> str:
        if commit is not None:
            commit(event)
        self.root.focus_set()
        return "break"

    def _register_shortcut_tag(self, widget: tk.Misc, shortcut_tag: str) -> None:
        bindtags = widget.bindtags()
        if shortcut_tag not in bindtags:
            widget.bindtags((shortcut_tag, *bindtags))
        for child in widget.winfo_children():
            self._register_shortcut_tag(child, shortcut_tag)

    def _on_drop(self, event: tk.Event) -> str:
        paths = self.root.tk.splitlist(event.data)
        if not paths:
            return "copy"
        if len(paths) > 1:
            messagebox.showinfo(
                "Choose one video",
                "Please drop one video file at a time.",
            )
            return "copy"
        if not self.ffmpeg:
            messagebox.showerror(
                "FFmpeg not found",
                "FFmpeg was not found on PATH. Add its bin folder to PATH, "
                "then close and reopen this app.",
            )
            return "copy"
        self._load_video_path(Path(paths[0]))
        return "copy"

    def _load_video_path(self, path: Path) -> None:
        if not path.is_file():
            messagebox.showerror("Video not found", f"File does not exist:\n{path}")
            return
        self._stop_playback()
        self.frame_search_generation += 1
        self.frame_search_direction = None
        self.frame_search_continuous = False
        self._cancel_frame_search_hold()
        self.frame_search_batch_future = None
        self.frame_search_cache.clear()
        self.frame_search_target_index = None
        self.frame_search_frame_ready = False
        self.request_id += 1
        request_id = self.request_id
        self.video_path = path
        self.video_label.configure(text=self.video_path.name)
        self.status_var.set("Reading video…")
        self.export_button.configure(state="disabled")
        if self.load_future:
            self.load_future.cancel()
        future = self.executor.submit(self._load_video, self.video_path)
        self.load_future = future
        future.add_done_callback(
            lambda completed: self._post_future("loaded", request_id, completed)
        )

    def _load_video(self, path: Path) -> tuple[float, float, bytes]:
        duration, frame_duration = self._probe_video(path)
        if duration <= 0:
            raise RuntimeError("The video has no playable duration.")
        image = self._extract_frame(path, 0)
        return duration, frame_duration, image

    def _probe_video(self, path: Path) -> tuple[float, float]:
        ffprobe = shutil.which("ffprobe")
        ffmpeg_path = Path(self.ffmpeg)
        sibling_ffprobe = ffmpeg_path.with_name(
            "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
        )
        if not ffprobe and sibling_ffprobe.is_file():
            ffprobe = str(sibling_ffprobe)

        if ffprobe:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,duration,duration_ts,time_base,avg_frame_rate,r_frame_rate,disposition",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                try:
                    metadata = json.loads(result.stdout.decode("utf-8"))
                    video_streams = [
                        stream
                        for stream in metadata.get("streams", [])
                        if stream.get("codec_type") == "video"
                    ]
                    default_streams = [
                        stream
                        for stream in video_streams
                        if stream.get("disposition", {}).get("default") == 1
                    ]
                    primary_stream = (
                        default_streams[0]
                        if default_streams
                        else video_streams[0] if video_streams else {}
                    )

                    def stream_duration(stream: dict[str, object]) -> float | None:
                        try:
                            value = stream.get("duration")
                            if value not in (None, "N/A"):
                                return float(str(value))
                            ticks = stream.get("duration_ts")
                            time_base = stream.get("time_base")
                            if ticks not in (None, "N/A") and time_base:
                                return float(int(str(ticks)) * Fraction(str(time_base)))
                        except (ValueError, ZeroDivisionError):
                            return None
                        return None

                    stream_seconds = stream_duration(primary_stream)
                    format_seconds = metadata.get("format", {}).get("duration")
                    if stream_seconds and stream_seconds > 0:
                        duration = stream_seconds
                    elif format_seconds not in (None, "N/A"):
                        duration = float(format_seconds)
                    else:
                        duration = 0.0

                    frame_duration = self.frame_duration
                    for rate_name in ("avg_frame_rate", "r_frame_rate"):
                        try:
                            frame_rate = Fraction(str(primary_stream.get(rate_name, "0/0")))
                            if frame_rate > 0:
                                frame_duration = float(1 / frame_rate)
                                break
                        except (ValueError, ZeroDivisionError):
                            continue
                    return duration, frame_duration
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        details = result.stderr.decode("utf-8", errors="replace")
        match = TIME_PATTERN.search(details)
        if not match:
            raise RuntimeError(
                "Could not read the video's duration. Check that the file is a supported video."
            )
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return duration, self.frame_duration

    def _extract_frame(
        self,
        path: Path,
        seconds: float,
        width: int = PREVIEW_WIDTH,
        height: int = PREVIEW_HEIGHT,
    ) -> bytes:
        offsets = (0, self.frame_duration, 0.1, 0.25, 0.5, 1.0, 2.0)
        candidate_times: list[float] = []
        for offset in offsets:
            candidate = max(0.0, seconds - offset)
            if not any(
                math.isclose(candidate, existing, abs_tol=0.001)
                for existing in candidate_times
            ):
                candidate_times.append(candidate)

        last_error = ""
        for candidate in candidate_times:
            result = subprocess.run(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{candidate:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            last_error = result.stderr.decode("utf-8", errors="replace").strip()

        result = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-ss",
                f"{candidate_times[0]:.3f}",
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        last_error = result.stderr.decode("utf-8", errors="replace").strip() or last_error
        raise RuntimeError(
            f"Could not decode a preview frame near {format_time(seconds)}."
            + (f"\nFFmpeg: {last_error}" if last_error else "")
        )

    def _extract_frame_batch(
        self,
        path: Path,
        first_frame_index: int,
        frame_count: int,
        width: int,
        height: int,
    ) -> tuple[int, int, int, tuple[bytes, ...]]:
        result = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{first_frame_index * self.frame_duration:.6f}",
                "-i",
                str(path),
                "-frames:v",
                str(frame_count),
                "-vf",
                f"scale={width}:{height},format=rgb24",
                "-fps_mode",
                "passthrough",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or "FFmpeg could not decode preview frames.")

        frame_size = width * height * 3
        decoded_count = len(result.stdout) // frame_size
        if decoded_count == 0 or len(result.stdout) % frame_size:
            raise RuntimeError("FFmpeg returned incomplete preview frames.")
        frames = tuple(
            result.stdout[index * frame_size : (index + 1) * frame_size]
            for index in range(decoded_count)
        )
        return first_frame_index, width, height, frames

    def _seek_to_time(self, seconds: float) -> None:
        if not self.video_path or self.duration <= 0:
            return
        self._stop_playback()
        self.current_time = min(self.duration, max(0.0, seconds))
        self.current_var.set(format_time(self.current_time))
        self._update_current_frame()
        self._update_current_playhead()
        if self.seek_after_id:
            self.root.after_cancel(self.seek_after_id)
        self.seek_after_id = self.root.after(SEEK_DEBOUNCE_MS, self._request_source_frame)

    def _draw_timeline_ruler(self, _event: tk.Event | None = None) -> None:
        canvas = self.timeline_ruler
        canvas.delete("all")
        if self.duration <= 0:
            return

        width = canvas.winfo_width()
        if width <= 1:
            return
        left, right = 10, width - 10
        usable_width = max(1, right - left)
        desired_interval = self.duration * RULER_LABEL_SPACING / usable_width
        magnitude = 10 ** math.floor(math.log10(desired_interval))
        interval = next(
            (
                step * magnitude
                for step in (1, 2, 5, 10)
                if step * magnitude >= desired_interval
            ),
            10 * magnitude,
        )

        def label_for(seconds: float) -> str:
            if interval < 1:
                precision = min(3, max(1, -math.floor(math.log10(interval))))
                return f"{seconds:.{precision}f}s"
            total_seconds = int(seconds)
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds_part = divmod(remainder, 60)
            if self.duration >= 3600:
                return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"
            return f"{minutes:02d}:{seconds_part:02d}"

        tick_count = int(self.duration // interval)
        major_times = [index * interval for index in range(tick_count + 1)]
        if not math.isclose(
            major_times[-1], self.duration, rel_tol=1e-9, abs_tol=1e-9
        ):
            major_times.append(self.duration)
        minor_interval = interval / 5
        minor_count = min(int(self.duration // minor_interval), 1000)
        major_set = {round(seconds, 6) for seconds in major_times}
        self._draw_selection_bar(left, usable_width)

        for index in range(minor_count + 1):
            seconds = index * minor_interval
            if round(seconds, 6) in major_set or seconds > self.duration:
                continue
            x = left + seconds / self.duration * usable_width
            canvas.create_line(x, 56, x, 61, fill="#888888")

        for index, seconds in enumerate(major_times):
            x = left + seconds / self.duration * usable_width
            canvas.create_line(x, 55, x, 62, fill="#555555")
            if (
                index == len(major_times) - 2
                and (self.duration - seconds) / self.duration * usable_width
                < RULER_LABEL_SPACING * 0.7
            ):
                continue
            anchor = "center"
            if seconds == 0:
                anchor = "w"
            elif seconds == self.duration:
                anchor = "e"
            canvas.create_text(
                x,
                77,
                text=label_for(seconds),
                anchor=anchor,
                fill="#404040",
                font=("TkDefaultFont", 8),
            )
        self._draw_current_playhead()
        self._draw_trim_markers(left, usable_width)

    def _draw_selection_bar(self, left: float, usable_width: float) -> None:
        start = self._trim_marker_time("start")
        end = self._trim_marker_time("end")
        if start is None or end is None or end < start:
            return
        start_x = left + start / self.duration * usable_width
        end_x = left + end / self.duration * usable_width
        self.timeline_ruler.create_rectangle(
            start_x,
            64,
            end_x,
            70,
            fill="#f2d13d",
            outline="",
            tags=("selection_bar",),
        )

    def _draw_current_playhead(self) -> None:
        if self.duration <= 0:
            return
        canvas = self.timeline_ruler
        canvas.delete("current_playhead")
        width = canvas.winfo_width()
        if width <= 1:
            return
        left, right = 10, width - 10
        x = left + self.current_time / self.duration * max(1, right - left)
        half_width = 5
        marker_bottom = 20
        canvas.create_rectangle(
            x - half_width,
            1,
            x + half_width,
            marker_bottom,
            fill="#0078d7",
            outline="#0065b3",
            width=1,
            tags=("current_playhead",),
        )
        canvas.create_line(
            x,
            marker_bottom,
            x,
            64,
            fill="#0078d7",
            width=1,
            tags=("current_playhead",),
        )

    def _update_current_playhead(self) -> None:
        if self.duration <= 0:
            return
        self._draw_current_playhead()

    def _trim_marker_time(self, marker: str) -> float | None:
        frame_var = self.start_frame_var if marker == "start" else self.end_frame_var
        try:
            frame = int(frame_var.get())
        except ValueError:
            return None
        if not 1 <= frame <= self._frame_count():
            return None
        if marker == "start":
            return (frame - 1) * self.frame_duration
        return min(frame * self.frame_duration, self.duration)

    def _draw_trim_markers(self, left: float, usable_width: float) -> None:
        if self.duration <= 0:
            return
        canvas = self.timeline_ruler
        half_width = 13
        for marker, symbol in (("start", "|◀"), ("end", "▶|")):
            seconds = self._trim_marker_time(marker)
            if seconds is None:
                continue
            pin_x = left + seconds / self.duration * usable_width
            marker_left = pin_x - 1 if marker == "start" else pin_x - 2 * half_width
            marker_right = marker_left + 2 * half_width if marker == "start" else pin_x
            icon_center = (marker_left + marker_right) / 2
            marker_top = 27
            marker_bottom = 49
            canvas.create_rectangle(
                marker_left,
                marker_top,
                marker_right,
                marker_bottom,
                fill="#16833b",
                outline="#126b30",
                width=1,
                tags=("trim_marker", marker),
            )
            canvas.create_text(
                icon_center,
                (marker_top + marker_bottom) / 2,
                text=symbol,
                anchor="center",
                fill="white",
                font=("Segoe UI Symbol", 11, "bold"),
                tags=("trim_marker", marker),
            )
            stem_x = pin_x
            canvas.create_line(
                stem_x,
                marker_bottom,
                stem_x,
                64,
                fill="#16833b",
                width=2,
                tags=("trim_marker", marker),
            )

    def _begin_timeline_drag(self, event: tk.Event) -> str:
        if not self.video_path or self.duration <= 0:
            return "break"
        canvas = self.timeline_ruler
        width = canvas.winfo_width()
        left, right = 10, width - 10
        usable_width = max(1, right - left)
        candidates: list[tuple[float, str]] = []
        playhead_x = left + self.current_time / self.duration * usable_width
        playhead_distance = abs(event.x - playhead_x)
        if playhead_distance <= 6 and 1 <= event.y <= 20:
            self.trim_marker_drag = "playhead"
            self.trim_marker_drag_offset = event.x - playhead_x
            self._drag_trim_marker(event)
            return "break"
        for marker in ("start", "end"):
            seconds = self._trim_marker_time(marker)
            if seconds is None:
                continue
            pin_x = left + seconds / self.duration * usable_width
            icon_center = pin_x + 13 if marker == "start" else pin_x - 13
            distance = abs(event.x - icon_center)
            if distance <= 15 and 27 <= event.y <= 49:
                candidates.append((distance, marker))
        if candidates:
            self.trim_marker_drag = min(candidates)[1]
        else:
            self.trim_marker_drag = "playhead"
            self.trim_marker_drag_offset = 0.0
            return self._drag_trim_marker(event)
        marker_time = (
            self.current_time
            if self.trim_marker_drag == "playhead"
            else self._trim_marker_time(self.trim_marker_drag)
        )
        if marker_time is not None:
            pin_x = left + marker_time / self.duration * usable_width
            self.trim_marker_drag_offset = event.x - pin_x
        self._drag_trim_marker(event)
        return "break"

    def _drag_trim_marker(self, event: tk.Event) -> str:
        if self.trim_marker_drag is None or self.duration <= 0:
            return "break"
        width = self.timeline_ruler.winfo_width()
        left, right = 10, width - 10
        pin_x = event.x - self.trim_marker_drag_offset
        fraction = min(1.0, max(0.0, (pin_x - left) / max(1, right - left)))
        seconds = fraction * self.duration
        if self.trim_marker_drag == "playhead":
            self._seek_to_time(seconds)
            return "break"
        count = self._frame_count()
        if self.trim_marker_drag == "start":
            end_frame = int(self.end_frame_var.get())
            frame = min(
                end_frame,
                max(1, int(math.floor(seconds / self.frame_duration + 0.5)) + 1),
            )
            self.start_frame_var.set(str(frame))
        else:
            start_frame = int(self.start_frame_var.get())
            frame = min(
                count,
                max(
                    start_frame,
                    int(math.floor(seconds / self.frame_duration + 0.5)),
                ),
            )
            self.end_frame_var.set(str(frame))
        self._sync_times_from_frames()
        return "break"

    def _end_trim_marker_drag(self, _event: tk.Event) -> str:
        self.trim_marker_drag = None
        return "break"

    def _go_to_time(self, _event: tk.Event | None = None) -> None:
        if not self.video_path:
            return
        try:
            seconds = parse_time(self.current_var.get())
        except ValueError as error:
            messagebox.showerror("Invalid time", str(error))
            return
        self._seek_to_time(seconds)

    def _frame_count(self) -> int:
        if self.duration <= 0 or self.frame_duration <= 0:
            return 0
        return max(1, math.ceil(self.duration / self.frame_duration - 1e-9))

    def _update_current_frame(self) -> None:
        count = self._frame_count()
        if count == 0:
            self.current_frame_var.set("— / —")
            return
        frame = min(
            count,
            max(
                1,
                int(math.floor(self.current_time / self.frame_duration + 1e-9))
                + 1,
            ),
        )
        self.current_frame_var.set(f"{frame:,} / {count:,}")

    def _sync_frames_from_times(self, _event: tk.Event | None = None) -> None:
        if not self.video_path:
            return
        try:
            start = parse_time(self.start_var.get())
            end = parse_time(self.end_var.get())
            if not 0 <= start <= self.duration or not 0 <= end <= self.duration:
                raise ValueError(f"Times must be between 0 and {format_time(self.duration)}.")
        except ValueError as error:
            messagebox.showerror("Invalid trim time", str(error))
            return

        if start > end:
            messagebox.showerror(
                "Invalid trim range",
                "The start time must be before or equal to the end time.",
            )
            return

        count = self._frame_count()
        start_frame = min(count, max(1, round(start / self.frame_duration) + 1))
        end_frame = min(
            count,
            max(start_frame, max(1, round(end / self.frame_duration))),
        )
        self.start_frame_var.set(str(start_frame))
        self.end_frame_var.set(str(end_frame))
        self._sync_times_from_frames()

    def _sync_times_from_frames(self, _event: tk.Event | None = None) -> None:
        if not self.video_path:
            return
        try:
            start_frame = int(self.start_frame_var.get())
            end_frame = int(self.end_frame_var.get())
        except ValueError:
            messagebox.showerror("Invalid frame", "Enter whole-number frame values.")
            return

        count = self._frame_count()
        if not 1 <= start_frame <= count or not 1 <= end_frame <= count:
            messagebox.showerror(
                "Invalid frame",
                f"Frame values must be between 1 and {count:,}.",
            )
            return
        if start_frame > end_frame:
            messagebox.showerror(
                "Invalid trim range",
                "The start frame must be before or equal to the end frame.",
            )
            return

        start_time = (start_frame - 1) * self.frame_duration
        end_time = min(end_frame * self.frame_duration, self.duration)
        self.start_var.set(format_time(start_time))
        self.end_var.set(format_time(end_time))
        self._draw_timeline_ruler()

    def _step_frame(self, direction: int) -> str:
        if not self.video_path:
            return "break"
        count = self._frame_count()
        frame = min(
            count,
            max(1, int(math.floor(self.current_time / self.frame_duration + 1e-9)) + 1),
        )
        next_frame = min(count, max(1, frame + direction))
        next_time = min((next_frame - 1) * self.frame_duration, self.duration)
        if math.isclose(next_time, self.current_time, abs_tol=1e-9):
            return "break"
        self._stop_playback()
        self.current_time = next_time
        self.current_var.set(format_time(next_time))
        self._update_current_frame()
        self._update_current_playhead()
        if self.seek_after_id:
            self.root.after_cancel(self.seek_after_id)
            self.seek_after_id = None
        if self.frame_search_direction is None:
            self._request_source_frame()
        else:
            self._request_frame_search_frame(next_frame - 1)
        return "break"

    def _start_frame_search(self, direction: int) -> str | None:
        if self.root.focus_get() in (
            self.seek_step_spinbox,
            self.frame_search_batch_spinbox,
            self.fps_spinbox,
            self.width_spinbox,
            self.current_entry,
            self.start_entry,
            self.end_entry,
            self.start_frame_entry,
            self.end_frame_entry,
        ):
            return None
        if self.frame_search_direction == direction:
            return "break"
        self._cancel_frame_search_hold()
        self.frame_search_direction = direction
        self.frame_search_continuous = False
        if not self.video_path:
            return "break"
        self.request_id += 1
        self.preview_pending = False
        self.frame_search_hold_after_id = self.root.after(
            FRAME_SEARCH_HOLD_DELAY_MS,
            lambda: self._begin_continuous_frame_search(direction),
        )
        return self._step_frame(direction)

    def _previous_frame(self, _event: tk.Event | None = None) -> str:
        return self._start_frame_search(-1)

    def _next_frame(self, _event: tk.Event | None = None) -> str:
        return self._start_frame_search(1)

    def _end_frame_search(self, event: tk.Event) -> str:
        direction = -1 if event.keysym == "Left" else 1
        if self.frame_search_direction == direction:
            self.frame_search_direction = None
            self.frame_search_continuous = False
            self._cancel_frame_search_hold()
        return "break"

    def _begin_continuous_frame_search(self, direction: int) -> None:
        self.frame_search_hold_after_id = None
        if self.frame_search_direction != direction:
            return
        self.frame_search_continuous = True
        self._step_frame(direction)

    def _cancel_frame_search_hold(self) -> None:
        if self.frame_search_hold_after_id is not None:
            self.root.after_cancel(self.frame_search_hold_after_id)
            self.frame_search_hold_after_id = None

    def _seek_by_seconds(self, direction: int) -> str | None:
        if self.root.focus_get() in (
            self.seek_step_spinbox,
            self.frame_search_batch_spinbox,
        ):
            return None
        if not self.video_path:
            return "break"
        try:
            step = float(self.seek_step_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid seek step", "Enter a positive number of seconds."
            )
            return "break"
        if not math.isfinite(step) or step <= 0:
            messagebox.showerror(
                "Invalid seek step", "Enter a positive number of seconds."
            )
            return "break"
        self._seek_to_time(self.current_time + direction * step)
        return "break"

    def _seek_forward(self, _event: tk.Event | None = None) -> str | None:
        return self._seek_by_seconds(1)

    def _seek_backward(self, _event: tk.Event | None = None) -> str | None:
        return self._seek_by_seconds(-1)

    def _toggle_playback(self, _event: tk.Event | None = None) -> str:
        if self.playback_process is not None:
            self._stop_playback()
            return "break"
        if not self.video_path or not self.ffmpeg:
            return "break"
        if self.current_time >= self.duration:
            self.current_time = 0
            self.current_var.set(format_time(0))
            self._update_current_frame()
            self._update_current_playhead()
        self.playback_generation += 1
        generation = self.playback_generation
        with self.playback_frame_lock:
            self.latest_playback_frame = None
        frame_width, frame_height = self._preview_decode_size()
        frame_filter = (
            f"fps={PLAYBACK_FPS},"
            f"scale={frame_width}:{frame_height},"
            "format=rgb24"
        )
        try:
            process = subprocess.Popen(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-re",
                    "-ss",
                    f"{self.current_time:.3f}",
                    "-i",
                    str(self.video_path),
                    "-an",
                    "-vf",
                    frame_filter,
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            messagebox.showerror("Playback failed", str(error))
            return "break"

        self.playback_process = process
        threading.Thread(
            target=self._read_playback_frames,
            args=(process, generation, self.current_time, frame_width, frame_height),
            daemon=True,
        ).start()
        return "break"

    def _read_playback_frames(
        self,
        process: subprocess.Popen[bytes],
        generation: int,
        start_time: float,
        frame_width: int,
        frame_height: int,
    ) -> None:
        frame_size = frame_width * frame_height * 3
        frame_index = 0
        try:
            if process.stdout is None:
                raise RuntimeError("Could not read video playback frames.")
            while True:
                frame = process.stdout.read(frame_size)
                if not frame:
                    break
                if len(frame) != frame_size:
                    raise RuntimeError("FFmpeg returned an incomplete video frame.")
                timestamp = min(
                    start_time + frame_index / PLAYBACK_FPS, self.duration
                )
                with self.playback_frame_lock:
                    self.latest_playback_frame = (
                        generation,
                        frame,
                        timestamp,
                        frame_width,
                        frame_height,
                    )
                frame_index += 1
            return_code = process.wait()
            if return_code and generation == self.playback_generation:
                self.events.put(
                    ("playback_error", generation, RuntimeError("FFmpeg playback failed."))
                )
            elif generation == self.playback_generation:
                self.events.put(("playback_done", generation, None))
        except (OSError, RuntimeError) as error:
            if generation == self.playback_generation:
                self.events.put(("playback_error", generation, error))

    def _stop_playback(self) -> None:
        process = self.playback_process
        if process is None:
            return
        self.playback_generation += 1
        self.playback_process = None
        with self.playback_frame_lock:
            self.latest_playback_frame = None
        if process.poll() is None:
            process.terminate()

    def _preview_decode_size(self) -> tuple[int, int]:
        if self.source_frame_image is None:
            return PREVIEW_WIDTH, PREVIEW_HEIGHT
        source_width, source_height = self.source_frame_image.size
        view_width = max(1, self.source_view.winfo_width() - 8)
        view_height = max(1, self.source_view.winfo_height() - 8)
        fit_scale = min(view_width / source_width, view_height / source_height)
        max_scale = min(
            PREVIEW_WIDTH / source_width,
            PREVIEW_HEIGHT / source_height,
        )
        scale = min(fit_scale * self.source_zoom, max_scale)
        return (
            max(1, round(source_width * scale)),
            max(1, round(source_height * scale)),
        )

    def _request_source_frame(self) -> None:
        self.seek_after_id = None
        if not self.video_path:
            return
        self.request_id += 1
        if self.preview_future is not None and not self.preview_future.done():
            self.preview_pending = True
            return
        self.preview_pending = False
        request_id = self.request_id
        width, height = self._preview_decode_size()
        future = self.executor.submit(
            self._extract_frame, self.video_path, self.current_time, width, height
        )
        self.preview_future = future
        future.add_done_callback(
            lambda completed: self._post_future("frame", request_id, completed)
        )

    def _request_frame_search_frame(self, frame_index: int) -> None:
        self.frame_search_target_index = frame_index
        cached_frame = self.frame_search_cache.get(frame_index)
        if cached_frame is not None:
            self._show_frame_search_image(cached_frame[0], cached_frame[1], cached_frame[2])
            self.frame_search_frame_ready = True
            self._prefetch_frame_search_batch()
            return
        if self.frame_search_batch_future is not None:
            return

        count = self._frame_count()
        batch_size = self._frame_search_batch_size()
        if batch_size is None:
            return

        if self.frame_search_direction == -1:
            first_frame_index = max(0, frame_index - batch_size + 1)
        else:
            first_frame_index = frame_index
        batch_count = min(batch_size, count - first_frame_index)
        self._submit_frame_search_batch(first_frame_index, batch_count)

    def _frame_search_batch_size(self) -> int | None:
        try:
            batch_size = int(self.frame_search_batch_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid frame-search batch size",
                f"Enter a whole number between 1 and {MAX_FRAME_SEARCH_BATCH_SIZE}.",
            )
            return None
        if not 1 <= batch_size <= MAX_FRAME_SEARCH_BATCH_SIZE:
            messagebox.showerror(
                "Invalid frame-search batch size",
                f"Enter a whole number between 1 and {MAX_FRAME_SEARCH_BATCH_SIZE}.",
            )
            return None
        return batch_size

    def _submit_frame_search_batch(
        self, first_frame_index: int, batch_count: int
    ) -> None:
        if not self.video_path or batch_count <= 0:
            return
        width, height = self._preview_decode_size()
        future = self.executor.submit(
            self._extract_frame_batch,
            self.video_path,
            first_frame_index,
            batch_count,
            width,
            height,
        )
        self.frame_search_batch_future = future
        generation = self.frame_search_generation
        future.add_done_callback(
            lambda completed: self._post_future(
                "frame_search_batch", generation, completed
            )
        )

    def _prefetch_frame_search_batch(self) -> None:
        if (
            not self.frame_search_continuous
            or self.frame_search_direction is None
            or self.frame_search_batch_future is not None
            or self.frame_search_target_index is None
            or not self.frame_search_cache
        ):
            return
        batch_size = self._frame_search_batch_size()
        if batch_size is None:
            self.frame_search_continuous = False
            return

        cached_indices = self.frame_search_cache.keys()
        if self.frame_search_direction > 0:
            last_cached_index = max(cached_indices)
            if last_cached_index - self.frame_search_target_index > batch_size:
                return
            first_frame_index = last_cached_index + 1
            batch_count = min(batch_size, self._frame_count() - first_frame_index)
        else:
            last_cached_index = min(cached_indices)
            if self.frame_search_target_index - last_cached_index > batch_size:
                return
            first_frame_index = max(0, last_cached_index - batch_size)
            batch_count = last_cached_index - first_frame_index
        self._submit_frame_search_batch(first_frame_index, batch_count)

    def _trim_frame_search_cache(self, batch_size: int) -> None:
        target_index = self.frame_search_target_index
        direction = self.frame_search_direction
        if target_index is None or direction is None:
            return
        if direction > 0:
            retained = sorted(
                index for index in self.frame_search_cache if index >= target_index
            )[: 2 * batch_size]
        else:
            retained = sorted(
                (index for index in self.frame_search_cache if index <= target_index),
                reverse=True,
            )[: 2 * batch_size]
        retained = set(retained)
        for index in self.frame_search_cache.keys() - retained:
            del self.frame_search_cache[index]

    def _show_frame_search_image(
        self, image_data: bytes, width: int, height: int
    ) -> None:
        image = Image.frombytes("RGB", (width, height), image_data)
        self.source_frame_image = image.convert("RGBA")
        image.close()
        self._render_source_preview(resample=Image.Resampling.BILINEAR)

    def _set_start(self) -> None:
        frame = min(
            self._frame_count(),
            max(1, int(math.floor(self.current_time / self.frame_duration + 1e-9)) + 1),
        )
        self.start_frame_var.set(str(frame))
        self._sync_times_from_frames()

    def _set_end(self) -> None:
        frame = min(
            self._frame_count(),
            max(1, int(math.floor(self.current_time / self.frame_duration + 1e-9)) + 1),
        )
        self.end_frame_var.set(str(frame))
        self._sync_times_from_frames()

    def _export_gif(self) -> None:
        if not self.video_path:
            return
        self._stop_playback()
        try:
            start_frame = int(self.start_frame_var.get())
            end_frame = int(self.end_frame_var.get())
            count = self._frame_count()
            if not 1 <= start_frame <= count or not 1 <= end_frame <= count:
                raise ValueError(f"Frame values must be between 1 and {count:,}.")
            start = (start_frame - 1) * self.frame_duration
            end = min(end_frame * self.frame_duration, self.duration)
            fps = int(self.fps_var.get())
            width = int(self.width_var.get())
        except (ValueError, tk.TclError) as error:
            messagebox.showerror("Invalid settings", str(error))
            return
        if start < 0 or end > self.duration or start >= end:
            messagebox.showerror(
                "Invalid selection",
                f"Choose a start and end between 0 and {format_time(self.duration)}, "
                "with start before end.",
            )
            return
        if not 1 <= fps <= 60 or not 100 <= width <= 1920:
            messagebox.showerror(
                "Invalid settings", "Use a frame rate from 1–60 and width from 100–1920."
            )
            return

        destination = filedialog.asksaveasfilename(
            title="Save GIF",
            defaultextension=".gif",
            initialfile=f"{self.video_path.stem}.gif",
            filetypes=[("GIF image", "*.gif")],
        )
        if not destination:
            return

        self.export_button.configure(state="disabled")
        self.status_var.set("Creating GIF…")
        self.export_generation += 1
        export_id = self.export_generation
        self.export_progress_var.set(0)
        self.export_percent_var.set("0%")
        future = self.executor.submit(
            self._create_gif,
            self.video_path,
            Path(destination),
            start,
            end,
            fps,
            width,
            export_id,
        )
        future.add_done_callback(
            lambda completed: self._post_future("exported", export_id, completed)
        )

    def _create_gif(
        self,
        source: Path,
        destination: Path,
        start: float,
        end: float,
        fps: int,
        width: int,
        export_id: int,
    ) -> Path:
        filter_graph = (
            f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
            "[a]palettegen[p];[b][p]paletteuse=dither=sierra2_4a"
        )
        process = subprocess.Popen(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostats",
                "-stats_period",
                "0.1",
                "-progress",
                "pipe:1",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{end - start:.3f}",
                "-i",
                str(source),
                "-an",
                "-filter_complex",
                filter_graph,
                "-loop",
                "0",
                "-y",
                str(destination),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.stdout is None:
            process.kill()
            raise RuntimeError("Could not read FFmpeg export progress.")

        for line in process.stdout:
            key, separator, value = line.partition("=")
            if not separator or key != "out_time_us":
                continue
            try:
                encoded_seconds = int(value) / 1_000_000
            except ValueError:
                continue
            percent = min(99, max(0, round(encoded_seconds / (end - start) * 100)))
            self.events.put(("export_progress", export_id, percent))

        _, error_output = process.communicate(timeout=3600)
        if process.returncode:
            error = error_output.strip()
            raise RuntimeError(error or "FFmpeg could not create the GIF.")
        return destination

    def _post_future(self, event: str, request_id: int, future: Future[object]) -> None:
        try:
            result: object = future.result()
        except Exception as error:
            result = error
        self.events.put((event, request_id, result))

    def _poll_events(self) -> None:
        try:
            while True:
                event, request_id, result = self.events.get_nowait()
                if event == "frame_search_batch":
                    if request_id != self.frame_search_generation:
                        continue
                    self.frame_search_batch_future = None
                    if isinstance(result, Exception):
                        messagebox.showerror("Frame search failed", str(result))
                        continue
                    if not isinstance(result, tuple) or len(result) != 4:
                        messagebox.showerror(
                            "Frame search failed",
                            "FFmpeg returned an invalid preview batch.",
                        )
                        continue
                    first_index, width, height, frames = result
                    self.frame_search_cache.update(
                        {
                            first_index + offset: (frame, width, height)
                            for offset, frame in enumerate(frames)
                        }
                    )
                    batch_size = self._frame_search_batch_size()
                    if batch_size is not None:
                        self._trim_frame_search_cache(batch_size)
                    target_index = self.frame_search_target_index
                    if target_index in self.frame_search_cache:
                        image_data, image_width, image_height = (
                            self.frame_search_cache[target_index]
                        )
                        self._show_frame_search_image(
                            image_data, image_width, image_height
                        )
                        self.frame_search_frame_ready = True
                        self._prefetch_frame_search_batch()
                    elif target_index is not None:
                        self._request_frame_search_frame(target_index)
                    continue
                if event in ("playback_done", "playback_error"):
                    if request_id == self.playback_generation:
                        self.playback_process = None
                        if event == "playback_error":
                            messagebox.showerror("Playback failed", str(result))
                    continue
                if event == "export_progress":
                    if request_id == self.export_generation:
                        progress = int(result)
                        self.export_progress_var.set(progress)
                        self.export_percent_var.set(f"{progress}%")
                    continue
                if event == "exported":
                    if self.video_path:
                        self.export_button.configure(state="normal")
                    if isinstance(result, Exception):
                        messagebox.showerror("GIF creation failed", str(result))
                        self.status_var.set("GIF creation failed.")
                    else:
                        self.export_progress_var.set(100)
                        self.export_percent_var.set("100%")
                        self._show_output(result)
                elif request_id == self.request_id:
                    if isinstance(result, Exception):
                        self.status_var.set("Could not load the video.")
                        messagebox.showerror("Video error", str(result))
                    elif event == "loaded":
                        self.duration, self.frame_duration, image = result
                        self._draw_timeline_ruler()
                        self.start_var.set(format_time(0))
                        self.end_var.set(format_time(self.duration))
                        frame_count = self._frame_count()
                        self.start_frame_var.set("1")
                        self.end_frame_var.set(str(frame_count))
                        self._draw_timeline_ruler()
                        self.frame_count_label.configure(
                            text=f"Total frames: {frame_count:,}"
                        )
                        self.current_time = 0
                        self.current_var.set(format_time(0))
                        self._update_current_frame()
                        self._update_current_playhead()
                        self.export_button.configure(state="normal")
                        self._show_source(image)
                        self.status_var.set(f"Ready — video duration {format_time(self.duration)}")
                    elif event == "frame":
                        self._show_source(result)
                        if self.frame_search_direction is not None:
                            self.frame_search_frame_ready = True
        except queue.Empty:
            pass
        with self.playback_frame_lock:
            playback_frame = self.latest_playback_frame
            self.latest_playback_frame = None
        if playback_frame is not None:
            generation, frame, timestamp, frame_width, frame_height = playback_frame
            if generation == self.playback_generation:
                self.source_frame_image = Image.frombytes(
                    "RGB", (frame_width, frame_height), frame
                )
                self._render_source_preview(resample=Image.Resampling.BILINEAR)
                self.current_time = timestamp
                self.current_var.set(format_time(timestamp))
                self._update_current_frame()
                self._update_current_playhead()
        if self.preview_future is not None and self.preview_future.done():
            self.preview_future = None
            if self.preview_pending:
                self._request_source_frame()
        frame_search_frame_ready = self.frame_search_frame_ready
        self.frame_search_frame_ready = False
        if (
            frame_search_frame_ready
            and self.frame_search_direction is not None
            and self.frame_search_continuous
        ):
            self._step_frame(self.frame_search_direction)
        poll_interval = (
            FRAME_SEARCH_POLL_MS
            if self.frame_search_continuous
            else PLAYBACK_POLL_MS
            if (
                self.playback_process is not None
                or self.preview_pending
            )
            else 80
        )
        self.root.after(poll_interval, self._poll_events)

    def _show_source(self, image_data: bytes) -> None:
        with Image.open(BytesIO(image_data)) as image:
            self.source_frame_image = image.convert("RGBA")
        self._render_source_preview()

    def _resize_source_preview(self, _event: tk.Event | None = None) -> None:
        if self.source_frame_image is not None:
            self._render_source_preview()

    def _on_source_mousewheel(self, event: tk.Event) -> str:
        if self.source_frame_image is None or event.delta == 0:
            return "break"

        old_width = self.source_view.winfo_width() - 8
        old_height = self.source_view.winfo_height() - 8
        if old_width <= 1 or old_height <= 1:
            return "break"

        image_width, image_height = self.source_frame_image.size
        fit_scale = min(old_width / image_width, old_height / image_height)
        old_draw_width = image_width * fit_scale * self.source_zoom
        old_draw_height = image_height * fit_scale * self.source_zoom
        old_left = (
            (old_width - old_draw_width) / 2
            if old_draw_width <= old_width
            else old_width / 2 - self.source_zoom_center[0] * old_draw_width
        )
        old_top = (
            (old_height - old_draw_height) / 2
            if old_draw_height <= old_height
            else old_height / 2 - self.source_zoom_center[1] * old_draw_height
        )
        pointer_x = min(old_width, max(0, event.x - 4))
        pointer_y = min(old_height, max(0, event.y - 4))
        image_point_x = min(1.0, max(0.0, (pointer_x - old_left) / old_draw_width))
        image_point_y = min(1.0, max(0.0, (pointer_y - old_top) / old_draw_height))

        zoom_factor = 1.2 if event.delta > 0 else 1 / 1.2
        new_zoom = min(8.0, max(0.25, self.source_zoom * zoom_factor))
        if math.isclose(new_zoom, self.source_zoom):
            return "break"
        self.source_zoom = new_zoom

        new_draw_width = image_width * fit_scale * new_zoom
        new_draw_height = image_height * fit_scale * new_zoom
        center_x = (
            image_point_x - (pointer_x - old_width / 2) / new_draw_width
        )
        center_y = (
            image_point_y - (pointer_y - old_height / 2) / new_draw_height
        )
        self.source_zoom_center = (
            min(1.0, max(0.0, center_x)),
            min(1.0, max(0.0, center_y)),
        )
        self._render_source_preview()
        return "break"

    def _reset_source_zoom(self, _event: tk.Event | None = None) -> str:
        self.source_zoom = 1.0
        self.source_zoom_center = (0.5, 0.5)
        self._render_source_preview()
        return "break"

    def _render_source_preview(
        self, resample: Image.Resampling = Image.Resampling.LANCZOS
    ) -> None:
        if self.source_frame_image is None:
            return
        width = self.source_view.winfo_width() - 8
        height = self.source_view.winfo_height() - 8
        if width <= 1 or height <= 1:
            return

        image_width, image_height = self.source_frame_image.size
        scale = min(width / image_width, height / image_height) * self.source_zoom
        target_size = (
            max(1, round(image_width * scale)),
            max(1, round(image_height * scale)),
        )
        resized = self.source_frame_image.resize(
            target_size, resample
        )
        left = (
            (width - target_size[0]) // 2
            if target_size[0] <= width
            else round(width / 2 - self.source_zoom_center[0] * target_size[0])
        )
        top = (
            (height - target_size[1]) // 2
            if target_size[1] <= height
            else round(height / 2 - self.source_zoom_center[1] * target_size[1])
        )
        image = Image.new("RGBA", (width, height), "#f0f0f0")
        image.paste(resized, (left, top))
        resized.close()
        self.source_photo = ImageTk.PhotoImage(image=image)
        image.close()
        self.source_view.configure(image=self.source_photo, text="")

    def _show_output(self, result: object) -> None:
        if not isinstance(result, Path):
            return
        self._stop_output_animation()
        try:
            self.output_image = Image.open(result)
            self.output_frame_index = 0
            self.status_var.set(f"GIF created: {result}")
            self._animate_output()
        except (OSError, ValueError) as error:
            self.output_image = None
            messagebox.showerror("Preview error", str(error))

    def _animate_output(self) -> None:
        if not self.output_image:
            return
        self.output_image.seek(self.output_frame_index)
        frame = self.output_image.convert("RGBA")
        frame.thumbnail((PREVIEW_WIDTH, PREVIEW_HEIGHT), Image.Resampling.LANCZOS)
        self.output_photo = ImageTk.PhotoImage(frame)
        self.output_view.configure(image=self.output_photo, text="")
        delay = max(20, self.output_image.info.get("duration", 100))
        self.output_frame_index = (self.output_frame_index + 1) % self.output_image.n_frames
        self.output_after_id = self.root.after(delay, self._animate_output)

    def _stop_output_animation(self) -> None:
        if self.output_after_id:
            self.root.after_cancel(self.output_after_id)
            self.output_after_id = None
        if self.output_image:
            self.output_image.close()
            self.output_image = None

    def _close(self) -> None:
        self._stop_playback()
        self._stop_output_animation()
        if self.source_frame_image is not None:
            self.source_frame_image.close()
            self.source_frame_image = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main() -> None:
    root = TkinterDnD.Tk()
    GifMaker(root)
    root.mainloop()


if __name__ == "__main__":
    main()
