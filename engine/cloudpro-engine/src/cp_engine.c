/* CloudPro engine: the heavy per-point loops of CloudPro in C.
 *
 * Loaded by CloudPro/core/engine.py with ctypes. Every function is re-entrant and keeps no state, so
 * CloudPro's worker threads call it in parallel (ctypes releases Python's lock for the call).
 * The Python code in CloudPro stays the reference: each function here gives the same numbers as its
 * Python counterpart (tests/test_engine.py), and CloudPro falls back to Python when the library is missing.
 *
 *   cp_georef_chunk  Hesai XT-32 packets -> georeferenced 33-byte records grouped by 50 m tile
 *                    (dcs_core.hesai.decode_packets + GeorefContext.points + importer.work + Store.bin)
 *   cp_grid_stats    per-tile 0.5 m grid of accepted points (Store.grid_stats)
 */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define CP_API __declspec(dllexport)
#else
#define CP_API __attribute__((visibility("default")))
#endif

#define CP_ENGINE_VERSION 1
#define PKT 1080
#define NCH 32
#define NBLK 8
#define REC_SIZE 33
#define MAXT 4096
#define DEG2RAD (3.14159265358979323846 / 180.0)

/* sin and cos of one angle in one call (glibc and mingw-w64 both have sincos; same values as sin, cos) */
extern void sincos(double x, double *s, double *c);

/* Round half to even, as np.round / nearbyint, for |x| < 2^51 (coordinates in mm, ranges in cm). */
static inline double rne(double x)
{
    const double big = 6755399441055744.0;          /* 1.5 * 2^52 */
    volatile double y = x + big;
    return y - big;
}

struct CpGeoref {
    /* trajectory (n samples): times, E and N relative to the origin, height (geoid already removed),
       roll, pitch, heading (rad, heading unwrapped), smoothed horizontal speed */
    const double *t, *E, *N, *h, *roll, *pitch, *heading, *speed;
    int64_t n;
    /* projection Jacobian on a coarse grid (nj samples), J00 J01 J10 J11 */
    const double *tj, *J00, *J01, *J10, *J11;
    int64_t nj;
    double M[9];         /* sensor -> vehicle rotation (boresight applied), row major */
    double lever[3];     /* INS -> Lidar optical centre, vehicle frame (m) */
    double R;            /* mean Earth radius for the curvature term (m) */
    double origin_z;     /* project origin height (m) */
    double latency;      /* pose taken at t - latency (s) */
};
typedef struct CpGeoref CpGeoref;

CP_API int cp_version(void) { return CP_ENGINE_VERSION; }

/* ------------------------------------------------------------------ numpy-identical helpers */

/* np.interp(x, xp, fp) for one value, with a search hint (x mostly increasing). */
static double interp1(double x, const double *xp, const double *fp, int64_t n, int64_t *hint)
{
    if (n == 1) return fp[0];
    if (x < xp[0]) return fp[0];
    if (x > xp[n - 1]) return fp[n - 1];
    if (x == xp[n - 1]) return fp[n - 1];
    int64_t j = *hint;
    if (j < 0 || j >= n - 1 || !(xp[j] <= x && x < xp[j + 1])) {
        /* short walk from the hint, else binary search */
        if (j >= 0 && j < n - 1 && x >= xp[j]) {
            int k = 0;
            while (j < n - 2 && x >= xp[j + 1] && k < 8) { j++; k++; }
        }
        if (j < 0 || j >= n - 1 || !(xp[j] <= x && x < xp[j + 1])) {
            int64_t lo = 0, hi = n - 1;          /* xp[lo] <= x < xp[hi] */
            while (hi - lo > 1) {
                int64_t mid = lo + (hi - lo) / 2;
                if (xp[mid] <= x) lo = mid; else hi = mid;
            }
            j = lo;
        }
        *hint = j;
    }
    double slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    double r = slope * (x - xp[j]) + fp[j];
    if (isnan(r)) {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if (isnan(r) && fp[j] == fp[j + 1]) r = fp[j];
    }
    return r;
}


/* The same, split in two: locate x once (j, or -1 below the first sample, -2 at/above the last), then
   evaluate any number of arrays sampled at the same xp with exactly np.interp's arithmetic. */
static int64_t locate(double x, const double *xp, int64_t n, int64_t *hint)
{
    if (n == 1 || x < xp[0]) return -1;
    if (x >= xp[n - 1]) return -2;
    int64_t j = *hint;
    if (j >= 0 && j < n - 1 && xp[j] <= x) {
        int k = 0;
        while (j < n - 2 && x >= xp[j + 1] && k < 8) { j++; k++; }
        if (x < xp[j + 1]) { *hint = j; return j; }
    }
    int64_t lo = 0, hi = n - 1;
    while (hi - lo > 1) {
        int64_t mid = lo + (hi - lo) / 2;
        if (xp[mid] <= x) lo = mid; else hi = mid;
    }
    *hint = lo;
    return lo;
}

static inline double evalj(int64_t j, double x, const double *xp, const double *fp, int64_t n)
{
    if (j == -1) return fp[0];
    if (j == -2) return fp[n - 1];
    double slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    double r = slope * (x - xp[j]) + fp[j];
    if (isnan(r)) {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if (isnan(r) && fp[j] == fp[j + 1]) r = fp[j];
    }
    return r;
}



/* The pose between two trajectory samples, prepared once per sample interval j (the same divisions numpy does,
   done once per interval instead of once per point):
   E, N, h = slope * (x - xp[j]) + fp[j], exactly np.interp's arithmetic (finite trajectory values);
   roll, pitch, heading are linear too, so sin a = sin a0 cos d + cos a0 sin d with d = slope * (x - xp[j]) small
   and short series for sin d, cos d (error < 1e-16 for |d| < 0.05 rad; a full sincos otherwise). */
typedef struct {
    int64_t j;
    double x0;                          /* xp[j] */
    double v0[3], sv[3];                /* E, N, h at xp[j] and their slopes */
    double a0[3], sa[3], s0[3], c0[3];  /* roll, pitch, heading: value, slope, sin, cos at xp[j] */
} PoseIv;

typedef struct { int64_t j; double x0, v0[4], sv[4]; } JacIv;

static void pose_prepare(PoseIv *iv, int64_t j, const CpGeoref *g)
{
    const double *fv[3] = {g->E, g->N, g->h}, *fa[3] = {g->roll, g->pitch, g->heading};
    int64_t n = g->n, i0 = j == -1 ? 0 : (j == -2 ? n - 1 : j);
    iv->j = j;
    iv->x0 = g->t[i0];
    for (int v = 0; v < 3; v++) {
        iv->v0[v] = fv[v][i0];
        iv->sv[v] = j >= 0 ? (fv[v][j + 1] - fv[v][j]) / (g->t[j + 1] - g->t[j]) : 0.0;
        iv->a0[v] = fa[v][i0];
        iv->sa[v] = j >= 0 ? (fa[v][j + 1] - fa[v][j]) / (g->t[j + 1] - g->t[j]) : 0.0;
        sincos(iv->a0[v], &iv->s0[v], &iv->c0[v]);
    }
}

static void jac_prepare(JacIv *iv, int64_t k, const CpGeoref *g)
{
    const double *fv[4] = {g->J00, g->J01, g->J10, g->J11};
    int64_t n = g->nj, i0 = k == -1 ? 0 : (k == -2 ? n - 1 : k);
    iv->j = k;
    iv->x0 = g->tj[i0];
    for (int v = 0; v < 4; v++) {
        iv->v0[v] = fv[v][i0];
        iv->sv[v] = k >= 0 ? (fv[v][k + 1] - fv[v][k]) / (g->tj[k + 1] - g->tj[k]) : 0.0;
    }
}

static inline void angle_sc(const PoseIv *iv, int v, double dx, double *s, double *c)
{
    if (iv->j < 0) { *s = iv->s0[v]; *c = iv->c0[v]; return; }
    double d = iv->sa[v] * dx;
    if (!(d < 0.05 && d > -0.05)) { sincos(d + iv->a0[v], s, c); return; }
    double d2 = d * d;
    double sd = d * (1.0 - d2 * (1.0 / 6) * (1.0 - d2 * (1.0 / 20) * (1.0 - d2 * (1.0 / 42))));
    double cd = 1.0 - d2 * 0.5 * (1.0 - d2 * (1.0 / 12) * (1.0 - d2 * (1.0 / 30)));
    *s = iv->s0[v] * cd + iv->c0[v] * sd;
    *c = iv->c0[v] * cd - iv->s0[v] * sd;
}

static int64_t days_from_civil(int64_t y, int64_t m, int64_t d)
{
    y -= (m <= 2);
    int64_t era = (y >= 0 ? y : y - 399) / 400;
    int64_t yoe = y - era * 400;
    int64_t mp = (m + 9) % 12;
    int64_t doy = (153 * mp + 2) / 5 + d - 1;
    int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

static double packet_time(const uint8_t *p)
{
    int64_t days = days_from_civil((int64_t)p[1065] + 1900, p[1066], p[1067]);
    uint32_t us = (uint32_t)p[1071] | ((uint32_t)p[1072] << 8) | ((uint32_t)p[1073] << 16) | ((uint32_t)p[1074] << 24);
    return (double)days * 86400.0 + (double)p[1068] * 3600.0 + (double)p[1069] * 60.0 + (double)p[1070] +
           (double)us * 1e-6;
}

static int is_dual(const uint8_t *p)
{
    uint8_t m = p[1062];
    return p[10] == 2 || m == 0x39 || m == 0x3B || m == 0x3C;
}

/* ------------------------------------------------------------------ georeferencing */



typedef struct {
    double calib[NCH * 2];   /* elevation, azimuth offset (deg) per channel */
    double rmin, rmax;
    int returns;             /* 0 both, 1 strongest, 2 last */
    int drop_duplicates;
    double min_speed;        /* m/s; 0 = keep stopped packets */
    double tmin, tmax;       /* points outside [tmin, tmax] are dropped (counted as outside) */
    double tile;             /* tile size (m) */
    int line;                /* line id written into the records */
} CpChunkOpts;

typedef struct {
    int64_t packets_stopped;
    int64_t points_outside;
    int64_t points;
    int64_t ntiles;
} CpChunkStats;

#define WR_I32(q, v) do { int32_t _v = (int32_t)(v); memcpy((q), &_v, 4); } while (0)
#define WR_U16(q, v) do { uint16_t _v = (uint16_t)(v); memcpy((q), &_v, 2); } while (0)

/* Decode, skip stopped packets, georeference and pack one chunk of packets.

   P       npkt packets of 1080 bytes
   out     room for npkt * 256 records of 33 bytes; on return the records grouped by tile
           (tiles in increasing (ix, iy), points in decoding order within a tile, as Store.bin)
   tiles   room for max_tiles (ix, iy, start, count) int64 quadruples
   tmp     scratch of npkt * 256 * (33 + 8) bytes
   Returns the number of records, or -1 when more than max_tiles tiles are hit. */
CP_API int64_t cp_georef_chunk(const uint8_t *P, int64_t npkt, const CpGeoref *g, const CpChunkOpts *o,
                               uint8_t *out, int64_t *tiles, int64_t max_tiles, uint8_t *tmp,
                               CpChunkStats *st)
{
    double ce[NCH], se[NCH], sca[NCH], cca[NCH];
    for (int c = 0; c < NCH; c++) {
        double el = o->calib[2 * c] * DEG2RAD;
        ce[c] = cos(el);
        se[c] = sin(el);
        sincos(o->calib[2 * c + 1] * DEG2RAD, &sca[c], &cca[c]);
    }
    int64_t nt = 0, last_code = INT64_MIN, last_slot = 0;
    int64_t *tcode = (int64_t *)malloc(sizeof(int64_t) * 3 * MAXT);
    if (!tcode) return -2;
    int64_t *tcount = tcode + MAXT, *trank = tcode + 2 * MAXT;
    PoseIv piv; JacIv jiv;
    piv.j = jiv.j = -3;

    /* settings in locals: the record writes go through a byte pointer, which would otherwise make the compiler
       re-read every setting for every point */
    const double M0 = g->M[0], M1 = g->M[1], M2 = g->M[2], M3 = g->M[3], M4 = g->M[4], M5 = g->M[5],
                 M6 = g->M[6], M7 = g->M[7], M8 = g->M[8];
    const double L0 = g->lever[0], L1 = g->lever[1], L2 = g->lever[2], R2 = 2 * g->R, OZ = g->origin_z;
    const double LAT = g->latency, RMIN = o->rmin, RMAX = o->rmax, TMIN = o->tmin, TMAX = o->tmax;
    const double *gt = g->t, *gtj = g->tj;
    const int64_t gn = g->n, gnj = g->nj;
    const int RET = o->returns, DROP = o->drop_duplicates;
    const uint16_t LINE = (uint16_t)o->line;
    double caz[NCH];
    for (int c = 0; c < NCH; c++) caz[c] = o->calib[2 * c + 1];
    uint8_t *rec = tmp;                                     /* records in decoding order */
    int64_t *code = (int64_t *)(tmp + (size_t)npkt * 256 * REC_SIZE);
    int64_t n = 0, hint = 0, hintj = 0, hints = 0, nout = 0;
    memset(st, 0, sizeof(*st));
    double ts = o->tile * 1000.0;
    int64_t tsi = (int64_t)ts;
    int int_tile = (double)tsi == ts && tsi > 0;

    for (int64_t ip = 0; ip < npkt; ip++) {
        const uint8_t *p = P + (size_t)ip * PKT;
        double tp = packet_time(p);
        if (o->min_speed > 0) {
            double v = interp1(tp, g->t, g->speed, g->n, &hints);
            if (!(v >= o->min_speed)) { st->packets_stopped++; continue; }
        }
        double unit = (p[9] > 0 ? (double)p[9] : 4.0) * 1e-3;
        int dual = is_dual(p);
        for (int ib = 0; ib < NBLK; ib++) {
            const uint8_t *b = p + 12 + 130 * ib;
            double az = (double)(uint16_t)(b[0] | (b[1] << 8)) * 0.01;
            double sb, cb;                                  /* sin, cos of the block azimuth */
            sincos(az * DEG2RAD, &sb, &cb);
            int ret = dual ? (ib % 2) : 2;
            if (RET == 1 && ret == 0) continue;
            if (RET == 2 && ret == 1) continue;
            int s_tot = dual ? 4 : 8, s = dual ? ib / 2 + 1 : ib + 1;
            for (int c = 0; c < NCH; c++) {
                const uint8_t *q = b + 2 + 4 * c;
                uint16_t dist = (uint16_t)(q[0] | (q[1] << 8));
                double r = dist * unit;
                if (!(dist > 0 && r >= RMIN && r <= RMAX)) continue;
                if (RET == 0 && DROP && dual && (ib % 2) == 1) {
                    const uint8_t *q0 = p + 12 + 130 * (ib - 1) + 2 + 4 * c;
                    int d0 = (int)(uint16_t)(q0[0] | (q0[1] << 8));
                    int dd = d0 - (int)dist;
                    if (dd <= 1 && dd >= -1) continue;
                }
                double t = tp + (3.28 - 50.0 * (s_tot - s) + 1.512 * c + 0.28) * 1e-6;
                if (!(t >= TMIN && t <= TMAX)) { nout++; continue; }
                double azc = az + caz[c];
                double rc = r * ce[c];
                double sa = sb * cca[c] + cb * sca[c];     /* sin, cos (block azimuth + channel offset) */
                double ca = cb * cca[c] - sb * sca[c];
                double xs = rc * sa, ys = rc * ca, zs = r * se[c];
                /* sensor -> vehicle */
                double vx = M0 * xs + M1 * ys + M2 * zs + L0;
                double vy = M3 * xs + M4 * ys + M5 * zs + L1;
                double vz = M6 * xs + M7 * ys + M8 * zs + L2;
                /* pose at the firing time */
                double tq = LAT != 0.0 ? t - LAT : t;
                int64_t j = locate(tq, gt, gn, &hint);
                if (j != piv.j) pose_prepare(&piv, j, g);
                int64_t k = locate(tq, gtj, gnj, &hintj);
                if (k != jiv.j) jac_prepare(&jiv, k, g);
                double dx = tq - piv.x0, dj = tq - jiv.x0;
                double E0, N0, hh, J0, J1, J2, J3;
                if (j >= 0) {
                    E0 = piv.sv[0] * dx + piv.v0[0]; N0 = piv.sv[1] * dx + piv.v0[1]; hh = piv.sv[2] * dx + piv.v0[2];
                } else { E0 = piv.v0[0]; N0 = piv.v0[1]; hh = piv.v0[2]; }
                if (k >= 0) {
                    J0 = jiv.sv[0] * dj + jiv.v0[0]; J1 = jiv.sv[1] * dj + jiv.v0[1];
                    J2 = jiv.sv[2] * dj + jiv.v0[2]; J3 = jiv.sv[3] * dj + jiv.v0[3];
                } else { J0 = jiv.v0[0]; J1 = jiv.v0[1]; J2 = jiv.v0[2]; J3 = jiv.v0[3]; }
                double cr, sr, cp, sp, cy, sy;
                angle_sc(&piv, 0, dx, &sr, &cr);
                angle_sc(&piv, 1, dx, &sp, &cp);
                angle_sc(&piv, 2, dx, &sy, &cy);
                double c0 = cy * cp, c1 = cy * sp * sr - sy * cr, c2 = cy * sp * cr + sy * sr;
                double c3 = sy * cp, c4 = sy * sp * sr + cy * cr, c5 = sy * sp * cr - cy * sr;
                double c6 = -sp, c7 = cp * sr, c8 = cp * cr;
                double n_ = c0 * vx + c1 * vy + c2 * vz;
                double e_ = c3 * vx + c4 * vy + c5 * vz;
                double d_ = c6 * vx + c7 * vy + c8 * vz;
                double E = E0 + J0 * e_ + J1 * n_;
                double N = N0 + J2 * e_ + J3 * n_;
                double Z = hh - d_ + (e_ * e_ + n_ * n_) / R2 - OZ;
                /* record */
                uint8_t *w = rec + (size_t)n * REC_SIZE;
                double xm = rne(E * 1000), ym = rne(N * 1000), zm = rne(Z * 1000);
                WR_I32(w, xm); WR_I32(w + 4, ym); WR_I32(w + 8, zm);
                memcpy(w + 12, &t, 8);
                w[20] = q[2];                     /* intensity */
                w[21] = (uint8_t)ret;
                w[22] = (uint8_t)c;
                w[23] = 1;                        /* ASPRS 1 = unclassified */
                w[24] = 0;                        /* flag */
                WR_U16(w + 25, LINE);
                double rg = rne(r * 100);
                rg = rg < 0 ? 0 : (rg > 65535 ? 65535 : rg);
                WR_U16(w + 27, rg);
                double aw = azc >= 360.0 ? azc - 360.0 : (azc < 0.0 ? azc + 360.0 : azc);   /* = np.mod(azc, 360), |azc| < 720 */
                double a100 = rne(aw * 100);
                if (a100 >= 36000.0) a100 -= 36000.0;
                WR_U16(w + 29, a100);
                WR_U16(w + 31, 0);                /* dz */
                int64_t ix, iy;
                if (int_tile) {                   /* exact: floor of an integer quotient */
                    int64_t xi = (int32_t)xm, yi = (int32_t)ym;
                    ix = xi >= 0 ? xi / tsi : -((-xi + tsi - 1) / tsi);
                    iy = yi >= 0 ? yi / tsi : -((-yi + tsi - 1) / tsi);
                } else {
                    ix = (int64_t)floor((double)(int32_t)xm / ts);
                    iy = (int64_t)floor((double)(int32_t)ym / ts);
                }
                int64_t cd_ = (ix + 1000000) * 2000003 + (iy + 1000000);
                if (cd_ != last_code) {            /* tile slot: the last one again, or a short search */
                    int64_t k2 = 0;
                    while (k2 < nt && tcode[k2] != cd_) k2++;
                    if (k2 == nt) {
                        if (nt >= max_tiles || nt >= MAXT) { free(tcode); return -1; }
                        tcode[nt] = cd_; tcount[nt] = 0; nt++;
                    }
                    last_code = cd_; last_slot = k2;
                }
                code[n] = last_slot;
                tcount[last_slot]++;
                n++;
            }
        }
    }
    st->points = n;
    st->points_outside = nout;
    /* group by tile: order the slots by tile code (as Store.bin), then a stable scatter */
    int64_t *ord = tiles;                                 /* slot numbers in code order, in tiles[] for now */
    for (int64_t a = 0; a < nt; a++) {
        int64_t b2 = a - 1;
        while (b2 >= 0 && tcode[ord[4 * b2]] > tcode[a]) { ord[4 * (b2 + 1)] = ord[4 * b2]; b2--; }
        ord[4 * (b2 + 1)] = a;
    }
    int64_t pos = 0;
    for (int64_t r = 0; r < nt; r++) {
        int64_t sl = ord[4 * r];
        trank[sl] = pos;                                  /* fill pointer of the slot */
        pos += tcount[sl];
    }
    for (int64_t i = 0; i < n; i++) {
        int64_t sl = code[i];
        memcpy(out + (size_t)trank[sl] * REC_SIZE, rec + (size_t)i * REC_SIZE, REC_SIZE);
        trank[sl]++;
    }
    pos = 0;
    for (int64_t r = 0; r < nt; r++) {                    /* (ix, iy, start, count) in code order */
        int64_t sl = ord[4 * r], c = tcode[sl];
        int64_t ix = c / 2000003, iy = c - ix * 2000003;  /* codes are positive */
        tiles[4 * r] = ix - 1000000;
        tiles[4 * r + 1] = iy - 1000000;
        tiles[4 * r + 2] = pos;
        tiles[4 * r + 3] = tcount[sl];
        pos += tcount[sl];
    }
    free(tcode);
    st->ntiles = nt;
    return n;
}

/* ------------------------------------------------------------------ grids */

/* Store.grid_stats: n x n cells of cmm millimetres over the tile whose lower-left corner is (x0, y0) mm.
   Accepted points only (flag & 0x80 == 0). Outputs (n*n): count, rej (int64), zsum, isum (double),
   zmin, zmax (int64 mm; untouched where count = 0). */
CP_API void cp_grid_stats(const uint8_t *recs, int64_t m, int64_t x0, int64_t y0, int64_t cmm, int64_t n,
                          int64_t *count, int64_t *rej, double *zsum, double *isum, int64_t *zmin, int64_t *zmax)
{
    for (int64_t i = 0; i < m; i++) {
        const uint8_t *w = recs + (size_t)i * REC_SIZE;
        int32_t x, y, z;
        memcpy(&x, w, 4); memcpy(&y, w + 4, 4); memcpy(&z, w + 8, 4);
        int64_t cx = ((int64_t)x - x0);
        int64_t cy = ((int64_t)y - y0);
        cx = cx >= 0 ? cx / cmm : -((-cx + cmm - 1) / cmm);   /* floor division */
        cy = cy >= 0 ? cy / cmm : -((-cy + cmm - 1) / cmm);
        cx = cx < 0 ? 0 : (cx > n - 1 ? n - 1 : cx);
        cy = cy < 0 ? 0 : (cy > n - 1 ? n - 1 : cy);
        int64_t c = cy * n + cx;
        if (w[24] & 0x80) { rej[c]++; continue; }
        if (count[c] == 0) { zmin[c] = z; zmax[c] = z; }
        else { if (z < zmin[c]) zmin[c] = z; if (z > zmax[c]) zmax[c] = z; }
        count[c]++;
        zsum[c] += z;
        isum[c] += w[20];
    }
}

/* ------------------------------------------------------------------ MCAP */

/* The payloads of length plen on channel `channel` in one decompressed MCAP chunk, copied one after another
   into out (room for max payloads); as dcs_core.mcap.iter_messages + the length check of read_lidar_chunk.
   Returns the number of payloads copied (stops at max). */
CP_API int64_t cp_mcap_payloads(const uint8_t *data, int64_t n, int channel, int64_t plen, uint8_t *out,
                                int64_t max)
{
    int64_t o = 0, k = 0;
    while (o + 9 <= n && k < max) {
        uint8_t op = data[o];
        uint64_t ln;
        memcpy(&ln, data + o + 1, 8);
        int64_t b = o + 9;
        if (ln > (uint64_t)(n - b)) break;                 /* truncated record: stop, as the Python reader */
        if (op == 0x05 && ln >= 22) {
            uint16_t ch;
            memcpy(&ch, data + b, 2);
            if (ch == channel && (int64_t)ln - 22 == plen) {
                memcpy(out + (size_t)k * plen, data + b + 22, (size_t)plen);
                k++;
            }
        }
        o = b + (int64_t)ln;
    }
    return k;
}

/* ------------------------------------------------------------------ Moving objects: free-space rays */

static inline uint64_t mix64(uint64_t x)
{
    x ^= x >> 30; x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27; x *= 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

/* A growing open-addressing count table (keys -1 = empty). */
typedef struct { int64_t *keys, *cnt; int64_t cap, used; } CpTable;

static int table_init(CpTable *t, int64_t cap)
{
    t->cap = cap; t->used = 0;
    t->keys = (int64_t *)malloc(sizeof(int64_t) * (size_t)cap);
    t->cnt = (int64_t *)malloc(sizeof(int64_t) * (size_t)cap);
    if (!t->keys || !t->cnt) { free(t->keys); free(t->cnt); return -1; }
    memset(t->keys, 0xFF, sizeof(int64_t) * (size_t)cap);
    return 0;
}

static int table_grow(CpTable *t)
{
    CpTable n;
    if (table_init(&n, t->cap * 2)) return -1;
    uint64_t mask = (uint64_t)n.cap - 1;
    for (int64_t i = 0; i < t->cap; i++) {
        if (t->keys[i] < 0) continue;
        uint64_t h = mix64((uint64_t)t->keys[i]) & mask;
        while (n.keys[h] >= 0) h = (h + 1) & mask;
        n.keys[h] = t->keys[i]; n.cnt[h] = t->cnt[i];
    }
    n.used = t->used;
    free(t->keys); free(t->cnt);
    *t = n;
    return 0;
}

static inline int table_add(CpTable *t, int64_t key)
{
    uint64_t mask = (uint64_t)t->cap - 1, h = mix64((uint64_t)key) & mask;
    for (;;) {
        int64_t k = t->keys[h];
        if (k == key) { t->cnt[h]++; return 0; }
        if (k < 0) break;
        h = (h + 1) & mask;
    }
    if ((t->used + 1) * 10 > t->cap * 7) {             /* keep below 70 % full */
        if (table_grow(t)) return -1;
        return table_add(t, key);
    }
    t->keys[h] = key; t->cnt[h] = 1; t->used++;
    return 0;
}

/* Count the voxels each ray passes through before its return (filters.DynamicObjects): ray i starts at o[i],
   runs along the unit vector u[i] and is sampled at Lmax[i] - k * step, k = 0 .. ceil(max(Lmax, 0) / step) - 1;
   a sample's voxel is floor((q - lo) / vs) (inside dims), its key (voxel index << lid_bits) | line[i]. Repeats
   of the previous in-grid sample of the same ray are skipped, as the Python code does.
   Returns a table handle (NULL when out of memory): cp_table_size, then cp_table_take copies the distinct keys
   and their counts out and frees it. */
CP_API void *cp_ray_count(const double *o, const double *u, const double *Lmax, const int64_t *line, int64_t nrays,
                          double step, const double *lo, double vs, const int64_t *dims, int lid_bits)
{
    const double lo0 = lo[0], lo1 = lo[1], lo2 = lo[2];
    const int64_t d0 = dims[0], d1 = dims[1], d2 = dims[2], d12 = d1 * d2;
    CpTable *t = (CpTable *)malloc(sizeof(CpTable));
    if (!t || table_init(t, 1 << 16)) { free(t); return NULL; }
    for (int64_t i = 0; i < nrays; i++) {
        double L = Lmax[i];
        int64_t ns = (int64_t)ceil((L > 0 ? L : 0) / step);
        const double ox = o[3 * i], oy = o[3 * i + 1], oz = o[3 * i + 2];
        const double ux = u[3 * i], uy = u[3 * i + 1], uz = u[3 * i + 2];
        int64_t prev = -1;
        for (int64_t k = 0; k < ns; k++) {
            double dist = L - (double)k * step;
            double qx = ox + ux * dist, qy = oy + uy * dist, qz = oz + uz * dist;
            int64_t vx = (int64_t)floor((qx - lo0) / vs), vy = (int64_t)floor((qy - lo1) / vs),
                    vz = (int64_t)floor((qz - lo2) / vs);
            if (vx < 0 || vy < 0 || vz < 0 || vx >= d0 || vy >= d1 || vz >= d2) continue;
            int64_t key = ((vx * d12 + vy * d2 + vz) << lid_bits) | line[i];
            if (key == prev) continue;
            prev = key;
            if (table_add(t, key)) { free(t->keys); free(t->cnt); free(t); return NULL; }
        }
    }
    return t;
}

CP_API int64_t cp_table_size(void *h) { return h ? ((CpTable *)h)->used : 0; }

CP_API void cp_table_take(void *h, int64_t *keys, int64_t *cnt)
{
    CpTable *t = (CpTable *)h;
    if (!t) return;
    int64_t k = 0;
    for (int64_t i = 0; i < t->cap; i++)
        if (t->keys[i] >= 0) { keys[k] = t->keys[i]; cnt[k] = t->cnt[i]; k++; }
    free(t->keys); free(t->cnt); free(t);
}

/* ------------------------------------------------------------------ single-linkage clusters */

static int64_t uf_find(int64_t *p, int64_t i)
{
    while (p[i] != i) { p[i] = p[p[i]]; i = p[i]; }
    return i;
}

static int64_t bsearch_key(const int64_t *a, int64_t n, int64_t key)
{
    int64_t lo = 0, hi = n - 1;
    while (lo <= hi) {
        int64_t mid = lo + (hi - lo) / 2;
        if (a[mid] < key) lo = mid + 1; else if (a[mid] > key) hi = mid - 1; else return mid;
    }
    return -1;
}

/* Clusters of points closer than r (features.cluster: query_pairs(r) + connected components).
   xyz (n,3) sorted by grid cell of size r; ukeys (m) the distinct cell keys cx*d1*d2 + cy*d2 + cz in increasing
   order (cells padded by one so neighbours never wrap) and starts (m+1) where each cell's points begin.
   Writes the root of each point's set into root (n). */
CP_API void cp_cluster(const double *xyz, int64_t n, const int64_t *ukeys, const int64_t *starts, int64_t m,
                       const int64_t *dims, double r, int64_t *root)
{
    const int64_t d1 = dims[1], d2 = dims[2];
    const double r2 = r * r;
    for (int64_t i = 0; i < n; i++) root[i] = i;
    for (int64_t a = 0; a < m; a++) {
        int64_t ka = ukeys[a];
        for (int dx = -1; dx <= 1; dx++)
            for (int dy = -1; dy <= 1; dy++)
                for (int dz = -1; dz <= 1; dz++) {
                    int64_t off = dx * d1 * d2 + dy * d2 + dz;
                    if (off < 0) continue;                     /* each pair of cells once */
                    int64_t b = off == 0 ? a : bsearch_key(ukeys, m, ka + off);
                    if (b < 0) continue;
                    for (int64_t i = starts[a]; i < starts[a + 1]; i++) {
                        const double *p = xyz + 3 * i;
                        int64_t j0 = off == 0 ? i + 1 : starts[b];
                        for (int64_t j = j0; j < starts[b + 1]; j++) {
                            const double *q = xyz + 3 * j;
                            double ex = p[0] - q[0], ey = p[1] - q[1], ez = p[2] - q[2];
                            if (ex * ex + ey * ey + ez * ez <= r2) {
                                int64_t ri = uf_find(root, i), rj = uf_find(root, j);
                                if (ri != rj) { if (ri < rj) root[rj] = ri; else root[ri] = rj; }
                            }
                        }
                    }
                }
    }
    for (int64_t i = 0; i < n; i++) root[i] = uf_find(root, i);
}

/* ------------------------------------------------------------------ Vertical plane: RANSAC scores */

/* For each candidate line c (anchor (ax, ay), unit normal (n0, n1)): the number of points of P2 (m, 2 float32)
   with |(P2 - anchor) . n| < half, computed as numpy does for float32 points and a float64 normal
   (the difference in float32, the dot product in float64). */
CP_API void cp_line_counts(const float *P2, int64_t m, const float *ax, const float *ay, const double *n0,
                           const double *n1, int64_t ncand, double half, int64_t *out)
{
    for (int64_t c = 0; c < ncand; c++) {
        const float x0 = ax[c], y0 = ay[c];
        const double a = n0[c], b = n1[c];
        int64_t k = 0;
        for (int64_t i = 0; i < m; i++) {
            float dx = P2[2 * i] - x0, dy = P2[2 * i + 1] - y0;
            double d = (double)dx * a + (double)dy * b;
            k += (d < half && d > -half);
        }
        out[c] = k;
    }
}
