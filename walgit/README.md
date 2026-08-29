# walgit — git at any scale, as a take-home

Rebuild the architecture from Cursor's [Git at any
scale](https://cursor.com/blog/git-at-any-scale): a git server whose only
durable state is objects in S3, where every push is made durable *before* it is
acknowledged, ordering comes from an atomic compare-and-swap instead of a
consensus protocol, and the repository on disk is a disposable cache.

You implement 30 functions. Real `git push` and `git clone` work against the
result. There is no simulation anywhere: MinIO is a real S3 API, `git
http-backend` is the same CGI that runs behind Apache on a real git host, and
the push path runs inside a real `pre-receive` hook with real object quarantine.

> **Naming, up front.** In the blog and in this codebase, **CAS means
> compare-and-swap**. The content-*addressed* blob store is called "the object
> store" (`walgit/objects.py`) and is never abbreviated. The two ideas sit next
> to each other in every file here, so the abbreviation is reserved for one.

---

## The design

```
      git push                                          git clone
         │                                                  ▲
         ▼                                                  │
   ┌────────────────────────────────────────────────────────────────┐
   │  walgit node        warm cache: <cache>/<repo>.git             │
   │                     (deletable at any time — never the truth)  │
   └───────┬────────────────────────────────────────────────┬───────┘
           │ pre-receive hook                     sync: conditional GET
           │                                      304 → serve immediately
           ▼                                                ▲
   ┌────────────────────────────────────────────────────────┴───────┐
   │  S3                                                            │
   │                                                                │
   │   repos/<id>/index.json          ← the compare-and-swap target │
   │        { base, entries[], refs }   one PUT with If-Match wins   │
   │                                                                │
   │   repos/<id>/wal/<entry>.json    ← immutable push records      │
   │   repos/<id>/objects/sha256/…    ← immutable packfiles         │
   └────────────────────────────────────────────────────────────────┘
```

A push, in order:

1. `receive-pack` stages the incoming objects in a **quarantine** directory —
   on disk, invisible to the repository.
2. `pre-receive` uploads that packfile to the object store, keyed by its hash.
3. It writes an immutable **WAL entry**: which refs move, from what, to what.
   Durable now. Still unreachable.
4. It **compare-and-swaps** `index.json` with `If-Match: <etag>`, splicing in a
   pointer to that entry. This is the linearization point of the whole system.
   One writer wins; everyone else gets a 412, reloads, re-validates, retries.
5. Only then does the hook exit zero, and only then does git update its refs
   and tell the client the push succeeded.

Crash anywhere before step 4 and the push simply never happened — there is an
orphan pack in S3 that the GC will sweep, and no reader could ever have seen it.
There is no window where a push is half-visible, and none where we acknowledge
something we have not persisted.

**Six invariants.** Every exercise is really about one of these:

| # | Invariant | Where it bites |
|---|-----------|----------------|
| 1 | Durable before visible: upload, *then* swap | `WalLog.append` |
| 2 | The WAL re-validates ref old-values; git's local check is not enough | `WalIndex.apply_push` |
| 3 | Retry on `CasConflict`, never on `RefConflict` | `append`, `compact` |
| 4 | Objects before refs, always | `LocalRepo.sync` |
| 5 | Never delete at swap time; delete later, with a grace period | `gc` |
| 6 | Nothing in `index.json` grows with the size of a push | `WalPointer` |

---

## Setup

```bash
make up          # MinIO in docker
make install     # venv + deps (uv)
make preflight   # exercise 0 — must pass before anything else
```

`make preflight` proves the three primitives the design rests on: `If-None-Match: *`
creates exactly once, `If-Match` rejects a stale write with 412, and a
conditional GET answers 304. If your MinIO is too old for conditional writes
these fail, and nothing downstream can be made to work.

## How to work

Every blank raises `NotImplementedError` with its exercise number and has a
docstring stating the contract. Run `make todo` to see what is left.

```bash
make todo        # every unimplemented blank
make t1          # exercise 1 tests   (t2, t3, t4 likewise)
make test        # everything
make peek EX=2.3 # the reference solution for one function
make peek EX=2   # ...or a whole exercise
make solve       # give up on everything, install the reference
make reset       # restore the blanks
```

Work in order — each exercise is used by the next. Rough sizes: exercise 1 is a
warm-up, 2 is the core idea, 3 is the hardest, 4 is the payoff where real git
starts working.

---

## The exercises

### 0 — Preflight · `tests/test_00_preflight.py`
Nothing to implement. Read it: it is the argument for why this architecture is
possible at all.

### 1 — Content-addressed object store · `walgit/objects.py` → `make t1`

| Blank | Function | The point |
|-------|----------|-----------|
| 1.1 | `digest_bytes` | `"sha256:<hex>"` everywhere; the algorithm is part of the digest |
| 1.2 | `digest_file` | hash without loading a 2 GiB pack into RAM |
| 1.3 | `key_for` | two levels of hex fan-out; reject non-sha256 input |
| 1.4 | `exists` | a 404 is an answer, not an error |
| 1.5 | `missing` | concurrent HEADs — serial round-trips would dominate push latency |
| 1.6 | `get` | verify on read, or content addressing bought you nothing |
| 1.7 | `download` | stream, hash as you go, rename last — a partial file must never look complete |
| 1.8 | `put` | `If-None-Match: *`; treat 412 as "already there", not as failure |
| 1.9 | `put_file` | multipart above the threshold, where conditional writes do not apply |

Hint for 1.8: this is the first place you will use `s3.is_precondition_failed`.
It will not be the last.

### 2 — WAL + compare-and-swap index · `walgit/wal.py` → `make t2`

| Blank | Function | The point |
|-------|----------|-----------|
| 2.1 | `_apply_updates` | ref old-value validation → `RefConflict` |
| 2.2 | `WalIndex.refs_at` | replay from the base's ref snapshot, using the entry objects you are handed |
| 2.3 | `WalIndex.apply_push` | pure, returns a *new* index, idempotent on `entry_id` |
| 2.4 | `WalLog.load` | "no index object" means "empty log", not an error |
| 2.5 | `WalLog.load_if_changed` | conditional GET; `None` means 304 |
| 2.6 | `WalLog.commit` | `If-Match` when it exists, `If-None-Match: *` when it does not |
| 2.7 | `WalLog.append` | the retry loop, in the right order |

The two tests to think hardest about are
`test_concurrent_pushes_to_distinct_refs_all_land` (eight nodes pushing at once
must produce a gapless sequence) and
`test_concurrent_pushes_to_the_same_ref_pick_exactly_one_winner` (exactly one
survivor, no lost update). If you re-use a stale index across retries, the first
fails. If you validate refs once instead of on every attempt, the second fails.

### 3 — Compaction and GC · `walgit/compact.py` → `make t3`

| Blank | Function | The point |
|-------|----------|-----------|
| 3.1 | `should_compact` | two triggers: entry count and incremental bytes |
| 3.2 | `plan_compaction` | pin a prefix; snapshot the refs *as of* the cut (replayed from entry objects), not "now" |
| 3.3 | `apply_compaction` | splice: replace the prefix, keep the tail, refuse if the world moved |
| 3.4 | `_repack_snapshot` | build the pinned snapshot, `git repack -a -d -f`, upload |
| 3.5 | `compact` | repack, then swap with retries |
| 3.6 | `unreferenced_packs` | liveness = base + live entries |
| 3.7 | `gc` | grace period, for packs *and* for WAL entry objects |

The hard test is `test_pushes_during_the_repack_are_not_lost`: a client pushes
while you hold a finished pack but have not swapped yet. Repacking a pinned
snapshot rather than "the current state" is what makes that survivable.

The subtle one is `test_grace_period_protects_an_in_flight_push`: a pack
uploaded by a push that has not yet won its swap is indistinguishable from
garbage. Only its age tells them apart.

### 4 — Serving real git · `walgit/repo.py`, `walgit/hooks.py` → `make t4`

| Blank | Function | The point |
|-------|----------|-----------|
| 4.1 | `LocalRepo.sync` | 304 → return immediately; otherwise objects, then refs; all under `self.lock()`, which pushes hold too |
| 4.2 | `_fetch_packs` | download and `git index-pack` what you do not have |
| 4.3 | `_prune_packs` | drop packs the index stopped referencing |
| 4.4 | `materialize_snapshot` | rebuild a repo at a pinned seq: packs from the index, refs from the caller |
| 4.5 | `parse_updates` | git's zero-oid means both "absent" and "delete" |
| 4.6 | `find_quarantine_pack` | `$GIT_QUARANTINE_PATH`; a delete-only push has no pack, and loose objects are refused |
| 4.7 | `pre_receive` | the whole push path; non-zero exit = rejected push |

`walgit/server.py` and `walgit/gitcmd.py` are given. Read `server.py` — it is
30 lines of CGI glue, and it shows exactly how little a git server has to do
once the storage layer is right.

---

## Running it for real

```bash
make demo        # narrated end-to-end: push, clone, compact, gc,
                 # delete the server's disk, clone again anyway
```

Or drive it yourself:

```bash
make serve                                   # :8099
git clone http://127.0.0.1:8099/myrepo
cd myrepo && echo hi > a.txt && git add -A && git commit -m one
git push origin main                         # watch the hook's output
make show REPO=myrepo                        # the WAL index in S3
make compact REPO=myrepo
make gc REPO=myrepo
rm -rf .walgit-cache/myrepo.git              # the truth is in S3
git clone http://127.0.0.1:8099/myrepo again # ...still there
```

MinIO console: <http://127.0.0.1:9001>, `walgit` / `walgitwalgit`. Watching
`repos/<id>/index.json` change while you push is worth doing once.

---

## Deliberately not here

Single node, by choice. The blog's multi-node layer — rendezvous hashing to
pick which node warms which repository, UDP gossip to nudge replicas, replicas
that download compacted packs instead of repacking — sits *on top* of what you
build here and changes none of it. The WAL is still the source of truth; a
replica is just another warm cache doing the same conditional GET.

Also skipped: authentication, protocol-v2 ref filtering on the server side,
multipart uploads for packs above 5 GiB, and cross-repository dedup (the object
store is namespaced per repo, which makes GC single-repo and therefore simple —
sharing it between forks is a real design question, not an oversight).

Stretch goals, roughly in order of interest:

1. Take invariant 6 one level further. Pointers no longer grow with a push, but
   `index.refs` still grows with the repository: at 100,000 refs every push
   rewrites about 6.6 MiB. Move the ref map into an immutable, content-addressed
   snapshot object, keep only its digest in the index, and work out what
   `apply_push` then has to fetch before it can validate a push.
2. Make compaction incremental: keep the N most recent entries hot
   (`keep_recent`) and measure clone latency against pack count.
3. Add a second node against the same bucket and find out whether your
   `apply_push` really is the linearization point.
4. Swap MinIO for S3 Express One Zone and see whether the bottleneck moves from
   the network to `git repack`, as the blog claims it does.
