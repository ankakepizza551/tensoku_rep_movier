"""FFmpeg を使ったウィンドウキャプチャ録画モジュール。"""

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable


# WASAPIループバックデバイスの識別プレフィックス
WASAPI_PREFIX = "[ループバック] "
_WASAPI_DEVICE_LABEL = f"{WASAPI_PREFIX}システム音声 (既定の再生デバイス)"


def get_audio_devices() -> list[str]:
    """
    録音に使える音声デバイスを列挙する。
    - WASAPIループバック: ステレオミキサー不要でゲーム音を録音できる
    - DirectShow: ステレオミキサー等の従来録音デバイス
    """
    devices: list[str] = []
    devices.extend(_get_wasapi_loopback_devices())
    devices.extend(_get_dshow_audio_devices())
    return devices


def _get_wasapi_loopback_devices() -> list[str]:
    """pyaudiowpatch でループバックデバイスを実名で全列挙して返す。"""
    try:
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        devices: list[str] = []
        try:
            # get_loopback_device_info_generator() が pyaudiowpatch 専用の正規 API
            for info in pa.get_loopback_device_info_generator():
                name = info.get("name", "")
                if name:
                    devices.append(f"{WASAPI_PREFIX}{name}")
        finally:
            pa.terminate()
        return devices
    except Exception:
        pass
    return []


def get_default_loopback_device() -> str | None:
    """既定の再生デバイスに対応するループバックデバイス名 (WASAPI_PREFIX 付き) を返す。"""
    try:
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        try:
            name = pa.get_default_wasapi_loopback().get("name", "")
            return f"{WASAPI_PREFIX}{name}" if name else None
        finally:
            pa.terminate()
    except Exception:
        return None


def _get_dshow_audio_devices() -> list[str]:
    """DirectShow の音声入力デバイスを列挙する（ステレオミキサー等）。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return []

    CREATE_NO_WINDOW = 0x08000000
    devices: list[str] = []
    import re

    try:
        proc = subprocess.run(
            [ffmpeg, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        lines = proc.stderr.splitlines()
        in_audio_section = False
        for line in lines:
            if "DirectShow audio devices" in line:
                in_audio_section = True
                continue
            if "DirectShow video devices" in line:
                in_audio_section = False
                continue

            m_new = re.search(r'"([^"]+)"\s*\(audio\)', line)
            if m_new:
                name = m_new.group(1)
                if name not in devices:
                    devices.append(name)
                continue

            if in_audio_section and "Alternative name" not in line:
                m_old = re.search(r'"([^"]+)"', line)
                if m_old:
                    name = m_old.group(1)
                    if name not in devices:
                        devices.append(name)
    except Exception:
        pass

    return devices


def find_ffmpeg() -> str | None:
    import sys
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    bundled = base / "ffmpeg" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("ffmpeg")


def encoder_args(encoder: str, crf: int, preset: str) -> list[str]:
    """エンコーダーごとの品質・速度オプション。crf は libx264 の CRF 相当の値。"""
    if encoder == "h264_nvenc":   # NVIDIA
        return ["-c:v", encoder, "-preset", "p1", "-rc", "vbr", "-cq", str(crf), "-gpu", "any"]
    if encoder == "h264_amf":     # AMD (preset は balanced / speed / quality のみ)
        return ["-c:v", encoder, "-preset", "speed", "-rc", "cqp",
                "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf)]
    if encoder == "h264_qsv":     # Intel
        return ["-c:v", encoder, "-preset", "veryfast", "-global_quality", str(crf)]
    return ["-c:v", "libx264", "-preset", preset, "-tune", "zerolatency", "-crf", str(crf)]


_best_encoder_cache: dict[str, str] = {}


def get_best_encoder(ffmpeg: str) -> str:
    """
    この PC で実際に使える最速のエンコーダーを返す。
    ffmpeg は NVIDIA / AMD / Intel 用のエンコーダーをすべて内蔵しているので、
    一覧に載っているかではなく、数フレーム試しにエンコードして成功したものを選ぶ
    (対応する GPU が無いエンコーダーは起動時に失敗する)。
    """
    if ffmpeg in _best_encoder_cache:
        return _best_encoder_cache[ffmpeg]
    CREATE_NO_WINDOW = 0x08000000
    best = "libx264"
    for encoder in ("h264_nvenc", "h264_amf", "h264_qsv"):
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=size=640x480:rate=60",
                 "-frames:v", "5", *encoder_args(encoder, 20, "veryfast"),
                 "-pix_fmt", "yuv420p", "-f", "null", "-"],
                capture_output=True, timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            continue
        if proc.returncode == 0:
            best = encoder
            break
    _best_encoder_cache[ffmpeg] = best
    return best


def has_gfxcapture(ffmpeg: str) -> bool:
    """ffmpeg が gfxcapture (Windows.Graphics.Capture) に対応しているか。"""
    CREATE_NO_WINDOW = 0x08000000
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW
        )
        return " gfxcapture " in proc.stdout
    except Exception:
        return False


_job_handle = None


def _kill_with_parent(proc: subprocess.Popen) -> None:
    """
    このツールが (異常終了も含めて) 終了したら、proc も一緒に終了させる。
    これをしないと、ツールが落ちたときに ffmpeg だけが残って録画し続ける。
    Windows のジョブオブジェクトに入れておくと、ツール側のハンドルが閉じた時点で
    OS がジョブ内のプロセスを終了させる。
    """
    global _job_handle
    import ctypes
    from ctypes import wintypes

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC),
            ("IoInfo", _IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if _job_handle is None:
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = _EXTENDED()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                               ctypes.byref(info), ctypes.sizeof(info)):
                return
            _job_handle = job   # プロセス終了まで開いたままにする
        k32.AssignProcessToJobObject(_job_handle, int(proc._handle))
    except Exception:
        pass   # 失敗しても録画自体はできる


class Recorder:
    def __init__(self, log: Callable = print):
        self._proc: subprocess.Popen | None = None
        self._stderr_file = None
        self._stderr_path: Path | None = None
        self._tmp_path: Path | None = None
        self._final_path: Path | None = None
        self.log = log
        # WASAPIループバック用
        self._wasapi_stop: threading.Event | None = None
        self._wasapi_thread: threading.Thread | None = None
        self._wasapi_wav_path: Path | None = None
        # DirectShow 別録り用
        self._dshow_proc: subprocess.Popen | None = None
        self._dshow_wav_path: Path | None = None
        self._dshow_log_path: Path | None = None
        self._dshow_log_file = None
        # 映像/音声の開始タイムスタンプ（マージ時の同期補正に使用）
        self._video_start_time: float = 0.0
        self._audio_start_time: float = 0.0
        self._audio_channels: int = 2
        self._audio_device: str | None = None
        # プロセス単位の録音 (ゲーム音のみ)
        self._proc_capture = None

    @property
    def video_start_time(self) -> float:
        """映像の録画を開始した時刻 (time.time())。"""
        return self._video_start_time

    def start(
        self,
        hwnd: int,
        output_path: str,
        framerate: int,
        crf: int,
        preset: str,
        audio_device: str | None = None,
        audio_pid: int | None = None,
    ) -> None:
        """
        hwnd のウィンドウの録画を始める。
        audio_pid を渡すと、そのプロセスの音だけを録音する (できなければ audio_device を使う)。
        """
        import win32gui

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise FileNotFoundError("ffmpeg.exe が見つかりません。")

        encoder = get_best_encoder(ffmpeg)
        self.log(f"[診断] 使用エンコーダー: {encoder}")

        # クライアント領域のサイズを取得
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        width, height = cr - cl, cb - ct
        width  = width  if width  % 2 == 0 else width  - 1
        height = height if height % 2 == 0 else height - 1

        # 録画中は MKV に書き、終了時に MP4 へ仕上げる。
        # MP4 は最後まで書き終えないと再生できないが、MKV は途中で切れても
        # そこまでの映像が再生できるので、ツールや PC が落ちても録画が残る。
        self._final_path = Path(output_path)
        self._tmp_path = self._final_path.with_name(self._final_path.stem + ".recording.mkv")

        # WASAPI・DirectShow ともに別録り → FFmpeg 映像のみ
        use_wasapi = audio_device and audio_device.startswith(WASAPI_PREFIX)
        use_dshow  = bool(audio_device) and not use_wasapi

        # 映像入力 — 音声は含めない
        # どちらの方式もウィンドウ自体をハンドル指定でキャプチャするので、他のウィンドウに
        # 重なられても正しく録画できる (最小化さえされなければOK)。
        #
        # gfxcapture (Windows.Graphics.Capture): 60fps で取りこぼしなく録れる。優先して使う。
        #   フレームは画面更新時にしか来ないので、cfr で一定フレームレートに揃える。
        # gdigrab: 古い ffmpeg / Windows 用のフォールバック。実測 50fps 前後までしか出ない。
        #   クライアント領域が原点なので offset は不要 (枠の分をずらすと
        #   "Capture area ... extends outside window area" で起動に失敗する)。
        #   title= だと同名ウィンドウを誤って掴むことがあるため hwnd= を使う。
        inputs = {
            "gfxcapture": (
                [
                    "-f", "lavfi",
                    # 後半の枝は、最初の 1 コマが届いた瞬間にログへ 1 行出すためだけのもの
                    # (その時刻を映像の開始時刻として、音声とのズレ補正に使う)
                    "-i", f"gfxcapture=hwnd={hwnd}:capture_cursor=0:max_framerate={framerate}"
                          ":width=-2:height=-2,split[out0][probe];"
                          "[probe]select=eq(n\\,0),showinfo=checksum=0,nullsink",
                ],
                ["-vf", "hwdownload,format=bgra", "-fps_mode", "cfr", "-r", str(framerate)],
            ),
            "gdigrab": (
                [
                    "-thread_queue_size", "2048",
                    "-f", "gdigrab",
                    "-draw_mouse", "0",
                    "-framerate", str(framerate),
                    "-video_size", f"{width}x{height}",
                    "-i", f"hwnd={hwnd}",
                ],
                [],
            ),
        }
        methods = ["gfxcapture", "gdigrab"] if has_gfxcapture(ffmpeg) else ["gdigrab"]

        # 映像エンコード
        if encoder == "libx264" and crf <= 17 and preset not in ("ultrafast", "superfast"):
            self.log(
                f"[注意] CRF {crf} はlibx264の {preset} では追いつかないため "
                f"preset を ultrafast に自動上書きしました (画質は同等、ファイル増)"
            )
            preset = "ultrafast"
        enc_args = encoder_args(encoder, crf, preset)
        # キーフレームを 1 秒ごとに入れる。仕上げで冒頭を無変換のままカットできるようにするため
        enc_args.extend(["-g", str(framerate), "-pix_fmt", "yuv420p"])

        self._stderr_path = Path(tempfile.mktemp(suffix="_ffmpeg.log"))

        CREATE_NO_WINDOW = 0x08000000
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        for i, method in enumerate(methods):
            in_args, out_args = inputs[method]
            # -flush_packets 1: 1 コマごとにディスクへ書き出す。まとめて書く既定の動作だと、
            # 強制終了されたときに最後の数秒 (短い録画なら全部) が失われる。
            cmd = [ffmpeg, "-y", *in_args, *out_args, *enc_args, "-flush_packets", "1", str(self._tmp_path)]
            self._stderr_file = open(self._stderr_path, "w", encoding="utf-8", errors="replace")
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_file,
                creationflags=CREATE_NO_WINDOW | ABOVE_NORMAL_PRIORITY_CLASS,
            )
            _kill_with_parent(self._proc)

            # 映像の開始時刻 = 最初のコマが届いた時刻。ffmpeg を起動してから最初のコマが
            # 来るまでには遅れ (gfxcapture で 0.2 秒ほど) があり、起動時刻を開始とみなすと
            # その分だけ音が遅れて聞こえる動画になる。
            launched = time.time()
            self._video_start_time = launched
            first_frame_seen = False
            while time.time() - launched < 0.5 or (
                    method == "gfxcapture" and not first_frame_seen and time.time() - launched < 3.0):
                if self._proc.poll() is not None:
                    break
                if method == "gfxcapture" and not first_frame_seen:
                    try:
                        if "pts_time:" in self._stderr_path.read_text(encoding="utf-8", errors="replace"):
                            first_frame_seen = True
                            self._video_start_time = time.time()
                    except OSError:
                        pass
                time.sleep(0.005)
            rc = self._proc.poll()
            if rc is None:
                self.log(f"[診断] キャプチャ方式: {method}")
                break

            self._stderr_file.close()
            err_text = ""
            try:
                err_text = self._stderr_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
            if i < len(methods) - 1:
                last = next((l.strip() for l in reversed(err_text.splitlines()) if l.strip()), "")
                self.log(f"[注意] {method} で録画を開始できませんでした。{methods[i + 1]} に切り替えます")
                if last:
                    self.log(f"  {last}")
                continue

            self._stderr_file = None
            self.log(f"[エラー] FFmpeg が即座に終了しました (終了コード: {rc} / 0x{rc & 0xFFFFFFFF:08X})")
            if err_text.strip():
                for line in err_text.splitlines()[-15:]:
                    if line.strip():
                        self.log(f"  {line}")
            else:
                self.log("  (stderr 出力なし — DLL 不足の可能性があります)")
                self.log("  → ffmpeg/ffmpeg.exe が正しい静的ビルドか確認してください")
                self.log("  → BtbN の ffmpeg-master-latest-win64-gpl.zip 推奨")
            raise RuntimeError(f"FFmpeg の起動に失敗しました (コード: {rc})")

        # FFmpeg 起動確認後に音声キャプチャ開始 → 映像との同期ズレを最小化
        self._audio_device = audio_device
        if audio_pid and self._start_process_capture(audio_pid):
            pass
        elif use_wasapi:
            self._start_wasapi_capture(audio_device)
        elif use_dshow:
            self._start_dshow_capture(audio_device)

        self.log(f"FFmpeg 録画開始: {self._final_path.name}")

    def _start_process_capture(self, pid: int) -> bool:
        """pid のプロセスの音だけを録音する。開始できなければ False。"""
        try:
            import os
            import process_audio
            tmp_fd, tmp_str = tempfile.mkstemp(suffix="_proc.wav")
            os.close(tmp_fd)
            capture = process_audio.ProcessLoopbackCapture(pid, Path(tmp_str), log=self.log)
            if not capture.start():
                Path(tmp_str).unlink(missing_ok=True)
                return False
            self._proc_capture = capture
            self._wasapi_wav_path = Path(tmp_str)
            self._audio_channels = 2
            self._audio_start_time = time.time()
            self.log("ゲームの音だけを録音します")
            return True
        except Exception as e:
            self.log(f"[警告] プロセス単位の録音を開始できませんでした ({e})")
            return False

    def _start_dshow_capture(self, audio_device: str) -> None:
        """DirectShow デバイスを別 FFmpeg プロセスで録音する。"""
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.log("[警告] ffmpeg が見つかりません (音声なしで録画します)")
            return
        import os
        tmp_fd, tmp_str = tempfile.mkstemp(suffix="_dshow.wav")
        os.close(tmp_fd)
        self._dshow_wav_path = Path(tmp_str)

        # stderr をファイルに保存して失敗時のデバッグに使う
        dshow_log_path = Path(tempfile.mktemp(suffix="_dshow.log"))
        self._dshow_log_path = dshow_log_path

        CREATE_NO_WINDOW = 0x08000000
        cmd = [
            ffmpeg, "-y",
            "-f", "dshow",
            "-i", f"audio={audio_device}",
            "-c:a", "pcm_s16le",
            str(self._dshow_wav_path),
        ]
        try:
            self._audio_channels = 2
            self._audio_start_time = time.time()
            dshow_log_file = open(dshow_log_path, "w", encoding="utf-8", errors="replace")
            self._dshow_log_file = dshow_log_file
            self._dshow_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=dshow_log_file,
                creationflags=CREATE_NO_WINDOW,
            )
            _kill_with_parent(self._dshow_proc)
            time.sleep(0.5)
            rc = self._dshow_proc.poll()
            if rc is not None:
                dshow_log_file.flush()
                err_text = ""
                try:
                    err_text = dshow_log_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
                self.log(f"[警告] DirectShow プロセスが即終了 (コード: {rc})")
                for line in err_text.splitlines()[-10:]:
                    if line.strip():
                        self.log(f"  {line}")
                self._dshow_proc = None
                self._dshow_wav_path = None
            else:
                self.log(f"DirectShow 音声キャプチャ開始: {audio_device}")
        except Exception as e:
            self.log(f"[警告] DirectShow キャプチャ開始失敗: {e}")
            self._dshow_wav_path = None

    def _start_wasapi_capture(self, audio_device: str) -> None:
        """pyaudiowpatch で選択されたループバックデバイスをキャプチャする。"""
        try:
            import pyaudiowpatch as pyaudio
            import wave
        except ImportError:
            self.log("[警告] pyaudiowpatch が見つかりません。pip install pyaudiowpatch")
            return

        try:
            pa = pyaudio.PyAudio()
            # get_loopback_device_info_generator() が pyaudiowpatch 専用の正規 API
            # (通常の get_device_info_by_index で得た index は pa.open() に使えない)
            target_name = audio_device[len(WASAPI_PREFIX):]
            device_info = None
            try:
                for info in pa.get_loopback_device_info_generator():
                    if info.get("name", "") == target_name:
                        device_info = info
                        break
                if device_info is None:
                    self.log(f"[警告] 選択デバイス「{target_name}」が見つかりません。最初のループバックを使用します")
                    for info in pa.get_loopback_device_info_generator():
                        device_info = info
                        break
            except Exception as e:
                self.log(f"[警告] ループバック列挙エラー: {e}")

            # ストリームは PyAudio を作ったのと同じスレッドで開く必要がある
            # (別スレッドで開くと "Unanticipated host error" で失敗して無音になる)。
            # ここではデバイス情報だけ取り、キャプチャスレッド側で作り直す。
            pa.terminate()

            if device_info is None:
                self.log("[警告] WASAPIループバックデバイスが見つかりません (音声なしで録画します)")
                return

            self.log(f"[診断] ループバック対象: {device_info.get('name', '?')} (index={device_info.get('index')})")

            sample_rate = int(device_info["defaultSampleRate"])
            native_channels = int(device_info.get("maxInputChannels") or device_info.get("maxOutputChannels") or 2)
            device_index = int(device_info["index"])

            tmp_fd, tmp_str = tempfile.mkstemp(suffix="_audio.wav")
            import os; os.close(tmp_fd)
            self._wasapi_wav_path = Path(tmp_str)
            self._audio_channels = 2  # マージ時はステレオ前提

            self._wasapi_stop = threading.Event()
            stop_event = self._wasapi_stop
            wav_path = self._wasapi_wav_path
            log = self.log

            def _capture():
                wf = None
                stream = None
                pa = pyaudio.PyAudio()
                frames_written = [0]  # コールバックから参照するためリストで包む

                # サラウンドヘッドセット等の多chデバイスはステレオで開く
                # ゲーム音は通常ステレオ出力なので2chで全音声をキャプチャできる
                # ステレオが失敗した場合のみネイティブchにフォールバック
                ch = 2
                if native_channels > 2:
                    try:
                        probe = pa.open(format=pyaudio.paInt16, channels=2,
                                        rate=sample_rate, frames_per_buffer=512,
                                        input=True, input_device_index=device_index)
                        probe.close()
                    except Exception as e:
                        log(f"[診断] ステレオ不可 ({e})、{native_channels}chで録音します")
                        ch = native_channels
                log(f"[診断] ストリームオープン: ch={ch} (デバイス報告={native_channels}ch) rate={sample_rate}")

                try:
                    wf = wave.open(str(wav_path), "wb")
                    wf.setnchannels(ch)
                    wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
                    wf.setframerate(sample_rate)

                    # コールバック方式 → blocking read() によるフリーズを防ぐ
                    def _callback(in_data, frame_count, time_info, status):
                        if frames_written[0] == 0:
                            # 最初のデータが届いた時刻から、その中身の長さを引いたものが録音の開始時刻
                            self._audio_start_time = time.time() - frame_count / sample_rate
                        wf.writeframes(in_data)
                        frames_written[0] += frame_count
                        return (None, pyaudio.paContinue if not stop_event.is_set() else pyaudio.paComplete)

                    stream = pa.open(
                        format=pyaudio.paInt16,
                        channels=ch,
                        rate=sample_rate,
                        frames_per_buffer=512,
                        input=True,
                        input_device_index=device_index,
                        stream_callback=_callback,
                    )
                    stream.start_stream()
                    while stream.is_active() and not stop_event.is_set():
                        time.sleep(0.1)
                    stream.stop_stream()
                    stream.close()
                    stream = None
                    log(f"[診断] WASAPIキャプチャ終了: {frames_written[0]} フレーム書き込み")
                except Exception as e:
                    log(f"[警告] 音声キャプチャエラー: {e}")
                finally:
                    if stream:
                        try:
                            stream.stop_stream()
                            stream.close()
                        except Exception:
                            pass
                    if wf:
                        try:
                            wf.close()
                        except Exception:
                            pass
                    pa.terminate()

            self._wasapi_thread = threading.Thread(target=_capture, daemon=True)
            self._audio_start_time = time.time()
            self._wasapi_thread.start()
            self.log("WASAPIループバック音声キャプチャ開始")

        except Exception as e:
            self.log(f"[警告] 音声キャプチャ開始失敗: {e}")

    def stop(self, trim_start: float = 0.0, discard: bool = False) -> None:
        """
        録画を止めて MP4 に仕上げる。trim_start 秒より前 (冒頭) は切り落とす。
        discard=True なら録ったものを保存せずに捨てる。
        """
        if self._proc and self._proc.poll() is None:
            self.log("FFmpeg 録画停止中...")
            try:
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
                # 画面が止まっている (最小化された等) と新しいコマが来るまで q が処理されない。
                # 長く待たずに終了させる。録画は MKV に書き出し済みなので失われない。
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.terminate()
                self._proc.wait(timeout=5)

        # プロセス単位の録音を停止
        if self._proc_capture:
            self._proc_capture.stop()
            self._audio_start_time = self._proc_capture.start_time
            self._proc_capture = None

        # WASAPIキャプチャを停止
        if self._wasapi_stop:
            self._wasapi_stop.set()
        if self._wasapi_thread:
            self._wasapi_thread.join(timeout=5)
        self._wasapi_stop = None
        self._wasapi_thread = None

        # DirectShow 別録りプロセスを停止
        if self._dshow_proc and self._dshow_proc.poll() is None:
            try:
                self._dshow_proc.stdin.write(b"q")
                self._dshow_proc.stdin.flush()
                self._dshow_proc.wait(timeout=10)
            except Exception:
                self._dshow_proc.terminate()
                self._dshow_proc.wait(timeout=5)
        if self._dshow_log_file:
            try:
                self._dshow_log_file.close()
            except Exception:
                pass
            self._dshow_log_file = None
        dshow_rc = self._dshow_proc.returncode if self._dshow_proc else None
        if dshow_rc is not None:
            self.log(f"[診断] DirectShow プロセス終了コード: {dshow_rc}")
        self._dshow_proc = None

        # DirectShow WAV を WASAPI マージパスへ統合（WASAPI が無い場合のみ）
        if self._dshow_wav_path and not self._wasapi_wav_path:
            wav_size = self._dshow_wav_path.stat().st_size if self._dshow_wav_path.exists() else 0
            self.log(f"[診断] DirectShow WAV サイズ: {wav_size} bytes")
            self._wasapi_wav_path = self._dshow_wav_path
        elif self._dshow_wav_path and self._dshow_wav_path.exists():
            self._dshow_wav_path.unlink(missing_ok=True)
        self._dshow_wav_path = None
        if self._dshow_log_path and self._dshow_log_path.exists():
            self._dshow_log_path.unlink(missing_ok=True)
        self._dshow_log_path = None

        # WASAPI WAV サイズを診断ログ
        if self._wasapi_wav_path and self._wasapi_wav_path.exists():
            wav_size = self._wasapi_wav_path.stat().st_size
            self.log(f"[診断] 音声WAV サイズ: {wav_size} bytes")

        if self._stderr_file:
            self._stderr_file.close()
            self._stderr_file = None

        rc = self._proc.returncode if self._proc else None
        self.log(f"[診断] FFmpeg 終了コード: {rc}")
        try:
            import re
            stats = re.findall(r"frame=\s*(\d+).*?dup=(\d+) drop=(\d+)",
                               self._stderr_path.read_text(encoding="utf-8", errors="replace"))
            if stats:
                frames, dup, drop = stats[-1]
                self.log(f"[診断] フレーム数: {frames} (補完: {dup} / 破棄: {drop})")
        except Exception:
            pass

        if discard:
            if self._tmp_path:
                self._tmp_path.unlink(missing_ok=True)
        elif self._tmp_path and self._tmp_path.exists() and self._tmp_path.stat().st_size > 0:
            # 音声を別録りしていた場合はマージしてからリネーム
            # WAVヘッダーのみ(44バイト以下)は音声データなしとみなしてスキップ
            if self._wasapi_wav_path and self._wasapi_wav_path.exists() and \
               self._wasapi_wav_path.stat().st_size > 44:
                self._merge_audio_video()
            elif self._wasapi_wav_path:
                # ループバックは対象デバイスに音が流れていないと 1 サンプルも録れない
                self.log("[警告] 音声が録音されませんでした (映像のみで保存します)")
                default = get_default_loopback_device()
                if default and default != self._audio_device:
                    self.log(f"  → ゲーム音は既定の再生デバイスから出ます。「音声録音」で次を選んでください: {default}")
            self._finish_output(trim_start)
        else:
            self._show_ffmpeg_error()
            if self._tmp_path and self._tmp_path.exists():
                self._tmp_path.unlink()

        if self._wasapi_wav_path and self._wasapi_wav_path.exists():
            self._wasapi_wav_path.unlink()
        self._wasapi_wav_path = None

        if self._stderr_path and self._stderr_path.exists():
            self._stderr_path.unlink()

        self._proc = None

    def _merge_audio_video(self) -> None:
        """映像（音声なし）とWASAPI別録り音声をマージして _tmp_path を置き換える。"""
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.log("[警告] マージ用 ffmpeg が見つかりません (映像のみ保存)")
            return

        self.log("映像と音声をマージ中...")
        merged = self._final_path.with_name(self._final_path.stem + ".merging.mkv")

        # 映像開始から音声キャプチャ開始までのズレを補正
        offset_ms = max(0, int((self._audio_start_time - self._video_start_time) * 1000))
        self.log(f"[診断] 音声オフセット補正: {offset_ms}ms")

        # adelay で音声を遅延 → ステレオへダウンミックス
        ch = self._audio_channels
        delay_str = "|".join([str(offset_ms)] * ch)
        af = f"adelay={delay_str}"

        CREATE_NO_WINDOW = 0x08000000
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-i", str(self._tmp_path),
                    "-i", str(self._wasapi_wav_path),
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-ac", "2",        # 8ch等をステレオへダウンミックス
                    "-ar", "48000",    # デバイスごとに違うサンプルレートを揃える (後で無変換で結合できるように)
                    "-af", af,
                    "-shortest",
                    str(merged),
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
                timeout=300,
            )
            if result.returncode == 0:
                self._tmp_path.unlink(missing_ok=True)
                self._tmp_path = merged
                self.log("マージ完了")
            else:
                last = next(
                    (l.strip() for l in reversed(result.stderr.splitlines()) if l.strip()),
                    "不明なエラー"
                )
                self.log(f"[警告] マージ失敗 ({last})、映像のみで保存します")
                merged.unlink(missing_ok=True)
        except Exception as e:
            self.log(f"[警告] マージエラー: {e}、映像のみで保存します")
            merged.unlink(missing_ok=True)

    def _finish_output(self, trim_start: float = 0.0) -> None:
        """録画用の MKV を MP4 に仕上げて出力ファイルに置く (無変換なので一瞬で終わる)。"""
        if not self._tmp_path or not self._final_path:
            return
        if not self._tmp_path.exists():
            self.log("[警告] 録画ファイルが見つかりません")
            return
        import os
        ffmpeg = find_ffmpeg()
        finishing = self._final_path.with_name(self._final_path.stem + ".finishing.mp4")
        cmd = [ffmpeg, "-y"]
        if trim_start > 0:
            # -i の前の -ss は直前のキーフレームから取り込み、指定位置から再生されるようにする
            cmd += ["-ss", f"{trim_start:.3f}"]
            self.log(f"冒頭 {trim_start:.1f} 秒をカットします")
        cmd += ["-i", str(self._tmp_path), "-c", "copy", "-movflags", "+faststart", str(finishing)]

        CREATE_NO_WINDOW = 0x08000000
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW, timeout=600,
            )
            ok = result.returncode == 0 and finishing.exists() and finishing.stat().st_size > 0
            err = "" if ok else next(
                (l.strip() for l in reversed(result.stderr.splitlines()) if l.strip()), "不明なエラー")
        except Exception as e:
            ok, err = False, str(e)

        if not ok:
            finishing.unlink(missing_ok=True)
            self.log(f"[警告] MP4 への仕上げに失敗しました ({err})")
            self.log(f"  → 録画は MKV のまま残しています: {self._tmp_path}")
            return
        try:
            os.replace(str(finishing), str(self._final_path))
            self._tmp_path.unlink(missing_ok=True)
            self.log(f"保存完了: {self._final_path}")
        except PermissionError:
            self.log(f"[警告] 上書き失敗: {self._final_path.name} が別のアプリで開かれています")
            self.log(f"  → そのアプリを閉じてから一時ファイルを手動でリネームしてください")
            self.log(f"  → 一時ファイル: {finishing}")
            self._tmp_path.unlink(missing_ok=True)
        except Exception as e:
            self.log(f"[警告] リネーム失敗: {e}")
            self.log(f"一時ファイルとして保存されています: {finishing}")

    def _show_ffmpeg_error(self) -> None:
        if not self._stderr_path or not self._stderr_path.exists():
            return
        try:
            lines = self._stderr_path.read_text(encoding="utf-8", errors="replace").splitlines()
            self.log("[FFmpeg エラー] 録画ファイルが作成されませんでした")
            shown = [l for l in lines if l.strip()]
            for line in shown[-20:]:
                self.log(f"  {line}")
            if not shown:
                self.log("  (stderr 出力なし)")
        except Exception:
            pass
        if self._tmp_path and self._tmp_path.exists():
            self._tmp_path.unlink()

    @property
    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
