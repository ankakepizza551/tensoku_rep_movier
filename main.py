"""東方非想天則 リプレイ → MP4 変換ツール"""

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import config as cfg_mod
import game
import recorder as rec_mod
import rep_parser

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

APP_TITLE = "非想天則 リプレイ録画ツール"
APP_VERSION = "1.1.0"
WIDTH, HEIGHT = 620, 920

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# 日本語をクッキリ表示するため Yu Gothic UI に上書き (Windows 8.1+)
try:
    _font_cfg = ctk.ThemeManager.theme["CTkFont"]
    if isinstance(_font_cfg, dict) and "Windows" in _font_cfg:
        _font_cfg["Windows"]["family"] = "Yu Gothic UI"
    elif isinstance(_font_cfg, dict):
        _font_cfg["family"] = "Yu Gothic UI"
except Exception:
    pass

_INVALID_NAME_CHARS = '\\/:*?"<>|'

_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]


# ──────────────────────────────────────────────
# ワーカースレッド
# ──────────────────────────────────────────────
class RecordSession:
    def __init__(self, cfg: dict, rep_path: str, output_path: str, log_q: queue.Queue):
        self.cfg = cfg
        self.rep_path = rep_path
        self.output_path = output_path
        self.log_q = log_q
        self._stop_event = threading.Event()
        self._recorder = rec_mod.Recorder(log=self._log)
        self._game_proc = None
        self._prev_default_device: str | None = None   # ゲーム音のみモードで退避した既定デバイス

    def _log(self, msg: str) -> None:
        self.log_q.put(msg)

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            self._run_inner()
        except Exception as e:
            self._log(f"[エラー] {e}")
        finally:
            self._cleanup()
            self.log_q.put("__DONE__")

    def _run_inner(self) -> None:
        cfg = self.cfg

        if game.is_already_running(cfg["th123_path"]):
            raise RuntimeError(
                "th123.EXE がすでに起動中です。\n"
                "先にゲームを閉じてから録画開始してください。"
            )

        self._log("リプレイファイルをゲームフォルダにコピー中...")
        dest = game.copy_rep_to_game(self.rep_path, cfg["th123_path"])
        self._rep_dest = dest

        auto_dur = rep_parser.auto_stop_duration(self.rep_path) if cfg.get("auto_stop", True) else None
        est = rep_parser.estimate_duration(self.rep_path)
        if est is not None:
            m, s = divmod(int(est), 60)
            self._log(f"推定リプレイ時間: {m}分{s:02d}秒")
        else:
            self._log("リプレイ時間を推定できませんでした (手動停止してください)")

        # ── ゲーム音のみモード: 起動前に VB-Cable を既定デバイスに一時変更 ──
        _routing_ok = False
        _vb_name: str | None = None
        if cfg.get("game_audio_only", False):
            try:
                import audio_routing
                vb = audio_routing.find_vbcable()
                if vb:
                    self._prev_default_device = audio_routing.get_default_playback_device_id()
                    if audio_routing.set_default_playback_device(vb[1]):
                        _routing_ok = True
                        _vb_name = vb[0]
                        self._log(f"[ゲーム音のみ] 既定デバイスを「{vb[0]}」に変更しました")
                    else:
                        self._log("[警告] 既定デバイスの変更に失敗しました (通常ルーティングで続行)")
                        self._prev_default_device = None
                else:
                    self._log("[警告] VB-Cable が見つかりません (通常ルーティングで続行)")
                    self._log("  → https://vb-audio.com/Cable/ からインストールしてください")
            except Exception as e:
                self._log(f"[警告] ゲーム音ルーティング失敗: {e}")

        self._log("th123.EXE を起動しています...")
        try:
            self._game_proc = game.launch_game(cfg["th123_path"])
            hwnd = game.wait_for_window(self._game_proc.pid, timeout=30, log=self._log)
        finally:
            # 起動後すぐに既定デバイスを元に戻す（th123 はすでに VB-Cable を掴んでいる）。
            # 起動に失敗した場合も必ず戻す。
            self._restore_default_device()

        game.disable_rounded_corners(hwnd)

        # ゲームウィンドウの位置をメインスレッドに通知 → ツールウィンドウを隣に移動
        import win32gui as _wg
        r = _wg.GetWindowRect(hwnd)
        self.log_q.put(f"__GAME_RECT__:{r[0]}:{r[1]}:{r[2]}:{r[3]}")

        # タイトル画面に着くまで待つ。ロゴ表示中に Z を送るとロゴスキップに消費されて
        # 以降のキーが 1 つずつずれ、リプレイではなく別のメニューに入ってしまう。
        pid = self._game_proc.pid
        wait = cfg["wait_after_launch"]
        self._log("タイトル画面を待っています...")
        reached = game.wait_for_scene(
            pid, (game.SCENE_TITLE,), timeout=max(wait, 60.0),
            should_stop=self._stop_event.is_set,
        )
        if self._stop_event.is_set():
            return
        if reached:
            time.sleep(1.0)   # タイトル表示直後は入力を受け付けないため少し待つ
        else:
            # シーンが読めない (非対応バージョン等) 場合は従来通り固定秒数で待つ
            if reached is None:
                self._log(f"画面状態を取得できないため固定で待ちます ({wait:.0f}秒)...")
            else:
                self._log("[警告] タイトル画面への到達を確認できませんでした (そのまま続行)")
            for _ in range(int(wait * 2)):
                if self._stop_event.is_set():
                    return
                time.sleep(0.5)

        if self._stop_event.is_set():
            return

        game.send_key_sequence(
            hwnd,
            down_count=cfg["down_count"],
            z_after_select=cfg["z_after_select"],
            key_delay=cfg["key_delay"],
            log=self._log,
        )

        # リプレイ再生 (ロード → 対戦画面) に入れたか確認。入れていなければ録画しない
        if reached:
            playing = game.wait_for_scene(
                pid, (game.SCENE_LOADING, game.SCENE_BATTLE), timeout=10.0,
                should_stop=self._stop_event.is_set,
            )
            if self._stop_event.is_set():
                return
            if playing is False:
                raise RuntimeError(
                    "リプレイ再生を開始できませんでした。\n"
                    "「メニュー↓回数」「選択後Z回数」を確認してください。"
                )

        audio_dev = cfg.get("audio_device", "なし(無音)")
        # ゲーム音のみモード: ルーティング成功時のみ VB-Cable ループバックを自動選択
        if cfg.get("game_audio_only", False) and _routing_ok:
            try:
                import audio_routing
                vb_lb = audio_routing.find_vbcable_loopback_name(_vb_name)
                if vb_lb:
                    audio_dev = vb_lb
                    self._log(f"[ゲーム音のみ] 音声キャプチャ: {vb_lb}")
            except Exception:
                pass
        audio_dev_arg = None if audio_dev == "なし(無音)" else audio_dev

        self._hwnd = hwnd
        self._recorder.start(
            hwnd=hwnd,
            output_path=self.output_path,
            framerate=cfg["framerate"],
            crf=cfg["crf"],
            preset=cfg["preset"],
            audio_device=audio_dev_arg,
        )
        self._log("録画中 (ウィンドウ単位でキャプチャしています。最小化しなければ他の作業をしても問題ありません)")

        self._log("リプレイ再生中...")
        deadline = (time.time() + auto_dur) if auto_dur is not None else None
        _next_notice = time.time() + 60.0

        # 画面静止によるリプレイ終了検出
        # 試合間の暗転 (通常10-15秒) で誤発火しないよう、検出秒数を長めに取る。
        _STATIC_SECS = 25   # この秒数以上画面が静止したらリプレイ終了と判定
        _GRACE_SECS  = 30   # 録画開始後この秒数経つまでは静止検出しない
        _record_start = time.time()
        _last_pixels: list | None = None
        _static_since: float | None = None
        _next_sample = 0.0
        _seen_battle = False    # 対戦画面に入ったのを確認済みか (シーン遷移による終了検出用)

        while True:
            if self._stop_event.is_set():
                self._log("停止を受け付けました")
                break
            if not game.is_running(self._game_proc):
                self._log("ゲームが終了しました")
                break
            now = time.time()
            if deadline is not None and now >= deadline:
                self._log("自動停止: 推定再生時間を超えました")
                break

            # 対戦画面を抜けた (リプレイメニューに戻った) らリプレイ終了。
            # 画面静止検出より正確で、終了後のメニュー画面が動画に残らない。
            if cfg.get("auto_stop", True):
                scene = game.read_scene(pid)
                if scene is not None:
                    # 片方だけ変わった時点はまだ暗転中。両方変わるまで待って暗転も録る
                    if game.SCENE_BATTLE in scene:
                        _seen_battle = _seen_battle or scene[0] == scene[1]
                    elif _seen_battle:
                        self._log("リプレイ終了を検出しました")
                        break

            elapsed = now - _record_start
            if elapsed >= _GRACE_SECS and cfg.get("auto_stop", True) and now >= _next_sample:
                _next_sample = now + 0.5
                pixels = game.sample_window_pixels(self._hwnd)
                if pixels is not None:
                    if _last_pixels is not None:
                        changed = sum(p != q for p, q in zip(pixels, _last_pixels))
                        if changed <= 2:                # ほぼ変化なし = 静止
                            if _static_since is None:
                                _static_since = now
                            elif now - _static_since >= _STATIC_SECS:
                                self._log("リプレイ終了を検出しました (画面静止)")
                                break
                        else:
                            _static_since = None        # 動いていたのでリセット
                    _last_pixels = pixels

            if deadline is not None and now >= _next_notice:
                remaining = int(deadline - now)
                rm, rs = divmod(remaining, 60)
                self._log(f"[自動停止まで残り {rm}分{rs:02d}秒]")
                _next_notice = now + 60.0
            time.sleep(0.1)

    def _restore_default_device(self) -> None:
        prev, self._prev_default_device = self._prev_default_device, None
        if not prev:
            return
        try:
            import audio_routing
            if audio_routing.set_default_playback_device(prev):
                self._log("[ゲーム音のみ] 既定デバイスを元に戻しました")
            else:
                self._log("[警告] 既定デバイスの復元に失敗しました")
        except Exception as e:
            self._log(f"[警告] デバイス復元エラー: {e}")

    def _cleanup(self) -> None:
        self._restore_default_device()
        self._recorder.stop()
        if self._game_proc and game.is_running(self._game_proc):
            try:
                self._game_proc.terminate()
            except Exception:
                pass
        if hasattr(self, "_rep_dest"):
            game.remove_rep_from_game(self._rep_dest)
        if Path(self.output_path).exists():
            size_mb = Path(self.output_path).stat().st_size / 1024 / 1024
            self._log(f"保存完了: {Path(self.output_path).name}  ({size_mb:.1f} MB)")
            self.log_q.put(f"__RECORDED__:{self.output_path}")
        else:
            self._log("[録画失敗] 出力ファイルが作成されませんでした")


# ──────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        if _DND_AVAILABLE:
            TkinterDnD._require(self)

        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.geometry(f"{WIDTH}x{HEIGHT}")
        self.resizable(False, False)

        self._cfg = cfg_mod.load()
        self._session: RecordSession | None = None
        self._log_queue: queue.Queue = queue.Queue()
        self._last_output: str | None = None
        self._trim_win = None

        self._rep_files: list[str] = []
        self._batch_total = 0
        self._batch_index = 0
        self._batch_stopped = False

        self._closing = False

        self._build_ui()
        self._poll_log()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 構築 ──────────────────────────────
    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        ctk.CTkLabel(
            self, text=APP_TITLE, font=ctk.CTkFont(size=18, weight="bold")
        ).pack(padx=12, pady=(14, 2))

        # ── th123.exe パス ──
        frame_th = ctk.CTkFrame(self)
        frame_th.pack(fill="x", **pad)
        ctk.CTkLabel(frame_th, text="th123.EXE").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self._th123_var = ctk.StringVar(value=self._cfg.get("th123_path", ""))
        ctk.CTkEntry(frame_th, textvariable=self._th123_var, width=380).grid(row=0, column=1, padx=4)
        ctk.CTkButton(frame_th, text="参照", width=60, command=self._browse_th123).grid(row=0, column=2, padx=4)

        # ── .rep リスト ──
        frame_rep = ctk.CTkFrame(self)
        frame_rep.pack(fill="x", **pad)
        hint = "  (ドラッグ&ドロップ可)" if _DND_AVAILABLE else ""
        ctk.CTkLabel(frame_rep, text=f".rep ファイル{hint}").grid(
            row=0, column=0, sticky="nw", padx=8, pady=6
        )

        list_wrap = tk.Frame(frame_rep, bg="#3b3b3b", bd=1, relief="flat")
        list_wrap.grid(row=0, column=1, padx=4, pady=4, sticky="nsew")
        self._rep_listbox = tk.Listbox(
            list_wrap, height=4, bg="#2b2b2b", fg="white",
            selectbackground="#1f6aa5", selectforeground="white",
            activestyle="none", bd=0, highlightthickness=0,
            font=("Yu Gothic UI", 10),
        )
        sb = tk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=self._rep_listbox.yview)
        self._rep_listbox.configure(yscrollcommand=sb.set)
        self._rep_listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        if _DND_AVAILABLE:
            self._rep_listbox.drop_target_register(DND_FILES)
            self._rep_listbox.dnd_bind("<<Drop>>", self._on_dnd_drop)

        btn_col = ctk.CTkFrame(frame_rep, fg_color="transparent")
        btn_col.grid(row=0, column=2, padx=6, pady=4, sticky="n")
        ctk.CTkButton(btn_col, text="追加", width=60, command=self._add_rep).pack(pady=2)
        ctk.CTkButton(btn_col, text="削除", width=60, command=self._remove_rep).pack(pady=2)
        ctk.CTkButton(btn_col, text="全削除", width=60, command=self._clear_rep).pack(pady=2)
        ctk.CTkButton(btn_col, text="↑", width=60, command=self._move_rep_up).pack(pady=2)
        ctk.CTkButton(btn_col, text="↓", width=60, command=self._move_rep_down).pack(pady=2)
        ctk.CTkButton(btn_col, text="スキャン", width=60, command=self._scan_replay_folder).pack(pady=(6, 2))

        # ── 出力フォルダ ──
        frame_out = ctk.CTkFrame(self)
        frame_out.pack(fill="x", **pad)
        ctk.CTkLabel(frame_out, text="出力フォルダ").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self._out_var = ctk.StringVar(value=self._cfg.get("output_dir", ""))
        ctk.CTkEntry(frame_out, textvariable=self._out_var, width=380).grid(row=0, column=1, padx=4)
        ctk.CTkButton(frame_out, text="参照", width=60, command=self._browse_out).grid(row=0, column=2, padx=4)

        # ── 詳細設定 ──
        detail = ctk.CTkFrame(self)
        detail.pack(fill="x", **pad)
        ctk.CTkLabel(detail, text="詳細設定", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(6, 2)
        )
        self._detail_vars: dict[str, ctk.StringVar] = {}
        for i, (lbl, key) in enumerate([
            ("起動待ち(秒)", "wait_after_launch"), ("キー間隔(秒)", "key_delay"),
            ("メニュー↓回数", "down_count"),       ("選択後Z回数", "z_after_select"),
            ("録画品質(CRF)", "crf"),              ("フレームレート", "framerate"),
        ]):
            col = (i % 2) * 3
            row = 1 + i // 2
            ctk.CTkLabel(detail, text=lbl).grid(row=row, column=col, sticky="e", padx=(8, 2), pady=3)
            var = ctk.StringVar(value=str(self._cfg.get(key)))
            self._detail_vars[key] = var
            ctk.CTkEntry(detail, textvariable=var, width=80).grid(row=row, column=col + 1, padx=4, pady=3)

        # プリセット (フレームレートの下の行)
        ctk.CTkLabel(detail, text="プリセット").grid(row=4, column=0, sticky="e", padx=(8, 2), pady=3)
        self._preset_var = ctk.StringVar(value=self._cfg.get("preset", "veryfast"))
        ctk.CTkOptionMenu(detail, values=_PRESETS, variable=self._preset_var, width=120).grid(
            row=4, column=1, padx=4, pady=3
        )

        # ── 音声録音 ──
        ctk.CTkLabel(detail, text="音声録音").grid(row=5, column=0, sticky="e", padx=(8, 2), pady=3)
        devices = ["なし(無音)"]
        import recorder as rec_mod
        devices.extend(rec_mod.get_audio_devices())

        cfg_audio = self._cfg.get("audio_device", "なし(無音)")
        if cfg_audio not in devices:
            cfg_audio = "なし(無音)"
        # 初回起動時(audio_device未保存)は既定の再生デバイスのループバックをデフォルトにする
        # (ゲーム音は既定デバイスから出るので、他のデバイスを選ぶと無音になる)
        if "audio_device" not in self._cfg:
            loopback = rec_mod.get_default_loopback_device()
            if loopback not in devices:
                loopback = next((d for d in devices if d.startswith(rec_mod.WASAPI_PREFIX)), None)
            if loopback:
                cfg_audio = loopback
        self._audio_var = ctk.StringVar(value=cfg_audio)

        ctk.CTkOptionMenu(detail, values=devices, variable=self._audio_var, width=200).grid(
            row=5, column=1, columnspan=4, sticky="w", padx=4, pady=3
        )
        _audio_hint_row = ctk.CTkFrame(detail, fg_color="transparent")
        _audio_hint_row.grid(row=6, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 4))
        ctk.CTkLabel(
            _audio_hint_row,
            text="※ ゲーム音は既定の再生デバイスから出ます。そのデバイスの「[ループバック]」を選んでください",
            font=ctk.CTkFont(size=10),
            text_color="#e0a020",
        ).pack(side="left")
        ctk.CTkButton(
            _audio_hint_row,
            text="サウンド設定を開く",
            width=130, height=22,
            font=ctk.CTkFont(size=10),
            command=self._open_sound_settings,
        ).pack(side="left", padx=(10, 0))

        # ── 自動停止 ──
        self._auto_stop_var = tk.BooleanVar(value=self._cfg.get("auto_stop", True))
        ctk.CTkCheckBox(
            detail,
            text="リプレイ終了を自動検出して停止（OFF にすると手動停止のみ）",
            variable=self._auto_stop_var,
        ).grid(row=7, column=0, columnspan=6, sticky="w", padx=8, pady=(2, 2))

        # ── ゲーム音のみ ──
        self._game_audio_only_var = tk.BooleanVar(value=self._cfg.get("game_audio_only", False))
        ctk.CTkCheckBox(
            detail,
            text="ゲーム音のみ録音（ゲーム起動時に既定デバイスを一時変更）",
            variable=self._game_audio_only_var,
        ).grid(row=8, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 0))

        _vb_hint_row = ctk.CTkFrame(detail, fg_color="transparent")
        _vb_hint_row.grid(row=9, column=0, columnspan=6, sticky="w", padx=(28, 8), pady=(0, 4))
        ctk.CTkLabel(
            _vb_hint_row,
            text="※ この機能を使うには VB-Cable のインストールが必要です",
            font=ctk.CTkFont(size=10),
            text_color="#e0a020",
        ).pack(side="left")
        ctk.CTkButton(
            _vb_hint_row,
            text="VB-Cable をダウンロード",
            width=160, height=22,
            font=ctk.CTkFont(size=10),
            command=lambda: __import__("webbrowser").open("https://vb-audio.com/Cable/"),
        ).pack(side="left", padx=(10, 0))

        # ── ファイル名テンプレート ──
        ctk.CTkLabel(detail, text="ファイル名").grid(row=10, column=0, sticky="e", padx=(8, 2), pady=(2, 6))
        self._template_var = ctk.StringVar(value=self._cfg.get("filename_template", "{stem}"))
        ctk.CTkEntry(detail, textvariable=self._template_var, width=200).grid(
            row=10, column=1, columnspan=2, sticky="w", padx=4, pady=(2, 6)
        )
        ctk.CTkLabel(detail, text="{stem}=rep名  {date}=日付(YYYYMMDD)",
                     font=ctk.CTkFont(size=10), text_color="#888888").grid(
            row=10, column=3, columnspan=3, sticky="w", padx=4, pady=(2, 6)
        )

        # ── 録画ボタン ──
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=8)
        self._start_btn = ctk.CTkButton(
            btn_frame, text="録画開始", width=150, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_batch,
        )
        self._start_btn.pack(side="left", padx=10)
        self._stop_btn = ctk.CTkButton(
            btn_frame, text="録画停止", width=150, height=40,
            font=ctk.CTkFont(size=14),
            fg_color="#c0392b", hover_color="#96281b",
            command=self._stop_recording,
            state="disabled",
        )
        self._stop_btn.pack(side="left", padx=10)

        # ── 録画後自動切り抜き ──
        self._auto_trim_var = tk.BooleanVar(value=self._cfg.get("auto_open_trim", False))
        auto_trim_row = ctk.CTkFrame(self, fg_color="transparent")
        auto_trim_row.pack(pady=(0, 4))
        ctk.CTkCheckBox(
            auto_trim_row, text="録画完了後に自動で切り抜きウィンドウを開く",
            variable=self._auto_trim_var,
        ).pack()

        # ── ログ ──
        ctk.CTkLabel(self, text="ログ").pack(anchor="w", padx=14)
        self._log_box = ctk.CTkTextbox(self, height=160, state="disabled")
        self._log_box.pack(fill="x", padx=12, pady=(2, 8))

        # ── 切り抜き / 結合 / プレビュー ──
        trim_row = ctk.CTkFrame(self, fg_color="transparent")
        trim_row.pack(pady=(0, 4))
        ctk.CTkButton(
            trim_row, text="録画ファイルを切り抜く", width=180, height=36,
            command=self._open_trim_last,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            trim_row, text="ファイルを指定して切り抜く", width=200, height=36,
            command=self._open_trim_browse,
        ).pack(side="left", padx=6)

        tool_row = ctk.CTkFrame(self, fg_color="transparent")
        tool_row.pack(pady=(0, 12))
        ctk.CTkButton(
            tool_row, text="動画を結合", width=180, height=36,
            command=self._open_concat_window,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            tool_row, text="キャプチャ確認", width=160, height=36,
            command=self._show_capture_preview,
        ).pack(side="left", padx=6)

    # ── ドラッグ&ドロップ ────────────────────
    def _on_dnd_drop(self, event) -> None:
        paths = self.tk.splitlist(event.data)
        for p in paths:
            p = p.strip("{}")
            if p.lower().endswith(".rep") and p not in self._rep_files:
                self._rep_files.append(p)
                self._rep_listbox.insert(tk.END, Path(p).name)

    # ── .rep リスト操作 ──────────────────────
    def _add_rep(self) -> None:
        paths = filedialog.askopenfilenames(
            title=".rep ファイルを選択 (複数可)",
            filetypes=[("リプレイファイル", "*.rep"), ("すべて", "*.*")],
        )
        for p in paths:
            if p not in self._rep_files:
                self._rep_files.append(p)
                self._rep_listbox.insert(tk.END, Path(p).name)

    def _remove_rep(self) -> None:
        for i in reversed(self._rep_listbox.curselection()):
            self._rep_listbox.delete(i)
            self._rep_files.pop(i)

    def _clear_rep(self) -> None:
        self._rep_listbox.delete(0, tk.END)
        self._rep_files.clear()

    def _move_rep_up(self) -> None:
        sel = self._rep_listbox.curselection()
        if not sel or sel[0] == 0:
            return
        i = sel[0]
        self._rep_files[i - 1], self._rep_files[i] = self._rep_files[i], self._rep_files[i - 1]
        prev, cur = self._rep_listbox.get(i - 1), self._rep_listbox.get(i)
        self._rep_listbox.delete(i - 1, i)
        self._rep_listbox.insert(i - 1, cur)
        self._rep_listbox.insert(i, prev)
        self._rep_listbox.selection_set(i - 1)

    def _move_rep_down(self) -> None:
        sel = self._rep_listbox.curselection()
        if not sel or sel[0] >= len(self._rep_files) - 1:
            return
        i = sel[0]
        self._rep_files[i], self._rep_files[i + 1] = self._rep_files[i + 1], self._rep_files[i]
        cur, nxt = self._rep_listbox.get(i), self._rep_listbox.get(i + 1)
        self._rep_listbox.delete(i, i + 1)
        self._rep_listbox.insert(i, nxt)
        self._rep_listbox.insert(i + 1, cur)
        self._rep_listbox.selection_set(i + 1)

    def _scan_replay_folder(self) -> None:
        th123_path = self._th123_var.get().strip()
        if not th123_path or not Path(th123_path).exists():
            messagebox.showinfo("スキャン", "th123.EXE のパスを先に設定してください。", parent=self)
            return
        replay_dir = Path(th123_path).parent / "replay"
        if not replay_dir.exists():
            messagebox.showinfo("スキャン", f"リプレイフォルダが見つかりません:\n{replay_dir}", parent=self)
            return
        rep_files = sorted(p for p in replay_dir.rglob("*.rep")
                           if not any(part.startswith("!") for part in p.parts))
        if not rep_files:
            messagebox.showinfo("スキャン", "リプレイファイルが見つかりませんでした。", parent=self)
            return
        self._show_scan_dialog(replay_dir, rep_files)

    def _show_scan_dialog(self, replay_dir: Path, rep_files: list) -> None:
        win = ctk.CTkToplevel(self)
        win.title("リプレイファイルを選択")
        win.geometry("500x500")
        win.resizable(False, False)
        win.lift()
        win.focus_force()

        ctk.CTkLabel(win, text=f"{len(rep_files)} 件見つかりました",
                     font=ctk.CTkFont(weight="bold")).pack(pady=(12, 4))

        frame = ctk.CTkScrollableFrame(win, height=330)
        frame.pack(fill="x", padx=12)

        vars_: list[tk.BooleanVar] = []
        for rep in rep_files:
            var = tk.BooleanVar(value=True)
            vars_.append(var)
            try:
                label = str(rep.relative_to(replay_dir))
            except ValueError:
                label = rep.name
            ctk.CTkCheckBox(frame, text=label, variable=var).pack(anchor="w", pady=1)

        sel_row = ctk.CTkFrame(win, fg_color="transparent")
        sel_row.pack(pady=(6, 0))
        ctk.CTkButton(sel_row, text="全選択", width=80,
                      command=lambda: [v.set(True) for v in vars_]).pack(side="left", padx=4)
        ctk.CTkButton(sel_row, text="全解除", width=80,
                      command=lambda: [v.set(False) for v in vars_]).pack(side="left", padx=4)

        def _add():
            added = 0
            for rep, var in zip(rep_files, vars_):
                p = str(rep)
                if var.get() and p not in self._rep_files:
                    self._rep_files.append(p)
                    self._rep_listbox.insert(tk.END, rep.name)
                    added += 1
            win.destroy()
            if added:
                self._log(f"{added} 件追加しました")

        ctk.CTkButton(win, text="追加", width=120, height=36,
                      font=ctk.CTkFont(weight="bold"), command=_add).pack(pady=10)

    # ── 参照ダイアログ ──────────────────────
    def _browse_th123(self) -> None:
        p = filedialog.askopenfilename(
            title="th123.EXE を選択",
            filetypes=[("実行ファイル", "*.EXE *.exe"), ("すべて", "*.*")],
        )
        if p:
            self._th123_var.set(p)

    def _browse_out(self) -> None:
        p = filedialog.askdirectory(title="出力フォルダを選択")
        if p:
            self._out_var.set(p)

    def _open_sound_settings(self) -> None:
        import subprocess
        # 再生・録音両方のタブが見えるサウンドコントロールパネルを開く
        subprocess.Popen(["control", "mmsys.cpl"])

    # ── 設定収集 ─────────────────────────────
    def _collect_cfg(self) -> dict:
        cfg = self._cfg.copy()
        cfg["th123_path"] = self._th123_var.get().strip()
        cfg["output_dir"] = self._out_var.get().strip()
        cfg["preset"] = self._preset_var.get()
        cfg["audio_device"] = self._audio_var.get()
        cfg["auto_open_trim"] = self._auto_trim_var.get()
        cfg["auto_stop"] = self._auto_stop_var.get()
        cfg["game_audio_only"] = self._game_audio_only_var.get()
        cfg["filename_template"] = self._template_var.get().strip() or "{stem}"
        for key, var in self._detail_vars.items():
            try:
                val = var.get().strip()
                cfg[key] = float(val) if "." in val else int(val)
            except ValueError:
                pass
        return cfg

    @staticmethod
    def _resolve_output_path(cfg: dict, rep_path: str) -> str:
        from datetime import datetime
        template = cfg.get("filename_template", "{stem}")
        name = template.format(
            stem=Path(rep_path).stem,
            date=datetime.now().strftime("%Y%m%d"),
        )
        # ファイル名に使えない文字は _ に置き換える
        name = "".join("_" if c in _INVALID_NAME_CHARS else c for c in name).strip() or Path(rep_path).stem
        return str(Path(cfg["output_dir"]) / (name + ".mp4"))

    # ── 上書き確認 ───────────────────────────
    def _check_overwrites(self, cfg: dict) -> bool:
        conflicts = [
            Path(self._resolve_output_path(cfg, p)).name
            for p in self._rep_files
            if Path(self._resolve_output_path(cfg, p)).exists()
        ]
        if not conflicts:
            return True
        msg = "以下のファイルはすでに存在します。上書きしますか？\n\n" + "\n".join(conflicts)
        return messagebox.askyesno("上書き確認", msg, parent=self)

    # ── バッチ録画 ───────────────────────────
    def _start_batch(self) -> None:
        if not self._rep_files:
            self._log(".rep ファイルが選択されていません")
            return

        cfg = self._collect_cfg()
        if not cfg["th123_path"] or not Path(cfg["th123_path"]).exists():
            self._log("th123.EXE のパスが正しくありません")
            return
        if not Path(cfg["output_dir"]).exists():
            self._log("出力フォルダが存在しません")
            return
        try:
            outputs = [self._resolve_output_path(cfg, p) for p in self._rep_files]
        except (KeyError, IndexError, ValueError):
            self._log("ファイル名の書式が正しくありません。使えるのは {stem} と {date} だけです")
            return
        if len(set(o.lower() for o in outputs)) < len(outputs):
            self._log("出力ファイル名が重複します。ファイル名に {stem} を入れるか、同名の .rep を外してください")
            return
        if not self._check_overwrites(cfg):
            return

        cfg_mod.save(cfg)
        self._cfg = cfg

        self._batch_total = len(self._rep_files)
        self._batch_index = 0
        self._batch_stopped = False

        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._clear_log()
        self._log(f"バッチ録画開始: {self._batch_total} 件")
        self._record_next()

    def _record_next(self) -> None:
        if self._batch_stopped or self._batch_index >= self._batch_total:
            if not self._batch_stopped:
                self._log(f"\n全 {self._batch_total} 件の録画が完了しました")
            self._start_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")
            self._session = None
            return

        rep_path = self._rep_files[self._batch_index]
        self._rep_listbox.selection_clear(0, tk.END)
        self._rep_listbox.selection_set(self._batch_index)
        self._rep_listbox.see(self._batch_index)

        cfg = self._collect_cfg()
        output_path = self._resolve_output_path(cfg, rep_path)

        self._log(f"\n[{self._batch_index + 1}/{self._batch_total}] {Path(rep_path).name}")
        self._session = RecordSession(cfg, rep_path, output_path, self._log_queue)
        threading.Thread(target=self._session.run, daemon=True).start()

    def _stop_recording(self) -> None:
        self._batch_stopped = True
        if self._session:
            self._session.request_stop()

    def _on_close(self) -> None:
        """
        録画中に閉じられたら、録画を止めて後始末 (FFmpeg 停止・ゲーム終了・一時ファイル削除)
        が終わってからウィンドウを閉じる。そのまま閉じると FFmpeg が録画し続けたまま残る。
        """
        if self._session is None:
            self.destroy()
            return
        if self._closing:
            return
        if not messagebox.askyesno("終了確認", "録画中です。録画を停止して終了しますか？", parent=self):
            return
        self._closing = True
        self._log("録画を停止して終了します...")
        self._stop_recording()

    # ── 切り抜きウィンドウ ────────────────────
    def _open_trim_window(self, path: str) -> None:
        from trim_window import TrimWindow
        if self._trim_win and self._trim_win.winfo_exists():
            self._trim_win.lift()
            return
        self._trim_win = TrimWindow(self, path)
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        mx, my = self.winfo_x(), self.winfo_y()
        mw = self.winfo_width()
        tw, th = 560, 720

        x = mx + mw + 8            # ツールの右
        if x + tw > sw:
            x = mx - tw - 8        # ツールの左
        if x < 0:
            x = max(0, sw - tw)    # 画面右端にフォールバック
        y = max(0, min(my, sh - th))
        self._trim_win.geometry(f"{tw}x{th}+{x}+{y}")

    def _open_trim_last(self) -> None:
        if self._last_output and Path(self._last_output).exists():
            self._open_trim_window(self._last_output)
        else:
            self._open_trim_browse()

    def _open_trim_browse(self) -> None:
        p = filedialog.askopenfilename(
            title="切り抜き元ファイルを選択",
            filetypes=[("動画ファイル", "*.mp4 *.mkv *.avi"), ("すべて", "*.*")],
        )
        if p:
            if self._trim_win and self._trim_win.winfo_exists():
                self._trim_win.destroy()
            self._open_trim_window(p)

    def _open_concat_window(self) -> None:
        from concat_window import ConcatWindow
        ConcatWindow(self)

    def _show_capture_preview(self) -> None:
        th123_path = self._th123_var.get().strip()
        if not th123_path or not Path(th123_path).exists():
            messagebox.showinfo("キャプチャ確認", "th123.EXE のパスを先に設定してください。", parent=self)
            return
        hwnd = game.find_game_hwnd(th123_path)
        if not hwnd:
            messagebox.showinfo("キャプチャ確認",
                                "th123.EXE が起動していません。\nゲームを起動してからもう一度お試しください。",
                                parent=self)
            return
        try:
            import io
            from PIL import Image, ImageTk
            ffmpeg = rec_mod.find_ffmpeg()
            png = game.grab_window_png(hwnd, ffmpeg) if ffmpeg else None
            if not png:
                messagebox.showinfo(
                    "キャプチャ確認",
                    "ゲーム画面を取得できませんでした。\nウィンドウが最小化されていないか確認してください。",
                    parent=self,
                )
                return
            img = Image.open(io.BytesIO(png))
            width, height = img.size
        except Exception as e:
            messagebox.showerror("キャプチャ確認", f"スクリーンショット取得に失敗しました:\n{e}", parent=self)
            return

        win = ctk.CTkToplevel(self)
        win.title(f"キャプチャ確認  {width}×{height}")
        win.resizable(False, False)
        win.lift()
        win.focus_force()

        max_w, max_h = 640, 480
        scale = min(max_w / width, max_h / height, 1.0)
        pw, ph = int(width * scale), int(height * scale)

        canvas = tk.Canvas(win, width=pw, height=ph, bg="black", highlightthickness=0)
        canvas.pack(padx=10, pady=10)
        photo = ImageTk.PhotoImage(img.resize((pw, ph)))
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas._photo = photo  # GC 防止

        ctk.CTkLabel(
            win,
            text=f"録画範囲: {width}×{height}（表示: {pw}×{ph}）",
            font=ctk.CTkFont(size=11),
        ).pack(pady=(0, 10))

    # ── ログ UI ─────────────────────────────
    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _clear_log(self) -> None:
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    def _poll_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                if msg == "__DONE__":
                    self._batch_index += 1
                    self._record_next()
                elif msg.startswith("__RECORDED__:"):
                    self._last_output = msg[len("__RECORDED__:"):]
                    is_last = self._batch_total <= 1 or self._batch_index >= self._batch_total - 1
                    if self._auto_trim_var.get() and is_last:
                        self._open_trim_window(self._last_output)
                elif msg.startswith("__GAME_RECT__:"):
                    _, gl, gt, gr, gb = msg.split(":")
                    self._move_beside_game(int(gl), int(gt), int(gr), int(gb))
                else:
                    self._log_box.configure(state="normal")
                    self._log_box.insert("end", msg + "\n")
                    self._log_box.see("end")
                    self._log_box.configure(state="disabled")
        except queue.Empty:
            pass
        if self._closing and self._session is None:
            self.destroy()   # 録画の後始末が終わったので閉じる
            return
        self.after(200, self._poll_log)

    def _move_beside_game(self, gl: int, gt: int, gr: int, gb: int) -> None:
        """ゲームウィンドウと被らない位置にメインウィンドウを移動する。"""
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        mw = self.winfo_width()
        mh = self.winfo_height()
        y = max(0, min(gt, sh - mh))

        if gr + 8 + mw <= sw:          # ゲームの右
            self.geometry(f"+{gr + 8}+{y}")
        elif gl - 8 - mw >= 0:         # ゲームの左
            self.geometry(f"+{gl - 8 - mw}+{y}")
        elif gb + 8 + mh <= sh:        # ゲームの下
            x = max(0, min(gl, sw - mw))
            self.geometry(f"+{x}+{gb + 8}")
        else:                          # 収まらない場合は右端
            self.geometry(f"+{sw - mw}+0")


def _set_dpi_aware() -> None:
    """
    高DPIディスプレイで文字がぼやけないようにする。
    Per-Monitor V2 → V1 → System DPI の順でフォールバック。
    早い段階で呼ばないと Windows がアプリをビットマップ拡大してしまう。
    """
    import ctypes
    # Per-Monitor DPI Aware V2 (Windows 10 1703+) — 一番クッキリ
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    # Per-Monitor DPI Aware V1 (Windows 8.1+)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    # System DPI Aware フォールバック
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if __name__ == "__main__":
    _set_dpi_aware()
    app = App()
    app.mainloop()
