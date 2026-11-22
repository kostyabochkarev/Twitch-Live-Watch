#!/usr/bin/env python3
"""Monitor public Twitch channels and publish stream/title changes to ntfy.

State is persisted in state.json by the GitHub Actions workflow. DecAPI provides
public channel information; it is cached, so alerts can be delayed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.txt"
STATE_FILE = ROOT / "state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/") or "https://ntfy.sh"
DECAPI_BASE = os.environ.get("DECAPI_BASE", "https://decapi.me/twitch").strip().rstrip("/")
# DecAPI documents a 100-request / 60-second IP limit. 0.7s keeps us below it.
DECAPI_MIN_INTERVAL = 0.70
HTTP_TIMEOUT = 20
_last_decapi_request = 0.0

OFFLINE_PATTERNS = (
    r"\boffline\b",
    r"not\s+live",
    r"isn't\s+live",
    r"is\s+not\s+live",
    r"stream\s+is\s+offline",
    r"currently\s+offline",
)


def clean_login(value: str) -> str | None:
    """Accept a Twitch login or channel URL and return its lowercase login."""
    text = value.strip()
    text = re.sub(r"^https?://", "", text, flags=re.I)
    text = re.sub(r"^www\.", "", text, flags=re.I)
    if text.lower().startswith("twitch.tv/"):
        text = text[len("twitch.tv/"):]
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/@ ")
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,25}", text):
        return None
    return text.lower()


def read_channels() -> list[dict[str, str]]:
    if not CHANNELS_FILE.exists():
        raise RuntimeError("channels.txt is missing")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        login = clean_login(line)
        if not login:
            print(f"Skip invalid channel entry: {line}")
            continue
        if login not in seen:
            seen.add(login)
            result.append({"login": login, "display": line.rsplit("/", 1)[-1].strip("/@ ")})
    return result


def http_text(url: str, timeout: int = HTTP_TIMEOUT) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TwitchLiveWatch-GitHubActions/2.0", "Accept": "text/plain"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace").strip()


def decapi_text(endpoint: str, login: str) -> str:
    global _last_decapi_request
    delay = DECAPI_MIN_INTERVAL - (time.monotonic() - _last_decapi_request)
    if delay > 0:
        time.sleep(delay)
    _last_decapi_request = time.monotonic()
    url = f"{DECAPI_BASE}/{endpoint}/{urllib.parse.quote(login)}"
    try:
        return http_text(url)
    except urllib.error.HTTPError as exc:
        # A brief retry helps with occasional DecAPI throttling or transient failures.
        if exc.code in (429, 500, 502, 503, 504):
            time.sleep(3)
            return http_text(url)
        raise


def parse_stream_status(body: str, login: str) -> tuple[bool, int]:
    text = body.strip()
    if text.isdigit():
        return True, int(text)
    if any(re.search(pattern, text, re.I) for pattern in OFFLINE_PATTERNS):
        return False, 0
    raise RuntimeError(f"Unexpected DecAPI viewercount response for {login}: {text[:160]}")


def read_channel_snapshot(login: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read live/offline state plus title/game, without losing transition data
    when a metadata endpoint temporarily fails.
    """
    raw_viewers = decapi_text("viewercount", login)
    online, viewers = parse_stream_status(raw_viewers, login)

    old = previous or {}
    old_metadata_known = bool(old.get("metadata_known"))
    title_ok = game_ok = False
    title = str(old.get("title") or "")
    game = str(old.get("game") or "")

    # DecAPI's title/game endpoints read channel info and work while the channel
    # is offline too. They share a ~3 minute cache on DecAPI. Keep previous
    # metadata if either request fails, so one transient error can't create a
    # false "status updated" alert on the next run.
    try:
        title = decapi_text("title", login).strip()
        title_ok = True
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        print(f"WARNING: title lookup failed for {login}: {exc}", file=sys.stderr)
    try:
        game = decapi_text("game", login).strip()
        game_ok = True
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        print(f"WARNING: game lookup failed for {login}: {exc}", file=sys.stderr)

    metadata_known = title_ok and game_ok
    if not metadata_known and old_metadata_known:
        title = str(old.get("title") or "")
        game = str(old.get("game") or "")
        metadata_known = True

    return {
        "online": online,
        "viewers": viewers,
        "title": title,
        "game": game,
        "metadata_known": metadata_known,
    }


def ntfy_publish(message_title: str, message: str, login: str) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC secret is not configured")
    topic_url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC, safe='')}"
    # Keep HTTP headers ASCII; UTF-8 Cyrillic/emojis go in the message body.
    request = urllib.request.Request(
        topic_url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Title": f"Twitch Live Watch: {message_title}",
            "Priority": "high",
            "Tags": "twitch,video",
            "Click": f"https://www.twitch.tv/{login}",
        },
    )
    # Use a short prefix in the body for each notification type; ntfy renders it
    # normally and no non-ASCII custom header needs to be encoded.
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"ntfy returned HTTP {response.status}")
        response.read()
    print(f"NOTIFIED: {login} — {message_title}")


def _display(value: str) -> str:
    return value[:1].upper() + value[1:] if value else value


def notify_started(login: str, display: str, current: dict[str, Any]) -> None:
    title = current.get("title") or "Название трансляции не указано"
    game = current.get("game") or "Категория не указана"
    viewers = int(current.get("viewers") or 0)
    message = (
        f"👾 {_display(display)} завёл стрим\n"
        f"{title}\n\n"
        f"Играет в: {game}\n"
        f"Зрителей: {viewers}\n\n"
        f"twitch.tv/{login}"
    )
    ntfy_publish("Stream started", message, login)


def notify_ended(login: str, display: str, previous: dict[str, Any]) -> None:
    viewers = int(previous.get("viewers") or 0)
    message = (
        f"Стрим {_display(display)} закончился.\n"
        f"Зрителей на стриме: {viewers} (последнее известное значение)\n"
    )
    old_title = str(previous.get("title") or "").strip()
    old_game = str(previous.get("game") or "").strip()
    if old_title:
        message += f"\n{old_title}\n"
    if old_game:
        message += f"Игра: {old_game}\n"
    message += "\n____\nСтрим сейчас оффлайн\n\n" f"twitch.tv/{login}"
    ntfy_publish("Stream ended", message, login)


def notify_status_changed(login: str, display: str, current: dict[str, Any]) -> None:
    """Notify about title/category edits, with live-specific wording while streaming."""
    title = current.get("title") or "Название не указано"
    game = current.get("game") or "Не указана"

    if current.get("online"):
        # During a live broadcast, describe the category as what the streamer is playing in.
        message = (
            f"{_display(display)} обновил статус:\n\n"
            f"{title}\n\n"
            f"Играет в: {game}\n\n"
            f"twitch.tv/{login}"
        )
        ntfy_publish("Live stream updated", message, login)
    else:
        message = (
            f"{_display(display)} обновил статус:\n\n"
            f"{title}\n"
            f"Игра: {game}\n"
            f"____\n"
            f"Стрим сейчас оффлайн\n\n"
            f"twitch.tv/{login}"
        )
        ntfy_publish("Channel status updated", message, login)


def normalize_previous(value: Any) -> dict[str, Any] | None:
    """Read both legacy boolean state and the richer state format."""
    if isinstance(value, bool):
        return {
            "online": value,
            "viewers": 0,
            "title": "",
            "game": "",
            "metadata_known": False,
        }
    if isinstance(value, dict) and isinstance(value.get("online"), bool):
        return {
            "online": value["online"],
            "viewers": int(value.get("viewers") or 0),
            "title": str(value.get("title") or ""),
            "game": str(value.get("game") or ""),
            "metadata_known": bool(value.get("metadata_known", True)),
        }
    return None


def load_state() -> tuple[dict[str, Any], bool]:
    """Return previous state and whether this is the first-ever baseline run."""
    if not STATE_FILE.exists():
        return {}, True
    try:
        previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: could not read state.json ({exc}); starting a fresh baseline", file=sys.stderr)
        return {}, True
    if not isinstance(previous, dict):
        return {}, True
    return previous, False


def process_channel(login: str, display: str, previous_raw: Any, first_run: bool) -> tuple[dict[str, Any], int]:
    previous = normalize_previous(previous_raw)
    current = read_channel_snapshot(login, previous)
    notifications = 0

    # A genuinely first-ever run establishes a quiet baseline. When a channel is
    # added later, however, an already-live channel produces a start notification.
    if previous is None:
        if not first_run and current["online"]:
            notify_started(login, display, current)
            notifications += 1
        else:
            print(f"BASELINE: {login} is {'LIVE' if current['online'] else 'offline'}")
        return current, notifications

    was_online = bool(previous["online"])
    is_online = bool(current["online"])

    if not was_online and is_online:
        notify_started(login, display, current)
        notifications += 1
    elif was_online and not is_online:
        notify_ended(login, display, previous)
        notifications += 1

    # Detect title and/or category edits both offline and during an active stream.
    # While live, notify_status_changed uses the requested "Играет в:" wording.
    # Skip metadata alerts for legacy state (which did not store those fields).
    # A start alert already contains the new title and category, so avoid duplicate
    # notices for the same update on the same polling run.
    metadata_was_known = bool(previous.get("metadata_known"))
    metadata_changed = metadata_was_known and (
        current["title"] != previous.get("title", "")
        or current["game"] != previous.get("game", "")
    )
    if metadata_changed and not (not was_online and is_online):
        notify_status_changed(login, display, current)
        notifications += 1

    print(
        f"OK: {login} is {'LIVE' if is_online else 'offline'}; "
        f"viewers={current['viewers']}; title_changed={metadata_changed}"
    )
    return current, notifications


def main() -> int:
    if not NTFY_TOPIC:
        print("ERROR: NTFY_TOPIC secret is not configured", file=sys.stderr)
        return 2

    channels = read_channels()
    if not channels:
        print("No valid Twitch channels in channels.txt")
        return 0

    previous, first_run = load_state()
    updated = dict(previous)
    notifications = 0
    failures = 0

    for item in channels:
        login = item["login"]
        display = item["display"] or login
        try:
            current, sent = process_channel(login, display, previous.get(login), first_run)
            updated[login] = current
            notifications += sent
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, ValueError) as exc:
            failures += 1
            print(f"ERROR: {login}: {exc}", file=sys.stderr)
        # Request pacing is applied to each DecAPI request in decapi_text().

    STATE_FILE.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Finished: {len(channels)} channel(s), {notifications} notification(s), "
        f"{failures} error(s)"
    )
    # Preserve previous state for channels that failed this run. Exit nonzero only
    # if every configured channel failed, so transient single-channel problems
    # remain visible in logs without blocking all other channels.
    return 1 if failures == len(channels) else 0


if __name__ == "__main__":
    raise SystemExit(main())
