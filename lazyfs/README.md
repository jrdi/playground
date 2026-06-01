# lazyfs

A toy lazy-loading, content-addressed FUSE filesystem — a laptop-sized reproduction of how
systems like [Modal](https://modal.com), [stargz/SOCI](https://github.com/awslabs/soci-snapshotter),
and [Nydus](https://github.com/dragonflyoss/nydus) make giant container images start *instantly*:
instead of downloading the whole image up front, they mount a tiny **metadata index** and fetch
file contents **lazily, on first read**, from a **content-addressed** blob store.

It's small on purpose — it exists to *show* the mechanics, not to be a real filesystem.

```
build_image.py   package a directory -> a tiny index + a content-addressed chunk store
lazyfs.py        a FUSE filesystem that mounts the index instantly, fetches on read
image.py         the shared on-disk model (the typed nodes + JSON boundary)
```

## The idea

A normal container runtime downloads and unpacks **every byte** of an image before the container
starts. But a process usually reads only a fraction of those bytes. So:

1. **Package** the image as a content-addressed store. Split each file into fixed-size chunks, hash
   each chunk, store the chunk under its hash. The *image* is then just an **index**: the directory
   tree plus, per file, the ordered list of chunk hashes — **no file bytes**. (`build_image.py`)
2. **Mount the index** through FUSE. Every file appears, with the right size and metadata, but
   nothing is downloaded. (`lazyfs.py`)
3. **Fetch on read.** When a process actually `read()`s a file, the FUSE server fetches only the
   chunks that read touches, and caches them. Files nobody opens are never fetched.

Content-addressing (hash = address) is what makes the chunk store **dedup for free** (identical
chunks resolve to one blob) and every cache layer **immutable** (a hash can't change under you, so
there is no invalidation).

## Requirements

- Linux with FUSE (`/dev/fuse`, `fusermount`) and `libfuse2`.
- Python 3.14+ and [`fusepy`](https://github.com/fusepy/fusepy).

```bash
uv sync                 # or: pip install fusepy
```

## Run it

Create a small sample tree (note the deliberate `cp`, so two files share blobs and dedup has
something to catch):

```bash
mkdir -p scratch/src/subfolder
head -c $((4 * 1024 * 1024)) /dev/urandom > scratch/src/big_file.bin   # 4 MiB
head -c $((256 * 1024))      /dev/urandom > scratch/src/file_a.bin     # 256 KiB
cp scratch/src/file_a.bin scratch/src/file_b.bin                      # identical -> shared blobs
printf 'hello\n' > scratch/src/note.md
printf 'nested\n' > scratch/src/subfolder/note.md
```

Package it into an index + chunk store:

```bash
uv run python build_image.py scratch/src scratch/image
#   files / dirs        : 5 / 2
#   logical size        : 4.50 MiB
#   store size on disk   : 4.25 MiB  (dedup 1.06x)
#   index size          : 6.7 KiB   <- this is all that mounts
```

Mount the index (runs in the foreground; use a second terminal for the commands below):

```bash
mkdir -p scratch/mnt
uv run python lazyfs.py scratch/image/image.json scratch/image/store scratch/mnt
```

Now watch it stay lazy:

```bash
ls -lR scratch/mnt          # every file + size shows up — and zero chunks were fetched
cat scratch/mnt/note.md     # one [fetch] in the server log
dd if=scratch/mnt/big_file.bin of=/dev/null bs=64K skip=32 count=1   # a 64 KiB read from the
                            # middle of a 4 MiB file fetches ONE chunk, not 64
```

Unmount when done:

```bash
fusermount -u scratch/mnt
```

The server logs a `[mount]` line, then a `[fetch]` per chunk pulled from the store and a `[hit]`
per cache hit — so you can watch laziness happen in real time.

## How this maps to the real systems

| This toy | Modal / stargz / SOCI / Nydus |
|---|---|
| `image.json` index, no bytes | image index / TOC, mounted in ~ms |
| content-addressed `store/<hash>` | content-addressed blob store, dedup across images |
| in-memory `chunk_cache` | tiered cache: page cache → SSD → zonal → CDN → blob |
| a `[fetch]` per touched chunk | real network fetches to the blob store / CDN |

## Deliberately left out

The lazy-fetch core is genuinely a weekend project — *these files*. The hard part is everything
wrapped around the filesystem, which is where the real engineering lives:

- A real distributed, multi-tier cache (zonal/regional/CDN) and the ops to run it.
- Integration with a container runtime (a containerd snapshotter, or a gVisor sandbox).
- Compression + encryption of chunks (this stores them raw, for clarity).
- The throughput tuning (concurrency, read-ahead window, request size) — and why one of those
  levers is out of reach from a Python FUSE binding at all.
- **Writes.** This image is read-only. Mutable files on top of immutable content-addressed
  storage, done consistently and globally, is the genuinely deep distributed-systems problem.

That split is the whole point: **FUSE is the small, learnable nucleus; the moat is the caching
fabric, the runtime integration, and the tuning bolted around it.**
