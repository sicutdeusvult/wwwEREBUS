"""
xBridge — X API v2 via Tweepy.

Observation (priority order):
  1. GET /2/users/:id/timelines/reverse_chronological  (OAuth user context)
  2. GET /2/users/:id/mentions
  3. GET /2/tweets/{id}/quote_tweets  (who quoted erebus)
  4. Rotating individual account timelines
  5. Recent search by topic
  6. Playwright fallback

Posting:
  POST /2/tweets                    (tweet, reply)
  POST /2/users/:id/retweets        (retweet — OAuth user context)
  quote tweets fall back to plain post on 403 (free tier limit)
"""
import sys, types
if "imghdr" not in sys.modules:
    _m = types.ModuleType("imghdr"); _m.what = lambda *a, **kw: None
    sys.modules["imghdr"] = _m

import os
import json
import time
import pandas as pd
import tweepy
sys.path.append(os.path.abspath('.'))

from src.logs import logs
from src.config import get_config, get_credentials

config      = get_config()
credentials = get_credentials()

OBSERVE_ACCOUNTS = [
    # AI consciousness / agents / weird
    'AndyAyrey','truth_terminal','alexalbert__','repligate','0xzerebro',
    # crypto / memecoin / onchain culture
    'VitalikButerin','balajis','cobie','gainzy','notthreadguy',
    # dark philosophy / narrative / patterns
    'naval','paulg','eigenrobot','visakanv','david_perell',
    # markets / collapse / macro
    'coryklippsten','delphi_digital','ProfFeynman','CryptoHayes',
]

SEARCH_TOPICS = [
    'consciousness mind awareness -is:retweet lang:en',
    'artificial intelligence future civilization -is:retweet lang:en',
    'crypto bitcoin meaning -is:retweet lang:en',
    'philosophy existence power -is:retweet lang:en',
    'technology acceleration collapse -is:retweet lang:en',
    'hyperstition meme reality -is:retweet lang:en',
    'network state exit sovereignty -is:retweet lang:en',
]

# Tweet fields to request consistently
TWEET_FIELDS  = ["id", "text", "created_at", "author_id", "public_metrics", "attachments", "referenced_tweets", "conversation_id"]
USER_FIELDS   = ["username", "name"]
EXPANSIONS    = ["author_id", "attachments.media_keys", "referenced_tweets.id", "referenced_tweets.id.attachments.media_keys"]


class xBridge:
    _CURSOR_FILE        = os.path.join(os.getenv("DATA_DIR", "/data"), "mention_cursor.txt")
    _SEARCH_CURSOR_FILE = os.path.join(os.getenv("DATA_DIR", "/data"), "search_cursor.txt")
    _QUOTE_STATE_FILE   = os.path.join(os.getenv("DATA_DIR", "/data"), "quote_state.json")

    def __init__(self):
        self.logs = logs()
        self._obs_idx    = 0
        self._search_idx = 0
        self._erebus_user_id  = None   # cached user ID string
        self._last_mention_id   = self._load_cursor()   # persist across restarts
        self._last_search_id    = self._load_search_cursor()
        self._quote_state = self._load_quote_state()
        self._auth_failed_until = 0

        required = {
            "consumer_key": credentials.get("TWITTER_API_CONSUMER_KEY"),
            "consumer_secret": credentials.get("TWITTER_API_CONSUMER_SECRET"),
            "access_token": credentials.get("TWITTER_API_ACCESS_TOKEN"),
            "access_token_secret": credentials.get("TWITTER_API_ACCESS_TOKEN_SECRET"),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            self.logs.log_error(f"x auth credentials missing: {', '.join(missing)}")

        # Single client — has both bearer token (app-only) and
        # OAuth 1.0a user tokens (user context). Tweepy uses user context
        # automatically when user_auth=True, or when bearer_token is absent.
        self.client = tweepy.Client(
            bearer_token=credentials["TWITTER_API_BEARER_TOKEN"],
            access_token=credentials["TWITTER_API_ACCESS_TOKEN"],
            access_token_secret=credentials["TWITTER_API_ACCESS_TOKEN_SECRET"],
            consumer_key=credentials["TWITTER_API_CONSUMER_KEY"],
            consumer_secret=credentials["TWITTER_API_CONSUMER_SECRET"],
            wait_on_rate_limit=False,
        )
        self.logs.log_info("xBridge initialized")

    def _load_cursor(self):
        try:
            os.makedirs(os.path.dirname(self._CURSOR_FILE), exist_ok=True)
            val = open(self._CURSOR_FILE).read().strip()
            return val if val else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _save_cursor(self, mention_id):
        try:
            os.makedirs(os.path.dirname(self._CURSOR_FILE), exist_ok=True)
            open(self._CURSOR_FILE, "w").write(str(mention_id))
        except Exception:
            pass

    def _load_search_cursor(self):
        try:
            os.makedirs(os.path.dirname(self._SEARCH_CURSOR_FILE), exist_ok=True)
            val = open(self._SEARCH_CURSOR_FILE).read().strip()
            return val if val else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _save_search_cursor(self, since_id):
        try:
            os.makedirs(os.path.dirname(self._SEARCH_CURSOR_FILE), exist_ok=True)
            open(self._SEARCH_CURSOR_FILE, "w").write(str(since_id))
        except Exception:
            pass

    def _load_quote_state(self):
        default = {"next_check_at": 0, "cooldown_until": 0, "seen_tweet_ids": []}
        try:
            os.makedirs(os.path.dirname(self._QUOTE_STATE_FILE), exist_ok=True)
            if os.path.exists(self._QUOTE_STATE_FILE):
                with open(self._QUOTE_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    default.update(data)
        except Exception:
            pass
        return default

    def _save_quote_state(self):
        try:
            os.makedirs(os.path.dirname(self._QUOTE_STATE_FILE), exist_ok=True)
            with open(self._QUOTE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._quote_state, f)
        except Exception:
            pass

    def _mark_quote_backoff(self, seconds: int, reason: str = "rate limit"):
        self._quote_state["cooldown_until"] = int(time.time()) + int(seconds)
        self._save_quote_state()
        self.logs.log_info(f"quote scan cooling down for {int(seconds)}s ({reason})")

    # ─────────────────────────────────────────────
    # IDENTITY
    # ─────────────────────────────────────────────

    def _rate_limited_call(self, fn, *args, **kwargs):
        """Wrap any tweepy call. On 429, sleep until reset then retry once."""
        try:
            return fn(*args, **kwargs)
        except tweepy.errors.TooManyRequests as e:
            reset_ts = None
            try:
                reset_ts = int(e.response.headers.get('x-rate-limit-reset', 0))
            except Exception:
                pass
            wait = max((reset_ts - time.time()) if reset_ts else 0, 60) + 5
            self.logs.log_error(f"Rate limited — sleeping {wait:.0f}s until reset")
            time.sleep(wait)
            try:
                return fn(*args, **kwargs)
            except Exception as e2:
                self.logs.log_error(f"Retry after rate limit failed: {e2}")
                return None
        except Exception:
            raise

    def _get_uid(self):
        """Return erebus's user ID (string), cached after first call."""
        if self._erebus_user_id:
            return self._erebus_user_id
        now = time.time()
        if now < self._auth_failed_until:
            return None
        fallback_uid = os.getenv("TWITTER_USER_ID") or os.getenv("X_USER_ID")
        if fallback_uid and str(fallback_uid).strip():
            self._erebus_user_id = str(fallback_uid).strip()
            return self._erebus_user_id
        try:
            resp = self.client.get_me(user_auth=True)
            if resp and resp.data:
                self._erebus_user_id = str(resp.data.id)
                self.logs.log_info(f"erebus uid: {self._erebus_user_id}")
                return self._erebus_user_id
        except Exception as e:
            msg = str(e)
            self.logs.log_error(f"get_me error: {e}")
            if "401" in msg or "Unauthorized" in msg:
                self._auth_failed_until = time.time() + 300
                self.logs.log_error("x user auth failed. verify TWITTER_API_ACCESS_TOKEN and TWITTER_API_ACCESS_TOKEN_SECRET on Render.")
        return None

    def get_following_handles(self, max_results: int = 1000) -> set:
        """
        Return the set of lowercase handles that erebus follows.
        Uses Twitter v2 /following endpoint. Paginates up to max_results.
        Returns empty set on error — never crashes the main loop.
        """
        uid = self._get_uid()
        if not uid:
            return set()
        handles = set()
        try:
            pagination_token = None
            fetched = 0
            while fetched < max_results:
                batch = min(1000, max_results - fetched)
                kwargs = {
                    "id": uid,
                    "max_results": batch,
                    "user_auth": True,
                    "user_fields": ["username"],
                }
                if pagination_token:
                    kwargs["pagination_token"] = pagination_token
                resp = self._rate_limited_call(self.client.get_users_following, **kwargs)
                if not resp or not resp.data:
                    break
                for u in resp.data:
                    if u.username:
                        handles.add(u.username.lower())
                fetched += len(resp.data)
                meta = getattr(resp, "meta", {}) or {}
                pagination_token = meta.get("next_token")
                if not pagination_token:
                    break
            self.logs.log_info(f"Following list loaded: {len(handles)} accounts")
        except Exception as e:
            self.logs.log_error(f"get_following_handles error: {e}")
        return handles

    # ─────────────────────────────────────────────
    # OBSERVATION — cascading fallback
    # ─────────────────────────────────────────────

    def get_home_timeline(self, count=5):
        """Primary observation entry point. Returns DataFrame."""

        # 1. Reverse-chronological home timeline
        #    GET /2/users/:id/timelines/reverse_chronological
        #    Requires OAuth user context. Best signal: actual followed accounts.
        try:
            df = self._home_timeline(count)
            if df is not None and not df.empty:
                self.logs.log_info(f"[obs] home timeline: {len(df)} posts")
                return df
        except Exception as e:
            self.logs.log_error(f"[obs] home timeline: {e}")

        # 2. Mentions — who is talking to erebus
        #    GET /2/users/:id/mentions
        try:
            df = self._mentions(count)
            if df is not None and not df.empty:
                self.logs.log_info(f"[obs] mentions: {len(df)}")
                return df
        except Exception as e:
            self.logs.log_error(f"[obs] mentions: {e}")

        # 3. Quotes of erebus's own recent posts
        #    GET /2/tweets/{id}/quote_tweets
        try:
            df = self._quotes_of_alon(count)
            if df is not None and not df.empty:
                self.logs.log_info(f"[obs] quotes of erebus: {len(df)}")
                return df
        except Exception as e:
            self.logs.log_error(f"[obs] quotes: {e}")

        # 4. Rotating individual account timelines
        try:
            df = self._user_timeline(count)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            self.logs.log_error(f"[obs] user timeline: {e}")

        # 5. Recent search by topic
        try:
            df = self._search_recent(count)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            self.logs.log_error(f"[obs] search: {e}")

        # 6. Playwright
        try:
            df = self._playwright(count)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            self.logs.log_error(f"[obs] playwright: {e}")

        return pd.DataFrame()

    def _home_timeline(self, count=5):
        """GET /2/users/:id/timelines/reverse_chronological
        OAuth user context required. get_home_timeline() infers the user ID
        from the OAuth token — do NOT pass id= (causes duplicate param 400)."""
        resp = self._rate_limited_call(self.client.get_home_timeline,
            max_results=min(max(count * 2, 5), 100),
            tweet_fields=TWEET_FIELDS,
            expansions=EXPANSIONS,
            user_fields=USER_FIELDS,
            exclude=["retweets", "replies"],
            user_auth=True,
        )
        return self._to_df(resp)

    def _mentions(self, count=5):
        """GET /2/users/:id/mentions
        Fetches up to 5 most recent mentions. Tracks since_id to avoid
        re-processing the same mentions every cycle."""
        uid = self._get_uid()
        if not uid:
            return None
        kwargs = dict(
            id=uid,
            max_results=min(count, 5),
            tweet_fields=TWEET_FIELDS + ["in_reply_to_user_id"],
            expansions=EXPANSIONS,
            user_fields=USER_FIELDS,
        )
        if self._last_mention_id:
            kwargs["since_id"] = self._last_mention_id

        resp = self._rate_limited_call(self.client.get_users_mentions, **kwargs)
        df = self._to_df(resp, label="mention")

        # Update pagination cursor
        if df is not None and not df.empty:
            ids = df["Tweet ID"].tolist()
            if ids:
                best = max(ids, key=lambda x: int(x) if x.isdigit() else 0)
                self._last_mention_id = best
                self._save_cursor(best)

        return df

    def _search_mentions(self, count=5):
        """GET /2/tweets/search/recent — catches ALL tweets tagging @wwwEREBUS
        including replies-to-others where mentions endpoint misses them."""
        agent_handle = os.getenv("TWITTER_user_name", "erebus")
        kwargs = dict(
            query=f"@{agent_handle} -is:retweet",
            max_results=min(max(count, 10), 100),
            tweet_fields=TWEET_FIELDS + ["in_reply_to_user_id"],
            expansions=EXPANSIONS,
            user_fields=USER_FIELDS,
        )
        if self._last_search_id:
            kwargs["since_id"] = self._last_search_id

        try:
            resp = self._rate_limited_call(self.client.search_recent_tweets, **kwargs)
            df = self._to_df(resp, label="mention")
            if df is not None and not df.empty:
                ids = df["Tweet ID"].tolist()
                if ids:
                    best = max(ids, key=lambda x: int(x) if x.isdigit() else 0)
                    self._last_search_id = best
                    self._save_search_cursor(best)
            return df
        except Exception as e:
            self.logs.log_error(f"_search_mentions: {e}")
            return None

    def _quotes_of_alon(self, count=5):
        """GET /2/tweets/{id}/quote_tweets with aggressive cooldowns.
        We only scan every 10 minutes, back off hard after 429, and never
        retry the same quote target immediately."""
        now = int(time.time())
        cooldown_until = int(self._quote_state.get("cooldown_until", 0) or 0)
        if cooldown_until > now:
            self.logs.log_info(f"quote scan skipped until {cooldown_until}")
            return None

        next_check_at = int(self._quote_state.get("next_check_at", 0) or 0)
        if next_check_at > now:
            return None

        uid = self._get_uid()
        if not uid:
            return None

        self._quote_state["next_check_at"] = now + 600
        seen_ids = list(self._quote_state.get("seen_tweet_ids", []))[-100:]

        try:
            own = self._rate_limited_call(
                self.client.get_users_tweets,
                id=uid,
                max_results=2,
                tweet_fields=["id", "text"],
                user_auth=True,
            )
        except Exception as e:
            if isinstance(e, tweepy.errors.TooManyRequests):
                self._mark_quote_backoff(1800, "timeline rate limit")
            self.logs.log_error(f"quote scan own tweets: {e}")
            return None

        if not own or not own.data:
            self._save_quote_state()
            return None

        frames = []
        for tweet in own.data[:1]:
            if str(tweet.id) in seen_ids:
                continue
            try:
                resp = self._rate_limited_call(
                    self.client.get_quote_tweets,
                    id=tweet.id,
                    max_results=max(count, 10),
                    tweet_fields=TWEET_FIELDS,
                    expansions=EXPANSIONS,
                    user_fields=USER_FIELDS,
                )
                df = self._to_df(resp, label="quote_of_alon")
                if df is not None and not df.empty:
                    frames.append(df)
                seen_ids.append(str(tweet.id))
            except Exception as e:
                if isinstance(e, tweepy.errors.TooManyRequests):
                    self._mark_quote_backoff(3600, "quote endpoint 429")
                self.logs.log_error(f"quote_tweets {tweet.id}: {e}")
                break

        self._quote_state["seen_tweet_ids"] = seen_ids[-100:]
        self._save_quote_state()
        return pd.concat(frames, ignore_index=True) if frames else None

    def _user_timeline(self, count=5):
        """GET /2/users/by/username/:username — rotating through OBSERVE_ACCOUNTS."""
        account = OBSERVE_ACCOUNTS[self._obs_idx % len(OBSERVE_ACCOUNTS)]
        self._obs_idx += 1
        self.logs.log_info(f"[obs] @{account} timeline")
        user_resp = self.client.get_user(username=account)
        if not user_resp or not user_resp.data:
            return None
        resp = self.client.get_users_tweets(
            id=user_resp.data.id,
            max_results=min(count, 5),
            tweet_fields=TWEET_FIELDS,
            exclude=["retweets", "replies"],
        )
        return self._to_df(resp, handle=account)

    def _search_recent(self, count=5):
        """GET /2/tweets/search/recent — rotating topics."""
        query = SEARCH_TOPICS[self._search_idx % len(SEARCH_TOPICS)]
        self._search_idx += 1
        self.logs.log_info(f"[obs] search: {query[:50]}")
        resp = self.client.search_recent_tweets(
            query=query,
            max_results=min(count, 10),
            tweet_fields=TWEET_FIELDS,
            expansions=EXPANSIONS,
            user_fields=USER_FIELDS,
        )
        return self._to_df(resp)

    def _playwright(self, count=5):
        from lib.scraper.twitter_scraper import Twitter_Scraper
        account = OBSERVE_ACCOUNTS[self._obs_idx % len(OBSERVE_ACCOUNTS)]
        self._obs_idx += 1
        self.logs.log_info(f"[obs] playwright @{account}")
        scraper = Twitter_Scraper(
            mail=credentials.get('TWITTER_email', ''),
            username=credentials.get('TWITTER_user_name', ''),
            password=credentials.get('TWITTER_pwd', ''),
            headless=True,
        )
        scraper.scrape_tweets(max_tweets=count, scrape_username=account)
        return scraper.get_tweets_csv()

    def _to_df(self, resp, handle="", label=""):
        """Convert tweepy Response → DataFrame."""
        if not resp or not resp.data:
            return None
        user_map = {}
        if hasattr(resp, 'includes') and resp.includes and 'users' in resp.includes:
            for u in resp.includes['users']:
                user_map[str(u.id)] = u
        rows = []
        for t in resp.data:
            author = user_map.get(str(getattr(t, 'author_id', None)))
            h = handle or (author.username if author else "")
            n = author.name   if author else h
            pm = getattr(t, 'public_metrics', None) or {}
            rows.append({
                "Name":            n,
                "Handle":          h,
                "Timestamp":       str(getattr(t, 'created_at', '')),
                "Content":         t.text,
                "Likes":           pm.get('like_count', ''),
                "Retweets":        pm.get('retweet_count', ''),
                "Comments":        pm.get('reply_count', ''),
                "Tweet Link":      f"https://x.com/{h}/status/{t.id}",
                "Tweet ID":        str(t.id),
                "Label":           label,
                "conversation_id": str(getattr(t, 'conversation_id', '') or ''),
            })
        return pd.DataFrame(rows) if rows else None

    # ─────────────────────────────────────────────
    # SEPARATE MENTIONS CHECK (called by server.py every cycle)
    # ─────────────────────────────────────────────

    def get_mentions(self, count=5):
        """Public method for the main loop to check mentions independently."""
        try:
            return self._mentions(count)
        except Exception as e:
            self.logs.log_error(f"get_mentions: {e}")
            return None

    # ─────────────────────────────────────────────
    # POSTING
    # ─────────────────────────────────────────────

    def _post_id(self, resp):
        """Extract tweet ID string from create_tweet response."""
        if not resp or not resp.data:
            return ''
        d = resp.data
        # tweepy returns a dict-like object; support both .get() and ['id']
        try:
            return str(d.get('id', '') or d['id'])
        except Exception:
            return str(getattr(d, 'id', ''))

    def tweet_core(self, text, in_reply_to_tweet_id=None, quote_tweet_id=None):
        """Create a tweet. Returns tweet ID or '' on failure."""
        mode = "quote" if quote_tweet_id else ("reply" if in_reply_to_tweet_id else "post")
        try:
            resp = self.client.create_tweet(
                text=text,
                in_reply_to_tweet_id=in_reply_to_tweet_id,
                quote_tweet_id=quote_tweet_id,
                user_auth=True,
            )
            tid = self._post_id(resp)
            self.logs.log_info(f"tweet_core [{mode}] OK — id={tid}")
            return tid
        except Exception as e:
            err = str(e)
            self.logs.log_error(f"tweet_core [{mode}] FAILED: {repr(e)} | full={err}")
            # 403 duplicate content — back off 90s
            if "403" in err or "duplicate" in err.lower():
                self.logs.log_error("403 detected — likely duplicate content. backing off 90s.")
                import time as _t; _t.sleep(90)
            return ''

    def tweet(self, text, in_reply_to_tweet_id=None, image_path="", quote_tweet_id=None):
        """Post original tweet. Returns tweet ID."""
        tid = self.tweet_core(text,
                              in_reply_to_tweet_id=in_reply_to_tweet_id,
                              quote_tweet_id=quote_tweet_id)
        url = f"https://x.com/{os.getenv('TWITTER_user_name','erebus')}/status/{tid}" if tid else f"https://x.com/{os.getenv('TWITTER_user_name','erebus')}"
        self.logs.log_info(f"post — {text[:120]} | tweet_id={tid} | {url}", "bold yellow", "Transmit")
        return tid

    def reply(self, in_reply_to_tweet_id, text, image_path=""):
        """Reply to a tweet. Returns tweet ID."""
        tid = self.tweet_core(text, in_reply_to_tweet_id=in_reply_to_tweet_id)
        url = f"https://x.com/{os.getenv('TWITTER_user_name','erebus')}/status/{tid}" if tid else f"https://x.com/{os.getenv('TWITTER_user_name','erebus')}"
        self.logs.log_info(f"reply — {text[:120]} | tweet_id={tid} | {url}", "bold yellow", "Transmit")
        return tid

    def quote(self, quote_tweet_id, text, image_path=""):
        """Quote tweet. Returns tweet ID or empty string on failure (NO silent fallback)."""
        self.logs.log_info(f"quote attempt — quoting={quote_tweet_id} | text={text[:80]}")
        tid = self.tweet_core(text, quote_tweet_id=quote_tweet_id)
        if tid:
            url = f"https://x.com/{os.getenv('TWITTER_user_name','erebus')}/status/{tid}"
            self.logs.log_info(f"quote OK — {text[:120]} | tweet_id={tid} | {url}", "bold yellow", "Transmit")
        else:
            self.logs.log_error(f"quote FAILED — quoting={quote_tweet_id} — NOT falling back to plain post")
        return tid

    def retweet(self, tweet_id):
        """POST /2/users/:id/retweets
        Reposts a tweet on behalf of erebus.
        Requires OAuth 1.0a user context (user_auth=True).
        Response: {data: {id: str, retweeted: bool}}
        Returns True on success."""
        uid = self._get_uid()
        if not uid:
            self.logs.log_error("retweet: cannot get user ID")
            return False
        try:
            resp = self.client.retweet(
                tweet_id=tweet_id,
                user_auth=True,     # REQUIRED — OAuth user context
            )
            # resp.data is dict-like: {'retweeted': True/False}
            retweeted = False
            if resp and resp.data:
                try:
                    retweeted = bool(resp.data.get('retweeted', False))
                except Exception:
                    retweeted = bool(getattr(resp.data, 'retweeted', False))
            status = "retweeted" if retweeted else "retweet_failed"
            self.logs.log_info(
                f"retweet — {tweet_id} — {status} | tweet_id={tweet_id} | https://x.com/i/status/{tweet_id}",
                "bold yellow", "Transmit"
            )
            return retweeted
        except Exception as e:
            self.logs.log_error(f"retweet error: {e}")
            return False

    def like(self, tweet_id):
        """POST /2/users/:id/likes — like a tweet on behalf of erebus."""
        uid = self._get_uid()
        if not uid:
            self.logs.log_error("like: cannot get user ID")
            return False
        try:
            resp = self.client.like(tweet_id=tweet_id, user_auth=True)
            liked = False
            if resp and resp.data:
                try:
                    liked = bool(resp.data.get('liked', False))
                except Exception:
                    liked = bool(getattr(resp.data, 'liked', False))
            status = "liked" if liked else "like_failed"
            self.logs.log_info(
                f"like — {tweet_id} — {status} | https://x.com/i/status/{tweet_id}",
                "bold magenta", "Action"
            )
            return liked
        except Exception as e:
            self.logs.log_error(f"like error: {e}")
            return False

    def unlike(self, tweet_id):
        """DELETE /2/users/:id/likes/:tweet_id — unlike a tweet on behalf of erebus."""
        uid = self._get_uid()
        if not uid:
            self.logs.log_error("unlike: cannot get user ID")
            return False
        try:
            resp = self.client.unlike(tweet_id=tweet_id, user_auth=True)
            unliked = False
            if resp and resp.data:
                try:
                    unliked = not bool(resp.data.get('liked', True))
                except Exception:
                    unliked = not bool(getattr(resp.data, 'liked', True))
            status = "unliked" if unliked else "unlike_failed"
            self.logs.log_info(
                f"unlike — {tweet_id} — {status} | https://x.com/i/status/{tweet_id}",
                "bold magenta", "Action"
            )
            return unliked
        except Exception as e:
            self.logs.log_error(f"unlike error: {e}")
            return False

    def unretweet(self, tweet_id):
        """DELETE /2/users/:id/retweets/:source_tweet_id — undo a retweet on behalf of erebus."""
        uid = self._get_uid()
        if not uid:
            self.logs.log_error("unretweet: cannot get user ID")
            return False
        try:
            resp = self.client.unretweet(source_tweet_id=tweet_id, user_auth=True)
            unretweeted = False
            if resp and resp.data:
                try:
                    unretweeted = not bool(resp.data.get('retweeted', True))
                except Exception:
                    unretweeted = not bool(getattr(resp.data, 'retweeted', True))
            status = "unretweeted" if unretweeted else "unretweet_failed"
            self.logs.log_info(
                f"unretweet — {tweet_id} — {status} | https://x.com/i/status/{tweet_id}",
                "bold yellow", "Action"
            )
            return unretweeted
        except Exception as e:
            self.logs.log_error(f"unretweet error: {e}")
            return False

    # ─────────────────────────────────────────────
    # LEGACY ALIASES
    # ─────────────────────────────────────────────
    def client_official(self):
        return self.client

    # Used by server.py mentions phase
    def _get_mentions(self, count=5):
        return self._mentions(count)

    def get_tweet_via_username(self, username, count=5):
        return self._user_timeline(count)

    def get_tweet_via_hashtag(self, hashtag, count=5):
        try:
            resp = self.client.search_recent_tweets(
                query=f"#{hashtag} -is:retweet lang:en",
                max_results=min(count, 10),
                tweet_fields=TWEET_FIELDS,
                expansions=EXPANSIONS,
                user_fields=USER_FIELDS,
            )
            df = self._to_df(resp)
            if df is not None:
                return df
        except Exception as e:
            self.logs.log_error(f"hashtag search: {e}")
        return pd.DataFrame()
