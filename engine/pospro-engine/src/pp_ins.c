/* PosPro engine: strapdown INS in ECEF with an error-state Kalman filter.
 *
 * "ins" runs one pass (forward or backward in time) and writes a trajectory
 * file. Loosely coupled passes use an RTKLIB solution file of the same
 * direction; tightly coupled passes use the raw rover and base observations
 * (pp_tc.c). Vehicle constraints: non-holonomic (no sideways / vertical
 * motion of the vehicle), zero velocity and zero angular rate when parked.
 *
 * Error convention: dx = estimate - truth; attitude C_true = Exp(phi) C_est.
 * Every update is fed back into the state at once (closed loop).
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "pp_ins.h"

/* ------------------------------------------------------------------------ */
/* Kalman filter                                                             */
/* ------------------------------------------------------------------------ */
void kf_init(kf_t *kf)
{
    memset(kf, 0, sizeof(*kf));
    kf->P = calloc((size_t)NMAX * NMAX, sizeof(double));
    kf->n = NC;
}

void kf_free(kf_t *kf) { free(kf->P); kf->P = NULL; }

#define PP(i, j) PP_(kf, i, j)
#define PK(i, j) PP_(&kf, i, j)

int kf_find_amb(const kf_t *kf, int sat, int f)
{
    int k;
    for (k = 0; k < kf->namb; k++) if (kf->amb_sat[k] == sat && kf->amb_f[k] == f) return k;
    return -1;
}

static long n_amb_full;    /* states refused because the state table was full */

int kf_add_amb(kf_t *kf, int sat, int f, double value, double var, double lam)
{
    int k = kf->namb, i, idx;
    if (k >= MAXAMB) {
        if (n_amb_full++ == 0)
            pp_warn("the filter's state table is full (%d ambiguity/atmosphere states); further satellites "
                    "are used with code only until states free up", MAXAMB);
        return -1;
    }
    idx = NC + k;
    for (i = 0; i < NMAX; i++) { PP(idx, i) = 0; PP(i, idx) = 0; }
    PP(idx, idx) = var;
    kf->amb_sat[k] = sat; kf->amb_f[k] = f; kf->amb[k] = value; kf->amb_lam[k] = lam;
    kf->amb_lock[k] = 0; kf->amb_tlast[k] = 0; kf->amb_half[k] = 0; kf->amb_fixed[k] = 0;
    kf->x[idx] = 0;
    kf->namb++;
    kf->n = NC + kf->namb;
    return k;
}

void kf_del_amb(kf_t *kf, int k)
{
    int last = kf->namb - 1, i, a = NC + k, b = NC + last;
    if (k < 0 || k > last) return;
    if (k != last) {
        /* move last into k */
        for (i = 0; i < NMAX; i++) PP(a, i) = PP(b, i);
        for (i = 0; i < NMAX; i++) PP(i, a) = PP(i, b);
        PP(a, a) = PP(b, b);
        kf->x[a] = kf->x[b];
        kf->amb_sat[k] = kf->amb_sat[last]; kf->amb_f[k] = kf->amb_f[last];
        kf->amb[k] = kf->amb[last]; kf->amb_lam[k] = kf->amb_lam[last];
        kf->amb_lock[k] = kf->amb_lock[last]; kf->amb_tlast[k] = kf->amb_tlast[last];
        kf->amb_half[k] = kf->amb_half[last]; kf->amb_fixed[k] = kf->amb_fixed[last];
    }
    for (i = 0; i < NMAX; i++) { PP(b, i) = 0; PP(i, b) = 0; }
    kf->x[b] = 0;
    kf->namb--;
    kf->n = NC + kf->namb;
}

/* sparse-aware update: H is m x n row-major (n = kf->n) */
int kf_update(kf_t *kf, const double *H, const double *y, const double *R, int m)
{
    int n = kf->n, i, j, k, *nzc, *nzn;
    double *PHt, *S, *Si, *K, *inno;
    if (m <= 0) return 0;
    PHt = malloc(sizeof(double) * n * m);
    S = malloc(sizeof(double) * m * m);
    Si = malloc(sizeof(double) * m * m);
    K = malloc(sizeof(double) * n * m);
    inno = malloc(sizeof(double) * m);
    nzc = malloc(sizeof(int) * m * n);
    nzn = calloc(m, sizeof(int));
    if (!PHt || !S || !Si || !K || !inno || !nzc || !nzn) {
        free(PHt); free(S); free(Si); free(K); free(inno); free(nzc); free(nzn);
        return -1;
    }
    for (i = 0; i < m; i++)
        for (j = 0; j < n; j++) if (H[i*n + j] != 0.0) nzc[i*n + nzn[i]++] = j;
    /* PHt (n x m) */
    for (k = 0; k < m; k++) {
        for (i = 0; i < n; i++) {
            double s = 0;
            int q;
            for (q = 0; q < nzn[k]; q++) { j = nzc[k*n + q]; s += PP(i, j) * H[k*n + j]; }
            PHt[i*m + k] = s;
        }
    }
    /* S = H PHt + R */
    for (i = 0; i < m; i++) for (k = 0; k < m; k++) {
        double s = R[i*m + k];
        int q;
        for (q = 0; q < nzn[i]; q++) { j = nzc[i*n + q]; s += H[i*n + j] * PHt[j*m + k]; }
        S[i*m + k] = s;
    }
    for (i = 0; i < m; i++) for (k = i + 1; k < m; k++) {
        double a = 0.5 * (S[i*m + k] + S[k*m + i]); S[i*m + k] = S[k*m + i] = a;
    }
    memcpy(Si, S, sizeof(double) * m * m);
    if (matinv(Si, m)) {
        free(PHt); free(S); free(Si); free(K); free(inno); free(nzc); free(nzn);
        return -1;
    }
    /* K = PHt Si */
    for (i = 0; i < n; i++) for (k = 0; k < m; k++) {
        double s = 0;
        for (j = 0; j < m; j++) s += PHt[i*m + j] * Si[j*m + k];
        K[i*m + k] = s;
    }
    /* innovation relative to current error estimate */
    for (i = 0; i < m; i++) {
        double s = y[i];
        int q;
        for (q = 0; q < nzn[i]; q++) { j = nzc[i*n + q]; s -= H[i*n + j] * kf->x[j]; }
        inno[i] = s;
    }
    /* a NaN or infinite gain or innovation (a corrupt observation, a singular S that matinv let through)
     * would spread through the whole state: refuse the update and leave x and P as they were */
    {
        int bad = 0;
        for (i = 0; i < m && !bad; i++) if (!isfinite(inno[i])) bad = 1;
        for (i = 0; i < n * m && !bad; i++) if (!isfinite(K[i])) bad = 1;
        if (bad) {
            free(PHt); free(S); free(Si); free(K); free(inno); free(nzc); free(nzn);
            return -1;
        }
    }
    for (i = 0; i < n; i++) {
        double s = 0;
        for (k = 0; k < m; k++) s += K[i*m + k] * inno[k];
        kf->x[i] += s;
    }
    /* P = P - K PHt' */
    for (i = 0; i < n; i++) for (j = i; j < n; j++) {
        double s = 0;
        for (k = 0; k < m; k++) s += K[i*m + k] * PHt[j*m + k];
        PP(i, j) -= s;
        if (j != i) PP(j, i) = PP(i, j);
    }
    for (i = 0; i < n; i++) if (PP(i, i) < 1e-30) PP(i, i) = 1e-30;
    free(PHt); free(S); free(Si); free(K); free(inno); free(nzc); free(nzn);
    return 0;
}

void ins_feedback(insstate_t *s, kf_t *kf)
{
    double R[9];
    int i;
    for (i = 0; i < 3; i++) {
        s->r[i] -= kf->x[IR + i];
        s->v[i] -= kf->x[IV + i];
        s->ba[i] -= kf->x[IBA + i];
        s->bg[i] -= kf->x[IBG + i];
        s->sa[i] -= kf->x[ISA + i];
        s->sg[i] -= kf->x[ISG + i];
    }
    m3_exp(kf->x + IA, R);
    m3_mul(R, s->C, s->C);
    m3_orth(s->C);
    for (i = 0; i < kf->namb; i++) kf->amb[i] -= kf->x[NC + i];
    memset(kf->x, 0, sizeof(kf->x));
}

void ins_lever_pos(const insstate_t *s, const double *lever, double *r)
{
    double d[3];
    m3_vec(s->C, lever, d);
    r[0] = s->r[0] + d[0]; r[1] = s->r[1] + d[1]; r[2] = s->r[2] + d[2];
}

/* ------------------------------------------------------------------------ */
/* IMU file reader (block cache, either direction)                           */
/* ------------------------------------------------------------------------ */
#define BLK 65536
typedef struct {
    FILE *fp;
    imu_hdr_t h;
    imu_rec_t *blk;
    long long b0, bn;     /* cached first index, count */
    double tshift;
} imureader_t;

static int imu_open(imureader_t *ir, const char *path, double toff)
{
    memset(ir, 0, sizeof(*ir));
    if (!(ir->fp = fopen(path, "rb"))) return 0;
    if (fread(&ir->h, sizeof(ir->h), 1, ir->fp) != 1 || memcmp(ir->h.magic, IMU_MAGIC, 8)) {
        fclose(ir->fp); return 0;
    }
    ir->blk = malloc(sizeof(imu_rec_t) * BLK);
    ir->b0 = -1;
    ir->tshift = toff + (ir->h.rate_hz > 0 ? ir->h.lag_samples / ir->h.rate_hz : 0);
    return 1;
}

static const imu_rec_t *imu_get(imureader_t *ir, long long k)
{
    if (k < 0 || k >= (long long)ir->h.count) return NULL;
    if (ir->b0 < 0 || k < ir->b0 || k >= ir->b0 + ir->bn) {
        long long s = (k / BLK) * BLK, i;
#ifdef _WIN32
        _fseeki64(ir->fp, (long long)sizeof(imu_hdr_t) + s * (long long)sizeof(imu_rec_t), SEEK_SET);
#else
        fseeko(ir->fp, (off_t)(sizeof(imu_hdr_t) + s * sizeof(imu_rec_t)), SEEK_SET);
#endif
        ir->bn = (long long)fread(ir->blk, sizeof(imu_rec_t), BLK, ir->fp);
        ir->b0 = s;
        for (i = 0; i < ir->bn; i++) ir->blk[i].t += ir->tshift;
        if (k >= ir->b0 + ir->bn) return NULL;
    }
    return &ir->blk[k - ir->b0];
}

static void imu_close(imureader_t *ir) { if (ir->fp) fclose(ir->fp); free(ir->blk); }

/* first index with t >= tt */
static long long imu_search(imureader_t *ir, double tt)
{
    long long lo = 0, hi = (long long)ir->h.count - 1;
    const imu_rec_t *r;
    if (hi < 0) return 0;
    while (lo < hi) {
        long long mid = (lo + hi) / 2;
        r = imu_get(ir, mid);
        if (!r) return 0;
        if (r->t < tt) lo = mid + 1; else hi = mid;
    }
    return lo;
}

/* ------------------------------------------------------------------------ */
/* real-time (packet 20) records                                             */
/* ------------------------------------------------------------------------ */
typedef struct { rt_rec_t *r; long n; } rtbuf_t;

static int rt_load(rtbuf_t *b, const char *path)
{
    FILE *fp = fopen(path, "rb");
    rt_hdr_t h;
    memset(b, 0, sizeof(*b));
    if (!fp) return 0;
    if (fread(&h, sizeof(h), 1, fp) != 1 || memcmp(h.magic, RT_MAGIC, 8)) { fclose(fp); return 0; }
    if (h.count > 100000000ULL || !(b->r = malloc(sizeof(rt_rec_t) * (size_t)(h.count + 1)))) { fclose(fp); return 0; }
    b->n = (long)fread(b->r, sizeof(rt_rec_t), (size_t)h.count, fp);
    fclose(fp);
    return 1;
}

static const rt_rec_t *rt_near(const rtbuf_t *b, double t, double maxdt)
{
    long lo = 0, hi = b->n - 1, best = -1;
    if (b->n <= 0) return NULL;
    while (lo <= hi) {
        long mid = (lo + hi) / 2;
        if (b->r[mid].t < t) lo = mid + 1; else hi = mid - 1;
    }
    if (lo < b->n) best = lo;
    if (lo > 0 && (best < 0 || fabs(b->r[lo-1].t - t) < fabs(b->r[best].t - t))) best = lo - 1;
    if (best < 0 || fabs(b->r[best].t - t) > maxdt) return NULL;
    return &b->r[best];
}

/* ------------------------------------------------------------------------ */
/* mechanisation and covariance propagation                                  */
/* ------------------------------------------------------------------------ */
static void compensate(const insstate_t *s, const imu_rec_t *a, const imu_rec_t *b,
                       double *f, double *w)
{
    int i;
    for (i = 0; i < 3; i++) {
        double fr = 0.5 * ((double)a->acc[i] + b->acc[i]);
        double wr = 0.5 * ((double)a->gyr[i] + b->gyr[i]);
        f[i] = (fr - s->ba[i]) / (1.0 + s->sa[i]);
        w[i] = (wr - s->bg[i]) / (1.0 + s->sg[i]);
    }
}

static void mech(insstate_t *s, const double *f, const double *w, double dt)
{
    double phi[3], R[9], Re[9], fe[3], g[3], wie[3] = {0, 0, PP_OMEGA_E}, cor[3], vnew[3];
    double fb[3], wxf[3];
    int i;
    /* specific force in ECEF with half-step attitude change */
    cross3(w, f, wxf);
    for (i = 0; i < 3; i++) fb[i] = f[i] + 0.5 * wxf[i] * dt;
    m3_vec(s->C, fb, fe);
    pp_gravity_ecef(s->r, g);
    cross3(wie, s->v, cor);
    for (i = 0; i < 3; i++) vnew[i] = s->v[i] + (fe[i] + g[i] - 2.0 * cor[i]) * dt;
    for (i = 0; i < 3; i++) s->r[i] += 0.5 * (s->v[i] + vnew[i]) * dt;
    memcpy(s->v, vnew, sizeof(vnew));
    /* attitude */
    for (i = 0; i < 3; i++) phi[i] = w[i] * dt;
    m3_exp(phi, R);
    m3_mul(s->C, R, s->C);
    phi[0] = 0; phi[1] = 0; phi[2] = -PP_OMEGA_E * dt;
    m3_exp(phi, Re);
    m3_mul(Re, s->C, s->C);
    s->t += dt;
}

static void propagate_cov(kf_t *kf, const insstate_t *s, const insopt_t *o,
                          const double *f, const double *w, double dt)
{
    static double F[NC*NC], Phi[NC*NC], T[NC*NC];
    double fe[3], S[9], Om[9], wie[3] = {0, 0, PP_OMEGA_E}, adt = fabs(dt);
    double rr = norm3(s->r), u[3], kgm;
    int i, j, k, n = kf->n;
    memset(F, 0, sizeof(F));
#define FF(a, b) F[(a)*NC + (b)]
    for (i = 0; i < 3; i++) FF(IR + i, IV + i) = 1.0;
    /* gravity gradient */
    for (i = 0; i < 3; i++) u[i] = s->r[i] / rr;
    kgm = PP_GM / (rr*rr*rr);
    for (i = 0; i < 3; i++) for (j = 0; j < 3; j++)
        FF(IV + i, IR + j) = -kgm * ((i == j ? 1.0 : 0.0) - 3.0 * u[i] * u[j]);
    m3_skew(wie, Om);
    for (i = 0; i < 3; i++) for (j = 0; j < 3; j++) {
        FF(IV + i, IV + j) = -2.0 * Om[i*3 + j];
        FF(IA + i, IA + j) = -Om[i*3 + j];
    }
    m3_vec(s->C, f, fe);
    m3_skew(fe, S);
    for (i = 0; i < 3; i++) for (j = 0; j < 3; j++) {
        FF(IV + i, IA + j) = S[i*3 + j];
        FF(IV + i, IBA + j) = -s->C[i*3 + j];
        FF(IV + i, ISA + j) = -s->C[i*3 + j] * f[j];
        FF(IA + i, IBG + j) = s->C[i*3 + j];
        FF(IA + i, ISG + j) = s->C[i*3 + j] * w[j];
    }
    for (i = 0; i < 3; i++) {
        /* Gauss-Markov decays in either direction of time */
        if (o->ba_tau > 0) FF(IBA + i, IBA + i) = (dt < 0 ? 1.0 : -1.0) / o->ba_tau;
        if (o->bg_tau > 0) FF(IBG + i, IBG + i) = (dt < 0 ? 1.0 : -1.0) / o->bg_tau;
    }
#undef FF
    /* Phi = I + F dt + (F dt)^2 / 2 */
    for (i = 0; i < NC*NC; i++) F[i] *= dt;
    for (i = 0; i < NC; i++) for (j = 0; j < NC; j++) {
        double s2 = 0;
        for (k = 0; k < NC; k++) s2 += F[i*NC + k] * F[k*NC + j];
        Phi[i*NC + j] = (i == j ? 1.0 : 0.0) + F[i*NC + j] + 0.5 * s2;
    }
    /* Pcc = Phi Pcc Phi' */
    for (i = 0; i < NC; i++) for (j = 0; j < NC; j++) {
        double s2 = 0;
        for (k = 0; k < NC; k++) s2 += Phi[i*NC + k] * PP(k, j);
        T[i*NC + j] = s2;
    }
    for (i = 0; i < NC; i++) for (j = i; j < NC; j++) {
        double s2 = 0;
        for (k = 0; k < NC; k++) s2 += T[i*NC + k] * Phi[j*NC + k];
        PP(i, j) = s2; PP(j, i) = s2;
    }
    /* Pca = Phi Pca */
    for (j = NC; j < n; j++) {
        double col[NC];
        for (i = 0; i < NC; i++) {
            double s2 = 0;
            for (k = 0; k < NC; k++) s2 += Phi[i*NC + k] * PP(k, j);
            col[i] = s2;
        }
        for (i = 0; i < NC; i++) { PP(i, j) = col[i]; PP(j, i) = col[i]; }
    }
    /* process noise */
    for (i = 0; i < 3; i++) {
        PP(IV + i, IV + i) += o->vrw * o->vrw * adt;
        PP(IA + i, IA + i) += o->arw * o->arw * adt;
        PP(IBA + i, IBA + i) += (o->ba_tau > 0 ? 2.0 * o->ba_sigma * o->ba_sigma / o->ba_tau : o->ba_sigma * o->ba_sigma / 3600.0) * adt;
        PP(IBG + i, IBG + i) += (o->bg_tau > 0 ? 2.0 * o->bg_sigma * o->bg_sigma / o->bg_tau : o->bg_sigma * o->bg_sigma / 3600.0) * adt;
        PP(ISA + i, ISA + i) += o->sa_rw * o->sa_rw * adt;
        PP(ISG + i, ISG + i) += o->sg_rw * o->sg_rw * adt;
    }
}

/* ------------------------------------------------------------------------ */
/* measurement helpers                                                       */
/* ------------------------------------------------------------------------ */
/* chi-square style test; returns scale for R (1 ok, >1 inflate, <0 reject) */
static double innov_test(kf_t *kf, const double *H, const double *y, const double *R,
                         int m, double gate)
{
    int n = kf->n, i, j, k;
    double S[36], q = 0;
    if (m > 6) return 1.0;
    for (i = 0; i < m; i++) for (j = 0; j < m; j++) {
        double s = R[i*m + j];
        int a, b;
        for (a = 0; a < n; a++) {
            double ha = H[i*n + a];
            if (ha == 0) continue;
            for (b = 0; b < n; b++) if (H[j*n + b] != 0) s += ha * PP(a, b) * H[j*n + b];
        }
        S[i*m + j] = s;
    }
    if (matinv(S, m)) return -1;
    for (i = 0; i < m; i++) for (k = 0; k < m; k++) q += y[i] * S[i*m + k] * y[k];
    q /= m;
    if (q <= gate) return 1.0;
    if (q > 25.0 * gate) return -1.0;
    return q / gate;
}

/* LC: GNSS antenna position (and optionally velocity) */
static int update_lc(insstate_t *s, kf_t *kf, const insopt_t *o, const gsol_t *g,
                     double dtm, int *rejected, int *consec)
{
    int n = kf->n, i, j, m = 3;
    double H[6 * NMAX], y[6], R[36], ra[3], la[3], L[9], fl;
    double scale;
    memset(H, 0, sizeof(double) * 6 * n);
    memset(R, 0, sizeof(R));
    ins_lever_pos(s, o->lever_ant, ra);
    m3_vec(s->C, o->lever_ant, la);
    m3_skew(la, L);
    for (i = 0; i < 3; i++) {
        y[i] = g->r[i] - (ra[i] + s->v[i] * dtm);
        H[i*n + IR + i] = -1.0;
        H[i*n + IV + i] = -dtm;
        for (j = 0; j < 3; j++) H[i*n + IA + j] = -L[i*3 + j];
    }
    R[0] = g->Q[0]; R[4] = g->Q[1]; R[8] = g->Q[2];
    R[1] = R[3] = g->Q[3]; R[5] = R[7] = g->Q[4]; R[2] = R[6] = g->Q[5];
    fl = (g->q == 1) ? o->lc_min_sd[0] : o->lc_min_sd[1];
    if (g->q == 2) for (i = 0; i < 9; i++) R[i] *= o->lc_float_scale;
    if (g->q == 4) fl = fmax(fl, 0.5);
    if (g->q == 3 || g->q == 5) fl = fmax(fl, 2.0);
    for (i = 0; i < 3; i++) if (R[i*4] < fl*fl) R[i*4] = fl*fl;
    if (o->lc_use_vel && g->has_vel && (g->q == 1 || g->q == 2)) {
        /* velocity: v_ant ~= v (lever-arm rate term ignored, small) */
        m = 6;
        {
            double H6[6 * NMAX], R6[36];
            memset(H6, 0, sizeof(double) * 6 * n);
            memset(R6, 0, sizeof(R6));
            memcpy(H6, H, sizeof(double) * 3 * n);
            for (i = 0; i < 3; i++) for (j = 0; j < 3; j++) R6[i*6 + j] = R[i*3 + j];
            for (i = 0; i < 3; i++) {
                y[3 + i] = g->v[i] - s->v[i];
                H6[(3 + i)*n + IV + i] = -1.0;
                R6[(3 + i)*6 + 3 + i] = fmax(g->Qv[i], 0.05 * 0.05);
            }
            scale = innov_test(kf, H6, y, R6, 6, o->gate_chi2);
            if (scale < 0) { (*rejected)++; return 0; }
            for (i = 0; i < 36; i++) R6[i] *= scale;
            if (kf_update(kf, H6, y, R6, 6)) return 0;
            ins_feedback(s, kf);
            return 1;
        }
    }
    scale = innov_test(kf, H, y, R, m, o->gate_chi2);
    if (scale < 0 && g->q <= 2) {
        /* remember consecutive rejections; if good GNSS has disagreed with
         * the INS by a steady offset for 10 s, the INS has drifted more than
         * its covariance says: open the position/velocity covariance. A
         * steady offset (spread < 0.5 m) separates INS drift from bad GNSS,
         * whose error changes from epoch to epoch. */
        static double hist[400][3], t_first;
        int nh = *consec, ok = 1;
        hist[nh % 400][0] = y[0]; hist[nh % 400][1] = y[1]; hist[nh % 400][2] = y[2];
        if (nh == 0) t_first = g->t;
        (*consec)++;
        if (fabs(g->t - t_first) >= 10.0 && nh >= 20) {
            int a, b, nn = nh + 1;
            for (a = 0; a < 3 && ok; a++) {
                double mn = 1e9, mx = -1e9;
                for (b = nn > 40 ? nn - 40 : 0; b < nn; b++) { mn = fmin(mn, hist[b % 400][a]); mx = fmax(mx, hist[b % 400][a]); }
                if (mx - mn > 0.5) ok = 0;
            }
            if (ok) {
                for (i = 0; i < 3; i++) {
                    PP(IR + i, IR + i) += y[i] * y[i] + R[i*4];
                    PP(IV + i, IV + i) += 0.25;
                }
                pp_warn("GPS %.1f: INS realigned to GNSS after %.0f s of steady disagreement",
                        g->t, g->t - t_first);
                scale = 1.0;
            }
        }
    }
    if (scale < 0) { (*rejected)++; return 0; }
    *consec = 0;
    for (i = 0; i < 9; i++) R[i] *= scale;
    if (kf_update(kf, H, y, R, 3)) return 0;
    ins_feedback(s, kf);
    return 1;
}

/* non-holonomic: vehicle velocity has no lateral / vertical component.
 * The vehicle frame may sit a little off the IMU (aligned body) frame: a
 * mount at 43 deg configured as 45 deg. Forcing the body velocity to zero
 * would then bend the pitch (and the accelerometer bias with it), so the
 * pitch and yaw offset are estimated as two extra states:
 *   v_veh_y = v_b_y - yaw_off * v_b_x,   v_veh_z = v_b_z + pitch_off * v_b_x */
static void update_nhc(insstate_t *s, kf_t *kf, const insopt_t *o, const double *wb)
{
    int n, i, j, km[2] = {-1, -1};
    double H[2 * NMAX], y[2], R[4] = {0}, vb[3], wxl[3], Vx[9], CtVx[9], Lx[9];
    double mis[2];
    mis[0] = o->nhc_mis0[0];
    mis[1] = o->nhc_mis0[1];
    if (o->nhc_mis) {
        int code[2] = {NHC_MIS_P, NHC_MIS_Y};
        for (i = 0; i < 2; i++) {
            if ((km[i] = kf_find_amb(kf, 0, code[i])) < 0)
                km[i] = kf_add_amb(kf, 0, code[i], o->nhc_mis0[i], o->nhc_mis_sd * o->nhc_mis_sd, 0.0);
            if (km[i] >= 0) mis[i] = kf->amb[km[i]];
        }
    }
    n = kf->n;
    memset(H, 0, sizeof(double) * 2 * n);
    m3_Tvec(s->C, s->v, vb);
    cross3(wb, o->lever_nhc, wxl);
    m3_skew(s->v, Vx);
    m3_Tmul(s->C, Vx, CtVx);
    m3_skew(o->lever_nhc, Lx);
    for (i = 0; i < 2; i++) {
        int row = i + 1;        /* body y and z */
        /* vehicle row = body row + k * body x row */
        double kx = row == 1 ? -mis[1] : mis[0];
        y[i] = 0.0 - (vb[row] + wxl[row] + kx * (vb[0] + wxl[0]));
        for (j = 0; j < 3; j++) {
            H[i*n + IV + j] = -(s->C[j*3 + row] + kx * s->C[j*3 + 0]);
            H[i*n + IA + j] = CtVx[row*3 + j] + kx * CtVx[0*3 + j];
            H[i*n + IBG + j] = -(Lx[row*3 + j] + kx * Lx[0*3 + j]);
        }
        if (row == 1 && km[1] >= 0) H[i*n + NC + km[1]] = (vb[0] + wxl[0]);
        if (row == 2 && km[0] >= 0) H[i*n + NC + km[0]] = -(vb[0] + wxl[0]);
        R[i*2 + i] = o->nhc_sigma * o->nhc_sigma;
    }
    if (innov_test(kf, H, y, R, 2, 4.0 * o->gate_chi2) < 0) return;
    if (!kf_update(kf, H, y, R, 2)) ins_feedback(s, kf);
}

static void update_zupt(insstate_t *s, kf_t *kf, const insopt_t *o, const double *wmean)
{
    int n = kf->n, i;
    double H[4 * NMAX], y[4], R[16], wie[3] = {0, 0, PP_OMEGA_E}, wb[3];
    memset(H, 0, sizeof(double) * 4 * n);
    memset(R, 0, sizeof(R));
    m3_Tvec(s->C, wie, wb);
    for (i = 0; i < 3; i++) {
        y[i] = -s->v[i];
        H[i*n + IV + i] = -1.0;
        R[i*4 + i] = o->zupt_sigma * o->zupt_sigma;
    }
    /* zero angular rate on the vertical (body z) axis only: the horizontal
     * axes would make the filter gyrocompass with MEMS gyro biases that are
     * comparable to the earth rate, which pulls the heading */
    y[3] = wb[2] - wmean[2];
    H[3*n + IBG + 2] = 1.0;
    R[15] = (o->arw * o->arw) / 0.5 + 1e-10;      /* mean over a 0.5 s window */
    if (!kf_update(kf, H, y, R, 4)) ins_feedback(s, kf);
}

/* ------------------------------------------------------------------------ */
/* output                                                                    */
/* ------------------------------------------------------------------------ */
static void make_record(const insstate_t *s, kf_t *kf, const insopt_t *o,
                        const double *f, const double *w, trj_rec_t *rec)
{
    double ro[3], llh[3], Cne[9], Cbn[9], rpy[3], vn[3], A[9], T[9], B[9], P3[9];
    int i, j, k;
    memset(rec, 0, sizeof(*rec));
    ins_lever_pos(s, o->lever_out, ro);
    pp_ecef2llh(ro, llh);
    pp_Cne(llh[0], llh[1], Cne);
    m3_Tmul(Cne, s->C, Cbn);
    pp_C2euler(Cbn, rpy);
    m3_Tvec(Cne, s->v, vn);
    rec->t = s->t;
    rec->lat = llh[0]; rec->lon = llh[1]; rec->h = llh[2];
    for (i = 0; i < 3; i++) {
        rec->vel[i] = (float)vn[i];
        rec->att[i] = (float)rpy[i];
        rec->acc[i] = (float)f[i]; rec->gyr[i] = (float)w[i];
        rec->ba[i] = (float)s->ba[i]; rec->bg[i] = (float)s->bg[i];
    }
    if (rec->att[2] < 0) rec->att[2] += (float)(2 * PI);
    /* std in NED: position, velocity; attitude via heading rotation */
    for (k = 0; k < 3; k++) {
        int base = k == 0 ? IR : (k == 1 ? IV : IA);
        for (i = 0; i < 3; i++) for (j = 0; j < 3; j++) P3[i*3 + j] = PP(base + i, base + j);
        m3_Tmul(Cne, P3, A);       /* Cne' P */
        m3_mul(A, Cne, B);         /* Cne' P Cne */
        if (k == 2) {
            double ch = cos(rpy[2]), sh = sin(rpy[2]);
            T[0] = ch; T[1] = sh; T[2] = 0;
            T[3] = -sh; T[4] = ch; T[5] = 0;
            T[6] = 0; T[7] = 0; T[8] = 1;
            m3_mul(T, B, A);
            m3_mulT(A, T, B);
        }
        for (i = 0; i < 3; i++) {
            float sd = (float)sqrt(fmax(B[i*4], 0));
            if (k == 0) rec->sdp[i] = sd; else if (k == 1) rec->sdv[i] = sd; else rec->sda[i] = sd;
        }
    }
    (void)o;
}

/* ------------------------------------------------------------------------ */
/* options                                                                   */
/* ------------------------------------------------------------------------ */
static void read_opts(const cfg_t *c, insopt_t *o)
{
    double v[3];
    memset(o, 0, sizeof(*o));
    o->arw = cfg_num(c, "imu.arw_deg_sqrth", 0.15) * D2R / 60.0;
    o->vrw = cfg_num(c, "imu.vrw_m_s_sqrth", 0.03) / 60.0;
    o->bg_sigma = cfg_num(c, "imu.gyro_bias_deg_h", 0.5) * D2R / 3600.0;
    o->bg_tau = cfg_num(c, "imu.gyro_bias_tau_s", 3600);
    o->ba_sigma = cfg_num(c, "imu.acc_bias_ug", 30) * 9.80665e-6;
    o->ba_tau = cfg_num(c, "imu.acc_bias_tau_s", 3600);
    o->bg0 = cfg_num(c, "imu.gyro_bias0_deg_h", 3.0) * D2R / 3600.0;
    o->ba0 = cfg_num(c, "imu.acc_bias0_mg", 0.5) * 9.80665e-3;
    o->sg0 = cfg_num(c, "imu.gyro_scale0_ppm", 300) * 1e-6;
    o->sa0 = cfg_num(c, "imu.acc_scale0_ppm", 300) * 1e-6;
    o->sg_rw = cfg_num(c, "imu.scale_rw_ppm_sqrth", 10) * 1e-6 / 60.0;
    o->sa_rw = o->sg_rw;
    o->pos0 = cfg_num(c, "init.pos_sd_m", 1.0);
    o->vel0 = cfg_num(c, "init.vel_sd_m_s", 0.3);
    o->rp0 = cfg_num(c, "init.rollpitch_sd_deg", 1.0) * D2R;
    o->hdg0 = cfg_num(c, "init.heading_sd_deg", 3.0) * D2R;
    if (cfg_vec(c, "lever.antenna", v, 3) == 3) memcpy(o->lever_ant, v, sizeof(v));
    if (cfg_vec(c, "lever.antenna2", v, 3) == 3) memcpy(o->lever_ant2, v, sizeof(v));
    if (cfg_vec(c, "lever.output", v, 3) == 3) memcpy(o->lever_out, v, sizeof(v));
    if (cfg_vec(c, "lever.nhc", v, 3) == 3) memcpy(o->lever_nhc, v, sizeof(v));
    o->time_offset = cfg_num(c, "imu.time_offset_s", 0.0);
    o->nhc = cfg_int(c, "nhc.enable", 1);
    o->nhc_sigma = cfg_num(c, "nhc.sigma_m_s", 0.1);
    o->nhc_rate = cfg_num(c, "nhc.rate_hz", 10);
    o->nhc_mis = cfg_int(c, "nhc.estimate_mount", 1);
    o->nhc_mis_sd = cfg_num(c, "nhc.mount_sd_deg", 3.0) * D2R;
    {
        double v[2] = {0, 0};
        cfg_vec(c, "nhc.mount_deg", v, 2);       /* pitch, yaw of the vehicle against the IMU */
        o->nhc_mis0[0] = v[0] * D2R;
        o->nhc_mis0[1] = v[1] * D2R;
    }
    o->zupt = cfg_int(c, "zupt.enable", 1);
    o->zupt_sigma = cfg_num(c, "zupt.sigma_m_s", 0.01);
    o->zupt_gyro = cfg_num(c, "zupt.gyro_std_deg_s", 0.3) * D2R;
    o->zupt_acc = cfg_num(c, "zupt.acc_std_m_s2", 0.15);
    o->zupt_speed = cfg_num(c, "zupt.max_speed_m_s", 0.3);
    o->lc_use_float = cfg_int(c, "lc.use_float", 1);
    o->lc_use_dgps = cfg_int(c, "lc.use_dgps", 1);
    o->lc_use_single = cfg_int(c, "lc.use_single", 0);
    o->lc_use_vel = cfg_int(c, "lc.use_velocity", 0);
    o->lc_min_sd[0] = cfg_num(c, "lc.min_sd_fixed_m", 0.015);
    o->lc_min_sd[1] = cfg_num(c, "lc.min_sd_float_m", 0.05);
    o->lc_float_scale = cfg_num(c, "lc.float_cov_scale", 1.0);
    o->gate_chi2 = cfg_num(c, "gate.chi2", 9.0);
    o->align_mode = cfg_int(c, "align.mode", 0);
    o->align_speed = cfg_num(c, "align.speed_m_s", 4.0);
    o->out_decim = cfg_int(c, "output.decimation", 1);
    if (o->out_decim < 1) o->out_decim = 1;
    o->t_start = cfg_num(c, "time.start", 0);
    o->gap_short_s = cfg_num(c, "imu.gap_short_s", 0.02);
    o->gap_acc = cfg_num(c, "imu.gap_acc_m_s2", 0.8);
    o->gap_rate = cfg_num(c, "imu.gap_rate_deg_s", 3.0) * D2R;
    {
        /* gnss.exclude = t0-t1;t0-t1;...  (GPS seconds) */
        const char *e = cfg_str(c, "gnss.exclude", "");
        o->nexcl = 0;
        while (e && *e && o->nexcl < 256) {
            char *end;
            double a = strtod(e, &end), b;
            if (end == e) break;
            e = end;
            if (*e != '-') break;
            b = strtod(e + 1, &end);
            if (end == e + 1) break;
            e = end;
            if (b > a) { o->excl[o->nexcl][0] = a; o->excl[o->nexcl][1] = b; o->nexcl++; }
            while (*e == ';' || *e == ',' || *e == ' ') e++;
        }
        if (o->nexcl) pp_info("GNSS excluded in %d window(s) (Trajectory Review)", o->nexcl);
    }
    o->t_end = cfg_num(c, "time.end", 0);
}

/* ------------------------------------------------------------------------ */
/* stationary detector (1 s window)                                          */
/* ------------------------------------------------------------------------ */
#define SWIN 256
typedef struct {
    double g[SWIN], a[SWIN], wv[SWIN][3];
    int n, i;
} still_t;

static void still_add(still_t *d, const double *f, const double *w)
{
    d->g[d->i] = norm3(w);
    d->a[d->i] = norm3(f);
    memcpy(d->wv[d->i], w, sizeof(double) * 3);
    d->i = (d->i + 1) % SWIN;
    if (d->n < SWIN) d->n++;
}

static int still_test(const still_t *d, const insopt_t *o, int need, double *wmean)
{
    double sg = 0, sg2 = 0, sa = 0, sa2 = 0, vg, va;
    int k, i;
    if (d->n < need) return 0;
    wmean[0] = wmean[1] = wmean[2] = 0;
    for (k = 0; k < need; k++) {
        int j = (d->i - 1 - k + SWIN) % SWIN;
        sg += d->g[j]; sg2 += d->g[j] * d->g[j];
        sa += d->a[j]; sa2 += d->a[j] * d->a[j];
        for (i = 0; i < 3; i++) wmean[i] += d->wv[j][i] / need;
    }
    vg = sg2 / need - (sg / need) * (sg / need);
    va = sa2 / need - (sa / need) * (sa / need);
    (void)vg; (void)va; (void)o;
    return 1;
}

/* stationary flags from a centred window (look-ahead is allowed in
 * post-processing, which avoids applying zero-velocity updates just after the
 * vehicle starts moving) */
static uint8_t *compute_still(imureader_t *ir, const insopt_t *o)
{
    long long N = (long long)ir->h.count, k, i;
    int W = (int)(ir->h.rate_hz > 0 ? ir->h.rate_hz : 200);
    int half = W / 2;
    uint8_t *fl = calloc((size_t)N, 1);
    double *g, *ax, *ay, *az;
    double sg = 0, sg2 = 0, sx = 0, sx2 = 0, sy = 0, sy2 = 0, sz = 0, sz2 = 0;
    if (!fl) return NULL;
    g = malloc(sizeof(double) * N); ax = malloc(sizeof(double) * N);
    ay = malloc(sizeof(double) * N); az = malloc(sizeof(double) * N);
    if (!g || !ax || !ay || !az) { free(g); free(ax); free(ay); free(az); return fl; }
    for (k = 0; k < N; k++) {
        const imu_rec_t *r = imu_get(ir, k);
        if (!r) { g[k] = ax[k] = ay[k] = az[k] = 0; continue; }
        g[k] = sqrt((double)r->gyr[0]*r->gyr[0] + (double)r->gyr[1]*r->gyr[1] + (double)r->gyr[2]*r->gyr[2]);
        ax[k] = r->acc[0]; ay[k] = r->acc[1]; az[k] = r->acc[2];
    }
    for (k = 0; k < N; k++) {
        long long a = k - half, b = k + half, n;
        if (a < 0 || b >= N) continue;
        if (k == half) {
            sg = sg2 = sx = sx2 = sy = sy2 = sz = sz2 = 0;
            for (i = a; i <= b; i++) {
                sg += g[i]; sg2 += g[i]*g[i]; sx += ax[i]; sx2 += ax[i]*ax[i];
                sy += ay[i]; sy2 += ay[i]*ay[i]; sz += az[i]; sz2 += az[i]*az[i];
            }
        } else {
            long long o0 = a - 1;
            sg += g[b] - g[o0]; sg2 += g[b]*g[b] - g[o0]*g[o0];
            sx += ax[b] - ax[o0]; sx2 += ax[b]*ax[b] - ax[o0]*ax[o0];
            sy += ay[b] - ay[o0]; sy2 += ay[b]*ay[b] - ay[o0]*ay[o0];
            sz += az[b] - az[o0]; sz2 += az[b]*az[b] - az[o0]*az[o0];
        }
        n = b - a + 1;
        {
            double vg = sg2/n - (sg/n)*(sg/n), vx = sx2/n - (sx/n)*(sx/n);
            double vy = sy2/n - (sy/n)*(sy/n), vz = sz2/n - (sz/n)*(sz/n);
            double sa = sqrt(fmax(fmax(vx, vy), fmax(vz, 0)));
            if (sqrt(fmax(vg, 0)) < o->zupt_gyro && sa < o->zupt_acc) fl[k] = 1;
        }
    }
    free(g); free(ax); free(ay); free(az);
    return fl;
}

/* ------------------------------------------------------------------------ */
/* main pass                                                                 */
/* ------------------------------------------------------------------------ */
static int status_from_q(int q)
{
    switch (q) {
        case 1: return PPST_FIX; case 2: return PPST_FLOAT; case 6: return PPST_PPP;
        case 4: return PPST_DGPS; case 3: case 5: return PPST_SPP;
    }
    return PPST_NONE;
}

static int gsol_cmp(const void *a, const void *b)
{
    double x = ((const gsol_t *)a)->t, y = ((const gsol_t *)b)->t;
    return x < y ? -1 : x > y;
}

int cmd_ins(int argc, char **argv)
{
    cfg_t cfg;
    insopt_t o;
    imureader_t ir;
    rtbuf_t rt = {0};
    gsolbuf_t gs = {0};
    tcctx_t *tc = NULL;
    kf_t kf;
    insstate_t s;
    still_t still;
    uint8_t *stillfl = NULL;
    int lc_consec = 0;
    FILE *fo;
    trj_hdr_t th;
    const char *conf = NULL, *imupath, *rtpath, *outpath;
    int dir = 1, mode, i, j;
    long long N, k0 = 0, kend, k, step;
    double t0, t1, tlast_gnss = -1e9, tlast_nhc = -1e9, tlast_zupt = -1e9, tlast_cov;
    double facc[3] = {0}, wacc[3] = {0}, dtacc = 0;
    int nacc = 0, last_status = PPST_NONE, last_ns = 0;
    double last_ratio = 0;
    long gi = 0, ngnss = 0, nused = 0, nzupt = 0, nout = 0, nfix = 0, n_short_gaps = 0;
    int nrej = 0;
    char err[512];

    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "-k") && i + 1 < argc) conf = argv[++i];
        else if (!strcmp(argv[i], "--backward")) dir = -1;
    }
    if (!conf || !cfg_load(&cfg, conf)) return pp_fail("cannot read the job file");
    read_opts(&cfg, &o);
    if (cfg_int(&cfg, "direction", dir) < 0) dir = -1;
    mode = !strcmp(cfg_str(&cfg, "mode", "lc"), "tc") ? 2 : 1;
    imupath = cfg_str(&cfg, "file.imu", "");
    rtpath = cfg_str(&cfg, "file.rt", "");
    outpath = cfg_str(&cfg, "file.out", "");
    if (!imu_open(&ir, imupath, o.time_offset)) return pp_fail("cannot read IMU file %s", imupath);
    N = (long long)ir.h.count;
    if (N < 1000) return pp_fail("too few IMU samples (%lld)", N);
    if (*rtpath) rt_load(&rt, rtpath);

    if (mode == 1) {
        const char *pf = cfg_str(&cfg, dir > 0 ? "file.gnss_fwd" : "file.gnss_bwd", "");
        if (!*pf) pf = cfg_str(&cfg, "file.gnss", "");
        if (!read_pos_file(pf, &gs) || gs.n == 0) return pp_fail("no GNSS solutions in %s", pf);
        qsort(gs.s, gs.n, sizeof(gsol_t), gsol_cmp);
        /* drop solution types not wanted */
        for (i = 0, j = 0; i < gs.n; i++) {
            int q = gs.s[i].q;
            if (q == 1 || q == 6 || (q == 2 && o.lc_use_float) || (q == 4 && o.lc_use_dgps) ||
                ((q == 3 || q == 5) && o.lc_use_single))
                gs.s[j++] = gs.s[i];
        }
        gs.n = j;
        if (!gs.n) return pp_fail("no usable GNSS solutions (check the solution types allowed)");
        ngnss = gs.n;
    } else {
        /* the GNSS-only solution (when there is one) seeds the alignment: a
         * fixed RTK position starts the filter at the centimetre level
         * instead of at a 5 m single-point fix, which otherwise has to
         * converge from a standstill and can settle on wrong integers */
        /* Both GNSS-only directions are used: at the end of a run the
         * backward GNSS solution is still converging while the forward one
         * has long been fixed (and the reverse at the start). */
        if (cfg_int(&cfg, "tc.init_from_rtk", 1)) {
            const char *pf[2] = {cfg_str(&cfg, "file.gnss_fwd", ""), cfg_str(&cfg, "file.gnss_bwd", "")};
            int d;
            gs.n = 0;
            for (d = 0; d < 2; d++) {
                gsolbuf_t g2;
                if (!*pf[d] || !read_pos_file(pf[d], &g2)) continue;
                if (gs.n + g2.n > 0) {
                    gsol_t *ns_ = realloc(gs.s, sizeof(gsol_t) * (size_t)(gs.n + g2.n));
                    if (ns_) {
                        gs.s = ns_; gs.nmax = gs.n + g2.n;
                        for (i = 0; i < g2.n; i++) if (g2.s[i].q == 1) gs.s[gs.n++] = g2.s[i];
                    }
                }
                free_gsol(&g2);
            }
            if (gs.n) qsort(gs.s, gs.n, sizeof(gsol_t), gsol_cmp);
        }
        tc = tc_open(&cfg, err, sizeof(err));
        if (!tc) return pp_fail("%s", err);
        ngnss = tc_count(tc);
    }

    /* time span */
    {
        const imu_rec_t *a = imu_get(&ir, 0);
        if (!a) return pp_fail("cannot read the IMU file");
        t0 = a->t;
        a = imu_get(&ir, N - 1);
        if (!a) return pp_fail("the IMU file is truncated");
        t1 = a->t;
        if (o.t_start > 0 && o.t_start > t0) t0 = o.t_start;
        if (o.t_end > 0 && o.t_end < t1) t1 = o.t_end;
        if (t1 - t0 < 10) return pp_fail("the selected time span is too short");
    }

    /* ---- initial alignment ---- */
    {
        double r0[3] = {0}, v0[3] = {0}, rpy[3] = {0}, llh[3], Cne[9], Cbn[9];
        double tinit = 0, pos_sd = o.pos0, hdg_sd = o.hdg0, rp_sd = o.rp0;
        int have_pos = 0, have_att = 0, have_hdg = 0, pos_is_ant = 0;
        long long ki;
        /* scan from the start (fwd) or end (bwd) for a usable instant */
        long long kscan0 = dir > 0 ? imu_search(&ir, t0) : imu_search(&ir, t1);
        if (dir < 0 && kscan0 > 0) {
            const imu_rec_t *r = imu_get(&ir, kscan0);
            if (r && r->t > t1) kscan0--;
        }
        for (ki = kscan0; ki >= 0 && ki < N; ki += dir * 20) {
            const imu_rec_t *r = imu_get(&ir, ki);
            const rt_rec_t *q;
            double tt;
            if (!r) break;
            tt = r->t;
            if (tt < t0 - 1e-6 || tt > t1 + 1e-6) break;
            have_pos = have_att = have_hdg = 0;
            /* position / velocity */
            q = rt_near(&rt, tt, 0.2);
            if (mode == 1) {
                /* nearest GNSS solution within 1 s */
                long lo = 0, hi = gs.n - 1;
                while (lo < hi) { long mid = (lo + hi) / 2; if (gs.s[mid].t < tt) lo = mid + 1; else hi = mid; }
                if (fabs(gs.s[lo].t - tt) < 1.0) {
                    memcpy(r0, gs.s[lo].r, sizeof(r0));
                    pos_sd = fmax(sqrt(gs.s[lo].Q[0] + gs.s[lo].Q[1] + gs.s[lo].Q[2]), 0.05) + 0.1;
                    have_pos = 1; pos_is_ant = 1;
                    if (gs.s[lo].has_vel) { memcpy(v0, gs.s[lo].v, sizeof(v0)); }
                }
            } else {
                double rr[3], vv[3], dtr;
                int e;
                for (e = 0; e < tc_count(tc); e++) if (tc_rx(tc, e) == 0 && fabs(tc_time(tc, e) - tt) < 0.5) break;
                if (e < tc_count(tc) && tc_spp(tc, e, rr, vv, &dtr)) {
                    memcpy(r0, rr, sizeof(r0)); memcpy(v0, vv, sizeof(v0));
                    pos_sd = 5.0; have_pos = 1; pos_is_ant = 1;
                    if (gs.n > 0) {
                        long lo = 0, hi = gs.n - 1;
                        while (lo < hi) { long mid = (lo + hi) / 2; if (gs.s[mid].t < tt) lo = mid + 1; else hi = mid; }
                        if (lo > 0 && fabs(gs.s[lo - 1].t - tt) < fabs(gs.s[lo].t - tt)) lo--;
                        if (fabs(gs.s[lo].t - tt) < 0.15) {
                            /* if both directions are fixed here they must agree */
                            long b2;
                            int a, ok = 1;
                            for (b2 = lo > 3 ? lo - 3 : 0; b2 < gs.n && b2 <= lo + 3; b2++) {
                                double d3 = 0;
                                if (fabs(gs.s[b2].t - gs.s[lo].t) > 0.15) continue;
                                for (a = 0; a < 3; a++) d3 += (gs.s[b2].r[a] - gs.s[lo].r[a] - gs.s[b2].v[a] * (gs.s[b2].t - gs.s[lo].t)) *
                                                              (gs.s[b2].r[a] - gs.s[lo].r[a] - gs.s[b2].v[a] * (gs.s[b2].t - gs.s[lo].t));
                                if (sqrt(d3) > 0.10 + 1.5 * fabs(gs.s[b2].t - gs.s[lo].t)) ok = 0;
                            }
                            if (ok) {
                                memcpy(r0, gs.s[lo].r, sizeof(r0));
                                for (a = 0; a < 3; a++) r0[a] += vv[a] * (tt - gs.s[lo].t);
                                pos_sd = fmax(sqrt(gs.s[lo].Q[0] + gs.s[lo].Q[1] + gs.s[lo].Q[2]), 0.02) + 0.03;
                                pp_info("alignment position from the fixed GNSS-only solution (%.2f m)", pos_sd);
                            }
                        }
                    }
                }
            }
            if (q && (q->filt_status & 0x02)) {
                double l3[3] = {q->lat, q->lon, q->h};
                double vn[3] = {q->vel[0], q->vel[1], q->vel[2]};
                pp_Cne(q->lat, q->lon, Cne);
                if (!have_pos) {
                    pp_llh2ecef(l3, r0);
                    pos_sd = fmax(fmax(q->sd[0], q->sd[1]), q->sd[2]) + 0.5;
                    have_pos = 1; pos_is_ant = 0;
                }
                m3_vec(Cne, vn, v0);
                if ((q->filt_status & 0x01) && o.align_mode != 2) {
                    rpy[0] = q->att[0]; rpy[1] = q->att[1]; have_att = 1;
                    if ((q->filt_status & 0x04)) {
                        rpy[2] = q->att[2]; have_hdg = 1;
                        /* dual-antenna heading active: the Certus heading is good */
                        if (q->filt_status & 0x0400) hdg_sd = fmin(hdg_sd, cfg_num(&cfg, "init.heading_sd_dual_deg", 0.5) * D2R);
                    }
                }
            }
            if (getenv("PP_DEBUG")) fprintf(stderr, "scan %lld t=%.3f pos=%d att=%d hdg=%d q=%p\n", ki, tt, have_pos, have_att, have_hdg, (void *)q);
            if (!have_pos) continue;
            /* coarse levelling from accelerometers (1 s) if needed */
            if (!have_att) {
                double fm[3] = {0};
                int c = 0;
                long long kk;
                for (kk = ki; kk >= 0 && kk < N && c < 200; kk += dir) {
                    const imu_rec_t *z = imu_get(&ir, kk);
                    if (!z) break;
                    fm[0] += z->acc[0]; fm[1] += z->acc[1]; fm[2] += z->acc[2]; c++;
                }
                if (c < 50) continue;
                fm[0] /= c; fm[1] /= c; fm[2] /= c;
                rpy[0] = atan2(-fm[1], -fm[2]);
                rpy[1] = atan2(fm[0], sqrt(fm[1]*fm[1] + fm[2]*fm[2]));
                rp_sd = fmax(o.rp0, 2.0 * D2R);
                have_att = 1;
            }
            if (!have_hdg) {
                double vn[3], sp;
                pp_ecef2llh(r0, llh);
                pp_Cne(llh[0], llh[1], Cne);
                m3_Tvec(Cne, v0, vn);
                sp = sqrt(vn[0]*vn[0] + vn[1]*vn[1]);
                if (sp < o.align_speed) continue;
                rpy[2] = atan2(vn[1], vn[0]);   /* assumes forward travel */
                hdg_sd = fmax(o.hdg0, 5.0 * D2R);
                have_hdg = 1;
            }
            tinit = tt;
            k0 = ki;
            break;
        }
        if (!have_pos || !have_att || !have_hdg || tinit == 0)
            return pp_fail("could not align: no instant with position, levelling and heading "
                           "(drive above %.0f m/s or record packet 20)", o.align_speed);
        memset(&s, 0, sizeof(s));
        s.t = tinit;
        pp_ecef2llh(r0, llh);
        pp_Cne(llh[0], llh[1], Cne);
        pp_euler2C(rpy[0], rpy[1], rpy[2], Cbn);
        m3_mul(Cne, Cbn, s.C);
        /* a GNSS position is the antenna: move to the IMU (packet 20 is not) */
        if (pos_is_ant) {
            double la[3];
            m3_vec(s.C, o.lever_ant, la);
            for (i = 0; i < 3; i++) r0[i] -= la[i];
        }
        memcpy(s.r, r0, sizeof(r0));
        memcpy(s.v, v0, sizeof(v0));
        kf_init(&kf);
        for (i = 0; i < 3; i++) {
            PK(IR + i, IR + i) = pos_sd * pos_sd;
            PK(IV + i, IV + i) = o.vel0 * o.vel0;
            PK(IBA + i, IBA + i) = o.ba0 * o.ba0;
            PK(IBG + i, IBG + i) = o.bg0 * o.bg0;
            PK(ISA + i, ISA + i) = o.sa0 * o.sa0;
            PK(ISG + i, ISG + i) = o.sg0 * o.sg0;
        }
        {
            double D[9] = {rp_sd*rp_sd, 0, 0, 0, rp_sd*rp_sd, 0, 0, 0, hdg_sd*hdg_sd}, A[9], B[9];
            m3_mul(Cne, D, A);
            m3_mulT(A, Cne, B);
            for (i = 0; i < 3; i++) for (j = 0; j < 3; j++) PK(IA + i, IA + j) = B[i*3 + j];
        }
        pp_info("%s pass aligned at GPS %.3f: roll %.2f pitch %.2f heading %.2f deg",
                dir > 0 ? "forward" : "backward", tinit, rpy[0]*R2D, rpy[1]*R2D, rpy[2]*R2D);
    }

    kend = dir > 0 ? imu_search(&ir, t1) : imu_search(&ir, t0);
    if (dir > 0 && kend < N && imu_get(&ir, kend) && imu_get(&ir, kend)->t > t1) kend--;
    if (kend >= N) kend = N - 1;

    /* first GNSS measurement in the right direction */
    if (mode == 1) {
        if (dir > 0) { for (gi = 0; gi < gs.n && gs.s[gi].t <= s.t + 1e-6; gi++); }
        else { for (gi = gs.n - 1; gi >= 0 && gs.s[gi].t >= s.t - 1e-6; gi--); }
    } else {
        int ne = tc_count(tc);
        if (dir > 0) { for (gi = 0; gi < ne && tc_time(tc, gi) <= s.t + 1e-6; gi++); }
        else { for (gi = ne - 1; gi >= 0 && tc_time(tc, gi) >= s.t - 1e-6; gi--); }
    }

    if (!(fo = fopen(outpath, "wb"))) return pp_fail("cannot write %s", outpath);
    memset(&th, 0, sizeof(th));
    memcpy(th.magic, TRJ_MAGIC, 8);
    memcpy(th.lever_out, o.lever_out, sizeof(th.lever_out));
    th.mode = mode; th.direction = dir;
    fwrite(&th, sizeof(th), 1, fo);

    memset(&still, 0, sizeof(still));
    stillfl = o.zupt ? compute_still(&ir, &o) : NULL;
    tlast_cov = s.t;
    step = dir;
    {
        double span = fabs((double)(kend - k0));
        double fprev[3] = {0}, wprev[3] = {0};
        double lp_f[3] = {0}, lp_w[3] = {0};
        int have_lp = 0;
        for (k = k0 + step; dir > 0 ? k <= kend : k >= kend; k += step) {
            const imu_rec_t *a, *b;
            imu_rec_t ra, rb;        /* copies: the block cache may move */
            double f[3], w[3], dt;
            int did_update = 0;
            if (!(a = imu_get(&ir, k - step))) break;
            ra = *a;
            if (!(b = imu_get(&ir, k))) break;
            rb = *b;
            dt = rb.t - ra.t;
            if (dt * dir <= 0) {
                s.t = rb.t;         /* repeated or reversed time stamp: skip */
                continue;
            }
            if (fabs(dt) > 5.0) {
                /* IMU gap: coast at constant velocity and open the covariance
                 * as a free-running INS over that time would */
                double zero[3] = {0, 0, 0};
                for (i = 0; i < 3; i++) s.r[i] += s.v[i] * dt;
                propagate_cov(&kf, &s, &o, zero, zero, dt);
                for (i = 0; i < 3; i++) {
                    PK(IV + i, IV + i) += 0.04 * fabs(dt);
                    PK(IR + i, IR + i) += 0.01 * dt * dt;
                }
                pp_warn("IMU data gap of %.1f s at GPS %.1f", fabs(dt), ra.t);
                s.t = rb.t;
                tlast_cov = s.t;
                continue;
            }
            if (fabs(dt) > o.gap_short_s && have_lp) {
                /* short IMU gap (lost packets): integrating the two samples
                 * around it over the whole gap turns their vibration into
                 * velocity kicks. Coast on the low-passed motion of the last
                 * 0.1 s in 5 ms steps and open the covariance for what the
                 * vehicle may have done meanwhile. */
                double fl[3], wl[3], rem = fabs(dt), h;
                for (i = 0; i < 3; i++) {
                    fl[i] = (lp_f[i] - s.ba[i]) / (1.0 + s.sa[i]);
                    wl[i] = (lp_w[i] - s.bg[i]) / (1.0 + s.sg[i]);
                }
                while (rem > 1e-9) {
                    h = rem > 0.005 ? 0.005 : rem;
                    mech(&s, fl, wl, h * dir);
                    rem -= h;
                }
                for (i = 0; i < 3; i++) {
                    double ga = o.gap_acc * fabs(dt), gw = o.gap_rate * fabs(dt);
                    PK(IV + i, IV + i) += ga * ga;
                    PK(IR + i, IR + i) += 0.25 * ga * ga * dt * dt;
                    PK(IA + i, IA + i) += gw * gw;
                    f[i] = fl[i]; w[i] = wl[i];
                }
                n_short_gaps++;
            } else {
                compensate(&s, &ra, &rb, f, w);
                mech(&s, f, w, dt);
            }
            for (i = 0; i < 3; i++) {
                double al = fabs(dt) > o.gap_short_s ? 0.0 : fmin(1.0, fabs(dt) / 0.1);
                if (!have_lp) { lp_f[i] = rb.acc[i]; lp_w[i] = rb.gyr[i]; }
                else if (fabs(dt) <= o.gap_short_s) {
                    lp_f[i] += al * (rb.acc[i] - lp_f[i]);
                    lp_w[i] += al * (rb.gyr[i] - lp_w[i]);
                }
            }
            have_lp = 1;
            s.t = rb.t;     /* avoid drift of the accumulated time */
            for (i = 0; i < 3; i++) { facc[i] += f[i]; wacc[i] += w[i]; }
            dtacc += dt; nacc++;
            {
                double fb[3] = {rb.acc[0] - s.ba[0], rb.acc[1] - s.ba[1], rb.acc[2] - s.ba[2]};
                double wbv[3] = {rb.gyr[0], rb.gyr[1], rb.gyr[2]};   /* raw: bias removed at use */
                still_add(&still, fb, wbv);
            }
            memcpy(fprev, f, sizeof(f)); memcpy(wprev, w, sizeof(w));

            /* is a GNSS measurement due inside this step? */
            {
                double tm = 1e300;
                int due = 0;
                if (mode == 1 && gi >= 0 && gi < gs.n) tm = gs.s[gi].t;
                if (mode == 2 && gi >= 0 && gi < tc_count(tc)) tm = tc_time(tc, gi);
                if (tm < 1e299) due = dir > 0 ? (tm <= s.t + 1e-9) : (tm >= s.t - 1e-9);
                if (due || fabs(s.t - tlast_cov) >= 0.05) {
                    double fm[3], wm[3];
                    for (i = 0; i < 3; i++) { fm[i] = facc[i] / nacc; wm[i] = wacc[i] / nacc; }
                    propagate_cov(&kf, &s, &o, fm, wm, dtacc);
                    memset(facc, 0, sizeof(facc)); memset(wacc, 0, sizeof(wacc));
                    dtacc = 0; nacc = 0; tlast_cov = s.t;
                }
                while (due) {
                    double dtm = tm - s.t;
                    int excluded = 0;
                    for (i = 0; i < o.nexcl; i++)
                        if (tm >= o.excl[i][0] && tm <= o.excl[i][1]) { excluded = 1; break; }
                    if (fabs(dtm) < 0.5 && !excluded) {
                        if (mode == 1) {
                            if (update_lc(&s, &kf, &o, &gs.s[gi], dtm, &nrej, &lc_consec)) {
                                nused++;
                                tlast_gnss = s.t;
                                last_status = status_from_q(gs.s[gi].q);
                                last_ns = gs.s[gi].ns;
                                last_ratio = gs.s[gi].ratio;
                                if (last_status == PPST_FIX) nfix++;
                                did_update = 1;
                            }
                        } else {
                            int st = PPST_NONE;
                            double ratio = 0;
                            int ns = tc_update(tc, gi, &s, &kf, &o, &st, &ratio, dir);
                            if (ns > 0) {
                                /* the secondary antenna reports only when it is better or the
                                 * primary has been silent, so the quality flag follows the primary */
                                int rx = tc_rx(tc, gi);
                                nused++;
                                if (rx == 0 || st == PPST_FIX || fabs(s.t - tlast_gnss) > 0.5) {
                                    last_status = st; last_ns = ns; last_ratio = ratio;
                                }
                                tlast_gnss = s.t;
                                if (st == PPST_FIX) nfix++;
                                did_update = 1;
                            }
                        }
                    }
                    gi += dir;
                    due = 0;
                    if (mode == 1 && gi >= 0 && gi < gs.n) { tm = gs.s[gi].t; due = dir > 0 ? (tm <= s.t + 1e-9) : (tm >= s.t - 1e-9); }
                    if (mode == 2 && gi >= 0 && gi < tc_count(tc)) { tm = tc_time(tc, gi); due = dir > 0 ? (tm <= s.t + 1e-9) : (tm >= s.t - 1e-9); }
                }
            }
            /* vehicle constraints */
            {
                double wmean[3];
                int isstill = o.zupt && stillfl && stillfl[k] && norm3(s.v) < o.zupt_speed &&
                              still_test(&still, &o, 100, wmean);
                if (isstill)
                    for (i = 0; i < 3; i++) wmean[i] = (wmean[i] - s.bg[i]) / (1.0 + s.sg[i]);
                if (isstill && fabs(s.t - tlast_zupt) >= 0.5) {
                    update_zupt(&s, &kf, &o, wmean);
                    tlast_zupt = s.t;
                    nzupt++;
                } else if (!isstill && o.nhc && fabs(s.t - tlast_nhc) >= 1.0 / o.nhc_rate) {
                    update_nhc(&s, &kf, &o, w);
                    tlast_nhc = s.t;
                }
            }
            (void)did_update;
            /* output */
            if (k % o.out_decim == 0) {
                trj_rec_t rec;
                double gap = fabs(s.t - tlast_gnss);
                make_record(&s, &kf, &o, f, w, &rec);
                rec.status = gap < 1.5 ? (uint8_t)last_status : PPST_INS;
                rec.nsat = (uint8_t)(last_ns > 255 ? 255 : last_ns);
                rec.ratio10 = (uint8_t)(last_ratio * 10 > 255 ? 255 : last_ratio * 10);
                rec.gap_s = (float)(gap > 1e6 ? 1e6 : gap);
                if (fabs(s.t - tlast_zupt) < 0.25) rec.flags |= 1;
                if (gap > 1.5) rec.flags |= 2;
                fwrite(&rec, sizeof(rec), 1, fo);
                nout++;
            }
            if ((k & 4095) == 0) {
                double fr = fabs((double)(k - k0)) / (span > 0 ? span : 1);
                pp_progress(fr, "%s %s %.0f%%", mode == 1 ? "loosely coupled" : "tightly coupled",
                            dir > 0 ? "forward" : "backward", fr * 100);
            }
            /* divergence guard */
            if (!isfinite(s.r[0]) || norm3(s.v) > 400.0) {
                fclose(fo);
                return pp_fail("the %s pass diverged at GPS %.3f (check lever arms, IMU "
                               "orientation and alignment)", dir > 0 ? "forward" : "backward", s.t);
            }
        }
    }
    th.count = (uint64_t)nout;
    fseek(fo, 0, SEEK_SET);
    fwrite(&th, sizeof(th), 1, fo);
    if (ferror(fo) | fclose(fo)) return pp_fail("could not write %s (disk full?)", outpath);
    pp_result("records", "%ld", nout);
    pp_result("gnss_epochs", "%ld", ngnss);
    pp_result("gnss_used", "%ld", nused);
    pp_result("gnss_rejected", "%d", nrej);
    pp_result("gnss_fixed", "%ld", nfix);
    pp_result("zupt_updates", "%ld", nzupt);
    pp_result("imu_short_gaps", "%ld", n_short_gaps);
    pp_result("filter_states_refused", "%ld", n_amb_full);
    {
        int kp = kf_find_amb(&kf, 0, NHC_MIS_P), ky = kf_find_amb(&kf, 0, NHC_MIS_Y);
        if (kp >= 0 && ky >= 0)
            pp_result("vehicle_mount_offset_deg", "%.3f,%.3f,%.3f,%.3f", kf.amb[kp]*R2D, kf.amb[ky]*R2D,
                      sqrt(PP_((&kf), NC + kp, NC + kp))*R2D, sqrt(PP_((&kf), NC + ky, NC + ky))*R2D);
    }
    pp_result("final_acc_bias_mg", "%.4f,%.4f,%.4f", s.ba[0]/9.80665e-3, s.ba[1]/9.80665e-3, s.ba[2]/9.80665e-3);
    pp_result("final_gyro_bias_deg_h", "%.3f,%.3f,%.3f", s.bg[0]*R2D*3600, s.bg[1]*R2D*3600, s.bg[2]*R2D*3600);
    pp_progress(1.0, "done");
    imu_close(&ir);
    free(stillfl);
    free(rt.r);
    free_gsol(&gs);
    if (tc) tc_close(tc);
    kf_free(&kf);
    cfg_free(&cfg);
    return 0;
}
