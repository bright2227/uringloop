#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

#include <errno.h>
#include <limits.h>
#include <liburing.h>
#include <stddef.h>
#include <string.h>

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
} UringCoreRequestKind;

typedef enum {
    URINGCORE_COMPLETION_ONE_SHOT = 0,
    URINGCORE_COMPLETION_MULTISHOT,
    URINGCORE_COMPLETION_NOTIFICATION,
} UringCoreCompletionModel;

typedef struct UringCoreLiburingRing UringCoreLiburingRing;
typedef struct UringCoreLiburingRequest UringCoreLiburingRequest;
typedef struct UringCoreLiburingCompletion UringCoreLiburingCompletion;

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
    unsigned long long generation;
};

static PyTypeObject UringCoreLiburingRequestType;
static PyTypeObject UringCoreLiburingCompletionType;

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
uringcore_liburing_ring_close_resources(UringCoreLiburingRing *self)
{
    UringCoreLiburingRequest *request;

    if (self->initialized) {
        self->generation++;
        io_uring_queue_exit(&self->ring);
        memset(&self->ring, 0, sizeof(self->ring));
        self->initialized = 0;
    }
    while ((request = self->request_head) != NULL) {
        request->state = URINGCORE_REQUEST_RELEASED;
        request->result = -ECANCELED;
        request->flags = 0;
        request->cancelled = 1;
        uringcore_liburing_request_unlink(request);
    }
    self->pending = 0;
    self->prepared = 0;
}

static void
uringcore_liburing_request_dealloc(UringCoreLiburingRequest *self)
{
    Py_XDECREF(self->cancel_target);
    Py_TYPE(self)->tp_free((PyObject *)self);
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
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = uringcore_liburing_request_doc,
    .tp_members = uringcore_liburing_request_members,
    .tp_getset = uringcore_liburing_request_getset,
};

static void
uringcore_liburing_completion_dealloc(UringCoreLiburingCompletion *self)
{
    Py_XDECREF(self->request);
    Py_TYPE(self)->tp_free((PyObject *)self);
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
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = uringcore_liburing_completion_doc,
    .tp_members = uringcore_liburing_completion_members,
    .tp_getset = uringcore_liburing_completion_getset,
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
    static char *keyword_names[] = {"entries", NULL};
    PyObject *entries_object = NULL;
    PyObject *entries_index = NULL;
    unsigned long parsed_entries;
    unsigned int entries = 256;
    struct io_uring_params params;
    int result;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "|O:Ring", keyword_names, &entries_object)) {
        return -1;
    }
    if (entries_object != NULL) {
        entries_index = PyNumber_Index(entries_object);
        if (entries_index == NULL) {
            return -1;
        }
        parsed_entries = PyLong_AsUnsignedLong(entries_index);
        Py_DECREF(entries_index);
        if (parsed_entries == (unsigned long)-1 && PyErr_Occurred()) {
            PyErr_Clear();
            PyErr_Format(
                PyExc_ValueError,
                "entries must be between 1 and %u",
                UINT_MAX);
            return -1;
        }
        if (parsed_entries == 0 || parsed_entries > UINT_MAX) {
            PyErr_Format(
                PyExc_ValueError,
                "entries must be between 1 and %u",
                UINT_MAX);
            return -1;
        }
        entries = (unsigned int)parsed_entries;
    }

    uringcore_liburing_ring_close_resources(self);
    memset(&params, 0, sizeof(params));

    result = io_uring_queue_init_params(entries, &self->ring, &params);
    if (result < 0) {
        errno = -result;
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }

    self->initialized = 1;
    self->generation++;
    self->sq_entries = params.sq_entries;
    self->cq_entries = params.cq_entries;
    self->features = params.features;
    return 0;
}

static void
uringcore_liburing_ring_dealloc(UringCoreLiburingRing *self)
{
    uringcore_liburing_ring_close_resources(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
uringcore_liburing_ring_close(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    uringcore_liburing_ring_close_resources(self);
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
    sqe = io_uring_get_sqe(&self->ring);
    if (sqe == NULL) {
        PyErr_SetString(
            PyExc_BufferError,
            "submission queue is full; call submit() before preparing more requests");
        return NULL;
    }
    return sqe;
}

static PyObject *
uringcore_liburing_ring_prepare_nop(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

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

static PyObject *
uringcore_liburing_ring_submit(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    UringCoreLiburingRequest *request;
    unsigned int remaining;
    unsigned long long generation;
    int result;

    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    generation = self->generation;

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

    remaining = (unsigned int)result;
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
    return PyLong_FromLong(result);
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
    return completion;
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
                uringcore_liburing_request_unlink(request);
            }
            Py_DECREF(completed);
            return NULL;
        }
        Py_DECREF(completion);
        if (final) {
            uringcore_liburing_request_unlink(request);
        }
        reaped++;
    }
    return completed;
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
    uringcore_liburing_ring_close_resources(self);
    Py_RETURN_FALSE;
}

static PyObject *
uringcore_liburing_ring_get_closed(
    UringCoreLiburingRing *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(!self->initialized);
}

static PyMethodDef uringcore_liburing_ring_methods[] = {
    {
        "prepare_nop",
        (PyCFunction)uringcore_liburing_ring_prepare_nop,
        METH_NOARGS,
        PyDoc_STR("Prepare one native no-op request without submitting it."),
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
    {NULL},
};

PyDoc_STRVAR(
    uringcore_liburing_ring_doc,
    "Ring(entries=256)\n"
    "--\n"
    "\n"
    "Own an io_uring lifecycle through statically linked liburing.");

static PyTypeObject UringCoreLiburingRingType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.Ring",
    .tp_basicsize = sizeof(UringCoreLiburingRing),
    .tp_dealloc = (destructor)uringcore_liburing_ring_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = uringcore_liburing_ring_doc,
    .tp_methods = uringcore_liburing_ring_methods,
    .tp_members = uringcore_liburing_ring_members,
    .tp_getset = uringcore_liburing_ring_getset,
    .tp_init = (initproc)uringcore_liburing_ring_init,
    .tp_new = uringcore_liburing_ring_new,
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

    if (PyType_Ready(&UringCoreLiburingRequestType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreLiburingCompletionType) < 0) {
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
    if (PyModule_AddIntConstant(module, "ABI_VERSION", 3) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
