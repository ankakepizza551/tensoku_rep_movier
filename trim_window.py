"""動画プレビュー付き切り抜きウィンドウ。"""

import subprocess
import threading
import time
from pathlib import Path

import customtkinter as ctk
import tkinter as tk

from recorder import find_ffmpeg


class TrimWindow(ctk.CTkToplevel):
    _PREVIEW_W = 480
    _PREVIEW_H = 270

    def __init__(self, parent, video_path: str):
        super().__init__(parent)
        self.title(f"切り抜き  —  {Path(video_path).name}")
        self.geometry("560x720")
        self.resizable(False, False)
        self.lift()
        self.focus_force()

        self._video_path = video_path
        self._cap = None
        self._total_frames = 1
        self._fps = 60.0
        self._start_frame = 0
        self._end_frame = 0
        self._active = "end"   # 最後に操作したバー (±秒ボタンの対象)
        self._playing = False
        self._play_frame = 0
        self._play_start_time: float = 0.0
        self._play_start_frame: int = 0
        self._photo = None

        try:
            self._load_video()
        except ImportError:
            ctk.CTkLabel(
                self,
                text="opencv-python と Pillow が必要です\npip install opencv-python Pillow",
                font=ctk.CTkFont(size=13),
            ).pack(expand=True)
            return
        except Exception as e:
            ctk.CTkLabel(self, text=f"動画を開けませんでした:\n{e}").pack(expand=True)
            return

        self._end_frame = self._total_frames - 1
        self._build_ui()
        self._show_frame(0)

    # ── 動画読み込み ─────────────────────────
    def _load_video(self) -> None:
        import cv2
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(self._video_path)
        if not self._cap.isOpened():
            raise RuntimeError("VideoCapture.open() 失敗")
        self._total_frames = max(1, int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        self._fps = self._cap.get(cv2.CAP_PROP_FPS) or 60.0

    # ── UI 構築 ──────────────────────────────
    def _build_ui(self) -> None:
        total_sec = self._total_frames / self._fps

        # プレビューキャンバス
        self._canvas = tk.Canvas(
            self, width=self._PREVIEW_W, height=self._PREVIEW_H,
            bg="black", highlightthickness=0,
        )
        self._canvas.pack(padx=10, pady=(10, 2))

        # 現在フレーム時刻
        self._time_var = ctk.StringVar(value="0:00.00")
        ctk.CTkLabel(self, textvariable=self._time_var,
                     font=ctk.CTkFont(size=12)).pack(pady=(0, 4))

        # ── 開始シークバー ──
        s_hdr = ctk.CTkFrame(self, fg_color="transparent")
        s_hdr.pack(fill="x", padx=14)
        ctk.CTkLabel(s_hdr, text="開始", font=ctk.CTkFont(weight="bold"),
                     text_color="#2ecc71").pack(side="left")
        self._start_lbl = ctk.CTkLabel(s_hdr, text="0:00.00", text_color="#2ecc71")
        self._start_lbl.pack(side="right")

        self._start_var = tk.DoubleVar(value=0)
        self._start_bar = tk.Scale(
            self, variable=self._start_var,
            from_=0, to=self._total_frames - 1,
            orient=tk.HORIZONTAL, length=536, showvalue=False,
            bg="#2b2b2b", fg="#2ecc71", troughcolor="#3a3a3a",
            activebackground="#27ae60", highlightthickness=0, bd=0,
            command=self._on_start_drag,
        )
        self._start_bar.pack(padx=10)

        s_ctrl = ctk.CTkFrame(self, fg_color="transparent")
        s_ctrl.pack(pady=(2, 6))
        for label, secs in [("◀◀ -5s", -5), ("◀ -1s", -1), ("+1s ▶", 1), ("+5s ▶▶", 5)]:
            ctk.CTkButton(s_ctrl, text=label, width=72, fg_color="#1a5e35", hover_color="#27ae60",
                          command=lambda s=secs: self._step_start(s)).pack(side="left", padx=2)

        # ── 終了シークバー ──
        e_hdr = ctk.CTkFrame(self, fg_color="transparent")
        e_hdr.pack(fill="x", padx=14)
        ctk.CTkLabel(e_hdr, text="終了", font=ctk.CTkFont(weight="bold"),
                     text_color="#e74c3c").pack(side="left")
        self._end_lbl = ctk.CTkLabel(e_hdr, text=self._fmt(total_sec),
                                      text_color="#e74c3c")
        self._end_lbl.pack(side="right")

        self._end_var = tk.DoubleVar(value=self._total_frames - 1)
        self._end_bar = tk.Scale(
            self, variable=self._end_var,
            from_=0, to=self._total_frames - 1,
            orient=tk.HORIZONTAL, length=536, showvalue=False,
            bg="#2b2b2b", fg="#e74c3c", troughcolor="#3a3a3a",
            activebackground="#c0392b", highlightthickness=0, bd=0,
            command=self._on_end_drag,
        )
        self._end_bar.pack(padx=10)

        e_ctrl = ctk.CTkFrame(self, fg_color="transparent")
        e_ctrl.pack(pady=(2, 6))
        for label, secs in [("◀◀ -5s", -5), ("◀ -1s", -1), ("+1s ▶", 1), ("+5s ▶▶", 5)]:
            ctk.CTkButton(e_ctrl, text=label, width=72, fg_color="#7b1a1a", hover_color="#c0392b",
                          command=lambda s=secs: self._step_end(s)).pack(side="left", padx=2)

        # ── 再生ボタン ──
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(pady=(0, 4))
        self._play_btn = ctk.CTkButton(
            ctrl, text="▶ 再生 (開始→終了)", width=180,
            font=ctk.CTkFont(weight="bold"),
            command=self._toggle_play,
        )
        self._play_btn.pack()

        # ── 精確切り抜きオプション ──
        self._precise_var = tk.BooleanVar(value=False)
        opt_row = ctk.CTkFrame(self, fg_color="transparent")
        opt_row.pack(fill="x", padx=14, pady=(0, 2))
        ctk.CTkCheckBox(
            opt_row, text="精確切り抜き（再エンコード・低速）",
            variable=self._precise_var,
        ).pack(side="left")
        ctk.CTkLabel(
            opt_row,
            text="  ※ OFF はキーフレーム境界に丸まりますが高速",
            font=ctk.CTkFont(size=10), text_color="#888888",
        ).pack(side="left")

        # ── 実行ボタン + ステータス ──
        self._status_var = ctk.StringVar(value="")
        ctk.CTkLabel(self, textvariable=self._status_var,
                     text_color="#aaaaaa").pack(pady=(2, 0))
        self._trim_btn = ctk.CTkButton(
            self, text="切り抜き実行", width=180, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._run_trim,
        )
        self._trim_btn.pack(pady=(4, 12))

    # ── フレーム描画 ─────────────────────────
    def _show_frame(self, frame_num: int) -> None:
        from PIL import Image, ImageTk

        frame_num = max(0, min(frame_num, self._total_frames - 1))

        self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = self._cap.read()
        if ret:
            frame = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
            h, w = frame.shape[:2]
            scale = min(self._PREVIEW_W / w, self._PREVIEW_H / h)
            nw, nh = int(w * scale), int(h * scale)
            frame = self._cv2.resize(frame, (nw, nh), interpolation=self._cv2.INTER_AREA)
            img = Image.fromarray(frame)
            self._photo = ImageTk.PhotoImage(img)
            self._canvas.delete("all")
            self._canvas.create_image(
                self._PREVIEW_W // 2, self._PREVIEW_H // 2,
                image=self._photo, anchor="center",
            )

        self._time_var.set(self._fmt(frame_num / self._fps))

    @staticmethod
    def _fmt(sec: float) -> str:
        m = int(sec) // 60
        s = sec % 60
        return f"{m}:{s:05.2f}"

    # ── シーク ───────────────────────────────
    def _on_start_drag(self, val) -> None:
        self._playing = False
        self._play_btn.configure(text="▶ 再生 (開始→終了)")
        frame = int(float(val))
        self._start_frame = frame
        self._start_lbl.configure(text=self._fmt(frame / self._fps))
        self._show_frame(frame)

    def _on_end_drag(self, val) -> None:
        self._playing = False
        self._play_btn.configure(text="▶ 再生 (開始→終了)")
        frame = int(float(val))
        self._end_frame = frame
        self._end_lbl.configure(text=self._fmt(frame / self._fps))
        self._show_frame(frame)

    def _step_start(self, seconds: float) -> None:
        self._playing = False
        self._play_btn.configure(text="▶ 再生 (開始→終了)")
        new = max(0, min(self._start_frame + int(seconds * self._fps), self._total_frames - 1))
        self._start_frame = new
        self._start_var.set(new)
        self._start_lbl.configure(text=self._fmt(new / self._fps))
        self._show_frame(new)

    def _step_end(self, seconds: float) -> None:
        self._playing = False
        self._play_btn.configure(text="▶ 再生 (開始→終了)")
        new = max(0, min(self._end_frame + int(seconds * self._fps), self._total_frames - 1))
        self._end_frame = new
        self._end_var.set(new)
        self._end_lbl.configure(text=self._fmt(new / self._fps))
        self._show_frame(new)

    # ── 再生 (開始→終了 トリムプレビュー) ───────
    def _toggle_play(self) -> None:
        self._playing = not self._playing
        if self._playing:
            self._play_btn.configure(text="⏸ 停止")
            self._play_start_frame = self._start_frame
            self._play_start_time = time.time()
            self._tick_play()
        else:
            self._play_btn.configure(text="▶ 再生 (開始→終了)")

    def _tick_play(self) -> None:
        if not self._playing:
            return
        end = max(self._start_frame, self._end_frame)
        elapsed = time.time() - self._play_start_time
        frame = self._play_start_frame + int(elapsed * self._fps)
        if frame > end:
            self._playing = False
            self._play_btn.configure(text="▶ 再生 (開始→終了)")
            return
        self._show_frame(frame)
        self.after(max(1, int(1000 / self._fps)), self._tick_play)

    # ── 切り抜き実行 ─────────────────────────
    def _run_trim(self) -> None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self._status_var.set("ffmpeg が見つかりません")
            return

        start_f = min(self._start_frame, self._end_frame)
        end_f   = max(self._start_frame, self._end_frame)
        start_sec = f"{start_f / self._fps:.3f}"
        duration  = f"{(end_f - start_f) / self._fps:.3f}"
        end_sec   = f"{end_f / self._fps:.3f}"

        src = Path(self._video_path)
        # 既存の切り抜きを上書きしないよう、空いている名前を探す (_clip, _clip2, ...)
        out = src.parent / f"{src.stem}_clip{src.suffix}"
        n = 2
        while out.exists():
            out = src.parent / f"{src.stem}_clip{n}{src.suffix}"
            n += 1

        if self._precise_var.get():
            # 再エンコード: -ss を -i の前に置いてフレーム精度で切り出す
            cmd = [
                ffmpeg, "-y",
                "-ss", start_sec,
                "-i", str(src),
                "-t", duration,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                str(out),
            ]
        else:
            # ストリームコピー: 高速だがキーフレーム境界に丸まる
            cmd = [
                ffmpeg, "-y",
                "-i", str(src),
                "-ss", start_sec,
                "-to", end_sec,
                "-c", "copy",
                str(out),
            ]

        self._trim_btn.configure(state="disabled")
        self._status_var.set("切り抜き中...")

        def _worker():
            try:
                CREATE_NO_WINDOW = 0x08000000
                result = subprocess.run(
                    cmd, capture_output=True, timeout=300,
                    creationflags=CREATE_NO_WINDOW,
                )
                if result.returncode == 0:
                    self.after(0, lambda: self._status_var.set(f"完了: {out.name}"))
                else:
                    lines = result.stderr.decode("utf-8", errors="replace").splitlines()
                    msg = next((l.strip() for l in reversed(lines) if l.strip()), "不明なエラー")
                    self.after(0, lambda: self._status_var.set(f"エラー: {msg}"))
            except Exception as e:
                self.after(0, lambda: self._status_var.set(f"エラー: {e}"))
            finally:
                self.after(0, lambda: self._trim_btn.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    # ── クリーンアップ ───────────────────────
    def destroy(self) -> None:
        self._playing = False
        if self._cap:
            self._cap.release()
            self._cap = None
        super().destroy()
