import os, sys, time
sys.path.append(os.path.abspath('.'))
from dotenv import load_dotenv
from src.config import get_config, get_credentials
config = get_config()
credentials = get_credentials()
from src.xBridge import xBridge

class observationX:
    def __init__(self):
        load_dotenv()
        self.config = config
        self.xBridge_instance = xBridge()

    def get(self):
        return self.get_home_timeline()

    def get_home_timeline(self, count=5):
        # xBridge calls scraper.scrape_tweets which now opens fresh browser per call
        try:
            df = self.xBridge_instance.get_home_timeline(count)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"Home timeline error: {e}")
        # Fallback: public scrape via scraper directly
        try:
            scraper = self.xBridge_instance.client_selenium_read
            if scraper:
                scraper.scrape_tweets(max_tweets=count)
                return scraper.get_tweets_csv()
        except Exception as e:
            print(f"Public scrape error: {e}")
        import pandas as pd
        return pd.DataFrame()

    def get_tweet_via_username(self, username, count=5):
        return self.xBridge_instance.get_tweet_via_username(username, count)

    def get_tweet_via_hashtag(self, hashtag, count=5):
        return self.xBridge_instance.get_tweet_via_hashtag(hashtag, count)
