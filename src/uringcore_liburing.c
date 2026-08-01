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
    URINGCORE_REQUEST_COMPLETED,
    URINGCORE_REQUEST_CANCELLED,
} UringCoreRequestState;

typedef struct UringCoreLiburingRing UringCoreLiburingRing;
typedef struct UringCoreLiburingRequest UringCoreLiburingRequest;

struct UringCoreLiburingRequest {
    PyObject_HEAD
    UringCoreLiburingRing *ring;
    UringCoreLiburingRequest *previous;
    UringCoreLiburingRequest *next;
    UringCoreRequestState state;
    int result;
    unsigned int flags;
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
};

static PyTypeObject UringCoreLiburingRequestType;

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
    if (request->state == URINGCORE_REQUEST_PREPARED) {
        ring->prepared--;
    }
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
        io_uring_queue_exit(&self->ring);
        memset(&self->ring, 0, sizeof(self->ring));
        self->initialized = 0;
    }
    while ((request = self->request_head) != NULL) {
        request->state = URINGCORE_REQUEST_CANCELLED;
        request->result = -ECANCELED;
        request->flags = 0;
        uringcore_liburing_request_unlink(request);
    }
    self->pending = 0;
    self->prepared = 0;
}

static void
uringcore_liburing_request_dealloc(UringCoreLiburingRequest *self)
{
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
uringcore_liburing_request_get_done(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(
        self->state == URINGCORE_REQUEST_COMPLETED ||
        self->state == URINGCORE_REQUEST_CANCELLED);
}

static PyObject *
uringcore_liburing_request_get_cancelled(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(self->state == URINGCORE_REQUEST_CANCELLED);
}

static PyObject *
uringcore_liburing_request_get_result(
    UringCoreLiburingRequest *self,
    void *Py_UNUSED(context))
{
    if (self->state != URINGCORE_REQUEST_COMPLETED &&
        self->state != URINGCORE_REQUEST_CANCELLED) {
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
        case URINGCORE_REQUEST_COMPLETED:
            state = "completed";
            break;
        case URINGCORE_REQUEST_CANCELLED:
            state = "cancelled";
            break;
        default:
            PyErr_SetString(PyExc_SystemError, "request has an invalid state");
            return NULL;
    }
    return PyUnicode_FromString(state);
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
    .tp_name = "uringloop._uringcore_liburing.Request",
    .tp_basicsize = sizeof(UringCoreLiburingRequest),
    .tp_dealloc = (destructor)uringcore_liburing_request_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = uringcore_liburing_request_doc,
    .tp_members = uringcore_liburing_request_members,
    .tp_getset = uringcore_liburing_request_getset,
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

static PyObject *
uringcore_liburing_ring_prepare_nop(
    UringCoreLiburingRing *self,
    PyObject *Py_UNUSED(ignored))
{
    UringCoreLiburingRequest *request;
    struct io_uring_sqe *sqe;

    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }

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
    request->result = 0;
    request->flags = 0;

    sqe = io_uring_get_sqe(&self->ring);
    if (sqe == NULL) {
        Py_DECREF(request);
        PyErr_SetString(
            PyExc_BufferError,
            "submission queue is full; call submit() before preparing more requests");
        return NULL;
    }
    io_uring_prep_nop(sqe);
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
    int result;

    if (!self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }

    do {
        result = io_uring_submit(&self->ring);
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

    completed = PyList_New(0);
    if (completed == NULL) {
        return NULL;
    }
    while (reaped < max_completions) {
        UringCoreLiburingRequest *request;
        struct io_uring_cqe *cqe;
        int result = io_uring_peek_cqe(&self->ring, &cqe);

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
            request->state != URINGCORE_REQUEST_SUBMITTED) {
            io_uring_cqe_seen(&self->ring, cqe);
            PyErr_SetString(
                PyExc_SystemError,
                "completion queue entry does not reference a submitted request");
            Py_DECREF(completed);
            return NULL;
        }

        request->state = URINGCORE_REQUEST_COMPLETED;
        request->result = cqe->res;
        request->flags = cqe->flags;
        io_uring_cqe_seen(&self->ring, cqe);
        if (PyList_Append(completed, (PyObject *)request) < 0) {
            uringcore_liburing_request_unlink(request);
            Py_DECREF(completed);
            return NULL;
        }
        uringcore_liburing_request_unlink(request);
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
        "submit",
        (PyCFunction)uringcore_liburing_ring_submit,
        METH_NOARGS,
        PyDoc_STR("Submit the requests currently prepared on the ring."),
    },
    {
        "reap",
        _PyCFunction_CAST(uringcore_liburing_ring_reap),
        METH_VARARGS | METH_KEYWORDS,
        PyDoc_STR("Return up to max_completions completed native requests."),
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
    .tp_name = "uringloop._uringcore_liburing.Ring",
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

static PyModuleDef uringcore_liburing_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_uringcore_liburing",
    .m_doc = "Statically linked liburing ring primitives.",
    .m_size = -1,
};

PyMODINIT_FUNC
PyInit__uringcore_liburing(void)
{
    PyObject *module;

    if (PyType_Ready(&UringCoreLiburingRequestType) < 0) {
        return NULL;
    }
    if (PyType_Ready(&UringCoreLiburingRingType) < 0) {
        return NULL;
    }

    module = PyModule_Create(&uringcore_liburing_module);
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
    if (PyModule_AddIntConstant(module, "ABI_VERSION", 2) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
