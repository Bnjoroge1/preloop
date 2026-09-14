# Internal opportunities

## Source-state webhook reconciliation

**Status:** deferred; intentionally removed from the active runtime.

The delivery watchdog repairs webhook deliveries that GitHub recorded but
Preloop did not receive. A separate source-state reconciler is interesting for
the remaining failure mode: GitHub never generated a delivery, so there is no
GUID for the watchdog to redeliver.

A future reconciler could compare repository state with runs known to Preloop
and identify an untested default-branch or open-PR head. It must be treated as
an eventual current-head safety net, not as exact GitHub event replay.

Reasons to keep it out of the runtime for now:

- Current branch/PR state cannot reconstruct multi-commit push history.
- Synthetic push payloads cannot reliably reproduce changed-file lists or
  `[skip ci]` markers from earlier commits, so `paths:` and skip behavior can
  differ from GitHub.
- A current PR head only supports an approximation such as `synchronize`; it
  cannot recreate historical actions such as `opened` or `labeled`.
- A late real webhook and a synthetic delivery need one shared atomic
  idempotency decision. A grace period alone is not a correctness guarantee.
- Reservation and queue insertion must be atomic, or a failed enqueue can
  suppress future repair.
- Lease loss must stop all workflow and external Check Run side effects before
  another worker can reclaim the delivery.

Before reconsidering this, define and test the contract explicitly:

1. Decide whether the product wants current-head coverage, exact GitHub
   semantics, or only an operator alert. Only the first is practical with
   polling.
2. Make real and synthetic delivery/run identity share an atomic store-level
   deduplication boundary.
3. Resolve changed paths from a trustworthy commit range, or make unknown-path
   behavior explicit instead of treating an empty list as known.
4. Add fault-injection tests for delayed real deliveries, queue failures after
   reservation, restarts, force-pushes, and skip markers outside the head
   commit.
5. Keep the feature opt-in and expose synthetic runs clearly in operator
   surfaces if it is ever enabled.
