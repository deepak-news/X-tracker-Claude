"""Decides which posts are actually newsworthy.

Two stages, cheapest first:
  1. Obvious junk is thrown out with plain rules (free, instant).
  2. Whatever survives goes to the AI in ONE batched request, so a run
     costs a single call no matter how many posts arrived.

The AI also collapses duplicates -- when a company account and its CEO
both announce the same thing, you get one entry, not two.
"""

import json
import os

import requests

API_KEY_ENV = "GEMINI_API_KEY"
# If the main model runs out of free quota, fall back to the lighter one
# rather than going silent.
MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    "gemini-2.5-flash-lite",
]
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """You are the overnight desk editor for a national wire service.
One reporter relies on you. Missing a real story is bad; waking them for a
non-story is worse.

WHAT THIS REPORTER COVERS:
{rubric}

Score each post 0-10 for whether it needs a wire alert.

  0-3  opinion, banter, memes, motivational posts, self-promotion,
       recycled news, vague teasers, congratulations, condolences
  4-6  mildly interesting industry colour, but not filable
  7-8  a filable story: a confirmed deal, acquisition, investment,
       partnership, product or model launch, leadership change,
       resignation, policy or regulatory move, hard numbers
  9-10 a story the reporter would be in trouble for missing

Rules:
- The post itself must contain the news. A bare link, a "big news coming"
  tease, or a reply-guy reaction is not news.
- An opinion is only newsworthy when the person saying it is senior enough
  that the statement is itself the story.
- Anything involving India, Indian companies, Indian executives or Indian
  policy gets roughly two extra points of weight.
- Routine international product updates and marketing posts are not news.
- If several posts cover the SAME underlying event, give the fullest or most
  authoritative one its real score and set "dupe_of" to that post's number on
  all the others.

Return ONLY a JSON array, one object per post, same order:
[{{"i": <post number>, "score": <0-10>, "headline": "<max 12 words, what actually happened>", "why": "<one short sentence>", "dupe_of": <post number or null>}}]

POSTS:
{posts}
"""


def prefilter(posts, cfg) -> list:
    """Free, instant rules that remove most of the volume before the AI runs."""
    kept = []
    for p in posts:
        if cfg.get("skip_retweets", True) and p.is_repost:
            continue
        if cfg.get("skip_replies", True) and p.is_reply:
            continue
        if len(p.text) < int(cfg.get("min_length", 60)):
            continue
        kept.append(p)
    return kept


def _extract_json(raw: str):
    """Models occasionally wrap JSON in ``` fences. Cope with it."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"AI did not return a JSON list. Got: {raw[:300]}")
    return json.loads(raw[start : end + 1])


def _ask(model: str, prompt: str, api_key: str):
    return requests.post(
        ENDPOINT.format(model=model),
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        },
        timeout=90,
    )


def score(posts, rubric: str) -> list[tuple]:
    """Returns [(post, score, headline, why), ...], duplicates already removed."""
    if not posts:
        return []

    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"No {API_KEY_ENV} secret found.")

    listing = "\n\n".join(
        f"[{i}] @{p.handle} ({p.likes} likes, {p.reposts} reposts)\n{p.text}"
        for i, p in enumerate(posts)
    )
    prompt = PROMPT.format(rubric=rubric, posts=listing)

    response = None
    for model in MODELS:
        response = _ask(model, prompt, api_key)
        if response.status_code == 429:
            print(f"  ! {model} is out of free quota, trying the next model")
            continue
        break
    response.raise_for_status()

    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]

    judged = []
    for item in _extract_json(text):
        idx = int(item.get("i", -1))
        if not (0 <= idx < len(posts)):
            continue
        if item.get("dupe_of") is not None:
            print(f"  [dupe] @{posts[idx].handle}: same story as post {item['dupe_of']}")
            continue
        judged.append(
            (
                posts[idx],
                float(item.get("score", 0)),
                str(item.get("headline", "")).strip(),
                str(item.get("why", "")).strip(),
            )
        )
    return judged
