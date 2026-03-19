import os, sys
sys.path.append(os.path.abspath('.'))
from src.xBridge import xBridge
from src.logs import logs
from src.memory import memory as MemoryStore

class actionX:
    def __init__(self):
        self.xBridge_instance = xBridge()
        self.logs = logs()
        self.memory_store = MemoryStore()

    def excute(self, action: dict) -> str:
        """Execute action. Returns tweet_id string (or tweet_id being retweeted)."""
        target_tweet_id = action.get('target_tweet_id', '') or ''
        action_type     = action.get('action', '') or 'post'
        content         = action.get('content', '') or ''

        # ── Actions that need only target_tweet_id, no content ──────────────
        if action_type == 'retweet':
            if not target_tweet_id:
                self.logs.log_error('retweet: no target_tweet_id')
                return ''
            success = self.xBridge_instance.retweet(target_tweet_id)
            if success:
                self.memory_store.add_entry('retweet', f'retweeted:{target_tweet_id}')
            return target_tweet_id if success else ''

        if action_type == 'unretweet':
            if not target_tweet_id:
                self.logs.log_error('unretweet: no target_tweet_id')
                return ''
            success = self.xBridge_instance.unretweet(target_tweet_id)
            if success:
                self.memory_store.add_entry('unretweet', f'unretweeted:{target_tweet_id}')
            return target_tweet_id if success else ''

        if action_type == 'like':
            if not target_tweet_id:
                self.logs.log_error('like: no target_tweet_id')
                return ''
            success = self.xBridge_instance.like(target_tweet_id)
            if success:
                self.memory_store.add_entry('like', f'liked:{target_tweet_id}')
            return target_tweet_id if success else ''

        if action_type == 'unlike':
            if not target_tweet_id:
                self.logs.log_error('unlike: no target_tweet_id')
                return ''
            success = self.xBridge_instance.unlike(target_tweet_id)
            if success:
                self.memory_store.add_entry('unlike', f'unliked:{target_tweet_id}')
            return target_tweet_id if success else ''

        # All other actions need content
        if not content:
            self.logs.log_error(f'No content for action: {action_type}')
            return ''

        content = content[:25000]   # X Blue/Verified: up to 25,000 chars (was 280 — pre-blue)

        tid = ''
        if action_type in ('tweet', 'post'):
            tid = self.xBridge_instance.tweet(text=content)

        elif action_type == 'reply' and target_tweet_id:
            tid = self.xBridge_instance.reply(
                in_reply_to_tweet_id=target_tweet_id,
                text=content
            )

        elif action_type == 'quote' and target_tweet_id:
            # Twitter only allows quoting tweets you're mentioned in or part of.
            # For feed tweets (not mentions), fall back to reply instead.
            tid = self.xBridge_instance.quote(
                quote_tweet_id=target_tweet_id,
                text=content
            )
            if not tid:
                # quote failed (likely 403 not-in-thread) — reply instead
                self.logs.log_error('quote failed, falling back to reply')
                tid = self.xBridge_instance.reply(
                    in_reply_to_tweet_id=target_tweet_id,
                    text=content
                )

        else:
            # Fallback: plain post
            self.logs.log_error(f'Unknown/incomplete action "{action_type}" — falling back to post')
            tid = self.xBridge_instance.tweet(text=content)

        if tid:
            self.memory_store.add_entry(action_type, content)

        return tid or ''
