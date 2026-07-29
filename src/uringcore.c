#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

#include <errno.h>
#include <linux/io_uring.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

typedef struct {
    PyObject_HEAD
    int ring_fd;
    void *sq_ring;
    size_t sq_ring_size;
    void *cq_ring;
    size_t cq_ring_size;
    void *sqes;
    size_t sqes_size;
    unsigned int sq_entries;
    unsigned int cq_entries;
    unsigned int features;
} UringCoreRing;

static void
uringcore_ring_close_resources(UringCoreRing *self)
{
    if (self->sqes != NULL) {
        munmap(self->sqes, self->sqes_size);
        self->sqes = NULL;
        self->sqes_size = 0;
    }

    if (self->cq_ring != NULL) {
        munmap(self->cq_ring, self->cq_ring_size);
        self->cq_ring = NULL;
        self->cq_ring_size = 0;
    }

    if (self->sq_ring != NULL) {
        munmap(self->sq_ring, self->sq_ring_size);
        self->sq_ring = NULL;
        self->sq_ring_size = 0;
    }

    if (self->ring_fd >= 0) {
        close(self->ring_fd);
        self->ring_fd = -1;
    }
}

static PyObject *
uringcore_ring_new(
    PyTypeObject *type,
    PyObject *Py_UNUSED(args),
    PyObject *Py_UNUSED(kwargs))
{
    UringCoreRing *self = (UringCoreRing *)type->tp_alloc(type, 0);

    if (self != NULL) {
        self->ring_fd = -1;
    }
    return (PyObject *)self;
}

static int
uringcore_ring_init(UringCoreRing *self, PyObject *args, PyObject *kwargs)
{
    static char *keyword_names[] = {"entries", NULL};
    unsigned int entries = 256;
    struct io_uring_params params;
    size_t sq_ring_size;
    size_t cq_ring_size;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "|I:Ring", keyword_names, &entries)) {
        return -1;
    }
    if (entries == 0) {
        PyErr_SetString(PyExc_ValueError, "entries must be greater than zero");
        return -1;
    }

    uringcore_ring_close_resources(self);
    memset(&params, 0, sizeof(params));

    self->ring_fd = (int)syscall(__NR_io_uring_setup, entries, &params);
    if (self->ring_fd < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return -1;
    }

    sq_ring_size =
        params.sq_off.array + params.sq_entries * sizeof(unsigned int);
    cq_ring_size =
        params.cq_off.cqes + params.cq_entries * sizeof(struct io_uring_cqe);

    if ((params.features & IORING_FEAT_SINGLE_MMAP) != 0) {
        self->sq_ring_size =
            sq_ring_size > cq_ring_size ? sq_ring_size : cq_ring_size;
        self->sq_ring = mmap(
            NULL,
            self->sq_ring_size,
            PROT_READ | PROT_WRITE,
            MAP_SHARED | MAP_POPULATE,
            self->ring_fd,
            IORING_OFF_SQ_RING);
        if (self->sq_ring == MAP_FAILED) {
            self->sq_ring = NULL;
            PyErr_SetFromErrno(PyExc_OSError);
            goto error;
        }
    } else {
        self->sq_ring_size = sq_ring_size;
        self->sq_ring = mmap(
            NULL,
            self->sq_ring_size,
            PROT_READ | PROT_WRITE,
            MAP_SHARED | MAP_POPULATE,
            self->ring_fd,
            IORING_OFF_SQ_RING);
        if (self->sq_ring == MAP_FAILED) {
            self->sq_ring = NULL;
            PyErr_SetFromErrno(PyExc_OSError);
            goto error;
        }

        self->cq_ring_size = cq_ring_size;
        self->cq_ring = mmap(
            NULL,
            self->cq_ring_size,
            PROT_READ | PROT_WRITE,
            MAP_SHARED | MAP_POPULATE,
            self->ring_fd,
            IORING_OFF_CQ_RING);
        if (self->cq_ring == MAP_FAILED) {
            self->cq_ring = NULL;
            PyErr_SetFromErrno(PyExc_OSError);
            goto error;
        }
    }

    self->sqes_size =
        params.sq_entries * sizeof(struct io_uring_sqe);
    self->sqes = mmap(
        NULL,
        self->sqes_size,
        PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_POPULATE,
        self->ring_fd,
        IORING_OFF_SQES);
    if (self->sqes == MAP_FAILED) {
        self->sqes = NULL;
        PyErr_SetFromErrno(PyExc_OSError);
        goto error;
    }

    self->sq_entries = params.sq_entries;
    self->cq_entries = params.cq_entries;
    self->features = params.features;
    return 0;

error:
    uringcore_ring_close_resources(self);
    return -1;
}

static void
uringcore_ring_dealloc(UringCoreRing *self)
{
    uringcore_ring_close_resources(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
uringcore_ring_close(UringCoreRing *self, PyObject *Py_UNUSED(ignored))
{
    uringcore_ring_close_resources(self);
    Py_RETURN_NONE;
}

static PyObject *
uringcore_ring_enter(UringCoreRing *self, PyObject *Py_UNUSED(ignored))
{
    if (self->ring_fd < 0) {
        PyErr_SetString(PyExc_RuntimeError, "ring is closed");
        return NULL;
    }
    return Py_NewRef(self);
}

static PyObject *
uringcore_ring_exit(
    UringCoreRing *self,
    PyObject *Py_UNUSED(args))
{
    uringcore_ring_close_resources(self);
    Py_RETURN_FALSE;
}

static PyObject *
uringcore_ring_get_closed(
    UringCoreRing *self,
    void *Py_UNUSED(context))
{
    return PyBool_FromLong(self->ring_fd < 0);
}

static PyMethodDef uringcore_ring_methods[] = {
    {
        "close",
        (PyCFunction)uringcore_ring_close,
        METH_NOARGS,
        PyDoc_STR("Release the ring mappings and file descriptor."),
    },
    {
        "__enter__",
        (PyCFunction)uringcore_ring_enter,
        METH_NOARGS,
        NULL,
    },
    {
        "__exit__",
        (PyCFunction)uringcore_ring_exit,
        METH_VARARGS,
        NULL,
    },
    {NULL, NULL, 0, NULL},
};

static PyMemberDef uringcore_ring_members[] = {
    {
        "sq_entries",
        T_UINT,
        offsetof(UringCoreRing, sq_entries),
        READONLY,
        PyDoc_STR("Number of submission queue entries allocated by the kernel."),
    },
    {
        "cq_entries",
        T_UINT,
        offsetof(UringCoreRing, cq_entries),
        READONLY,
        PyDoc_STR("Number of completion queue entries allocated by the kernel."),
    },
    {
        "features",
        T_UINT,
        offsetof(UringCoreRing, features),
        READONLY,
        PyDoc_STR("Feature flags returned by io_uring_setup."),
    },
    {NULL},
};

static PyGetSetDef uringcore_ring_getset[] = {
    {
        "closed",
        (getter)uringcore_ring_get_closed,
        NULL,
        PyDoc_STR("Whether the native ring resources have been released."),
        NULL,
    },
    {NULL},
};

PyDoc_STRVAR(
    uringcore_ring_doc,
    "Ring(entries=256)\n"
    "--\n"
    "\n"
    "Own an io_uring file descriptor and its SQ, CQ, and SQE mappings.");

static PyTypeObject UringCoreRingType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.Ring",
    .tp_basicsize = sizeof(UringCoreRing),
    .tp_dealloc = (destructor)uringcore_ring_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = uringcore_ring_doc,
    .tp_methods = uringcore_ring_methods,
    .tp_members = uringcore_ring_members,
    .tp_getset = uringcore_ring_getset,
    .tp_init = (initproc)uringcore_ring_init,
    .tp_new = uringcore_ring_new,
};

static PyModuleDef uringcore_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_uringcore",
    .m_doc = "Native io_uring ring primitives.",
    .m_size = -1,
};

PyMODINIT_FUNC
PyInit__uringcore(void)
{
    PyObject *module;

    if (PyType_Ready(&UringCoreRingType) < 0) {
        return NULL;
    }

    module = PyModule_Create(&uringcore_module);
    if (module == NULL) {
        return NULL;
    }

    if (PyModule_AddObjectRef(
            module, "Ring", (PyObject *)&UringCoreRingType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddIntConstant(module, "ABI_VERSION", 1) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
