import os
import sys
sys.path.append(os.path.abspath('.'))
import json
from dotenv import load_dotenv

# /data is the persistent disk mount on Render
DATA_DIR = os.getenv("DATA_DIR", "/data")
PROMPT_PATH = os.getenv("PROMPT_PATH", os.path.join("data", "prompt.json"))


def ensure_data_dirs():
    """Create all required /data subdirectories on startup"""
    dirs = [
        os.path.join(DATA_DIR, "logs"),
        os.path.join(DATA_DIR, "dialog"),
        os.path.join(DATA_DIR, "tweets"),
        os.path.join(DATA_DIR, "cookies"),
        os.path.join(DATA_DIR, "memory"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)


def get_config():
    with open("config.json") as f:
        cfg = json.load(f)
    # Override paths to use DATA_DIR (works locally too)
    cfg["log_path"]    = os.path.join(DATA_DIR, "logs", "erebus.log")
    cfg["dialog_path"] = os.path.join(DATA_DIR, "dialog", "dialog.jsonl")
    cfg["tweets_path"] = os.path.join(DATA_DIR, "tweets")
    cfg["cookies_path"] = os.path.join(DATA_DIR, "cookies")
    cfg["memory_path"] = os.path.join(DATA_DIR, "memory", "memory.json")
    cfg["prompt_path"] = PROMPT_PATH
    return cfg


def _first_env(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip().strip('"').strip("'")
    return default


def get_credentials():
    load_dotenv()
    return {
        "ANTHROPIC_API_KEY":               _first_env("ANTHROPIC_API_KEY"),
        "OPENAI_API_KEY":                  _first_env("OPENAI_API_KEY"),
        "TWITTER_API_CONSUMER_KEY":        _first_env("TWITTER_API_CONSUMER_KEY", "TWITTER_CONSUMER_KEY", "X_API_KEY"),
        "TWITTER_API_CONSUMER_SECRET":     _first_env("TWITTER_API_CONSUMER_SECRET", "TWITTER_CONSUMER_SECRET", "X_API_SECRET", "X_API_SECRET_KEY"),
        "TWITTER_API_BEARER_TOKEN":        _first_env("TWITTER_API_BEARER_TOKEN", "X_BEARER_TOKEN"),
        "TWITTER_API_ACCESS_TOKEN":        _first_env("TWITTER_API_ACCESS_TOKEN", "X_ACCESS_TOKEN"),
        "TWITTER_API_ACCESS_TOKEN_SECRET": _first_env("TWITTER_API_ACCESS_TOKEN_SECRET", "X_ACCESS_TOKEN_SECRET"),
        "TWITTER_user_name":               _first_env("TWITTER_user_name", "TWITTER_USERNAME", "X_USERNAME"),
        "TWITTER_email":                   _first_env("TWITTER_email", "TWITTER_EMAIL", "X_EMAIL"),
        "TWITTER_pwd":                     _first_env("TWITTER_pwd", "TWITTER_PASSWORD", "X_PASSWORD"),
        "TWITTER_phone":                   _first_env("TWITTER_phone", "TWITTER_PHONE", "X_PHONE", default=""),
    }


def get_prompt():
    if not os.path.exists(PROMPT_PATH):
        raise FileNotFoundError(f"Prompt file not found: {PROMPT_PATH}")
    with open(PROMPT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if "erebus" not in data:
        raise KeyError(f"Prompt file missing 'erebus' root key: {PROMPT_PATH}")
    block = data["erebus"]
    if not isinstance(block, dict) or not block.get("system") or not block.get("user"):
        raise ValueError(f"Prompt file missing erebus.system or erebus.user: {PROMPT_PATH}")
    return data
