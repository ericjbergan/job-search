"""One place for the app's AI calls, so ChatGPT (OpenAI) or Claude (Anthropic) can do the work.

Each task uses the AI that did it best in a side-by-side test (compare_ai.py):
  * "listings" - reading job listings into fields: ChatGPT (the two were about even; uses the OpenAI balance).
  * "benefits" - benefits lookups (reading benefits pages, web search): Claude (found real figures with quotes).
  * "matching" - scoring a job against her resume and interests (ranking.py): ChatGPT for now.
Change any of them in .env with AI_LISTINGS / AI_BENEFITS / AI_MATCHING = openai|claude. If the chosen one's
key (OPENAI_API_KEY / ANTHROPIC_API_KEY) isn't in .env, the other one is used, so a laptop with only
an OpenAI key keeps working.

Models can be changed in .env: OPENAI_MODEL, CLAUDE_MODEL (reading text), CLAUDE_WEB_MODEL (web search).
"""

import contextlib
import os
import re

PROVIDERS = {"openai": ("ChatGPT", "OPENAI_API_KEY"), "claude": ("Claude", "ANTHROPIC_API_KEY")}
TASK_DEFAULTS = {"listings": ("AI_LISTINGS", "openai"), "benefits": ("AI_BENEFITS", "claude"),
                 "matching": ("AI_MATCHING", "openai")}
_override = None  # set by using() for side-by-side comparisons


class AIError(Exception):
    pass


def key_set(p):
    return bool(os.getenv(PROVIDERS[p][1]))


def provider(task="listings"):
    if _override:
        return _override
    setting, default = TASK_DEFAULTS[task]
    p = (os.getenv(setting) or default).strip().lower()
    p = "claude" if p in ("claude", "anthropic") else "openai"
    other = "openai" if p == "claude" else "claude"
    return other if not key_set(p) and key_set(other) else p


def name(task="listings"):
    return PROVIDERS[provider(task)][0]


def key_name(task="listings"):
    return PROVIDERS[provider(task)][1]


def has_key(task="listings"):
    return key_set(provider(task))


@contextlib.contextmanager
def using(p):
    """Temporarily send AI calls to one provider (for comparing the two)."""
    global _override
    before, _override = _override, p
    try:
        yield
    finally:
        _override = before


def _need_key(task):
    if not has_key(task):
        raise AIError(f"Missing {key_name(task)} in .env (needed for {name(task)})")


def openai_model():
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def claude_model():
    return os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")


def claude_web_model():
    return os.getenv("CLAUDE_WEB_MODEL", "claude-sonnet-5-5")


# --- clients -----------------------------------------------------------------------

def _openai():
    from openai import OpenAI
    # Extra retries: some home networks drop the first connection now and then.
    return OpenAI(max_retries=5)


def _anthropic():
    import anthropic
    return anthropic.Anthropic(max_retries=5)


def _claude_text(resp):
    if resp.stop_reason == "refusal":
        raise AIError("Claude declined to answer that request")
    return "".join(b.text for b in resp.content if b.type == "text")


# --- calls ------------------------------------------------------------------------

def json_answer(instructions, text, task="listings"):
    """Have the AI read `text` following `instructions`; returns its raw reply (JSON expected)."""
    _need_key(task)
    if provider(task) == "claude":
        resp = _anthropic().messages.create(
            model=claude_model(), max_tokens=8000,
            output_config={"effort": "low"},  # copying details out of text doesn't need deep thought
            system=instructions + "\nReply with the JSON object only, no other text.",
            messages=[{"role": "user", "content": text}])
        return _claude_text(resp)
    resp = _openai().chat.completions.create(
        model=openai_model(), temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": instructions}, {"role": "user", "content": text}])
    return resp.choices[0].message.content


def web_answer(instructions, question, task="benefits"):
    """Search the web to answer `question`. Returns (reply text, set of cited page URLs)."""
    _need_key(task)
    if provider(task) == "claude":
        client = _anthropic()
        messages = [{"role": "user", "content": question}]
        for _ in range(4):  # a long search can pause; send it back to let it finish
            resp = client.beta.messages.create(
                model=claude_web_model(), max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                system=instructions + "\nReply with the JSON object only, no other text.",
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5,
                        "user_location": {"type": "approximate", "country": "US"}}],
                messages=messages)
            if resp.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
        cited = {c.url for b in resp.content if b.type == "text"
                 for c in (getattr(b, "citations", None) or []) if getattr(c, "url", None)}
        return _claude_text(resp), cited
    resp = _openai().responses.create(
        model=openai_model(), tools=[{"type": "web_search"}], instructions=instructions, input=question)
    cited = {a.url for o in resp.output if getattr(o, "type", "") == "message"
             for part in o.content for a in (getattr(part, "annotations", None) or []) if getattr(a, "url", None)}
    return resp.output_text, cited


def first_json_object(text):
    """The {...} part of a reply (models sometimes wrap JSON in a code fence)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    return m.group(0) if m else None
