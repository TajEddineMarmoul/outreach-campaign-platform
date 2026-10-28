# Shared sender scheduling

Every canonical Gmail sender has one durable clock shared by all its campaigns,
including campaigns using overlapping sender groups. There is no fixed 30-second
gap and no workspace-wide simultaneous-account limit.

For campaign `c`, sender `a`, full-window target `N`, window duration `T` seconds,
and `M` connected accounts in the group, the sender's share contributes
`r[c,a] = N / (T * M)` emails per second. The account's total rate is the sum
of its active campaign rates and its slot interval is `1 / total_rate`.
Explicit fixed-delay campaigns contribute one email per configured batch delay
per sender. Zero-delay sending is represented at millisecond resolution.

The dispatcher uses virtual-finish tags to divide turns proportionally without
updating every campaign's score on each send. A new or edited lane starts at the
account's current virtual time plus one stride (`1 / lane_rate`). After service,
its next tag advances by its stride. Earliest window end and campaign ID break
ties. The account clock preserves fractional progress when its rate changes.
Actual attempts anchor the next slot to the start of the Gmail call, after
rendering and credential refresh. Missed slots are capped at one due opportunity;
restarts, late launches and live edits never replay a backlog in a burst.

## Data and query costs

- `sender_dispatch`: one aggregate rate, clock, virtual time and fenced lease per
  account. It is locked only for short database transactions, not Gmail calls.
- `sender_campaign_lanes`: one edge per campaign/account allocation. The fair
  queue index starts with sender, active/selectable state and virtual-finish tag.
- Window keys group identical boundary shapes independently of target, pacing
  or sender count. A due matching group is advanced with set-based SQL. Targets
  are read from each row's JSON, so different targets retain different rates.
- Due accounts, due boundaries, recipient eligibility, reservations and daily
  counters have supporting indexes. No email scans the entire campaign table,
  recomputes a global rate sum, or loads/locks all siblings in a sender group.
- Bootstrap and maintenance are bounded. Already active campaigns are adopted
  in pages. A changed sender group's edges are invalidated once and rebuilt in
  pages; unrelated campaigns are not replanned. Configuration work scales with
  affected allocations, and a bulk boundary update scales with changed rows.

This avoids a fixed campaign-count limit. Storage still grows with the number of
campaign/account edges. A million stored or queued campaigns does not imply a
million emails per second: sender quotas, database capacity, Gmail latency and
available workers remain real throughput limits.

## Safety and lifecycle

The account lease prevents concurrent workers using the same sender. Lease tokens
are checked immediately before delivery; expired workers cannot release newer
reservations. Sender, campaign, membership, windows and quotas are rechecked after
preparation, before calling Gmail. Independent sender accounts remain independent.
The Google API client's default 60-second transport timeout is shorter than the
15-minute reservation lifetime; stale-job recovery remains enabled.

Pausing or changing a campaign replans its own edges transactionally. Natural
completion after consuming the full daily target retains its planned density
until the current boundary, preserving neighbouring campaigns' last slots. A
campaign finishing with fewer recipients releases its unused contribution.
Deletion removes retained contributions. Unfinished campaigns advance to their
next enabled window. Account errors retain the existing cooldown across campaigns.

Campaign target values remain full-window averages and daily maxima. Late starts,
transport failures, blocked accounts and worker delays can produce fewer sends.
Window-local displayed timestamps are approximate; the persistent clock uses
microsecond timestamps rounded upwards to avoid dispatch before a boundary.

## Validation

Run the mathematical/worker examples with:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_shared_dispatch.py -q
```

PostgreSQL locking and migration checks use newly created schemas, no production
tables or email calls. They require explicit opt-in and remove their own schemas:

```powershell
$env:OUTREACH_TEST_POSTGRES = '1'
.venv/Scripts/python.exe -m pytest tests/test_shared_dispatch_postgres.py -q
```

The on-disk, isolated SQLite benchmark exercises a million real campaign and queue
rows, checks the fair-queue query plan, and measures a coalesced opening boundary:

```powershell
.venv/Scripts/python.exe scripts/benchmark_shared_dispatch.py --campaigns 1000000
```

It measures scheduler database work, not Gmail throughput or production latency.
The million-row Windows SQLite run measured 2.977 ms median and 3.695 ms p95
for 100 selections, with five SQL statements per selection. Opening all one
million matching windows took 20.321 seconds and eleven SQL statements. These
figures describe that local run; production PostgreSQL needs its own capacity
measurements for a deployment of that size.
The serverless worker is still bounded per invocation. Its once-per-minute trigger
can delay slots; a continuously running worker or more worker invocations provides
finer timing and more aggregate throughput without bypassing account leases.

Apply additive migration `0018_shared_sender_dispatch` before deploying this code.
Old servers ignore the new tables/columns. The rollback switch
`OUTREACH_SHARED_DISPATCH=false` retains the former batch path for rollback and
legacy regression tests. It is not a user-facing timing or concurrency setting.
