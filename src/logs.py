import os
import sys
from datetime import datetime
sys.path.append(os.path.abspath('.'))

try:
    from rich.panel import Panel
    from rich import print as rprint
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from src.config import get_config
from src.utils import make_dir_not_exist

config = get_config()

class logs:
    def __init__(self):
        self.log_file = config['log_path']
        make_dir_not_exist(self.log_file)

    def _write(self, message):
        try:
            make_dir_not_exist(self.log_file)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(message + "\n")
        except Exception as e:
            print(f"[LOG WRITE ERROR] {e}")

    def log_error(self, s):
        message = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] {s}"
        print(message)
        self._write(message)

    def log_info(self, s, border_style=None, title=None, subtitle=None):
        message = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {s}"
        if border_style and RICH_AVAILABLE:
            rprint(Panel(message, title=title, subtitle=subtitle,
                         border_style=border_style, padding=(1, 2)))
        else:
            if title:
                print(f"\n{'='*10} {title} {'='*10}")
            print(message)
        self._write(message)
