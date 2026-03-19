"""
twAuto — Twitter actions via Playwright.
Fresh browser context per action to avoid asyncio/sync conflicts.
"""
import os, sys, time, pickle

class twAuto:
    def __init__(self, username="", email="", password="", phone="",
                 headless=True, debugMode=False, chromeDriverMode="auto",
                 driverPath="", pathType="testId", createCookies=True,
                 cookies_path="/data/cookies/cookies.pkl", **kwargs):
        self.username = username
        self.email = email
        self.password = password
        self.phone = phone
        self.headless = headless
        self.createCookies = createCookies
        self.cookies_path = cookies_path
        self._cookies = None
        self.logged_in = False
        self._load_cookies()

    def _load_cookies(self):
        if self.createCookies and os.path.exists(self.cookies_path):
            try:
                with open(self.cookies_path,"rb") as f:
                    self._cookies = pickle.load(f)
                print("twAuto: cookies loaded")
            except Exception as e:
                print(f"twAuto: cookie load failed: {e}")

    def _save_cookies(self, ctx):
        if self.createCookies:
            try:
                os.makedirs(os.path.dirname(self.cookies_path), exist_ok=True)
                with open(self.cookies_path,"wb") as f:
                    pickle.dump(ctx.cookies(), f)
                print("twAuto: cookies saved")
            except Exception as e:
                print(f"twAuto: cookie save failed: {e}")

    def _args(self):
        return ["--no-sandbox","--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"]

    def _make_context(self, browser):
        ctx = browser.new_context(
            viewport={"width":1280,"height":800},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True,
        )
        if self._cookies:
            try: ctx.add_cookies(self._cookies)
            except Exception: pass
        return ctx

    def _open_logged_in(self, pw):
        """Return (browser, page) with best-effort login."""
        browser = pw.chromium.launch(headless=self.headless, args=self._args())
        ctx = self._make_context(browser)
        ctx.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4}", lambda r: r.abort())
        page = ctx.new_page()

        # Try cookies first
        if self._cookies:
            page.goto("https://twitter.com/home",
                      wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            if "/home" in page.url:
                self.logged_in = True
                print("twAuto: session restored via cookies")
                return browser, page, ctx

        # Fresh login
        print("twAuto: performing fresh login...")
        page.goto("https://twitter.com/i/flow/login",
                  wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        def fill(sels, val):
            for sel in sels:
                try:
                    el = page.wait_for_selector(sel, timeout=10000)
                    if el and el.is_visible():
                        el.click(); time.sleep(.3); el.fill(val); return True
                except Exception: continue
            return False

        fill(["input[autocomplete='username']","input[name='text']"], self.username)
        page.keyboard.press("Enter"); time.sleep(3)

        for _ in range(3):
            if "/home" in page.url: break
            pw_el = (page.query_selector("input[name='password']") or
                     page.query_selector("input[type='password']"))
            if pw_el and pw_el.is_visible():
                pw_el.click(); time.sleep(.3); pw_el.fill(self.password)
                page.keyboard.press("Enter"); time.sleep(5)
                break
            v_el = page.query_selector("input[name='text']")
            if v_el and v_el.is_visible():
                v_el.click(); v_el.press("Control+a"); v_el.press("Delete")
                v_el.type(self.phone or self.username, delay=50)
                page.keyboard.press("Enter"); time.sleep(3)

        if "/home" in page.url:
            self.logged_in = True
            self._save_cookies(ctx)
            print("twAuto: login successful")
        else:
            print(f"twAuto: login uncertain, URL={page.url}")

        return browser, page, ctx

    def start(self):
        pass  # no-op — browser opened per action

    def login(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, page, ctx = self._open_logged_in(pw)
            browser.close()

    def like(self, url):
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as pw:
                browser, page, ctx = self._open_logged_in(pw)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)
                    page.click("[data-testid='like']")
                    time.sleep(2)
                    print(f"twAuto: liked {url}")
                finally:
                    browser.close()
        except Exception as e:
            print(f"twAuto like error: {e}")

    def close(self):
        pass  # no-op
