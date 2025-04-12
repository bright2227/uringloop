import cffi


ffibuilder = cffi.FFI()

source_code = """
    #include <fcntl.h>
    #include <linux/poll.h>
    #include <linux/types.h>
    #include <sys/socket.h>
    #include <sys/types.h>
    #include <sys/un.h>
    #include <sys/uio.h>
    #include <netinet/in.h>
    #include <unistd.h>
    #include <Python.h>
    #include "liburing.h"

    int io_uring_wait_cqe_nogil(struct io_uring *ring, struct io_uring_cqe **cqe_ptr) {
        int res;
        Py_BEGIN_ALLOW_THREADS
        res = io_uring_wait_cqe(ring, cqe_ptr);
        Py_END_ALLOW_THREADS
        return res;
    }

    int io_uring_wait_cqe_timeout_nogil(struct io_uring *ring, struct io_uring_cqe **cqe_ptr,
                                       struct __kernel_timespec *ts) {
        int res;
        Py_BEGIN_ALLOW_THREADS
        res = io_uring_wait_cqe_timeout(ring, cqe_ptr, ts);
        Py_END_ALLOW_THREADS
        return res;
    }
    """


ffibuilder.set_source(
    "uringloop._liburing",
    source_code,
    sources=[],
    include_dirs=["./libs/src/include"],
    define_macros=[("_GNU_SOURCE", "1")],
    extra_compile_args=["-D_GNU_SOURCE"],
    libraries=["uring"],  # Link against liburing
    library_dirs=["./libs/src"],  # Path to the library
)


ffibuilder.cdef("""
    typedef uint8_t __u8;
    typedef uint16_t __u16;
    typedef uint32_t __u32;
    // typedef uint64_t __u64;
    typedef unsigned long long __u64;
    typedef int32_t __s32;
    typedef int __kernel_rwf_t;
""")

# liburing.h
ffibuilder.cdef("""
    struct io_uring_sq {
        unsigned *khead;
        unsigned *ktail;
        // Deprecated: use `ring_mask` instead of `*kring_mask`
        unsigned *kring_mask;
        // Deprecated: use `ring_entries` instead of `*kring_entries`
        unsigned *kring_entries;
        unsigned *kflags;
        unsigned *kdropped;
        unsigned *array;
        struct io_uring_sqe *sqes;

        unsigned sqe_head;
        unsigned sqe_tail;

        size_t ring_sz;
        void *ring_ptr;

        unsigned ring_mask;
        unsigned ring_entries;

        unsigned pad[2];
    };

    struct io_uring_cq {
        unsigned *khead;
        unsigned *ktail;
        // Deprecated: use `ring_mask` instead of `*kring_mask`
        unsigned *kring_mask;
        // Deprecated: use `ring_entries` instead of `*kring_entries`
        unsigned *kring_entries;
        unsigned *kflags;
        unsigned *koverflow;
        struct io_uring_cqe *cqes;

        size_t ring_sz;
        void *ring_ptr;

        unsigned ring_mask;
        unsigned ring_entries;

        unsigned pad[2];
    };

    struct io_uring {
        struct io_uring_sq sq;
        struct io_uring_cq cq;
        unsigned flags;
        int ring_fd;

        unsigned features;
        int enter_ring_fd;
        __u8 int_flags;
        __u8 pad[3];
        unsigned pad2;
    };

    struct io_uring_sqe {
        __u8	opcode;		/* type of operation for this sqe */
        __u8	flags;		/* IOSQE_ flags */
        __u16	ioprio;		/* ioprio for the request */
        __s32	fd;		/* file descriptor to do IO on */
        union {
            __u64	off;	/* offset into file */
            __u64	addr2;
            struct {
                __u32	cmd_op;
                __u32	__pad1;
            };
        };
        union {
            __u64	addr;	/* pointer to buffer or iovecs */
            __u64	splice_off_in;
        };
        __u32	len;		/* buffer size or number of iovecs */
        union {
            __kernel_rwf_t	rw_flags;
            __u32		fsync_flags;
            __u16		poll_events;	/* compatibility */
            __u32		poll32_events;	/* word-reversed for BE */
            __u32		sync_range_flags;
            __u32		msg_flags;
            __u32		timeout_flags;
            __u32		accept_flags;
            __u32		cancel_flags;
            __u32		open_flags;
            __u32		statx_flags;
            __u32		fadvise_advice;
            __u32		splice_flags;
            __u32		rename_flags;
            __u32		unlink_flags;
            __u32		hardlink_flags;
            __u32		xattr_flags;
            __u32		msg_ring_flags;
            __u32		uring_cmd_flags;
        };
        __u64	user_data;	/* data to be passed back at completion time */
    	/* pack this to avoid bogus arm OABI complaints */
        union {
            /* index into fixed buffers, if used */
            __u16	buf_index;
            /* for grouped buffer selection */
            __u16	buf_group;
        };  /* NOTE(bright): __attribute__((packed)) is removed for now*/
        /* personality to use, if used */
        __u16	personality;
        union {
            __s32	splice_fd_in;
            __u32	file_index;
            struct {
                __u16	addr_len;
                __u16	__pad3[1];
            };
        };
        union {
            struct {
                __u64	addr3;
                __u64	__pad2[1];
            };
            /*
            * If the ring is initialized with IORING_SETUP_SQE128, then
            * this field is used for 80 bytes of arbitrary command data
            */
            __u8	cmd[0];
        };
    };

    struct io_uring_cqe {
        __u64	user_data;	/* sqe->data submission passed back */
        __s32	res;		/* result code for this event */
        __u32	flags;

        /*
        * If the ring is initialized with IORING_SETUP_CQE32, then this field
        * contains 16-bytes of padding, doubling the size of the CQE.
        */
        __u64 big_cqe[];
    };

    #define IORING_FILE_INDEX_ALLOC	 ...

    enum {
        IOSQE_FIXED_FILE_BIT,
        IOSQE_IO_DRAIN_BIT,
        IOSQE_IO_LINK_BIT,
        IOSQE_IO_HARDLINK_BIT,
        IOSQE_ASYNC_BIT,
        IOSQE_BUFFER_SELECT_BIT,
        IOSQE_CQE_SKIP_SUCCESS_BIT,
    };

    #define IOSQE_FIXED_FILE	...
    #define IOSQE_IO_DRAIN		...
    #define IOSQE_IO_LINK		...
    #define IOSQE_IO_HARDLINK	...
    #define IOSQE_ASYNC		...
    #define IOSQE_BUFFER_SELECT	...
    #define IOSQE_CQE_SKIP_SUCCESS	...

    #define POLLIN    ...
    #define POLLPRI   ...
    #define POLLOUT   ...
    #define POLLERR   ...
    #define POLLHUP   ...
    #define POLLNVAL  ...

    #define IORING_SETUP_IOPOLL               ...
    #define IORING_SETUP_SQPOLL               ...
    #define IORING_SETUP_SQ_AFF               ...
    #define IORING_SETUP_CQSIZE               ...
    #define IORING_SETUP_CLAMP                ...
    #define IORING_SETUP_ATTACH_WQ            ...
    #define IORING_SETUP_R_DISABLED           ...
    #define IORING_SETUP_SUBMIT_ALL           ...
    #define IORING_SETUP_COOP_TASKRUN         ...
    #define IORING_SETUP_TASKRUN_FLAG         ...
    #define IORING_SETUP_SQE128	              ...
    #define IORING_SETUP_CQE32	              ...
    #define IORING_SETUP_SINGLE_ISSUER        ...
    #define IORING_SETUP_DEFER_TASKRUN        ...
    #define IORING_SETUP_NO_MMAP	          ...
    #define IORING_SETUP_REGISTERED_FD_ONLY   ...
""")


ffibuilder.cdef("""
    typedef unsigned int socklen_t;
    typedef unsigned short sa_family_t;

    struct sockaddr {
        sa_family_t sa_family;
        char sa_data[14];
    };

    struct sockaddr_in {
        sa_family_t sin_family;
        uint16_t sin_port;
        struct in_addr sin_addr;
        unsigned char sin_zero[8];
    };

    struct sockaddr_in6 {
        sa_family_t sin6_family;
        uint16_t sin6_port;
        uint32_t sin6_flowinfo;
        struct in6_addr sin6_addr;
        uint32_t sin6_scope_id;
    };

    struct sockaddr_un {
        sa_family_t sun_family;
        char sun_path[108];
    };

    struct in_addr {
        uint32_t s_addr;
    };

    struct in6_addr {
        uint8_t s6_addr[16];
        ...;  /* Make the structure flexible */
    };

    struct __kernel_timespec {
        int64_t     tv_sec;
        long long   tv_nsec;
    };

    struct iovec {
        void *iov_base;
        size_t iov_len;
    };

    struct msghdr {
        void *msg_name;
        unsigned int msg_namelen;
        struct iovec *msg_iov;
        size_t msg_iovlen;
        void *msg_control;
        size_t msg_controllen;
        int msg_flags;
    };



    int io_uring_queue_init(unsigned entries, struct io_uring *ring, unsigned flags);
    void io_uring_queue_exit(struct io_uring *ring);

    static inline void io_uring_prep_send(struct io_uring_sqe *sqe, int sockfd, const void *buf, size_t len, int flags);
    static inline void io_uring_prep_recv(struct io_uring_sqe *sqe, int sockfd, void *buf, size_t len, int flags);
    static inline void io_uring_prep_accept(struct io_uring_sqe *sqe, int fd, struct sockaddr *addr, socklen_t *addrlen, int flags);
    static inline void io_uring_prep_connect(struct io_uring_sqe *sqe, int fd, const struct sockaddr *addr, socklen_t addrlen);
    static inline void io_uring_prep_cancel64(struct io_uring_sqe *sqe,  __u64 user_data, int flags);
    static inline void io_uring_prep_recvmsg(struct io_uring_sqe *sqe, int fd, struct msghdr *msg, unsigned flags);
    static inline void io_uring_prep_sendmsg(struct io_uring_sqe *sqe, int fd, const struct msghdr *msg, unsigned flags);
    static inline void io_uring_prep_splice(struct io_uring_sqe *sqe, int fd_in, int64_t off_in, int fd_out, int64_t off_out, unsigned int nbytes, unsigned int splice_flags);
    static inline void io_uring_prep_sendto(struct io_uring_sqe *sqe, int sockfd, const void *buf, size_t len, int flags, const struct sockaddr *addr, socklen_t addrlen);
    static inline void io_uring_prep_poll_add(struct io_uring_sqe *sqe, int fd, unsigned poll_mask);
    static inline void io_uring_prep_read(struct io_uring_sqe *sqe, int fd, void *buf, unsigned nbytes, __u64 offset);
    static inline void io_uring_prep_write(struct io_uring_sqe *sqe, int fd, const void *buf, unsigned nbytes, __u64 offset);

    struct io_uring_sqe *io_uring_get_sqe(struct io_uring *ring);
    static inline void io_uring_sqe_set_data64(struct io_uring_sqe *sqe, __u64 data);
    static inline void io_uring_sqe_set_flags(struct io_uring_sqe *sqe, unsigned flags);

    int io_uring_submit(struct io_uring *ring);
    static inline int io_uring_peek_cqe(struct io_uring *ring, struct io_uring_cqe **cqe_ptr);
    static inline int io_uring_wait_cqe(struct io_uring *ring, struct io_uring_cqe **cqe_ptr);
    static inline void io_uring_cqe_seen(struct io_uring *ring, struct io_uring_cqe *cqe);
    int io_uring_wait_cqe_timeout(struct io_uring *ring, struct io_uring_cqe **cqe_ptr, struct __kernel_timespec *ts);

    int io_uring_wait_cqe_nogil(struct io_uring *ring, struct io_uring_cqe **cqe_ptr);
    int io_uring_wait_cqe_timeout_nogil(struct io_uring *ring, struct io_uring_cqe **cqe_ptr, struct __kernel_timespec *ts);
""")

# TODO: remove this trick, i feel it is not a official way.
if __name__ == "__cffi__" or  __name__ == "__main__":
    ffibuilder.compile(verbose=True)
