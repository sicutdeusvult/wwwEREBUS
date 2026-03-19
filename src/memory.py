import os
import sys
import json
from datetime import datetime
sys.path.append(os.path.abspath('.'))

from src.config import get_config
from src.utils import make_dir_not_exist

config = get_config()

class memory:
    def __init__(self):
        self.memory_path = config.get('memory_path', '/data/memory/memory.json')
        self.stats_path  = self.memory_path.replace('memory.json', 'stats.json')
        make_dir_not_exist(self.memory_path)
        if not os.path.exists(self.memory_path):
            self._save([])
        if not os.path.exists(self.stats_path):
            self._save_stats({})

    def _load(self):
        try:
            with open(self.memory_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self, data):
        try:
            make_dir_not_exist(self.memory_path)
            with open(self.memory_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[MEMORY SAVE ERROR] {e}")

    def _load_stats(self):
        try:
            with open(self.stats_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_stats(self, data):
        try:
            with open(self.stats_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[STATS SAVE ERROR] {e}")

    def add_entry(self, action: str, content: str, tweet_id: str = "",
                  shape: str = "", topic: str = "", self_score: int = 0):
        memories = self._load()
        entry = {
            "ts":         datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "action":     action,
            "content":    content,
            "tweet_id":   tweet_id,
            "shape":      shape,
            "topic":      topic,
            "self_score": self_score,
            "engagement": 0,
            "likes":      0,
            "replies":    0,
            "retweets":   0,
            "day":        datetime.now().strftime('%Y-%m-%d'),
            "hour":       datetime.now().hour,
        }
        memories.append(entry)
        if len(memories) > 2000:
            memories = memories[-2000:]
        self._save(memories)
        self._update_stats(memories)
        return entry

    def update_engagement(self, tweet_id: str, likes: int, replies: int, retweets: int):
        memories = self._load()
        for m in memories:
            if m.get('tweet_id') == tweet_id:
                m['likes']      = likes
                m['replies']    = replies
                m['retweets']   = retweets
                m['engagement'] = likes + (replies * 3) + (retweets * 2)
                break
        self._save(memories)
        self._update_stats(memories)

    def updat_memory(self):
        pass

    def quer_memory(self) -> str:
        memories = self._load()
        if not memories:
            return ""
        parts = []

        recent = [m for m in memories if m.get('action') in ('post', 'reply')][-8:]
        if recent:
            lines = []
            for m in recent:
                shape = f"[{m.get('shape','?')}]" if m.get('shape') else ""
                eng   = f" +{m.get('engagement',0)}" if m.get('engagement', 0) > 0 else ""
                lines.append(f"{m.get('ts','')} {shape} {m.get('action','')} — {m.get('content','')[:80]}{eng}")
            parts.append("[RECENT TRANSMISSIONS]\n" + "\n".join(lines))

        top = sorted(
            [m for m in memories if m.get('engagement', 0) > 0],
            key=lambda x: x.get('engagement', 0), reverse=True
        )[:3]
        if top:
            lines = [f"  +{m.get('engagement',0)} | {m.get('content','')[:80]}" for m in top]
            parts.append("[YOUR BEST TRANSMISSIONS — these worked]\n" + "\n".join(lines))

        report = self._pattern_report(memories)
        if report:
            parts.append(report)

        return "\n\n".join(parts)

    def best_posts(self, n: int = 10) -> list:
        memories = self._load()
        posts = [m for m in memories if m.get('action') == 'post']
        return sorted(posts, key=lambda x: x.get('engagement', 0), reverse=True)[:n]

    def recent_posts(self, n: int = 20) -> list:
        memories = self._load()
        return [m for m in memories if m.get('action') in ('post', 'reply')][-n:]

    def used_shapes_recently(self, n: int = 10) -> list:
        return [m.get('shape', '') for m in self.recent_posts(n) if m.get('shape')]

    def _update_stats(self, memories):
        posts = [m for m in memories if m.get('action') == 'post']
        if not posts:
            return
        shape_stats = {}
        for m in posts:
            s = m.get('shape', 'unknown')
            if s not in shape_stats:
                shape_stats[s] = {'count': 0, 'total_eng': 0, 'avg_eng': 0}
            shape_stats[s]['count'] += 1
            shape_stats[s]['total_eng'] += m.get('engagement', 0)
        for s in shape_stats:
            c = shape_stats[s]['count']
            shape_stats[s]['avg_eng'] = round(shape_stats[s]['total_eng'] / c, 1) if c else 0
        hour_eng = {}
        for m in posts:
            h = m.get('hour', 0)
            hour_eng[h] = hour_eng.get(h, 0) + m.get('engagement', 0)
        self._save_stats({
            "total_posts":       len(posts),
            "total_replies":     len([m for m in memories if m.get('action') == 'reply']),
            "total_engagement":  sum(m.get('engagement', 0) for m in posts),
            "shape_performance": shape_stats,
            "best_hour":         max(hour_eng, key=hour_eng.get) if hour_eng else None,
            "last_updated":      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })

    def _pattern_report(self, memories) -> str:
        stats = self._load_stats()
        if not stats:
            return ""
        shape_perf = stats.get('shape_performance', {})
        ranked = [(s, d['avg_eng'], d['count']) for s, d in shape_perf.items() if d['count'] >= 3]
        if not ranked:
            return ""
        ranked.sort(key=lambda x: -x[1])
        lines = ["[PATTERN REPORT — what erebus has learned]"]
        for shape, avg, count in ranked[:2]:
            lines.append(f"  WORKS: {shape} — avg engagement {avg} ({count} posts)")
        for shape, avg, count in ranked[-2:] if len(ranked) > 3 else []:
            lines.append(f"  WEAK:  {shape} — avg engagement {avg} ({count} posts)")
        total = stats.get('total_posts', 0)
        total_eng = stats.get('total_engagement', 0)
        if total > 0:
            lines.append(f"  TOTAL: {total} transmissions, {total_eng} total engagement")
        return "\n".join(lines)

    def get_stats(self) -> dict:
        return self._load_stats()
