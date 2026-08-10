"""複数の動画ファイルを1本に結合するウィンドウ。"""

import os
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from recorder import find_ffmpeg


class ConcatWindow(ctk.CTkToplevel):
    def __init__(self, parent, initial_files: list[str] | None = None):
        super().__init__(parent)
        self.title("動画を結合")
        self.geometry("560x520")
        self.resizable(False, False)
        self.lift()
        self.focus_force()

        self._files: list[str] = list(initial_files or [])
        self._build_ui()
        for f in self._files:
            self._listbox.insert(tk.END, Path(f).name)
        if self._files:
            self._out_var.set(str(Path(self._files[0]).parent / "combined.mp4"))

    def _build_ui(self) -> None:
        ctk.CTkLabel(self, text="結合する動画ファイル（上から順に結合）",
                     font=ctk.CTkFont(weight="bold")).pack(pady=(12, 4), padx=12, anchor="w")

        # ── ファイルリスト ──
        list_frame = ctk.CTkFrame(self)
        list_frame.pack(fill="x", padx=12, pady=(0, 6))

        lb_wrap = tk.Frame(list_frame, bg="#3b3b3b", bd=1, relief="flat")
        lb_wrap.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="nsew")
        self._listbox = tk.Listbox(
            lb_wrap, height=8, bg="#2b2b2b", fg="white",
            selectbackground="#1f6aa5", activestyle="none",
            bd=0, highlightthickness=0, font=("Yu Gothic UI", 10),
        )
        sb = tk.Scrollbar(lb_wrap, orient=tk.VERTICAL, command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=sb.set)
        self._listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btn_col = ctk.CTkFrame(list_frame, fg_color="transparent")
        btn_col.grid(row=0, column=1, padx=(0, 8), pady=8, sticky="n")
        ctk.CTkButton(btn_col, text="追加", width=60, command=self._add).pack(pady=2)
        ctk.CTkButton(btn_col, text="削除", width=60, command=self._remove).pack(pady=2)
        ctk.CTkButton(btn_col, text="↑",   width=60, command=self._move_up).pack(pady=2)
        ctk.CTkButton(btn_col, text="↓",   width=60, command=self._move_down).pack(pady=2)

        list_frame.columnconfigure(0, weight=1)

        # ── 出力ファイル ──
        out_frame = ctk.CTkFrame(self)
        out_frame.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(out_frame, text="出力ファイル").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self._out_var = ctk.StringVar()
        ctk.CTkEntry(out_frame, textvariable=self._out_var, width=350).grid(row=0, column=1, padx=4)
        ctk.CTkButton(out_frame, text="参照", width=60, command=self._browse_out).grid(row=0, column=2, padx=4)

        # ── ステータス + 実行 ──
        self._status_var = ctk.StringVar(value="")
        ctk.CTkLabel(self, textvariable=self._status_var,
                     text_color="#aaaaaa").pack(pady=(4, 0))
        self._btn = ctk.CTkButton(
            self, text="結合実行", width=180, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._run_concat,
        )
        self._btn.pack(pady=(4, 14))

    # ── リスト操作 ──────────────────────────
    def _add(self) -> None:
        paths = filedialog.askopenfilenames(
            title="結合する動画を選択（複数可）",
            filetypes=[("動画ファイル", "*.mp4 *.mkv *.avi"), ("すべて", "*.*")],
        )
        for p in paths:
            if p not in self._files:
                self._files.append(p)
                self._listbox.insert(tk.END, Path(p).name)
        if self._files and not self._out_var.get():
            self._out_var.set(str(Path(self._files[0]).parent / "combined.mp4"))

    def _remove(self) -> None:
        for i in reversed(self._listbox.curselection()):
            self._listbox.delete(i)
            self._files.pop(i)

    def _move_up(self) -> None:
        sel = self._listbox.curselection()
        if not sel or sel[0] == 0:
            return
        i = sel[0]
        self._files[i - 1], self._files[i] = self._files[i], self._files[i - 1]
        prev, cur = self._listbox.get(i - 1), self._listbox.get(i)
        self._listbox.delete(i - 1, i)
        self._listbox.insert(i - 1, cur)
        self._listbox.insert(i, prev)
        self._listbox.selection_set(i - 1)

    def _move_down(self) -> None:
        sel = self._listbox.curselection()
        if not sel or sel[0] >= len(self._files) - 1:
            return
        i = sel[0]
        self._files[i], self._files[i + 1] = self._files[i + 1], self._files[i]
        cur, nxt = self._listbox.get(i), self._listbox.get(i + 1)
        self._listbox.delete(i, i + 1)
        self._listbox.insert(i, nxt)
        self._listbox.insert(i + 1, cur)
        self._listbox.selection_set(i + 1)

    def _browse_out(self) -> None:
        init_dir = str(Path(self._files[0]).parent) if self._files else ""
        p = filedialog.asksaveasfilename(
            title="出力ファイル名",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("すべて", "*.*")],
            initialdir=init_dir,
        )
        if p:
            self._out_var.set(p)

    # ── 結合実行 ─────────────────────────────
    def _run_concat(self) -> None:
        if len(self._files) < 2:
            self._status_var.set("2本以上選択してください")
            return
        out = self._out_var.get().strip()
        if not out:
            self._status_var.set("出力ファイルを指定してください")
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self._status_var.set("ffmpeg が見つかりません")
            return

        self._btn.configure(state="disabled")
        self._status_var.set("結合中...")
        files = list(self._files)

        def _worker():
            try:
                fd, list_path = tempfile.mkstemp(suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for p in files:
                        escaped = p.replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")
                CREATE_NO_WINDOW = 0x08000000
                result = subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                     "-i", list_path, "-c", "copy", out],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                    timeout=3600,
                )
                os.unlink(list_path)
                if result.returncode == 0:
                    self.after(0, lambda: self._status_var.set(f"完了: {Path(out).name}"))
                else:
                    lines = result.stderr.splitlines()
                    msg = next((l.strip() for l in reversed(lines) if l.strip()), "不明なエラー")
                    self.after(0, lambda: self._status_var.set(f"エラー: {msg}"))
            except Exception as e:
                self.after(0, lambda: self._status_var.set(f"エラー: {e}"))
            finally:
                self.after(0, lambda: self._btn.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()
