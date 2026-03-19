import os
import sys
import json
from datetime import datetime
sys.path.append(os.path.abspath('.'))

from src.config import get_config
from src.utils import make_dir_not_exist
from interface.dialogManagerInterface import dialogManagerInterface

config = get_config()

class dialogManager(dialogManagerInterface):
    def __init__(self):
        self.dialog_path = config.get('dialog_path', '/data/dialog/dialog.jsonl')
        make_dir_not_exist(self.dialog_path)

    def write_dialog(self, decision: dict, path: str = None):
        """Append decision as JSONL entry to dialog file"""
        path = path or self.dialog_path
        try:
            make_dir_not_exist(path)
            entry = {
                "ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "action": decision.get("action", ""),
                "target_tweet_id": decision.get("target_tweet_id", ""),
                "content": decision.get("content", "")
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[DIALOG WRITE ERROR] {e}")

    def read_dialog(self, path: str = None) -> str:
        """Read last 5 dialog entries as context string"""
        path = path or self.dialog_path
        if not os.path.exists(path):
            return "None"
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            if not lines:
                return "None"
            recent = lines[-5:]
            entries = []
            for line in recent:
                try:
                    d = json.loads(line)
                    entries.append(f"[{d.get('ts','')}] {d.get('action','')} — {d.get('content','')[:80]}")
                except Exception:
                    pass
            return "\n".join(entries) if entries else "None"
        except Exception as e:
            return "None"
