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


def get_best_encoder(ffmpeg: str) -> str:
    """利用可能な最速のエンコーダーを返す。"""
    CREATE_NO_WINDOW = 0x08000000
    try:
        proc = subprocess.run(
            [ffmpeg, "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW
        )
        if "h264_nvenc" in proc.stdout: return "h264_nvenc"  # NVIDIA
        if "h264_amf" in proc.stdout:   return "h264_amf"    # AMD
        if "h264_qsv" in proc.stdout:   return "h264_qsv"    # Intel
    except Exception:
        pass
    return "libx264"


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

    def start(
        self,
        hwnd: int,
        output_path: str,
        framerate: int,
        crf: int,
        preset: str,
        audio_device: str | None = None,
    ) -> None:
        import win32gui

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise FileNotFoundError("ffmpeg.exe が見つかりません。")

        encoder = get_best_encoder(ffmpeg)
        self.log(f"[診断] 使用エンコーダー: {encoder}")

        # クライアント領域を取得
        import ctypes
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        pt = ctypes.wintypes.POINT(cl, ct)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        left, top = pt.x, pt.y
        width, height = cr - cl, cb - ct
        width  = width  if width  % 2 == 0 else width  - 1
        height = height if height % 2 == 0 else height - 1

        self._final_path = Path(output_path)
        tmp_fd, tmp_str = tempfile.mkstemp(suffix=".mp4", dir=self._final_path.parent)
        import os; os.close(tmp_fd)
        self._tmp_path = Path(tmp_str)

        # WASAPI・DirectShow ともに別録り → FFmpeg 映像のみ
        use_wasapi = audio_device and audio_device.startswith(WASAPI_PREFIX)
        use_dshow  = bool(audio_device) and not use_wasapi

        # 映像入力 (gdigrab) — 音声は含めない
        cmd = [
            ffmpeg, "-y",
            "-thread_queue_size", "2048",
            "-f", "gdigrab",
            "-draw_mouse", "0",
            "-framerate", str(framerate),
            "-offset_x", str(left),
            "-offset_y", str(top),
            "-video_size", f"{width}x{height}",
            "-i", "desktop",
        ]

        # 映像エンコード
        cmd.extend(["-c:v", encoder])
        if encoder == "libx264":
            effective_preset = preset
            if crf <= 17 and preset not in ("ultrafast", "superfast"):
                effective_preset = "ultrafast"
                self.log(
                    f"[注意] CRF {crf} はlibx264の {preset} では追いつかないため "
                    f"preset を ultrafast に自動上書きしました (画質は同等、ファイル増)"
                )
            cmd.extend([
                "-preset", effective_preset,
                "-tune", "zerolatency",
                "-crf", str(crf),
            ])
        elif encoder == "h264_nvenc":
            cmd.extend(["-preset", "p1", "-rc", "vbr", "-cq", str(crf), "-gpu", "any"])
        else:
            cmd.extend(["-preset", "fast"])

        cmd.extend(["-pix_fmt", "yuv420p"])
        cmd.extend([str(self._tmp_path)])

        self._stderr_path = Path(tempfile.mktemp(suffix="_ffmpeg.log"))
        self._stderr_file = open(self._stderr_path, "w", encoding="utf-8", errors="replace")

        CREATE_NO_WINDOW = 0x08000000
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_file,
            creationflags=CREATE_NO_WINDOW | ABOVE_NORMAL_PRIORITY_CLASS,
        )

        self._video_start_time = time.time()
        time.sleep(0.5)
        rc = self._proc.poll()
        if rc is not None:
            self._stderr_file.flush()
            err_text = ""
            try:
                err_text = self._stderr_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
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
        if use_wasapi:
            self._start_wasapi_capture(audio_device)
        elif use_dshow:
            self._start_dshow_capture(audio_device)

        self.log(f"FFmpeg 録画開始: {self._final_path.name}")

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

            if device_info is None:
                self.log("[警告] WASAPIループバックデバイスが見つかりません (音声なしで録画します)")
                pa.terminate()
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

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self.log("FFmpeg 録画停止中...")
            try:
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
                self._proc.wait(timeout=15)
            except Exception:
                self._proc.terminate()
                self._proc.wait(timeout=5)

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

        if self._tmp_path and self._tmp_path.exists() and self._tmp_path.stat().st_size > 0:
            # 音声を別録りしていた場合はマージしてからリネーム
            # WAVヘッダーのみ(44バイト以下)は音声データなしとみなしてスキップ
            if self._wasapi_wav_path and self._wasapi_wav_path.exists() and \
               self._wasapi_wav_path.stat().st_size > 44:
                self._merge_audio_video()
            self._rename_output()
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
        tmp_fd, tmp_str = tempfile.mkstemp(suffix="_merged.mp4", dir=self._final_path.parent)
        import os; os.close(tmp_fd)
        merged = Path(tmp_str)

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

    def _rename_output(self) -> None:
        if not self._tmp_path or not self._final_path:
            return
        if not self._tmp_path.exists():
            self.log("[警告] 録画ファイルが見つかりません")
            return
        try:
            import os
            os.replace(str(self._tmp_path), str(self._final_path))
            self.log(f"保存完了: {self._final_path}")
        except PermissionError:
            self.log(f"[警告] 上書き失敗: {self._final_path.name} が別のアプリで開かれています")
            self.log(f"  → そのアプリを閉じてから一時ファイルを手動でリネームしてください")
            self.log(f"  → 一時ファイル: {self._tmp_path}")
        except Exception as e:
            self.log(f"[警告] リネーム失敗: {e}")
            self.log(f"一時ファイルとして保存されています: {self._tmp_path}")

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
