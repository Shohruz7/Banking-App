# Load test results

Run with `make load` against the stack `make up` brings up. Re-runnable; these are the numbers from
the run recorded here, not a target.

## Environment

**Read this before quoting any number below.** It describes a laptop, not the production box.

| | |
|---|---|
| Host | Apple Silicon laptop, Docker Desktop (Linux VM) |
| Topology | the real one — nginx → two app replicas (2 uvicorn workers each) → Postgres 16 / Redis 7, all containers |
| Measured from | a `grafana/k6` container on the compose network, addressing `web` by service name |
| Dataset | `seed_demo --seed 1` — 400 customers, 2,427 accounts, 13,011 entries, 27,228 journal lines |
| Postgres | `fsync=off` (the CI override), so write latency is optimistic |
| Throttles | **on**, at their shipped rates (ADR-0015) |

Two caveats that matter more than the rest. `fsync=off` makes the transfer figures better than a
durable box would give. And an M-series laptop is faster than a `t3.small`, which is burstable — a
sustained run there would drain CPU credits and degrade, which is the single most interesting thing
this profile would find on real hardware and cannot find here.

## Profile

100 read VUs ramped over 2m50s plus 10 write VUs, each VU pinned to its own seeded customer and
paced inside that customer's own budget: reads at 2 rps against a 4 rps ceiling, writes at 0.33 rps
against 0.5. Read mix weighted toward the endpoints that derive — 40% `/accounts/`, 30%
`/portfolio/`, 20% `/holdings/`, 10% `/orders/`.

## Results

**21,585 requests at 125.3 rps, 0.00% failed, 0 throttled, and the ledger still balanced.**

| Endpoint | median | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| `/accounts/` (derived balances) | 5.4 ms | 15.8 ms | **23.6 ms** | 43.2 ms | 99.7 ms |
| `/holdings/` (derived + valued) | 5.6 ms | 16.6 ms | **24.4 ms** | 42.1 ms | 78.3 ms |
| `/orders/` (plain indexed read) | 5.6 ms | 16.3 ms | **26.3 ms** | 40.1 ms | 105.8 ms |
| `/portfolio/` (three aggregates) | 6.9 ms | 18.4 ms | **26.4 ms** | 42.9 ms | 79.4 ms |
| `POST /transfers/` (locked write) | 14.0 ms | 33.4 ms | **52.3 ms** | 81.2 ms | 126.9 ms |
| all | 6.0 ms | 17.7 ms | **25.6 ms** | 44.2 ms | 126.9 ms |

`check_ledger_invariants` passes after the run — zero-sum, share conservation, no negative asset
balance, no negative holding — across the ~570 transfers the write scenario posted.

The result worth reading is the **comparison between rows, not the rows themselves**: `/portfolio/`
derives cash, holdings, valuation and realized P&L on every request and lands within 5 ms of
`/orders/`, which is an indexed filter with a `select_related` and derives nothing. At this data
size, deriving balances on read costs almost nothing. That is a claim about 27,000 journal lines,
not a general one — the cost of a `SUM` grows with an account's lifetime line count, and one run at
one size cannot show where that stops being true.

## The three topologies, measured

Same profile, same dataset, same machine — run once before the connection pool, once after, and
once after the second app replica.

| | 1 replica, no pool | 1 replica, pooled | 2 replicas, pooled |
|---|---|---|---|
| failed | 5.35% | 0.00% | **0.00%** |
| median | 20.7 ms | 9.0 ms | **6.0 ms** |
| p95 | 108.5 ms | 48.9 ms | **25.6 ms** |
| p99 | 168.0 ms | 87.9 ms | **44.2 ms** |
| throughput | 120.1 rps | 123.3 rps | **125.3 rps** |
| peak Postgres connections | 100 (exhausted) | 26 | **33** |

Throughput barely moves because the harness paces itself — it is pinned near 123 rps by the rate
limits, not by the server. Latency is where the change shows: the pool halved it by removing
connection churn, and the second replica halved it again by doubling the worker count behind nginx.
The replicas exist for gapless releases (ADR-0043); the latency was a side effect.

The `t3.small` caveat gets sharper here, not softer. Four gunicorn workers plus two Celery forks on
2 burstable vCPUs is a different machine from the one these numbers came off.

## What the first run found

The first run of this harness failed, and the failure was the point.

**5.35% of requests returned 500.** Not throttling — `throttled_429` was zero throughout. Postgres
was refusing connections:

```
psycopg.OperationalError: connection failed: FATAL: sorry, too many clients already
```

Django's ASGI handler runs each sync view in its own thread, and a Django connection is
thread-local, so the connections one process holds are bounded by requests *in flight* rather than
by worker count — and `CONN_MAX_AGE = 60` then held each one open for a further minute. At 100
concurrent readers across two workers that went past Postgres's 100-connection ceiling. Nine weeks
of tests could not have found this: the suite runs one request at a time.

The fix was a psycopg 3 connection pool (`DB_POOL_MAX`, 20 per process), which bounds what a
process can hold and makes an overloaded app queue for a connection rather than fail to get one.
Raising `max_connections` would only have moved the cliff.

| | before | after |
|---|---|---|
| failed requests | 5.35% | **0.00%** |
| median | 20.7 ms | **9.0 ms** |
| p95 | 108.5 ms | **48.9 ms** |
| p99 | 168.0 ms | **87.9 ms** |
| peak Postgres connections | 100 (exhausted) | **26** |

Latency more than halved. Connection churn, not query cost, had been the dominant expense.

## Two harness bugs, also worth recording

Both produced failures that looked like findings and were not.

**Unfunded accounts.** The minter took each customer's first two spendable accounts without
checking balances, and the seed leaves some savings accounts empty, so part of the write budget went
on transfers correctly rejected for insufficient funds. It now requires both accounts funded.

**Idempotency keys repeating across runs.** Keys were `load-<vu>-<iteration>`, which collide between
runs against the same database — same key, different transfer, which is a 409 by design, because
ADR-0024 binds a key to a digest of its payload rather than letting it replay the wrong movement.
The harness was wrong and the ledger was right. Keys now carry a per-run nonce, and
`transfer_conflicts` is a threshold so a future recurrence fails the run instead of being absorbed
into the error rate.

## What this does not measure

- **Anything on the target hardware.** No EC2 instance exists yet; re-run there before quoting.
- **Sustained load.** Under three minutes, which is far too short to see `t3` credit exhaustion.
- **Growth.** One data size. The interesting curve for a derived-balance design is latency against
  an account's lifetime line count, which needs several seeded sizes.
- **WebSockets.** HTTP only. `deploy/smoke_socket.py` covers socket delivery, but not under load.
- **The breaking point.** Every run stayed inside the rate limits on purpose, so this establishes
  the stack is comfortable at 123 rps — not where it stops being comfortable.
