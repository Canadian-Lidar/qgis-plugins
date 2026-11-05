/* PosPro engine: tightly coupled GNSS update (double differences).
 *
 * At every rover epoch the double-differenced carrier phase and pseudorange
 * between rover and base are used directly in the INS filter. Base data are
 * interpolated to the rover epoch through their residuals (observation minus
 * computed range), which works for any base interval. Ambiguities are kept
 * as single-difference states; integer fixing uses LAMBDA (RTKLIB) with a
 * ratio test and partial fixing, and validated fixes are applied to the
 * filter as constraints.
 *
 * Cycle slips: loss-of-lock flags, geometry-free jumps and an INS-aided test
 * (change of the phase residual between epochs, with the common clock change
 * removed).
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "pp_ins.h"

#define MAXF 2              /* frequencies used: L1/E1/B1 and L2/E5b/B2 */
#define BCACHE 4

typedef struct {
    int j;                  /* base epoch index, -1 empty */
    int n;
    int sat[MAXOBS];
    double zphi[MAXOBS][MAXF], zcode[MAXOBS][MAXF];
    int arc[MAXOBS][MAXF];
    int lli[MAXOBS][MAXF];
} bzd_t;

typedef struct {            /* per satellite/frequency slip state */
    double t_last;
    double q_last;          /* SD phase residual (m) at last epoch */
    double gf_last;
    double gf_t;            /* time of gf_last (kept across epochs with a missing frequency) */
    int base_arc;
    int valid;
} slip_t;

#define MAXRX 2
#define ATM_IONO 40    /* ambiguity-table "frequency" codes for atmosphere states */
#define ATM_TROP 41
#define LEV_X 42       /* lever-arm states: LEV_X + axis, keyed by receiver */

struct tcctx_s {
    obs_t robs[MAXRX], bobs;
    int nrx;
    int *ev_rx;             /* receiver of each rover event */
    pcv_t bpcv;             /* base antenna calibration */
    int have_bpcv;
    double bdel[3];         /* base antenna delta E, N, U (m) */
    nav_t nav;
    prcopt_t popt;
    double rb[3], rbllh[3];
    /* rover epochs */
    int ne;
    int *ei, *en;           /* index, count into robs.data */
    double *et;             /* true (clock-corrected) epoch time, GPS s */
    double *rt;             /* receiver time, GPS s */
    double (*spp)[7];       /* SPP x,y,z,vx,vy,vz,ok */
    /* base epochs */
    int nb;
    int *bi, *bn;
    double *bt;
    int *barc;              /* arc id per base obs record x MAXF */
    bzd_t cache[BCACHE];
    int cache_next;
    /* options */
    double elmin, elmin_ar, err_a, err_b, eratio, slip_thr, gf_thr, max_base_dt;
    double ar_ratio, hold_var, amb_var0;
    int ar_mode, glo_ar, min_fix_sats, min_lock, maxout, nf;
    /* state */
    slip_t slip[MAXRX][MAXSAT][MAXF];
    double last_t[MAXRX];
    long n_slips, n_out_code, n_out_phase, n_fix, n_float, n_reset, n_common;
    int fail_count, fix_streak; long n_release;
    /* fix validation: running mean of the post-fit code residuals expressed
     * as a position offset. A held fix that sits away from what the code
     * says, steadily, is a wrong fix. */
    double cb[3], cb_var[3], cb_t, cb_age, cb_tau, cb_thr;
    long n_cbreset;
    /* atmosphere: relative (rover - base) slant ionosphere per satellite and
     * zenith wet troposphere, estimated when the baseline is long */
    int iono_mode, trop_mode, iono_on, trop_on;   /* mode 0 off, 1 on, 2 auto */
    double iono_bl, trop_bl, trop_dh, iono_std, iono_prn, trop_std, trop_prn, tq_last;
    double err_snr, snr_ref;
    double bl_max;
    /* lever-arm solving */
    int lev_est; double lev_sd0;
    int lev_have[MAXRX]; double lev_val[MAXRX][3], lev_sd[MAXRX][3], lev_cfg[MAXRX][3];
};

static double sat_lambda(const tcctx_t *tc, const obsd_t *o, int f)
{
    double fr = sat2freq(o->sat, o->code[f], &tc->nav);
    return fr > 0 ? CLIGHT / fr : 0.0;
}

int tc_count(const tcctx_t *tc) { return tc->ne; }
double tc_time(const tcctx_t *tc, int i) { return tc->et[i]; }
int tc_rx(const tcctx_t *tc, int i) { return tc->ev_rx[i]; }

int tc_spp(tcctx_t *tc, int i, double *r, double *v, double *dt)
{
    if (i < 0 || i >= tc->ne || tc->spp[i][6] < 0.5) return 0;
    memcpy(r, tc->spp[i], sizeof(double) * 3);
    memcpy(v, tc->spp[i] + 3, sizeof(double) * 3);
    *dt = tc->rt[i] - tc->et[i];
    return 1;
}

static void index_epochs(const obs_t *obs, int **idx, int **cnt, double **t, int *n)
{
    int i, k = 0, cap = 1024;
    *idx = malloc(sizeof(int) * cap); *cnt = malloc(sizeof(int) * cap); *t = malloc(sizeof(double) * cap);
    for (i = 0; i < obs->n; ) {
        int j = i;
        while (j < obs->n && fabs(timediff(obs->data[j].time, obs->data[i].time)) < 1e-6) j++;
        if (k >= cap) {
            cap *= 2;
            *idx = realloc(*idx, sizeof(int) * cap); *cnt = realloc(*cnt, sizeof(int) * cap);
            *t = realloc(*t, sizeof(double) * cap);
        }
        (*idx)[k] = i; (*cnt)[k] = j - i; (*t)[k] = pp_gtime2gps(obs->data[i].time);
        k++;
        i = j;
    }
    *n = k;
}

tcctx_t *tc_open(const cfg_t *cfg, char *err, int errn)
{
    tcctx_t *tc = calloc(1, sizeof(tcctx_t));
    char files[64][1024];
    int nfile, i, j, f;
    const char *rov = cfg_str(cfg, "file.rover_obs", ""), *bas = cfg_str(cfg, "file.base_obs", "");
    double rb[3];
    gtime_t t0 = {0}, t1 = {0};
    const char *sys = cfg_str(cfg, "gnss.systems", "GREJ");

    if (!tc) { snprintf(err, errn, "out of memory"); return NULL; }
    tc->popt = prcopt_default;
    tc->popt.mode = PMODE_SINGLE;
    tc->popt.ionoopt = IONOOPT_BRDC;
    tc->popt.tropopt = TROPOPT_SAAS;
    tc->popt.navsys = 0;
    for (; *sys; sys++) switch (*sys) {
        case 'G': tc->popt.navsys |= SYS_GPS; break;
        case 'R': tc->popt.navsys |= SYS_GLO; break;
        case 'E': tc->popt.navsys |= SYS_GAL; break;
        case 'J': tc->popt.navsys |= SYS_QZS; break;
        case 'C': tc->popt.navsys |= SYS_CMP; break;
    }
    tc->elmin = cfg_num(cfg, "gnss.elmask_deg", 12) * D2R;
    tc->popt.elmin = tc->elmin;
    tc->elmin_ar = cfg_num(cfg, "tc.elmask_ar_deg", 15) * D2R;
    tc->err_a = cfg_num(cfg, "tc.phase_err_a_m", 0.003);
    tc->err_b = cfg_num(cfg, "tc.phase_err_b_m", 0.003);
    tc->eratio = cfg_num(cfg, "tc.code_phase_ratio", 300);
    tc->err_snr = cfg_num(cfg, "tc.snr_err_m", 0.0006);     /* 0 = elevation weighting only */
    tc->snr_ref = cfg_num(cfg, "tc.snr_ref_dbhz", 50.0);
    {
        const char *im = cfg_str(cfg, "tc.iono", "auto"), *tm = cfg_str(cfg, "tc.tropo", "auto");
        tc->iono_mode = !strcmp(im, "on") ? 1 : !strcmp(im, "off") ? 0 : 2;
        tc->trop_mode = !strcmp(tm, "on") ? 1 : !strcmp(tm, "off") ? 0 : 2;
    }
    tc->iono_bl = cfg_num(cfg, "tc.iono_baseline_km", 10.0) * 1e3;
    tc->trop_bl = cfg_num(cfg, "tc.tropo_baseline_km", 10.0) * 1e3;
    tc->trop_dh = cfg_num(cfg, "tc.tropo_height_diff_m", 150.0);
    tc->iono_std = cfg_num(cfg, "tc.iono_std_m_10km", 0.015);   /* initial, per 10 km */
    tc->iono_prn = cfg_num(cfg, "tc.iono_prn_m_10km", 4e-4);   /* m/sqrt(s), per 10 km */
    tc->trop_std = cfg_num(cfg, "tc.tropo_std_m", 0.03);
    tc->lev_est = !strcmp(cfg_str(cfg, "lever.estimate", "off"), "on");
    tc->lev_sd0 = cfg_num(cfg, "lever.estimate_sd_m", 0.10);
    tc->trop_prn = cfg_num(cfg, "tc.tropo_prn_m", 1e-4);       /* m/sqrt(s) */
    tc->slip_thr = cfg_num(cfg, "tc.slip_m", 0.08);
    tc->gf_thr = cfg_num(cfg, "tc.gf_slip_m", 0.05);
    tc->max_base_dt = cfg_num(cfg, "tc.max_base_interval_s", 60);
    tc->ar_mode = cfg_int(cfg, "tc.ar", 1);
    tc->glo_ar = cfg_int(cfg, "tc.glonass_ar", 0);
    tc->ar_ratio = cfg_num(cfg, "tc.ar_ratio", 3.0);
    tc->hold_var = cfg_num(cfg, "tc.hold_var_cyc2", 0.001);
    tc->amb_var0 = cfg_num(cfg, "tc.amb_var0_cyc2", 100.0);
    tc->min_fix_sats = cfg_int(cfg, "tc.min_fix_sats", 4);
    tc->min_lock = cfg_int(cfg, "tc.min_lock", 5);
    tc->cb_tau = cfg_num(cfg, "tc.code_check_s", 0.0);   /* off: receiver-to-receiver code biases (GLONASS IFB, L2 tracking) reach metres */
    tc->cb_thr = cfg_num(cfg, "tc.code_check_m", 0.5);
    tc->maxout = cfg_int(cfg, "tc.maxout_s", 5);
    tc->nf = cfg_int(cfg, "gnss.frequencies", 2);
    if (tc->nf < 1) tc->nf = 1;
    if (tc->nf > MAXF) tc->nf = MAXF;
    if (cfg_vec(cfg, "base.xyz", rb, 3) != 3 || norm3(rb) < 6.0e6) {
        snprintf(err, errn, "the base station position is not set");
        free(tc); return NULL;
    }
    memcpy(tc->rb, rb, sizeof(rb));
    pp_ecef2llh(rb, tc->rbllh);
    cfg_vec(cfg, "base.antdel_enu", tc->bdel, 3);

    /* observations: primary antenna, and the secondary one when given */
    if (!readrnxt(rov, 1, t0, t1, 0.0, "", &tc->robs[0], &tc->nav, NULL) || tc->robs[0].n <= 0) {
        snprintf(err, errn, "cannot read rover observations %s", rov); free(tc); return NULL;
    }
    tc->nrx = 1;
    {
        const char *rov2 = cfg_str(cfg, "file.rover2_obs", "");
        if (*rov2) {
            if (readrnxt(rov2, 1, t0, t1, 0.0, "", &tc->robs[1], &tc->nav, NULL) && tc->robs[1].n > 0) tc->nrx = 2;
            else pp_warn("secondary antenna observations could not be read; using the primary only");
        }
    }
    if (!readrnxt(bas, 2, t0, t1, 0.0, "", &tc->bobs, &tc->nav, NULL) || tc->bobs.n <= 0) {
        snprintf(err, errn, "cannot read base observations %s", bas); free(tc); return NULL;
    }
    nfile = cfg_list(cfg, "file.nav", files, 64);
    for (i = 0; i < nfile; i++) readrnx(files[i], 0, "", NULL, &tc->nav, NULL);
    if (tc->nav.n <= 0 && tc->nav.ng <= 0) {
        snprintf(err, errn, "no broadcast ephemeris (navigation file) was given"); free(tc); return NULL;
    }
    uniqnav(&tc->nav);
    {
        const char *atx = cfg_str(cfg, "file.atx", ""), *ant = cfg_str(cfg, "base.anttype", "");
        pcvs_t pcvs = {0};
        if (*atx && *ant && readpcv(atx, &pcvs)) {
            pcv_t *pc = searchpcv(0, ant, tc->bobs.data[0].time, &pcvs);
            if (pc) { tc->bpcv = *pc; tc->have_bpcv = 1; }
            else pp_warn("base antenna '%s' is not in the antenna file; no phase centre corrections", ant);
            free(pcvs.pcv);
        }
    }
    sortobs(&tc->bobs);
    index_epochs(&tc->bobs, &tc->bi, &tc->bn, &tc->bt, &tc->nb);
    {
        int *ei[MAXRX], *en[MAXRX], ne[MAXRX] = {0}, r, tot = 0, q;
        double *rt[MAXRX];
        for (r = 0; r < tc->nrx; r++) {
            sortobs(&tc->robs[r]);
            index_epochs(&tc->robs[r], &ei[r], &en[r], &rt[r], &ne[r]);
            tot += ne[r];
        }
        tc->ei = malloc(sizeof(int) * tot); tc->en = malloc(sizeof(int) * tot);
        tc->rt = malloc(sizeof(double) * tot); tc->ev_rx = malloc(sizeof(int) * tot);
        tc->ne = tot;
        for (q = 0, r = 0; r < tc->nrx; r++) {
            for (i = 0; i < ne[r]; i++, q++) {
                tc->ei[q] = ei[r][i]; tc->en[q] = en[r][i]; tc->rt[q] = rt[r][i]; tc->ev_rx[q] = r;
            }
            free(ei[r]); free(en[r]); free(rt[r]);
        }
    }
    tc->et = malloc(sizeof(double) * tc->ne);
    tc->spp = calloc(tc->ne, sizeof(*tc->spp));

    /* base arcs (slips from LLI, geometry-free jumps and gaps) */
    tc->barc = calloc((size_t)tc->bobs.n * MAXF, sizeof(int));
    {
        static int arc[MAXSAT][MAXF];
        static double tl[MAXSAT], gfl[MAXSAT];
        memset(arc, 0, sizeof(arc)); memset(tl, 0, sizeof(tl)); memset(gfl, 0, sizeof(gfl));
        for (j = 0; j < tc->nb; j++) {
            for (i = tc->bi[j]; i < tc->bi[j] + tc->bn[j]; i++) {
                const obsd_t *o = &tc->bobs.data[i];
                int s = o->sat - 1, slip_gf = 0;
                double gf = 0, l1 = sat_lambda(tc, o, 0), l2 = sat_lambda(tc, o, 1);
                if (s < 0 || s >= MAXSAT) continue;
                if (o->L[0] != 0 && o->L[1] != 0 && l1 > 0 && l2 > 0) {
                    gf = o->L[0] * l1 - o->L[1] * l2;
                    if (tl[s] > 0 && gfl[s] != 0 &&
                        fabs(gf - gfl[s]) > tc->gf_thr * 2 + 0.01 * (tc->bt[j] - tl[s])) slip_gf = 1;
                    gfl[s] = gf;
                } else gfl[s] = 0;
                for (f = 0; f < MAXF; f++) {
                    if ((o->LLI[f] & 1) || slip_gf || (tl[s] > 0 && tc->bt[j] - tl[s] > tc->max_base_dt))
                        arc[s][f]++;
                    tc->barc[(size_t)i * MAXF + f] = arc[s][f];
                }
                tl[s] = tc->bt[j];
            }
        }
    }
    /* single point positions: true epoch times and initial positions */
    {
        sol_t sol;
        double azel[MAXOBS * 2];
        char msg[128];
        static ssat_t ssat[MAXSAT];
        for (i = 0; i < tc->ne; i++) {
            memset(&sol, 0, sizeof(sol));
            tc->et[i] = tc->rt[i];
            if (pntpos(tc->robs[tc->ev_rx[i]].data + tc->ei[i], tc->en[i] < MAXOBS ? tc->en[i] : MAXOBS, &tc->nav, &tc->popt, &sol, azel, ssat, msg)) {
                tc->et[i] = pp_gtime2gps(sol.time);
                for (j = 0; j < 6; j++) tc->spp[i][j] = sol.rr[j];
                tc->spp[i][6] = 1;
            }
            if ((i & 1023) == 0) pp_progress(0.02 * i / tc->ne, "reading GNSS observations");
        }
    }
    /* events in true-time order (a stable insertion sort: the two receivers
     * are already in order each, so this is a merge) */
    for (i = 1; i < tc->ne; i++) {
        int e1 = tc->ei[i], n1 = tc->en[i], x1 = tc->ev_rx[i];
        double r1 = tc->rt[i], t1v = tc->et[i], sp[7];
        memcpy(sp, tc->spp[i], sizeof(sp));
        j = i - 1;
        while (j >= 0 && tc->et[j] > t1v) {
            tc->ei[j + 1] = tc->ei[j]; tc->en[j + 1] = tc->en[j]; tc->ev_rx[j + 1] = tc->ev_rx[j];
            tc->rt[j + 1] = tc->rt[j]; tc->et[j + 1] = tc->et[j];
            memcpy(tc->spp[j + 1], tc->spp[j], sizeof(sp));
            j--;
        }
        tc->ei[j + 1] = e1; tc->en[j + 1] = n1; tc->ev_rx[j + 1] = x1;
        tc->rt[j + 1] = r1; tc->et[j + 1] = t1v; memcpy(tc->spp[j + 1], sp, sizeof(sp));
    }
    for (i = 0; i < BCACHE; i++) tc->cache[i].j = -1;
    pp_info("tightly coupled: %d rover epochs from %d antenna(s), %d base epochs", tc->ne, tc->nrx, tc->nb);
    return tc;
}

void tc_close(tcctx_t *tc)
{
    if (!tc) return;
    pp_result("tc_slips", "%ld", tc->n_slips);
    pp_result("tc_code_outliers", "%ld", tc->n_out_code);
    pp_result("tc_phase_outliers", "%ld", tc->n_out_phase);
    pp_result("tc_fixed_epochs", "%ld", tc->n_fix);
    pp_result("tc_float_epochs", "%ld", tc->n_float);
    pp_result("tc_resets", "%ld", tc->n_reset);
    pp_result("tc_fix_rejected_by_code", "%ld", tc->n_cbreset);
    pp_result("tc_common_mode", "%ld", tc->n_common);
    pp_result("tc_fix_releases", "%ld", tc->n_release);
    pp_result("tc_baseline_max_km", "%.2f", tc->bl_max / 1e3);
    {
        int r;
        for (r = 0; r < MAXRX; r++) if (tc->lev_have[r]) {
            const char *nm = r ? "lever_antenna2" : "lever_antenna";
            pp_result(nm, "%.4f,%.4f,%.4f", tc->lev_val[r][0], tc->lev_val[r][1], tc->lev_val[r][2]);
            pp_result(r ? "lever_antenna2_sd" : "lever_antenna_sd", "%.4f,%.4f,%.4f",
                      tc->lev_sd[r][0], tc->lev_sd[r][1], tc->lev_sd[r][2]);
            pp_result(r ? "lever_antenna2_prior" : "lever_antenna_prior", "%.4f,%.4f,%.4f",
                      tc->lev_cfg[r][0], tc->lev_cfg[r][1], tc->lev_cfg[r][2]);
        }
    }
    pp_result("tc_iono_estimated", "%d", tc->iono_on);
    pp_result("tc_tropo_estimated", "%d", tc->trop_on);
    free(tc->robs[0].data); free(tc->robs[1].data); free(tc->bobs.data); free(tc->ev_rx);
    freenav(&tc->nav, 0xFF);
    free(tc->ei); free(tc->en); free(tc->et); free(tc->rt); free(tc->spp);
    free(tc->bi); free(tc->bn); free(tc->bt); free(tc->barc);
    free(tc);
}

/* zero-difference residuals of base epoch j (cached) */
static const bzd_t *base_zd(tcctx_t *tc, int j)
{
    int c, i, f;
    bzd_t *z;
    double rs[6 * MAXOBS], dts[2 * MAXOBS], var[MAXOBS], e[3], azel[2];
    int svh[MAXOBS], n;
    const obsd_t *ob;
    for (c = 0; c < BCACHE; c++) if (tc->cache[c].j == j) return &tc->cache[c];
    z = &tc->cache[tc->cache_next];
    tc->cache_next = (tc->cache_next + 1) % BCACHE;
    z->j = j;
    z->n = 0;
    ob = tc->bobs.data + tc->bi[j];
    n = tc->bn[j];
    if (n > MAXOBS) n = MAXOBS;
    satposs(ob[0].time, ob, n, &tc->nav, EPHOPT_BRDC, rs, dts, var, svh);
    for (i = 0; i < n; i++) {
        double r, trop, base;
        const obsd_t *o = &ob[i];
        if (norm3(rs + i*6) < 1.0 || svh[i]) continue;
        if ((r = geodist(rs + i*6, tc->rb, e)) <= 0) continue;
        satazel(tc->rbllh, e, azel);
        if (azel[1] < tc->elmin * 0.5) continue;
        trop = tropmodel(o->time, tc->rbllh, azel, 0.7);
        base = r - CLIGHT * dts[i*2] + trop;
        {
            double dant[NFREQ] = {0};
            pcv_t pc0;
            if (tc->have_bpcv) antmodel(&tc->bpcv, tc->bdel, azel, 1, dant);
            else {
                memset(&pc0, 0, sizeof(pc0));
                antmodel(&pc0, tc->bdel, azel, 0, dant);
            }
            z->sat[z->n] = o->sat;
            for (f = 0; f < MAXF; f++) {
                double lam = sat_lambda(tc, o, f), bf = base + dant[f];
                z->zphi[z->n][f] = (o->L[f] != 0 && lam > 0) ? o->L[f] * lam - bf : 0;
                z->zcode[z->n][f] = (o->P[f] != 0) ? o->P[f] - bf : 0;
                z->arc[z->n][f] = tc->barc[(size_t)(tc->bi[j] + i) * MAXF + f];
                z->lli[z->n][f] = o->LLI[f];
            }
        }
        z->n++;
    }
    return z;
}

static int find_sat(const bzd_t *z, int sat)
{
    int i;
    for (i = 0; i < z->n; i++) if (z->sat[i] == sat) return i;
    return -1;
}

/* one satellite/frequency measurement candidate */
typedef struct {
    int sat, f, sys, ok_phase, ok_code;
    double el, e[3], lam;
    double sdphi, sdcode;    /* SD residuals (m) */
    double var;              /* SD phase variance (m^2) */
    int arc, half;
    double gam, mw;          /* iono factor (f1/f)^2, wet mapping */
    int ak;                  /* ambiguity key: satellite + 1000 x receiver */
} cand_t;

static int sys_group(int sat)
{
    int s = satsys(sat, NULL);
    switch (s) {
        case SYS_GPS: return 0; case SYS_GLO: return 1; case SYS_GAL: return 2;
        case SYS_QZS: return 3; case SYS_CMP: return 4; case SYS_SBS: return 5;
    }
    return 6;
}

static int cand_cmp_el(const void *a, const void *b)
{
    double x = ((const cand_t *)a)->el, y = ((const cand_t *)b)->el;
    return x < y ? 1 : x > y ? -1 : 0;
}

int tc_update(tcctx_t *tc, int ie, insstate_t *s, kf_t *kf, const insopt_t *o,
              int *status, double *ratio_out, int dir)
{
    const int rx = tc->ev_rx[ie];
    const obsd_t *ro = tc->robs[rx].data + tc->ei[ie];
    const double *lever = rx ? o->lever_ant2 : o->lever_ant;
    double lev_now[3];
    int il[3] = {-1, -1, -1};
    int nr = tc->en[ie], i, f, k, nc = 0, j0, j1, m, iter;
    double rs[6 * MAXOBS], dts[2 * MAXOBS], var[MAXOBS], ra[3], rallh[3], la[3], L[9];
    int svh[MAXOBS];
    double t = tc->et[ie], trx = tc->rt[ie], w = 0, dtm = t - s->t;
    const bzd_t *b0, *b1 = NULL;
    cand_t *c;
    double *H = NULL, *y = NULL, *R = NULL;
    int nsat_used = 0;
    double ra_pre[3];

    *status = PPST_NONE;
    *ratio_out = 0;
    if (nr > MAXOBS) nr = MAXOBS;

    /* base epochs around the rover receiver time */
    {
        int lo = 0, hi = tc->nb - 1;
        while (lo < hi) { int mid = (lo + hi + 1) / 2; if (tc->bt[mid] <= trx + 1e-6) lo = mid; else hi = mid - 1; }
        j0 = lo;
        if (tc->bt[j0] > trx + 1e-3) return 0;
        if (fabs(tc->bt[j0] - trx) < 2e-3) { j1 = -1; }
        else {
            j1 = j0 + 1;
            if (j1 >= tc->nb || tc->bt[j1] - tc->bt[j0] > tc->max_base_dt) return 0;
            w = (trx - tc->bt[j0]) / (tc->bt[j1] - tc->bt[j0]);
        }
    }
    b0 = base_zd(tc, j0);
    if (j1 >= 0) b1 = base_zd(tc, j1);

    /* lever-arm states (solved from the data when asked) */
    if (tc->lev_est) {
        int a;
        for (a = 0; a < 3; a++) {
            if ((il[a] = kf_find_amb(kf, rx, LEV_X + a)) < 0)
                il[a] = kf_add_amb(kf, rx, LEV_X + a, lever[a], tc->lev_sd0 * tc->lev_sd0, 0.0);
            lev_now[a] = il[a] >= 0 ? kf->amb[il[a]] : lever[a];
        }
        if (!tc->lev_have[rx]) memcpy(tc->lev_cfg[rx], lever, sizeof(lev_now));
        lever = lev_now;
    }

    /* rover antenna at the epoch */
    ins_lever_pos(s, lever, ra);
    for (i = 0; i < 3; i++) ra[i] += s->v[i] * dtm;
    pp_ecef2llh(ra, rallh);
    m3_vec(s->C, lever, la);
    m3_skew(la, L);

    satposs(ro[0].time, ro, nr, &tc->nav, EPHOPT_BRDC, rs, dts, var, svh);
    c = calloc((size_t)nr * MAXF, sizeof(cand_t));
    for (i = 0; i < nr; i++) {
        const obsd_t *q = &ro[i];
        double r, e[3], azel[2], trop, comp, mw = 1.0;
        int ib0, ib1 = -1, sys = sys_group(q->sat);
        if (!(satsys(q->sat, NULL) & tc->popt.navsys)) continue;
        if (sys > 4) continue;
        if (norm3(rs + i*6) < 1.0 || svh[i]) continue;
        if ((r = geodist(rs + i*6, ra, e)) <= 0) continue;
        satazel(rallh, e, azel);
        if (azel[1] < tc->elmin) continue;
        if ((ib0 = find_sat(b0, q->sat)) < 0) continue;
        if (b1 && (ib1 = find_sat(b1, q->sat)) < 0) continue;
        trop = tropmodel(q->time, rallh, azel, 0.7);
        comp = r - CLIGHT * dts[i*2] + trop;
        tropmapf(q->time, rallh, azel, &mw);
        {
            double gf = 0, l1 = sat_lambda(tc, q, 0), l2 = sat_lambda(tc, q, 1);
            int gf_slip = 0, gf_ok = 0;
            slip_t *sl0 = &tc->slip[rx][q->sat - 1][0];
            if (tc->nf > 1 && q->L[0] != 0 && q->L[1] != 0 && l1 > 0 && l2 > 0) {
                /* geometry-free test against the last epoch that had both
                 * frequencies. An epoch with one frequency missing must not
                 * clear the reference: a phase that returns after a dropout
                 * is exactly when a slip is most likely (and its loss-of-lock
                 * flag may have been in a lost packet). */
                double dtg = fabs(t - sl0->gf_t);
                gf = q->L[0] * l1 - q->L[1] * l2;
                if (sl0->gf_t != 0 && dtg <= tc->maxout) {
                    gf_ok = 1;
                    if (fabs(gf - sl0->gf_last) > tc->gf_thr + 0.01 * dtg) gf_slip = 1;
                }
                sl0->gf_last = gf;
                sl0->gf_t = t;
            }
            for (f = 0; f < tc->nf; f++) {
                cand_t *x = &c[nc];
                double lam = sat_lambda(tc, q, f), zb_phi, zb_code;
                int arc;
                if (lam <= 0 || q->P[f] == 0) continue;
                zb_code = b0->zcode[ib0][f];
                zb_phi = b0->zphi[ib0][f];
                arc = b0->arc[ib0][f];
                if (zb_code == 0) continue;
                if (b1) {
                    if (b1->zcode[ib1][f] == 0) continue;
                    zb_code = (1 - w) * zb_code + w * b1->zcode[ib1][f];
                    if (zb_phi != 0 && b1->zphi[ib1][f] != 0 && b1->arc[ib1][f] == arc)
                        zb_phi = (1 - w) * zb_phi + w * b1->zphi[ib1][f];
                    else zb_phi = 0;
                }
                memset(x, 0, sizeof(*x));
                x->sat = q->sat; x->ak = q->sat + 1000 * rx; x->f = f; x->sys = sys; x->el = azel[1]; x->lam = lam;
                memcpy(x->e, e, sizeof(e));
                x->gam = l1 > 0 ? (lam / l1) * (lam / l1) : 1.0;
                x->mw = mw;
                x->sdcode = (q->P[f] - comp) - zb_code;
                x->ok_code = 1;
                if (q->L[f] != 0 && zb_phi != 0) {
                    x->sdphi = (q->L[f] * lam - comp) - zb_phi;
                    x->ok_phase = 1;
                }
                x->arc = arc;
                {
                    double sinel = sin(azel[1]);
                    x->var = 2.0 * (tc->err_a * tc->err_a + tc->err_b * tc->err_b / (sinel * sinel));
                    /* signal strength: weak (diffracted, multipath) signals count less */
                    if (tc->err_snr > 0 && q->SNR[f] > 10) {
                        double dsn = tc->snr_ref - q->SNR[f];
                        if (dsn < 0) dsn = 0;
                        if (dsn > 30) dsn = 30;
                        x->var += tc->err_snr * tc->err_snr * pow(10.0, 0.1 * dsn);
                    }
                }
                /* slip flags */
                if (x->ok_phase) {
                    slip_t *sl = &tc->slip[rx][q->sat - 1][f];
                    int k2 = kf_find_amb(kf, q->sat + 1000 * rx, f);
                    int slip = (q->LLI[f] & 1) || gf_slip;
                    if (k2 >= 0 && sl->valid && sl->base_arc != arc) slip = 1;
                    if (k2 >= 0 && kf->amb_tlast[k2] != 0 && fabs(t - kf->amb_tlast[k2]) > tc->maxout) slip = 1;
                    /* phase returning after an absence that neither the
                     * geometry-free nor the INS-aided test can check */
                    if (k2 >= 0 && !sl->valid && sl->t_last != 0 && !gf_ok) slip = 1;
                    if (slip && k2 >= 0) { kf_del_amb(kf, k2); tc->n_slips++; }
                    if (q->LLI[f] & 2) x->half = 1;       /* half-cycle: no AR */
                }
                nc++;
            }
        }
    }

    /* INS-aided slip test: change of SD phase residual minus the median */
    {
        double d[MAXOBS * MAXF];
        int nd = 0, idx[MAXOBS * MAXF];
        double sv = sqrt(PP_(kf, IV, IV) + PP_(kf, IV + 1, IV + 1) + PP_(kf, IV + 2, IV + 2));
        for (k = 0; k < nc; k++) {
            slip_t *sl = &tc->slip[rx][c[k].sat - 1][c[k].f];
            int ka = kf_find_amb(kf, c[k].ak, c[k].f);
            if (!c[k].ok_phase || ka < 0 || !sl->valid || fabs(t - sl->t_last) > 1.5) continue;
            d[nd] = (c[k].sdphi - sl->q_last);
            idx[nd++] = k;
        }
        if (nd >= 4) {
            double tmp[MAXOBS * MAXF], med, thr;
            memcpy(tmp, d, sizeof(double) * nd);
            for (i = 0; i < nd; i++) for (k = i + 1; k < nd; k++) if (tmp[k] < tmp[i]) { double z = tmp[i]; tmp[i] = tmp[k]; tmp[k] = z; }
            med = nd % 2 ? tmp[nd/2] : 0.5 * (tmp[nd/2 - 1] + tmp[nd/2]);
            thr = tc->slip_thr + 3.0 * sv * fabs(t - tc->last_t[rx]);
            for (i = 0; i < nd; i++) {
                if (fabs(d[i] - med) > thr) {
                    int ka = kf_find_amb(kf, c[idx[i]].ak, c[idx[i]].f);
                    if (ka >= 0) { kf_del_amb(kf, ka); tc->n_slips++; }
                }
            }
        }
    }

    /* add new ambiguity states */
    for (k = 0; k < nc; k++) {
        if (!c[k].ok_phase) continue;
        if (kf_find_amb(kf, c[k].ak, c[k].f) < 0) {
            double n0 = (c[k].sdphi - c[k].sdcode) / c[k].lam;
            if (kf_add_amb(kf, c[k].ak, c[k].f, n0, tc->amb_var0, c[k].lam) < 0) {
                c[k].ok_phase = 0;       /* table full: use the code only */
                continue;
            }
        }
        {
            int ka = kf_find_amb(kf, c[k].ak, c[k].f);
            if (ka >= 0) kf->amb_half[ka] = c[k].half;
        }
    }
    {
        double ra_pre_[3];
        memcpy(ra_pre_, ra, sizeof(ra_pre_));
        memcpy(ra_pre, ra_pre_, sizeof(ra_pre_));
    }
    /* drop ambiguities not seen for too long */
    for (k = kf->namb - 1; k >= 0; k--)
        if (kf->amb_tlast[k] != 0 && fabs(t - kf->amb_tlast[k]) > tc->maxout) kf_del_amb(kf, k);

    /* recovery: several epochs in a row with satellites but no accepted
     * update means the INS has drifted beyond its covariance */
    if (tc->fail_count >= 8) {
        for (i = 0; i < 3; i++) {
            PP_(kf, IR + i, IR + i) += 25.0;
            PP_(kf, IV + i, IV + i) += 1.0;
        }
        /* drop the ambiguities; the atmosphere and lever-arm states stay */
        for (k = kf->namb - 1; k >= 0; k--) if (kf->amb_f[k] < MAXF) kf_del_amb(kf, k);
        tc->fail_count = 0;
        tc->n_reset++;
        pp_warn("GPS %.1f: INS position reopened after persistent GNSS disagreement", t);
        for (k = 0; k < nc; k++) if (c[k].ok_phase) {
            double n0 = (c[k].sdphi - c[k].sdcode) / c[k].lam;
            if (kf_add_amb(kf, c[k].ak, c[k].f, n0, tc->amb_var0, c[k].lam) < 0) c[k].ok_phase = 0;
        }
    }

    /* atmosphere states */
    {
        double bl = sqrt((ra[0] - tc->rb[0]) * (ra[0] - tc->rb[0]) + (ra[1] - tc->rb[1]) * (ra[1] - tc->rb[1]) + (ra[2] - tc->rb[2]) * (ra[2] - tc->rb[2]));
        double dtq = tc->tq_last != 0 ? fabs(t - tc->tq_last) : 0;
        double b10 = bl / 1e4;
        if (bl > tc->bl_max) tc->bl_max = bl;
        if (!tc->iono_on && (tc->iono_mode == 1 || (tc->iono_mode == 2 && bl > tc->iono_bl))) {
            tc->iono_on = 1;
            pp_info("GPS %.1f: baseline %.1f km, estimating the relative ionosphere", t, bl / 1e3);
        }
        if (!tc->trop_on && (tc->trop_mode == 1 ||
                             (tc->trop_mode == 2 && (bl > tc->trop_bl || fabs(rallh[2] - tc->rbllh[2]) > tc->trop_dh)))) {
            tc->trop_on = 1;
            pp_info("GPS %.1f: baseline %.1f km, height difference %.0f m, estimating the relative troposphere",
                    t, bl / 1e3, rallh[2] - tc->rbllh[2]);
        }
        if (dtq > 60) dtq = 60;
        if (b10 < 0.1) b10 = 0.1;
        for (k = 0; k < kf->namb; k++) {
            if (kf->amb_f[k] == ATM_IONO) {
                double sc = tc->iono_prn * b10;
                PP_(kf, NC + k, NC + k) += sc * sc * dtq;
            } else if (kf->amb_f[k] == ATM_TROP) {
                PP_(kf, NC + k, NC + k) += tc->trop_prn * tc->trop_prn * dtq;
            }
        }
        if (getenv("PP_DEBUG_ATM") && fmod(t, 60.0) < 0.13) {
            int it = kf_find_amb(kf, 0, ATM_TROP);
            if (it >= 0) pp_info("ATM t %.0f rx %d trop %.4f sd %.4f", t, rx, kf->amb[it], sqrt(PP_(kf, NC + it, NC + it)));
        }
        tc->tq_last = t;
        if (tc->trop_on && kf_find_amb(kf, 0, ATM_TROP) < 0)
            kf_add_amb(kf, 0, ATM_TROP, 0.0, tc->trop_std * tc->trop_std, 0.0);
        if (tc->iono_on) {
            for (k = 0; k < nc; k++) {
                if (kf_find_amb(kf, c[k].sat, ATM_IONO) < 0) {
                    double sd = tc->iono_std * (b10 > 0.3 ? b10 : 0.3) / sin(c[k].el > 0.2 ? c[k].el : 0.2);
                    kf_add_amb(kf, c[k].sat, ATM_IONO, 0.0, sd * sd, 0.0);
                }
            }
        }
    }

    /* measurements with outlier screening */
    int common_done = 0;
    for (iter = 0; iter < 10; iter++) {
        int nbad_ph = 0, n_ph = 0;
        double maxy_ph = 0;
        int ref[7][MAXF], refc[7][MAXF], g, n = kf->n, worst = -1, rowc = 0;
        int *rk = NULL, *rtype = NULL, *rref = NULL;
        double worstv = 0;
        for (g = 0; g < 7; g++) for (f = 0; f < MAXF; f++) ref[g][f] = refc[g][f] = -1;
        qsort(c, nc, sizeof(cand_t), cand_cmp_el);
        for (k = 0; k < nc; k++) {
            if (!c[k].ok_code) continue;
            /* phase reference: highest satellite with phase and an ambiguity;
             * code reference: highest satellite with code (so a group without
             * phase still gives code rows) */
            if (refc[c[k].sys][c[k].f] < 0) refc[c[k].sys][c[k].f] = k;
            if (ref[c[k].sys][c[k].f] < 0 && c[k].ok_phase && kf_find_amb(kf, c[k].ak, c[k].f) >= 0)
                ref[c[k].sys][c[k].f] = k;
        }
        m = 0;
        for (k = 0; k < nc; k++) {
            int r0 = ref[c[k].sys][c[k].f], rc = refc[c[k].sys][c[k].f];
            if (!c[k].ok_code) continue;
            if (rc >= 0 && rc != k) m++;
            if (r0 >= 0 && r0 != k && c[k].ok_phase) m++;
        }
        if (m == 0) break;
        free(H); free(y); free(R);
        H = calloc((size_t)m * n, sizeof(double));
        y = calloc(m, sizeof(double));
        R = calloc((size_t)m * m, sizeof(double));
        rk = calloc(m, sizeof(int)); rtype = calloc(m, sizeof(int)); rref = calloc(m, sizeof(int));
        /* rows: phase rows then code rows, grouped by system/frequency */
        for (int type = 0; type < 2; type++) {
            for (g = 0; g < 7; g++) for (f = 0; f < MAXF; f++) {
                int r0 = type == 0 ? ref[g][f] : refc[g][f], first = rowc;
                cand_t *cr;
                if (r0 < 0) continue;
                cr = &c[r0];
                for (k = 0; k < nc; k++) {
                    cand_t *cs = &c[k];
                    double de[3], hh[3];
                    int a, b, ks, kr;
                    if (k == r0 || cs->sys != g || cs->f != f || !cs->ok_code) continue;
                    if (type == 0 && !cs->ok_phase) continue;
                    for (a = 0; a < 3; a++) de[a] = cs->e[a] - cr->e[a];
                    /* H_phi = de' [la x] */
                    for (a = 0; a < 3; a++) { hh[a] = 0; for (b = 0; b < 3; b++) hh[a] += de[b] * L[b*3 + a]; }
                    for (a = 0; a < 3; a++) {
                        H[rowc*n + IR + a] = de[a];
                        H[rowc*n + IV + a] = de[a] * dtm;
                        H[rowc*n + IA + a] = hh[a];
                    }
                    if (tc->lev_est) {          /* d(antenna)/d(lever) = C */
                        for (a = 0; a < 3; a++) {
                            int ia = kf_find_amb(kf, rx, LEV_X + a);
                            double v = 0;
                            if (ia < 0) continue;
                            for (b = 0; b < 3; b++) v += de[b] * s->C[b*3 + a];
                            H[rowc*n + NC + ia] = v;
                        }
                    }
                    if (type == 0) {
                        ks = kf_find_amb(kf, cs->ak, f);
                        kr = kf_find_amb(kf, cr->ak, f);
                        if (ks < 0 || kr < 0) continue;
                        y[rowc] = (cs->sdphi - cr->sdphi) - (cs->lam * kf->amb[ks] - cr->lam * kf->amb[kr]);
                        H[rowc*n + NC + ks] = -cs->lam;
                        H[rowc*n + NC + kr] = cr->lam;
                    } else {
                        y[rowc] = cs->sdcode - cr->sdcode;
                    }
                    /* ionosphere: -I on phase, +I on code (L1 slant, scaled by (f1/f)^2) */
                    {
                        int is = kf_find_amb(kf, cs->sat, ATM_IONO), ir = kf_find_amb(kf, cr->sat, ATM_IONO);
                        if (is >= 0 && ir >= 0) {
                            double sg = type == 0 ? -1.0 : 1.0;
                            y[rowc] -= sg * (cs->gam * kf->amb[is] - cr->gam * kf->amb[ir]);
                            H[rowc*n + NC + is] = -sg * cs->gam;
                            H[rowc*n + NC + ir] = sg * cr->gam;
                        }
                    }
                    /* troposphere: relative zenith wet delay */
                    {
                        int it = kf_find_amb(kf, 0, ATM_TROP);
                        if (it >= 0) {
                            double dm = cs->mw - cr->mw;
                            y[rowc] -= dm * kf->amb[it];
                            H[rowc*n + NC + it] = -dm;
                        }
                    }
                    rk[rowc] = k; rtype[rowc] = type;
                    rref[rowc] = r0;
                    rowc++;
                }
                /* covariance block: R_ij = var_ref + delta_ij var_i */
                {
                    double fac = type == 0 ? 1.0 : tc->eratio * tc->eratio;
                    int r1, r2;
                    for (r1 = first; r1 < rowc; r1++) for (r2 = first; r2 < rowc; r2++)
                        R[r1*m + r2] = fac * (cr->var + (r1 == r2 ? c[rk[r1]].var : 0.0));
                }
            }
        }
        if (rowc < m) {             /* compact R to the rows actually built */
            int r1, r2;
            for (r1 = 0; r1 < rowc; r1++) for (r2 = 0; r2 < rowc; r2++) R[r1*rowc + r2] = R[r1*m + r2];
        }
        m = rowc;
        /* normalized innovations */
        for (i = 0; i < m; i++) {
            double sii = R[i*m + i];
            int a, b;
            for (a = 0; a < n; a++) {
                double ha = H[i*n + a];
                if (ha == 0) continue;
                for (b = 0; b < n; b++) if (H[i*n + b] != 0) sii += ha * PP_(kf, a, b) * H[i*n + b];
            }
            {
                double v = fabs(y[i]) / sqrt(sii);
                double lim = rtype[i] == 0 ? 6.0 : 5.0;
                if (v > lim && v > worstv) { worstv = v; worst = i; }
                if (rtype[i] == 0) {
                    n_ph++;
                    if (v > lim) { nbad_ph++; if (fabs(y[i]) > maxy_ph) maxy_ph = fabs(y[i]); }
                }
            }
        }
        /* common-mode disagreement: when most phase rows fail together the
         * INS (not the satellites) is wrong, typically after an IMU dropout.
         * Open the position / velocity / attitude covariance once and retry
         * rather than deleting good (possibly fixed) ambiguities one by one. */
        if (worst >= 0 && !common_done && n_ph >= 4 && 2 * nbad_ph > n_ph) {
            double d2 = maxy_ph * maxy_ph;
            for (i = 0; i < 3; i++) {
                PP_(kf, IR + i, IR + i) += d2 + 0.01;
                PP_(kf, IV + i, IV + i) += d2 + 0.04;
                PP_(kf, IA + i, IA + i) += (2.0 * D2R) * (2.0 * D2R);
            }
            common_done = 1;
            tc->n_common++;
            free(rk); free(rtype); free(rref);
            continue;
        }
        if (worst >= 0) {
            cand_t *cs = &c[rk[worst]];
            if (rtype[worst] == 1) { cs->ok_code = 0; cs->ok_phase = 0; tc->n_out_code++; }
            else {
                int ka = kf_find_amb(kf, cs->ak, cs->f);
                if (ka >= 0) kf_del_amb(kf, ka);
                cs->ok_phase = 0;
                tc->n_out_phase++;
            }
            free(rk); free(rtype); free(rref);
            continue;
        }
        /* update */
        if (m > 0 && kf_update(kf, H, y, R, m) == 0) {
            int seen[MAXSAT] = {0}, used[MAXOBS * MAXF] = {0}, nphase = 0;
            /* post-fit code residuals -> position offset (weighted LS) */
            if (tc->cb_tau > 0) {
                double Nn[9] = {0}, bb[3] = {0}, dxc[3];
                int ncr = 0, a, b2;
                for (i = 0; i < m; i++) {
                    double r, wgt;
                    if (rtype[i] != 1 || R[i*m + i] <= 0) continue;
                    r = y[i];
                    for (a = 0; a < n; a++) if (H[i*n + a] != 0) r -= H[i*n + a] * kf->x[a];
                    wgt = 1.0 / R[i*m + i];
                    for (a = 0; a < 3; a++) {
                        bb[a] += wgt * H[i*n + IR + a] * r;
                        for (b2 = 0; b2 < 3; b2++) Nn[a*3 + b2] += wgt * H[i*n + IR + a] * H[i*n + IR + b2];
                    }
                    ncr++;
                }
                if (ncr >= 6 && !matinv(Nn, 3)) {
                    double dte = tc->cb_t != 0 ? fabs(t - tc->cb_t) : 0, al;
                    for (a = 0; a < 3; a++) { dxc[a] = 0; for (b2 = 0; b2 < 3; b2++) dxc[a] += Nn[a*3 + b2] * bb[b2]; }
                    if (dte > 5.0) { memset(tc->cb, 0, sizeof(tc->cb)); memset(tc->cb_var, 0, sizeof(tc->cb_var)); tc->cb_age = 0; dte = 0; }
                    al = tc->cb_age <= 0 ? 1.0 : fmin(1.0, fmax(dte / tc->cb_tau, 1.0 / (1.0 + tc->cb_age / fmax(dte, 0.05))));
                    for (a = 0; a < 3; a++) {
                        tc->cb[a] = (1 - al) * tc->cb[a] + al * dxc[a];
                        /* epoch-to-epoch code errors are correlated (multipath):
                         * count the variance at the 1 s level */
                        tc->cb_var[a] = (1 - al) * (1 - al) * tc->cb_var[a] + al * al * Nn[a*4] * fmax(1.0, 1.0 / fmax(dte, 0.05));
                    }
                    tc->cb_age += dte;
                    tc->cb_t = t;
                }
            }
            ins_feedback(s, kf);
            /* count only satellites that gave a row (the reference included) */
            for (i = 0; i < m; i++) {
                int sa = c[rk[i]].sat - 1, sr = c[rref[i]].sat - 1;
                if (!seen[sa]) { seen[sa] = 1; nsat_used++; }
                if (!seen[sr]) { seen[sr] = 1; nsat_used++; }
                if (rtype[i] == 0) { used[rk[i]] = 1; used[rref[i]] = 1; nphase++; }
            }
            *status = PPST_DGPS;
            for (k = 0; k < nc; k++) {
                int ka = kf_find_amb(kf, c[k].ak, c[k].f);
                if (ka >= 0 && used[k]) {
                    kf->amb_lock[ka]++;
                    kf->amb_tlast[ka] = t;
                }
                if (seen[c[k].sat - 1] && (ka = kf_find_amb(kf, c[k].sat, ATM_IONO)) >= 0) kf->amb_tlast[ka] = t;
            }
            if ((k = kf_find_amb(kf, 0, ATM_TROP)) >= 0) kf->amb_tlast[k] = t;
            if (nphase > 0) *status = PPST_FLOAT;
        }
        free(rk); free(rtype); free(rref);
        break;
    }

    if (nc >= 5 && nsat_used == 0) tc->fail_count++;
    else if (nsat_used > 0) tc->fail_count = 0;

    /* integer ambiguity resolution */
    if (*status == PPST_FLOAT && tc->ar_mode) {
        int passed = 0;
        int sel[MAXAMB], nsel = 0, g, attempt;
        double elv[MAXAMB];
        int grp[MAXAMB];
        for (k = 0; k < kf->namb; k++) {
            int ci, ok = 0;
            for (ci = 0; ci < nc; ci++)
                if (c[ci].ak == kf->amb_sat[k] && c[ci].f == kf->amb_f[k] && c[ci].ok_phase) { ok = 1; break; }
            if (!ok) continue;
            if (c[ci].el < tc->elmin_ar || kf->amb_lock[k] < tc->min_lock) continue;
            if (c[ci].sys == 1 && !tc->glo_ar) continue;
            if (kf->amb_half[k]) continue;
            sel[nsel] = k; elv[nsel] = c[ci].el; grp[nsel] = c[ci].sys * MAXF + c[ci].f; nsel++;
        }
        for (attempt = 0; attempt < 4 && nsel >= tc->min_fix_sats + 1; attempt++) {
            /* reference per group = highest elevation */
            int refk[16], nb = 0, a, b;
            double *D, *af, *Q, *F2, s2[2], *PND, *DPa;
            for (g = 0; g < 16; g++) refk[g] = -1;
            for (a = 0; a < nsel; a++) if (refk[grp[a]] < 0 || elv[a] > elv[refk[grp[a]]]) refk[grp[a]] = a;
            for (a = 0; a < nsel; a++) if (refk[grp[a]] != a) nb++;
            if (nb < tc->min_fix_sats) break;
            D = calloc((size_t)nb * kf->namb, sizeof(double));
            af = calloc(nb, sizeof(double));
            Q = calloc((size_t)nb * nb, sizeof(double));
            F2 = calloc((size_t)nb * 2, sizeof(double));
            PND = calloc((size_t)kf->namb * nb, sizeof(double));
            DPa = NULL;
            for (a = 0, b = 0; a < nsel; a++) {
                int r0 = refk[grp[a]];
                if (r0 == a) continue;
                D[b * kf->namb + sel[a]] = 1.0;
                D[b * kf->namb + sel[r0]] = -1.0;
                af[b] = kf->amb[sel[a]] - kf->amb[sel[r0]];
                b++;
            }
            /* Q = D Pnn D' */
            for (a = 0; a < kf->namb; a++) for (b = 0; b < nb; b++) {
                double v = 0; int q2;
                for (q2 = 0; q2 < kf->namb; q2++) if (D[b * kf->namb + q2] != 0) v += PP_(kf, NC + a, NC + q2) * D[b * kf->namb + q2];
                PND[a * nb + b] = v;
            }
            for (a = 0; a < nb; a++) for (b = 0; b < nb; b++) {
                double v = 0; int q2;
                for (q2 = 0; q2 < kf->namb; q2++) if (D[a * kf->namb + q2] != 0) v += D[a * kf->namb + q2] * PND[q2 * nb + b];
                Q[a * nb + b] = v;
            }
            if (!lambda(nb, 2, af, Q, F2, s2) && s2[0] > 0) {
                double ratio = s2[1] / s2[0];
                if (ratio > 999.9) ratio = 999.9;
                *ratio_out = ratio;
                if (ratio >= tc->ar_ratio) {
                    /* Hold only after two passing epochs in a row, and only
                     * when the fix adds something new; a fix that contradicts
                     * ambiguities already held releases them instead. */
                    int newinfo = 0, clash = 0, q2;
                    passed = 1;
                    for (a = 0; a < nb; a++) {
                        int involves_new = 0;
                        for (q2 = 0; q2 < kf->namb; q2++)
                            if (D[a * kf->namb + q2] != 0 && !kf->amb_fixed[q2]) involves_new = 1;
                        if (involves_new) newinfo = 1;
                        else if (fabs(F2[a] - af[a]) > 0.25) clash = 1;
                    }
                    if (clash) {
                        for (q2 = 0; q2 < kf->namb; q2++) if (kf->amb_fixed[q2]) {
                            kf->amb_fixed[q2] = 0;
                            PP_(kf, NC + q2, NC + q2) += 1.0;
                        }
                        tc->n_release++;
                        passed = 0;
                    } else if (tc->fix_streak + 1 >= 2 && newinfo) {
                        double *Hf = calloc((size_t)nb * kf->n, sizeof(double));
                        double *Rf = calloc((size_t)nb * nb, sizeof(double));
                        double *yf = calloc(nb, sizeof(double));
                        for (a = 0; a < nb; a++) {
                            for (q2 = 0; q2 < kf->namb; q2++) Hf[a * kf->n + NC + q2] = -D[a * kf->namb + q2];
                            Rf[a * nb + a] = tc->hold_var;
                            yf[a] = F2[a] - af[a];
                        }
                        if (!kf_update(kf, Hf, yf, Rf, nb)) {
                            ins_feedback(s, kf);
                            for (a = 0; a < nb; a++) for (q2 = 0; q2 < kf->namb; q2++)
                                if (D[a * kf->namb + q2] != 0) kf->amb_fixed[q2] = 1;
                            *status = PPST_FIX;
                        }
                        free(Hf); free(Rf); free(yf);
                    } else if (tc->fix_streak + 1 >= 2) {
                        *status = PPST_FIX;      /* agrees with what is held */
                    }
                }
            }
            free(D); free(af); free(Q); free(F2); free(PND); free(DPa);
            if (passed) break;
            /* partial fixing. First drop the second frequency (a rover L2C
             * against a base L2P(Y) can differ by a quarter cycle), then the
             * lowest satellite, and retry */
            if (attempt == 0) {
                int k2 = 0, had = 0;
                for (a = 0; a < nsel; a++) {
                    if (grp[a] % MAXF) { had = 1; continue; }
                    sel[k2] = sel[a]; elv[k2] = elv[a]; grp[k2] = grp[a]; k2++;
                }
                if (had && k2 >= tc->min_fix_sats + 1) { nsel = k2; continue; }
            }
            {
                int lowest = 0;
                for (a = 1; a < nsel; a++) if (elv[a] < elv[lowest]) lowest = a;
                sel[lowest] = sel[nsel - 1]; elv[lowest] = elv[nsel - 1]; grp[lowest] = grp[nsel - 1];
                nsel--;
            }
        }
        tc->fix_streak = passed ? tc->fix_streak + 1 : 0;
        /* fix validation against the code */
        if (tc->cb_tau > 0 && tc->cb_age >= tc->cb_tau) {
            int nfx = 0;
            double n3 = norm3(tc->cb), sd = sqrt(tc->cb_var[0] + tc->cb_var[1] + tc->cb_var[2]);
            for (k = 0; k < kf->namb; k++) nfx += kf->amb_fixed[k];
            if (nfx > 0 && n3 > tc->cb_thr && n3 > 5.0 * sd) {
                for (i = 0; i < 3; i++) {
                    PP_(kf, IR + i, IR + i) += n3 * n3 + 0.25;
                    PP_(kf, IV + i, IV + i) += 0.04;
                }
                for (k = kf->namb - 1; k >= 0; k--) if (kf->amb_f[k] < MAXF) kf_del_amb(kf, k);
                pp_warn("GPS %.1f: fixed solution was %.2f m from the pseudoranges for %.0f s; ambiguities re-solved",
                        t, n3, tc->cb_age);
                memset(tc->cb, 0, sizeof(tc->cb)); memset(tc->cb_var, 0, sizeof(tc->cb_var));
                tc->cb_age = 0;
                tc->fix_streak = 0;
                tc->n_cbreset++;
                *status = PPST_FLOAT;
            }
        }
        if (*status == PPST_FIX) tc->n_fix++; else tc->n_float++;
    }

    /* remember residuals (with the updated position) for the next slip test */
    {
        double ra_post[3], dra[3];
        ins_lever_pos(s, lever, ra_post);
        for (i = 0; i < 3; i++) dra[i] = (ra_post[i] + s->v[i] * dtm) - ra_pre[i];
        for (k = 0; k < nc; k++) {
            slip_t *sl = &tc->slip[rx][c[k].sat - 1][c[k].f];
            if (c[k].ok_phase) {
                sl->q_last = c[k].sdphi + dot3(c[k].e, dra);
                sl->t_last = t;
                sl->base_arc = c[k].arc;
                sl->valid = 1;
            } else sl->valid = 0;
        }
    }
    tc->last_t[rx] = t;
    if (tc->lev_est) {
        int a, ia;
        for (a = 0; a < 3; a++) if ((ia = kf_find_amb(kf, rx, LEV_X + a)) >= 0) {
            tc->lev_val[rx][a] = kf->amb[ia];
            tc->lev_sd[rx][a] = sqrt(PP_(kf, NC + ia, NC + ia));
        }
        tc->lev_have[rx] = 1;
    }
    (void)dir;
    free(c); free(H); free(y); free(R);
    return nsat_used;
}
