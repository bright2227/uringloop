# Phase 1 provided-buffer pool

`Ring.setup_buffer_pool()` allocates one fixed, bounded memory slab and
registers an io_uring provided-buffer ring. The number of entries must be a
power of two no greater than 32,768. The buffer size and group ID are validated
before allocation or registration.

The ring reports `buffer_pool_entries`, `buffer_size`, `buffer_pool_bytes`,
`buffers_available`, and `buffers_in_use`. These counters make the pool's
memory cost and backpressure state explicit. A second pool cannot be registered
on the same ring.

Every slot is added to the shared buffer ring once during setup. Native receive
operations consume the buffer ID reported in `IORING_CQE_F_BUFFER`; ownership
then moves from `buffers_available` to `buffers_in_use`. Returning or releasing
the consumer object adds that exact slot back to the ring. If no slot is
available, the kernel completes a buffer-select receive with `-ENOBUFS`; the
core does not allocate an unbounded replacement.

Materializing `bytes` from a selected buffer necessarily allocates and copies.
The memoryview path instead keeps the native consumer object and ring alive
until the view is released, at which point the slot can safely return to the
kernel. Ring closure is rejected while consumer-owned slots remain in use.

Pool registration and accounting are introduced before receive operations so
the later I/O branch reuses one ownership mechanism rather than embedding
buffer allocation in each request type.
