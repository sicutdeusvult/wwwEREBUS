"""
threadReader.py — Fetch full conversation thread context before replying.
When GNOSIS is tagged, read the full thread so it replies with full context,
not just the single tweet it was mentioned in.
"""
import os, sys
sys.path.append(os.path.abspath('.'))
from src.logs import logs

class threadReader:
    def __init__(self, tweepy_client):
        self.client = tweepy_client
        self.logs = logs()

    def get_thread(self, tweet_id: str, conversation_id: str = None, max_tweets: int = 8) -> list[dict]:
        """
        Fetch up to max_tweets from the conversation thread.
        Returns list of {handle, text, tweet_id} dicts, oldest first.
        """
        try:
            cid = conversation_id or tweet_id
            resp = self.client.search_recent_tweets(
                query=f"conversation_id:{cid}",
                max_results=max(10, min(max_tweets, 100)),
                tweet_fields=["id", "text", "author_id", "created_at", "in_reply_to_user_id"],
                expansions=["author_id"],
                user_fields=["username"],
                sort_order="recency",
            )
            if not resp or not resp.data:
                return []

            user_map = {}
            if hasattr(resp, 'includes') and resp.includes and 'users' in resp.includes:
                for u in resp.includes['users']:
                    user_map[str(u.id)] = u.username

            thread = []
            for t in reversed(resp.data):
                handle = user_map.get(str(getattr(t, 'author_id', '')), '?')
                thread.append({
                    "tweet_id": str(t.id),
                    "handle": handle,
                    "text": t.text,
                })

            self.logs.log_info(f"thread: {len(thread)} tweets in conversation {cid[:12]}...", "dim cyan", "Thread")
            return thread

        except Exception as e:
            self.logs.log_error(f"threadReader: {e}")
            return []

    def format_for_prompt(self, thread: list[dict]) -> str:
        """Format thread as readable context string."""
        if not thread:
            return ""
        lines = ["[THREAD CONTEXT — full conversation before this mention]"]
        for t in thread:
            lines.append(f"@{t['handle']}: {t['text']}")
        return "\n".join(lines)

    def _extract_media_from_response(self, resp) -> list[str]:
        """Pull image/video URLs from a tweepy response includes.media block."""
        urls = []
        if not (hasattr(resp, 'includes') and resp.includes and 'media' in resp.includes):
            return urls
        for m in resp.includes['media']:
            mtype = getattr(m, 'type', '')
            if mtype == 'photo':
                url = getattr(m, 'url', None)
                if url:
                    urls.append(url)
            elif mtype in ('video', 'animated_gif'):
                # preview_image_url is the thumbnail — best we can do without video download
                url = getattr(m, 'preview_image_url', None)
                if url:
                    urls.append(url)
        return urls

    def extract_media_urls(self, tweet_id: str) -> list[str]:
        """
        Fetch media from a tweet AND any tweets it references (quotes/replies).
        Per API docs: use expansions=[attachments.media_keys, referenced_tweets.id,
        referenced_tweets.id.attachments.media_keys] to get media from quoted tweets too.
        Returns list of image/video thumbnail URLs.
        """
        try:
            resp = self.client.get_tweet(
                id=tweet_id,
                expansions=[
                    "attachments.media_keys",
                    "referenced_tweets.id",
                    "referenced_tweets.id.attachments.media_keys",
                ],
                media_fields=["url", "preview_image_url", "type", "media_key", "variants"],
                tweet_fields=["id", "text", "attachments", "referenced_tweets"],
            )
            if not resp or not resp.data:
                return []

            urls = self._extract_media_from_response(resp)

            # Also check referenced tweets (quoted posts, parent replies)
            # The API returns these in resp.includes['tweets']
            if hasattr(resp, 'includes') and resp.includes:
                ref_tweets = resp.includes.get('tweets', []) if isinstance(resp.includes, dict) else getattr(resp.includes, 'tweets', [])
                if ref_tweets:
                    self.logs.log_info(f"vision: found {len(ref_tweets)} referenced tweet(s) to check for media", "dim cyan", "Vision")
                    for ref_t in ref_tweets:
                        # Fetch the referenced tweet's media separately
                        ref_id = str(ref_t.id if hasattr(ref_t, 'id') else ref_t.get('id', ''))
                        if ref_id:
                            try:
                                ref_resp = self.client.get_tweet(
                                    id=ref_id,
                                    expansions=["attachments.media_keys"],
                                    media_fields=["url", "preview_image_url", "type"],
                                )
                                ref_urls = self._extract_media_from_response(ref_resp)
                                if ref_urls:
                                    self.logs.log_info(f"vision: {len(ref_urls)} media from referenced tweet {ref_id[:12]}...", "bold cyan", "Vision")
                                    urls.extend(ref_urls)
                            except Exception as re:
                                self.logs.log_error(f"threadReader ref media: {re}")

            if urls:
                self.logs.log_info(f"vision: total {len(urls)} media URL(s) found for tweet {tweet_id[:12]}...", "bold cyan", "Vision")
            return urls[:3]  # cap at 3

        except Exception as e:
            self.logs.log_error(f"threadReader media: {e}")
            return []
