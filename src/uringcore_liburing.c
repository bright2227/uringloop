#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

#include <errno.h>
#include <limits.h>
#include <liburing.h>
#include <stddef.h>
#include <string.h>

typedef struct {
    PyObject_HEAD
    struct io_uring ring;
    int initialized;
    unsigned int sq_entries;
    unsigned int cq_entries;
    unsigned int features;
} UringCoreLiburingRing;

static void
uringcore_liburing_ring_close_resources(UringCoreLiburingRing *self)
{
    if (self->initialized) {
        io_uring_queue_exit(&self->ring);
        memset(&self->ring, 0, sizeof(self->ring));
        self->initialized = 0;
    }
}

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
    if (PyModule_AddIntConstant(module, "ABI_VERSION", 1) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
