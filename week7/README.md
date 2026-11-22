# Wrap up Answers

## 1. What does `docker network inspect apilab` show, and how is it built from Week 6's building blocks?

It shows the user-defined bridge network `apilab`: its subnet and gateway, and the two attached containers (`api` and `client`) with their IP and MAC addresses. Underneath, Docker built it from Linux networking pieces: a network namespace for each container, a veth pair linking each container's namespace to the host, and a Linux bridge joining the host ends of the veth pairs. Docker's built-in DNS on user-defined networks lets `client` reach `api` by name.

## 2. For each status code in Step 2, what should a client do next, and which should it never retry?

| Code | Client should | Retry? |
|---|---|---|
| 400 | Fix the malformed request | Never |
| 403 | Not allowed: use proper credentials or stop | Never |
| 404 | Treat as "absent" or fix the path | Never |
| 405 | Use the correct method | Never |
| 409 | Resource already exists: use PUT or skip | Never |
| 422 | Fix the invalid value | Never |
| 429 | Wait for `Retry-After`, then resend | Yes, after waiting |
| 503 | Back off, then resend | Yes, only if the request is idempotent or carries an `Idempotency-Key` |

Never retry 400, 403, 404, 405, 409 and 422: sending the same request again gives the same error.

## 3. Why did `retry_naive.py` create duplicates, and how does the fix prevent them? Why must the `Idempotency-Key` be chosen once, before the first attempt?

With lost responses, the server applied the POST but dropped the connection before replying. The script saw "no response", assumed the request had failed and retried. POST is not idempotent, so the server created a second identical ACL entry.

The fix sends an `Idempotency-Key` header with the POST. On a retry, the server recognises the key, applies nothing new and returns the saved result (`Idempotent-Replayed: true`). The audit log then shows exactly one entry.

The key must be created once, before the first attempt, because the server can only match a retry to the original request if the key is identical. A new key on each attempt would look like a new request to the server and would bring the duplicates back.

## 4. How many requests did `inventory.py` send, and why more than the number of pages?

37 requests to read 263 devices. With `limit=10` there are 27 pages, so 10 requests were extra: 8 retries after 503 errors and 2 retries after 429 waits. The exact number varies between runs because the chaos is random.

## 5. What did you find in the OpenAPI document that you would never have guessed?

`/api/v1/acl/entries/{id}` allows only GET and DELETE. There is no PUT or PATCH, so an ACL entry cannot be edited in place: to change a rule, the client must delete the old entry and create a new one with POST. This differs from VLANs, where PUT updates the resource directly, and it affects how `sync.py` has to plan changes to ACL entries. A DELETE returns 204 the first time and 404 if the entry is already gone, so a retried DELETE is safe and a client should treat 404 on a DELETE as success.

The OpenAPI document also answers the other Step 7 questions:

- **Maximum `limit` for devices:** 50 (minimum 1, default 20). Asking for more gives a 422.
- **Same `Idempotency-Key` with a different body:** rejected with 422. The same key with the same body returns the first result with `Idempotent-Replayed: true`.
- **Filtering devices by site:** the lecture never mentioned the `site` query parameter on `/api/v1/devices`. Its allowed values are `kl-hq`, `pj-dc`, `penang` and `johor`.

Listing one site's devices with `curl`:


## Assignment Answers

### 1. How does `sync.py` decide what to change?

It reads the current VLANs and ACL entries from the API, compares them with `intent.yaml` and builds a plan of delete, change and create steps. Nothing is stored between runs: the plan always comes from the current state, so a re-run after a crash only does what is left.

- **VLAN created:** VLAN 30 (`guests`) is in the intent but not on the API, so the plan has `create VLAN 30` and the script sends `PUT /vlans/30 {"name": "guests"}`.
- **VLAN renamed:** if VLAN 10 exists as `old` but the intent says `users`, the id matches and the name differs, so the plan has `change VLAN 10`, which is also a `PUT`.
- **VLAN deleted:** VLAN 99 (`old-guest`) is on the API but not in the intent, so the plan has `delete VLAN 99` and the script sends `DELETE /vlans/99`. VLAN 1 is ignored and never deleted.
- **ACL entry:** each entry is normalised to a tuple (action, src, dst, protocol, port, description) and the two sides are compared as a multiset. An intent entry missing from the API, such as `guests-no-ssh`, becomes `POST /acl/entries`. An API entry that is not in the intent, or an extra duplicate, becomes `DELETE /acl/entries/{id}`. A changed entry is a delete plus a create, because ACL entries cannot be edited.

Deletes run first, then VLAN creates and changes, then ACL creates. After applying, the script reads the state again and plans again. The plan must be empty, otherwise it exits with an error.

### 2. Why is each request safe to retry, or why is it not retried?

| Request | Retried? | Why |
|---|---|---|
| `GET` (reads) | Yes | It changes nothing. |
| `PUT /vlans/{id}` | Yes | It sets the desired state, so repeating it has the same effect as doing it once. |
| `DELETE` (VLAN or ACL entry) | Yes | The end state is the same. A 404 on a retry means it is already gone, so the script counts it as success. |
| `POST /acl/entries` | Yes, with a key | POST is not idempotent. Every attempt carries the same `Idempotency-Key`, so the server returns the first result and adds nothing. |
| `POST /auth/token` (login) | Yes | It only returns a token and changes nothing on the network. |

Retries happen only for 429 (wait `Retry-After`), 503 and lost replies (backoff with jitter, up to 6 tries), and 401 (log in again, then resend). Every other error (400, 403, 404 on a read, 409, 422) is **not** retried: the same request would fail again, so the script stops and prints it.

### 3. VLANs have PUT; ACL entries only have POST. How did that change the design, and how is each key chosen?

PUT means "make it so", so VLAN changes are naturally safe to retry and need no key. ACL entries have no PUT or PATCH, so a changed rule must be deleted and created again, and the create is a POST that needs an `Idempotency-Key`.

The key is `sync-<run id>-<hash of the entry>`. The run id is a random value made once per run, and the hash comes from the entry's fields. The key is built **once**, before the first attempt, and every retry of that POST reuses it.

- **A new key for every attempt:** if the first attempt was applied but its reply was lost, the retry would carry a new key, so the server would treat it as a new request and add a second identical rule.
- **The same key for every run:** if an entry was added, later deleted, and then needed again, the old key would still be remembered. The server would replay the old result and add nothing, so the script would never reach the intended state. The run id prevents this. Using the same key with a different body is also rejected with 422.

### 4. What happens if two people run `sync.py` with different intents at the same time?

Each run reads the state, plans, and writes without any lock, so the two runs would undo each other. For VLANs, the last write wins. For ACL entries, one run deletes the entries the other just created, and if both want the same entry they can create it twice, because their keys differ (different run ids). The final verify step would notice that the state does not match and exit with an error, but it cannot fix it.

To make this safe, the API would need a way to stop concurrent changes: a lock or lease on the configuration, or optimistic concurrency (a version or ETag on the collection with `If-Match`, so a stale write is rejected). Replacing a whole collection in one atomic call, instead of many separate writes, would also help.

### 5. Where are the credentials, and how do I know none is in the repository?

The username and password are only in the environment (`LAB_USERNAME` and `LAB_PASSWORD`), which the script reads with `os.environ`. The token is requested at run time and kept only in memory. The script never prints a token or password, so none is in the evidence files either. To check, I ran `git grep -n -i -E "password|token|lab_[0-9a-f]{8}"` and `git status` before committing, and `.gitignore` lists any `.env` file.

## AI use

I used an AI assistant (Claude) for explanations of the lab concepts and for help drafting and debugging `retry_safe.py`, `inventory.py` and `sync.py`. I ran the scripts against the lab API myself, produced the evidence files, and reviewed the answers above.