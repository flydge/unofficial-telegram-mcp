/* Native, inert bootstrap admission seed. No application or interpreter is run.
 * Only libSystem (including CommonCrypto) and Apple system zlib are linked.
 * Relative archive paths deliberately use printable ASCII to make case/Unicode
 * alias rejection possible before any writes without a second OS parser.
 * Canonical absolute CLI/account paths retain strict UTF-8 support.
 */
#include <CommonCrypto/CommonDigest.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pwd.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/acl.h>
#include <sys/types.h>
#include <unistd.h>
#include <zlib.h>

#define INDEX_LIMIT (2U * 1024U * 1024U)
#define ARCHIVE_LIMIT (UINT64_C(1024) * 1024 * 1024)
#define MEMBER_LIMIT (UINT64_C(256) * 1024 * 1024)
#define UNPACKED_LIMIT (UINT64_C(2) * 1024 * 1024 * 1024)
#define FILE_LIMIT 10000
#define DIR_LIMIT 10000
#define REL_LIMIT 1024
#define ABS_LIMIT 4095
#define CHUNK 65536
#define JSON_OUTPUT_LIMIT 16384

static volatile sig_atomic_t interrupted;
static uid_t owner;
static void on_signal(int number) { (void)number; interrupted = 1; }
static bool live(void) { return interrupted == 0; }
static bool zero(const unsigned char *p, size_t n) {
    for (size_t i = 0; i < n; i++) if (p[i]) return false;
    return true;
}
static bool hash_text(const char *s) {
    if (strlen(s) != 64) return false;
    for (size_t i = 0; i < 64; i++)
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
    return true;
}
static void digest_text(const unsigned char *digest, char out[65]) {
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) {
        out[2*i] = hex[digest[i] >> 4]; out[2*i+1] = hex[digest[i] & 15];
    }
    out[64] = 0;
}
/* Reject overlong encodings, surrogates, noncharacters and invalid UTF-8. */
static bool utf8(const unsigned char *s, size_t n) {
    for (size_t i = 0; i < n;) {
        uint32_t c = s[i++], min = 0; unsigned more = 0;
        if (c < 128) continue;
        if (c >= 0xc2 && c <= 0xdf) { c &= 31; more = 1; min = 128; }
        else if (c >= 0xe0 && c <= 0xef) { c &= 15; more = 2; min = 2048; }
        else if (c >= 0xf0 && c <= 0xf4) { c &= 7; more = 3; min = 65536; }
        else return false;
        if (more > n - i) return false;
        while (more--) { if ((s[i] & 0xc0) != 0x80) return false; c = (c << 6) | (s[i++] & 63); }
        if (c < min || c > 0x10ffff || (c >= 0xd800 && c <= 0xdfff) ||
            (c >= 0xfdd0 && c <= 0xfdef) || (c & 0xffff) >= 0xfffe) return false;
    }
    return true;
}
static bool path_valid(const char *path, bool absolute) {
    size_t n = strlen(path);
    if (!n || n > (absolute ? ABS_LIMIT : REL_LIMIT) || !utf8((const unsigned char *)path, n)) return false;
    if (absolute ? path[0] != '/' : path[0] == '/') return false;
    if (absolute && n == 1) return true;
    const char *p = path + (absolute ? 1 : 0), *start = p;
    for (;; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '\\' || (c && (c < 32 || c == 127 || (!absolute && c >= 128)))) return false;
        if (!c || c == '/') {
            size_t len = (size_t)(p - start);
            if (!len || len > 255 || (len == 1 && start[0] == '.') ||
                (len == 2 && start[0] == '.' && start[1] == '.')) return false;
            if (!c) return true;
            start = p + 1;
        }
    }
}
static bool same(const struct stat *a, const struct stat *b) {
    return a->st_dev == b->st_dev && a->st_ino == b->st_ino && a->st_size == b->st_size &&
        a->st_uid == b->st_uid && a->st_gid == b->st_gid && a->st_mode == b->st_mode &&
        a->st_nlink == b->st_nlink && a->st_flags == b->st_flags &&
        a->st_mtimespec.tv_sec == b->st_mtimespec.tv_sec && a->st_mtimespec.tv_nsec == b->st_mtimespec.tv_nsec &&
        a->st_ctimespec.tv_sec == b->st_ctimespec.tv_sec && a->st_ctimespec.tv_nsec == b->st_ctimespec.tv_nsec;
}
static bool same_inode(const struct stat *a, const struct stat *b) {
    return a->st_dev == b->st_dev && a->st_ino == b->st_ino && a->st_uid == b->st_uid && a->st_mode == b->st_mode;
}
/* POSIX modes do not revoke macOS inherited ACL grants. Reject allow ACEs
 * descriptor-relatively; permit deny-only ACLs such as home deny-delete.
 * Never strip or rewrite an ACL on supplied paths. macOS acl_get_entry returns
 * 0 for an entry and -1/EINVAL at end, unlike the POSIX draft convention. */
static bool safe_acl(int fd) {
    errno = 0;
    acl_t acl = acl_get_fd_np(fd, ACL_TYPE_EXTENDED);
    if (!acl) {
        /* Apple's FILESEC_ACL property reports ENOENT for no extended ACL.
         * Require the descriptor still be valid; all other errors fail closed.
         * Source: apple-oss-distributions/Libc posix1e/acl_file.c + gen/filesec.c. */
        int error = errno; struct stat current;
        return error == ENOENT && !fstat(fd, &current) && (S_ISDIR(current.st_mode) || S_ISREG(current.st_mode));
    }
    bool ok = false;
    if (!acl_valid(acl)) {
        for (unsigned i = 0; i <= ACL_MAX_ENTRIES; i++) {
            acl_entry_t entry; errno = 0;
            if (acl_get_entry(acl, i ? ACL_NEXT_ENTRY : ACL_FIRST_ENTRY, &entry)) {
                ok = errno == EINVAL; break;
            }
            acl_tag_t tag;
            if (i == ACL_MAX_ENTRIES || acl_get_tag_type(entry, &tag) || tag != ACL_EXTENDED_DENY) break;
        }
    }
    if (acl_free(acl)) ok = false;
    return ok;
}
static bool safe_dir(const struct stat *s, bool private) {
    if (!S_ISDIR(s->st_mode)) return false;
    if (private) return s->st_uid == owner && (s->st_mode & 07777) == 0700;
    return (s->st_uid == owner || s->st_uid == 0) &&
        (!(s->st_mode & 0022) || (s->st_uid == 0 && (s->st_mode & S_ISVTX)));
}
static bool canonical_fd(int fd, const char *expected) {
    char actual[ABS_LIMIT + 1] = {0};
    return !fcntl(fd, F_GETPATH, actual) && !strcmp(actual, expected);
}
/* Walk every ancestor from /, never resolving symlinks or spelling aliases. */
static int open_parent(const char *path, char leaf[256], struct stat *parent_meta) {
    if (!path_valid(path, true) || !strcmp(path, "/")) return -1;
    char copy[ABS_LIMIT + 1]; memcpy(copy, path, strlen(path) + 1);
    char *last = strrchr(copy, '/'); strcpy(leaf, last + 1); *last = 0;
    char expected[ABS_LIMIT + 1]; strcpy(expected, *copy ? copy : "/");
    int fd = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat s;
    if (fd < 0 || fstat(fd, &s) || !safe_dir(&s, false) || !safe_acl(fd)) { if (fd >= 0) close(fd); return -1; }
    char *p = *copy ? copy + 1 : copy;
    while (*p) {
        char *end = strchr(p, '/'); if (end) *end = 0;
        int next = openat(fd, p, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(fd); fd = next;
        if (fd < 0 || fstat(fd, &s) || !safe_dir(&s, false) || !safe_acl(fd)) { if (fd >= 0) close(fd); return -1; }
        if (!end) break;
        p = end + 1;
    }
    if (!canonical_fd(fd, expected)) { close(fd); return -1; }
    if (parent_meta) *parent_meta = s;
    return fd;
}
static bool safe_input(const struct stat *s, uint64_t limit) {
    return S_ISREG(s->st_mode) && s->st_uid == owner && s->st_nlink == 1 &&
        !(s->st_mode & 0022) && !(s->st_mode & 07000) && s->st_size > 0 && (uint64_t)s->st_size <= limit;
}
typedef struct { int fd; const char *path; struct stat metadata; } Input;
static bool input_open(Input *in, const char *path, uint64_t limit) {
    char leaf[256]; int parent = open_parent(path, leaf, NULL);
    if (parent < 0) return false;
    int fd = openat(parent, leaf, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC); close(parent);
    if (fd < 0) return false;
    struct stat s;
    if (fstat(fd, &s) || !safe_input(&s, limit) || !safe_acl(fd) || !canonical_fd(fd, path)) { close(fd); return false; }
    in->fd = fd; in->path = path; in->metadata = s; return true;
}
static bool input_check(const Input *in) {
    struct stat a, b; char leaf[256]; int parent = open_parent(in->path, leaf, NULL);
    if (parent < 0) return false;
    bool ok = !fstat(in->fd, &a) && !fstatat(parent, leaf, &b, AT_SYMLINK_NOFOLLOW) &&
        same(&in->metadata, &a) && same(&in->metadata, &b) && safe_acl(in->fd) && canonical_fd(in->fd, in->path); close(parent); return ok;
}
static bool fd_hash(int fd, uint64_t bytes, char out[65]) {
    if (lseek(fd, 0, SEEK_SET) != 0) return false;
    unsigned char buffer[CHUNK], digest[CC_SHA256_DIGEST_LENGTH]; CC_SHA256_CTX ctx;
    if (!CC_SHA256_Init(&ctx)) return false;
    uint64_t left = bytes;
    while (left && live()) {
        size_t wanted = left > CHUNK ? CHUNK : (size_t)left; ssize_t n = read(fd, buffer, wanted);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0 || !CC_SHA256_Update(&ctx, buffer, (CC_LONG)n)) return false;
        left -= (uint64_t)n;
    }
    if (!live() || left || read(fd, buffer, 1) != 0 || !CC_SHA256_Final(digest, &ctx)) return false;
    digest_text(digest, out); return true;
}

/* Closed-schema JSON decoder: only root object, file array and file objects.
 * There is no generic recursion and no accepted extra value to skip. */
typedef struct { const unsigned char *p, *end; } Json;
typedef struct { char path[REL_LIMIT + 1], sha[65]; uint64_t bytes; bool executable; struct stat created; } File;
typedef struct { File *files; size_t count; uint64_t archive_bytes, unpacked_bytes; char archive_sha[65]; } Index;
static void ws(Json *j) { while (j->p < j->end && (*j->p == ' ' || *j->p == '\n' || *j->p == '\r' || *j->p == '\t')) j->p++; }
static bool take(Json *j, unsigned char c) { ws(j); if (j->p == j->end || *j->p != c) return false; j->p++; return true; }
static int hex_value(unsigned c) {
    if (c >= '0' && c <= '9') return (int)c - '0';
    if (c >= 'a' && c <= 'f') return (int)c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return (int)c - 'A' + 10;
    return -1;
}
static bool codepoint(Json *j, uint32_t *out) {
    if ((size_t)(j->end - j->p) < 4) return false;
    uint32_t c = 0;
    for (unsigned i = 0; i < 4; i++) { int h = hex_value(*j->p++); if (h < 0) return false; c = (c << 4) | (unsigned)h; }
    *out = c; return true;
}
static bool string(Json *j, char *out, size_t cap) {
    if (!take(j, '"')) return false;
    size_t n = 0;
    while (j->p < j->end) {
        uint32_t c = *j->p++;
        if (c == '"') { out[n] = 0; return utf8((const unsigned char *)out, n); }
        if (c < 32) return false;
        if (c == '\\') {
            if (j->p == j->end) return false;
            c = *j->p++;
            switch (c) {
                case '"': case '\\': case '/': break;
                case 'b': c = 8; break; case 'f': c = 12; break; case 'n': c = 10; break;
                case 'r': c = 13; break; case 't': c = 9; break;
                case 'u': {
                    if (!codepoint(j, &c) || c == 0) return false;
                    if (c >= 0xd800 && c <= 0xdbff) {
                        if ((size_t)(j->end - j->p) < 6 || j->p[0] != '\\' || j->p[1] != 'u') return false;
                        j->p += 2; uint32_t low;
                        if (!codepoint(j, &low) || low < 0xdc00 || low > 0xdfff) return false;
                        c = 0x10000 + ((c - 0xd800) << 10) + low - 0xdc00;
                    } else if (c >= 0xdc00 && c <= 0xdfff) return false;
                    unsigned char encoded[4]; size_t k;
                    if (c < 128) { encoded[0] = (unsigned char)c; k = 1; }
                    else if (c < 2048) { encoded[0] = 0xc0 | (c >> 6); encoded[1] = 0x80 | (c & 63); k = 2; }
                    else if (c < 65536) { encoded[0] = 0xe0 | (c >> 12); encoded[1] = 0x80 | ((c >> 6) & 63); encoded[2] = 0x80 | (c & 63); k = 3; }
                    else { encoded[0] = 0xf0 | (c >> 18); encoded[1] = 0x80 | ((c >> 12) & 63); encoded[2] = 0x80 | ((c >> 6) & 63); encoded[3] = 0x80 | (c & 63); k = 4; }
                    if (k >= cap - n) return false;
                    memcpy(out + n, encoded, k); n += k; continue;
                }
                default: return false;
            }
        }
        if (n + 1 >= cap) return false;
        out[n++] = (char)c;
    }
    return false;
}
static bool integer(Json *j, uint64_t limit, uint64_t *out) {
    ws(j); if (j->p == j->end || *j->p < '0' || *j->p > '9') return false;
    uint64_t n = 0; bool leading_zero = *j->p == '0'; size_t digits = 0;
    while (j->p < j->end && *j->p >= '0' && *j->p <= '9') {
        unsigned d = *j->p++ - '0'; if (++digits > 20 || (leading_zero && digits > 1) || n > (limit - d) / 10 || d > limit) return false;
        n = n * 10 + d;
    }
    if (j->p < j->end && (*j->p == '.' || *j->p == 'e' || *j->p == 'E')) return false;
    *out = n; return true;
}
static bool boolean(Json *j, bool *out) {
    ws(j); size_t n = (size_t)(j->end - j->p);
    if (n >= 4 && !memcmp(j->p, "true", 4)) { j->p += 4; *out = true; return true; }
    if (n >= 5 && !memcmp(j->p, "false", 5)) { j->p += 5; *out = false; return true; }
    return false;
}
/* One number of a version: 0, or up to six digits with no leading zero. */
static bool version_number(const char **s) {
    const char *start = *s;
    while (**s >= '0' && **s <= '9') (*s)++;
    size_t n = (size_t)(*s - start);
    return n && n <= 6 && (n == 1 || *start != '0');
}
/* The one release version grammar (docs/releasing.md, Versions): X.Y.Z, then optionally aN, bN or rcN,
 * then optionally .devN. */
static bool version(const char *s) {
    for (unsigned part = 0; part < 3; part++) {
        if (!version_number(&s)) return false;
        if (part < 2) { if (*s++ != '.') return false; }
    }
    if (*s == 'a' || *s == 'b') { s++; if (!version_number(&s)) return false; }
    else if (s[0] == 'r' && s[1] == 'c') { s += 2; if (!version_number(&s)) return false; }
    if (!strncmp(s, ".dev", 4)) { s += 4; if (!version_number(&s)) return false; }
    return !*s;
}
static bool record(Json *j, File *f) {
    if (!take(j, '{')) return false;
    unsigned seen = 0;
    for (;;) {
        char key[32]; if (!string(j, key, sizeof key) || !take(j, ':')) return false;
        unsigned bit; bool ok;
        if (!strcmp(key, "path")) { bit = 1; ok = string(j, f->path, sizeof f->path) && path_valid(f->path, false); }
        else if (!strcmp(key, "bytes")) { bit = 2; ok = integer(j, MEMBER_LIMIT, &f->bytes); }
        else if (!strcmp(key, "sha256")) { bit = 4; ok = string(j, f->sha, sizeof f->sha) && hash_text(f->sha); }
        else if (!strcmp(key, "executable")) { bit = 8; ok = boolean(j, &f->executable); }
        else return false;
        if (!ok || (seen & bit)) return false; seen |= bit;
        if (take(j, '}')) return seen == 15;
        if (!take(j, ',')) return false;
    }
}
static bool index_parse(Index *idx, const unsigned char *bytes, size_t length) {
    Json j = {bytes, bytes + length}; unsigned seen = 0;
    if (!utf8(bytes, length) || !take(&j, '{')) return false;
    idx->files = calloc(FILE_LIMIT, sizeof *idx->files); if (!idx->files) return false;
    for (;;) {
        char key[32], value[128]; uint64_t n; unsigned bit; bool ok = false;
        if (!string(&j, key, sizeof key) || !take(&j, ':')) return false;
        if (!strcmp(key, "schema_version")) { bit = 1; ok = integer(&j, 1, &n) && n == 1; }
        else if (!strcmp(key, "target")) { bit = 2; ok = string(&j, value, sizeof value) && !strcmp(value, "macos-arm64"); }
        else if (!strcmp(key, "release_version")) { bit = 4; ok = string(&j, value, sizeof value) && version(value); }
        else if (!strcmp(key, "archive_bytes")) { bit = 8; ok = integer(&j, ARCHIVE_LIMIT, &idx->archive_bytes) && idx->archive_bytes > 0; }
        else if (!strcmp(key, "archive_sha256")) { bit = 16; ok = string(&j, idx->archive_sha, sizeof idx->archive_sha) && hash_text(idx->archive_sha); }
        else if (!strcmp(key, "unpacked_bytes")) { bit = 32; ok = integer(&j, UNPACKED_LIMIT, &idx->unpacked_bytes); }
        else if (!strcmp(key, "files")) {
            bit = 64; if (!take(&j, '[')) return false;
            for (;;) {
                if (idx->count >= FILE_LIMIT || !record(&j, &idx->files[idx->count++])) return false;
                if (take(&j, ']')) break;
                if (!take(&j, ',')) return false;
            }
            ok = true;
        } else return false;
        if (!ok || (seen & bit)) return false; seen |= bit;
        if (take(&j, '}')) break;
        if (!take(&j, ',')) return false;
    }
    ws(&j); if (j.p != j.end || seen != 127) return false;
    uint64_t sum = 0;
    for (size_t i = 0; i < idx->count; i++) {
        File *f = &idx->files[i]; if (i && strcmp(idx->files[i-1].path, f->path) >= 0) return false;
        if (f->bytes > UNPACKED_LIMIT - sum) return false; sum += f->bytes;
    }
    return sum == idx->unpacked_bytes;
}

/* All file and parent spellings are sorted case-insensitively before writing.
 * Equal case-folded names must denote the very same directory spelling.
 * This rejects both leaf aliases and aliases in divergent parent subtrees. */
typedef struct { char *path; bool directory; struct stat created; } Node;
typedef struct { Node *nodes; size_t count; int root; struct stat root_meta; const char *path; } Tree;
static int folded_compare(const void *a, const void *b) {
    const Node *x = a, *y = b; int c = strcasecmp(x->path, y->path);
    return c ? c : strcmp(x->path, y->path);
}
static int exact_compare(const void *a, const void *b) { return strcmp(((const Node *)a)->path, ((const Node *)b)->path); }
static bool tree_plan(Tree *tree, const Index *idx) {
    size_t capacity = FILE_LIMIT + DIR_LIMIT;
    tree->nodes = calloc(capacity, sizeof *tree->nodes); if (!tree->nodes) return false;
    /* Construct unique exact parents with a bounded temporary list. Deduplicate
     * incrementally so a common Python subtree does not consume the dir limit. */
    for (size_t i = 0; i < idx->count; i++) {
        if (tree->count == capacity) return false;
        Node *leaf = &tree->nodes[tree->count++]; leaf->path = strdup(idx->files[i].path);
        if (!leaf->path) return false;
        char path[REL_LIMIT + 1]; strcpy(path, leaf->path);
        for (char *p = path; *p; p++) if (*p == '/') {
            *p = 0; bool found = false;
            for (size_t k = 0; k < tree->count; k++) if (tree->nodes[k].directory && !strcmp(tree->nodes[k].path, path)) { found = true; break; }
            if (!found) {
                if (tree->count == capacity) return false;
                Node *n = &tree->nodes[tree->count++]; n->path = strdup(path); n->directory = true;
                if (!n->path) return false;
            }
            *p = '/';
        }
    }
    if (tree->count - idx->count > DIR_LIMIT) return false;
    qsort(tree->nodes, tree->count, sizeof *tree->nodes, folded_compare);
    for (size_t i = 1; i < tree->count; i++) if (!strcasecmp(tree->nodes[i-1].path, tree->nodes[i].path)) return false;
    qsort(tree->nodes, tree->count, sizeof *tree->nodes, exact_compare); return true;
}
static Node *node_find(Tree *tree, const char *path) {
    Node key = {.path = (char *)path}; return bsearch(&key, tree->nodes, tree->count, sizeof *tree->nodes, exact_compare);
}
/* zlib streaming reader consumes one gzip member exactly, never gzread. */
typedef struct { z_stream z; int fd; uint64_t remaining, total, limit; bool ended, initialized; unsigned char input[CHUNK]; } Reader;
static bool reader_init(Reader *r, int fd, uint64_t bytes, uint64_t limit) {
    memset(r, 0, sizeof *r); r->fd = fd; r->remaining = bytes; r->limit = limit;
    if (lseek(fd, 0, SEEK_SET) != 0 || inflateInit2(&r->z, 15 + 16) != Z_OK) return false;
    r->initialized = true; return true;
}
static bool reader_get(Reader *r, unsigned char *out, size_t wanted, size_t *got) {
    *got = 0;
    while (*got < wanted && !r->ended && live()) {
        if (!r->z.avail_in && r->remaining) {
            size_t need = r->remaining > CHUNK ? CHUNK : (size_t)r->remaining;
            ssize_t n = read(r->fd, r->input, need);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) return false;
            r->remaining -= (uint64_t)n; r->z.next_in = r->input; r->z.avail_in = (uInt)n;
        }
        r->z.next_out = out + *got; r->z.avail_out = (uInt)(wanted - *got);
        uInt before_in = r->z.avail_in, before_out = r->z.avail_out;
        int result = inflate(&r->z, Z_NO_FLUSH); size_t produced = before_out - r->z.avail_out;
        if (produced > r->limit - r->total) return false;
        r->total += produced; *got += produced;
        if (result == Z_STREAM_END) {
            unsigned char extra;
            if (r->remaining || r->z.avail_in || read(r->fd, &extra, 1) != 0) return false;
            r->ended = true;
        } else if (result != Z_OK || (!produced && before_in == r->z.avail_in)) return false;
    }
    return live();
}
static bool exact(Reader *r, unsigned char *out, size_t n) { size_t got; return reader_get(r, out, n, &got) && got == n; }
static bool octal(const unsigned char *p, size_t n, uint64_t limit, uint64_t *out) {
    /* The producer uses fixed-width ASCII octal with exactly one final NUL. */
    if (n < 2 || p[n-1]) return false;
    uint64_t v = 0;
    for (size_t i = 0; i < n - 1; i++) {
        if (p[i] < '0' || p[i] > '7') return false;
        unsigned d = p[i] - '0'; if (d > limit || v > (limit - d) / 8) return false; v = v * 8 + d;
    }
    *out = v; return true;
}
static bool field_string(const unsigned char *p, size_t n, char *out) {
    size_t length = 0; while (length < n && p[length]) length++;
    if (length < n && !zero(p + length, n - length)) return false;
    memcpy(out, p, length); out[length] = 0; return utf8(p, length);
}
static bool tar_header(const unsigned char h[512], const File *file) {
    if (memcmp(h + 257, "ustar\0" "00", 8) || h[156] != '0' || !zero(h+157, 100) ||
        !zero(h+265, 64) || !zero(h+329, 16) || !zero(h+500, 12)) return false;
    uint64_t check = 0;
    if (h[154] || h[155] != ' ') return false;
    for (size_t i = 148; i < 154; i++) { if (h[i] < '0' || h[i] > '7') return false; check = check * 8 + h[i] - '0'; }
    uint64_t sum = 0; for (size_t i = 0; i < 512; i++) sum += (i >= 148 && i < 156) ? ' ' : h[i];
    if (sum != check) return false;
    uint64_t mode, uid, gid, size, mtime;
    if (!octal(h+100, 8, 07777, &mode) || !octal(h+108, 8, 0, &uid) || !octal(h+116, 8, 0, &gid) ||
        !octal(h+124, 12, MEMBER_LIMIT, &size) || !octal(h+136, 12, 0, &mtime) ||
        mode != (file->executable ? 0500U : 0400U) || size != file->bytes) return false;
    char name[101], prefix[156], path[REL_LIMIT+1];
    if (!field_string(h, 100, name) || !field_string(h+345, 155, prefix)) return false;
    if (*prefix) { int n = snprintf(path, sizeof path, "%s/%s", prefix, name); if (n < 0 || (size_t)n >= sizeof path) return false; }
    else strcpy(path, name);
    return path_valid(path, false) && !strcmp(path, file->path);
}
static bool write_all(int fd, const unsigned char *bytes, size_t length) {
    while (length && live()) {
        ssize_t n = write(fd, bytes, length); if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false; bytes += n; length -= (size_t)n;
    }
    return live() && length == 0;
}
static int tree_parent(Tree *tree, const char *path, char leaf[256], bool create) {
    char copy[REL_LIMIT+1], current[REL_LIMIT+1] = ""; strcpy(copy, path);
    char *last = strrchr(copy, '/');
    if (!last) { strcpy(leaf, copy); return dup(tree->root); }
    strcpy(leaf, last+1); *last = 0;
    int fd = dup(tree->root); if (fd < 0) return -1;
    char *p = copy;
    while (*p) {
        char *end = strchr(p, '/'); if (end) *end = 0;
        size_t used = strlen(current), part = strlen(p);
        if (used + (used ? 1 : 0) + part > REL_LIMIT) { close(fd); return -1; }
        if (used) strcat(current, "/"); strcat(current, p);
        Node *node = node_find(tree, current);
        if (!node || !node->directory) { close(fd); return -1; }
        bool fresh = false;
        if (create && !node->created.st_ino) {
            if (mkdirat(fd, p, 0700)) { close(fd); return -1; }
            fresh = true;
        }
        int next = openat(fd, p, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        struct stat opened, named;
        if (next < 0 || fstat(next, &opened) || !safe_dir(&opened, true) || !safe_acl(next) ||
            fstatat(fd, p, &named, AT_SYMLINK_NOFOLLOW) || !same_inode(&opened, &named) ||
            (!fresh && !same_inode(&node->created, &opened)) || (fresh && fsync(fd))) {
            if (next >= 0) close(next); close(fd); return -1;
        }
        if (fresh) node->created = opened;
        close(fd); fd = next; if (!end) break; p = end + 1;
    }
    return fd;
}
static bool parse_archive(Input *archive, const Index *idx, Tree *tree, bool extract) {
    uint64_t envelope = idx->unpacked_bytes + (uint64_t)idx->count * 1023 + 1024 + 10240;
    Reader r; if (!reader_init(&r, archive->fd, idx->archive_bytes, envelope)) return false;
    bool ok = false; unsigned char buffer[CHUNK], h[512], digest[CC_SHA256_DIGEST_LENGTH];
    for (size_t i = 0; i < idx->count; i++) {
        const File *file = &idx->files[i];
        if (!exact(&r, h, sizeof h) || !tar_header(h, file)) goto finish;
        int output = -1, parent = -1; char leaf[256];
        if (extract) {
            parent = tree_parent(tree, file->path, leaf, true);
            if (parent < 0) goto finish;
            output = openat(parent, leaf, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
            if (output < 0 || !safe_acl(output)) { if (output >= 0) close(output); close(parent); goto finish; }
        }
        CC_SHA256_CTX ctx; bool content_ok = CC_SHA256_Init(&ctx) != 0; uint64_t left = file->bytes;
        while (content_ok && left && live()) {
            size_t n = left > CHUNK ? CHUNK : (size_t)left;
            content_ok = exact(&r, buffer, n) && CC_SHA256_Update(&ctx, buffer, (CC_LONG)n) &&
                (!extract || write_all(output, buffer, n)); left -= n;
        }
        char hash[65];
        content_ok = content_ok && live() && CC_SHA256_Final(digest, &ctx);
        if (content_ok) { digest_text(digest, hash); content_ok = !strcmp(hash, file->sha); }
        if (extract) {
            struct stat opened, named;
            content_ok = content_ok && !fchmod(output, file->executable ? 0500 : 0400) && !fsync(output) &&
                !fstat(output, &opened) && safe_acl(output) && S_ISREG(opened.st_mode) && opened.st_uid == owner &&
                opened.st_nlink == 1 && (uint64_t)opened.st_size == file->bytes &&
                !fstatat(parent, leaf, &named, AT_SYMLINK_NOFOLLOW) && same(&opened, &named) && !fsync(parent);
            if (content_ok) {
                Node *n = node_find(tree, file->path); if (!n || n->directory) content_ok = false;
                else n->created = opened;
            }
            if (close(output)) content_ok = false; close(parent);
        }
        if (!content_ok) goto finish;
        size_t padding = (size_t)((512 - file->bytes % 512) % 512);
        if (!exact(&r, h, padding) || !zero(h, padding)) goto finish;
    }
    if (!exact(&r, buffer, 1024) || !zero(buffer, 1024)) goto finish;
    size_t tail;
    if (!reader_get(&r, buffer, 10241, &tail) || tail > 10240 || !zero(buffer, tail) || !r.ended) goto finish;
    ok = input_check(archive);
finish:
    if (r.initialized && inflateEnd(&r.z) != Z_OK) ok = false;
    return ok;
}
/* Enumerate the actual directory tree, then reopen/hash every expected leaf.
 * Unindexed entries (including links/specials) and spelling/inode aliases fail. */
static bool scan_tree(Tree *tree, int fd, const char *prefix, size_t *seen, unsigned depth) {
    if (depth > 512 || !live()) return false;
    /* A fresh open description prevents one enumeration consuming the retained
     * directory descriptor's offset and hiding entries from the second audit. */
    int copy = openat(fd, ".", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC); if (copy < 0) return false;
    DIR *dir = fdopendir(copy); if (!dir) { close(copy); return false; }
    bool ok = true; struct dirent *entry; errno = 0;
    while ((entry = readdir(dir))) {
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        char path[REL_LIMIT+1]; int n = snprintf(path, sizeof path, "%s%s%s", prefix, *prefix ? "/" : "", entry->d_name);
        if (n < 0 || (size_t)n >= sizeof path || !path_valid(path, false)) { ok = false; break; }
        Node *node = node_find(tree, path); struct stat named, opened;
        if (!node || !node->created.st_ino || ++*seen > tree->count || fstatat(fd, entry->d_name, &named, AT_SYMLINK_NOFOLLOW) ||
            !same_inode(&node->created, &named)) { ok = false; break; }
        if (node->directory) {
            int child = openat(fd, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0 || fstat(child, &opened) || !safe_dir(&opened, true) || !safe_acl(child) || !same_inode(&opened, &named) ||
                !scan_tree(tree, child, path, seen, depth+1) || fsync(child)) ok = false;
            if (child >= 0) close(child);
            if (!ok) break;
        } else if (!S_ISREG(named.st_mode) || named.st_nlink != 1 || !same(&node->created, &named)) { ok = false; break; }
        errno = 0;
    }
    if (errno) ok = false; if (closedir(dir)) ok = false; return ok;
}
static bool tree_verify(Tree *tree, const Index *idx) {
    struct stat current; char leaf[256]; struct stat parent_meta;
    int parent = open_parent(tree->path, leaf, &parent_meta);
    if (parent < 0) return false;
    bool ok = safe_dir(&parent_meta, true) && !fstat(tree->root, &current) && safe_acl(tree->root) && same_inode(&tree->root_meta, &current) &&
        !fstatat(parent, leaf, &current, AT_SYMLINK_NOFOLLOW) && same_inode(&tree->root_meta, &current);
    close(parent); if (!ok) return false;
    size_t seen = 0; if (!scan_tree(tree, tree->root, "", &seen, 0) || seen != tree->count) return false;
    for (size_t i = 0; i < idx->count; i++) {
        const File *file = &idx->files[i]; Node *node = node_find(tree, file->path);
        int p = tree_parent(tree, file->path, leaf, false); if (p < 0) return false;
        int fd = openat(p, leaf, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC); close(p);
        struct stat before, after; char hash[65];
        ok = fd >= 0 && !fstat(fd, &before) && safe_acl(fd) && same(&node->created, &before) &&
            (before.st_mode & 07777) == (file->executable ? 0500 : 0400) && fd_hash(fd, file->bytes, hash) &&
            !strcmp(hash, file->sha) && !fstat(fd, &after) && safe_acl(fd) && same(&before, &after);
        if (fd >= 0) close(fd); if (!ok) return false;
    }
    seen = 0;
    return scan_tree(tree, tree->root, "", &seen, 0) && seen == tree->count && !fsync(tree->root);
}
static bool account_home(char home[ABS_LIMIT+1]) {
    long suggested = sysconf(_SC_GETPW_R_SIZE_MAX); size_t size = suggested > 0 && suggested <= 65536 ? (size_t)suggested : 16384;
    char *buffer = malloc(size); if (!buffer) return false;
    struct passwd pw, *result = NULL; bool ok = !getpwuid_r(owner, &pw, buffer, size, &result) && result &&
        result->pw_uid == owner && result->pw_dir && path_valid(result->pw_dir, true) && strcmp(result->pw_dir, "/");
    if (ok) strcpy(home, result->pw_dir); free(buffer); if (!ok) return false;
    char leaf[256]; int parent = open_parent(home, leaf, NULL); if (parent < 0) return false;
    int fd = openat(parent, leaf, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC); close(parent);
    struct stat s; ok = fd >= 0 && !fstat(fd, &s) && safe_dir(&s, false) && safe_acl(fd) && s.st_uid == owner && canonical_fd(fd, home);
    if (fd >= 0) close(fd); return ok;
}
static bool index_copy(const Tree *tree, const struct stat *parent_before,
                       const unsigned char *content, size_t size, const char *trusted) {
    char leaf[256]; struct stat parent_meta, root_meta;
    int parent = open_parent(tree->path, leaf, &parent_meta);
    if (parent < 0) return false;
    bool ok = safe_dir(&parent_meta, true) && same_inode(parent_before, &parent_meta) &&
        !fstatat(parent, leaf, &root_meta, AT_SYMLINK_NOFOLLOW) && same_inode(&tree->root_meta, &root_meta);
    int fd = ok ? openat(parent, "verified-index.json", O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600) : -1;
    struct stat written, named, after; char hash[65];
    ok = fd >= 0 && safe_acl(fd) && write_all(fd, content, size) && !fchmod(fd, 0600) && !fsync(fd) && !fstat(fd, &written) && safe_acl(fd) &&
        S_ISREG(written.st_mode) && written.st_uid == owner && written.st_nlink == 1 &&
        (written.st_mode & 07777) == 0600 && (uint64_t)written.st_size == size &&
        fd_hash(fd, size, hash) && !strcmp(hash, trusted) && !fstat(fd, &after) && same(&written, &after) &&
        !fstatat(parent, "verified-index.json", &named, AT_SYMLINK_NOFOLLOW) && same(&written, &named) && !fsync(parent);
    if (fd >= 0 && close(fd)) ok = false;
    close(parent); return ok;
}
typedef struct { char value[JSON_OUTPUT_LIMIT]; size_t used; } Output;
static bool append(Output *out, const char *s) {
    size_t n = strlen(s); if (n >= sizeof out->value - out->used) return false;
    memcpy(out->value + out->used, s, n+1); out->used += n; return true;
}
static bool json_string(Output *out, const char *s) {
    if (!append(out, "\"")) return false;
    for (; *s; s++) { char part[3] = {*s, 0, 0}; if (*s == '"' || *s == '\\') { part[0] = '\\'; part[1] = *s; } if (!append(out, part)) return false; }
    return append(out, "\"");
}
static bool success(Output *out, const Index *idx, const char *root, const char *index_path, const char *index_sha, const char *home) {
    char numbers[256];
    if (!append(out, "{\"schema_version\":1,\"verified\":true,\"target\":\"macos-arm64\",\"minimum_macos\":\"26.0\",\"release_root\":") ||
        !json_string(out, root) || !append(out, ",\"index_path\":") || !json_string(out, index_path) ||
        !append(out, ",\"index_sha256\":") || !json_string(out, index_sha) || !append(out, ",\"archive_sha256\":") || !json_string(out, idx->archive_sha)) return false;
    int n = snprintf(numbers, sizeof numbers, ",\"archive_bytes\":%" PRIu64 ",\"unpacked_bytes\":%" PRIu64 ",\"files\":%zu,\"os_home\":", idx->archive_bytes, idx->unpacked_bytes, idx->count);
    return n > 0 && (size_t)n < sizeof numbers && append(out, numbers) && json_string(out, home) && append(out, "}\n");
}
int main(int argc, char **argv) {
    const char *index_path = NULL, *archive_path = NULL, *destination = NULL, *trusted = NULL;
    unsigned flags = 0; bool ok = false;
    Input index = {.fd = -1}, archive = {.fd = -1}; Index idx = {0}; Tree tree = {.root = -1};
    unsigned char *content = NULL; char hash[65], home[ABS_LIMIT+1]; Output output = {{0}, 0};
    owner = getuid(); umask(077);
    struct sigaction action = {0}; action.sa_handler = on_signal; sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL) || sigaction(SIGHUP, &action, NULL) ||
        sigaction(SIGXFSZ, &action, NULL)) goto done;
    action.sa_handler = SIG_IGN; if (sigaction(SIGPIPE, &action, NULL)) goto done;
    if (owner == 0 || getgid() != getegid() || owner != geteuid() || argc != 11 || strcmp(argv[1], "prepare")) goto done;
    for (int i = 2; i < argc; i++) {
        unsigned bit;
        if (!strcmp(argv[i], "--json")) { bit = 16; }
        else {
            const char **value;
            if (!strcmp(argv[i], "--index")) { bit = 1; value = &index_path; }
            else if (!strcmp(argv[i], "--trusted-index-sha256")) { bit = 2; value = &trusted; }
            else if (!strcmp(argv[i], "--archive")) { bit = 4; value = &archive_path; }
            else if (!strcmp(argv[i], "--destination")) { bit = 8; value = &destination; }
            else goto done;
            if (++i >= argc) goto done; *value = argv[i];
        }
        if (flags & bit) goto done; flags |= bit;
    }
    if (flags != 31 || !hash_text(trusted) || !path_valid(destination, true) || !account_home(home) ||
        !input_open(&index, index_path, INDEX_LIMIT)) goto done;
    /* Authenticate bounded index bytes before its contents direct any work. */
    if (!fd_hash(index.fd, (uint64_t)index.metadata.st_size, hash) || strcmp(hash, trusted) || !input_check(&index)) goto done;
    size_t size = (size_t)index.metadata.st_size; content = malloc(size); if (!content || lseek(index.fd, 0, SEEK_SET) != 0) goto done;
    size_t got = 0;
    while (got < size && live()) { ssize_t n = read(index.fd, content+got, size-got); if (n < 0 && errno == EINTR) continue; if (n <= 0) goto done; got += (size_t)n; }
    unsigned char digest[CC_SHA256_DIGEST_LENGTH]; if (!live() || !CC_SHA256(content, (CC_LONG)size, digest)) goto done;
    digest_text(digest, hash);
    if (strcmp(hash, trusted) || !input_check(&index) || !index_parse(&idx, content, size) || !tree_plan(&tree, &idx) ||
        !input_open(&archive, archive_path, ARCHIVE_LIMIT) || (uint64_t)archive.metadata.st_size != idx.archive_bytes ||
        !fd_hash(archive.fd, idx.archive_bytes, hash) || strcmp(hash, idx.archive_sha) || !input_check(&archive)) goto done;
    char leaf[256], verified_index[ABS_LIMIT+1]; struct stat parent_meta, absent;
    size_t parent_length = (size_t)(strrchr(destination, '/') - destination);
    int path_length = snprintf(verified_index, sizeof verified_index, "%.*s/verified-index.json", (int)parent_length, destination);
    if (path_length < 0 || (size_t)path_length >= sizeof verified_index || !path_valid(verified_index, true)) goto done;
    int parent = open_parent(destination, leaf, &parent_meta); if (parent < 0) goto done;
    bool fresh = safe_dir(&parent_meta, true) && fstatat(parent, leaf, &absent, AT_SYMLINK_NOFOLLOW) < 0 && errno == ENOENT &&
        fstatat(parent, "verified-index.json", &absent, AT_SYMLINK_NOFOLLOW) < 0 && errno == ENOENT;
    if (!fresh || !parse_archive(&archive, &idx, &tree, false) || !input_check(&index) ||
        !fd_hash(archive.fd, idx.archive_bytes, hash) || strcmp(hash, idx.archive_sha) || !input_check(&archive) ||
        !success(&output, &idx, destination, verified_index, trusted, home)) { close(parent); goto done; }
    /* No filesystem writes have occurred before the complete preflight above. */
    struct stat parent_after; int parent_now = open_parent(destination, leaf, &parent_after);
    fresh = parent_now >= 0 && same_inode(&parent_meta, &parent_after) && safe_dir(&parent_after, true) &&
        !fstat(parent, &parent_after) && same_inode(&parent_meta, &parent_after);
    if (parent_now >= 0) close(parent_now);
    if (!fresh || mkdirat(parent, leaf, 0700)) { close(parent); goto done; }
    tree.root = openat(parent, leaf, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    tree.path = destination;
    fresh = tree.root >= 0 && !fstat(tree.root, &tree.root_meta) && safe_dir(&tree.root_meta, true) && safe_acl(tree.root) && !fsync(parent);
    close(parent);
    if (!fresh || !parse_archive(&archive, &idx, &tree, true) || !input_check(&index) ||
        !fd_hash(archive.fd, idx.archive_bytes, hash) || strcmp(hash, idx.archive_sha) || !input_check(&archive) ||
        !tree_verify(&tree, &idx) || !input_check(&index) || !input_check(&archive) ||
        !index_copy(&tree, &parent_meta, content, size, trusted) || !tree_verify(&tree, &idx) ||
        !input_check(&index) || !input_check(&archive) || !live()) goto done;
    ok = write_all(STDOUT_FILENO, (const unsigned char *)output.value, output.used);
done:
    if (index.fd >= 0) close(index.fd); if (archive.fd >= 0) close(archive.fd); if (tree.root >= 0) close(tree.root);
    free(content); free(idx.files);
    if (tree.nodes) { for (size_t i = 0; i < tree.count; i++) free(tree.nodes[i].path); free(tree.nodes); }
    if (!ok) { static const char error[] = "{\"schema_version\":1,\"error_code\":\"bootstrap_verification_failed\"}\n"; (void)write(STDERR_FILENO, error, sizeof error - 1); }
    return ok ? 0 : 1;
}
