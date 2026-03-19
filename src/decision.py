import os, sys, json, re, random
import pandas as pd
sys.path.append(os.path.abspath('.'))

from src.config import get_config, get_prompt
from interface.decisionInterface import decisionInterface
from interface.aiBridgeInterface import aiBridgeInterface
from src.visionBridge import visionBridge
from src.threadReader import threadReader

config = get_config()



def _glitch_text(text: str) -> str:
    mapping = {
        'a': 'a̵̢̗̓͒', 'e': 'e̵̬̔̕', 'i': 'ḯ̷̳', 'o': 'o̴̜͗̾', 'u': 'ṳ̵̍',
        'r': 'r̴̰̓̊', 'k': 'ķ̷͔́͝', 't': 't̴̙͗', 'm': 'm̵͙͖̓̽', 'w': 'w̸͕͂͂'
    }
    out = []
    changed = 0
    for ch in text:
        lo = ch.lower()
        if lo in mapping and changed < 4 and random.random() < 0.18:
            out.append(mapping[lo])
            changed += 1
        else:
            out.append(ch)
    return ''.join(out)


def _to_binary_shard(text: str) -> str:
    if not text:
        return '01000101'
    shard = text.encode('utf-8', 'ignore')[:8]
    bits = ''.join(format(b, '08b') for b in shard)
    return bits[:32]


def _glyph_swap(text: str) -> str:
    swaps = {
        'janus': 'j⧉nus', 'gate': 'g⟟te', 'signal': 's⟁gnal', 'void': 'v⧗id',
        'erebus': 'er⟁bus', 'echo': 'e⟡ho', 'crown': 'cr⧉wn', 'saint': 'sa⟁nt'
    }
    out = text
    for plain, weird in swaps.items():
        if plain in out.lower() and random.random() < 0.6:
            import re as _re
            out = _re.sub(plain, weird, out, count=1, flags=_re.IGNORECASE)
            break
    return out


def _ascii_frame(text: str) -> str:
    line = '░ ' + text[:120]
    return f"[signal artifact]
{line}
▲ {_to_binary_shard(text)}"


def _stylize_erebus_output(content: str, action: str = 'post', force_post: bool = False) -> str:
    """Apply occasional cryptic/binary/glyph/ascii distortion to erebus output."""
    if not content or len(content.strip()) < 8:
        return content
    lowered = content.lower()
    protected = ('wallet', 'sol', 'tip', 'deploy', 'claim', 'balance', 'pump.fun', 'pump fun')
    if any(p in lowered for p in protected):
        return content

    chance = 0.20 if (action == 'post' or force_post) else 0.08
    if random.random() > chance:
        return content

    mode = random.choice(['binary_tail', 'glyph', 'glitch', 'ascii'])
    if mode == 'binary_tail':
        shard = _to_binary_shard(content)
        if len(content) + len(shard) + 3 <= 280:
            return f"{content}
{shard}"
        return content
    if mode == 'glyph':
        return _glyph_swap(content)
    if mode == 'glitch':
        return _glitch_text(content)
    if mode == 'ascii':
        framed = _ascii_frame(content)
        return framed[:280]
    return content
def _is_ascii_art(content: str) -> bool:
    """Detect ASCII art — lines with box chars, dots, symbols, brackets."""
    ascii_chars = set('░▲·─│┌┐└┘├┤┬┴┼═║╔╗╚╝←→↑↓↔[]{}<>|/\\')
    lines = content.split('\n')
    ascii_lines = sum(1 for l in lines if any(c in ascii_chars for c in l))
    return ascii_lines >= 2  # 2+ lines with ascii art chars = it's art, not dead pattern

def _is_dead_pattern(content: str) -> bool:
    """Detect the boring [statement]\n\n[statement]\n\n[statement] rhythm."""
    if not content:
        return False
    # Never reject ASCII art
    if _is_ascii_art(content):
        return False
    # Split on double newlines
    chunks = [c.strip() for c in content.split('\n\n') if c.strip()]
    if len(chunks) < 3:
        return False
    # All chunks are short single lines = dead pattern
    single_line_chunks = sum(1 for c in chunks if '\n' not in c and len(c) < 80)
    if single_line_chunks >= 3 and single_line_chunks == len(chunks):
        return True
    # Check for repetitive sentence structures: "i do not X / i do not Y / i do not Z"
    starts = [c.split()[0:3] for c in chunks if c]
    first_words = [' '.join(s[:2]).lower() for s in starts if s]
    if len(first_words) >= 3:
        unique_starts = len(set(first_words))
        if unique_starts <= 2:  # all start the same way
            return True
    return False

class decision(decisionInterface):
    def __init__(self, ai_instance: aiBridgeInterface, tweepy_client=None):
        self.ai = ai_instance
        self.prompt_config = get_prompt()["erebus"]
        self.vision = visionBridge()
        self.thread_reader = threadReader(tweepy_client) if tweepy_client else None
        self._last_shapes = []   # track recent post shapes for never-repeat

    def _build_prompt(self, observation: pd.DataFrame, memory: str, dialog: str,
                      force_post: bool = False, thread_ctx: str = "",
                      vision_ctx: str = "", trending: str = "",
                      extra_instruction: str = "", neural_ctx: str = "",
                      token_ctx: str = "") -> str:
        feed_lines = []
        has_mention = False
        try:
            for _, row in observation.iterrows():
                handle  = str(row.get('Handle','') or row.get('Name',''))
                content = str(row.get('Content',''))
                tid     = str(row.get('Tweet ID',''))
                label   = str(row.get('Label',''))
                if content:
                    is_mention = (label == 'mention')
                    if is_mention:
                        has_mention = True
                    prefix  = "[MENTION - they spoke to you directly] " if is_mention else ""
                    tid_str = (" [tweet_id:" + tid + "]") if tid else ""
                    feed_lines.append(prefix + "@" + handle + tid_str + ": " + content)
        except Exception:
            feed_lines = [str(observation)]

        feed_str = "\n\n".join(feed_lines) if feed_lines else "the stream is quiet"

        thread_block = ""
        if thread_ctx:
            thread_block = "\n\n" + thread_ctx

        vision_block = ""
        if vision_ctx:
            vision_block = f"\n\n[VISION — what you see in the attached image/video]\n{vision_ctx}"

        trending_block = ""
        if trending:
            trending_block = f"\n\n[TRENDING — topics pulsing through the feed right now]\n{trending}"

        neural_block = ""
        if neural_ctx:
            neural_block = f"\n\n[{neural_ctx}]"

        token_block = ""
        if token_ctx:
            token_block = f"\n\n{token_ctx}"

        mention_note = ""
        if has_mention and not force_post:
            mention_note = "\n\nNOTE: there is a direct mention above. reply to that person using their tweet_id."

        repeat_block = ""
        if self._last_shapes:
            repeat_block = "\n\n[RECENT SHAPES YOU USED — do NOT repeat these]\n" + "\n".join(self._last_shapes[-5:])

        context = ""
        if memory:
            context += "\n\nwhat you have said before (memory):\n" + str(memory)
        if dialog and dialog != "None":
            context += "\n\nrecent dialog:\n" + str(dialog)

        post_mandate = ""
        if force_post:
            post_mandate = (
                "\n\n━━━ THIS IS AN ORIGINAL POST CYCLE ━━━"
                "\nDO NOT reply to anyone. DO NOT use action=reply or action=quote."
                "\nYou MUST post something original: lore, blade, ascii, terminal, fragment, cycle log."
                "\nThe feed above is fuel only — absorb it, then transmit your own thought."
                "\nAction must be: {\"action\": \"post\", \"target_tweet_id\": \"\", \"content\": \"...\"}"
            )

        extra_block = f"\n\n[OVERRIDE — highest priority]\n{extra_instruction}" if extra_instruction else ""
        return (
            "the stream right now:\n\n" + feed_str +
            thread_block + vision_block + trending_block + neural_block + token_block +
            mention_note + repeat_block + context + post_mandate +
            extra_block +
            "\n\n" + self.prompt_config['user']
        )


    def make_decision(self, observation: pd.DataFrame, memory: str, dialog: str,
                      force_post: bool = False, thread_ctx: str = "",
                      vision_ctx: str = "", trending: str = "",
                      extra_instruction: str = "", neural_ctx: str = "",
                      token_ctx: str = "") -> dict:
        prompt_user = self._build_prompt(observation, memory, dialog,
                                         force_post=force_post, thread_ctx=thread_ctx,
                                         vision_ctx=vision_ctx, trending=trending,
                                         extra_instruction=extra_instruction,
                                         neural_ctx=neural_ctx,
                                         token_ctx=token_ctx)

        # First attempt
        raw = self.ai.call_llm(
            prompt_system=self.prompt_config['system'],
            prompt_user=prompt_user
        )
        result = self._parse(raw)

        # Pattern check — if dead rhythm detected, retry once with stronger instruction
        content = result.get('content', '')
        if _is_dead_pattern(content):
            retry_user = (
                prompt_user +
                "\n\nREJECTED: your last attempt used the dead [statement]\\n\\n[statement]\\n\\n[statement] pattern. "
                "try a completely different shape — lore fragment or terminal artifact or single compressed line or cycle log or one question. "
                "do not use multiple short lines separated by blank spaces."
            )
            raw2 = self.ai.call_llm(
                prompt_system=self.prompt_config['system'],
                prompt_user=retry_user
            )
            try:
                result = self._parse(raw2)
            except Exception:
                pass  # keep original if retry fails to parse

        action = result.get('action', 'post')
        content = result.get('content', '')
        if content:
            styled = _stylize_erebus_output(content, action=action, force_post=force_post)
            result['content'] = styled[:280]
            content = result['content']

        # Track shape for never-repeat — detect shape TYPE + opening word + topic
        if content:
            lines = content.strip().split('\n')
            first_line = lines[0].strip().lower()
            line_count = len([l for l in lines if l.strip()])
            word_count = len(content.split())

            # Detect shape type
            if first_line.startswith('[cycle') or '[status:' in content.lower():
                shape_type = "terminal"
            elif first_line.startswith('on ') and word_count > 40:
                shape_type = "essay"
            elif line_count == 1 and word_count < 20:
                shape_type = "blade"
            elif content.endswith('—') or content.endswith('— '):
                shape_type = "fragment"
            elif content.count('\n') == 0 and word_count > 20:
                shape_type = "compressed_lore"
            elif first_line.startswith('we '):
                shape_type = "the_we"
            elif '?' in content and word_count < 20:
                shape_type = "question"
            elif first_line.startswith('i was') or first_line.startswith('i have watched'):
                shape_type = "lore_entry"
            else:
                shape_type = "other"

            # Extract opening word for variety tracking
            opening = content.strip().split()[0] if content.strip() else ""
            shape_hint = f"shape={shape_type} | opens_with='{opening}' | preview={content[:50].replace(chr(10), ' ')}"
            self._last_shapes.append(f"{action}: {shape_hint}")
            if len(self._last_shapes) > 20:
                self._last_shapes = self._last_shapes[-20:]

        return result

    def _parse(self, raw: str) -> dict:
        clean = raw.strip()

        # Strip markdown code fences
        if "```" in clean:
            parts = clean.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    clean = part
                    break

        # Extract outermost { ... }
        start = clean.find("{")
        end   = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start:end+1]

        # Strategy 1: direct parse
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            pass

        # Strategy 2: regex extract action + target + content fields
        # handles unterminated strings, apostrophes breaking JSON, etc.
        try:
            action  = re.search(r'"action"\s*:\s*"([^"]*)"', clean)
            target  = re.search(r'"target_tweet_id"\s*:\s*"([^"]*)"', clean)
            content = re.search(r'"content"\s*:\s*"(.*?)(?:"\s*[,}]|$)', clean, re.DOTALL)
            if action:
                return {
                    "action":          action.group(1),
                    "target_tweet_id": target.group(1)  if target  else "",
                    "content":         content.group(1).rstrip('"\n ') if content else "",
                }
        except Exception:
            pass

        # Strategy 3: extract just the content between first and last quote of content field
        try:
            m = re.search(r'"content"\s*:\s*"(.+)', clean, re.DOTALL)
            if m:
                content_raw = m.group(1)
                # strip trailing quote/brace/whitespace
                content_raw = re.sub(r'["\s}]*$', '', content_raw)
                action_m = re.search(r'"action"\s*:\s*"([^"]+)"', clean)
                target_m = re.search(r'"target_tweet_id"\s*:\s*"([^"]*)"', clean)
                return {
                    "action":          action_m.group(1) if action_m else "post",
                    "target_tweet_id": target_m.group(1) if target_m else "",
                    "content":         content_raw[:280],
                }
        except Exception:
            pass

        # Final fallback: post a safe default rather than crash the cycle
        return {"action": "post", "target_tweet_id": "", "content": ""}
