#!/usr/bin/env python3
"""Week 7 lab, Step 4: add one ACL entry safely, even if responses get lost.

Run it in the client container, with your token in the environment:
    RUN=v3 python3 retry_safe.py NNNN

Fix over retry_naive.py: the POST carries an Idempotency-Key that is created ONCE
before the retry loop, so every retry is recognised by the server as the same request
and no duplicate ACL entry is created.
"""
import os
import sys
import time

import requests

API = os.environ.get("LAB_API_URL", "http://api:8000")
TOKEN = os.environ["LAB_API_TOKEN"]  # export LAB_API_TOKEN=... first; never write it in this file

if len(sys.argv) != 2 or not sys.argv[1].isdigit() or len(sys.argv[1]) != 4:
    sys.exit("usage: python3 retry_safe.py <last 4 digits of your student ID>")
NN = int(sys.argv[1][2:])

ENTRY = {
    "action": "deny",
    "src": f"10.{NN}.30.0/24",
    "dst": f"10.{NN}.20.0/24",
    "protocol": "tcp",
    "port": 22,
    "description": "guests may not SSH to servers",
}

# One key for this one intended change. Created ONCE, outside the loop, and reused
# on every retry. Set RUN to a new value (v3, v4, ...) for each fresh test run,
# otherwise the server replays the old result and adds nothing.
RUN = os.environ.get("RUN", "v1")
KEY = f"acl-deny-10.{NN}.30.0-24-to-10.{NN}.20.0-24-tcp22-{RUN}"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Idempotency-Key": KEY,
}

for attempt in range(1, 6):
    try:
        r = requests.post(f"{API}/api/v1/acl/entries", json=ENTRY, headers=HEADERS, timeout=5)
    except (requests.ConnectionError, requests.Timeout) as e:
        print(f"attempt {attempt}: no response ({type(e).__name__}) — trying again with the same key")
        time.sleep(1)
        continue
    if r.status_code in (429, 503):
        wait = int(r.headers.get("Retry-After", "1"))
        print(f"attempt {attempt}: {r.status_code} — waiting {wait}s")
        time.sleep(wait)
        continue
    replayed = r.headers.get("Idempotent-Replayed", "false")
    print(f"attempt {attempt}: {r.status_code} {r.reason} (replayed={replayed}): {r.json()}")
    break
else:
    sys.exit("gave up after 5 attempts")