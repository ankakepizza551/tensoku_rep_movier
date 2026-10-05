"""th123 の起動・ウィンドウ検出・キー操作を担当するモジュール。"""

import ctypes
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

import win32api
import win32con
import win32gui
import win32process

# Virtual Key コード
VK_Z = 0x5A
VK_DOWN = 0x28

# ! はどの数字・漢字より前にソートされるので replay/ の先頭に来る
TEMP_FOLDER_NAME = "!"          # 先頭フォルダ名（どの日付フォルダより前にソート）
TEMP_REP_NAME    = "temp.rep"   # フォルダ内の一時ファイル名


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]

class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg",    ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]

class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]
    _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]

_INPUT_KEYBOARD       = 1
_KEYEVENTF_KEYUP      = 0x0002
_KEYEVENTF_EXTENDEDKEY = 0x0001

# 拡張キー（E0プレフィックスが必要な矢印キー等）
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
# VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_INSERT, VK_DELETE


def _send_key(hwnd: int, vk: int) -> int:
    """
    SendInput でキーダウン→待機→キーアップを送る。
    DirectInput の即時ポーリングに確実に捕捉させるためダウンとアップを分ける。
    """
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)

    scan  = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
    sz    = ctypes.sizeof(_INPUT)

    down = (_INPUT * 1)(
        _INPUT(type=_INPUT_KEYBOARD,
               _u=_INPUT._U(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))
    )
    up = (_INPUT * 1)(
        _INPUT(type=_INPUT_KEYBOARD,
               _u=_INPUT._U(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags | _KEYEVENTF_KEYUP)))
    )
    n  = ctypes.windll.user32.SendInput(1, down, sz)
    time.sleep(0.1)   # DirectInput のポーリング間隔より長く保持
    n += ctypes.windll.user32.SendInput(1, up, sz)
    return n


# ── ウィンドウ検索 ────────────────────────────────────────

def _get_exe_name_for_pid(pid: int) -> str | None:
    """
    管理者権限で動いているプロセスにも対応できるよう
    PROCESS_QUERY_LIMITED_INFORMATION + QueryFullProcessImageNameW で取得する。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    hproc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size))
        return buf.value if ok else None
    finally:
        ctypes.windll.kernel32.CloseHandle(hproc)


def _find_hwnd_by_exe(exe_name: str) -> int | None:
    """プロセス名で可視ウィンドウの hwnd を返す（管理者プロセスも対応）。"""
    found: list[int] = []
    exe_lower = exe_name.lower()

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            path = _get_exe_name_for_pid(pid)
            if path and Path(path).name.lower() == exe_lower:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def is_already_running(th123_path: str) -> bool:
    """th123.EXE がすでに起動中かどうかを確認する。"""
    return _find_hwnd_by_exe(Path(th123_path).name) is not None


def find_game_hwnd(th123_path: str) -> int | None:
    """起動中の th123.EXE のウィンドウハンドルを返す。見つからなければ None。"""
    return _find_hwnd_by_exe(Path(th123_path).name)


def _find_hwnd_by_pid(pid: int) -> int | None:
    """指定 PID のプロセスに紐づく可視ウィンドウの hwnd を返す。"""
    found: list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
            if win_pid == pid:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def wait_for_window(pid: int, timeout: float = 30.0, log: Callable = print) -> int:
    """起動したプロセスの PID でウィンドウを待機する。既存の同名プロセスとは区別される。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_hwnd_by_pid(pid)
        if hwnd:
            title = win32gui.GetWindowText(hwnd)
            log(f"ゲームウィンドウを検出しました: 「{title}」")
            return hwnd
        time.sleep(0.5)
    raise RuntimeError(f"ゲームウィンドウが {timeout:.0f}秒以内に見つかりませんでした")


def focus_window(hwnd: int) -> None:
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    except Exception:
        pass


def disable_rounded_corners(hwnd: int) -> None:
    """
    Windows 11 のウィンドウ角丸を無効にする。
    角丸のままだと、録画した映像の下隅数ピクセルに後ろの画面が写り込む。
    """
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_DONOTROUND = 1
    try:
        pref = ctypes.c_int(DWMWCP_DONOTROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
    except Exception:
        pass   # Windows 10 以前は角丸自体がない


def grab_window_png(hwnd: int, ffmpeg: str) -> bytes | None:
    """
    ゲームウィンドウのクライアント領域を 1 枚だけ撮って PNG のバイト列で返す。
    録画と同じくウィンドウ単位で撮るので、他のウィンドウが重なっていても写り込まない。
    """
    CREATE_NO_WINDOW = 0x08000000
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "gdigrab", "-draw_mouse", "0", "-i", f"hwnd={hwnd}",
             "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
            capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        )
        return proc.stdout if proc.returncode == 0 and proc.stdout else None
    except Exception:
        return None


def sample_window_pixels(hwnd: int, cols: int = 5, rows: int = 4) -> list[int] | None:
    """
    ゲームウィンドウのクライアント領域をグリッドサンプリングしてピクセル値リストを返す。
    画面変化の検出に使用する。取得失敗時は None を返す。

    ウィンドウ DC から BitBlt で取得する（録画の gdigrab と同じ取り方）。
    PrintWindow は th123 (Direct3D) だと常に真っ黒を返すため使えない。
    真っ黒だとキー送信の受付判定が毎回「反応なし」になって Z を余分に再送し、
    メニュー遷移がずれる / 静止検出が誤発火する。
    """
    try:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        w, h = cr - cl, cb - ct
        if w <= 0 or h <= 0:
            return None

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        SRCCOPY = 0x00CC0020

        hdc_win = user32.GetDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
        bitmap = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
        old_obj = gdi32.SelectObject(hdc_mem, bitmap)
        try:
            if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_win, 0, 0, SRCCOPY):
                return None
            xs = [int(w * (i + 1) / (cols + 1)) for i in range(cols)]
            ys = [int(h * (j + 1) / (rows + 1)) for j in range(rows)]
            return [gdi32.GetPixel(hdc_mem, x, y) for x in xs for y in ys]
        finally:
            gdi32.SelectObject(hdc_mem, old_obj)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_win)
    except Exception:
        return None


# ── シーン取得 ──────────────────────────────────────────

# th123.exe (Ver1.10a) が現在のシーン ID / 遷移先シーン ID を置いているアドレス
_ADDR_SCENE_ID     = 0x008A0040
_ADDR_NEW_SCENE_ID = 0x008A0044

SCENE_LOGO    = 0
SCENE_TITLE   = 2
SCENE_BATTLE  = 5
SCENE_LOADING = 6

_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.ReadProcessMemory.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def read_scene(pid: int) -> tuple[int, int] | None:
    """(現在のシーン ID, 遷移先シーン ID) を返す。読めなければ None。"""
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    hproc = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hproc:
        return None
    try:
        values = []
        for addr in (_ADDR_SCENE_ID, _ADDR_NEW_SCENE_ID):
            buf = ctypes.c_uint32(0)
            if not _kernel32.ReadProcessMemory(hproc, addr, ctypes.byref(buf), 4, None):
                return None
            values.append(buf.value)
        return values[0], values[1]
    finally:
        _kernel32.CloseHandle(hproc)


def wait_for_scene(
    pid: int,
    scenes: tuple[int, ...],
    timeout: float,
    should_stop: Callable[[], bool] = lambda: False,
) -> bool | None:
    """
    現在・遷移先の両方が scenes のいずれかになる (= 遷移が完了する) まで待つ。
    到達したら True、タイムアウト/中断なら False、シーンが読めない場合は None。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if should_stop():
            return False
        scene = read_scene(pid)
        if scene is None:
            return None
        if scene[0] in scenes and scene[1] in scenes:
            return True
        time.sleep(0.1)
    return False


# ── リプレイファイル管理 ──────────────────────────────────

def copy_rep_to_game(rep_path: str, th123_path: str) -> Path:
    """
    replay/!/temp.rep としてコピーする。
    「!」フォルダはどの日付フォルダよりソート順が前なのでリスト先頭に来る。
    ゲームはフォルダを先に表示するため、ファイルではなくフォルダで先頭を取る。
    """
    temp_dir = Path(th123_path).parent / "replay" / TEMP_FOLDER_NAME
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest = temp_dir / TEMP_REP_NAME
    shutil.copy2(rep_path, dest)
    return dest


def remove_rep_from_game(dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    # フォルダが空になったら消す
    try:
        dest.parent.rmdir()
    except OSError:
        pass


# ── キー操作 ────────────────────────────────────────────

def _send_key_with_verify(
    hwnd: int,
    vk: int,
    log: Callable = print,
    attempts: int = 3,
    wait_secs: float = 2.0,
    threshold: int = 8,
) -> None:
    """
    キーを送信し、画面が大きく変化したら受付成功と判定する。
    反応がなければ最大 attempts 回まで再送する。
    タイトル画面で初回 Z が入らずリプレイ画面に進めない問題への対策。
    """
    for i in range(1, attempts + 1):
        baseline = sample_window_pixels(hwnd)
        _send_key(hwnd, vk)
        if baseline is None:
            return  # ピクセル取得不可なら単発送信で終了
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            time.sleep(0.15)
            current = sample_window_pixels(hwnd)
            if current is None or len(current) != len(baseline):
                continue
            changed = sum(p != q for p, q in zip(baseline, current))
            if changed >= threshold:
                if i > 1:
                    log(f"  → {i} 回目で受付")
                return
        if i < attempts:
            log(f"  → 反応なし、再送 ({i}/{attempts})")
        else:
            log(f"  → 警告: 画面変化が検出できませんでした")


def send_key_sequence(
    hwnd: int,
    down_count: int,
    z_after_select: int,
    key_delay: float,
    log: Callable = print,
) -> None:
    """
    タイトル画面 → リプレイ再生開始 までキーを送る。
    一時ファイルは常にリスト先頭なので ↓ での位置合わせは不要。

    シーケンス:
        Z                   タイトル確定
        ↓ × down_count      リプレイ鑑賞メニューへ
        Z × z_after_select  リスト入場 → 先頭選択 → 再生開始
    """
    # 診断: フォーカスが正しくゲームウィンドウに当たっているか確認
    focus_hwnd = ctypes.windll.user32.GetForegroundWindow()
    focus_title = win32gui.GetWindowText(focus_hwnd)
    game_title  = win32gui.GetWindowText(hwnd)
    log(f"[診断] フォーカス中: 「{focus_title}」 / ゲーム: 「{game_title}」 / 一致: {focus_hwnd == hwnd}")

    log("z (タイトル確定)")
    _send_key_with_verify(hwnd, VK_Z, log=log)
    # タイトル確定後はメニュー遷移アニメーションがあり、その間は入力を受け付けない。
    # key_delay より長い固定1秒を確保する。
    time.sleep(max(key_delay, 1.0))

    log(f"↓ × {down_count} (リプレイ鑑賞メニューへ)")
    for _ in range(down_count):
        _send_key(hwnd, VK_DOWN)
        time.sleep(key_delay)

    log(f"Z × {z_after_select} (リスト入場・選択・再生)")
    for i in range(z_after_select):
        if i == 0:
            # 最初の Z はメニュー→リスト遷移で大きく変わる。検証付きで送る
            _send_key_with_verify(hwnd, VK_Z, log=log)
        else:
            _send_key(hwnd, VK_Z)
        time.sleep(key_delay)

    log("キー操作完了")


# ── ゲーム起動 ───────────────────────────────────────────

class _ElevatedProcess:
    """ShellExecuteEx で起動した管理者プロセスのラッパー。Popen と同じ poll/terminate インターフェース。"""

    def __init__(self, h_process: int):
        self._h = h_process
        import win32process as _wp
        self.pid: int = _wp.GetProcessId(h_process)

    def poll(self) -> int | None:
        import win32event
        result = win32event.WaitForSingleObject(self._h, 0)
        return None if result == win32event.WAIT_TIMEOUT else 0

    def terminate(self) -> None:
        import win32process
        try:
            win32process.TerminateProcess(self._h, 0)
        except Exception:
            pass


def launch_game(th123_path: str) -> subprocess.Popen | _ElevatedProcess:
    """
    th123.EXE を起動する。
    戻り値の .pid で起動したプロセスの PID を取得できる。

    ゲームが管理者権限を要求する設定 (WinError 740) の場合、このツールも管理者で
    動いていないとキー送信が届かない。ツールが管理者なら昇格して起動し、
    そうでなければ案内を出して中断する。
    """
    try:
        return subprocess.Popen(
            [th123_path],
            cwd=str(Path(th123_path).parent),
        )
    except OSError as e:
        if getattr(e, "winerror", None) == 740:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                raise RuntimeError(
                    "th123.EXE が管理者権限を要求しています。\n"
                    "このツールを右クリック →「管理者として実行」で起動し直してください。"
                ) from e
            import win32com.shell.shell as shell
            import win32com.shell.shellcon as shellcon
            result = shell.ShellExecuteEx(
                fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
                lpVerb="runas",
                lpFile=th123_path,
                lpDirectory=str(Path(th123_path).parent),
                nShow=1,
            )
            return _ElevatedProcess(result["hProcess"])
        raise


def is_running(proc) -> bool:
    return proc.poll() is None
