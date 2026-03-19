"""
Twitter scraper — Playwright sync API, fresh browser per scrape call.
This avoids asyncio conflicts when running inside a threading.Thread.
"""
import os, time
import pandas as pd

TWITTER_LOGIN_URL = "https://twitter.com/i/flow/login"
TWITTER_HOME_URL  = "https://twitter.com/home"

# Public accounts scraped without login as fallback
PUBLIC_ACCOUNTS = [
    'elonmusk','VitalikButerin','naval','pmarca','balajis',
    'saylor','aantonop','coinbase','BitcoinMagazine','cz_binance',
    'sama','karpathy','ylecun','GaryMarcus','fchollet',
    'BrendanEich','dhh','ID_AA_Carmack','paulg','levelsio',
]

class Twitter_Scraper:
    def __init__(self, mail='', username='', password='', phone='',
                 max_tweets=10, headless=True, **kwargs):
        self.mail     = mail
        self.username = username
        self.password = password
        self.phone    = phone
        self.max_tweets = max_tweets
        self.headless = headless
        self.login_bool = False
        self.data = []
        self._pub_idx = 0
        print("Initializing Twitter Scraper (Playwright)...")

    # ── public API ──────────────────────────────────────
    def login(self):
        """Attempt login; sets self.login_bool."""
        print("Logging in to Twitter...")
        ok = self._do_login()
        self.login_bool = ok

    def scrape_tweets(self, max_tweets=None, scrape_username=None,
                      scrape_hashtag=None, **kwargs):
        count = max_tweets or self.max_tweets
        self.data = []

        # Try logged-in home timeline first; fall back to public
        if scrape_username:
            self.data = self._scrape_url(
                f"https://twitter.com/{scrape_username}", count)
        elif scrape_hashtag:
            self.data = self._scrape_url(
                f"https://twitter.com/hashtag/{scrape_hashtag}", count)
        else:
            if self.login_bool:
                self.data = self._scrape_url(TWITTER_HOME_URL, count)
            if not self.data:
                self.data = self._scrape_public(count)

        print(f"Scraped {len(self.data)} tweets")

    def get_tweets_csv(self):
        return pd.DataFrame(self.data) if self.data else pd.DataFrame()

    # ── internals ──────────────────────────────────────
    def _browser_args(self):
        return ["--no-sandbox","--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security"]

    def _new_context(self, browser):
        return browser.new_context(
            viewport={"width":1280,"height":800},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True,
        )

    def _block_media(self, page):
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,webp}",
                   lambda r: r.abort())

    def _do_login(self):
        """Fresh browser session just for login — saves cookies to class."""
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=self.headless, args=self._browser_args())
                ctx = self._new_context(browser)
                page = ctx.new_page()
                self._block_media(page)

                page.goto(TWITTER_LOGIN_URL,
                          wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)

                # Step 1 — username
                self._fill(page,[
                    "input[autocomplete='username']",
                    "input[name='text']","input[type='text']",
                ], self.username, "username")
                page.keyboard.press("Enter"); time.sleep(3)

                # Up to 4 steps — handle verification / password
                for attempt in range(4):
                    url = page.url
                    print(f"Login step {attempt+1}: {url}")

                    if "/home" in url:
                        self._cookies = ctx.cookies()
                        print("Login Successful"); return True

                    # Password
                    pw_el = (page.query_selector("input[name='password']") or
                             page.query_selector("input[type='password']"))
                    if pw_el and pw_el.is_visible():
                        pw_el.click(); time.sleep(.3)
                        pw_el.fill(self.password)
                        page.keyboard.press("Enter"); time.sleep(6)
                        if "/home" in page.url:
                            self._cookies = ctx.cookies()
                            print("Login Successful"); return True
                        # Try navigating directly
                        page.goto(TWITTER_HOME_URL,
                                  wait_until="domcontentloaded",timeout=30000)
                        time.sleep(3)
                        if "/home" in page.url:
                            self._cookies = ctx.cookies()
                            print("Login Successful (nav)"); return True
                        return False

                    # Verification field — rotate phone/username/email
                    v_el = (page.query_selector(
                                "input[data-testid='ocfEnterTextTextInput']") or
                            page.query_selector("input[name='text']"))
                    if v_el and v_el.is_visible():
                        candidates=[v for v in
                            [self.phone,self.username,self.mail] if v]
                        val = candidates[attempt % len(candidates)]
                        print(f"Login: verify attempt {attempt+1}: {val}")
                        v_el.click(); time.sleep(.2)
                        v_el.press("Control+a"); v_el.press("Delete")
                        v_el.type(val, delay=50)
                        page.keyboard.press("Enter"); time.sleep(3)
                        continue

                    time.sleep(2)

                print(f"Login failed — final URL: {page.url}"); return False
        except Exception as e:
            print(f"Login error: {e}"); return False

    def _scrape_url(self, url, count):
        """Scrape tweets from a URL. Uses fresh browser each call."""
        from playwright.sync_api import sync_playwright
        data = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=self.headless, args=self._browser_args())
                ctx = self._new_context(browser)
                # Restore login cookies if available
                if hasattr(self,'_cookies') and self._cookies:
                    ctx.add_cookies(self._cookies)
                page = ctx.new_page()
                self._block_media(page)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(4)

                seen = set()
                attempts = 0
                while len(data) < count and attempts < 15:
                    for art in page.query_selector_all(
                            "article[data-testid='tweet']"):
                        if len(data) >= count: break
                        t = self._parse(art)
                        if t and t["Tweet ID"] not in seen:
                            seen.add(t["Tweet ID"]); data.append(t)
                    if len(data) < count:
                        page.keyboard.press("End"); time.sleep(2)
                    attempts += 1
                browser.close()
        except Exception as e:
            print(f"Scrape error ({url}): {e}")
        return data

    def _scrape_public(self, count):
        """Scrape public accounts without login, rotating each call."""
        account = PUBLIC_ACCOUNTS[self._pub_idx % len(PUBLIC_ACCOUNTS)]
        self._pub_idx += 1
        print(f"Observation: public scrape @{account}")
        return self._scrape_url(
            f"https://twitter.com/{account}", count)

    def _fill(self, page, selectors, value, name, timeout=12000):
        for sel in selectors:
            try:
                el = page.wait_for_selector(sel, timeout=timeout)
                if el and el.is_visible():
                    el.click(); time.sleep(.3); el.fill(value)
                    print(f"Login: filled {name} via {sel}"); return el
            except Exception: continue
        print(f"Login: could not find {name}")
        return None

    def _parse(self, article):
        def txt(sel):
            try:
                el = article.query_selector(sel)
                return el.inner_text().strip() if el else ""
            except: return ""

        content = txt("[data-testid='tweetText']")
        if not content: return None

        name, handle = "", ""
        try:
            ne = article.query_selector("div[data-testid='User-Name']")
            if ne:
                spans = [s.inner_text().strip()
                         for s in ne.query_selector_all("span")
                         if s.inner_text().strip()]
                name = spans[0] if spans else ""
                hs = [s for s in spans if s.startswith("@")]
                handle = hs[0].lstrip("@") if hs else ""
        except: pass

        tweet_id = ""
        try:
            lnk = article.query_selector("a[href*='/status/']")
            if lnk:
                href = lnk.get_attribute("href")
                tweet_id = href.split("/status/")[-1].split("/")[0].split("?")[0]
        except: pass

        ts = ""
        try:
            te = article.query_selector("time")
            ts = te.get_attribute("datetime") if te else ""
        except: pass

        return {
            "Name":name,"Handle":handle,"Timestamp":ts,
            "Content":content,
            "Comments":txt("[data-testid='reply'] span"),
            "Retweets":txt("[data-testid='retweet'] span"),
            "Likes":txt("[data-testid='like'] span"),
            "Tweet Link":f"https://twitter.com/{handle}/status/{tweet_id}",
            "Tweet ID":tweet_id,
        }
