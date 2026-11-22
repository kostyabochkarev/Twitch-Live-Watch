#!/usr/bin/env python3
"""Poll public Twitch stream status through DecAPI and publish transitions to ntfy."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.txt"
STATE_FILE = ROOT / "state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def read_channels():
    if not CHANNELS_FILE.exists():
        raise RuntimeError("channels.txt is missing")
    result = []
    for raw in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        login = line.lower().removeprefix("https://twitch.tv/").removeprefix("http://twitch.tv/").strip("/@ ")
        if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
            print(f"Skip invalid channel entry: {line}")
            continue
        if login not in result:
            result.append(login)
    return result


def http_text(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "TwitchLiveWatch-GitHubActions/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace").strip()


def stream_status(login):
    url = "https://decapi.me/twitch/viewercount/" + urllib.parse.quote(login)
    body = http_text(url)
    if body.isdigit():
        return True, int(body)
    if re.search(r"\boffline\b|not live|isn't live|is not live", body, re.I):
        return False, 0
    raise RuntimeError(f"Unexpected DecAPI response for {login}: {body[:160]}")


def publish(login, viewers):
    title = f"{login} is LIVE!"
    message = f"Канал {login} начал трансляцию на Twitch. Зрителей: {viewers}."
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC, safe='')}"
    data = message.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
            "Priority": "high",
            "Tags": "twitch,video",
            "Click": f"https://www.twitch.tv/{login}",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"ntfy returned HTTP {response.status}")
        response.read()


def main():
    if not NTFY_TOPIC:
        print("ERROR: NTFY_TOPIC secret is not configured", file=sys.stderr)
        return 2
    channels = read_channels()
    if not channels:
        print("No valid Twitch channels in channels.txt")
        return 0

    try:
        previous = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        if not isinstance(previous, dict):
            previous = {}
    except Exception:
        previous = {}

    updated = dict(previous)
    notifications = 0
    failures = 0

    for index, login in enumerate(channels):
        try:
            online, viewers = stream_status(login)
            old = previous.get(login)
            # First successful run establishes a baseline and intentionally sends no alert.
            if old is not None and old is False and online is True:
                publish(login, viewers)
                notifications += 1
                print(f"NOTIFIED: {login} went live ({viewers} viewers)")
            else:
                print(f"OK: {login} is {'LIVE' if online else 'offline'}")
            updated[login] = online
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
            failures += 1
            print(f"ERROR: {login}: {exc}", file=sys.stderr)
        if index < len(channels) - 1:
            time.sleep(1.0)

    STATE_FILE.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Finished: {len(channels)} channel(s), {notifications} notification(s), {failures} error(s)")
    return 1 if failures == len(channels) else 0


if __name__ == "__main__":
    raise SystemExit(main())
