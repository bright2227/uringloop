# Phase 1 native request state machine

The native core uses an explicit request lifecycle so a Python reference can
never disappear while the kernel may still dereference an SQE buffer or emit a
CQE for the request.

## States

| State | Meaning |
| --- | --- |
| `prepared` | An SQE exists in userspace but has not been submitted. |
| `submitted` | The kernel owns the operation and may emit CQEs. |
| `cancel_pending` | An asynchronous cancel SQE was submitted, but the target operation still owns its resources until its own final CQE. |
| `operation_complete` | The operation CQE was observed. This is terminal for one-shot operations but not for zero-copy sends whose buffer notification is pending. |
| `notification_pending` | The operation result is known, but a later `IORING_CQE_F_NOTIF` CQE still controls buffer lifetime. |
| `released` | The final CQE or ring teardown released kernel ownership. |

Multishot operations remain `submitted` or `cancel_pending` while
`IORING_CQE_F_MORE` is present. Their final CQE passes through
`operation_complete` and releases the request. Notification-producing
operations pass through `operation_complete`, remain `notification_pending`
when the operation CQE has `IORING_CQE_F_MORE`, and release only after the
`IORING_CQE_F_NOTIF` CQE.

## Ownership invariants

- The ring holds one strong reference from preparation through the final CQE.
- Each immutable `Completion` snapshot holds its own strong reference to the
  request, so batched completions never alias mutable result fields.
- A cancel request holds its target strongly, but cancellation completion does
  not release the target. The target's own final CQE does that, regardless of
  which CQE wins the race.
- `Ring.close()` marks every owned request cancelled and released before
  dropping the ring references.
- CQE flags and the request's declared completion model determine lifetime;
  an error result alone never predicts whether another notification follows.

The first concrete operations are NOP, relative timeout, and asynchronous
cancel. Later Phase 1 branches assign the same completion models to multishot
and zero-copy operations rather than adding a second ownership mechanism.
