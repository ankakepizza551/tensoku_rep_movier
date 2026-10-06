"""配布用 exe をビルドするスクリプト。 py build.py で実行。"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DIST = HERE / "dist" / "非想天則リプレイ録画"
NAME = "非想天則リプレイ録画"


def run(cmd: list) -> None:
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print(f"\n[エラー] コマンド失敗: {' '.join(str(c) for c in cmd)}")
        sys.exit(1)


def main() -> None:
    print("===== 非想天則リプレイ録画ツール ビルド =====\n")

    # PyInstaller がなければインストール
    if not shutil.which("pyinstaller") and subprocess.run(
        [sys.executable, "-m", "pip", "show", "pyinstaller"],
        capture_output=True,
    ).returncode != 0:
        print("PyInstaller をインストールしています...")
        run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    print("ビルド中...")
    icon = HERE / "icon.ico"
    icon_args = [f"--icon={icon}"] if icon.exists() else []

    # Python DLL を検索 (PyInstaller が含め忘れるケースへの対策)
    py_ver = f"{sys.version_info.major}{sys.version_info.minor}"
    dll_name = f"python{py_ver}.dll"
    dll_src = None
    for search_dir in [
        Path(sys.executable).parent,
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.executable).parent.parent,  # venv 経由の場合
    ]:
        candidate = search_dir / dll_name
        if candidate.exists():
            dll_src = candidate
            break
    if dll_src:
        print(f"{dll_name} を検出: {dll_src}")
        dll_args = ["--add-binary", f"{dll_src};."]
    else:
        print(f"[注意] {dll_name} が見つかりません。ビルド後に手動コピーが必要な場合があります。")
        dll_args = []

    run([
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--noconfirm",   # 前回のビルド出力を確認なしで上書きする
        "--windowed",
        f"--name={NAME}",
        *icon_args,
        *dll_args,
        "--collect-all", "customtkinter",
        "--collect-all", "tkinterdnd2",
        "--hidden-import", "win32com.shell.shell",
        "--hidden-import", "win32com.shell.shellcon",
        "--hidden-import", "win32event",
        "--hidden-import", "audio_routing",
        "--hidden-import", "process_audio",
        "--hidden-import", "cv2",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageTk",
        "--collect-all", "pyaudiowpatch",
        "main.py",
    ])

    # ffmpeg フォルダをコピー
    ffmpeg_src = HERE / "ffmpeg" / "ffmpeg.exe"
    ffmpeg_dst = DIST / "ffmpeg"
    if ffmpeg_src.exists():
        print("ffmpeg をコピーしています...")
        ffmpeg_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ffmpeg_src, ffmpeg_dst / "ffmpeg.exe")
        print("ffmpeg のコピー完了")
    else:
        print(f"[注意] ffmpeg\\ffmpeg.exe が見つかりません。")
        print(f"       ビルド後に手動で {ffmpeg_dst} へコピーしてください。")

    # README とライセンスをコピー
    readme = HERE / "README.txt"
    if readme.exists():
        shutil.copy2(readme, DIST / "README.txt")
    license_file = HERE / "LICENSE"
    if license_file.exists():
        shutil.copy2(license_file, DIST / "LICENSE.txt")

    print(f"\n===== ビルド完了 =====")
    print(f"出力先: {DIST}")
    print("配布する場合はこのフォルダをまるごと zip にしてください。")


if __name__ == "__main__":
    main()
