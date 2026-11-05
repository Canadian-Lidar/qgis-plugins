/* PosPro engine: "extract" — read Certus .anpp / mapper .clraw files and write
 *   imu.bin       packet 28 raw IMU with GPS time from packet 20
 *   rt.bin        packet 20 real-time solution (20 Hz)
 *   rover.obs     RINEX 3.04 observations from packet 60 (receiver 0)
 *   rover_rx2.obs secondary receiver, if present
 *   rover.nav     RINEX 3.04 GPS navigation from packet 61
 *   <attachments> files embedded in .clraw (hardware.json, session.json ...)
 *   extract.txt   summary (key = value)
 *
 * Packet layouts follow the Certus Reference Manual v2.10 chapter 9; the
 * packet 60/61 decoding is the one checked against real Certus logs in the
 * mapper (carrier rate vs Doppler, RTKLIB single point vs Certus track).
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "pp.h"

#define MIN_VALID_UNIX 1577836800.0   /* 2020-01-01 */
#define MAXRX 2

/* ------------------------------------------------------------------------ */
/* ANPP framing                                                              */
/* ------------------------------------------------------------------------ */
static uint16_t crc_tab[256];

static void crc_init(void)
{
    int i, j;
    for (i = 0; i < 256; i++) {
        uint16_t c = (uint16_t)(i << 8);
        for (j = 0; j < 8; j++) c = (c & 0x8000) ? (uint16_t)((c << 1) ^ 0x1021) : (uint16_t)(c << 1);
        crc_tab[i] = c;
    }
}

static uint16_t crc16(const uint8_t *p, size_t n)
{
    uint16_t c = 0xFFFF;
    while (n--) c = (uint16_t)((c << 8) ^ crc_tab[((c >> 8) ^ *p++) & 0xFF]);
    return c;
}

typedef struct ext_s ext_t;
static void on_packet(ext_t *x, int id, const uint8_t *p, int n);

typedef struct {
    uint8_t buf[1 << 17];
    size_t n;
    uint64_t bad_crc, skipped;
} framer_t;

static void framer_feed(framer_t *f, ext_t *x, const uint8_t *data, size_t len)
{
    while (len > 0) {
        size_t take = sizeof(f->buf) - f->n, i = 0;
        if (take > len) take = len;
        memcpy(f->buf + f->n, data, take);
        f->n += take; data += take; len -= take;
        while (f->n - i >= 5) {
            const uint8_t *h = f->buf + i;
            uint8_t lrc = (uint8_t)(((h[1] + h[2] + h[3] + h[4]) ^ 0xFF) + 1);
            if (lrc == h[0]) {
                size_t pl = h[2];
                if (f->n - i < 5 + pl) break;
                if (crc16(h + 5, pl) == (uint16_t)(h[3] | (h[4] << 8))) {
                    on_packet(x, h[1], h + 5, (int)pl);
                    i += 5 + pl;
                    continue;
                }
                f->bad_crc++;
            }
            f->skipped++;
            i++;
        }
        memmove(f->buf, f->buf + i, f->n - i);
        f->n -= i;
    }
}

/* ------------------------------------------------------------------------ */
/* signals                                                                   */
/* ------------------------------------------------------------------------ */
static char sys_letter(int s)
{
    switch (s) {
        case 1: return 'G'; case 2: return 'R'; case 3: return 'C'; case 4: return 'E';
        case 5: return 'S'; case 6: return 'J'; case 10: return 'I';
    }
    return 0;
}

static const char *sig_code(int sys, int f)
{
    static const struct { int s, f; const char *c; } T[] = {
        {1,1,"1C"},{1,2,"1L"},{1,3,"1P"},{1,4,"1M"},{1,5,"2L"},{1,6,"2P"},{1,7,"2M"},{1,8,"5Q"},
        {2,1,"1C"},{2,3,"1P"},{2,5,"2C"},{2,6,"2P"},{2,8,"3Q"},
        {4,1,"1C"},{4,2,"1A"},{4,5,"6C"},{4,6,"6A"},{4,8,"5Q"},{4,9,"7Q"},{4,10,"8Q"},
        {3,1,"2I"},{3,5,"7I"},{3,8,"6I"},
        {5,1,"1C"},{5,8,"5I"},
        {6,1,"1C"},{6,2,"1L"},{6,3,"1Z"},{6,5,"2L"},{6,6,"6L"},{6,8,"5Q"},
        {10,8,"5A"}};
    size_t i;
    for (i = 0; i < sizeof(T)/sizeof(T[0]); i++) if (T[i].s == sys && T[i].f == f) return T[i].c;
    return NULL;
}

static const char SYS_ORDER[] = "GRECJSI";

/* ------------------------------------------------------------------------ */
/* RINEX observation writer (two passes: body to a temp file, then header)   */
/* ------------------------------------------------------------------------ */
#pragma pack(push, 1)
typedef struct { char sys; uint8_t prn; char code[2]; uint8_t track; double cp, pr; float dop, snr; } obsent_t;
#pragma pack(pop)

#define MAXENT 512
typedef struct {
    int used, rx;
    char path[1024], tmppath[1024];
    FILE *tmp;
    double cur_t;           /* GPS seconds of epoch being assembled, <0 none */
    obsent_t ent[MAXENT];
    int nent;
    char codes[8][24][3];   /* per system (index in SYS_ORDER) code list */
    int ncode[8];
    long epochs;
    double first, last, sat_sum;
    long dt_hist[64];        /* histogram of epoch spacing in 1 ms bins up to 63 ms*16 */
    double dt_val[16];
    long dt_cnt[16];
    int ndt;
} obsw_t;

static int sys_index(char s) { const char *p = strchr(SYS_ORDER, s); return p ? (int)(p - SYS_ORDER) : -1; }

static void obsw_note_code(obsw_t *w, char s, const char *c)
{
    int k = sys_index(s), i;
    if (k < 0) return;
    for (i = 0; i < w->ncode[k]; i++) if (!strcmp(w->codes[k][i], c)) return;
    if (w->ncode[k] < 24) { memcpy(w->codes[k][w->ncode[k]], c, 3); w->ncode[k]++; }
}

static struct ext_s *g_cov_x;
static void cov_sat_note(int rx, double t, int nsat);
static void obsw_flush(obsw_t *w)
{
    int i, nsat = 0;
    uint32_t n;
    if (w->cur_t < 0 || w->nent == 0) { w->nent = 0; return; }
    if (w->epochs > 0 && w->cur_t <= w->last + 1e-6) { w->nent = 0; return; }
    if (w->epochs > 0) {
        double dt = w->cur_t - w->last;
        int j;
        for (j = 0; j < w->ndt; j++) if (fabs(w->dt_val[j] - dt) < 5e-4) { w->dt_cnt[j]++; break; }
        if (j == w->ndt && w->ndt < 16) { w->dt_val[w->ndt] = dt; w->dt_cnt[w->ndt++] = 1; }
    } else w->first = w->cur_t;
    w->last = w->cur_t;
    w->epochs++;
    /* count distinct satellites */
    for (i = 0; i < w->nent; i++) {
        int j, dup = 0;
        for (j = 0; j < i; j++)
            if (w->ent[j].sys == w->ent[i].sys && w->ent[j].prn == w->ent[i].prn) { dup = 1; break; }
        if (!dup) nsat++;
    }
    w->sat_sum += nsat;
    cov_sat_note(w->rx, w->cur_t, nsat);
    n = (uint32_t)w->nent;
    fwrite(&w->cur_t, sizeof(double), 1, w->tmp);
    fwrite(&n, sizeof(n), 1, w->tmp);
    fwrite(w->ent, sizeof(obsent_t), w->nent, w->tmp);
    w->nent = 0;
}

static int ent_cmp(const void *a, const void *b)
{
    const obsent_t *x = a, *y = b;
    int sx = sys_index(x->sys), sy = sys_index(y->sys);
    if (sx != sy) return sx - sy;
    return (int)x->prn - (int)y->prn;
}

static void hl(FILE *fp, const char *content, const char *label)
{
    fprintf(fp, "%-60.60s%-20s\n", content, label);
}

static void gps_cal(double g, int *ep6i, double *sec)
{
    double ep[6];
    gtime_t t = pp_gps2gtime(g);
    time2epoch(t, ep);
    ep6i[0] = (int)ep[0]; ep6i[1] = (int)ep[1]; ep6i[2] = (int)ep[2];
    ep6i[3] = (int)ep[3]; ep6i[4] = (int)ep[4];
    *sec = ep[5];
    if (*sec >= 59.99999995) { /* guard rounding in %11.7f */
        t = timeadd(t, 1e-7); time2epoch(t, ep);
        ep6i[0] = (int)ep[0]; ep6i[1] = (int)ep[1]; ep6i[2] = (int)ep[2];
        ep6i[3] = (int)ep[3]; ep6i[4] = (int)ep[4]; *sec = ep[5];
    }
}

static int obsw_close(obsw_t *w, int leap, const double *approx, const int *glo_fcn,
                      double *interval_out)
{
    FILE *fp, *in;
    char line[256];
    int k, i, e6[6];
    double sec, interval = 0;
    long best = 0;
    obsw_flush(w);
    fclose(w->tmp);
    for (i = 0; i < w->ndt; i++) if (w->dt_cnt[i] > best) { best = w->dt_cnt[i]; interval = w->dt_val[i]; }
    *interval_out = interval;
    if (!(fp = fopen(w->path, "w"))) return 0;
    snprintf(line, sizeof(line), "%9.2f%-11s%-20s%-20s", 3.04, "", "OBSERVATION DATA", "M");
    hl(fp, line, "RINEX VERSION / TYPE");
    snprintf(line, sizeof(line), "%-20s%-20s%-20s", "PosPro " PP_VERSION, "", "");
    hl(fp, line, "PGM / RUN BY / DATE");
    hl(fp, "Converted from Advanced Navigation ANPP packet 60", "COMMENT");
    snprintf(line, sizeof(line), "Receiver %d (0 = primary antenna, 1 = secondary)", w->rx);
    hl(fp, line, "COMMENT");
    hl(fp, w->rx ? "ROVER_RX2" : "ROVER", "MARKER NAME");
    hl(fp, "GROUND_CRAFT", "MARKER TYPE");
    hl(fp, "", "OBSERVER / AGENCY");
    snprintf(line, sizeof(line), "%-20s%-20s%-20s", "", "ADVNAV CERTUS", "");
    hl(fp, line, "REC # / TYPE / VERS");
    hl(fp, "", "ANT # / TYPE");
    snprintf(line, sizeof(line), "%14.4f%14.4f%14.4f", approx[0], approx[1], approx[2]);
    hl(fp, line, "APPROX POSITION XYZ");
    snprintf(line, sizeof(line), "%14.4f%14.4f%14.4f", 0.0, 0.0, 0.0);
    hl(fp, line, "ANTENNA: DELTA H/E/N");
    for (k = 0; k < 8; k++) {
        char types[96][4];
        int nt = 0, j;
        if (!w->ncode[k]) continue;
        /* sort codes */
        for (i = 0; i < w->ncode[k]; i++)
            for (j = i + 1; j < w->ncode[k]; j++)
                if (strcmp(w->codes[k][j], w->codes[k][i]) < 0) {
                    char t[3]; memcpy(t, w->codes[k][i], 3);
                    memcpy(w->codes[k][i], w->codes[k][j], 3); memcpy(w->codes[k][j], t, 3);
                }
        for (i = 0; i < w->ncode[k]; i++) {
            snprintf(types[nt++], 4, "C%s", w->codes[k][i]);
            snprintf(types[nt++], 4, "L%s", w->codes[k][i]);
            snprintf(types[nt++], 4, "D%s", w->codes[k][i]);
            snprintf(types[nt++], 4, "S%s", w->codes[k][i]);
        }
        for (i = 0; i < nt; i += 13) {
            int m = nt - i < 13 ? nt - i : 13, o;
            if (i == 0) o = snprintf(line, sizeof(line), "%c  %3d", SYS_ORDER[k], nt);
            else o = snprintf(line, sizeof(line), "      ");
            for (j = 0; j < m; j++) o += snprintf(line + o, sizeof(line) - o, " %s", types[i + j]);
            hl(fp, line, "SYS / # / OBS TYPES");
        }
    }
    if (interval > 0) { snprintf(line, sizeof(line), "%10.3f", interval); hl(fp, line, "INTERVAL"); }
    gps_cal(w->first, e6, &sec);
    snprintf(line, sizeof(line), "%6d%6d%6d%6d%6d%13.7f     GPS", e6[0], e6[1], e6[2], e6[3], e6[4], sec);
    hl(fp, line, "TIME OF FIRST OBS");
    gps_cal(w->last, e6, &sec);
    snprintf(line, sizeof(line), "%6d%6d%6d%6d%6d%13.7f     GPS", e6[0], e6[1], e6[2], e6[3], e6[4], sec);
    hl(fp, line, "TIME OF LAST OBS");
    {
        int ns = 0, o = 0, prn;
        for (prn = 1; prn <= 32; prn++) if (glo_fcn[prn] != -99) ns++;
        if (ns) {
            int cnt = 0;
            o = snprintf(line, sizeof(line), "%3d ", ns);
            for (prn = 1; prn <= 32; prn++) {
                if (glo_fcn[prn] == -99) continue;
                o += snprintf(line + o, sizeof(line) - o, "R%02d %2d ", prn, glo_fcn[prn]);
                if (++cnt % 8 == 0) { hl(fp, line, "GLONASS SLOT / FRQ #"); o = snprintf(line, sizeof(line), "    "); }
            }
            if (cnt % 8) hl(fp, line, "GLONASS SLOT / FRQ #");
        }
    }
    snprintf(line, sizeof(line), "%6d", leap);
    hl(fp, line, "LEAP SECONDS");
    hl(fp, "", "END OF HEADER");

    if (!(in = fopen(w->tmppath, "rb"))) { fclose(fp); return 0; }
    for (;;) {
        double t;
        uint32_t n, a, b;
        obsent_t *E;
        if (fread(&t, sizeof(double), 1, in) != 1 || fread(&n, 4, 1, in) != 1) break;
        if (n > MAXENT) break;
        E = w->ent;
        if (fread(E, sizeof(obsent_t), n, in) != n) break;
        qsort(E, n, sizeof(obsent_t), ent_cmp);
        /* number of satellites */
        {
            int nsat = 0;
            for (a = 0; a < n; a++) if (a == 0 || E[a].sys != E[a-1].sys || E[a].prn != E[a-1].prn) nsat++;
            gps_cal(t, e6, &sec);
            fprintf(fp, "> %04d %02d %02d %02d %02d%11.7f  0%3d\n", e6[0], e6[1], e6[2], e6[3], e6[4], sec, nsat);
        }
        for (a = 0; a < n; a = b) {
            int ks = sys_index(E[a].sys), ci;
            char buf[1600];
            int o;
            for (b = a; b < n && E[b].sys == E[a].sys && E[b].prn == E[a].prn; b++);
            o = snprintf(buf, sizeof(buf), "%c%02d", E[a].sys, E[a].prn);
            for (ci = 0; ci < w->ncode[ks]; ci++) {
                const obsent_t *x = NULL;
                uint32_t q;
                for (q = a; q < b; q++) if (!memcmp(E[q].code, w->codes[ks][ci], 2)) { x = E + q; break; }
                if (!x) { o += snprintf(buf + o, sizeof(buf) - o, "%64s", ""); continue; }
                {
                    char ssi[2] = "";
                    int tr = x->track;
                    if ((tr & 0x20) && x->snr > 0) { int v = (int)(x->snr / 6.0); if (v < 1) v = 1; if (v > 9) v = 9; snprintf(ssi, 2, "%d", v); }
                    if ((tr & 0x08) && x->pr != 0.0 && fabs(x->pr) < 1e9)
                        o += snprintf(buf + o, sizeof(buf) - o, "%14.3f %1s", x->pr, ssi);
                    else o += snprintf(buf + o, sizeof(buf) - o, "%16s", "");
                    if ((tr & 0x01) && x->cp != 0.0 && fabs(x->cp) < 1e9) {
                        int lli = ((tr & 0x02) ? 1 : 0) | ((tr & 0x04) ? 2 : 0);
                        char ls[2] = "";
                        if (lli) snprintf(ls, 2, "%d", lli);
                        o += snprintf(buf + o, sizeof(buf) - o, "%14.3f%1s%1s", x->cp, ls, ssi);
                    } else o += snprintf(buf + o, sizeof(buf) - o, "%16s", "");
                    if (tr & 0x10) o += snprintf(buf + o, sizeof(buf) - o, "%14.3f  ", (double)x->dop);
                    else o += snprintf(buf + o, sizeof(buf) - o, "%16s", "");
                    if (tr & 0x20) o += snprintf(buf + o, sizeof(buf) - o, "%14.3f  ", (double)x->snr);
                    else o += snprintf(buf + o, sizeof(buf) - o, "%16s", "");
                }
            }
            while (o > 3 && buf[o-1] == ' ') buf[--o] = '\0';
            fprintf(fp, "%s\n", buf);
        }
    }
    fclose(in);
    fclose(fp);
    remove(w->tmppath);
    return 1;
}

/* ------------------------------------------------------------------------ */
/* GPS ephemeris from packet 61                                              */
/* ------------------------------------------------------------------------ */
typedef struct {
    int prn, iode, iodc, week, ura, flags;
    double toe, af0, af1, af2, crs, dn, m0, cuc, e, cus, sqa, cic, om0, cis, i0, crc, om, omd, idot, tgd, ttx;
} geph_t_;

/* ------------------------------------------------------------------------ */
/* extractor state                                                           */
/* ------------------------------------------------------------------------ */
struct ext_s {
    char outdir[1024];
    int leap, leap_fixed;
    /* imu */
    FILE *fimu;
    imu_hdr_t ih;
    double pend_t; int pend_ok; float pend_w[3];
    double last_imu_t;
    uint64_t imu_n, imu_dropped_invalid, imu_unpaired, imu_backwards, imu_gaps;
    uint32_t imu_dthist[500];   /* sample spacing, 0.1 ms bins up to 50 ms */
    double imu_missing;         /* fraction of the expected samples not in the recording */
    double imu_longest_gap;
    /* pairing check: gyro 28 vs packet 20 angular velocity */
    float *pc_g, *pc_w;
    size_t pc_n, pc_max;
    /* rt */
    FILE *frt;
    uint64_t rt_n;
    double rt_last;
    double approx[3];
    int have_approx;
    double first_valid_unix, last_valid_unix;
    uint64_t pkt20_invalid;
    /* gnss */
    obsw_t obs[MAXRX];
    int glo_fcn[33];
    geph_t_ *eph;
    int neph, maxeph;
    uint64_t eph_other;
    uint64_t counts[256];
    uint64_t unknown_signals;
    /* extra */
    int clraw_files;
    /* coverage per GPS second (timescale bars): IMU samples, satellites per
     * receiver, real-time fix type */
    double cov_t0;
    long cov_n, cov_max;
    uint16_t *cov_imu;
    uint8_t *cov_sat[MAXRX], *cov_fix;
    /* Certus installation alignment (packet 185, latest seen) */
    int have_align;
    float align_dcm[9], ant_off[3], odo_off[3], ext_off[3];
    /* dual antenna configuration (packet 196, latest seen) */
    int have_dual, dual_auto, dual_orient;
    float dual_off[3];
};

static long cov_bin(ext_t *x, double g)
{
    long k, i;
    if (g <= 0) return -1;
    if (x->cov_t0 == 0) x->cov_t0 = floor(g);
    k = (long)floor(g - x->cov_t0);
    if (k < 0 || k > 7 * 86400) return -1;
    if (k >= x->cov_max) {
        long nm = (k + 3600) * 2;
        int r;
        x->cov_imu = realloc(x->cov_imu, nm * sizeof(uint16_t));
        x->cov_fix = realloc(x->cov_fix, nm);
        for (r = 0; r < MAXRX; r++) x->cov_sat[r] = realloc(x->cov_sat[r], nm);
        if (!x->cov_imu || !x->cov_fix) { x->cov_max = 0; return -1; }
        for (i = x->cov_max; i < nm; i++) {
            x->cov_imu[i] = 0; x->cov_fix[i] = 255;
            for (r = 0; r < MAXRX; r++) if (x->cov_sat[r]) x->cov_sat[r][i] = 0;
        }
        x->cov_max = nm;
    }
    if (k + 1 > x->cov_n) x->cov_n = k + 1;
    return k;
}

static void cov_sat_note(int rx, double t, int nsat)
{
    ext_t *x = g_cov_x;
    long k;
    if (!x || rx < 0 || rx >= MAXRX) return;
    k = cov_bin(x, t);
    if (k >= 0 && x->cov_sat[rx] && nsat > x->cov_sat[rx][k]) x->cov_sat[rx][k] = (uint8_t)(nsat > 255 ? 255 : nsat);
}

static void cov_write(ext_t *x)
{
    char path[1100];
    FILE *f;
    long i;
    if (!x->cov_n) return;
    snprintf(path, sizeof(path), "%s/coverage.csv", x->outdir);
    if (!(f = fopen(path, "w"))) return;
    fprintf(f, "gps_s,imu,sats_rx1,sats_rx2,fix\n");
    for (i = 0; i < x->cov_n; i++)
        fprintf(f, "%.0f,%u,%u,%u,%d\n", x->cov_t0 + i, x->cov_imu[i], x->cov_sat[0] ? x->cov_sat[0][i] : 0,
                x->cov_sat[1] ? x->cov_sat[1][i] : 0, x->cov_fix[i] == 255 ? -1 : x->cov_fix[i]);
    fclose(f);
}

static void open_obs(ext_t *x, int rx)
{
    obsw_t *w = &x->obs[rx];
    if (w->used) return;
    w->used = 1;
    w->rx = rx;
    w->cur_t = -1;
    snprintf(w->path, sizeof(w->path), "%s/%s", x->outdir, rx ? "rover_rx2.obs" : "rover.obs");
    snprintf(w->tmppath, sizeof(w->tmppath), "%s.body", w->path);
    w->tmp = fopen(w->tmppath, "wb");
    if (!w->tmp) { w->used = 0; pp_warn("cannot write %s", w->tmppath); }
}

static float rf(const uint8_t *p) { float v; memcpy(&v, p, 4); return v; }
static double rd(const uint8_t *p) { double v; memcpy(&v, p, 8); return v; }
static uint32_t ru32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static int32_t ri32(const uint8_t *p) { int32_t v; memcpy(&v, p, 4); return v; }
static uint16_t ru16(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }

static void pkt20(ext_t *x, const uint8_t *p, int n)
{
    uint16_t sys, filt;
    uint32_t ut, us;
    double unix_s, g;
    if (n < 100) return;
    sys = ru16(p); filt = ru16(p + 2);
    ut = ru32(p + 4); us = ru32(p + 8);
    unix_s = ut + us * 1e-6;
    if (!(filt & 0x08) || unix_s < MIN_VALID_UNIX || us >= 1000000) {
        x->pkt20_invalid++;
        x->pend_ok = 0;
        return;
    }
    if (!x->leap_fixed && x->first_valid_unix == 0) x->leap = pp_leap_for_unix(unix_s);
    if (x->first_valid_unix == 0) x->first_valid_unix = unix_s;
    x->last_valid_unix = unix_s;
    g = unix_s - GPS_EPOCH_UNIX + x->leap;
    x->pend_t = g;
    x->pend_ok = 1;
    x->pend_w[0] = rf(p + 76); x->pend_w[1] = rf(p + 80); x->pend_w[2] = rf(p + 84);
    {
        long k = cov_bin(x, g);
        if (k >= 0) {
            int fx = (filt >> 4) & 7;
            if (x->cov_fix[k] == 255 || fx > x->cov_fix[k]) x->cov_fix[k] = (uint8_t)fx;
        }
    }
    if ((filt & 0x02) && (x->rt_n == 0 || g - x->rt_last >= 0.0499)) {
        rt_rec_t r;
        memset(&r, 0, sizeof(r));
        r.t = g;
        r.lat = rd(p + 12); r.lon = rd(p + 20); r.h = rd(p + 28);
        r.vel[0] = rf(p + 36); r.vel[1] = rf(p + 40); r.vel[2] = rf(p + 44);
        r.att[0] = rf(p + 64); r.att[1] = rf(p + 68); r.att[2] = rf(p + 72);
        r.gyr[0] = rf(p + 76); r.gyr[1] = rf(p + 80); r.gyr[2] = rf(p + 84);
        r.sd[0] = rf(p + 88); r.sd[1] = rf(p + 92); r.sd[2] = rf(p + 96);
        /* angular velocity is at 76..87 and std at 88..99 */
        r.sys_status = sys; r.filt_status = filt;
        if (fabs(r.lat) <= PI/2 && fabs(r.lon) <= PI && fabs(r.h) < 1e5) {
            fwrite(&r, sizeof(r), 1, x->frt);
            x->rt_n++;
            x->rt_last = g;
            if (!x->have_approx && ((filt >> 4) & 7) >= 2) {
                double llh[3] = {r.lat, r.lon, r.h};
                pp_llh2ecef(llh, x->approx);
                x->have_approx = 1;
            }
        }
    }
}

static void pkt28(ext_t *x, const uint8_t *p, int n)
{
    imu_rec_t r;
    int i;
    if (n < 48) return;
    if (!x->pend_ok) {
        if (x->first_valid_unix == 0) x->imu_dropped_invalid++;
        else x->imu_unpaired++;
        return;
    }
    memset(&r, 0, sizeof(r));
    r.t = x->pend_t;
    for (i = 0; i < 3; i++) { r.acc[i] = rf(p + 4*i); r.gyr[i] = rf(p + 12 + 4*i); }
    r.temp = rf(p + 36);
    x->pend_ok = 0;   /* one sample per state packet */
    for (i = 0; i < 3; i++) if (!isfinite(r.acc[i]) || !isfinite(r.gyr[i])) return;
    if (x->imu_n > 0) {
        double dt = r.t - x->last_imu_t;
        if (dt <= 0) { x->imu_backwards++; return; }
        if (x->ih.rate_hz == 0 && x->imu_n > 10) x->ih.rate_hz = 1.0 / dt;
        if (dt < 0.05) x->imu_dthist[(int)(dt * 1e4)]++;
        if (dt > 0.0125) {   /* more than 2.5 samples at 200 Hz */
            x->imu_gaps++;
            if (dt > x->imu_longest_gap) x->imu_longest_gap = dt;
        }
    } else x->ih.t0 = r.t;
    fwrite(&r, sizeof(r), 1, x->fimu);
    {
        long k = cov_bin(x, r.t);
        if (k >= 0 && x->cov_imu[k] < 65535) x->cov_imu[k]++;
    }
    x->imu_n++;
    x->last_imu_t = r.t;
    x->ih.t1 = r.t;
    if (x->pc_n < x->pc_max) {
        for (i = 0; i < 3; i++) { x->pc_g[x->pc_n*3 + i] = r.gyr[i]; x->pc_w[x->pc_n*3 + i] = x->pend_w[i]; }
        x->pc_n++;
    }
}

static void pkt60(ext_t *x, const uint8_t *p, int n)
{
    uint32_t ut, ns;
    int rx, nsat, o, i, j;
    double g;
    obsw_t *w;
    if (n < 16) return;
    ut = ru32(p); ns = ru32(p + 4);
    rx = p[12]; nsat = p[15];
    if (rx >= MAXRX || ut < MIN_VALID_UNIX || ns >= 1000000000u) return;
    if (!x->leap_fixed && x->first_valid_unix == 0) x->leap = pp_leap_for_unix((double)ut);
    g = ut - GPS_EPOCH_UNIX + x->leap + ns * 1e-9;
    open_obs(x, rx);
    w = &x->obs[rx];
    if (!w->used) return;
    if (w->cur_t < 0 || fabs(g - w->cur_t) > 1e-7) {
        obsw_flush(w);
        w->cur_t = g;
    }
    o = 16;
    for (i = 0; i < nsat; i++) {
        int sys, prn, nf;
        char L;
        if (o + 6 > n) break;
        sys = p[o]; prn = p[o + 1]; nf = p[o + 5];
        o += 6;
        L = sys_letter(sys);
        if (L == 'J' && prn >= 193) prn -= 192;
        else if (L == 'S' && prn >= 100) prn -= 100;
        for (j = 0; j < nf; j++) {
            const char *code;
            if (o + 26 > n) break;
            code = sig_code(sys, p[o]);
            if (L && code && prn > 0 && prn < 100 && w->nent < MAXENT) {
                obsent_t *e = &w->ent[w->nent++];
                e->sys = L; e->prn = (uint8_t)prn;
                memcpy(e->code, code, 2);
                e->track = p[o + 1];
                e->cp = rd(p + o + 2); e->pr = rd(p + o + 10);
                e->dop = rf(p + o + 18); e->snr = rf(p + o + 22);
                obsw_note_code(w, L, code);
            } else x->unknown_signals++;
            o += 26;
        }
    }
}

static void pkt61(ext_t *x, const uint8_t *p, int n)
{
    geph_t_ e;
    int i;
    if (n >= 94 && p[4] == 2 && p[5] >= 1 && p[5] <= 32) {
        x->glo_fcn[p[5]] = (int8_t)p[91];
    }
    if (n < 132 || p[4] != 1) { x->eph_other++; return; }
    memset(&e, 0, sizeof(e));
    e.prn = p[5];
    e.toe = ru32(p + 6); e.iodc = ru16(p + 10); e.iode = ru16(p + 12);
    e.af0 = rf(p + 14); e.af1 = rf(p + 18); e.af2 = rf(p + 22);
    e.crs = rf(p + 26); e.dn = rf(p + 30); e.m0 = rd(p + 34);
    e.cuc = rf(p + 42); e.e = rd(p + 46); e.cus = rf(p + 54);
    e.sqa = rd(p + 58); e.cic = rf(p + 66); e.om0 = rd(p + 70);
    e.cis = rf(p + 78); e.i0 = rd(p + 82); e.crc = rf(p + 90);
    e.om = rd(p + 94); e.omd = rd(p + 102); e.idot = rd(p + 110);
    e.tgd = rf(p + 118); e.week = ru16(p + 122); e.ttx = ru32(p + 124);
    e.ura = ru16(p + 128); e.flags = ru16(p + 130);
    if (e.prn < 1 || e.prn > 32 || e.sqa < 4000 || e.sqa > 6000) { x->eph_other++; return; }
    if (e.week < 1500) {
        double ref = (ru32(p) - GPS_EPOCH_UNIX + x->leap) / 604800.0;
        e.week = (int)(e.week + 1024 * floor((ref - e.week) / 1024.0 + 0.5));
    }
    for (i = 0; i < x->neph; i++) {
        geph_t_ *q = &x->eph[i];
        if (q->prn == e.prn && q->iode == e.iode && q->toe == e.toe && q->week == e.week) return;
    }
    if (x->neph >= x->maxeph) {
        x->maxeph = x->maxeph ? x->maxeph * 2 : 256;
        x->eph = realloc(x->eph, sizeof(geph_t_) * x->maxeph);
    }
    x->eph[x->neph++] = e;
}

static void on_packet(ext_t *x, int id, const uint8_t *p, int n)
{
    x->counts[id & 255]++;
    switch (id) {
        case 20: pkt20(x, p, n); break;
        case 28: pkt28(x, p, n); break;
        case 60: pkt60(x, p, n); break;
        case 61: pkt61(x, p, n); break;
        case 185:                       /* installation alignment */
            if (n >= 73) {
                int i;
                for (i = 0; i < 9; i++) x->align_dcm[i] = rf(p + 1 + 4*i);
                for (i = 0; i < 3; i++) {
                    x->ant_off[i] = rf(p + 37 + 4*i);
                    x->odo_off[i] = rf(p + 49 + 4*i);
                    x->ext_off[i] = rf(p + 61 + 4*i);
                }
                x->have_align = 1;
            }
            break;
        case 196:                       /* dual antenna configuration */
            if (n >= 17) {
                int i;
                x->dual_auto = (p[1] | (p[2] << 8)) & 1;
                x->dual_orient = p[3];
                for (i = 0; i < 3; i++) x->dual_off[i] = rf(p + 5 + 4*i);
                x->have_dual = 1;
            }
            break;
    }
}

static void enav(FILE *fp, double v) { fprintf(fp, "%19.12E", v); }

static int write_nav(ext_t *x)
{
    char path[1100];
    FILE *fp;
    int i, j;
    snprintf(path, sizeof(path), "%s/rover.nav", x->outdir);
    if (!(fp = fopen(path, "w"))) return 0;
    {
        char l[100];
        snprintf(l, sizeof(l), "%9.2f%-11s%-20s%-20s", 3.04, "", "N: GNSS NAV DATA", "G: GPS");
        hl(fp, l, "RINEX VERSION / TYPE");
        snprintf(l, sizeof(l), "%-20s%-20s%-20s", "PosPro " PP_VERSION, "", "");
        hl(fp, l, "PGM / RUN BY / DATE");
        hl(fp, "GPS ephemeris from Advanced Navigation ANPP packet 61", "COMMENT");
        snprintf(l, sizeof(l), "%6d", x->leap);
        hl(fp, l, "LEAP SECONDS");
        hl(fp, "", "END OF HEADER");
    }
    /* sort by week, toe, prn */
    for (i = 0; i < x->neph; i++)
        for (j = i + 1; j < x->neph; j++) {
            geph_t_ *a = &x->eph[i], *b = &x->eph[j];
            double ka = a->week * 604800.0 + a->toe, kb = b->week * 604800.0 + b->toe;
            if (kb < ka || (kb == ka && b->prn < a->prn)) { geph_t_ t = *a; *a = *b; *b = t; }
        }
    for (i = 0; i < x->neph; i++) {
        geph_t_ *e = &x->eph[i];
        int e6[6]; double sec;
        gps_cal(e->week * 604800.0 + e->toe, e6, &sec);
        fprintf(fp, "G%02d %04d %02d %02d %02d %02d %02d", e->prn, e6[0], e6[1], e6[2], e6[3], e6[4], (int)floor(sec + 0.5));
        enav(fp, e->af0); enav(fp, e->af1); enav(fp, e->af2); fprintf(fp, "\n    ");
        enav(fp, e->iode); enav(fp, e->crs); enav(fp, e->dn); enav(fp, e->m0); fprintf(fp, "\n    ");
        enav(fp, e->cuc); enav(fp, e->e); enav(fp, e->cus); enav(fp, e->sqa); fprintf(fp, "\n    ");
        enav(fp, e->toe); enav(fp, e->cic); enav(fp, e->om0); enav(fp, e->cis); fprintf(fp, "\n    ");
        enav(fp, e->i0); enav(fp, e->crc); enav(fp, e->om); enav(fp, e->omd); fprintf(fp, "\n    ");
        enav(fp, e->idot); enav(fp, (e->flags >> 1) & 3); enav(fp, e->week); enav(fp, e->flags & 1); fprintf(fp, "\n    ");
        enav(fp, e->ura); enav(fp, (e->flags >> 4) & 0x3F); enav(fp, e->tgd); enav(fp, e->iodc); fprintf(fp, "\n    ");
        enav(fp, e->ttx); enav(fp, (e->flags & 0x400) ? 6.0 : 4.0); fprintf(fp, "\n");
    }
    fclose(fp);
    return 1;
}

/* .clraw callbacks */
static int on_msg(void *user, const char *topic, uint64_t lt, uint64_t pt,
                  const uint8_t *data, size_t n)
{
    static framer_t *fr = NULL;
    ext_t *x = user;
    (void)lt; (void)pt;
    if (!data) { free(fr); fr = NULL; return 0; }
    if (strcmp(topic, "/ins/anpp")) return 0;
    if (!fr) fr = calloc(1, sizeof(framer_t));
    framer_feed(fr, x, data, n);
    return 0;
}

static int on_att(void *user, const char *name, const uint8_t *data, size_t n)
{
    ext_t *x = user;
    char safe[200], path[1300];
    const char *b = strrchr(name, '/');
    FILE *fp;
    size_t i, k = 0;
    b = b ? b + 1 : name;
    for (i = 0; b[i] && k < sizeof(safe) - 1; i++) {
        char c = b[i];
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')
            safe[k++] = c;
    }
    safe[k] = '\0';
    if (!k || safe[0] == '.') return 0;
    snprintf(path, sizeof(path), "%s/clraw_%s", x->outdir, safe);
    if ((fp = fopen(path, "wb"))) { fwrite(data, 1, n, fp); fclose(fp); }
    return 0;
}

/* lag that best matches packet 28 gyro to packet 20 angular velocity */
static int pairing_lag(ext_t *x, double *score)
{
    int best = 0, L, i;
    double bestc = -2;
    size_t k;
    for (L = -1; L <= 1; L++) {
        double sxy = 0, sxx = 0, syy = 0;
        for (k = 2; k + 1 < x->pc_n; k++) {
            for (i = 0; i < 3; i++) {
                double a = x->pc_g[k*3 + i] - x->pc_g[(k-1)*3 + i];
                double b = x->pc_w[(k+L)*3 + i] - x->pc_w[(k+L-1)*3 + i];
                sxy += a*b; sxx += a*a; syy += b*b;
            }
        }
        if (sxx > 0 && syy > 0) {
            double c = sxy / sqrt(sxx*syy);
            if (c > bestc) { bestc = c; best = L; }
        }
    }
    *score = bestc;
    return best;
}

int cmd_extract(int argc, char **argv)
{
    ext_t *x = calloc(1, sizeof(ext_t));
    framer_t *fr = calloc(1, sizeof(framer_t));
    int i, nin = 0, rx;
    const char *in[256];
    uint64_t total = 0, done = 0;
    char path[1200];
    FILE *fs;
    double interval[MAXRX] = {0};
    double score = 0;
    int lag;
    int force_lag = -99;
    if (!x || !fr) return pp_fail("out of memory");
    g_cov_x = x;
    crc_init();
    x->leap = 18;
    for (i = 0; i < 33; i++) x->glo_fcn[i] = -99;
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--out") && i + 1 < argc) snprintf(x->outdir, sizeof(x->outdir), "%s", argv[++i]);
        else if (!strcmp(argv[i], "--leap") && i + 1 < argc) { x->leap = atoi(argv[++i]); x->leap_fixed = 1; }
        else if (!strcmp(argv[i], "--lag") && i + 1 < argc) force_lag = atoi(argv[++i]);
        else if (nin < 256) in[nin++] = argv[i];
    }
    if (!x->outdir[0] || nin == 0) return pp_fail("usage: extract --out DIR file.anpp|file.clraw ...");
    for (i = 0; i < nin; i++) {
        FILE *fp = fopen(in[i], "rb");
        if (!fp) return pp_fail("cannot open %s", in[i]);
#ifdef _WIN32
        _fseeki64(fp, 0, SEEK_END);
        total += (uint64_t)_ftelli64(fp);
#else
        fseeko(fp, 0, SEEK_END);
        total += (uint64_t)ftello(fp);
#endif
        fclose(fp);
    }
    snprintf(path, sizeof(path), "%s/imu.bin", x->outdir);
    if (!(x->fimu = fopen(path, "wb"))) return pp_fail("cannot write %s", path);
    memcpy(x->ih.magic, IMU_MAGIC, 8);
    fwrite(&x->ih, sizeof(x->ih), 1, x->fimu);
    snprintf(path, sizeof(path), "%s/rt.bin", x->outdir);
    if (!(x->frt = fopen(path, "wb"))) return pp_fail("cannot write %s", path);
    {
        rt_hdr_t h; memset(&h, 0, sizeof(h)); memcpy(h.magic, RT_MAGIC, 8);
        fwrite(&h, sizeof(h), 1, x->frt);
    }
    x->pc_max = 200 * 600;   /* first 10 minutes */
    x->pc_g = malloc(sizeof(float) * 3 * x->pc_max);
    x->pc_w = malloc(sizeof(float) * 3 * x->pc_max);

    for (i = 0; i < nin; i++) {
        FILE *fp;
        uint8_t magic[8] = {0};
        size_t r;
        fp = fopen(in[i], "rb");
        if (!fp) return pp_fail("cannot open %s", in[i]);
        r = fread(magic, 1, 8, fp);
        if (r == 8 && magic[0] == 0x89 && !memcmp(magic + 1, "MCAP", 4)) {
            char err[512] = "";
            uint64_t d0 = done;
            fclose(fp);
            x->clraw_files++;
            if (!mcap_read(in[i], on_msg, on_att, x, &done, err, sizeof(err)))
                return pp_fail("%s", err);
            (void)d0;
            pp_progress(total ? (double)done / total : 1, "reading %s", in[i]);
        } else {
            static uint8_t buf[1 << 20];
            fseek(fp, 0, SEEK_SET);
            while ((r = fread(buf, 1, sizeof(buf), fp)) > 0) {
                framer_feed(fr, x, buf, r);
                done += r;
                pp_progress(total ? (double)done / total : 1, "reading %s", in[i]);
            }
            fclose(fp);
        }
    }
    on_msg(NULL, NULL, 0, 0, NULL, 0);

    /* finish IMU */
    lag = pairing_lag(x, &score);
    if (x->pc_n < 2000 || score < 0.3) lag = 0;
    if (force_lag != -99) lag = force_lag;
    x->ih.count = x->imu_n;
    x->ih.lag_samples = lag;
    x->ih.leap = x->leap;
    if (x->imu_n > 1) {
        /* nominal rate: the most common sample spacing (lost data does not
         * bias it, as it would a mean), snapped to a common IMU rate */
        static const double rates[] = {1000, 800, 500, 400, 250, 200, 125, 100, 50, 25, 20, 10};
        double mean = (x->imu_n - 1) / (x->ih.t1 - x->ih.t0), best = mean;
        size_t q;
        int b, bm = -1;
        for (b = 0; b < 500; b++) if (x->imu_dthist[b] && (bm < 0 || x->imu_dthist[b] > x->imu_dthist[bm])) bm = b;
        if (bm >= 0) {
            /* centre of mass of the modal bin and its neighbours */
            double sw = 0, sx = 0;
            for (b = bm > 0 ? bm - 1 : 0; b <= bm + 1 && b < 500; b++) { sw += x->imu_dthist[b]; sx += x->imu_dthist[b] * (b + 0.5) * 1e-4; }
            if (sw > 0) best = sw / sx;
        }
        for (q = 0; q < sizeof(rates) / sizeof(rates[0]); q++)
            if (fabs(best / rates[q] - 1.0) < 0.05) { best = rates[q]; break; }
        x->ih.rate_hz = best;
        x->imu_missing = 1.0 - mean / best;
        if (x->imu_missing < 0) x->imu_missing = 0;
    }
    fseek(x->fimu, 0, SEEK_SET);
    fwrite(&x->ih, sizeof(x->ih), 1, x->fimu);
    if (ferror(x->fimu) | fclose(x->fimu)) return pp_fail("could not write imu.bin (disk full?)");
    {
        rt_hdr_t h; memset(&h, 0, sizeof(h)); memcpy(h.magic, RT_MAGIC, 8); h.count = x->rt_n;
        fseek(x->frt, 0, SEEK_SET); fwrite(&h, sizeof(h), 1, x->frt); fclose(x->frt);
    }
    for (rx = 0; rx < MAXRX; rx++)
        if (x->obs[rx].used) obsw_close(&x->obs[rx], x->leap, x->approx, x->glo_fcn, &interval[rx]);
    if (x->neph) write_nav(x);
    if (x->ih.t1 > 0 && pp_leap_for_unix(x->ih.t1 - x->leap + GPS_EPOCH_UNIX) != x->leap)
        pp_warn("a leap second falls inside this capture; times after it are 1 s off");

    /* summary */
    snprintf(path, sizeof(path), "%s/extract.txt", x->outdir);
    fs = fopen(path, "w");
#define SUM(k, ...) do { char _b[512]; snprintf(_b, sizeof(_b), __VA_ARGS__); pp_result(k, "%s", _b); if (fs) fprintf(fs, "%s = %s\n", k, _b); } while (0)
    SUM("engine", "%s", PP_VERSION);
    SUM("files", "%d", nin);
    SUM("bytes", "%.0f", (double)total);
    SUM("leap_seconds", "%d", x->leap);
    SUM("imu_samples", "%.0f", (double)x->imu_n);
    SUM("imu_rate_hz", "%.3f", x->ih.rate_hz);
    SUM("imu_t0", "%.6f", x->ih.t0);
    SUM("imu_t1", "%.6f", x->ih.t1);
    SUM("imu_gaps", "%.0f", (double)x->imu_gaps);
    SUM("imu_longest_gap_s", "%.4f", x->imu_longest_gap);
    SUM("imu_missing_pct", "%.1f", 100.0 * x->imu_missing);
    if (x->imu_missing > 0.02)
        pp_warn("%.0f%% of the IMU samples are missing (%.0f gaps, the longest %.2f s): the INS stream did not arrive "
                "in full. PosPro bridges short gaps; for the next capture see Recording settings in the user guide.",
                100.0 * x->imu_missing, (double)x->imu_gaps, x->imu_longest_gap);
    SUM("imu_before_time_valid", "%.0f", (double)x->imu_dropped_invalid);
    SUM("imu_unpaired", "%.0f", (double)x->imu_unpaired);
    SUM("imu_time_backwards", "%.0f", (double)x->imu_backwards);
    SUM("imu_pairing_lag", "%d", lag);
    SUM("imu_pairing_score", "%.3f", score);
    SUM("rt_records", "%.0f", (double)x->rt_n);
    SUM("state_packets_without_time", "%.0f", (double)x->pkt20_invalid);
    SUM("approx_xyz", "%.3f,%.3f,%.3f", x->approx[0], x->approx[1], x->approx[2]);
    for (rx = 0; rx < MAXRX; rx++) {
        char k[64];
        if (!x->obs[rx].used) continue;
        snprintf(k, sizeof(k), "obs%d_epochs", rx); SUM(k, "%ld", x->obs[rx].epochs);
        snprintf(k, sizeof(k), "obs%d_interval_s", rx); SUM(k, "%.3f", interval[rx]);
        snprintf(k, sizeof(k), "obs%d_first", rx); SUM(k, "%.3f", x->obs[rx].first);
        snprintf(k, sizeof(k), "obs%d_last", rx); SUM(k, "%.3f", x->obs[rx].last);
        snprintf(k, sizeof(k), "obs%d_mean_sats", rx); SUM(k, "%.1f", x->obs[rx].epochs ? x->obs[rx].sat_sum / x->obs[rx].epochs : 0);
        {
            char sig[512] = ""; int o = 0, s, c;
            for (s = 0; s < 8; s++) for (c = 0; c < x->obs[rx].ncode[s]; c++)
                o += snprintf(sig + o, sizeof(sig) - o, "%s%c%s", o ? " " : "", SYS_ORDER[s], x->obs[rx].codes[s][c]);
            snprintf(k, sizeof(k), "obs%d_signals", rx); SUM(k, "%s", sig);
        }
    }
    if (x->have_align) {
        /* DCM -> roll, pitch, heading (vehicle from sensor), as the Certus web page shows */
        const float *c = x->align_dcm;
        double roll = atan2(c[5], c[8]) * R2D, pitch = -asin(c[2] < -1 ? -1 : c[2] > 1 ? 1 : c[2]) * R2D,
               head = atan2(c[1], c[0]) * R2D;
        SUM("certus_antenna_offset", "%.4f,%.4f,%.4f", x->ant_off[0], x->ant_off[1], x->ant_off[2]);
        SUM("certus_odometer_offset", "%.4f,%.4f,%.4f", x->odo_off[0], x->odo_off[1], x->odo_off[2]);
        SUM("certus_external_offset", "%.4f,%.4f,%.4f", x->ext_off[0], x->ext_off[1], x->ext_off[2]);
        SUM("certus_alignment_dcm", "%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f",
            c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8]);
        SUM("certus_alignment_rph", "%.3f,%.3f,%.3f", roll, pitch, head < 0 ? head + 360 : head);
    }
    if (x->have_dual) {
        static const char *on[] = {"primary front, secondary rear", "primary rear, secondary front",
                                   "primary right, secondary left", "primary left, secondary right"};
        SUM("certus_dual_mode", "%s", x->dual_auto ? "automatic" : "manual");
        SUM("certus_dual_orientation", "%s", x->dual_orient >= 0 && x->dual_orient < 4 ? on[x->dual_orient] : "?");
        SUM("certus_dual_offset", "%.4f,%.4f,%.4f", x->dual_off[0], x->dual_off[1], x->dual_off[2]);
    }
    cov_write(x);
    SUM("coverage_seconds", "%ld", x->cov_n);
    SUM("gps_ephemerides", "%d", x->neph);
    SUM("unknown_signals", "%.0f", (double)x->unknown_signals);
    SUM("crc_errors", "%.0f", (double)fr->bad_crc);
    {
        char pc[1024] = ""; int o = 0, k;
        for (k = 0; k < 256; k++) if (x->counts[k]) o += snprintf(pc + o, sizeof(pc) - o, "%s%d:%.0f", o ? " " : "", k, (double)x->counts[k]);
        SUM("packets", "%s", pc);
    }
    SUM("clraw_files", "%d", x->clraw_files);
    if (fs) fclose(fs);
    if (x->imu_n == 0) pp_warn("no raw IMU (packet 28) with valid time was found");
    if (!x->obs[0].used) pp_warn("no raw satellite observations (packet 60) were found");
    if (lag) pp_warn("packet 28 matched the %s packet 20; IMU times shifted by %d sample(s)",
                     lag > 0 ? "next" : "previous", lag);
    pp_progress(1.0, "done");
    free(x->pc_g); free(x->pc_w); free(x->eph); free(x); free(fr);
    return 0;
}
