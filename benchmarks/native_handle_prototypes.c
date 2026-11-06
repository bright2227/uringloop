#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

/*
 * Benchmark-only prototypes for the Phase 2 handle design gate.  This module
 * is built only when URINGLOOP_BUILD_HANDLE_PROTOTYPES is set; it is not part
 * of the uringloop package or its runtime ABI.
 */

typedef struct {
    PyObject *callback;
    PyObject *args;
    PyObject *loop;
    PyObject *context;
    int cancelled;
} HandleStorage;

typedef struct {
    PyObject_HEAD
    HandleStorage storage;
} HandleRecord;

typedef struct {
    PyObject_HEAD
    HandleStorage storage;
    PyObject *source_traceback;
    PyObject *cached_repr;
    PyObject *weakrefs;
} NativeHandle;

typedef struct {
    NativeHandle base;
    double when;
    int scheduled;
} NativeTimerHandle;

static PyTypeObject HandleRecordType;
static PyTypeObject NativeHandleType;
static PyTypeObject NativeTimerHandleType;

static int
storage_assign(
    HandleStorage *storage,
    PyObject *callback,
    PyObject *args,
    PyObject *loop,
    PyObject *context)
{
    PyObject *old_callback;
    PyObject *old_args;
    PyObject *old_loop;
    PyObject *old_context;

    if (!PyTuple_Check(args)) {
        PyErr_SetString(PyExc_TypeError, "args must be a tuple");
        return -1;
    }
    Py_INCREF(callback);
    Py_INCREF(args);
    Py_INCREF(loop);
    Py_INCREF(context);
    old_callback = storage->callback;
    old_args = storage->args;
    old_loop = storage->loop;
    old_context = storage->context;
    storage->callback = callback;
    storage->args = args;
    storage->loop = loop;
    storage->context = context;
    storage->cancelled = 0;
    Py_XDECREF(old_callback);
    Py_XDECREF(old_args);
    Py_XDECREF(old_loop);
    Py_XDECREF(old_context);
    return 0;
}

static int
storage_traverse(HandleStorage *storage, visitproc visit, void *arg)
{
    Py_VISIT(storage->callback);
    Py_VISIT(storage->args);
    Py_VISIT(storage->loop);
    Py_VISIT(storage->context);
    return 0;
}

static void
storage_clear(HandleStorage *storage)
{
    Py_CLEAR(storage->callback);
    Py_CLEAR(storage->args);
    Py_CLEAR(storage->loop);
    Py_CLEAR(storage->context);
}

static PyObject *
storage_cancel(HandleStorage *storage)
{
    if (!storage->cancelled) {
        storage->cancelled = 1;
        Py_CLEAR(storage->callback);
        Py_CLEAR(storage->args);
    }
    Py_RETURN_NONE;
}

static PyObject *
storage_run(HandleStorage *storage)
{
    PyObject *callback;
    PyObject *callback_args;
    PyObject *context;
    PyObject *runner;
    PyObject *result;
    PyObject *small_stack[9];
    PyObject **stack = small_stack;
    Py_ssize_t index;
    Py_ssize_t argument_count;

    if (storage->callback == NULL || storage->args == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "cannot run a cancelled prototype handle");
        return NULL;
    }
    callback = Py_NewRef(storage->callback);
    callback_args = Py_NewRef(storage->args);
    context = Py_NewRef(storage->context);
    runner = PyObject_GetAttrString(context, "run");
    if (runner == NULL) {
        Py_DECREF(callback);
        Py_DECREF(callback_args);
        Py_DECREF(context);
        return NULL;
    }
    argument_count = PyTuple_GET_SIZE(callback_args);
    if (argument_count + 1 > (Py_ssize_t)(sizeof(small_stack) / sizeof(small_stack[0]))) {
        stack = PyMem_Malloc((size_t)(argument_count + 1) * sizeof(*stack));
        if (stack == NULL) {
            Py_DECREF(runner);
            Py_DECREF(callback);
            Py_DECREF(callback_args);
            Py_DECREF(context);
            return PyErr_NoMemory();
        }
    }
    stack[0] = callback;
    for (index = 0; index < argument_count; index++) {
        stack[index + 1] = PyTuple_GET_ITEM(callback_args, index);
    }
    result = PyObject_Vectorcall(runner, stack, (size_t)argument_count + 1, NULL);
    if (stack != small_stack) {
        PyMem_Free(stack);
    }
    Py_DECREF(runner);
    Py_DECREF(callback);
    Py_DECREF(callback_args);
    Py_DECREF(context);
    return result;
}

static int
record_traverse(HandleRecord *self, visitproc visit, void *arg)
{
    return storage_traverse(&self->storage, visit, arg);
}

static int
record_clear(HandleRecord *self)
{
    storage_clear(&self->storage);
    return 0;
}

static void
record_dealloc(HandleRecord *self)
{
    PyObject_GC_UnTrack(self);
    record_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
record_init(HandleRecord *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"callback", "args", "loop", "context", NULL};
    PyObject *callback;
    PyObject *callback_args;
    PyObject *loop;
    PyObject *context;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OOOO:HandleRecord",
            keywords,
            &callback,
            &callback_args,
            &loop,
            &context)) {
        return -1;
    }
    return storage_assign(&self->storage, callback, callback_args, loop, context);
}

static PyObject *
record_cancel(HandleRecord *self, PyObject *Py_UNUSED(ignored))
{
    return storage_cancel(&self->storage);
}

static PyObject *
record_run(HandleRecord *self, PyObject *Py_UNUSED(ignored))
{
    return storage_run(&self->storage);
}

static PyObject *
record_cancelled(HandleRecord *self, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(self->storage.cancelled);
}

static PyObject *
record_cancel_timer(HandleRecord *self, PyObject *handle)
{
    PyObject *result;

    if (!self->storage.cancelled) {
        result = PyObject_CallMethod(
            self->storage.loop,
            "_timer_handle_cancelled",
            "O",
            handle);
        if (result == NULL) {
            return NULL;
        }
        Py_DECREF(result);
    }
    return storage_cancel(&self->storage);
}

static PyObject *
record_context(HandleRecord *self, PyObject *Py_UNUSED(ignored))
{
    if (self->storage.context == NULL) {
        Py_RETURN_NONE;
    }
    return Py_NewRef(self->storage.context);
}

static PyMethodDef record_methods[] = {
    {"cancel", (PyCFunction)record_cancel, METH_NOARGS, NULL},
    {"cancel_timer", (PyCFunction)record_cancel_timer, METH_O, NULL},
    {"cancelled", (PyCFunction)record_cancelled, METH_NOARGS, NULL},
    {"get_context", (PyCFunction)record_context, METH_NOARGS, NULL},
    {"run", (PyCFunction)record_run, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject HandleRecordType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "benchmarks._native_handle_prototypes.HandleRecord",
    .tp_basicsize = sizeof(HandleRecord),
    .tp_dealloc = (destructor)record_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_traverse = (traverseproc)record_traverse,
    .tp_clear = (inquiry)record_clear,
    .tp_methods = record_methods,
    .tp_init = (initproc)record_init,
    .tp_new = PyType_GenericNew,
};

static int
native_handle_traverse(NativeHandle *self, visitproc visit, void *arg)
{
    if (storage_traverse(&self->storage, visit, arg) < 0) {
        return -1;
    }
    Py_VISIT(self->source_traceback);
    Py_VISIT(self->cached_repr);
    return 0;
}

static int
native_handle_clear(NativeHandle *self)
{
    storage_clear(&self->storage);
    Py_CLEAR(self->source_traceback);
    Py_CLEAR(self->cached_repr);
    return 0;
}

static void
native_handle_dealloc(NativeHandle *self)
{
    PyObject_GC_UnTrack(self);
    if (self->weakrefs != NULL) {
        PyObject_ClearWeakRefs((PyObject *)self);
    }
    native_handle_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
native_handle_assign(
    NativeHandle *self,
    PyObject *callback,
    PyObject *callback_args,
    PyObject *loop,
    PyObject *context)
{
    if (storage_assign(&self->storage, callback, callback_args, loop, context) < 0) {
        return -1;
    }
    Py_XSETREF(self->source_traceback, Py_NewRef(Py_None));
    Py_XSETREF(self->cached_repr, Py_NewRef(Py_None));
    return 0;
}

static int
native_handle_init(NativeHandle *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"callback", "args", "loop", "context", NULL};
    PyObject *callback;
    PyObject *callback_args;
    PyObject *loop;
    PyObject *context;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "OOOO:NativeHandle",
            keywords,
            &callback,
            &callback_args,
            &loop,
            &context)) {
        return -1;
    }
    return native_handle_assign(self, callback, callback_args, loop, context);
}

static PyObject *
native_handle_cancel(NativeHandle *self, PyObject *Py_UNUSED(ignored))
{
    return storage_cancel(&self->storage);
}

static PyObject *
native_handle_run(NativeHandle *self, PyObject *Py_UNUSED(ignored))
{
    return storage_run(&self->storage);
}

static PyObject *
native_handle_cancelled(NativeHandle *self, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(self->storage.cancelled);
}

static PyObject *
native_handle_context(NativeHandle *self, PyObject *Py_UNUSED(ignored))
{
    if (self->storage.context == NULL) {
        Py_RETURN_NONE;
    }
    return Py_NewRef(self->storage.context);
}

static PyMethodDef native_handle_methods[] = {
    {"cancel", (PyCFunction)native_handle_cancel, METH_NOARGS, NULL},
    {"cancelled", (PyCFunction)native_handle_cancelled, METH_NOARGS, NULL},
    {"get_context", (PyCFunction)native_handle_context, METH_NOARGS, NULL},
    {"_run", (PyCFunction)native_handle_run, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyMemberDef native_handle_members[] = {
    {"_callback", T_OBJECT, offsetof(NativeHandle, storage.callback), READONLY, NULL},
    {"_args", T_OBJECT, offsetof(NativeHandle, storage.args), READONLY, NULL},
    {"_loop", T_OBJECT, offsetof(NativeHandle, storage.loop), READONLY, NULL},
    {"_context", T_OBJECT, offsetof(NativeHandle, storage.context), READONLY, NULL},
    {"_cancelled", T_BOOL, offsetof(NativeHandle, storage.cancelled), READONLY, NULL},
    {"_source_traceback", T_OBJECT, offsetof(NativeHandle, source_traceback), READONLY, NULL},
    {"_repr", T_OBJECT, offsetof(NativeHandle, cached_repr), READONLY, NULL},
    {NULL, 0, 0, 0, NULL},
};

static PyTypeObject NativeHandleType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "benchmarks._native_handle_prototypes.NativeHandle",
    .tp_basicsize = sizeof(NativeHandle),
    .tp_dealloc = (destructor)native_handle_dealloc,
    .tp_weaklistoffset = offsetof(NativeHandle, weakrefs),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_BASETYPE,
    .tp_traverse = (traverseproc)native_handle_traverse,
    .tp_clear = (inquiry)native_handle_clear,
    .tp_methods = native_handle_methods,
    .tp_members = native_handle_members,
    .tp_init = (initproc)native_handle_init,
    .tp_new = PyType_GenericNew,
};

static int
native_timer_init(NativeTimerHandle *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"when", "callback", "args", "loop", "context", NULL};
    PyObject *callback;
    PyObject *callback_args;
    PyObject *loop;
    PyObject *context;
    double when;

    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "dOOOO:NativeTimerHandle",
            keywords,
            &when,
            &callback,
            &callback_args,
            &loop,
            &context)) {
        return -1;
    }
    if (native_handle_assign(&self->base, callback, callback_args, loop, context) < 0) {
        return -1;
    }
    self->when = when;
    self->scheduled = 0;
    return 0;
}

static PyObject *
native_timer_cancel(NativeTimerHandle *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *result;

    if (!self->base.storage.cancelled) {
        result = PyObject_CallMethod(
            self->base.storage.loop,
            "_timer_handle_cancelled",
            "O",
            self);
        if (result == NULL) {
            return NULL;
        }
        Py_DECREF(result);
    }
    return storage_cancel(&self->base.storage);
}

static PyObject *
native_timer_when(NativeTimerHandle *self, PyObject *Py_UNUSED(ignored))
{
    return PyFloat_FromDouble(self->when);
}

static PyMethodDef native_timer_methods[] = {
    {"cancel", (PyCFunction)native_timer_cancel, METH_NOARGS, NULL},
    {"when", (PyCFunction)native_timer_when, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyMemberDef native_timer_members[] = {
    {"_when", T_DOUBLE, offsetof(NativeTimerHandle, when), READONLY, NULL},
    {"_scheduled", T_BOOL, offsetof(NativeTimerHandle, scheduled), 0, NULL},
    {NULL, 0, 0, 0, NULL},
};

static PyTypeObject NativeTimerHandleType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "benchmarks._native_handle_prototypes.NativeTimerHandle",
    .tp_basicsize = sizeof(NativeTimerHandle),
    .tp_dealloc = (destructor)native_handle_dealloc,
    .tp_weaklistoffset = offsetof(NativeHandle, weakrefs),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_traverse = (traverseproc)native_handle_traverse,
    .tp_clear = (inquiry)native_handle_clear,
    .tp_methods = native_timer_methods,
    .tp_members = native_timer_members,
    .tp_init = (initproc)native_timer_init,
    .tp_new = PyType_GenericNew,
};

static struct PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_native_handle_prototypes",
    .m_doc = "Benchmark-only Phase 2 native handle prototypes.",
    .m_size = -1,
};

PyMODINIT_FUNC
PyInit__native_handle_prototypes(void)
{
    PyObject *module;

    NativeTimerHandleType.tp_base = &NativeHandleType;
    if (PyType_Ready(&HandleRecordType) < 0 ||
        PyType_Ready(&NativeHandleType) < 0 ||
        PyType_Ready(&NativeTimerHandleType) < 0) {
        return NULL;
    }
    module = PyModule_Create(&module_definition);
    if (module == NULL) {
        return NULL;
    }
    if (PyModule_AddObjectRef(module, "HandleRecord", (PyObject *)&HandleRecordType) < 0 ||
        PyModule_AddObjectRef(module, "NativeHandle", (PyObject *)&NativeHandleType) < 0 ||
        PyModule_AddObjectRef(module, "NativeTimerHandle", (PyObject *)&NativeTimerHandleType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
