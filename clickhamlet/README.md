# ClickHamlet, the smallest version

A server that speaks ClickHouse's native protocol well enough for `clickhouse client` to connect and feel connected,
and does nothing else: every query comes back with its kind.

```
$ clickhouse client --port 9001 --user anyone --password anything
ClickHouse client version 26.9.19.9 (official build).
Connecting to localhost:9001 as user anyone.
Connected to ClickHouse server version 26.10.1.

hello :) SELECT * FROM users LIMIT 10

┌─type───┐
1. │ Select │
   └────────┘

1 row in set. Elapsed: 0.001 sec.
```

It's about 140 lines of C++ in `main.cpp`, and almost none of it is protocol code of its own: it links ClickHouse's
`dbms` library and uses its wire format (`readVarUInt`, `NativeReader`, `NativeWriter`), compression, settings, parser
and exceptions. It is where [ClickHamlet](https://clickhamlet.com), a router that sends each query to the backend that
should run it, started.

## What it answers

| Query | Answer |
| --- | --- |
| `SELECT count() FROM events WHERE day = today()` | `Select` |
| `INSERT INTO events SELECT * FROM staging` | `Insert` |
| `CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x` | `Create` |
| `ALTER TABLE t ADD COLUMN y String` | `Alter` |
| `DROP TABLE t`, and `TRUNCATE TABLE t` too | `Drop` |
| `SHOW TABLES` | `Show` |
| `SYSTEM FLUSH LOGS` | `System` |
| `USE db; SELECT 1; EXPLAIN SELECT 1` | `Use`, `Select`, `Explain` |
| `SELEC 1` | ClickHouse's own `Syntax error` |

The kind is `IAST::getQueryKind` from ClickHouse's parser.

## Build

ClickHouse has to be the top-level CMake project, so the server is added to ClickHouse's build as one more target,
through `CMAKE_PROJECT_ClickHouse_INCLUDE` (`clickhamlet.cmake`). `build.sh` configures ClickHouse without its
optional libraries and builds the server in `build/`:

```sh
./build.sh                      # fetches ClickHouse v26.10.2.103-stable into ClickHouse/, once
./build.sh ~/src/ClickHouse     # or builds against a checkout you have, with its submodules
build/clickhamlet 9001
clickhouse client --port 9001 --user anyone --password anything
```

The first build compiles most of ClickHouse and takes a long while. Later ones only rebuild `main.cpp`. A `build/`
belongs to one source tree: to build against another, remove it first.

On macOS: `brew install llvm lld ninja findutils grep ccache`, and `export PATH="$(brew --prefix llvm)/bin:$PATH"`.

## How it works

1. **Hello.** The client sends its name, version, database, user and password. The server ignores them, so any user
   and password connect, and answers with its own name, version, time zone and protocol revision.
2. **The revision.** The server advertises `DBMS_MIN_PROTOCOL_VERSION_WITH_PARAMETERS`, an old revision every client
   still speaks. Clients then leave out the fields added since, which only servers talking to each other need, and
   the handshake ends with a single field, the quota key.
3. **Query.** Its ID, the client's info, settings, stage, whether data is compressed, the query text and its
   parameters. After it come the query's external tables, ended by an empty block, which have to be read even though
   there are none.
4. **The answer.** A `Data` packet with one block of one `String` column, `type`, then `EndOfStream`. A query that
   does not parse gets an `Exception` packet instead, written by ClickHouse's `writeException`, so the client shows
   it as it would a server's.
5. **Ping** gets a `Pong`; anything else is ignored.

## Quirks

- **"Warnings: \* Select" when you connect.** The interactive client asks the server for its warnings
  (`system.warnings`) when it connects, and gets back the kind of that query. Clients send queries of their own.
- **"ClickHouse client version is older than ClickHouse server."** The server says it is 26.10. Change the version
  numbers in its `Hello` if that bothers you.
- **`INSERT ... VALUES` doesn't work.** The client waits for the table's structure before sending rows, and this server
  never sends one, so the kind comes back as data instead. `INSERT ... SELECT` works.
- **No cancel, no shutdown.** Each query is answered at once, so there's nothing to cancel, and Ctrl-C kills the
  server.
