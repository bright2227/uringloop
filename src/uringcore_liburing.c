#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <liburing.h>
#include <math.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/eventfd.h>
#include <unistd.h>

#include "uringcore_protocol.h"

typedef enum {
    URINGCORE_REQUEST_PREPARED = 0,
    URINGCORE_REQUEST_SUBMITTED,
    URINGCORE_REQUEST_CANCEL_PENDING,
    URINGCORE_REQUEST_OPERATION_COMPLETE,
    URINGCORE_REQUEST_NOTIFICATION_PENDING,
    URINGCORE_REQUEST_RELEASED,
} UringCoreRequestState;

typedef enum {
    URINGCORE_REQUEST_NOP = 0,
    URINGCORE_REQUEST_TIMEOUT,
    URINGCORE_REQUEST_CANCEL,
    URINGCORE_REQUEST_RECV,
    URINGCORE_REQUEST_SEND,
    URINGCORE_REQUEST_SEND_ZC,
    URINGCORE_REQUEST_READ,
    URINGCORE_REQUEST_WRITE,
    URINGCORE_REQUEST_POLL,
} UringCoreRequestKind;

typedef enum {
    URINGCORE_COMPLETION_ONE_SHOT = 0,
    URINGCORE_COMPLETION_MULTISHOT,
    URINGCORE_COMPLETION_NOTIFICATION,
} UringCoreCompletionModel;

typedef struct UringCoreLiburingRing UringCoreLiburingRing;
typedef struct UringCoreLiburingRequest UringCoreLiburingRequest;
typedef struct UringCoreLiburingCompletion UringCoreLiburingCompletion;
typedef struct UringCoreProvidedBuffer UringCoreProvidedBuffer;
typedef struct UringCoreLoopCore UringCoreLoopCore;
typedef struct UringCoreEventFD UringCoreEventFD;

typedef struct {
    PyObject *handle;
    double when;
    uint64_t sequence;
} UringCoreTimerEntry;

struct UringCoreLoopCore {
    PyObject_HEAD
    PyObject **ready;
    Py_ssize_t ready_count;
    Py_ssize_t ready_capacity;
    Py_ssize_t max_ready;
    UringCoreTimerEntry *timers;
    Py_ssize_t timer_count;
    Py_ssize_t timer_capacity;
    Py_ssize_t max_timers;
    uint64_t next_timer_sequence;
    unsigned long long tick_count;
    unsigned long long ready_budget_exhaustions;
    unsigned long long timer_budget_exhaustions;
    int python_callback_active;
    int initialized;
    int closed;
};

struct UringCoreEventFD {
    PyObject_HEAD
    unsigned long long write_count;
    unsigned long long drain_count;
    int fd;
    int signalled;
    int closed;
    int initialized;
};

struct UringCoreLiburingRequest {
    PyObject_HEAD
    UringCoreLiburingRing *ring;
    UringCoreLiburingRequest *previous;
    UringCoreLiburingRequest *next;
    UringCoreRequestState state;
    UringCoreRequestKind kind;
    UringCoreCompletionModel completion_model;
    struct __kernel_timespec timeout;
    PyObject *cancel_target;
    PyObject *file_object;
    Py_buffer buffer;
    int has_buffer;
    int fd;
    int result;
    unsigned int flags;
    int cancelled;
};

struct UringCoreLiburingCompletion {
    PyObject_HEAD
    UringCoreLiburingRequest *request;
    UringCoreRequestState state;
    int result;
    unsigned int flags;
    int final;
    PyObject *buffer;
};

struct UringCoreProvidedBuffer {
    PyObject_HEAD
    UringCoreLiburingRing *ring;
    unsigned int buffer_id;
    Py_ssize_t length;
    unsigned int exports;
    int released;
};

struct UringCoreLiburingRing {
    PyObject_HEAD
    struct io_uring ring;
    UringCoreLiburingRequest *request_head;
    UringCoreLiburingRequest *request_tail;
    int initialized;
    unsigned int sq_entries;
    unsigned int cq_entries;
    unsigned int features;
    unsigned int pending;
    unsigned int prepared;
    unsigned int cq_dropped;
    unsigned long long generation;
    PyObject *supported_opcodes;
    struct io_uring_buf_ring *buffer_ring;
    unsigned char *buffer_storage;
    unsigned int buffer_entries;
    unsigned int buffer_size;
    unsigned int buffer_group_id;
    unsigned int buffers_available;
    unsigned int buffers_in_use;
    int waiting;
};

static PyTypeObject UringCoreLiburingRequestType;
static PyTypeObject UringCoreLiburingCompletionType;
static PyTypeObject UringCoreProvidedBufferType;
static PyTypeObject UringCoreLoopCoreType;
static PyTypeObject UringCoreEventFDType;

static void
uringcore_loop_core_release_ready(UringCoreLoopCore *self)
{
    PyObject **ready = self->ready;
    Py_ssize_t ready_count = self->ready_count;
    Py_ssize_t index;

    /* Detach storage before running arbitrary Python finalizers. A finalizer
     * that reaches a still-open core may enqueue without being overwritten by
     * this clear operation. */
    self->ready = NULL;
    self->ready_count = 0;
    self->ready_capacity = 0;
    for (index = 0; index < ready_count; index++) {
        Py_DECREF(ready[index]);
    }
    PyMem_Free(ready);
}

static void
uringcore_loop_core_release_timers(UringCoreLoopCore *self)
{
    UringCoreTimerEntry *timers = self->timers;
    Py_ssize_t timer_count = self->timer_count;
    Py_ssize_t index;

    self->timers = NULL;
    self->timer_count = 0;
    self->timer_capacity = 0;
    for (index = 0; index < timer_count; index++) {
        Py_DECREF(timers[index].handle);
    }
    PyMem_Free(timers);
}

static int
uringcore_loop_core_require_idle(UringCoreLoopCore *self)
{
    if (self->python_callback_active) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "loop core cannot be mutated during timer handle access");
        return -1;
    }
    return 0;
}

static int
uringcore_loop_core_require_open(UringCoreLoopCore *self)
{
    if (uringcore_loop_core_require_idle(self) < 0) {
        return -1;
    }
    if (self->closed) {
        PyErr_SetString(PyExc_RuntimeError, "loop core is closed");
        return -1;
    }
    return 0;
}

static int
uringcore_loop_core_reserve(UringCoreLoopCore *self)
{
    PyObject **ready;
    Py_ssize_t capacity;

    if (self->ready_count < self->ready_capacity) {
        return 0;
    }
    if (self->ready_count >= self->max_ready) {
        PyErr_NoMemory();
        return -1;
    }
    if (self->ready_capacity == 0) {
        capacity = self->max_ready < 16 ? self->max_ready : 16;
    }
    else if (self->ready_capacity > PY_SSIZE_T_MAX / 2) {
        capacity = self->max_ready;
    }
    else {
        capacity = self->ready_capacity * 2;
        if (capacity > self->max_ready) {
            capacity = self->max_ready;
        }
    }
    if (capacity <= self->ready_count ||
        (size_t)capacity > SIZE_MAX / sizeof(*ready)) {
        PyErr_NoMemory();
        return -1;
    }
    ready = PyMem_Realloc(
        self->ready,
        (size_t)capacity * sizeof(*ready));
    if (ready == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->ready = ready;
    self->ready_capacity = capacity;
    return 0;
}

static int
uringcore_timer_less(
    const UringCoreTimerEntry *left,
    const UringCoreTimerEntry *right)
{
    if (left->when < right->when) {
        return 1;
    }
    if (left->when == right->when) {
        return left->sequence < right->sequence;
    }
    return 0;
}

static int
uringcore_loop_core_reserve_timers(UringCoreLoopCore *self)
{
    UringCoreTimerEntry *timers;
    Py_ssize_t capacity;

    if (self->timer_count < self->timer_capacity) {
        return 0;
    }
    if (self->timer_count >= self->max_timers) {
        PyErr_NoMemory();
        return -1;
    }
    if (self->timer_capacity == 0) {
        capacity = self->max_timers < 16 ? self->max_timers : 16;
    }
    else if (self->timer_capacity > PY_SSIZE_T_MAX / 2) {
        capacity = self->max_timers;
    }
    else {
        capacity = self->timer_capacity * 2;
        if (capacity > self->max_timers) {
            capacity = self->max_timers;
        }
    }
    if (capacity <= self->timer_count ||
        (size_t)capacity > SIZE_MAX / sizeof(*timers)) {
        PyErr_NoMemory();
        return -1;
    }
    timers = PyMem_Realloc(
        self->timers,
        (size_t)capacity * sizeof(*timers));
    if (timers == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->timers = timers;
    self->timer_capacity = capacity;
    return 0;
}

static void
uringcore_loop_core_sift_down(
    UringCoreLoopCore *self,
    Py_ssize_t root)
{
    UringCoreTimerEntry entry = self->timers[root];
    Py_ssize_t child;
    Py_ssize_t right;

    while ((child = root * 2 + 1) < self->timer_count) {
        right = child + 1;
        if (right < self->timer_count &&
            uringcore_timer_less(&self->timers[right], &self->timers[child])) {
            child = right;
        }
        if (!uringcore_timer_less(&self->timers[child], &entry)) {
            break;
        }
        self->timers[root] = self->timers[child];
        root = child;
    }
    self->timers[root] = entry;
}

static UringCoreTimerEntry
uringcore_loop_core_pop_timer(UringCoreLoopCore *self)
{
    UringCoreTimerEntry removed = self->timers[0];
    UringCoreTimerEntry last = self->timers[self->timer_count - 1];

    self->timer_count--;
    if (self->timer_count != 0) {
        self->timers[0] = last;
        uringcore_loop_core_sift_down(self, 0);
    }
    return removed;
}

static int
uringcore_loop_core_handle_cancelled(
    UringCoreLoopCore *self,
    PyObject *handle)
{
    PyObject *cancelled;
    int result;

    self->python_callback_active = 1;
    cancelled = PyObject_GetAttrString(handle, "_cancelled");
    if (cancelled == NULL) {
        self->python_callback_active = 0;
        return -1;
    }
    result = PyObject_IsTrue(cancelled);
    Py_DECREF(cancelled);
    self->python_callback_active = 0;
    return result;
}

static int
uringcore_loop_core_mark_timer_unscheduled(
    UringCoreLoopCore *self,
    PyObject *handle)
{
    int result;

    self->python_callback_active = 1;
    result = PyObject_SetAttrString(handle, "_scheduled", Py_False);
    self->python_callback_active = 0;
    return result;
}

static PyObject *
uringcore_loop_core_new(
    PyTypeObject *type,
    PyObject *Py_UNUSED(args),
    PyObject *Py_UNUSED(kwargs))
{
    UringCoreLoopCore *self = (UringCoreLoopCore *)type->tp_alloc(type, 0);

    if (self != NULL) {
        self->max_ready = PY_SSIZE_T_MAX;
        self->max_timers = PY_SSIZE_T_MAX;
    }
    return (PyObject *)self;
}

static int
uringcore_loop_core_init(
    UringCoreLoopCore *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"max_ready", "max_timers", NULL};
    PyObject *max_ready_object = NULL;
    PyObject *max_timers_object = NULL;
    PyObject *index = NULL;
    Py_ssize_t max_ready = PY_SSIZE_T_MAX;
    Py_ssize_t max_timers = PY_SSIZE_T_MAX;

    if (self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "loop core cannot be reinitialized");
        return -1;
    }
    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "|OO:LoopCore",
            keyword_names,
            &max_ready_object,
            &max_timers_object)) {
        return -1;
    }
    if (max_ready_object != NULL && max_ready_object != Py_None) {
        index = PyNumber_Index(max_ready_object);
        if (index == NULL) {
            return -1;
        }
        max_ready = PyLong_AsSsize_t(index);
        Py_DECREF(index);
        if (max_ready < 1 || PyErr_Occurred()) {
            PyErr_Clear();
            PyErr_SetString(PyExc_ValueError, "max_ready must be a positive integer");
            return -1;
        }
    }
    if (max_timers_object != NULL && max_timers_object != Py_None) {
        index = PyNumber_Index(max_timers_object);
        if (index == NULL) {
            return -1;
        }
        max_timers = PyLong_AsSsize_t(index);
        Py_DECREF(index);
        if (max_timers < 1 || PyErr_Occurred()) {
            PyErr_Clear();
            PyErr_SetString(PyExc_ValueError, "max_timers must be a positive integer");
            return -1;
        }
    }
    uringcore_loop_core_release_ready(self);
    uringcore_loop_core_release_timers(self);
    self->max_ready = max_ready;
    self->max_timers = max_timers;
    self->next_timer_sequence = 0;
    self->initialized = 1;
    self->closed = 0;
    return 0;
}

static void
uringcore_loop_core_dealloc(UringCoreLoopCore *self)
{
    PyObject_GC_UnTrack(self);
    self->closed = 1;
    uringcore_loop_core_release_ready(self);
    uringcore_loop_core_release_timers(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
uringcore_loop_core_traverse(
    UringCoreLoopCore *self,
    visitproc visit,
    void *arg)
{
    Py_ssize_t index;

    for (index = 0; index < self->ready_count; index++) {
        Py_VISIT(self->ready[index]);
    }
    for (index = 0; index < self->timer_count; index++) {
        Py_VISIT(self->timers[index].handle);
    }
    return 0;
}

static int
uringcore_loop_core_clear(UringCoreLoopCore *self)
{
    if (self->python_callback_active) {
        return 0;
    }
    uringcore_loop_core_release_ready(self);
    uringcore_loop_core_release_timers(self);
    return 0;
}

static PyObject *
uringcore_loop_core_enqueue_ready(
    UringCoreLoopCore *self,
    PyObject *handle)
{
    if (uringcore_loop_core_require_open(self) < 0 ||
        uringcore_loop_core_reserve(self) < 0) {
        return NULL;
    }
    Py_INCREF(handle);
    self->ready[self->ready_count++] = handle;
    Py_RETURN_NONE;
}

static PyObject *
uringcore_loop_core_take_ready_batch(
    UringCoreLoopCore *self,
    PyObject *args)
{
    Py_ssize_t limit;
    Py_ssize_t count;
    Py_ssize_t remaining;
    Py_ssize_t index;
    PyObject *batch;

    if (!PyArg_ParseTuple(args, "n:take_ready_batch", &limit)) {
        return NULL;
    }
    if (limit < 0) {
        PyErr_SetString(PyExc_ValueError, "ready batch limit cannot be negative");
        return NULL;
    }
    if (uringcore_loop_core_require_open(self) < 0) {
        return NULL;
    }
    count = limit < self->ready_count ? limit : self->ready_count;
    batch = PyList_New(count);
    if (batch == NULL) {
        return NULL;
    }
    for (index = 0; index < count; index++) {
        /* Transfer the queue's strong reference into the result list. */
        PyList_SET_ITEM(batch, index, self->ready[index]);
    }
    remaining = self->ready_count - count;
    if (remaining != 0) {
        memmove(
            self->ready,
            self->ready + count,
            (size_t)remaining * sizeof(*self->ready));
    }
    if (count != 0) {
        memset(
            self->ready + remaining,
            0,
            (size_t)count * sizeof(*self->ready));
    }
    self->ready_count = remaining;
    return batch;
}

static PyObject *
uringcore_loop_core_clear_ready(
    UringCoreLoopCore *self,
    PyObject *Py_UNUSED(ignored))
{
    if (uringcore_loop_core_require_idle(self) < 0) {
        return NULL;
    }
    uringcore_loop_core_release_ready(self);
    Py_RETURN_NONE;
}

static PyObject *
uringcore_loop_core_schedule_timer(
    UringCoreLoopCore *self,
    PyObject *args)
{
    PyObject *handle;
    double when;
    Py_ssize_t index;
    Py_ssize_t parent;
    UringCoreTimerEntry entry;

    if (!PyArg_ParseTuple(args, "Od:schedule_timer", &handle, &when)) {
        return NULL;
    }
    if (uringcore_loop_core_require_open(self) < 0 ||
        uringcore_loop_core_reserve_timers(self) < 0) {
        return NULL;
    }
    if (self->next_timer_sequence == UINT64_MAX) {
        PyErr_SetString(PyExc_OverflowError, "timer sequence is exhausted");
        return NULL;
    }
    Py_INCREF(handle);
    entry.handle = handle;
    entry.when = when;
    entry.sequence = self->next_timer_sequence++;
    index = self->timer_count++;
    while (index != 0) {
        parent = (index - 1) / 2;
        if (!uringcore_timer_less(&entry, &self->timers[parent])) {
            break;
        }
        self->timers[index] = self->timers[parent];
        index = parent;
    }
    self->timers[index] = entry;
    Py_RETURN_NONE;
}

static int
uringcore_loop_core_prune_cancelled_roots(UringCoreLoopCore *self)
{
    while (self->timer_count != 0) {
        UringCoreTimerEntry removed;
        int cancelled = uringcore_loop_core_handle_cancelled(
            self,
            self->timers[0].handle);
        if (cancelled < 0) {
            return -1;
        }
        if (!cancelled) {
            return 0;
        }
        if (uringcore_loop_core_mark_timer_unscheduled(
                self,
                self->timers[0].handle) < 0) {
            return -1;
        }
        removed = uringcore_loop_core_pop_timer(self);
        Py_DECREF(removed.handle);
    }
    return 0;
}

static PyObject *
uringcore_loop_core_next_deadline(
    UringCoreLoopCore *self,
    PyObject *Py_UNUSED(ignored))
{
    if (uringcore_loop_core_require_open(self) < 0 ||
        uringcore_loop_core_prune_cancelled_roots(self) < 0) {
        return NULL;
    }
    if (self->timer_count == 0) {
        Py_RETURN_NONE;
    }
    return PyFloat_FromDouble(self->timers[0].when);
}

static PyObject *
uringcore_loop_core_promote_due(
    UringCoreLoopCore *self,
    PyObject *args)
{
    double now;
    double resolution;
    double end_time;
    Py_ssize_t limit;
    Py_ssize_t promoted = 0;

    if (!PyArg_ParseTuple(args, "ddn:promote_due", &now, &resolution, &limit)) {
        return NULL;
    }
    if (resolution < 0.0) {
        PyErr_SetString(PyExc_ValueError, "clock resolution cannot be negative");
        return NULL;
    }
    if (limit < 0) {
        PyErr_SetString(PyExc_ValueError, "timer promotion limit cannot be negative");
        return NULL;
    }
    if (uringcore_loop_core_require_open(self) < 0) {
        return NULL;
    }
    end_time = now + resolution;
    while (promoted < limit) {
        UringCoreTimerEntry removed;
        if (uringcore_loop_core_prune_cancelled_roots(self) < 0) {
            return NULL;
        }
        if (self->timer_count == 0 ||
            self->timers[0].when >= end_time) {
            break;
        }
        if (uringcore_loop_core_reserve(self) < 0) {
            return NULL;
        }
        if (uringcore_loop_core_mark_timer_unscheduled(
                self,
                self->timers[0].handle) < 0) {
            return NULL;
        }
        removed = uringcore_loop_core_pop_timer(self);
        self->ready[self->ready_count++] = removed.handle;
        promoted++;
    }
    return PyLong_FromSsize_t(promoted);
}

static PyObject *
uringcore_loop_core_compact_timers(
    UringCoreLoopCore *self,
    PyObject *Py_UNUSED(ignored))
{
    UringCoreTimerEntry *old_timers;
    UringCoreTimerEntry *new_timers = NULL;
    unsigned char *cancelled;
    Py_ssize_t old_count;
    Py_ssize_t live_count = 0;
    Py_ssize_t index;

    if (uringcore_loop_core_require_open(self) < 0) {
        return NULL;
    }
    old_count = self->timer_count;
    if (old_count == 0) {
        return PyLong_FromLong(0);
    }
    cancelled = PyMem_Calloc((size_t)old_count, sizeof(*cancelled));
    if (cancelled == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    for (index = 0; index < old_count; index++) {
        int is_cancelled = uringcore_loop_core_handle_cancelled(
            self,
            self->timers[index].handle);
        if (is_cancelled < 0) {
            PyMem_Free(cancelled);
            return NULL;
        }
        cancelled[index] = (unsigned char)is_cancelled;
        if (!is_cancelled) {
            live_count++;
        }
    }
    if (live_count != 0) {
        new_timers = PyMem_Malloc(
            (size_t)live_count * sizeof(*new_timers));
        if (new_timers == NULL) {
            PyMem_Free(cancelled);
            PyErr_NoMemory();
            return NULL;
        }
    }
    for (index = 0; index < old_count; index++) {
        if (cancelled[index] && uringcore_loop_core_mark_timer_unscheduled(
                self,
                self->timers[index].handle) < 0) {
            PyMem_Free(new_timers);
            PyMem_Free(cancelled);
            return NULL;
        }
    }
    old_timers = self->timers;
    live_count = 0;
    for (index = 0; index < old_count; index++) {
        if (!cancelled[index]) {
            new_timers[live_count++] = old_timers[index];
        }
    }
    self->timers = new_timers;
    self->timer_count = live_count;
    self->timer_capacity = live_count;
    for (index = live_count / 2; index > 0; index--) {
        uringcore_loop_core_sift_down(self, index - 1);
    }
    for (index = 0; index < old_count; index++) {
        if (cancelled[index]) {
            Py_DECREF(old_timers[index].handle);
        }
    }
    PyMem_Free(old_timers);
    PyMem_Free(cancelled);
    return PyLong_FromSsize_t(old_count - live_count);
}

static PyObject *
uringcore_loop_core_clear_timers(
    UringCoreLoopCore *self,
    PyObject *Py_UNUSED(ignored))
{
    if (uringcore_loop_core_require_idle(self) < 0) {
        return NULL;
    }
    uringcore_loop_core_release_timers(self);
    Py_RETURN_NONE;
}

static PyObject *
uringcore_loop_core_prepare_tick(
    UringCoreLoopCore *self,
    PyObject *args)
{
    double now;
    double maximum_timeout;
    double timeout;
    int immediate;

    if (!PyArg_ParseTuple(
            args,
            "ddp:prepare_tick",
            &now,
            &maximum_timeout,
            &immediate)) {
        return NULL;
    }
    if (!isfinite(maximum_timeout) || maximum_timeout < 0.0) {
        PyErr_SetString(
            PyExc_ValueError,
            "maximum timeout must be finite and non-negative");
        return NULL;
    }
    if (uringcore_loop_core_require_open(self) < 0 ||
        uringcore_loop_core_prune_cancelled_roots(self) < 0) {
        return NULL;
    }
    if (immediate || self->ready_count != 0) {
        return PyFloat_FromDouble(0.0);
    }
    if (self->timer_count == 0) {
        Py_RETURN_NONE;
    }
    timeout = self->timers[0].when - now;
    if (!(timeout > 0.0)) {
        timeout = 0.0;
    }
    if (timeout > maximum_timeout) {
        timeout = maximum_timeout;
    }
    return PyFloat_FromDouble(timeout);
}

static PyObject *
uringcore_loop_core_finish_tick(
    UringCoreLoopCore *self,
    PyObject *args)
{
    double now;
    double resolution;
    Py_ssize_t timer_budget;
    Py_ssize_t ready_budget;
    PyObject *promote_args = NULL;
    PyObject *promoted_object = NULL;
    PyObject *batch_args = NULL;
    PyObject *batch = NULL;
    PyObject *result = NULL;
    PyObject *promoted_count_object = NULL;
    PyObject *immediate_object;
    Py_ssize_t promoted;
    int immediate = 0;
    int timer_exhausted = 0;

    if (!PyArg_ParseTuple(
            args,
            "ddnn:finish_tick",
            &now,
            &resolution,
            &timer_budget,
            &ready_budget)) {
        return NULL;
    }
    if (!isfinite(resolution) ||
        resolution < 0.0 ||
        timer_budget < 1 ||
        ready_budget < 1) {
        PyErr_SetString(
            PyExc_ValueError,
            "resolution must be finite and non-negative and tick budgets must be positive");
        return NULL;
    }
    if (uringcore_loop_core_require_open(self) < 0) {
        return NULL;
    }
    promote_args = Py_BuildValue("(ddn)", now, resolution, timer_budget);
    if (promote_args == NULL) {
        goto error;
    }
    promoted_object = uringcore_loop_core_promote_due(self, promote_args);
    Py_CLEAR(promote_args);
    if (promoted_object == NULL) {
        goto error;
    }
    promoted = PyLong_AsSsize_t(promoted_object);
    Py_CLEAR(promoted_object);
    if (promoted < 0 && PyErr_Occurred()) {
        goto error;
    }
    if (uringcore_loop_core_prune_cancelled_roots(self) < 0) {
        goto error;
    }
    if (promoted == timer_budget &&
        self->timer_count != 0 &&
        self->timers[0].when < now + resolution) {
        immediate = 1;
        timer_exhausted = 1;
    }
    result = PyTuple_New(3);
    if (result == NULL) {
        goto error;
    }
    promoted_count_object = PyLong_FromSsize_t(promoted);
    if (promoted_count_object == NULL) {
        goto error;
    }
    batch_args = Py_BuildValue("(n)", ready_budget);
    if (batch_args == NULL) {
        goto error;
    }
    batch = uringcore_loop_core_take_ready_batch(self, batch_args);
    Py_CLEAR(batch_args);
    if (batch == NULL) {
        goto error;
    }
    if (self->ready_count != 0) {
        immediate = 1;
    }
    immediate_object = PyBool_FromLong(immediate);
    PyTuple_SET_ITEM(result, 0, batch);
    batch = NULL;
    PyTuple_SET_ITEM(result, 1, immediate_object);
    PyTuple_SET_ITEM(result, 2, promoted_count_object);
    promoted_count_object = NULL;
    if (self->ready_count != 0) {
        self->ready_budget_exhaustions++;
    }
    if (timer_exhausted) {
        self->timer_budget_exhaustions++;
    }
    self->tick_count++;
    return result;

error:
    Py_XDECREF(promote_args);
    Py_XDECREF(promoted_object);
    Py_XDECREF(batch_args);
    Py_XDECREF(batch);
    Py_XDECREF(promoted_count_object);
    Py_XDECREF(result);
    return NULL;
}

static PyObject *
uringcore_loop_core_close(
    UringCoreLoopCore *self,
    PyObject *Py_UNUSED(ignored))
{
    if (uringcore_loop_core_require_idle(self) < 0) {
        return NULL;
    }
    if (!self->closed) {
        self->closed = 1;
        uringcore_loop_core_release_ready(self);
        uringcore_loop_core_release_timers(self);
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_loop_core_get_ready_count(
    UringCoreLoopCore *self,
    void *Py_UNUSED(context))
{
    if (uringcore_loop_core_require_open(self) < 0) {
        return NULL;
    }
    return PyLong_FromSsize_t(self->ready_count);
}

static PyObject *
uringcore_loop_core_get_timer_count(
    UringCoreLoopCore *self,
    void *Py_UNUSED(context))
{
    if (uringcore_loop_core_require_open(self) < 0) {
        return NULL;
    }
    return PyLong_FromSsize_t(self->timer_count);
}

static PyObject *
uringcore_loop_core_get_cancelled_timer_count(
    UringCoreLoopCore *self,
    void *Py_UNUSED(context))
{
    Py_ssize_t cancelled = 0;
    Py_ssize_t index;

    if (uringcore_loop_core_require_open(self) < 0) {
        return NULL;
    }
    for (index = 0; index < self->timer_count; index++) {
        int is_cancelled = uringcore_loop_core_handle_cancelled(
            self,
            self->timers[index].handle);
        if (is_cancelled < 0) {
            return NULL;
        }
        cancelled += is_cancelled;
    }
    return PyLong_FromSsize_t(cancelled);
}

static PyObject *
uringcore_loop_core_get_tick_count(
    UringCoreLoopCore *self,
    void *Py_UNUSED(context))
{
    return PyLong_FromUnsignedLongLong(self->tick_count);
}

static PyObject *
uringcore_loop_core_get_ready_budget_exhaustions(
    UringCoreLoopCore *self,
    void *Py_UNUSED(context))
{
    return PyLong_FromUnsignedLongLong(self->ready_budget_exhaustions);
}

static PyObject *
uringcore_loop_core_get_timer_budget_exhaustions(
    UringCoreLoopCore *self,
    void *Py_UNUSED(context))
{
    return PyLong_FromUnsignedLongLong(self->timer_budget_exhaustions);
}

static PyObject *
uringcore_loop_core_get_closed(
    UringCoreLoopCore *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(self->closed);
}

static PyMethodDef uringcore_loop_core_methods[] = {
    {
        "enqueue_ready",
        (PyCFunction)uringcore_loop_core_enqueue_ready,
        METH_O,
        PyDoc_STR("Append one strongly-owned ready handle."),
    },
    {
        "take_ready_batch",
        (PyCFunction)uringcore_loop_core_take_ready_batch,
        METH_VARARGS,
        PyDoc_STR("Remove and return at most limit ready handles in FIFO order."),
    },
    {
        "clear_ready",
        (PyCFunction)uringcore_loop_core_clear_ready,
        METH_NOARGS,
        PyDoc_STR("Release all queued handles while keeping the core open."),
    },
    {
        "schedule_timer",
        (PyCFunction)uringcore_loop_core_schedule_timer,
        METH_VARARGS,
        PyDoc_STR("Schedule one strongly-owned handle at a monotonic deadline."),
    },
    {
        "next_deadline",
        (PyCFunction)uringcore_loop_core_next_deadline,
        METH_NOARGS,
        PyDoc_STR("Return the next live deadline after pruning cancelled roots."),
    },
    {
        "promote_due",
        (PyCFunction)uringcore_loop_core_promote_due,
        METH_VARARGS,
        PyDoc_STR("Move a bounded number of due timers to the ready queue."),
    },
    {
        "compact_timers",
        (PyCFunction)uringcore_loop_core_compact_timers,
        METH_NOARGS,
        PyDoc_STR("Release cancelled timers and rebuild the native heap."),
    },
    {
        "clear_timers",
        (PyCFunction)uringcore_loop_core_clear_timers,
        METH_NOARGS,
        PyDoc_STR("Release all timer handles while keeping the core open."),
    },
    {
        "prepare_tick",
        (PyCFunction)uringcore_loop_core_prepare_tick,
        METH_VARARGS,
        PyDoc_STR("Compute the bounded tick wait from ready and timer state."),
    },
    {
        "finish_tick",
        (PyCFunction)uringcore_loop_core_finish_tick,
        METH_VARARGS,
        PyDoc_STR("Promote timers and return one bounded ready snapshot."),
    },
    {
        "close",
        (PyCFunction)uringcore_loop_core_close,
        METH_NOARGS,
        PyDoc_STR("Idempotently release ready-queue storage and close the core."),
    },
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef uringcore_loop_core_getset[] = {
    {
        "ready_count",
        (getter)uringcore_loop_core_get_ready_count,
        NULL,
        PyDoc_STR("Number of strongly-owned ready handles."),
        NULL,
    },
    {
        "timer_count",
        (getter)uringcore_loop_core_get_timer_count,
        NULL,
        PyDoc_STR("Number of strongly-owned timer entries, including cancelled entries."),
        NULL,
    },
    {
        "cancelled_timer_count",
        (getter)uringcore_loop_core_get_cancelled_timer_count,
        NULL,
        PyDoc_STR("Number of cancelled timer entries awaiting pruning or compaction."),
        NULL,
    },
    {
        "tick_count",
        (getter)uringcore_loop_core_get_tick_count,
        NULL,
        PyDoc_STR("Number of completed native scheduling ticks."),
        NULL,
    },
    {
        "ready_budget_exhaustions",
        (getter)uringcore_loop_core_get_ready_budget_exhaustions,
        NULL,
        PyDoc_STR("Number of ticks that left native ready work pending."),
        NULL,
    },
    {
        "timer_budget_exhaustions",
        (getter)uringcore_loop_core_get_timer_budget_exhaustions,
        NULL,
        PyDoc_STR("Number of ticks that left already-due timers pending."),
        NULL,
    },
    {
        "closed",
        (getter)uringcore_loop_core_get_closed,
        NULL,
        PyDoc_STR("Whether this loop core has been closed."),
        NULL,
    },
    {NULL},
};

PyDoc_STRVAR(
    uringcore_loop_core_doc,
    "LoopCore(max_ready=None, max_timers=None)\n"
    "--\n"
    "\n"
    "Own GC-visible ready-queue and timer-heap state.");

static PyTypeObject UringCoreLoopCoreType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.LoopCore",
    .tp_basicsize = sizeof(UringCoreLoopCore),
    .tp_dealloc = (destructor)uringcore_loop_core_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_doc = uringcore_loop_core_doc,
    .tp_methods = uringcore_loop_core_methods,
    .tp_getset = uringcore_loop_core_getset,
    .tp_init = (initproc)uringcore_loop_core_init,
    .tp_new = uringcore_loop_core_new,
    .tp_traverse = (traverseproc)uringcore_loop_core_traverse,
    .tp_clear = (inquiry)uringcore_loop_core_clear,
};

static int
uringcore_eventfd_require_open(UringCoreEventFD *self)
{
    if (self->closed || self->fd < 0) {
        PyErr_SetString(PyExc_RuntimeError, "eventfd wakeup is closed");
        return -1;
    }
    return 0;
}

static PyObject *
uringcore_eventfd_new(
    PyTypeObject *type,
    PyObject *Py_UNUSED(args),
    PyObject *Py_UNUSED(kwargs))
{
    UringCoreEventFD *self = (UringCoreEventFD *)type->tp_alloc(type, 0);

    if (self != NULL) {
        self->fd = -1;
        self->closed = 1;
    }
    return (PyObject *)self;
}

static int
uringcore_eventfd_init(
    UringCoreEventFD *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"setup_errno", NULL};
    int setup_errno = 0;
    int fd;

    if (self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "eventfd wakeup cannot be reinitialized");
        return -1;
    }
    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "|i:EventFD",
            keyword_names,
            &setup_errno)) {
        return -1;
    }
    if (setup_errno < 0) {
        PyErr_SetString(PyExc_ValueError, "setup_errno cannot be negative");
        return -1;
    }
    if (setup_errno != 0) {
        errno = setup_errno;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (fd < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }
    self->fd = fd;
    self->closed = 0;
    self->signalled = 0;
    self->initialized = 1;
    return 0;
}

static PyObject *
uringcore_eventfd_notify(
    UringCoreEventFD *self,
    PyObject *Py_UNUSED(ignored))
{
    uint64_t value = 1;
    ssize_t result;

    if (uringcore_eventfd_require_open(self) < 0) {
        return NULL;
    }
    if (self->signalled) {
        Py_RETURN_FALSE;
    }
    do {
        result = write(self->fd, &value, sizeof(value));
    } while (result < 0 && errno == EINTR);
    if (result < 0 && errno == EAGAIN) {
        self->signalled = 1;
        Py_RETURN_FALSE;
    }
    if (result < 0) {
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    if (result != (ssize_t)sizeof(value)) {
        PyErr_SetString(PyExc_OSError, "eventfd wakeup write was incomplete");
        return NULL;
    }
    self->signalled = 1;
    self->write_count++;
    Py_RETURN_TRUE;
}

static PyObject *
uringcore_eventfd_drain(
    UringCoreEventFD *self,
    PyObject *Py_UNUSED(ignored))
{
    uint64_t value;
    unsigned long long total = 0;
    ssize_t result;

    if (uringcore_eventfd_require_open(self) < 0) {
        return NULL;
    }
    for (;;) {
        do {
            result = read(self->fd, &value, sizeof(value));
        } while (result < 0 && errno == EINTR);
        if (result < 0 && errno == EAGAIN) {
            break;
        }
        if (result < 0) {
            return PyErr_SetFromErrno(PyExc_OSError);
        }
        if (result != (ssize_t)sizeof(value)) {
            PyErr_SetString(PyExc_OSError, "eventfd wakeup read was incomplete");
            return NULL;
        }
        if (ULLONG_MAX - total < value) {
            total = ULLONG_MAX;
        }
        else {
            total += value;
        }
        self->drain_count++;
    }
    self->signalled = 0;
    return PyLong_FromUnsignedLongLong(total);
}

static PyObject *
uringcore_eventfd_fileno(
    UringCoreEventFD *self,
    PyObject *Py_UNUSED(ignored))
{
    if (uringcore_eventfd_require_open(self) < 0) {
        return NULL;
    }
    return PyLong_FromLong(self->fd);
}

static PyObject *
uringcore_eventfd_close(
    UringCoreEventFD *self,
    PyObject *Py_UNUSED(ignored))
{
    int fd = self->fd;

    self->fd = -1;
    self->closed = 1;
    self->signalled = 0;
    if (fd >= 0 && close(fd) < 0) {
        return PyErr_SetFromErrno(PyExc_OSError);
    }
    Py_RETURN_NONE;
}

static void
uringcore_eventfd_dealloc(UringCoreEventFD *self)
{
    int saved_errno = errno;

    if (self->fd >= 0) {
        close(self->fd);
    }
    errno = saved_errno;
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
uringcore_eventfd_get_signalled(
    UringCoreEventFD *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(self->signalled);
}

static PyObject *
uringcore_eventfd_get_closed(
    UringCoreEventFD *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(self->closed);
}

static PyObject *
uringcore_eventfd_get_write_count(
    UringCoreEventFD *self,
    void *Py_UNUSED(context))
{
    return PyLong_FromUnsignedLongLong(self->write_count);
}

static PyObject *
uringcore_eventfd_get_drain_count(
    UringCoreEventFD *self,
    void *Py_UNUSED(context))
{
    return PyLong_FromUnsignedLongLong(self->drain_count);
}

static PyMethodDef uringcore_eventfd_methods[] = {
    {
        "notify",
        (PyCFunction)uringcore_eventfd_notify,
        METH_NOARGS,
        PyDoc_STR("Publish one coalesced eventfd wakeup."),
    },
    {
        "drain",
        (PyCFunction)uringcore_eventfd_drain,
        METH_NOARGS,
        PyDoc_STR("Drain the eventfd counter and clear coalescing state."),
    },
    {
        "fileno",
        (PyCFunction)uringcore_eventfd_fileno,
        METH_NOARGS,
        PyDoc_STR("Return the private eventfd descriptor while open."),
    },
    {
        "close",
        (PyCFunction)uringcore_eventfd_close,
        METH_NOARGS,
        PyDoc_STR("Idempotently close the private eventfd descriptor."),
    },
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef uringcore_eventfd_getset[] = {
    {
        "signalled",
        (getter)uringcore_eventfd_get_signalled,
        NULL,
        PyDoc_STR("Whether a wakeup is currently coalesced."),
        NULL,
    },
    {
        "closed",
        (getter)uringcore_eventfd_get_closed,
        NULL,
        PyDoc_STR("Whether the descriptor is closed."),
        NULL,
    },
    {
        "write_count",
        (getter)uringcore_eventfd_get_write_count,
        NULL,
        PyDoc_STR("Number of kernel writes performed after coalescing."),
        NULL,
    },
    {
        "drain_count",
        (getter)uringcore_eventfd_get_drain_count,
        NULL,
        PyDoc_STR("Number of eventfd counter reads performed."),
        NULL,
    },
    {NULL},
};

PyDoc_STRVAR(
    uringcore_eventfd_doc,
    "EventFD(setup_errno=0)\n"
    "--\n"
    "\n"
    "Own a private coalescing eventfd for loop wakeups.");

static PyTypeObject UringCoreEventFDType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.EventFD",
    .tp_basicsize = sizeof(UringCoreEventFD),
    .tp_dealloc = (destructor)uringcore_eventfd_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = uringcore_eventfd_doc,
    .tp_methods = uringcore_eventfd_methods,
    .tp_getset = uringcore_eventfd_getset,
    .tp_init = (initproc)uringcore_eventfd_init,
    .tp_new = uringcore_eventfd_new,
};

static void
uringcore_liburing_ring_free_buffer_pool(UringCoreLiburingRing *self)
{
    if (self->buffer_ring != NULL && self->initialized) {
        int result = io_uring_free_buf_ring(
            &self->ring,
            self->buffer_ring,
            self->buffer_entries,
            (int)self->buffer_group_id);
        if (result != 0) {
            /* Keep both mappings alive until queue teardown cancels every
             * request that may still select from this buffer group. */
            return;
        }
    }
    else if (self->buffer_ring != NULL) {
        munmap(
            self->buffer_ring,
            (size_t)self->buffer_entries * sizeof(struct io_uring_buf));
    }
    PyMem_RawFree(self->buffer_storage);
    self->buffer_ring = NULL;
    self->buffer_storage = NULL;
    self->buffer_entries = 0;
    self->buffer_size = 0;
    self->buffer_group_id = 0;
    self->buffers_available = 0;
    self->buffers_in_use = 0;
}

static void
uringcore_liburing_request_link(
    UringCoreLiburingRing *ring,
    UringCoreLiburingRequest *request)
{
    request->ring = ring;
    request->previous = ring->request_tail;
    request->next = NULL;
    if (ring->request_tail != NULL) {
        ring->request_tail->next = request;
    }
    else {
        ring->request_head = request;
    }
    ring->request_tail = request;
    ring->pending++;
    ring->prepared++;

    /* Keep the request alive while an SQE or CQE contains its address. */
    Py_INCREF(request);
}

static void
uringcore_liburing_request_unlink(UringCoreLiburingRequest *request)
{
    UringCoreLiburingRing *ring = request->ring;

    if (ring == NULL) {
        return;
    }
    if (request->previous != NULL) {
        request->previous->next = request->next;
    }
    else {
        ring->request_head = request->next;
    }
    if (request->next != NULL) {
        request->next->previous = request->previous;
    }
    else {
        ring->request_tail = request->previous;
    }
    ring->pending--;
    request->ring = NULL;
    request->previous = NULL;
    request->next = NULL;

    Py_DECREF(request);
}

static void
uringcore_liburing_request_release_resources(
    UringCoreLiburingRequest *request)
{
    if (request->has_buffer) {
        PyBuffer_Release(&request->buffer);
        request->has_buffer = 0;
        memset(&request->buffer, 0, sizeof(request->buffer));
    }
    Py_CLEAR(request->file_object);
    Py_CLEAR(request->cancel_target);
}

static void
uringcore_liburing_ring_close_resources(UringCoreLiburingRing *self)
{
    UringCoreLiburingRequest *request;

    if (self->initialized) {
        uringcore_liburing_ring_free_buffer_pool(self);
        self->cq_dropped = *self->ring.cq.koverflow;
        self->generation++;
        io_uring_queue_exit(&self->ring);
        memset(&self->ring, 0, sizeof(self->ring));
        self->initialized = 0;
        if (self->buffer_ring != NULL) {
            uringcore_liburing_ring_free_buffer_pool(self);
        }
    }
    while ((request = self->request_head) != NULL) {
        request->state = URINGCORE_REQUEST_RELEASED;
        request->result = -ECANCELED;
        request->flags = 0;
        request->cancelled = 1;
        uringcore_liburing_request_release_resources(request);
        uringcore_liburing_request_unlink(request);
    }
    self->pending = 0;
    self->prepared = 0;
}

static int
uringcore_liburing_parse_uint(
    PyObject *object,
    const char *name,
    unsigned int default_value,
    unsigned int *value)
{
    PyObject *index;
    unsigned long parsed;

    if (object == NULL || object == Py_None) {
        *value = default_value;
        return 0;
    }
    index = PyNumber_Index(object);
    if (index == NULL) {
        return -1;
    }
    parsed = PyLong_AsUnsignedLong(index);
    Py_DECREF(index);
    if ((parsed == (unsigned long)-1 && PyErr_Occurred()) ||
        parsed == 0 || parsed > UINT_MAX) {
        PyErr_Clear();
        PyErr_Format(
            PyExc_ValueError,
            "%s must be between 1 and %u",
            name,
            UINT_MAX);
        return -1;
    }
    *value = (unsigned int)parsed;
    return 0;
}

static PyObject *
uringcore_liburing_validate_supported_opcodes(PyObject *object)
{
    PyObject *opcodes;
    PyObject *iterator;
    PyObject *item;

    opcodes = PyFrozenSet_New(object);
    if (opcodes == NULL) {
        return NULL;
    }
    iterator = PyObject_GetIter(opcodes);
    if (iterator == NULL) {
        Py_DECREF(opcodes);
        return NULL;
    }
    while ((item = PyIter_Next(iterator)) != NULL) {
        PyObject *index = PyNumber_Index(item);
        long opcode;

        Py_DECREF(item);
        if (index == NULL) {
            Py_DECREF(iterator);
            Py_DECREF(opcodes);
            return NULL;
        }
        opcode = PyLong_AsLong(index);
        Py_DECREF(index);
        if ((opcode == -1 && PyErr_Occurred()) ||
            opcode < 0 || opcode >= IORING_OP_LAST) {
            PyErr_Clear();
            PyErr_Format(
                PyExc_ValueError,
                "supported opcode must be between 0 and %d",
                IORING_OP_LAST - 1);
            Py_DECREF(iterator);
            Py_DECREF(opcodes);
            return NULL;
        }
    }
    Py_DECREF(iterator);
    if (PyErr_Occurred()) {
        Py_DECREF(opcodes);
        return NULL;
    }
    return opcodes;
}

static PyObject *
uringcore_liburing_probe_supported_opcodes(struct io_uring *ring)
{
    struct io_uring_probe *probe;
    PyObject *set;
    PyObject *opcodes;
    int opcode;

    probe = io_uring_get_probe_ring(ring);
    if (probe == NULL) {
        PyErr_SetString(PyExc_OSError, "io_uring opcode probing failed");
        return NULL;
    }
    set = PySet_New(NULL);
    if (set == NULL) {
        io_uring_free_probe(probe);
        return NULL;
    }
    for (opcode = 0; opcode < IORING_OP_LAST; opcode++) {
        if (io_uring_opcode_supported(probe, opcode)) {
            PyObject *value = PyLong_FromLong(opcode);

            if (value == NULL || PySet_Add(set, value) < 0) {
                Py_XDECREF(value);
                Py_DECREF(set);
                io_uring_free_probe(probe);
                return NULL;
            }
            Py_DECREF(value);
        }
    }
    io_uring_free_probe(probe);
    opcodes = PyFrozenSet_New(set);
    Py_DECREF(set);
    return opcodes;
}

static void
uringcore_liburing_request_dealloc(UringCoreLiburingRequest *self)
{
    PyObject_GC_UnTrack(self);
    uringcore_liburing_request_release_resources(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
uringcore_liburing_request_traverse(
    UringCoreLiburingRequest *self,
    visitproc visit,
    void *arg)
{
    Py_VISIT(self->cancel_target);
    Py_VISIT(self->file_object);
    if (self->has_buffer) {
        Py_VISIT(self->buffer.obj);
    }
    return 0;
}

static PyObject *
uringcore_liburing_request_get_done(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(self->state == URINGCORE_REQUEST_RELEASED);
}

static PyObject *
uringcore_liburing_request_get_cancelled(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(self->cancelled);
}

static PyObject *
uringcore_liburing_request_get_result(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    if (self->state != URINGCORE_REQUEST_OPERATION_COMPLETE &&
        self->state != URINGCORE_REQUEST_NOTIFICATION_PENDING &&
        self->state != URINGCORE_REQUEST_RELEASED) {
        Py_RETURN_NONE;
    }
    return PyLong_FromLong(self->result);
}

static PyObject *
uringcore_liburing_request_get_state(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    const char *state;

    switch (self->state) {
        case URINGCORE_REQUEST_PREPARED:
            state = "prepared";
            break;
        case URINGCORE_REQUEST_SUBMITTED:
            state = "submitted";
            break;
        case URINGCORE_REQUEST_CANCEL_PENDING:
            state = "cancel_pending";
            break;
        case URINGCORE_REQUEST_OPERATION_COMPLETE:
            state = "operation_complete";
            break;
        case URINGCORE_REQUEST_NOTIFICATION_PENDING:
            state = "notification_pending";
            break;
        case URINGCORE_REQUEST_RELEASED:
            state = "released";
            break;
        default:
            PyErr_SetString(PyExc_SystemError, "request has an invalid state");
            return NULL;
    }
    return PyUnicode_FromString(state);
}

static PyObject *
uringcore_liburing_request_get_kind(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    const char *kind;

    switch (self->kind) {
        case URINGCORE_REQUEST_NOP:
            kind = "nop";
            break;
        case URINGCORE_REQUEST_TIMEOUT:
            kind = "timeout";
            break;
        case URINGCORE_REQUEST_CANCEL:
            kind = "cancel";
            break;
        case URINGCORE_REQUEST_RECV:
            kind = "recv";
            break;
        case URINGCORE_REQUEST_SEND:
            kind = "send";
            break;
        case URINGCORE_REQUEST_SEND_ZC:
            kind = "send_zc";
            break;
        case URINGCORE_REQUEST_READ:
            kind = "read";
            break;
        case URINGCORE_REQUEST_WRITE:
            kind = "write";
            break;
        case URINGCORE_REQUEST_POLL:
            kind = "poll";
            break;
        default:
            PyErr_SetString(PyExc_SystemError, "request has an invalid kind");
            return NULL;
    }
    return PyUnicode_FromString(kind);
}

static PyGetSetDef uringcore_liburing_request_getset[] = {
    {
        "done",
        (getter)uringcore_liburing_request_get_done,
        NULL,
        PyDoc_STR("Whether the request reached a terminal state."),
        NULL,
    },
    {
        "cancelled",
        (getter)uringcore_liburing_request_get_cancelled,
        NULL,
        PyDoc_STR("Whether ring teardown cancelled the request."),
        NULL,
    },
    {
        "result",
        (getter)uringcore_liburing_request_get_result,
        NULL,
        PyDoc_STR("Raw CQE result, or None before completion."),
        NULL,
    },
    {
        "state",
        (getter)uringcore_liburing_request_get_state,
        NULL,
        PyDoc_STR("Current native request lifecycle state."),
        NULL,
    },
    {
        "kind",
        (getter)uringcore_liburing_request_get_kind,
        NULL,
        PyDoc_STR("The native operation kind."),
        NULL,
    },
    {NULL},
};

static PyMemberDef uringcore_liburing_request_members[] = {
    {
        "flags",
        T_UINT,
        offsetof(UringCoreLiburingRequest, flags),
        READONLY,
        PyDoc_STR("Flags from the completion queue entry."),
    },
    {NULL},
};

PyDoc_STRVAR(
    uringcore_liburing_request_doc,
    "A native request whose lifetime is owned by its ring until completion.");

static PyTypeObject UringCoreLiburingRequestType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.Request",
    .tp_basicsize = sizeof(UringCoreLiburingRequest),
    .tp_dealloc = (destructor)uringcore_liburing_request_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_doc = uringcore_liburing_request_doc,
    .tp_members = uringcore_liburing_request_members,
    .tp_getset = uringcore_liburing_request_getset,
    .tp_traverse = (traverseproc)uringcore_liburing_request_traverse,
};

static void
uringcore_liburing_completion_dealloc(UringCoreLiburingCompletion *self)
{
    PyObject_GC_UnTrack(self);
    Py_XDECREF(self->buffer);
    Py_XDECREF(self->request);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
uringcore_liburing_completion_traverse(
    UringCoreLiburingCompletion *self,
    visitproc visit,
    void *arg)
{
    Py_VISIT(self->request);
    Py_VISIT(self->buffer);
    return 0;
}

static PyObject *
uringcore_liburing_completion_get_state(
    UringCoreLiburingCompletion *self,
    void *Py_UNUSED(context))
{
    const char *state;

    switch (self->state) {
        case URINGCORE_REQUEST_SUBMITTED:
            state = "submitted";
            break;
        case URINGCORE_REQUEST_CANCEL_PENDING:
            state = "cancel_pending";
            break;
        case URINGCORE_REQUEST_OPERATION_COMPLETE:
            state = "operation_complete";
            break;
        case URINGCORE_REQUEST_NOTIFICATION_PENDING:
            state = "notification_pending";
            break;
        case URINGCORE_REQUEST_RELEASED:
            state = "released";
            break;
        default:
            PyErr_SetString(
                PyExc_SystemError,
                "completion has an invalid request state");
            return NULL;
    }
    return PyUnicode_FromString(state);
}

static PyMemberDef uringcore_liburing_completion_members[] = {
    {
        "request",
        T_OBJECT_EX,
        offsetof(UringCoreLiburingCompletion, request),
        READONLY,
        PyDoc_STR("The request that produced this completion."),
    },
    {
        "result",
        T_INT,
        offsetof(UringCoreLiburingCompletion, result),
        READONLY,
        PyDoc_STR("Raw CQE result."),
    },
    {
        "flags",
        T_UINT,
        offsetof(UringCoreLiburingCompletion, flags),
        READONLY,
        PyDoc_STR("Flags from the completion queue entry."),
    },
    {
        "final",
        T_BOOL,
        offsetof(UringCoreLiburingCompletion, final),
        READONLY,
        PyDoc_STR("Whether the request released kernel ownership."),
    },
    {
        "buffer",
        T_OBJECT,
        offsetof(UringCoreLiburingCompletion, buffer),
        READONLY,
        PyDoc_STR("Selected provided buffer, or None."),
    },
    {NULL},
};

static PyGetSetDef uringcore_liburing_completion_getset[] = {
    {
        "state",
        (getter)uringcore_liburing_completion_get_state,
        NULL,
        PyDoc_STR("Request state represented by this CQE."),
        NULL,
    },
    {NULL},
};

PyDoc_STRVAR(
    uringcore_liburing_completion_doc,
    "An immutable snapshot of one completion queue entry.");

static PyTypeObject UringCoreLiburingCompletionType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.Completion",
    .tp_basicsize = sizeof(UringCoreLiburingCompletion),
    .tp_dealloc = (destructor)uringcore_liburing_completion_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_doc = uringcore_liburing_completion_doc,
    .tp_members = uringcore_liburing_completion_members,
    .tp_getset = uringcore_liburing_completion_getset,
    .tp_traverse = (traverseproc)uringcore_liburing_completion_traverse,
};

static int
uringcore_provided_buffer_return(UringCoreProvidedBuffer *self, int report_error)
{
    UringCoreLiburingRing *ring = self->ring;
    unsigned char *address;
    int mask;

    if (self->released) {
        return 0;
    }
    if (self->exports != 0) {
        if (report_error) {
            PyErr_SetString(
                PyExc_BufferError,
                "cannot release a provided buffer while views are exported");
        }
        return -1;
    }
    if (ring == NULL || !ring->initialized || ring->buffer_ring == NULL) {
        if (report_error) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "provided-buffer ring is no longer available");
        }
        return -1;
    }
    if (self->buffer_id >= ring->buffer_entries ||
        ring->buffers_in_use == 0) {
        if (report_error) {
            PyErr_SetString(PyExc_SystemError, "invalid provided-buffer state");
        }
        return -1;
    }

    address = ring->buffer_storage +
        (size_t)self->buffer_id * ring->buffer_size;
    mask = io_uring_buf_ring_mask(ring->buffer_entries);
    io_uring_buf_ring_add(
        ring->buffer_ring,
        address,
        ring->buffer_size,
        (unsigned short)self->buffer_id,
        mask,
        0);
    io_uring_buf_ring_advance(ring->buffer_ring, 1);
    ring->buffers_in_use--;
    ring->buffers_available++;
    self->released = 1;
    return 0;
}

static void
uringcore_provided_buffer_dealloc(UringCoreProvidedBuffer *self)
{
    PyObject_GC_UnTrack(self);
    uringcore_provided_buffer_return(self, 0);
    Py_XDECREF(self->ring);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
uringcore_provided_buffer_traverse(
    UringCoreProvidedBuffer *self,
    visitproc visit,
    void *arg)
{
    Py_VISIT(self->ring);
    return 0;
}

static int
uringcore_provided_buffer_getbuffer(
    PyObject *object,
    Py_buffer *view,
    int flags)
{
    UringCoreProvidedBuffer *self = (UringCoreProvidedBuffer *)object;
    unsigned char *address;

    if (self->released) {
        PyErr_SetString(PyExc_BufferError, "provided buffer has been released");
        return -1;
    }
    address = self->ring->buffer_storage +
        (size_t)self->buffer_id * self->ring->buffer_size;
    if (PyBuffer_FillInfo(
            view, object, address, self->length, 0, flags) < 0) {
        return -1;
    }
    self->exports++;
    return 0;
}

static void
uringcore_provided_buffer_releasebuffer(
    PyObject *object,
    Py_buffer *Py_UNUSED(view))
{
    UringCoreProvidedBuffer *self = (UringCoreProvidedBuffer *)object;

    if (self->exports > 0) {
        self->exports--;
    }
}

static PyObject *
uringcore_provided_buffer_release(
    UringCoreProvidedBuffer *self,
    PyObject *Py_UNUSED(ignored))
{
    if (uringcore_provided_buffer_return(self, 1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_provided_buffer_to_bytes(
    UringCoreProvidedBuffer *self,
    PyObject *Py_UNUSED(ignored))
{
    unsigned char *address;

    if (self->released) {
        PyErr_SetString(PyExc_BufferError, "provided buffer has been released");
        return NULL;
    }
    address = self->ring->buffer_storage +
        (size_t)self->buffer_id * self->ring->buffer_size;
    return PyBytes_FromStringAndSize((const char *)address, self->length);
}

static PyMethodDef uringcore_provided_buffer_methods[] = {
    {
        "release",
        (PyCFunction)uringcore_provided_buffer_release,
        METH_NOARGS,
        PyDoc_STR("Return this buffer slot to the kernel buffer ring."),
    },
    {
        "to_bytes",
        (PyCFunction)uringcore_provided_buffer_to_bytes,
        METH_NOARGS,
        PyDoc_STR("Copy the received bytes into a new bytes object."),
    },
    {NULL, NULL, 0, NULL},
};

static PyMemberDef uringcore_provided_buffer_members[] = {
    {
        "buffer_id",
        T_UINT,
        offsetof(UringCoreProvidedBuffer, buffer_id),
        READONLY,
        PyDoc_STR("Kernel-selected buffer identifier."),
    },
    {
        "length",
        T_PYSSIZET,
        offsetof(UringCoreProvidedBuffer, length),
        READONLY,
        PyDoc_STR("Number of received bytes."),
    },
    {
        "released",
        T_BOOL,
        offsetof(UringCoreProvidedBuffer, released),
        READONLY,
        PyDoc_STR("Whether the slot has returned to the buffer ring."),
    },
    {NULL},
};

static PyBufferProcs uringcore_provided_buffer_as_buffer = {
    .bf_getbuffer = uringcore_provided_buffer_getbuffer,
    .bf_releasebuffer = uringcore_provided_buffer_releasebuffer,
};

PyDoc_STRVAR(
    uringcore_provided_buffer_doc,
    "A kernel-selected receive buffer with explicit return semantics.");

static PyTypeObject UringCoreProvidedBufferType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.ProvidedBuffer",
    .tp_basicsize = sizeof(UringCoreProvidedBuffer),
    .tp_dealloc = (destructor)uringcore_provided_buffer_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_doc = uringcore_provided_buffer_doc,
    .tp_methods = uringcore_provided_buffer_methods,
    .tp_members = uringcore_provided_buffer_members,
    .tp_as_buffer = &uringcore_provided_buffer_as_buffer,
    .tp_traverse = (traverseproc)uringcore_provided_buffer_traverse,
};

static PyObject *
uringcore_liburing_ring_new(
    PyTypeObject *type,
    PyObject *Py_UNUSED(args),
    PyObject *Py_UNUSED(kwargs))
{
    return type->tp_alloc(type, 0);
}

static int
uringcore_liburing_ring_init(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {
        "entries",
        "cq_entries",
        "supported_opcodes",
        "flags",
        NULL,
    };
    PyObject *entries_object = NULL;
    PyObject *cq_entries_object = NULL;
    PyObject *supported_opcodes_object = NULL;
    PyObject *flags_object = NULL;
    PyObject *flags_index = NULL;
    PyObject *supported_opcodes = NULL;
    unsigned int entries = 256;
    unsigned int cq_entries;
    unsigned int default_cq_entries;
    unsigned long parsed_flags = 0;
    struct io_uring_params params;
    int result;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "|OOOO:Ring",
            keyword_names,
            &entries_object,
            &cq_entries_object,
            &supported_opcodes_object,
            &flags_object)) {
        return -1;
    }
    if (self->waiting) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "cannot reinitialize a ring while another thread is waiting");
        return -1;
    }
    if (uringcore_liburing_parse_uint(
            entries_object, "entries", 256, &entries) < 0) {
        return -1;
    }
    default_cq_entries = entries <= UINT_MAX / 2 ? entries * 2 : UINT_MAX;
    if (uringcore_liburing_parse_uint(
            cq_entries_object,
            "cq_entries",
            default_cq_entries,
            &cq_entries) < 0) {
        return -1;
    }
    if (cq_entries < entries) {
        PyErr_SetString(
            PyExc_ValueError,
            "cq_entries must be greater than or equal to entries");
        return -1;
    }
    if (supported_opcodes_object != NULL &&
        supported_opcodes_object != Py_None) {
        supported_opcodes = uringcore_liburing_validate_supported_opcodes(
            supported_opcodes_object);
        if (supported_opcodes == NULL) {
            return -1;
        }
    }
    if (flags_object != NULL && flags_object != Py_None) {
        flags_index = PyNumber_Index(flags_object);
        if (flags_index == NULL) {
            Py_XDECREF(supported_opcodes);
            return -1;
        }
        parsed_flags = PyLong_AsUnsignedLong(flags_index);
        Py_DECREF(flags_index);
        if ((parsed_flags == ULONG_MAX && PyErr_Occurred()) ||
            parsed_flags > UINT_MAX) {
            Py_XDECREF(supported_opcodes);
            PyErr_Clear();
            PyErr_Format(
                PyExc_ValueError,
                "flags must be between 0 and %u",
                UINT_MAX);
            return -1;
        }
    }
    if (self->buffers_in_use != 0) {
        Py_XDECREF(supported_opcodes);
        PyErr_SetString(
            PyExc_BufferError,
            "cannot reinitialize a ring while provided buffers are in use");
        return -1;
    }

    uringcore_liburing_ring_close_resources(self);
    Py_CLEAR(self->supported_opcodes);
    memset(&params, 0, sizeof(params));
    params.flags = IORING_SETUP_CQSIZE | (unsigned int)parsed_flags;
    params.cq_entries = cq_entries;

    result = io_uring_queue_init_params(entries, &self->ring, &params);
    if (result < 0) {
        Py_XDECREF(supported_opcodes);
        errno = -result;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }

    self->initialized = 1;
    self->generation++;
    self->cq_dropped = 0;
    self->sq_entries = params.sq_entries;
    self->cq_entries = params.cq_entries;
    self->features = params.features;
    if (supported_opcodes == NULL) {
        supported_opcodes = uringcore_liburing_probe_supported_opcodes(
            &self->ring);
        if (supported_opcodes == NULL) {
            uringcore_liburing_ring_close_resources(self);
            return -1;
        }
    }
    self->supported_opcodes = supported_opcodes;
    return 0;
}

static void
uringcore_liburing_ring_dealloc(UringCoreLiburingRing *self)
{
    PyObject_GC_UnTrack(self);
    uringcore_liburing_ring_close_resources(self);
    Py_CLEAR(self->supported_opcodes);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
uringcore_liburing_ring_traverse(
    UringCoreLiburingRing *self,
    visitproc visit,
    void *arg)
{
    UringCoreLiburingRequest *request;

    Py_VISIT(self->supported_opcodes);
    request = self->request_head;
    while (request != NULL) {
        Py_VISIT(request);
        request = request->next;
    }
    return 0;
}

static int
uringcore_liburing_ring_clear(UringCoreLiburingRing *self)
{
    uringcore_liburing_ring_close_resources(self);
    Py_CLEAR(self->supported_opcodes);
    return 0;
}

static PyObject *
uringcore_liburing_ring_close(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    if (self->waiting) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "cannot close a ring while another thread is waiting");
        return NULL;
    }
    if (self->buffers_in_use != 0) {
        PyErr_SetString(
            PyExc_BufferError,
            "cannot close a ring while provided buffers are in use");
        return NULL;
    }
    uringcore_liburing_ring_close_resources(self);
    Py_RETURN_NONE;
}

static PyObject *
uringcore_liburing_ring_setup_buffer_pool(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {
        "entries",
        "buffer_size",
        "group_id",
        NULL,
    };
    PyObject *entries_object;
    PyObject *buffer_size_object;
    PyObject *group_id_object = NULL;
    PyObject *group_id_index = NULL;
    unsigned int entries;
    unsigned int buffer_size;
    unsigned long parsed_group_id = 0;
    size_t allocation_size;
    unsigned char *storage;
    struct io_uring_buf_ring *buffer_ring;
    int mask;
    int result;
    unsigned int index;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OO|O:setup_buffer_pool",
            keyword_names,
            &entries_object,
            &buffer_size_object,
            &group_id_object)) {
        return NULL;
    }
    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    if (self->waiting) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "cannot set up a buffer pool while another thread is waiting");
        return NULL;
    }
    if (self->buffer_ring != NULL) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "a provided-buffer pool is already registered");
        return NULL;
    }
    if (uringcore_liburing_parse_uint(
            entries_object, "entries", 0, &entries) < 0 ||
        uringcore_liburing_parse_uint(
            buffer_size_object, "buffer_size", 0, &buffer_size) < 0) {
        return NULL;
    }
    if ((entries & (entries - 1)) != 0 || entries > 32768) {
        PyErr_SetString(
            PyExc_ValueError,
            "buffer-pool entries must be a power of two no greater than 32768");
        return NULL;
    }
    if (group_id_object != NULL) {
        group_id_index = PyNumber_Index(group_id_object);
        if (group_id_index == NULL) {
            return NULL;
        }
        parsed_group_id = PyLong_AsUnsignedLong(group_id_index);
        Py_DECREF(group_id_index);
        if ((parsed_group_id == ULONG_MAX && PyErr_Occurred()) ||
            parsed_group_id > USHRT_MAX) {
            PyErr_Clear();
            PyErr_Format(
                PyExc_ValueError,
                "group_id must be between 0 and %u",
                USHRT_MAX);
            return NULL;
        }
    }
    if ((size_t)buffer_size > SIZE_MAX / entries) {
        PyErr_SetString(PyExc_OverflowError, "buffer-pool allocation is too large");
        return NULL;
    }
    allocation_size = (size_t)entries * buffer_size;
    storage = PyMem_RawMalloc(allocation_size);
    if (storage == NULL) {
        return PyErr_NoMemory();
    }
    buffer_ring = io_uring_setup_buf_ring(
        &self->ring,
        entries,
        (int)parsed_group_id,
        0,
        &result);
    if (buffer_ring == NULL) {
        PyMem_RawFree(storage);
        errno = -result;
        return PyErr_SetFromErrno(PyExc_OSError);
    }

    mask = io_uring_buf_ring_mask(entries);
    for (index = 0; index < entries; index++) {
        io_uring_buf_ring_add(
            buffer_ring,
            storage + (size_t)index * buffer_size,
            buffer_size,
            (unsigned short)index,
            mask,
            (int)index);
    }
    io_uring_buf_ring_advance(buffer_ring, (int)entries);
    self->buffer_ring = buffer_ring;
    self->buffer_storage = storage;
    self->buffer_entries = entries;
    self->buffer_size = buffer_size;
    self->buffer_group_id = (unsigned int)parsed_group_id;
    self->buffers_available = entries;
    self->buffers_in_use = 0;
    Py_RETURN_NONE;
}

static UringCoreLiburingRequest *
uringcore_liburing_request_new(
    UringCoreRequestKind kind,
    UringCoreCompletionModel completion_model)
{
    UringCoreLiburingRequest *request;

    request = (UringCoreLiburingRequest *)
        UringCoreLiburingRequestType.tp_alloc(
            &UringCoreLiburingRequestType, 0);
    if (request == NULL) {
        return NULL;
    }
    request->ring = NULL;
    request->previous = NULL;
    request->next = NULL;
    request->state = URINGCORE_REQUEST_PREPARED;
    request->kind = kind;
    request->completion_model = completion_model;
    memset(&request->timeout, 0, sizeof(request->timeout));
    request->cancel_target = NULL;
    request->file_object = NULL;
    memset(&request->buffer, 0, sizeof(request->buffer));
    request->has_buffer = 0;
    request->fd = -1;
    request->result = 0;
    request->flags = 0;
    request->cancelled = 0;
    return request;
}

static struct io_uring_sqe *
uringcore_liburing_ring_get_sqe(UringCoreLiburingRing *self)
{
    struct io_uring_sqe *sqe;

    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    if (self->waiting) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "cannot prepare a request while another thread is waiting");
        return NULL;
    }
    sqe = io_uring_get_sqe(&self->ring);
    if (sqe == NULL) {
        PyErr_SetString(
            PyExc_BufferError,
            "submission queue is full; call submit() before preparing more requests");
        return NULL;
    }
    return sqe;
}

static int
uringcore_liburing_ring_supports_opcode(
    UringCoreLiburingRing *self,
    int opcode)
{
    PyObject *value;
    int supported;

    value = PyLong_FromLong(opcode);
    if (value == NULL) {
        return -1;
    }
    supported = PySet_Contains(self->supported_opcodes, value);
    Py_DECREF(value);
    return supported;
}

static int
uringcore_liburing_ring_require_opcode(
    UringCoreLiburingRing *self,
    int opcode,
    const char *operation)
{
    int supported = uringcore_liburing_ring_supports_opcode(self, opcode);

    if (supported < 0) {
        return -1;
    }
    if (!supported) {
        PyErr_Format(
            PyExc_NotImplementedError,
            "%s is not supported by this io_uring",
            operation);
        return -1;
    }
    return 0;
}

static PyObject *
uringcore_liburing_ring_prepare_nop(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (uringcore_liburing_ring_require_opcode(
            self, IORING_OP_NOP, "NOP") < 0) {
        return NULL;
    }
    request = uringcore_liburing_request_new(
        URINGCORE_REQUEST_NOP,
        URINGCORE_COMPLETION_ONE_SHOT);
    if (request == NULL) {
        return NULL;
    }
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    io_uring_prep_nop(sqe);
    io_uring_sqe_set_data(sqe, request);
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static PyObject *
uringcore_liburing_ring_prepare_timeout(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"delay_ns", NULL};
    PyObject *delay_object;
    PyObject *delay_index;
    unsigned long long delay_ns;
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "O:prepare_timeout", keyword_names, &delay_object)) {
        return NULL;
    }
    if (uringcore_liburing_ring_require_opcode(
            self, IORING_OP_TIMEOUT, "TIMEOUT") < 0) {
        return NULL;
    }
    delay_index = PyNumber_Index(delay_object);
    if (delay_index == NULL) {
        return NULL;
    }
    delay_ns = PyLong_AsUnsignedLongLong(delay_index);
    Py_DECREF(delay_index);
    if ((delay_ns == ULLONG_MAX && PyErr_Occurred()) ||
        delay_ns > (unsigned long long)LLONG_MAX) {
        PyErr_Clear();
        PyErr_SetString(
            PyExc_ValueError,
            "delay_ns must be between 0 and 9223372036854775807");
        return NULL;
    }

    request = uringcore_liburing_request_new(
        URINGCORE_REQUEST_TIMEOUT,
        URINGCORE_COMPLETION_ONE_SHOT);
    if (request == NULL) {
        return NULL;
    }
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    request->timeout.tv_sec = (__kernel_time64_t)(delay_ns / 1000000000ULL);
    request->timeout.tv_nsec = (long)(delay_ns % 1000000000ULL);
    io_uring_prep_timeout(sqe, &request->timeout, 0, 0);
    io_uring_sqe_set_data(sqe, request);
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static PyObject *
uringcore_liburing_ring_prepare_cancel(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"request", NULL};
    UringCoreLiburingRequest *target;
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "O!:prepare_cancel",
            keyword_names,
            &UringCoreLiburingRequestType,
            &target)) {
        return NULL;
    }
    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    if (uringcore_liburing_ring_require_opcode(
            self, IORING_OP_ASYNC_CANCEL, "ASYNC_CANCEL") < 0) {
        return NULL;
    }
    if (target->ring != self) {
        PyErr_SetString(
            PyExc_ValueError,
            "request is not pending on this ring");
        return NULL;
    }
    if (target->state != URINGCORE_REQUEST_SUBMITTED) {
        PyErr_SetString(
            PyExc_ValueError,
            "request must be submitted and not already cancelling");
        return NULL;
    }

    request = uringcore_liburing_request_new(
        URINGCORE_REQUEST_CANCEL,
        URINGCORE_COMPLETION_ONE_SHOT);
    if (request == NULL) {
        return NULL;
    }
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    request->cancel_target = Py_NewRef((PyObject *)target);
    io_uring_prep_cancel(sqe, target, 0);
    io_uring_sqe_set_data(sqe, request);
    target->state = URINGCORE_REQUEST_CANCEL_PENDING;
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static UringCoreLiburingRequest *
uringcore_liburing_prepare_buffer_request(
    UringCoreLiburingRing *self,
    PyObject *file_object,
    PyObject *buffer_object,
    int buffer_flags,
    UringCoreRequestKind kind,
    UringCoreCompletionModel completion_model,
    int opcode,
    const char *operation)
{
    UringCoreLiburingRequest *request;

    if (uringcore_liburing_ring_require_opcode(
            self, opcode, operation) < 0) {
        return NULL;
    }
    request = uringcore_liburing_request_new(kind, completion_model);
    if (request == NULL) {
        return NULL;
    }
    request->fd = PyObject_AsFileDescriptor(file_object);
    if (request->fd < 0) {
        Py_DECREF(request);
        return NULL;
    }
    request->file_object = Py_NewRef(file_object);
    if (PyObject_GetBuffer(
            buffer_object, &request->buffer, buffer_flags) < 0) {
        Py_DECREF(request);
        return NULL;
    }
    request->has_buffer = 1;
    if (request->buffer.len < 0 ||
        (size_t)request->buffer.len > UINT_MAX) {
        PyErr_SetString(
            PyExc_ValueError,
            "buffer length must fit in an unsigned 32-bit io_uring length");
        Py_DECREF(request);
        return NULL;
    }
    return request;
}

static PyObject *
uringcore_liburing_ring_prepare_recv(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"file", "buffer", "flags", NULL};
    PyObject *file_object;
    PyObject *buffer_object;
    int flags = 0;
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OO|i:prepare_recv",
            keyword_names,
            &file_object,
            &buffer_object,
            &flags)) {
        return NULL;
    }
    request = uringcore_liburing_prepare_buffer_request(
        self,
        file_object,
        buffer_object,
        PyBUF_WRITABLE,
        URINGCORE_REQUEST_RECV,
        URINGCORE_COMPLETION_ONE_SHOT,
        IORING_OP_RECV,
        "RECV");
    if (request == NULL) {
        return NULL;
    }
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    io_uring_prep_recv(
        sqe,
        request->fd,
        request->buffer.buf,
        (size_t)request->buffer.len,
        flags);
    io_uring_sqe_set_data(sqe, request);
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static PyObject *
uringcore_liburing_ring_prepare_recv_multishot(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"file", "flags", NULL};
    PyObject *file_object;
    int flags = 0;
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "O|i:prepare_recv_multishot",
            keyword_names,
            &file_object,
            &flags)) {
        return NULL;
    }
    if (self->buffer_ring == NULL) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "prepare_recv_multishot requires a provided-buffer pool");
        return NULL;
    }
    if (uringcore_liburing_ring_require_opcode(
            self, IORING_OP_RECV, "RECV") < 0) {
        return NULL;
    }
    request = uringcore_liburing_request_new(
        URINGCORE_REQUEST_RECV,
        URINGCORE_COMPLETION_MULTISHOT);
    if (request == NULL) {
        return NULL;
    }
    request->fd = PyObject_AsFileDescriptor(file_object);
    if (request->fd < 0) {
        Py_DECREF(request);
        return NULL;
    }
    request->file_object = Py_NewRef(file_object);
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    io_uring_prep_recv_multishot(sqe, request->fd, NULL, 0, flags);
    sqe->buf_group = (unsigned short)self->buffer_group_id;
    io_uring_sqe_set_flags(sqe, IOSQE_BUFFER_SELECT);
    io_uring_sqe_set_data(sqe, request);
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static PyObject *
uringcore_liburing_ring_prepare_send_common(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs,
    int zero_copy)
{
    static char *keyword_names[] = {"file", "buffer", "flags", NULL};
    PyObject *file_object;
    PyObject *buffer_object;
    int flags = 0;
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OO|i:prepare_send",
            keyword_names,
            &file_object,
            &buffer_object,
            &flags)) {
        return NULL;
    }
    request = uringcore_liburing_prepare_buffer_request(
        self,
        file_object,
        buffer_object,
        PyBUF_CONTIG_RO,
        zero_copy ? URINGCORE_REQUEST_SEND_ZC : URINGCORE_REQUEST_SEND,
        zero_copy ? URINGCORE_COMPLETION_NOTIFICATION :
                    URINGCORE_COMPLETION_ONE_SHOT,
        zero_copy ? IORING_OP_SEND_ZC : IORING_OP_SEND,
        zero_copy ? "SEND_ZC" : "SEND");
    if (request == NULL) {
        return NULL;
    }
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    if (zero_copy) {
        io_uring_prep_send_zc(
            sqe,
            request->fd,
            request->buffer.buf,
            (size_t)request->buffer.len,
            flags,
            0);
    }
    else {
        io_uring_prep_send(
            sqe,
            request->fd,
            request->buffer.buf,
            (size_t)request->buffer.len,
            flags);
    }
    io_uring_sqe_set_data(sqe, request);
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static PyObject *
uringcore_liburing_ring_prepare_send(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    return uringcore_liburing_ring_prepare_send_common(
        self, args, kwargs, 0);
}

static PyObject *
uringcore_liburing_ring_prepare_send_zc(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    return uringcore_liburing_ring_prepare_send_common(
        self, args, kwargs, 1);
}

static PyObject *
uringcore_liburing_ring_prepare_file_io(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs,
    int write_operation)
{
    static char *keyword_names[] = {"file", "buffer", "offset", NULL};
    PyObject *file_object;
    PyObject *buffer_object;
    long long offset = -1;
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OO|L:prepare_file_io",
            keyword_names,
            &file_object,
            &buffer_object,
            &offset)) {
        return NULL;
    }
    if (offset < -1) {
        PyErr_SetString(PyExc_ValueError, "offset must be -1 or non-negative");
        return NULL;
    }
    request = uringcore_liburing_prepare_buffer_request(
        self,
        file_object,
        buffer_object,
        write_operation ? PyBUF_CONTIG_RO : PyBUF_WRITABLE,
        write_operation ? URINGCORE_REQUEST_WRITE : URINGCORE_REQUEST_READ,
        URINGCORE_COMPLETION_ONE_SHOT,
        write_operation ? IORING_OP_WRITE : IORING_OP_READ,
        write_operation ? "WRITE" : "READ");
    if (request == NULL) {
        return NULL;
    }
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    if (write_operation) {
        io_uring_prep_write(
            sqe,
            request->fd,
            request->buffer.buf,
            (unsigned int)request->buffer.len,
            (__u64)offset);
    }
    else {
        io_uring_prep_read(
            sqe,
            request->fd,
            request->buffer.buf,
            (unsigned int)request->buffer.len,
            (__u64)offset);
    }
    io_uring_sqe_set_data(sqe, request);
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static PyObject *
uringcore_liburing_ring_prepare_read(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    return uringcore_liburing_ring_prepare_file_io(
        self, args, kwargs, 0);
}

static PyObject *
uringcore_liburing_ring_prepare_write(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    return uringcore_liburing_ring_prepare_file_io(
        self, args, kwargs, 1);
}

static PyObject *
uringcore_liburing_ring_prepare_poll(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"file", "mask", NULL};
    PyObject *file_object;
    unsigned int mask;
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OI:prepare_poll",
            keyword_names,
            &file_object,
            &mask)) {
        return NULL;
    }
    if (uringcore_liburing_ring_require_opcode(
            self, IORING_OP_POLL_ADD, "POLL_ADD") < 0) {
        return NULL;
    }
    request = uringcore_liburing_request_new(
        URINGCORE_REQUEST_POLL,
        URINGCORE_COMPLETION_ONE_SHOT);
    if (request == NULL) {
        return NULL;
    }
    request->fd = PyObject_AsFileDescriptor(file_object);
    if (request->fd < 0) {
        Py_DECREF(request);
        return NULL;
    }
    request->file_object = Py_NewRef(file_object);
    sqe = uringcore_liburing_ring_get_sqe(self);
    if (sqe == NULL) {
        Py_DECREF(request);
        return NULL;
    }
    io_uring_prep_poll_add(sqe, request->fd, mask);
    io_uring_sqe_set_data(sqe, request);
    uringcore_liburing_request_link(self, request);
    return (PyObject *)request;
}

static PyObject *
uringcore_liburing_ring_submit(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    UringCoreLiburingRequest *request;
    unsigned int remaining;
    unsigned int total = 0;
    unsigned long long generation;
    int result;

    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    if (self->waiting) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "cannot submit while another thread is waiting");
        return NULL;
    }
    generation = self->generation;

    while (self->prepared > 0) {
        do {
            result = io_uring_submit(&self->ring);
            if (result == -EINTR && PyErr_CheckSignals() < 0) {
                return NULL;
            }
            if (result == -EINTR &&
                (!self->initialized || self->generation != generation)) {
                PyErr_SetString(
                    PyExc_RuntimeError,
                    "ring changed while submit was interrupted");
                return NULL;
            }
        } while (result == -EINTR);
        if (result < 0) {
            errno = -result;
            PyErr_SetFromErrno(PyExc_OSError);
            return NULL;
        }
        if (result == 0) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "io_uring made no progress submitting prepared requests");
            return NULL;
        }

        remaining = (unsigned int)result;
        total += remaining;
        request = self->request_head;
        while (request != NULL && remaining > 0) {
            if (request->state == URINGCORE_REQUEST_PREPARED) {
                request->state = URINGCORE_REQUEST_SUBMITTED;
                self->prepared--;
                remaining--;
            }
            request = request->next;
        }
        if (remaining != 0) {
            PyErr_SetString(
                PyExc_SystemError,
                "liburing submitted more requests than the ring had prepared");
            return NULL;
        }
    }
    return PyLong_FromUnsignedLong(total);
}

static UringCoreLiburingCompletion *
uringcore_liburing_completion_new(
    UringCoreLiburingRequest *request,
    UringCoreRequestState state,
    int result,
    unsigned int flags,
    int final)
{
    UringCoreLiburingCompletion *completion;

    completion = (UringCoreLiburingCompletion *)
        UringCoreLiburingCompletionType.tp_alloc(
            &UringCoreLiburingCompletionType, 0);
    if (completion == NULL) {
        return NULL;
    }
    completion->request = (UringCoreLiburingRequest *)Py_NewRef(
        (PyObject *)request);
    completion->state = state;
    completion->result = result;
    completion->flags = flags;
    completion->final = final;
    completion->buffer = NULL;
    return completion;
}

static int
uringcore_liburing_completion_claim_buffer(
    UringCoreLiburingCompletion *completion)
{
    UringCoreLiburingRequest *request = completion->request;
    UringCoreLiburingRing *ring = request->ring;
    UringCoreProvidedBuffer *buffer;
    unsigned int buffer_id;

    if (!(completion->flags & IORING_CQE_F_BUFFER)) {
        return 0;
    }
    if (request->kind != URINGCORE_REQUEST_RECV ||
        ring->buffer_ring == NULL || completion->result < 0) {
        PyErr_SetString(
            PyExc_SystemError,
            "unexpected provided buffer on completion");
        return -1;
    }
    buffer_id = completion->flags >> IORING_CQE_BUFFER_SHIFT;
    if (buffer_id >= ring->buffer_entries ||
        (unsigned int)completion->result > ring->buffer_size ||
        ring->buffers_available == 0) {
        PyErr_SetString(
            PyExc_SystemError,
            "kernel returned an invalid provided-buffer selection");
        return -1;
    }

    buffer = (UringCoreProvidedBuffer *)
        UringCoreProvidedBufferType.tp_alloc(
            &UringCoreProvidedBufferType, 0);
    if (buffer == NULL) {
        return -1;
    }
    buffer->ring = (UringCoreLiburingRing *)Py_NewRef((PyObject *)ring);
    buffer->buffer_id = buffer_id;
    buffer->length = completion->result;
    buffer->exports = 0;
    buffer->released = 0;
    ring->buffers_available--;
    ring->buffers_in_use++;
    completion->buffer = (PyObject *)buffer;
    return 0;
}

static int
uringcore_liburing_ring_check_cq_dropped(UringCoreLiburingRing *self)
{
    unsigned int dropped = *self->ring.cq.koverflow;

    if (dropped == self->cq_dropped) {
        return 0;
    }
    self->cq_dropped = dropped;
    PyErr_Format(
        PyExc_BufferError,
        "io_uring dropped CQEs; cumulative dropped count is %u",
        dropped);
    return -1;
}

static PyObject *
uringcore_liburing_ring_reap(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {"max_completions", NULL};
    PyObject *max_completions_object = NULL;
    PyObject *max_completions_index = NULL;
    unsigned long parsed_max_completions;
    unsigned int max_completions = 64;
    PyObject *completed;
    unsigned int reaped = 0;
    unsigned long long generation;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "|O:reap",
            keyword_names,
            &max_completions_object)) {
        return NULL;
    }
    if (max_completions_object != NULL) {
        max_completions_index = PyNumber_Index(max_completions_object);
        if (max_completions_index == NULL) {
            return NULL;
        }
        parsed_max_completions =
            PyLong_AsUnsignedLong(max_completions_index);
        Py_DECREF(max_completions_index);
        if (parsed_max_completions == (unsigned long)-1 &&
            PyErr_Occurred()) {
            PyErr_Clear();
            PyErr_Format(
                PyExc_ValueError,
                "max_completions must be between 1 and %u",
                UINT_MAX);
            return NULL;
        }
        if (parsed_max_completions == 0 ||
            parsed_max_completions > UINT_MAX) {
            PyErr_Format(
                PyExc_ValueError,
                "max_completions must be between 1 and %u",
                UINT_MAX);
            return NULL;
        }
        max_completions = (unsigned int)parsed_max_completions;
    }
    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    if (self->waiting) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "cannot reap while another thread is waiting");
        return NULL;
    }
    if (uringcore_liburing_ring_check_cq_dropped(self) < 0) {
        return NULL;
    }
    generation = self->generation;

    completed = PyList_New(0);
    if (completed == NULL) {
        return NULL;
    }
    while (reaped < max_completions) {
        UringCoreLiburingCompletion *completion;
        UringCoreLiburingRequest *request;
        UringCoreRequestState completion_state;
        struct io_uring_cqe *cqe;
        int final;
        int result;

        do {
            result = io_uring_peek_cqe(&self->ring, &cqe);
            if (result == -EINTR && PyErr_CheckSignals() < 0) {
                Py_DECREF(completed);
                return NULL;
            }
            if (result == -EINTR &&
                (!self->initialized || self->generation != generation)) {
                PyErr_SetString(
                    PyExc_RuntimeError,
                    "ring changed while completion reaping was interrupted");
                Py_DECREF(completed);
                return NULL;
            }
        } while (result == -EINTR);

        if (result == -EAGAIN) {
            break;
        }
        if (result < 0) {
            errno = -result;
            PyErr_SetFromErrno(PyExc_OSError);
            Py_DECREF(completed);
            return NULL;
        }

        request = (UringCoreLiburingRequest *)io_uring_cqe_get_data(cqe);
        if (request == NULL || request->ring != self ||
            (request->state != URINGCORE_REQUEST_SUBMITTED &&
             request->state != URINGCORE_REQUEST_CANCEL_PENDING &&
             request->state != URINGCORE_REQUEST_NOTIFICATION_PENDING)) {
            io_uring_cqe_seen(&self->ring, cqe);
            PyErr_SetString(
                PyExc_SystemError,
                "completion queue entry does not reference a submitted request");
            Py_DECREF(completed);
            return NULL;
        }

        final = 0;
        if (cqe->flags & IORING_CQE_F_NOTIF) {
            if (request->completion_model != URINGCORE_COMPLETION_NOTIFICATION ||
                request->state != URINGCORE_REQUEST_NOTIFICATION_PENDING) {
                io_uring_cqe_seen(&self->ring, cqe);
                PyErr_SetString(
                    PyExc_SystemError,
                    "unexpected notification completion queue entry");
                Py_DECREF(completed);
                return NULL;
            }
            completion_state = URINGCORE_REQUEST_NOTIFICATION_PENDING;
            final = 1;
        }
        else if (request->completion_model ==
                 URINGCORE_COMPLETION_NOTIFICATION) {
            completion_state = URINGCORE_REQUEST_OPERATION_COMPLETE;
            final = !(cqe->flags & IORING_CQE_F_MORE);
        }
        else if (request->completion_model == URINGCORE_COMPLETION_MULTISHOT &&
                 (cqe->flags & IORING_CQE_F_MORE)) {
            completion_state = request->state;
        }
        else {
            if (cqe->flags & IORING_CQE_F_MORE) {
                io_uring_cqe_seen(&self->ring, cqe);
                PyErr_SetString(
                    PyExc_SystemError,
                    "one-shot request produced a multi-completion CQE");
                Py_DECREF(completed);
                return NULL;
            }
            completion_state = URINGCORE_REQUEST_OPERATION_COMPLETE;
            final = 1;
        }

        completion = uringcore_liburing_completion_new(
            request,
            completion_state,
            cqe->res,
            cqe->flags,
            final);
        if (completion == NULL) {
            Py_DECREF(completed);
            return NULL;
        }
        if (uringcore_liburing_completion_claim_buffer(completion) < 0) {
            Py_DECREF(completion);
            Py_DECREF(completed);
            return NULL;
        }

        request->state = completion_state;
        if (!(cqe->flags & IORING_CQE_F_NOTIF)) {
            request->result = cqe->res;
            if (cqe->res == -ECANCELED) {
                request->cancelled = 1;
            }
        }
        request->flags = cqe->flags;
        if (final) {
            request->state = URINGCORE_REQUEST_RELEASED;
        }
        else if (request->completion_model ==
                 URINGCORE_COMPLETION_NOTIFICATION) {
            request->state = URINGCORE_REQUEST_NOTIFICATION_PENDING;
        }
        io_uring_cqe_seen(&self->ring, cqe);
        if (PyList_Append(completed, (PyObject *)completion) < 0) {
            Py_DECREF(completion);
            if (final) {
                uringcore_liburing_request_release_resources(request);
                uringcore_liburing_request_unlink(request);
            }
            Py_DECREF(completed);
            return NULL;
        }
        Py_DECREF(completion);
        if (final) {
            uringcore_liburing_request_release_resources(request);
            uringcore_liburing_request_unlink(request);
        }
        reaped++;
    }
    if (uringcore_liburing_ring_check_cq_dropped(self) < 0) {
        Py_DECREF(completed);
        return NULL;
    }
    return completed;
}

static PyObject *
uringcore_liburing_ring_wait(
    UringCoreLiburingRing *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keyword_names[] = {
        "timeout_ns",
        "max_completions",
        NULL,
    };
    PyObject *timeout_object = Py_None;
    PyObject *max_completions_object = NULL;
    PyObject *timeout_index = NULL;
    PyObject *max_completions_index = NULL;
    PyObject *reap_args;
    PyObject *result_object;
    unsigned long parsed_max_completions;
    unsigned int max_completions = 64;
    unsigned long long timeout_ns = 0;
    int has_timeout = 0;
    int result;
    unsigned long long generation;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "|OO:wait",
            keyword_names,
            &timeout_object,
            &max_completions_object)) {
        return NULL;
    }
    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    if (self->waiting) {
        PyErr_SetString(PyExc_RuntimeError, "another thread is already waiting");
        return NULL;
    }
    if (timeout_object != Py_None) {
        timeout_index = PyNumber_Index(timeout_object);
        if (timeout_index == NULL) {
            return NULL;
        }
        timeout_ns = PyLong_AsUnsignedLongLong(timeout_index);
        Py_DECREF(timeout_index);
        if ((timeout_ns == ULLONG_MAX && PyErr_Occurred()) ||
            timeout_ns > (unsigned long long)LLONG_MAX) {
            PyErr_Clear();
            PyErr_SetString(
                PyExc_ValueError,
                "timeout_ns must be between 0 and 9223372036854775807");
            return NULL;
        }
        has_timeout = 1;
    }
    if (max_completions_object != NULL) {
        max_completions_index = PyNumber_Index(max_completions_object);
        if (max_completions_index == NULL) {
            return NULL;
        }
        parsed_max_completions =
            PyLong_AsUnsignedLong(max_completions_index);
        Py_DECREF(max_completions_index);
        if ((parsed_max_completions == (unsigned long)-1 &&
             PyErr_Occurred()) ||
            parsed_max_completions == 0 ||
            parsed_max_completions > UINT_MAX) {
            PyErr_Clear();
            PyErr_Format(
                PyExc_ValueError,
                "max_completions must be between 1 and %u",
                UINT_MAX);
            return NULL;
        }
        max_completions = (unsigned int)parsed_max_completions;
    }

    generation = self->generation;
    if (io_uring_cq_ready(&self->ring) == 0 &&
        (!has_timeout || timeout_ns != 0)) {
        struct io_uring_cqe *cqe;

        self->waiting = 1;
        do {
            Py_BEGIN_ALLOW_THREADS
            if (has_timeout) {
                struct __kernel_timespec timeout = {
                    .tv_sec = (__kernel_time64_t)(
                        timeout_ns / 1000000000ULL),
                    .tv_nsec = (long)(timeout_ns % 1000000000ULL),
                };
                result = io_uring_wait_cqe_timeout(
                    &self->ring, &cqe, &timeout);
            }
            else {
                result = io_uring_wait_cqe(&self->ring, &cqe);
            }
            Py_END_ALLOW_THREADS
            if (result == -EINTR) {
                self->waiting = 0;
                if (PyErr_CheckSignals() < 0) {
                    return NULL;
                }
                if (!self->initialized || self->generation != generation) {
                    PyErr_SetString(
                        PyExc_RuntimeError,
                        "ring changed while completion wait was interrupted");
                    return NULL;
                }
                if (has_timeout) {
                    /* Return control to the event loop instead of restarting
                     * the full relative timeout after every handled signal. */
                    return PyList_New(0);
                }
                self->waiting = 1;
            }
        } while (result == -EINTR);
        self->waiting = 0;
        if (result == -ETIME) {
            return PyList_New(0);
        }
        if (result < 0) {
            errno = -result;
            return PyErr_SetFromErrno(PyExc_OSError);
        }
    }

    max_completions_object = PyLong_FromUnsignedLong(max_completions);
    if (max_completions_object == NULL) {
        return NULL;
    }
    reap_args = PyTuple_Pack(1, max_completions_object);
    Py_DECREF(max_completions_object);
    if (reap_args == NULL) {
        return NULL;
    }
    result_object = uringcore_liburing_ring_reap(self, reap_args, NULL);
    Py_DECREF(reap_args);
    return result_object;
}

static PyObject *
uringcore_liburing_ring_enter(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    return Py_NewRef(self);
}

static PyObject *
uringcore_liburing_ring_exit(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(args))
{
    PyObject *result = uringcore_liburing_ring_close(self, NULL);

    if (result == NULL) {
        return NULL;
    }
    Py_DECREF(result);
    Py_RETURN_FALSE;
}

static PyObject *
uringcore_liburing_ring_supports(
    UringCoreLiburingRing *self,
    PyObject *opcode_object)
{
    PyObject *index;
    long opcode;
    int supported;

    index = PyNumber_Index(opcode_object);
    if (index == NULL) {
        return NULL;
    }
    opcode = PyLong_AsLong(index);
    Py_DECREF(index);
    if ((opcode == -1 && PyErr_Occurred()) ||
        opcode < 0 || opcode >= IORING_OP_LAST) {
        PyErr_Clear();
        PyErr_Format(
            PyExc_ValueError,
            "opcode must be between 0 and %d",
            IORING_OP_LAST - 1);
        return NULL;
    }
    supported = uringcore_liburing_ring_supports_opcode(self, (int)opcode);
    if (supported < 0) {
        return NULL;
    }
    return PyBool_FromLong(supported);
}

static PyObject *
uringcore_liburing_ring_get_closed(
    UringCoreLiburingRing *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(!self->initialized);
}

static PyObject *
uringcore_liburing_ring_get_supported_opcodes(
    UringCoreLiburingRing *self,
    void *Py_UNUSED(context))
{
    return Py_NewRef(self->supported_opcodes);
}

static PyObject *
uringcore_liburing_ring_get_cq_ready(
    UringCoreLiburingRing *self,
    void *Py_UNUSED(context))
{
    if (!self->initialized) {
        return PyLong_FromLong(0);
    }
    return PyLong_FromUnsignedLong(io_uring_cq_ready(&self->ring));
}

static PyObject *
uringcore_liburing_ring_get_cq_overflowed(
    UringCoreLiburingRing *self,
    void *Py_UNUSED(context))
{
    if (!self->initialized) {
        Py_RETURN_FALSE;
    }
    return PyBool_FromLong(io_uring_cq_has_overflow(&self->ring));
}

static PyObject *
uringcore_liburing_ring_get_cq_dropped(
    UringCoreLiburingRing *self,
    void *Py_UNUSED(context))
{
    unsigned int dropped = self->cq_dropped;

    if (self->initialized) {
        dropped = *self->ring.cq.koverflow;
    }
    return PyLong_FromUnsignedLong(dropped);
}

static PyObject *
uringcore_liburing_ring_get_buffer_pool_bytes(
    UringCoreLiburingRing *self,
    void *Py_UNUSED(context))
{
    size_t bytes = (size_t)self->buffer_entries * self->buffer_size;

    return PyLong_FromSize_t(bytes);
}

static PyMethodDef uringcore_liburing_ring_methods[] = {
    {
        "prepare_nop",
        (PyCFunction)uringcore_liburing_ring_prepare_nop,
        METH_NOARGS,
        PyDoc_STR("Prepare one native no-op request without submitting it."),
    },
    {
        "setup_buffer_pool",
        _PyCFunction_CAST(uringcore_liburing_ring_setup_buffer_pool),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Allocate and register one bounded provided-buffer pool."),
    },
    {
        "prepare_timeout",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_timeout),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare a relative timeout expressed in nanoseconds."),
    },
    {
        "prepare_cancel",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_cancel),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare asynchronous cancellation of a submitted request."),
    },
    {
        "prepare_recv",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_recv),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare one receive into a caller-owned writable buffer."),
    },
    {
        "prepare_recv_multishot",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_recv_multishot),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare multishot receive using the provided-buffer pool."),
    },
    {
        "prepare_send",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_send),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare one copying socket send."),
    },
    {
        "prepare_send_zc",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_send_zc),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare zero-copy send with notification lifetime."),
    },
    {
        "prepare_read",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_read),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare one file read into a writable buffer."),
    },
    {
        "prepare_write",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_write),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare one file write from a readable buffer."),
    },
    {
        "prepare_poll",
        _PyCFunction_CAST(uringcore_liburing_ring_prepare_poll),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Prepare one poll request."),
    },
    {
        "submit",
        (PyCFunction)uringcore_liburing_ring_submit,
        METH_NOARGS,
        PyDoc_STR("Submit the requests currently prepared on the ring."),
    },
    {
        "reap",
        _PyCFunction_CAST(uringcore_liburing_ring_reap),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Return immutable snapshots of up to max_completions CQEs."),
    },
    {
        "wait",
        _PyCFunction_CAST(uringcore_liburing_ring_wait),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Wait for at least one CQE, then reap one batch."),
    },
    {
        "supports",
        (PyCFunction)uringcore_liburing_ring_supports,
        METH_O,
        PyDoc_STR("Return whether runtime probing reported an opcode."),
    },
    {
        "close",
        (PyCFunction)uringcore_liburing_ring_close,
        METH_NOARGS,
        PyDoc_STR("Release the liburing ring resources."),
    },
    {
        "__enter__",
        (PyCFunction)uringcore_liburing_ring_enter,
        METH_NOARGS,
        NULL,
    },
    {
        "__exit__",
        (PyCFunction)uringcore_liburing_ring_exit,
        METH_VARARGS,
        NULL,
    },
    {NULL, NULL, 0, NULL},
};

static PyMemberDef uringcore_liburing_ring_members[] = {
    {
        "pending",
        T_UINT,
        offsetof(UringCoreLiburingRing, pending),
        READONLY,
        PyDoc_STR("Number of prepared or submitted requests owned by the ring."),
    },
    {
        "sq_entries",
        T_UINT,
        offsetof(UringCoreLiburingRing, sq_entries),
        READONLY,
        PyDoc_STR("Number of submission queue entries allocated by the kernel."),
    },
    {
        "cq_entries",
        T_UINT,
        offsetof(UringCoreLiburingRing, cq_entries),
        READONLY,
        PyDoc_STR("Number of completion queue entries allocated by the kernel."),
    },
    {
        "features",
        T_UINT,
        offsetof(UringCoreLiburingRing, features),
        READONLY,
        PyDoc_STR("Feature flags returned by io_uring_setup."),
    },
    {
        "buffer_pool_entries",
        T_UINT,
        offsetof(UringCoreLiburingRing, buffer_entries),
        READONLY,
        PyDoc_STR("Number of buffers allocated in the registered pool."),
    },
    {
        "buffer_size",
        T_UINT,
        offsetof(UringCoreLiburingRing, buffer_size),
        READONLY,
        PyDoc_STR("Bytes allocated for each provided buffer."),
    },
    {
        "buffer_group_id",
        T_UINT,
        offsetof(UringCoreLiburingRing, buffer_group_id),
        READONLY,
        PyDoc_STR("Kernel buffer-group identifier."),
    },
    {
        "buffers_available",
        T_UINT,
        offsetof(UringCoreLiburingRing, buffers_available),
        READONLY,
        PyDoc_STR("Buffers currently available for kernel selection."),
    },
    {
        "buffers_in_use",
        T_UINT,
        offsetof(UringCoreLiburingRing, buffers_in_use),
        READONLY,
        PyDoc_STR("Buffers retained by completion consumers."),
    },
    {NULL},
};

static PyGetSetDef uringcore_liburing_ring_getset[] = {
    {
        "closed",
        (getter)uringcore_liburing_ring_get_closed,
        NULL,
        PyDoc_STR("Whether the liburing ring resources have been released."),
        NULL,
    },
    {
        "supported_opcodes",
        (getter)uringcore_liburing_ring_get_supported_opcodes,
        NULL,
        PyDoc_STR("Immutable opcode set from probing or test injection."),
        NULL,
    },
    {
        "cq_ready",
        (getter)uringcore_liburing_ring_get_cq_ready,
        NULL,
        PyDoc_STR("Number of CQEs currently ready to reap."),
        NULL,
    },
    {
        "cq_overflowed",
        (getter)uringcore_liburing_ring_get_cq_overflowed,
        NULL,
        PyDoc_STR("Whether the kernel is using the CQ overflow path."),
        NULL,
    },
    {
        "cq_dropped",
        (getter)uringcore_liburing_ring_get_cq_dropped,
        NULL,
        PyDoc_STR("Number of CQEs the kernel reports as dropped."),
        NULL,
    },
    {
        "buffer_pool_bytes",
        (getter)uringcore_liburing_ring_get_buffer_pool_bytes,
        NULL,
        PyDoc_STR("Total bytes reserved by the provided-buffer pool."),
        NULL,
    },
    {NULL},
};

PyDoc_STRVAR(
    uringcore_liburing_ring_doc,
    "Ring(entries=256, cq_entries=None, supported_opcodes=None, flags=0)\n"
    "--\n"
    "\n"
    "Own an io_uring lifecycle through statically linked liburing.");

static PyTypeObject UringCoreLiburingRingType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.Ring",
    .tp_basicsize = sizeof(UringCoreLiburingRing),
    .tp_dealloc = (destructor)uringcore_liburing_ring_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_doc = uringcore_liburing_ring_doc,
    .tp_methods = uringcore_liburing_ring_methods,
    .tp_members = uringcore_liburing_ring_members,
    .tp_getset = uringcore_liburing_ring_getset,
    .tp_init = (initproc)uringcore_liburing_ring_init,
    .tp_new = uringcore_liburing_ring_new,
    .tp_traverse = (traverseproc)uringcore_liburing_ring_traverse,
    .tp_clear = (inquiry)uringcore_liburing_ring_clear,
};

static PyModuleDef uringcore_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_uringcore",
    .m_doc = "Native ring primitives backed by statically linked liburing.",
    .m_size = -1,
};

PyMODINIT_FUNC
PyInit__uringcore(void)
{
    PyObject *module;

    if (PyType_Ready(&UringCoreProtocolDispatcherType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreLoopCoreType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreEventFDType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreLiburingRequestType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreLiburingCompletionType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreProvidedBufferType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreLiburingRingType) < 0) {
        return NULL;
    }

    module = PyModule_Create(&uringcore_module);
    if (module == NULL) {
        return NULL;
    }

    if (PyModule_AddObjectRef(
            module,
            "ProtocolDispatcher",
            (PyObject *)&UringCoreProtocolDispatcherType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddObjectRef(
            module,
            "LoopCore",
            (PyObject *)&UringCoreLoopCoreType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddObjectRef(
            module,
            "EventFD",
            (PyObject *)&UringCoreEventFDType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddObjectRef(
            module,
            "Ring",
            (PyObject *)&UringCoreLiburingRingType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddObjectRef(
            module,
            "Request",
            (PyObject *)&UringCoreLiburingRequestType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddObjectRef(
            module,
            "Completion",
            (PyObject *)&UringCoreLiburingCompletionType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddObjectRef(
            module,
            "ProvidedBuffer",
            (PyObject *)&UringCoreProvidedBufferType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddIntConstant(module, "ABI_VERSION", 13) < 0 ||
        PyModule_AddIntConstant(module, "OP_NOP", IORING_OP_NOP) < 0 ||
        PyModule_AddIntConstant(module, "OP_TIMEOUT", IORING_OP_TIMEOUT) < 0 ||
        PyModule_AddIntConstant(
            module, "OP_ASYNC_CANCEL", IORING_OP_ASYNC_CANCEL) < 0 ||
        PyModule_AddIntConstant(module, "OP_RECV", IORING_OP_RECV) < 0 ||
        PyModule_AddIntConstant(module, "OP_SEND", IORING_OP_SEND) < 0 ||
        PyModule_AddIntConstant(module, "OP_READ", IORING_OP_READ) < 0 ||
        PyModule_AddIntConstant(module, "OP_WRITE", IORING_OP_WRITE) < 0 ||
        PyModule_AddIntConstant(
            module, "OP_POLL_ADD", IORING_OP_POLL_ADD) < 0 ||
        PyModule_AddIntConstant(module, "OP_SEND_ZC", IORING_OP_SEND_ZC) < 0 ||
        PyModule_AddIntConstant(
            module, "OP_PROVIDE_BUFFERS", IORING_OP_PROVIDE_BUFFERS) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
