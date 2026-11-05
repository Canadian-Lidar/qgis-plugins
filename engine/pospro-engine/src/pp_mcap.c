/* PosPro engine: minimal MCAP reader for the mapper's .clraw files.
 * Reads the data section record by record, decompresses chunks (zstd or
 * none), and reports messages with their topic plus attachments. A
 * truncated file (".partial", power loss) is read up to the last complete
 * record. */
#include <stdlib.h>
#include <string.h>
#include "pp.h"

size_t ZSTD_decompress(void *dst, size_t dstCapacity, const void *src, size_t compressedSize);
unsigned ZSTD_isError(size_t code);

#define OP_HEADER 0x01
#define OP_FOOTER 0x02
#define OP_CHANNEL 0x04
#define OP_MESSAGE 0x05
#define OP_CHUNK 0x06
#define OP_ATTACH 0x09
#define OP_DATAEND 0x0F
#define MAXCH 256

static const uint8_t MAGIC[8] = {0x89, 'M', 'C', 'A', 'P', '0', '\r', '\n'};

typedef struct {
    int used;
    char topic[128];
} chan_t;

static uint64_t rd64(const uint8_t *p) { uint64_t v; memcpy(&v, p, 8); return v; }
static uint32_t rd32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static uint16_t rd16(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }

/* parse a string field at p (len-limited); returns bytes consumed or 0 */
static size_t rdstr(const uint8_t *p, size_t n, char *out, size_t outn)
{
    uint32_t l;
    if (n < 4) return 0;
    l = rd32(p);
    if ((size_t)l + 4 > n) return 0;
    if (out) {
        size_t c = l < outn - 1 ? l : outn - 1;
        memcpy(out, p + 4, c);
        out[c] = '\0';
    }
    return 4 + (size_t)l;
}

/* handle the records inside a byte area (data section or chunk body) */
static int records(const uint8_t *p, size_t n, chan_t *ch, mcap_msg_cb on_msg,
                   void *user)
{
    size_t o = 0;
    while (o + 9 <= n) {
        uint8_t op = p[o];
        uint64_t len = rd64(p + o + 1);
        const uint8_t *c = p + o + 9;
        if (len > n - o - 9) break;
        if (op == OP_CHANNEL && len >= 4) {
            uint16_t id = rd16(c);
            if (id < MAXCH) {
                ch[id].used = 1;
                rdstr(c + 4, (size_t)len - 4, ch[id].topic, sizeof(ch[id].topic));
            }
        } else if (op == OP_MESSAGE && len >= 22) {
            uint16_t id = rd16(c);
            if (id < MAXCH && ch[id].used && on_msg) {
                if (on_msg(user, ch[id].topic, rd64(c + 6), rd64(c + 14), c + 22,
                           (size_t)len - 22))
                    return 1;
            }
        }
        o += 9 + (size_t)len;
    }
    return 0;
}

int mcap_read(const char *path, mcap_msg_cb on_msg, mcap_att_cb on_att,
              void *user, uint64_t *bytes_done, char *err, size_t errn)
{
    FILE *fp;
    uint8_t magic[8], hdr[9];
    uint8_t *rec = NULL, *dec = NULL;
    size_t recn = 0, decn = 0;
    chan_t *ch = calloc(MAXCH, sizeof(chan_t));
    int stop = 0;

    if (!ch || !(fp = fopen(path, "rb"))) {
        snprintf(err, errn, "cannot open %s", path);
        free(ch);
        return 0;
    }
    if (fread(magic, 1, 8, fp) != 8 || memcmp(magic, MAGIC, 8)) {
        snprintf(err, errn, "%s is not an MCAP (.clraw) file", path);
        fclose(fp);
        free(ch);
        return 0;
    }
    while (!stop && fread(hdr, 1, 9, fp) == 9) {
        uint8_t op = hdr[0];
        uint64_t len = rd64(hdr + 1);
        if (len > ((uint64_t)1 << 32)) break;            /* damaged */
        if (op == OP_FOOTER || op == OP_DATAEND) break;   /* summary follows */
        if (len > recn) {
            uint8_t *t = realloc(rec, (size_t)len);
            if (!t) break;
            rec = t;
            recn = (size_t)len;
        }
        if (fread(rec, 1, (size_t)len, fp) != (size_t)len) {
            pp_warn("%s ends inside a record (file was not closed); the complete part is used",
                    path);
            break;
        }
        if (bytes_done) *bytes_done += 9 + len;
        if (op == OP_CHANNEL || op == OP_MESSAGE) {
            uint8_t *tmp = malloc((size_t)len + 9);
            if (!tmp) break;
            memcpy(tmp, hdr, 9);
            memcpy(tmp + 9, rec, (size_t)len);
            stop = records(tmp, (size_t)len + 9, ch, on_msg, user);
            free(tmp);
        } else if (op == OP_CHUNK && len >= 32) {
            uint64_t usize = rd64(rec + 16);
            char comp[32];
            size_t sl = rdstr(rec + 28, (size_t)len - 28, comp, sizeof(comp)), o;
            uint64_t blen;
            const uint8_t *body;
            if (!sl || 28 + sl + 8 > len) continue;
            o = 28 + sl;
            blen = rd64(rec + o);
            body = rec + o + 8;
            if (blen > len - o - 8) continue;
            if (!comp[0]) {
                stop = records(body, (size_t)blen, ch, on_msg, user);
            } else if (!strcmp(comp, "zstd")) {
                size_t r;
                if (usize > ((uint64_t)1 << 31)) continue;
                if (usize > decn) {
                    uint8_t *t = realloc(dec, (size_t)usize);
                    if (!t) break;
                    dec = t;
                    decn = (size_t)usize;
                }
                r = ZSTD_decompress(dec, (size_t)usize, body, (size_t)blen);
                if (ZSTD_isError(r) || r != usize) {
                    pp_warn("a damaged chunk in %s was skipped", path);
                    continue;
                }
                stop = records(dec, (size_t)usize, ch, on_msg, user);
            } else {
                pp_warn("chunk compression '%s' is not supported; chunk skipped", comp);
            }
        } else if (op == OP_ATTACH && on_att && len > 16) {
            char name[256];
            size_t o = 16, sl;
            uint64_t dl;
            sl = rdstr(rec + o, (size_t)len - o, name, sizeof(name));
            if (!sl) continue;
            o += sl;
            sl = rdstr(rec + o, (size_t)len - o, NULL, 0);   /* media type */
            if (!sl) continue;
            o += sl;
            if (o + 8 > len) continue;
            dl = rd64(rec + o);
            if (dl > len - o - 8) continue;
            on_att(user, name, rec + o + 8, (size_t)dl);
        }
    }
    fclose(fp);
    free(rec);
    free(dec);
    free(ch);
    return 1;
}
