"""Decides which posts are actually newsworthy.

Two stages, cheapest first:
  1. Obvious junk is thrown out with plain rules (free, instant).
  2. Whatever survives goes to the AI in small batches.

The AI also collapses duplicates -- when a company account and its CEO both
announce the same thing, you get one entry, not two.
"""

import datetime as dt
import json
import os
import re
import time

import requests

API_KEY_ENV = "GEMINI_API_KEY"
BASE = "https://generativelanguage.googleapis.com/v1beta"


class AllModelsExhausted(RuntimeError):
    """Every model tried was dead. Carries newly_dead so the caller can
    still remember them for next time, even though this run failed."""

    def __init__(self, message: str, newly_dead: set):
        super().__init__(message)
        self.newly_dead = newly_dead

# Posts per request. One huge request to a model that "thinks" before
# answering will time out; several small ones are faster and far more
# resilient, since a single bad batch no longer sinks the whole run.
CHUNK = 15
TIMEOUT = 120

# Google's servers return these when they are momentarily overloaded. They
# mean "ask again in a moment", not "this model is unusable" -- so we retry
# the same model with a growing pause before moving on.
TRANSIENT = (500, 502, 503, 504)
RETRIES = 2

# Very long posts are almost always pasted articles or threads. Keeping the
# opening is enough to judge newsworthiness and keeps requests small.
MAX_POST_CHARS = 700

# Google retires model names on a schedule (the whole 2.x Flash line went
# away in mid-2026). Rather than hardcode a name that will rot, we ask the
# API what exists and pick the best current Flash model. Set GEMINI_MODEL
# to pin one specific model and skip the lookup entirely.
_EXCLUDE = ("embedding", "aqa", "image", "tts", "audio", "vision", "live")


def _rank(name: str):
    """Higher is better. Returns None for models we never want."""
    if any(bad in name for bad in _EXCLUDE):
        return None
    match = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(match.group(1)) if match else 0.0
    if "preview" in name or "exp" in name:
        version -= 0.05  # prefer stable releases at the same version
    # Flash-Lite is tried FIRST, not as a fallback. On this key, Lite
    # variants carry a far larger daily quota than plain Flash (500/day vs
    # 20/day, confirmed on the account's own AI Studio dashboard) -- and
    # Flash-Lite is plenty capable for a structured yes/no scoring task
    # like this one. Plain Flash still gets tried, just after.
    if "flash" in name and "lite" in name:
        kind = 2
    elif "flash" in name:
        kind = 1
    else:
        kind = 0         # pro and friends: burn the free quota too quickly
    return (kind, version)


def _discover(api_key: str) -> list[str]:
    """Ask Google which models this key can actually use, best first."""
    response = requests.get(f"{BASE}/models", params={"key": api_key}, timeout=30)
    response.raise_for_status()

    usable = []
    for model in response.json().get("models", []):
        name = model.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        if "generateContent" not in model.get("supportedGenerationMethods", []):
            continue
        rank = _rank(name)
        if rank is not None:
            usable.append((rank, name))

    usable.sort(reverse=True)
    return [name for _, name in usable]


def _candidates(api_key: str, dead_today: set) -> list[str]:
    """Which models to try, best first. Set GEMINI_MODEL to pin one.

    Google gives each model its OWN small daily quota (20 requests/day on
    the free tier, seen empirically -- not the ~1500/day older Gemini
    generations used to allow). Trying more distinct models is what turns a
    useless 20-a-day budget into a workable ~100-a-day combined one, so this
    tries up to 8 rather than 3. Anything already known to be exhausted
    today is skipped rather than re-probed and wasting a request on it.
    """
    pinned = os.environ.get("GEMINI_MODEL", "").strip()
    if pinned:
        return [pinned]
    try:
        found = [m for m in _discover(api_key) if m not in dead_today]
        if found:
            print(f"  models to try today: {', '.join(found[:8])}"
                  + (f" ({len(dead_today)} already exhausted today, skipped)" if dead_today else ""))
            return found[:8]
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not list models ({exc}), falling back to known names")
    fallback = ["gemini-3-flash", "gemini-2.5-flash", "gemini-3-flash-lite", "gemini-2.5-flash-lite"]
    return [m for m in fallback if m not in dead_today]

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
    max_age = float(cfg.get("max_age_hours", 6) or 0)
    cutoff = None
    if max_age > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age)

    kept, stale = [], 0
    for p in posts:
        if cfg.get("skip_retweets", True) and p.is_repost:
            continue
        if cfg.get("skip_replies", True) and p.is_reply:
            continue
        if len(p.text) < int(cfg.get("min_length", 60)):
            continue

        # Old news is not news. A post can be new TO US and still be hours
        # old -- the account may not have been checked in a while, or the
        # tracker may be catching up after an outage. Either way you do not
        # want an alert about something that broke last night.
        if cutoff and p.created_at:
            try:
                when = dt.datetime.fromisoformat(p.created_at)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
                if when < cutoff:
                    stale += 1
                    continue
            except ValueError:
                pass  # unparseable date: let it through rather than lose it

        kept.append(p)

    if stale:
        print(f"  dropped {stale} post(s) older than {max_age:g}h")
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
        f"{BASE}/models/{model}:generateContent",
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        },
        timeout=TIMEOUT,
    )


def _try_model(model: str, prompt: str, api_key: str, newly_dead: set):
    """A successful response from this model, or None if it cannot serve us.

    When Google says a model is out of quota (429), that model is added to
    newly_dead so the caller can remember it and skip it for the rest of
    today, instead of paying for the same failed probe every ten minutes.
    """
    for attempt in range(1, RETRIES + 1):
        try:
            response = _ask(model, prompt, api_key)
        except requests.exceptions.Timeout:
            print(f"  ! {model} timed out after {TIMEOUT}s")
            return None

        if response.status_code == 404:
            print(f"  ! {model} does not exist")
            return None
        if response.status_code == 429:
            print(f"  ! {model} is out of free quota for today")
            newly_dead.add(model)
            return None
        if response.status_code in TRANSIENT:
            if attempt == RETRIES:
                print(f"  ! {model} still overloaded after {RETRIES} tries")
                return None
            pause = 5 * attempt
            print(f"  ! {model} returned {response.status_code} (overloaded), "
                  f"retrying in {pause}s")
            time.sleep(pause)
            continue

        return response
    return None


def _score_batch(batch, rubric: str, models: list[str], api_key: str, newly_dead: set) -> list[tuple]:
    listing = "\n\n".join(
        f"[{i}] @{p.handle} ({p.likes} likes, {p.reposts} reposts)\n{p.text[:MAX_POST_CHARS]}"
        for i, p in enumerate(batch)
    )
    prompt = PROMPT.format(rubric=rubric, posts=listing)

    response = None
    for model in models:
        if model in newly_dead:
            continue  # already ran out of quota earlier in THIS run
        response = _try_model(model, prompt, api_key, newly_dead)
        if response is not None:
            print(f"  scored with {model}")
            break

    if response is None:
        raise RuntimeError(f"no model would answer (tried {', '.join(models)})")
    response.raise_for_status()

    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]

    judged = []
    for item in _extract_json(text):
        idx = int(item.get("i", -1))
        if not (0 <= idx < len(batch)):
            continue
        if item.get("dupe_of") is not None:
            print(f"  [dupe] @{batch[idx].handle}: same story as another post")
            continue
        judged.append(
            (
                batch[idx],
                float(item.get("score", 0)),
                str(item.get("headline", "")).strip(),
                str(item.get("why", "")).strip(),
            )
        )
    return judged


def score(posts, rubric: str, dead_today: set | None = None):
    """Returns (judged, unscreened, newly_dead).

    judged     = [(post, score, headline, why), ...], duplicates removed
    unscreened = posts the AI could not look at, so the caller can surface
                 them rather than silently dropping them.
    newly_dead = model names that hit their daily quota just now -- the
                 caller should remember these so tomorrow's runs (and even
                 this run's later batches) do not waste a request re-asking.
    """
    if not posts:
        return [], [], set()

    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"No {API_KEY_ENV} secret found.")

    newly_dead: set = set()
    models = _candidates(api_key, dead_today or set())
    batches = [posts[i : i + CHUNK] for i in range(0, len(posts), CHUNK)]

    judged, unscreened = [], []
    for n, batch in enumerate(batches, 1):
        print(f"  batch {n} of {len(batches)} ({len(batch)} posts)")
        try:
            judged.extend(_score_batch(batch, rubric, models, api_key, newly_dead))
        except Exception as exc:  # noqa: BLE001
            # One bad batch must not throw away the batches that worked, but
            # these posts are about to be marked as seen -- so hand them back
            # to be listed in the email rather than dropped in silence.
            print(f"  ! batch {n} failed: {exc}")
            unscreened.extend(batch)

    if unscreened and not judged:
        raise AllModelsExhausted(
            f"all {len(batches)} scoring batches failed "
            f"(exhausted today: {', '.join(sorted(newly_dead)) or 'none new'})",
            newly_dead,
        )
    if unscreened:
        print(f"  ! {len(unscreened)} posts could not be screened; listing them raw")
    return judged, unscreened, newly_dead
