import os, sys
import anthropic
sys.path.append(os.path.abspath('.'))

from interface.aiBridgeInterface import aiBridgeInterface
from src.config import get_config

config = get_config()

class claude_ai(aiBridgeInterface):
    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
        self.client     = anthropic.Anthropic(api_key=api_key)
        self.model      = config.get('llm_settings', {}).get('claude', {}).get('model', 'claude-sonnet-4-6')
        self.max_tokens = config.get('llm_settings', {}).get('claude', {}).get('max_tokens', 1000)

    def call_llm(self, prompt_system: str, prompt_user: str, response_format='json') -> str:
        message = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=prompt_system,
            messages=[
                {"role": "user", "content": prompt_user}
            ]
        )
        return message.content[0].text
