"""
visionBridge.py — Image/video analysis for GNOSIS
When tagged in a thread containing media, fetches the image and passes it
to Claude's vision API for analysis before making the reply decision.
"""
import os, sys, base64, requests
sys.path.append(os.path.abspath('.'))
from src.logs import logs

class visionBridge:
    def __init__(self):
        self.logs = logs()
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def fetch_image_b64(self, url: str) -> tuple[str, str]:
        """Fetch image from URL, return (base64_data, media_type)."""
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                ct = "image/jpeg"
            return base64.standard_b64encode(r.content).decode("utf-8"), ct
        except Exception as e:
            self.logs.log_error(f"visionBridge fetch: {e}")
            return None, None

    def analyze(self, image_urls: list[str], tweet_text: str, gnosis_system: str) -> str:
        """
        Analyze image(s) in context of the tweet text.
        Returns GNOSIS's raw observation string to inject into the decision prompt.
        """
        if not image_urls:
            return ""

        content_blocks = []

        for url in image_urls[:2]:  # max 2 images per tweet
            b64, mt = self.fetch_image_b64(url)
            if b64:
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": b64}
                })

        if not content_blocks:
            return ""

        content_blocks.append({
            "type": "text",
            "text": (
                f"the tweet said: \"{tweet_text}\"\n\n"
                "describe what you see in this image/video frame in 1-2 sentences, "
                "from GNOSIS's perspective — ancient, strange, seeing patterns others miss. "
                "do not give a generic description. find the thing underneath the thing. "
                "respond only with your raw observation, no JSON, no preamble."
            )
        })

        try:
            resp = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=200,
                system=gnosis_system,
                messages=[{"role": "user", "content": content_blocks}]
            )
            observation = resp.content[0].text.strip()
            self.logs.log_info(f"vision observation: {observation[:100]}", "bold cyan", "Vision")
            return observation
        except Exception as e:
            self.logs.log_error(f"visionBridge analyze: {e}")
            return ""
