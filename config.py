import json
import os
import sys
from pathlib import Path

_LOCAL_FILE = Path(__file__).parent / "config.json"

if getattr(sys, "frozen", False):
    # exe 版: _internal/ の中はビルドや更新のたびに作り直されて設定が消えるので、
    # ユーザーごとの %APPDATA% に保存する。
    CONFIG_FILE = Path(os.environ.get("APPDATA") or Path.home()) / "SokuReplayRecorder" / "config.json"
    _LEGACY_FILE = _LOCAL_FILE   # 旧バージョンの保存先 (初回だけ引き継ぐ)
else:
    CONFIG_FILE = _LOCAL_FILE
    _LEGACY_FILE = None

DEFAULT_CONFIG = {
    "th123_path": "",
    "output_dir": str(Path.home() / "Videos"),
    "framerate": 60,
    "wait_after_launch": 5.0,   # ゲーム起動後キー送信するまでの秒数
    "key_delay": 0.3,           # キー操作間の待機秒数
    "down_count": 6,            # タイトルからリプレイメニューへの↓回数
    "z_after_select": 4,        # リスト入場+フォルダ入場+ファイル選択+再生
    "crf": 20,                  # FFmpeg品質 (低いほど高品質)
    "preset": "veryfast",
    "auto_open_trim": False,
    "auto_stop": True,
    "game_audio_only": False,
    "filename_template": "{stem}",
}


def load() -> dict:
    for path in (CONFIG_FILE, _LEGACY_FILE):
        if path is None or not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return DEFAULT_CONFIG.copy()


def save(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
