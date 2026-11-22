#!/usr/bin/env python3
"""Week 7 assignment: make the lab API match intent.yaml, safely.

    export LAB_USERNAME=netops LAB_PASSWORD=netops-pass   # environment only, never in files
    python3 sync.py intent.yaml --dry-run     # show the plan, send NO writes
    python3 sync.py intent.yaml               # apply the plan, then verify by reading back

How it stays safe:
  - the plan is computed from the CURRENT state, so a re-run after a crash only does what is left;
  - VLANs use PUT (idempotent) and DELETE (404 counts as success), so retries are harmless;
  - ACL entries have no PUT, so changes are delete + create; every POST carries an
    Idempotency-Key that is made ONCE per change per run and reused on every retry;
  - 401 -> log in again; 429 -> wait Retry-After; 503 / lost connection -> backoff with jitter.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import random
import re
import sys
import time
import uuid
from collections import Counter

import requests
import yaml

API = os.environ.get("LAB_API_URL", "http://api:8000")
BASE = f"{API}/api/v1"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_TRIES = 6  # for 503 / lost connections, per request


# ---------------------------------------------------------------- API client
class Api:
    def __init__(self):
        self.token = None
        self.requests = 0  # every HTTP request sent, retries included
        self.writes = 0    # logical writes (PUT / POST / DELETE), retries not counted
        self.logins = 0    # how many times we logged in (more than 1 = a token expired)

    def login(self):
        try:
            user, password = os.environ["LAB_USERNAME"], os.environ["LAB_PASSWORD"]
        except KeyError:
            sys.exit("Set LAB_USERNAME and LAB_PASSWORD in the environment first.")
        r = self._send("POST", f"{BASE}/auth/token", {"username": user, "password": password}, {}, auth=False)
        r.raise_for_status()
        self.token = r.json()["token"]
        self.logins += 1

    def call(self, method, path, body=None, extra_headers=None, accept=()):
        """One logical request. Returns the response if 2xx or in `accept`, else stops loudly."""
        url = path if path.startswith("http") else f"{BASE}{path}"
        if self.token is None:
            self.login()
        if method in ("PUT", "POST", "DELETE", "PATCH"):
            self.writes += 1
        r = self._send(method, url, body, extra_headers or {}, auth=True)
        if r.ok or r.status_code in accept:
            return r
        sys.exit(f"{method} {url} -> {r.status_code}: {r.text}")

    def _send(self, method, url, body, extra_headers, auth):
        short = url.split("/api/v1")[-1]
        failures = relogins = 0
        for _ in range(200):  # hard cap, so nothing can loop forever
            headers = dict(extra_headers)
            if auth:
                headers["Authorization"] = f"Bearer {self.token}"
            self.requests += 1
            try:
                r = requests.request(method, url, json=body, headers=headers, timeout=5)
            except (requests.ConnectionError, requests.Timeout):
                r = None  # reply lost or no connection: treat like a 503

            if r is not None and r.status_code == 401 and auth:
                relogins += 1
                if relogins > 3:
                    sys.exit("still 401 after logging in again")
                print("  [401] token expired: logging in again")
                self.login()
                continue
            relogins = 0

            if r is not None and r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "1"))
                print(f"  [429] rate limited: waiting {wait}s")
                time.sleep(wait)
                continue

            if r is None or r.status_code == 503:
                failures += 1
                what = "no reply" if r is None else "503"
                print(f"  [{what}] {method} {short}: retry {failures}/{MAX_TRIES - 1}")
                if failures >= MAX_TRIES:
                    sys.exit(f"gave up on {method} {url} after {MAX_TRIES} tries")
                time.sleep(2 ** (failures - 1) * 0.5 + random.random() * 0.5)
                continue

            return r
        sys.exit(f"too many attempts for {method} {url}")

    def read_list(self, path):
        """GET a collection (follows `next` if there is one) and return a list of items."""
        out, url = [], f"{BASE}{path}"
        while url:
            body = self.call("GET", url).json()
            if isinstance(body, list):
                out += body
                break
            items = body.get("items")
            if items is None:  # some other key holding the list
                items = next((v for v in body.values() if isinstance(v, list)), [])
            out += items
            url = body.get("next")
        return out


# ------------------------------------------------------------------- intent
def acl_tuple(e):
    """Normal form of an ACL entry, so intent and API entries can be compared."""
    port = e.get("port")
    return (
        e["action"], e["src"], e["dst"],
        e.get("protocol") or "any",
        int(port) if port is not None else None,
        e.get("description") or "",
    )


def load_intent(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    last4 = str(data["student_id_last4"])
    nn = int(last4[2:])
    block = ipaddress.ip_network(f"10.{nn}.0.0/16")

    vlans = {}
    for v in data["vlans"]:
        if not 2 <= v["id"] <= 4094:
            sys.exit(f"VLAN id {v['id']} out of range (2-4094; VLAN 1 belongs to the API)")
        if not NAME_RE.match(v["name"]):
            sys.exit(f"bad VLAN name: {v['name']!r}")
        if v["id"] in vlans:
            sys.exit(f"duplicate VLAN id {v['id']}")
        vlans[v["id"]] = v["name"]

    acl = []
    for e in data["acl"]:
        for side in ("src", "dst"):
            if not ipaddress.ip_network(e[side]).subnet_of(block):
                sys.exit(f"{e[side]} is outside {block}")
        if e.get("port") is not None and e.get("protocol") not in ("tcp", "udp"):
            sys.exit(f"port only works with tcp or udp: {e}")
        t = acl_tuple(e)
        if t in acl:
            sys.exit(f"duplicate ACL entry in intent: {e}")
        acl.append(t)
    return vlans, acl


# --------------------------------------------------------------- state, plan
def read_state(api):
    vlans = {v["id"]: v["name"] for v in api.read_list("/vlans") if v["id"] != 1}
    acl = [(e["id"], acl_tuple(e)) for e in api.read_list("/acl/entries")]
    return vlans, acl


def make_plan(want_vlans, want_acl, have_vlans, have_acl):
    """Return the steps needed, in a safe order: deletes first, then creates/changes."""
    plan = []
    # ACL entries, compared as a multiset (so duplicates on the API get removed)
    want_count = Counter(want_acl)
    for eid, t in have_acl:
        if want_count[t] > 0:
            want_count[t] -= 1
        else:
            plan.append(("delete", "acl", eid, t))
    have_count = Counter(t for _, t in have_acl)
    for t in want_acl:
        if have_count[t] > 0:
            have_count[t] -= 1
        else:
            plan.append(("create", "acl", None, t))
    # VLANs
    for vid in sorted(have_vlans):
        if vid not in want_vlans:
            plan.append(("delete", "vlan", vid, have_vlans[vid]))
    for vid in sorted(want_vlans):
        if vid not in have_vlans:
            plan.append(("create", "vlan", vid, want_vlans[vid]))
        elif have_vlans[vid] != want_vlans[vid]:
            plan.append(("change", "vlan", vid, want_vlans[vid]))
    # order: ACL deletes, VLAN deletes, VLAN create/change, ACL creates
    rank = {("delete", "acl"): 0, ("delete", "vlan"): 1,
            ("create", "vlan"): 2, ("change", "vlan"): 2, ("create", "acl"): 3}
    return sorted(plan, key=lambda s: rank[(s[0], s[1])])


def show(step):
    action, kind, ident, detail = step
    if kind == "vlan":
        return f"{action:6} VLAN {ident} ({detail})"
    if action == "delete":
        return f"{action:6} ACL entry id={ident}: {detail}"
    return f"{action:6} ACL entry: {detail}"


# --------------------------------------------------------------------- apply
def apply_step(api, step, run_id):
    action, kind, ident, detail = step
    if kind == "vlan" and action == "delete":
        api.call("DELETE", f"/vlans/{ident}", accept=(404,))      # already gone = success
    elif kind == "vlan":
        api.call("PUT", f"/vlans/{ident}", {"name": detail})      # create or replace: idempotent
    elif action == "delete":
        api.call("DELETE", f"/acl/entries/{ident}", accept=(404,))
    else:
        act, src, dst, proto, port, desc = detail
        body = {"action": act, "src": src, "dst": dst, "protocol": proto, "description": desc}
        if port is not None:
            body["port"] = port
        # Key made ONCE (here, before the first attempt), reused by every retry inside call().
        # The run id keeps keys from an old run from being replayed after an entry was deleted.
        digest = hashlib.sha256(json.dumps(detail).encode()).hexdigest()[:12]
        api.call("POST", "/acl/entries", body, {"Idempotency-Key": f"sync-{run_id}-{digest}"})


def main():
    ap = argparse.ArgumentParser(description="Make the lab API match intent.yaml")
    ap.add_argument("intent", nargs="?", default="intent.yaml", help="intent file (default: intent.yaml)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan; send no writes")
    args = ap.parse_args()

    want_vlans, want_acl = load_intent(args.intent)
    api = Api()
    have_vlans, have_acl = read_state(api)
    plan = make_plan(want_vlans, want_acl, have_vlans, have_acl)

    if not plan:
        print("already in sync: nothing to do")
    else:
        print(f"plan: {len(plan)} step(s)")
        for step in plan:
            print("  " + show(step))

    if args.dry_run:
        print(f"dry run: no writes sent ({api.requests} read requests)")
        return

    run_id = uuid.uuid4().hex[:8]
    for step in plan:
        apply_step(api, step, run_id)
        print("done:  " + show(step))

    # verify by reading the state back and planning again: the plan must be empty
    have_vlans, have_acl = read_state(api)
    left = make_plan(want_vlans, want_acl, have_vlans, have_acl)
    print(f"{api.writes} write(s), {api.requests} request(s), {api.logins} login(s) in total")
    if left:
        print("NOT in sync after apply:")
        for step in left:
            print("  " + show(step))
        sys.exit(1)
    print("verified: the API matches intent.yaml")


if __name__ == "__main__":
    main()