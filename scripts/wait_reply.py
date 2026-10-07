#!/usr/bin/env python3
"""Collect correlated Lark replies, save results, optionally notify via OCS."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

PART = re.compile(r"\((\d+)/(\d+)\)\s*$")


class ReplyError(Exception):
    pass


def page_data(payload):
    if not isinstance(payload, dict) or payload.get("code", 0) != 0:
        raise ReplyError("API returned an error or invalid JSON object")
    data = payload.get("data", payload)
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ReplyError("expected messages array in CLI response")
    return data


class Replies:
    def __init__(self, message_id, bot_id=None):
        self.message_id = message_id
        self.bot_id = bot_id
        self.messages = {}
        self.threads = set()

    def add(self, messages, trusted_thread=False):
        if not isinstance(messages, list):
            raise ReplyError("expected messages array")
        for msg in messages:
            if not isinstance(msg, dict):
                raise ReplyError("invalid message object")
            own_root = msg.get("message_id") == self.message_id
            if own_root and msg.get("thread_id"):
                self.threads.add(msg["thread_id"])
            if own_root:
                self.add(msg.get("thread_replies", []), trusted_thread=True)
            related = trusted_thread or msg.get("reply_to") == self.message_id
            sender = msg.get("sender") or {}
            if not isinstance(sender, dict):
                raise ReplyError("invalid sender object")
            if msg.get("deleted"):
                self.messages.pop(msg.get("message_id"), None)
                continue
            if (own_root or not related or msg.get("deleted")
                    or sender.get("sender_type") != "app"
                    or (self.bot_id and sender.get("id") != self.bot_id)):
                continue
            content = msg.get("content")
            mid = msg.get("message_id")
            if not isinstance(content, str) or not isinstance(mid, str) or not mid:
                raise ReplyError("reply requires message_id and text content")
            if content.strip():
                self.messages[mid] = msg

    def result(self):
        messages = list(self.messages.values())
        if not messages:
            return None
        parts = {}
        total = None
        for msg in messages:
            match = PART.search(msg["content"])
            if not match:
                continue
            k, n = map(int, match.groups())
            if n < 1 or n > 10000 or k < 1 or k > n or (total and total != n):
                raise ReplyError("invalid or inconsistent reply part numbers")
            total = n
            if k in parts and parts[k]["content"] != msg["content"]:
                raise ReplyError("conflicting content for the same reply part")
            parts[k] = msg
        if total:
            if set(parts) != set(range(1, total + 1)):
                return None
            messages = [parts[k] for k in range(1, total + 1)]
        else:
            messages.sort(key=lambda m: (str(m.get("create_time", "")), m["message_id"]))
        return {"message_id": self.message_id, "replies": messages,
                "content": "\n".join(m["content"] for m in messages)}


def run_json(command, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                              timeout=min(30, remaining))
    except subprocess.TimeoutExpired as exc:
        raise ReplyError("lark-cli request timed out") from exc
    if proc.returncode:
        # Raw CLI errors may contain credentials or private messages.
        raise ReplyError(f"lark-cli exited {proc.returncode}; check auth status/scopes")
    try:
        return page_data(json.loads(proc.stdout))
    except json.JSONDecodeError as exc:
        raise ReplyError("lark-cli returned invalid JSON") from exc


def pages(command, deadline):
    seen = set()
    token = None
    while True:
        data = run_json(command + (["--page-token", token] if token else []), deadline)
        yield data
        if not data.get("has_more"):
            return
        token = data.get("page_token")
        if not isinstance(token, str) or not token or token in seen:
            raise ReplyError("missing or repeated pagination token")
        seen.add(token)


def poll(args, replies, deadline):
    start = args.start or datetime.fromtimestamp(time.time() - 120, timezone.utc).isoformat()
    base = ["lark-cli", "im", "+chat-messages-list", "--chat-id", args.chat_id,
            "--as", "user", "--start", start, "--order", "asc", "--page-size", "50",
            "--no-reactions", "--json"]
    failures = 0
    while time.monotonic() < deadline:
        try:
            if not replies.threads:
                for data in pages(base, deadline):
                    replies.add(data["messages"])
            # Fetch our own thread beyond CLI enrichment limits.
            for thread in sorted(replies.threads):
                command = ["lark-cli", "im", "+threads-messages-list", "--thread", thread,
                           "--as", "user", "--order", "asc", "--page-size", "500",
                           "--no-reactions", "--json"]
                for data in pages(command, deadline):
                    replies.add(data["messages"], trusted_thread=True)
            failures = 0
        except ReplyError as exc:
            failures += 1
            print(f"request failed ({failures}/3): {exc}", file=sys.stderr)
            if failures >= 3:
                raise
        else:
            result = replies.result()
            if result:
                return result
        time.sleep(max(0, min(args.interval * 2 ** failures, deadline - time.monotonic())))
    raise TimeoutError()


def stream(args, replies, deadline):
    # A blocking reader thread keeps the deadline usable on Windows too.
    incoming = queue.Queue(maxsize=64)

    def reader():
        for line in sys.stdin:
            incoming.put(line)
        incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError()
        try:
            line = incoming.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise TimeoutError() from exc
        if line is None:
            raise ReplyError("push stream closed before a complete reply")
        if not line.strip():
            continue
        try:
            data = page_data(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ReplyError("invalid JSON in push stream") from exc
        replies.add(data["messages"])
        result = replies.result()
        if result:
            return result


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id")
    parser.add_argument("message_id")
    parser.add_argument("timeout", nargs="?", type=positive, default=900)
    parser.add_argument("interval", nargs="?", type=positive, default=20)
    parser.add_argument("--source", choices=("poll", "stdin"), default="poll")
    parser.add_argument("--start", help="question timestamp in ISO 8601; needed for delayed waits")
    parser.add_argument("--bot-id", help="expected bot sender.id")
    parser.add_argument("--output", type=Path, help="new JSON result file, never overwrite")
    parser.add_argument("--notify", help="explicit local OCS recipient; requires --output")
    args = parser.parse_args(argv)
    if args.start:
        try:
            parsed_start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
            if parsed_start.tzinfo is None:
                raise ValueError()
        except ValueError:
            parser.error("--start must be an ISO 8601 timestamp with timezone")
    if args.notify and (not args.output or "@" in args.notify or args.notify.startswith("-")):
        parser.error("--notify requires --output and an explicit local OCS address")
    binaries = (["lark-cli"] if args.source == "poll" else []) + (["ocs"] if args.notify else [])
    for binary in binaries:
        if not shutil.which(binary):
            print(f"{binary} not installed", file=sys.stderr)
            return 3
    if args.output and (args.output.exists() or not args.output.parent.is_dir()):
        print("output must be a new file in an existing directory", file=sys.stderr)
        return 3
    deadline = time.monotonic() + args.timeout
    try:
        replies = Replies(args.message_id, args.bot_id)
        result = (poll if args.source == "poll" else stream)(args, replies, deadline)
        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(result, file, ensure_ascii=False, indent=2)
                file.write("\n")
        print(result["content"])
        if args.notify:
            notice = f"code-search reply collected for {args.message_id}. Result file: {args.output.resolve()}"
            # One attempt: OCS exit 2/3 may mean already stored or unknown delivery.
            try:
                worker = "code-search-" + hashlib.sha256(args.message_id.encode()).hexdigest()[:12]
                receipt = subprocess.run(["ocs", "dm", args.notify, notice, "--as", worker], timeout=30,
                                         stdout=sys.stderr, stderr=sys.stderr)
            except subprocess.TimeoutExpired:
                print("OCS outcome unknown; result saved; do not resend", file=sys.stderr)
                return 5
            if receipt.returncode:
                print("OCS wake not confirmed; result saved; inspect receipt, do not resend", file=sys.stderr)
                return 5
        return 0
    except TimeoutError:
        print("timeout: no complete reply before deadline", file=sys.stderr)
        return 2
    except (ReplyError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    for output_stream in (sys.stdout, sys.stderr):
        if hasattr(output_stream, "reconfigure"):
            output_stream.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
