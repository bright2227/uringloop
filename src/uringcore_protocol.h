#ifndef URINGCORE_PROTOCOL_H
#define URINGCORE_PROTOCOL_H

/* Experimental Protocol-only dispatch. This helper owns no socket, request,
 * transport state, callback context or event-loop exception policy. */

enum {
    URINGCORE_PROTOCOL_CONNECTION_MADE,
    URINGCORE_PROTOCOL_DATA_RECEIVED,
    URINGCORE_PROTOCOL_EOF_RECEIVED,
    URINGCORE_PROTOCOL_CONNECTION_LOST,
    URINGCORE_PROTOCOL_PAUSE_WRITING,
    URINGCORE_PROTOCOL_RESUME_WRITING,
    URINGCORE_PROTOCOL_METHOD_COUNT
};

static const char *const uringcore_protocol_method_names[] = {
    "connection_made",
    "data_received",
    "eof_received",
    "connection_lost",
    "pause_writing",
    "resume_writing"
};

typedef struct {
    PyObject_HEAD
    PyObject *protocol;
    PyObject *methods[URINGCORE_PROTOCOL_METHOD_COUNT];
    int replacing;
} UringCoreProtocolDispatcher;

static int
uringcore_protocol_dispatcher_traverse(
    UringCoreProtocolDispatcher *self,
    visitproc visit,
    void *arg)
{
    int index;

    Py_VISIT(self->protocol);
    for (index = 0; index < URINGCORE_PROTOCOL_METHOD_COUNT; index++) {
        Py_VISIT(self->methods[index]);
    }
    return 0;
}

static int
uringcore_protocol_dispatcher_clear(UringCoreProtocolDispatcher *self)
{
    PyObject *old_protocol = self->protocol;
    PyObject *old_methods[URINGCORE_PROTOCOL_METHOD_COUNT];
    int index;

    /* Publish the completely empty state before any DECREF can execute a
     * finalizer. A finalizer cannot repopulate an object being GC-cleared. */
    self->replacing = 1;
    self->protocol = NULL;
    for (index = 0; index < URINGCORE_PROTOCOL_METHOD_COUNT; index++) {
        old_methods[index] = self->methods[index];
        self->methods[index] = NULL;
    }
    for (index = 0; index < URINGCORE_PROTOCOL_METHOD_COUNT; index++) {
        Py_XDECREF(old_methods[index]);
    }
    Py_XDECREF(old_protocol);
    self->replacing = 0;
    return 0;
}

static void
uringcore_protocol_dispatcher_dealloc(UringCoreProtocolDispatcher *self)
{
    PyObject_GC_UnTrack(self);
    uringcore_protocol_dispatcher_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
uringcore_protocol_dispatcher_replace(
    UringCoreProtocolDispatcher *self,
    PyObject *protocol)
{
    PyObject *new_methods[URINGCORE_PROTOCOL_METHOD_COUNT] = {NULL};
    PyObject *old_methods[URINGCORE_PROTOCOL_METHOD_COUNT];
    PyObject *old_protocol;
    PyObject *get_buffer;
    int index;

    if (self->replacing) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "cannot replace a protocol during method lookup or GC clearing");
        return -1;
    }

    self->replacing = 1;
    /* BufferedProtocol needs a separate exporter-owned receive path. Reject
     * its structural get_buffer interface rather than accidentally dispatching
     * its receives as ordinary immutable-byte data_received callbacks. */
    get_buffer = PyObject_GetAttrString(protocol, "get_buffer");
    if (get_buffer != NULL) {
        Py_DECREF(get_buffer);
        PyErr_SetString(
            PyExc_TypeError,
            "BufferedProtocol requires a separate receive dispatcher");
        goto failure;
    }
    if (!PyErr_ExceptionMatches(PyExc_AttributeError)) {
        goto failure;
    }
    PyErr_Clear();

    for (index = 0; index < URINGCORE_PROTOCOL_METHOD_COUNT; index++) {
        new_methods[index] = PyObject_GetAttrString(
            protocol, uringcore_protocol_method_names[index]);
        if (new_methods[index] == NULL) {
            goto failure;
        }
        if (!PyCallable_Check(new_methods[index])) {
            PyErr_Format(
                PyExc_TypeError,
                "protocol.%s must be callable",
                uringcore_protocol_method_names[index]);
            goto failure;
        }
    }

    /* No Python executes while publishing the new complete cache. Strong
     * references keep the previous cache valid through all later finalizers. */
    old_protocol = self->protocol;
    self->protocol = Py_NewRef(protocol);
    for (index = 0; index < URINGCORE_PROTOCOL_METHOD_COUNT; index++) {
        old_methods[index] = self->methods[index];
        self->methods[index] = new_methods[index];
    }
    self->replacing = 0;
    for (index = 0; index < URINGCORE_PROTOCOL_METHOD_COUNT; index++) {
        Py_XDECREF(old_methods[index]);
    }
    Py_XDECREF(old_protocol);
    return 0;

failure:
    /* Cleanup can run arbitrary finalizers. Keep mutation blocked until all
     * temporary lookups are released and preserve the original exception. */
    {
        PyObject *error = PyErr_GetRaisedException();
        for (index = 0; index < URINGCORE_PROTOCOL_METHOD_COUNT; index++) {
            Py_XDECREF(new_methods[index]);
        }
        self->replacing = 0;
        PyErr_SetRaisedException(error);
    }
    return -1;
}

static int
uringcore_protocol_dispatcher_init(
    UringCoreProtocolDispatcher *self,
    PyObject *args,
    PyObject *kwargs)
{
    static char *keywords[] = {"protocol", NULL};
    PyObject *protocol;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "O:ProtocolDispatcher", keywords, &protocol)) {
        return -1;
    }
    /* Explicit repeated __init__ has the same atomic replacement contract as
     * set_protocol. A failure leaves an existing cache unchanged. */
    return uringcore_protocol_dispatcher_replace(self, protocol);
}

static PyObject *
uringcore_protocol_dispatcher_set_protocol(
    UringCoreProtocolDispatcher *self,
    PyObject *protocol)
{
    if (uringcore_protocol_dispatcher_replace(self, protocol) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_protocol_dispatcher_get_protocol(
    UringCoreProtocolDispatcher *self,
    PyObject *Py_UNUSED(ignored))
{
    if (self->protocol == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "protocol dispatcher is not initialized");
        return NULL;
    }
    return Py_NewRef(self->protocol);
}

static PyObject *
uringcore_protocol_dispatcher_call(
    UringCoreProtocolDispatcher *self,
    int index,
    PyObject *const *args,
    size_t count)
{
    PyObject *method = self->methods[index];
    PyObject *result;

    if (method == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "protocol dispatcher is not initialized");
        return NULL;
    }
    /* A callback may replace the entire protocol and DECREF the old cache.
     * Pin this callable independently until vectorcall returns. */
    Py_INCREF(method);
    result = PyObject_Vectorcall(method, args, count, NULL);
    Py_DECREF(method);
    return result;
}

static PyObject *
uringcore_protocol_dispatcher_connection_made(
    UringCoreProtocolDispatcher *self,
    PyObject *transport)
{
    return uringcore_protocol_dispatcher_call(
        self, URINGCORE_PROTOCOL_CONNECTION_MADE, &transport, 1);
}

static PyObject *
uringcore_protocol_dispatcher_data_received(
    UringCoreProtocolDispatcher *self,
    PyObject *data)
{
    return uringcore_protocol_dispatcher_call(
        self, URINGCORE_PROTOCOL_DATA_RECEIVED, &data, 1);
}

static PyObject *
uringcore_protocol_dispatcher_eof_received(
    UringCoreProtocolDispatcher *self,
    PyObject *Py_UNUSED(ignored))
{
    return uringcore_protocol_dispatcher_call(
        self, URINGCORE_PROTOCOL_EOF_RECEIVED, NULL, 0);
}

static PyObject *
uringcore_protocol_dispatcher_connection_lost(
    UringCoreProtocolDispatcher *self,
    PyObject *exception)
{
    return uringcore_protocol_dispatcher_call(
        self, URINGCORE_PROTOCOL_CONNECTION_LOST, &exception, 1);
}

static PyObject *
uringcore_protocol_dispatcher_pause_writing(
    UringCoreProtocolDispatcher *self,
    PyObject *Py_UNUSED(ignored))
{
    return uringcore_protocol_dispatcher_call(
        self, URINGCORE_PROTOCOL_PAUSE_WRITING, NULL, 0);
}

static PyObject *
uringcore_protocol_dispatcher_resume_writing(
    UringCoreProtocolDispatcher *self,
    PyObject *Py_UNUSED(ignored))
{
    return uringcore_protocol_dispatcher_call(
        self, URINGCORE_PROTOCOL_RESUME_WRITING, NULL, 0);
}

static PyMethodDef uringcore_protocol_dispatcher_methods[] = {
    {"get_protocol", (PyCFunction)uringcore_protocol_dispatcher_get_protocol,
     METH_NOARGS, PyDoc_STR("Return the currently cached protocol.")},
    {"set_protocol", (PyCFunction)uringcore_protocol_dispatcher_set_protocol,
     METH_O, PyDoc_STR("Replace the protocol and all cached methods atomically.")},
    {"connection_made", (PyCFunction)uringcore_protocol_dispatcher_connection_made,
     METH_O, PyDoc_STR("Invoke cached connection_made(transport).")},
    {"data_received", (PyCFunction)uringcore_protocol_dispatcher_data_received,
     METH_O, PyDoc_STR("Invoke cached ordinary-Protocol data_received(data).")},
    {"eof_received", (PyCFunction)uringcore_protocol_dispatcher_eof_received,
     METH_NOARGS, PyDoc_STR("Invoke cached eof_received(), preserving its result.")},
    {"connection_lost", (PyCFunction)uringcore_protocol_dispatcher_connection_lost,
     METH_O, PyDoc_STR("Invoke cached connection_lost(exception).")},
    {"pause_writing", (PyCFunction)uringcore_protocol_dispatcher_pause_writing,
     METH_NOARGS, PyDoc_STR("Invoke cached pause_writing().")},
    {"resume_writing", (PyCFunction)uringcore_protocol_dispatcher_resume_writing,
     METH_NOARGS, PyDoc_STR("Invoke cached resume_writing().")},
    {NULL}
};

static PyTypeObject UringCoreProtocolDispatcherType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.ProtocolDispatcher",
    .tp_basicsize = sizeof(UringCoreProtocolDispatcher),
    .tp_dealloc = (destructor)uringcore_protocol_dispatcher_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_doc = PyDoc_STR(
        "Experimental cached vectorcall dispatch for ordinary Protocol only.\n"
        "Repeated initialization atomically refreshes the protocol cache.\n"
        "Callbacks return their result or raise their original exception.\n"
        "No transport lifecycle, context or exception-handler policy is owned."),
    .tp_traverse = (traverseproc)uringcore_protocol_dispatcher_traverse,
    .tp_clear = (inquiry)uringcore_protocol_dispatcher_clear,
    .tp_methods = uringcore_protocol_dispatcher_methods,
    .tp_init = (initproc)uringcore_protocol_dispatcher_init,
    .tp_new = PyType_GenericNew,
};

#endif
