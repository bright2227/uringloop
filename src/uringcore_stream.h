#ifndef URINGCORE_STREAM_H
#define URINGCORE_STREAM_H

/* Experimental one-shot stream transport. The Python proactor retains kernel
 * request/buffer ownership; this type retains logical transport ownership and
 * calls ordinary Protocol methods without Python transport/Future frames. */

typedef struct UringCoreStreamWrite {
    PyObject *payload;
    struct UringCoreStreamWrite *next;
} UringCoreStreamWrite;

typedef struct {
    PyObject_HEAD
    PyObject *loop;
    PyObject *owner;
    PyObject *weakrefs;
    PyObject *proactor;
    PyObject *sock;
    PyObject *dispatcher;
    PyObject *waiter;
    PyObject *extra;
    PyObject *server;
    PyObject *context;
    PyObject *read_request;
    PyObject *write_request;
    PyObject *read_buffer;
    PyObject *pending_read;
    PyObject *active_write;
    PyObject *exception;
    PyObject *read_handler;
    PyObject *write_handler;
    PyObject *read_callback;
    PyObject *write_callback;
    PyObject *made_callback;
    PyObject *lost_callback;
    UringCoreStreamWrite *write_head;
    UringCoreStreamWrite *write_tail;
    Py_ssize_t write_offset;
    Py_ssize_t queued_bytes;
    Py_ssize_t high_water;
    Py_ssize_t low_water;
    int initialized;
    int ready;
    int connected;
    int closing;
    int closed;
    int read_paused;
    int read_eof;
    int pending_eof;
    int write_eof;
    int eof_sent;
    int protocol_paused;
    int read_scheduled;
    int write_scheduled;
    int lost_scheduled;
    int lost_called;
    int attached;
} UringCoreStreamTransport;

static int uringcore_stream_finish(UringCoreStreamTransport *, PyObject *, int);

static PyObject *
uringcore_stream_public(UringCoreStreamTransport *self)
{
    return self->owner == NULL || self->owner == Py_None
        ? (PyObject *)self : self->owner;
}

static int
uringcore_stream_check(UringCoreStreamTransport *self)
{
    if (!self->ready || self->loop == NULL || self->dispatcher == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "stream transport is not initialized");
        return -1;
    }
    return 0;
}

static int
uringcore_stream_schedule(UringCoreStreamTransport *self, PyObject *callback)
{
    PyObject *call_soon, *args, *kwargs, *handle;

    call_soon = PyObject_GetAttrString(self->loop, "call_soon");
    if (call_soon == NULL) {
        return -1;
    }
    args = PyTuple_Pack(1, callback);
    kwargs = Py_BuildValue("{s:O}", "context", self->context);
    if (args == NULL || kwargs == NULL) {
        Py_XDECREF(args);
        Py_XDECREF(kwargs);
        Py_DECREF(call_soon);
        return -1;
    }
    handle = PyObject_Call(call_soon, args, kwargs);
    Py_DECREF(call_soon);
    Py_DECREF(args);
    Py_DECREF(kwargs);
    if (handle == NULL) {
        return -1;
    }
    Py_DECREF(handle);
    return 0;
}

static int
uringcore_stream_schedule_failure(UringCoreStreamTransport *self)
{
    PyObject *error = PyErr_GetRaisedException();

    /* No producer can make progress after ready scheduling fails. Close the
     * fd and settle ownership before preserving the scheduling exception. */
    uringcore_stream_finish(self, error, 1);
    PyErr_Clear();
    PyErr_SetRaisedException(error);
    return -1;
}

static int
uringcore_stream_schedule_read(UringCoreStreamTransport *self)
{
    if (self->closed || self->closing || self->read_scheduled ||
        self->read_paused || !self->connected) {
        return 0;
    }
    self->read_scheduled = 1;
    if (uringcore_stream_schedule(self, self->read_callback) < 0) {
        self->read_scheduled = 0;
        return uringcore_stream_schedule_failure(self);
    }
    return 0;
}

static int
uringcore_stream_schedule_write(UringCoreStreamTransport *self)
{
    if (self->closed || self->write_scheduled || !self->connected) {
        return 0;
    }
    self->write_scheduled = 1;
    if (uringcore_stream_schedule(self, self->write_callback) < 0) {
        self->write_scheduled = 0;
        return uringcore_stream_schedule_failure(self);
    }
    return 0;
}

static int
uringcore_stream_report(UringCoreStreamTransport *self, const char *message)
{
    PyObject *error, *details, *protocol, *result;

    if (PyErr_ExceptionMatches(PyExc_SystemExit) ||
        PyErr_ExceptionMatches(PyExc_KeyboardInterrupt)) {
        return -1;
    }
    error = PyErr_GetRaisedException();
    if (error == NULL) {
        return 0;
    }
    protocol = ((UringCoreProtocolDispatcher *)self->dispatcher)->protocol;
    protocol = Py_NewRef(protocol != NULL ? protocol : Py_None);
    details = Py_BuildValue(
        "{s:s,s:O,s:O,s:O}", "message", message, "exception", error,
        "transport", uringcore_stream_public(self),
        "protocol", protocol);
    Py_DECREF(protocol);
    Py_DECREF(error);
    if (details == NULL) {
        return -1;
    }
    result = PyObject_CallMethod(self->loop, "call_exception_handler", "O", details);
    Py_DECREF(details);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

static PyObject *
uringcore_stream_protocol_call(
    UringCoreStreamTransport *self, int index, PyObject *argument)
{
    UringCoreProtocolDispatcher *dispatcher =
        (UringCoreProtocolDispatcher *)self->dispatcher;

    return uringcore_protocol_dispatcher_call(
        dispatcher, index, argument == NULL ? NULL : &argument,
        argument == NULL ? 0 : 1);
}

static int
uringcore_stream_watermark(UringCoreStreamTransport *self)
{
    PyObject *result;
    int index;

    if (self->closed || !self->connected) {
        return 0;
    }
    if (!self->protocol_paused && self->queued_bytes > self->high_water) {
        self->protocol_paused = 1;
        index = URINGCORE_PROTOCOL_PAUSE_WRITING;
    }
    else if (self->protocol_paused && self->queued_bytes <= self->low_water) {
        self->protocol_paused = 0;
        index = URINGCORE_PROTOCOL_RESUME_WRITING;
    }
    else {
        return 0;
    }
    result = uringcore_stream_protocol_call(self, index, NULL);
    if (result == NULL) {
        return uringcore_stream_report(
            self, index == URINGCORE_PROTOCOL_PAUSE_WRITING
                ? "protocol.pause_writing() failed"
                : "protocol.resume_writing() failed");
    }
    Py_DECREF(result);
    return 0;
}

static void
uringcore_stream_clear_writes(UringCoreStreamTransport *self)
{
    UringCoreStreamWrite *entry = self->write_head;

    self->write_head = NULL;
    self->write_tail = NULL;
    self->queued_bytes = 0;
    self->write_offset = 0;
    Py_CLEAR(self->active_write);
    while (entry != NULL) {
        UringCoreStreamWrite *next = entry->next;
        Py_DECREF(entry->payload);
        PyMem_Free(entry);
        entry = next;
    }
}

static int
uringcore_stream_settle_waiter(UringCoreStreamTransport *self, PyObject *error)
{
    PyObject *waiter = self->waiter;
    PyObject *result;
    int done;

    if (waiter == NULL || waiter == Py_None) {
        return 0;
    }
    self->waiter = NULL;
    result = PyObject_CallMethod(waiter, "done", NULL);
    if (result == NULL) {
        Py_DECREF(waiter);
        return -1;
    }
    done = PyObject_IsTrue(result);
    Py_DECREF(result);
    if (done < 0) {
        Py_DECREF(waiter);
        return -1;
    }
    if (!done) {
        result = PyObject_CallMethod(
            waiter, error == NULL || error == Py_None ? "set_result" : "set_exception",
            "O", error == NULL ? Py_None : error);
        if (result == NULL) {
            Py_DECREF(waiter);
            return -1;
        }
        Py_DECREF(result);
    }
    Py_DECREF(waiter);
    return 0;
}

static int
uringcore_stream_cancel(UringCoreStreamTransport *self, PyObject *request)
{
    PyObject *result;

    if (request == NULL || self->proactor == NULL) {
        return 0;
    }
    result = PyObject_CallMethod(self->proactor, "cancel_transport", "O", request);
    if (result == NULL) {
        return uringcore_stream_report(self, "Cancelling native stream operation failed");
    }
    Py_DECREF(result);
    return 0;
}

static int
uringcore_stream_detach_server(UringCoreStreamTransport *self)
{
    PyObject *result;

    if (!self->attached) {
        return 0;
    }
    self->attached = 0;
    result = PyObject_CallMethod(
        self->loop, "_native_server_detach", "OO", self->server,
        uringcore_stream_public(self));
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

static int
uringcore_stream_finish(UringCoreStreamTransport *self, PyObject *error, int notify)
{
    PyObject *read_request, *write_request, *result;
    PyObject *first_error = NULL;

    if (self->closed) {
        return 0;
    }
    self->closed = 1;
    self->closing = 1;
    self->read_paused = 1;
    self->read_eof = 1;
    if (self->exception == NULL) {
        self->exception = Py_NewRef(error == NULL ? Py_None : error);
    }
    read_request = self->read_request;
    write_request = self->write_request;
    self->read_request = NULL;
    self->write_request = NULL;
    /* Cancel/submit prepared requests before closing their fd. The request
     * core keeps all exports alive even though logical references are cleared. */
    if (uringcore_stream_cancel(self, read_request) < 0) {
        first_error = PyErr_GetRaisedException();
    }
    if (uringcore_stream_cancel(self, write_request) < 0) {
        if (first_error == NULL) {
            first_error = PyErr_GetRaisedException();
        }
        else {
            PyErr_Clear();
        }
    }
    Py_XDECREF(read_request);
    Py_XDECREF(write_request);
    Py_CLEAR(self->read_buffer);
    Py_CLEAR(self->pending_read);
    self->pending_eof = 0;
    uringcore_stream_clear_writes(self);
    if (self->sock != NULL) {
        result = PyObject_CallMethod(self->sock, "close", NULL);
        if (result == NULL) {
            if (first_error == NULL) {
                first_error = PyErr_GetRaisedException();
            }
            else {
                PyErr_Clear();
            }
        }
        else {
            Py_DECREF(result);
        }
    }
    if (notify && !self->lost_scheduled && self->lost_callback != NULL) {
        self->lost_scheduled = 1;
        if (uringcore_stream_schedule(self, self->lost_callback) < 0) {
            self->lost_scheduled = 0;
            /* A closed loop cannot run callbacks, but the fd is already closed. */
            if (first_error == NULL) {
                first_error = PyErr_GetRaisedException();
            }
            else {
                PyErr_Clear();
            }
        }
    }
    /* Server wait_closed normally observes connection_lost first. If no
     * callback can run, detach immediately so server accounting cannot leak. */
    if (!self->lost_scheduled && uringcore_stream_detach_server(self) < 0) {
        if (first_error == NULL) {
            first_error = PyErr_GetRaisedException();
        }
        else {
            PyErr_Clear();
        }
    }
    if (first_error != NULL) {
        PyErr_SetRaisedException(first_error);
        return -1;
    }
    return 0;
}

static int
uringcore_stream_fail_current(UringCoreStreamTransport *self, const char *message)
{
    PyObject *error = PyErr_GetRaisedException();
    int result;

    if (error == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "native stream failed without an exception");
        return -1;
    }
    result = uringcore_stream_finish(self, error, 1);
    if (result < 0) {
        PyErr_Clear();
    }
    PyErr_SetRaisedException(error);
    return uringcore_stream_report(self, message);
}

static int
uringcore_stream_traverse(UringCoreStreamTransport *self, visitproc visit, void *arg)
{
    UringCoreStreamWrite *entry;

    Py_VISIT(self->loop); Py_VISIT(self->owner); Py_VISIT(self->proactor); Py_VISIT(self->sock);
    Py_VISIT(self->dispatcher); Py_VISIT(self->waiter); Py_VISIT(self->extra);
    Py_VISIT(self->server); Py_VISIT(self->context); Py_VISIT(self->read_request);
    Py_VISIT(self->write_request); Py_VISIT(self->read_buffer);
    Py_VISIT(self->pending_read); Py_VISIT(self->active_write); Py_VISIT(self->exception);
    Py_VISIT(self->read_handler); Py_VISIT(self->write_handler);
    Py_VISIT(self->read_callback); Py_VISIT(self->write_callback);
    Py_VISIT(self->made_callback); Py_VISIT(self->lost_callback);
    for (entry = self->write_head; entry != NULL; entry = entry->next) {
        Py_VISIT(entry->payload);
    }
    return 0;
}

static int
uringcore_stream_clear(UringCoreStreamTransport *self)
{
    PyObject *saved_error = PyErr_GetRaisedException();
    self->ready = 0;
    /* An unreachable transport must close its fd even when cached bound methods
     * form a cycle. Reachable requests retain their own exported buffers. */
    if (!self->closed && self->sock != NULL) {
        PyObject *result;
        self->closed = 1;
        self->closing = 1;
        result = PyObject_CallMethod(self->sock, "close", NULL);
        Py_XDECREF(result);
        if (result == NULL) {
            PyErr_Clear();
        }
    }
    self->closed = 1;
    self->closing = 1;
    if (self->attached && self->loop != NULL && self->server != NULL) {
        if (uringcore_stream_detach_server(self) < 0) {
            PyErr_Clear();
        }
    }
    uringcore_stream_clear_writes(self);
    Py_CLEAR(self->loop); Py_CLEAR(self->owner); Py_CLEAR(self->proactor); Py_CLEAR(self->sock);
    Py_CLEAR(self->dispatcher); Py_CLEAR(self->waiter); Py_CLEAR(self->extra);
    Py_CLEAR(self->server); Py_CLEAR(self->context); Py_CLEAR(self->read_request);
    Py_CLEAR(self->write_request); Py_CLEAR(self->read_buffer);
    Py_CLEAR(self->pending_read); Py_CLEAR(self->exception);
    Py_CLEAR(self->read_handler); Py_CLEAR(self->write_handler);
    Py_CLEAR(self->read_callback); Py_CLEAR(self->write_callback);
    Py_CLEAR(self->made_callback); Py_CLEAR(self->lost_callback);
    PyErr_SetRaisedException(saved_error);
    return 0;
}

static void
uringcore_stream_dealloc(UringCoreStreamTransport *self)
{
    PyObject_GC_UnTrack(self);
    if (self->weakrefs != NULL) {
        PyObject_ClearWeakRefs((PyObject *)self);
    }
    uringcore_stream_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static int
uringcore_stream_init(UringCoreStreamTransport *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"loop", "sock", "protocol", "waiter", "extra", "server", "owner", NULL};
    PyObject *loop, *sock, *protocol;
    PyObject *waiter = Py_None, *extra = Py_None, *server = Py_None;
    PyObject *owner = Py_None;
    PyObject *contextvars, *result;

    if (self->initialized) {
        PyErr_SetString(PyExc_RuntimeError, "stream transport is already initialized");
        return -1;
    }
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OOO|OOOO:StreamTransport", keywords,
            &loop, &sock, &protocol, &waiter, &extra, &server, &owner)) {
        return -1;
    }
    /* Block repeated/reentrant __init__ now, but expose no usable transport
     * until every owned field and the initial callback are established. */
    self->initialized = 1;
    self->high_water = 64 * 1024;
    self->low_water = 16 * 1024;
    self->dispatcher = PyObject_CallOneArg((PyObject *)&UringCoreProtocolDispatcherType, protocol);
    if (self->dispatcher == NULL) {
        return -1;
    }
    self->loop = Py_NewRef(loop);
    self->owner = Py_NewRef(owner);
    self->sock = Py_NewRef(sock);
    self->waiter = Py_NewRef(waiter);
    self->server = Py_NewRef(server);
    if (extra != Py_None && !PyDict_Check(extra)) {
        PyErr_SetString(PyExc_TypeError, "extra must be a dict or None");
        goto failure;
    }
    self->extra = extra == Py_None ? PyDict_New() : PyDict_Copy(extra);
    if (self->extra == NULL) {
        goto failure;
    }
    self->proactor = PyObject_GetAttrString(loop, "_proactor");
    if (self->proactor == NULL) {
        goto failure;
    }
    contextvars = PyImport_ImportModule("contextvars");
    if (contextvars == NULL) {
        goto failure;
    }
    self->context = PyObject_CallMethod(contextvars, "copy_context", NULL);
    Py_DECREF(contextvars);
    if (self->context == NULL) {
        goto failure;
    }
    self->read_handler = PyObject_GetAttrString((PyObject *)self, "_read_done");
    if (self->read_handler == NULL) {
        goto failure;
    }
    self->write_handler = PyObject_GetAttrString((PyObject *)self, "_write_done");
    if (self->write_handler == NULL) {
        goto failure;
    }
    self->read_callback = PyObject_GetAttrString((PyObject *)self, "_read_pump");
    if (self->read_callback == NULL) {
        goto failure;
    }
    self->write_callback = PyObject_GetAttrString((PyObject *)self, "_write_pump");
    if (self->write_callback == NULL) {
        goto failure;
    }
    self->made_callback = PyObject_GetAttrString((PyObject *)self, "_connected");
    if (self->made_callback == NULL) {
        goto failure;
    }
    self->lost_callback = PyObject_GetAttrString((PyObject *)self, "_lost");
    if (self->lost_callback == NULL) {
        goto failure;
    }
    if (server != Py_None) {
        result = PyObject_CallMethod(
            loop, "_native_server_attach", "OO", server, uringcore_stream_public(self));
        if (result == NULL) {
            goto failure;
        }
        Py_DECREF(result);
        self->attached = 1;
    }
    result = PyObject_CallMethod(
        self->proactor, "register_transport", "O", uringcore_stream_public(self));
    if (result == NULL) {
        goto failure;
    }
    Py_DECREF(result);
    if (uringcore_stream_schedule(self, self->made_callback) < 0) {
        goto failure;
    }
    self->ready = 1;
    return 0;

failure:
    {
        PyObject *error = PyErr_GetRaisedException();
        self->ready = 0;
        uringcore_stream_finish(self, error, 0);
        PyErr_Clear();
        PyErr_SetRaisedException(error);
    }
    return -1;
}

static PyObject *
uringcore_stream_connected(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *result;

    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (!self->connected) {
        self->connected = 1;
        result = uringcore_stream_protocol_call(
            self, URINGCORE_PROTOCOL_CONNECTION_MADE, uringcore_stream_public(self));
        if (result == NULL) {
            PyObject *error = PyErr_GetRaisedException();
            if (uringcore_stream_settle_waiter(self, error) < 0) {
                PyErr_Clear();
            }
            PyErr_SetRaisedException(error);
            if (uringcore_stream_fail_current(self, "protocol.connection_made() failed") < 0) {
                return NULL;
            }
            Py_RETURN_NONE;
        }
        Py_DECREF(result);
    }
    if (uringcore_stream_settle_waiter(self, NULL) < 0) {
        if (uringcore_stream_fail_current(self, "Settling native stream connection waiter failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    /* Publish producers before synchronous flow callbacks can stop the loop
     * or propagate an exception. Restarting the loop can then make progress. */
    if (uringcore_stream_schedule_read(self) < 0 ||
        uringcore_stream_schedule_write(self) < 0 ||
        uringcore_stream_watermark(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_lost(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *result, *error = NULL;

    if (!self->ready || self->lost_called || self->dispatcher == NULL) {
        Py_RETURN_NONE;
    }
    self->lost_called = 1;
    result = uringcore_stream_protocol_call(self, URINGCORE_PROTOCOL_CONNECTION_LOST,
        self->exception == NULL ? Py_None : self->exception);
    if (result == NULL) {
        error = PyErr_GetRaisedException();
    }
    else {
        Py_DECREF(result);
    }
    if (uringcore_stream_detach_server(self) < 0) {
        if (error == NULL) {
            return NULL;
        }
        PyErr_Clear();
    }
    if (error != NULL) {
        PyErr_SetRaisedException(error);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_read_pump(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *result, *data;
    int keep_open;

    self->read_scheduled = 0;
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (self->closed || self->closing || self->read_paused) {
        Py_RETURN_NONE;
    }
    if (self->pending_read != NULL) {
        data = self->pending_read;
        self->pending_read = NULL;
        result = uringcore_stream_protocol_call(self, URINGCORE_PROTOCOL_DATA_RECEIVED, data);
        Py_DECREF(data);
        if (result == NULL) {
            if (uringcore_stream_fail_current(self, "protocol.data_received() failed") < 0) {
                return NULL;
            }
            Py_RETURN_NONE;
        }
        Py_DECREF(result);
    }
    if (self->closed || self->closing || self->read_paused) {
        Py_RETURN_NONE;
    }
    if (self->pending_eof) {
        self->pending_eof = 0;
        result = uringcore_stream_protocol_call(self, URINGCORE_PROTOCOL_EOF_RECEIVED, NULL);
        if (result == NULL) {
            if (uringcore_stream_fail_current(self, "protocol.eof_received() failed") < 0) {
                return NULL;
            }
            Py_RETURN_NONE;
        }
        keep_open = PyObject_IsTrue(result);
        Py_DECREF(result);
        if (keep_open < 0) {
            if (uringcore_stream_fail_current(self, "protocol.eof_received() result failed") < 0) {
                return NULL;
            }
            Py_RETURN_NONE;
        }
        if (!keep_open && !self->closed) {
            self->closing = 1;
            if (self->queued_bytes == 0) {
                if (uringcore_stream_finish(self, NULL, 1) < 0) {
                    return NULL;
                }
            }
            else if (uringcore_stream_schedule_write(self) < 0) {
                return NULL;
            }
        }
        Py_RETURN_NONE;
    }
    if (self->read_eof || self->read_request != NULL) {
        Py_RETURN_NONE;
    }
    self->read_buffer = PyByteArray_FromStringAndSize(NULL, 64 * 1024);
    if (self->read_buffer == NULL) {
        if (uringcore_stream_fail_current(self, "Allocating native stream read buffer failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    result = PyObject_CallMethod(self->proactor, "recv_transport", "OOO",
        self->sock, self->read_buffer, self->read_handler);
    if (result == NULL) {
        Py_CLEAR(self->read_buffer);
        if (uringcore_stream_fail_current(self, "Preparing native stream receive failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    self->read_request = result;
    Py_RETURN_NONE;
}

static int
uringcore_stream_completion_count(
    PyObject *completion, PyObject *active, Py_ssize_t *count)
{
    PyObject *request = PyObject_GetAttrString(completion, "request");
    PyObject *result;

    if (request == NULL) {
        return -1;
    }
    if (request != active) {
        Py_DECREF(request);
        return 0;
    }
    Py_DECREF(request);
    result = PyObject_GetAttrString(completion, "result");
    if (result == NULL) {
        return -1;
    }
    if (!PyLong_CheckExact(result)) {
        Py_DECREF(result);
        {
            PyObject *error = PyObject_CallFunction(
                PyExc_OSError, "is", EIO, "invalid native completion result");
            if (error != NULL) {
                PyErr_SetObject(PyExc_OSError, error);
                Py_DECREF(error);
            }
        }
        return -1;
    }
    *count = PyLong_AsSsize_t(result);
    Py_DECREF(result);
    if (*count == -1 && PyErr_Occurred()) {
        return -1;
    }
    if (*count < -INT_MAX || *count > INT_MAX) {
        PyErr_SetString(PyExc_OverflowError, "completion result exceeds native result range");
        return -1;
    }
    return 1;
}

static PyObject *
uringcore_stream_read_done(UringCoreStreamTransport *self, PyObject *completion)
{
    PyObject *expected;
    Py_ssize_t count = 0;
    int matched;

    if (self->closed || self->read_request == NULL) {
        Py_RETURN_NONE;
    }
    expected = Py_NewRef(self->read_request);
    matched = uringcore_stream_completion_count(completion, expected, &count);
    /* Completion attributes are native immutable getters in production, but
     * defensive/private callers can reenter and retire this request while an
     * attribute is read. Never apply its result to a newly armed request. */
    if (self->closed || self->read_request != expected) {
        Py_DECREF(expected);
        PyErr_Clear();
        Py_RETURN_NONE;
    }
    Py_DECREF(expected);
    if (matched < 0) {
        if (uringcore_stream_fail_current(self, "Invalid native stream receive completion") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (!matched) {
        Py_RETURN_NONE;
    }
    Py_CLEAR(self->read_request);
    if (self->closing) {
        Py_CLEAR(self->read_buffer);
        Py_RETURN_NONE;
    }
    if (count < 0) {
        int error_number = (int)-count;
        Py_CLEAR(self->read_buffer);
        if (error_number == EAGAIN || error_number == EINTR) {
            if (uringcore_stream_schedule_read(self) < 0) {
                return NULL;
            }
            Py_RETURN_NONE;
        }
        {
            PyObject *error = PyObject_CallFunction(PyExc_OSError, "is", error_number, strerror(error_number));
            if (error != NULL) {
                PyErr_SetObject(PyExc_OSError, error);
                Py_DECREF(error);
            }
        }
        if (uringcore_stream_fail_current(self, "Native stream receive failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (self->read_buffer == NULL || count > PyByteArray_GET_SIZE(self->read_buffer)) {
        PyErr_SetString(PyExc_RuntimeError, "native receive reported an invalid byte count");
        if (uringcore_stream_fail_current(self, "Native stream receive failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (count == 0) {
        self->read_eof = 1;
        self->pending_eof = 1;
    }
    else {
        self->pending_read = PyBytes_FromStringAndSize(PyByteArray_AS_STRING(self->read_buffer), count);
        if (self->pending_read == NULL) {
            if (uringcore_stream_fail_current(self, "Copying native stream receive failed") < 0) {
                return NULL;
            }
            Py_RETURN_NONE;
        }
    }
    Py_CLEAR(self->read_buffer);
    if (uringcore_stream_schedule_read(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_write_pump(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *result, *payload;

    self->write_scheduled = 0;
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (self->closed) {
        Py_RETURN_NONE;
    }
    if (uringcore_stream_watermark(self) < 0) {
        PyObject *error = PyErr_GetRaisedException();
        /* A resume callback may have queued more bytes before propagating.
         * Keep a producer for them even when the loop is stopped by the error. */
        if (uringcore_stream_schedule_write(self) < 0) {
            PyErr_Clear();
        }
        PyErr_SetRaisedException(error);
        return NULL;
    }
    if (self->closed || self->write_request != NULL) {
        Py_RETURN_NONE;
    }
    if (self->active_write == NULL && self->write_head != NULL) {
        UringCoreStreamWrite *entry = self->write_head;
        self->write_head = entry->next;
        if (self->write_head == NULL) {
            self->write_tail = NULL;
        }
        self->active_write = entry->payload;
        self->write_offset = 0;
        PyMem_Free(entry);
    }
    if (self->active_write == NULL) {
        if (self->closing) {
            if (uringcore_stream_finish(self, NULL, 1) < 0) {
                return NULL;
            }
        }
        else if (self->write_eof && !self->eof_sent) {
            self->eof_sent = 1;
            result = PyObject_CallMethod(self->sock, "shutdown", "i", SHUT_WR);
            if (result == NULL) {
                if (uringcore_stream_fail_current(self, "Native stream shutdown failed") < 0) {
                    return NULL;
                }
            }
            else {
                Py_DECREF(result);
            }
        }
        Py_RETURN_NONE;
    }
    payload = self->write_offset == 0 ? Py_NewRef(self->active_write)
        : PyBytes_FromStringAndSize(
            PyBytes_AS_STRING(self->active_write) + self->write_offset,
            PyBytes_GET_SIZE(self->active_write) - self->write_offset);
    if (payload == NULL) {
        if (uringcore_stream_fail_current(self, "Copying native stream write suffix failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    result = PyObject_CallMethod(self->proactor, "send_transport", "OOO",
        self->sock, payload, self->write_handler);
    Py_DECREF(payload);
    if (result == NULL) {
        if (uringcore_stream_fail_current(self, "Preparing native stream send failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    self->write_request = result;
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_write_done(UringCoreStreamTransport *self, PyObject *completion)
{
    PyObject *expected;
    Py_ssize_t count = 0, remaining;
    int matched;

    if (self->closed || self->write_request == NULL) {
        Py_RETURN_NONE;
    }
    expected = Py_NewRef(self->write_request);
    matched = uringcore_stream_completion_count(completion, expected, &count);
    if (self->closed || self->write_request != expected) {
        Py_DECREF(expected);
        PyErr_Clear();
        Py_RETURN_NONE;
    }
    Py_DECREF(expected);
    if (matched < 0) {
        if (uringcore_stream_fail_current(self, "Invalid native stream send completion") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (!matched) {
        Py_RETURN_NONE;
    }
    Py_CLEAR(self->write_request);
    remaining = self->active_write == NULL ? 0 : PyBytes_GET_SIZE(self->active_write) - self->write_offset;
    if (count <= 0 || count > remaining) {
        if (count < 0 && (-count == EAGAIN || -count == EINTR)) {
            if (uringcore_stream_schedule_write(self) < 0) {
                return NULL;
            }
            Py_RETURN_NONE;
        }
        if (count < 0) {
            int error_number = (int)-count;
            PyObject *error = PyObject_CallFunction(PyExc_OSError, "is", error_number, strerror(error_number));
            if (error != NULL) {
                PyErr_SetObject(PyExc_OSError, error);
                Py_DECREF(error);
            }
        }
        else {
            if (count == 0) {
                PyErr_SetString(PyExc_ConnectionResetError, "stream write made no progress");
            }
            else {
                PyObject *error = PyObject_CallFunction(
                    PyExc_OSError, "is", EIO, "invalid stream write completion");
                if (error != NULL) {
                    PyErr_SetObject(PyExc_OSError, error);
                    Py_DECREF(error);
                }
            }
        }
        if (uringcore_stream_fail_current(self, "Native stream send failed") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    self->write_offset += count;
    self->queued_bytes -= count;
    if (self->write_offset == PyBytes_GET_SIZE(self->active_write)) {
        Py_CLEAR(self->active_write);
        self->write_offset = 0;
    }
    if (uringcore_stream_schedule_write(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_write(UringCoreStreamTransport *self, PyObject *data)
{
    Py_buffer view;
    PyObject *payload;
    UringCoreStreamWrite *entry;

    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (self->write_eof) {
        PyErr_SetString(PyExc_RuntimeError, "Cannot call write() after write_eof()");
        return NULL;
    }
    if (PyObject_GetBuffer(data, &view, PyBUF_CONTIG_RO) < 0) {
        return NULL;
    }
    payload = PyBytes_FromStringAndSize(view.buf, view.len);
    PyBuffer_Release(&view);
    if (payload == NULL) {
        return NULL;
    }
    if (self->write_eof) {
        Py_DECREF(payload);
        PyErr_SetString(PyExc_RuntimeError, "Cannot call write() after write_eof()");
        return NULL;
    }
    if (self->closing || PyBytes_GET_SIZE(payload) == 0) {
        Py_DECREF(payload);
        Py_RETURN_NONE;
    }
    if (PyBytes_GET_SIZE(payload) > PY_SSIZE_T_MAX - self->queued_bytes) {
        Py_DECREF(payload);
        PyErr_SetString(PyExc_OverflowError, "native stream write buffer is too large");
        return NULL;
    }
    entry = PyMem_Malloc(sizeof(*entry));
    if (entry == NULL) {
        Py_DECREF(payload);
        return PyErr_NoMemory();
    }
    entry->payload = payload;
    entry->next = NULL;
    if (self->write_tail == NULL) {
        self->write_head = entry;
    }
    else {
        self->write_tail->next = entry;
    }
    self->write_tail = entry;
    self->queued_bytes += PyBytes_GET_SIZE(payload);
    if (uringcore_stream_schedule_write(self) < 0 || uringcore_stream_watermark(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_writelines(UringCoreStreamTransport *self, PyObject *iterable)
{
    PyObject *iterator = PyObject_GetIter(iterable);
    PyObject *item, *result;

    if (iterator == NULL) {
        return NULL;
    }
    while ((item = PyIter_Next(iterator)) != NULL) {
        result = uringcore_stream_write(self, item);
        Py_DECREF(item);
        if (result == NULL) {
            Py_DECREF(iterator);
            return NULL;
        }
        Py_DECREF(result);
    }
    Py_DECREF(iterator);
    if (PyErr_Occurred()) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_close(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (!self->closing) {
        PyObject *read_request;
        self->closing = 1;
        self->read_paused = 1;
        if (self->queued_bytes == 0) {
            if (uringcore_stream_finish(self, NULL, 1) < 0) {
                return NULL;
            }
        }
        else {
            PyObject *error = NULL;
            read_request = self->read_request;
            self->read_request = NULL;
            Py_CLEAR(self->read_buffer);
            Py_CLEAR(self->pending_read);
            self->pending_eof = 0;
            if (uringcore_stream_cancel(self, read_request) < 0) {
                error = PyErr_GetRaisedException();
            }
            Py_XDECREF(read_request);
            if (uringcore_stream_schedule_write(self) < 0) {
                if (error != NULL) {
                    PyErr_Clear();
                    PyErr_SetRaisedException(error);
                }
                return NULL;
            }
            if (error != NULL) {
                PyErr_SetRaisedException(error);
                return NULL;
            }
        }
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_abort(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    if (uringcore_stream_check(self) < 0 || uringcore_stream_finish(self, NULL, 1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_close_for_loop(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *error = NULL;

    /* Registration may have exposed an object before later construction
     * failed. Its failure cleanup already closed/detached it; shutdown must
     * still tolerate that retained registration. */
    if (self->initialized && !self->ready && self->closed) {
        Py_RETURN_NONE;
    }
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (uringcore_stream_finish(self, NULL, 0) < 0) {
        error = PyErr_GetRaisedException();
    }
    /* An earlier abort may already have queued lost. loop.close drops that
     * queue, so detach even when finish was an idempotent no-op. */
    if (uringcore_stream_detach_server(self) < 0) {
        if (error == NULL) {
            return NULL;
        }
        PyErr_Clear();
    }
    if (error != NULL) {
        PyErr_SetRaisedException(error);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_write_eof(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (!self->closing && !self->write_eof) {
        self->write_eof = 1;
        if (uringcore_stream_schedule_write(self) < 0) {
            return NULL;
        }
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_can_write_eof(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    Py_RETURN_TRUE;
}

static PyObject *
uringcore_stream_is_closing(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(self->closing);
}

static PyObject *
uringcore_stream_is_reading(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(self->ready && !self->closing && !self->read_paused && !self->read_eof);
}

static PyObject *
uringcore_stream_pause_reading(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    self->read_paused = 1;
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_resume_reading(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    if (!self->closing) {
        self->read_paused = 0;
        if (uringcore_stream_schedule_read(self) < 0) {
            return NULL;
        }
    }
    Py_RETURN_NONE;
}

static PyObject *
uringcore_stream_get_protocol(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    return uringcore_protocol_dispatcher_get_protocol(
        (UringCoreProtocolDispatcher *)self->dispatcher, NULL);
}

static PyObject *
uringcore_stream_set_protocol(UringCoreStreamTransport *self, PyObject *protocol)
{
    if (uringcore_stream_check(self) < 0) {
        return NULL;
    }
    return uringcore_protocol_dispatcher_set_protocol(
        (UringCoreProtocolDispatcher *)self->dispatcher, protocol);
}

static PyObject *
uringcore_stream_get_extra_info(UringCoreStreamTransport *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"name", "default", NULL};
    PyObject *name, *fallback = Py_None, *value;

    if (uringcore_stream_check(self) < 0 ||
        !PyArg_ParseTupleAndKeywords(args, kwargs, "O|O:get_extra_info", keywords, &name, &fallback)) {
        return NULL;
    }
    value = PyDict_GetItemWithError(self->extra, name);
    if (value == NULL && PyErr_Occurred()) {
        return NULL;
    }
    return Py_NewRef(value == NULL ? fallback : value);
}

static PyObject *
uringcore_stream_get_write_buffer_size(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    return PyLong_FromSsize_t(self->queued_bytes);
}

static PyObject *
uringcore_stream_get_write_buffer_limits(UringCoreStreamTransport *self, PyObject *Py_UNUSED(ignored))
{
    return Py_BuildValue("nn", self->low_water, self->high_water);
}

static PyObject *
uringcore_stream_set_write_buffer_limits(UringCoreStreamTransport *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"high", "low", NULL};
    PyObject *high = Py_None, *low = Py_None;
    Py_ssize_t high_value, low_value;

    if (uringcore_stream_check(self) < 0 ||
        !PyArg_ParseTupleAndKeywords(args, kwargs, "|OO:set_write_buffer_limits", keywords, &high, &low)) {
        return NULL;
    }
    if (low == Py_None) {
        high_value = high == Py_None ? 64 * 1024 : PyLong_AsSsize_t(high);
        if (high_value == -1 && PyErr_Occurred()) {
            return NULL;
        }
        low_value = high_value / 4;
    }
    else {
        low_value = PyLong_AsSsize_t(low);
        if (low_value == -1 && PyErr_Occurred()) {
            return NULL;
        }
        if (high == Py_None) {
            if (low_value < 0) {
                PyErr_SetString(PyExc_ValueError, "high must be >= low must be >= 0");
                return NULL;
            }
            if (low_value > PY_SSIZE_T_MAX / 4) {
                PyErr_SetString(PyExc_OverflowError, "write buffer limit is too large");
                return NULL;
            }
            high_value = 4 * low_value;
        }
        else {
            high_value = PyLong_AsSsize_t(high);
            if (high_value == -1 && PyErr_Occurred()) {
                return NULL;
            }
        }
    }
    if (low_value < 0 || high_value < low_value) {
        PyErr_SetString(PyExc_ValueError, "high must be >= low must be >= 0");
        return NULL;
    }
    self->high_water = high_value;
    self->low_water = low_value;
    if (uringcore_stream_watermark(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyMemberDef uringcore_stream_members[] = {
    {"_loop", T_OBJECT, offsetof(UringCoreStreamTransport, loop), READONLY, NULL},
    {"_sock", T_OBJECT, offsetof(UringCoreStreamTransport, sock), READONLY, NULL},
    {"_protocol_paused", T_INT, offsetof(UringCoreStreamTransport, protocol_paused), READONLY, NULL},
    {NULL}
};

static PyMethodDef uringcore_stream_methods[] = {
    {"write", (PyCFunction)uringcore_stream_write, METH_O, NULL},
    {"writelines", (PyCFunction)uringcore_stream_writelines, METH_O, NULL},
    {"close", (PyCFunction)uringcore_stream_close, METH_NOARGS, NULL},
    {"abort", (PyCFunction)uringcore_stream_abort, METH_NOARGS, NULL},
    {"_close_for_loop", (PyCFunction)uringcore_stream_close_for_loop, METH_NOARGS, NULL},
    {"write_eof", (PyCFunction)uringcore_stream_write_eof, METH_NOARGS, NULL},
    {"can_write_eof", (PyCFunction)uringcore_stream_can_write_eof, METH_NOARGS, NULL},
    {"is_closing", (PyCFunction)uringcore_stream_is_closing, METH_NOARGS, NULL},
    {"is_reading", (PyCFunction)uringcore_stream_is_reading, METH_NOARGS, NULL},
    {"pause_reading", (PyCFunction)uringcore_stream_pause_reading, METH_NOARGS, NULL},
    {"resume_reading", (PyCFunction)uringcore_stream_resume_reading, METH_NOARGS, NULL},
    {"get_protocol", (PyCFunction)uringcore_stream_get_protocol, METH_NOARGS, NULL},
    {"set_protocol", (PyCFunction)uringcore_stream_set_protocol, METH_O, NULL},
    {"get_extra_info", (PyCFunction)(void (*)(void))uringcore_stream_get_extra_info, METH_VARARGS | METH_KEYWORDS, NULL},
    {"get_write_buffer_size", (PyCFunction)uringcore_stream_get_write_buffer_size, METH_NOARGS, NULL},
    {"get_write_buffer_limits", (PyCFunction)uringcore_stream_get_write_buffer_limits, METH_NOARGS, NULL},
    {"set_write_buffer_limits", (PyCFunction)(void (*)(void))uringcore_stream_set_write_buffer_limits, METH_VARARGS | METH_KEYWORDS, NULL},
    {"_connected", (PyCFunction)uringcore_stream_connected, METH_NOARGS, NULL},
    {"_lost", (PyCFunction)uringcore_stream_lost, METH_NOARGS, NULL},
    {"_read_pump", (PyCFunction)uringcore_stream_read_pump, METH_NOARGS, NULL},
    {"_write_pump", (PyCFunction)uringcore_stream_write_pump, METH_NOARGS, NULL},
    {"_read_done", (PyCFunction)uringcore_stream_read_done, METH_O, NULL},
    {"_write_done", (PyCFunction)uringcore_stream_write_done, METH_O, NULL},
    {NULL}
};

static PyTypeObject UringCoreStreamTransportType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "uringloop._uringcore.StreamTransport",
    .tp_basicsize = sizeof(UringCoreStreamTransport),
    .tp_dealloc = (destructor)uringcore_stream_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    .tp_doc = PyDoc_STR(
        "Experimental native one-shot TCP/Unix stream transport.\n"
        "Ordinary Protocol dispatch; no multishot receive or SEND_ZC.\n"
        "Scheduled callbacks use the Context captured at construction."),
    .tp_traverse = (traverseproc)uringcore_stream_traverse,
    .tp_clear = (inquiry)uringcore_stream_clear,
    .tp_methods = uringcore_stream_methods,
    .tp_members = uringcore_stream_members,
    .tp_weaklistoffset = offsetof(UringCoreStreamTransport, weakrefs),
    .tp_init = (initproc)uringcore_stream_init,
    .tp_new = PyType_GenericNew,
};

#endif
