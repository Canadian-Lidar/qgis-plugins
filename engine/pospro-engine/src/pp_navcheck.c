/* PosPro engine: navigation (broadcast ephemeris) file check.
 *
 *   pospro-engine navcheck --out DIR --ref rover.nav [--ref brdc.nav ...] file1.nav [file2.nav ...]
 *
 * Every navigation file that did not come from the INS itself or from the
 * IGS broadcast product (a base station's .nav/.26P, files the user added)
 * is checked orbit by orbit before processing uses it. A single wrong
 * ephemeris in such a file makes every measurement of that satellite look
 * wrong, and the whole trajectory can run away (field test 2026-11-05).
 *
 * For each ephemeris of an input file:
 *   - the satellite position and clock are computed at its Toe and +-1 h
 *     (GLONASS: +-15 min) and compared with the reference ephemeris of the
 *     same satellite nearest in time; more than LIM_POS_M of position or
 *     LIM_CLK_M of clock (as range) drops it;
 *   - without a reference for that satellite, it is kept only if the
 *     orbit radius is a GNSS orbit and Toe and Toc agree (a wrong week).
 * The kept ephemerides are written to DIR/navcheck_N.rnx (RINEX 3.04),
 * which processing then uses in place of the original file.
 *
 * RESULT nav<N>_in=<file>, nav<N>_out=<file|"">, nav<N>_checked, nav<N>_dropped,
 *        nav<N>_unchecked, nav<N>_dropped_sats (comma list)
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include "pp.h"
#include "rtklib.h"

#define LIM_POS_M   30.0
#define LIM_CLK_M   30.0
#define MAXIN       64

static int read_nav(const char *path, nav_t *nav)
{
    obs_t obs = {0};
    sta_t sta;
    int r;
    memset(&sta, 0, sizeof(sta));
    r = readrnx(path, 1, "", &obs, nav, &sta);
    free(obs.data);
    return r >= 0 && (nav->n > 0 || nav->ng > 0);
}

static double maxdtoe(int sys)
{
    if (sys == SYS_GAL) return 14400.0;
    if (sys == SYS_CMP) return 21600.0;
    if (sys == SYS_QZS) return 7200.0;
    return 7200.0;
}

/* nearest reference ephemeris of the same satellite valid at t */
static const eph_t *ref_eph(const nav_t *const *refs, int nref, int sat, gtime_t t)
{
    const eph_t *best = NULL;
    double bd = 1e18;
    int r, i, sys = satsys(sat, NULL);
    for (r = 0; r < nref; r++) {
        for (i = 0; i < refs[r]->n; i++) {
            const eph_t *e = refs[r]->eph + i;
            double d;
            if (e->sat != sat || e->svh) continue;
            d = fabs(timediff(t, e->toe));
            if (d <= maxdtoe(sys) && d < bd) { bd = d; best = e; }
        }
    }
    return best;
}

static const geph_t *ref_geph(const nav_t *const *refs, int nref, int sat, gtime_t t)
{
    const geph_t *best = NULL;
    double bd = 1e18;
    int r, i;
    for (r = 0; r < nref; r++) {
        for (i = 0; i < refs[r]->ng; i++) {
            const geph_t *e = refs[r]->geph + i;
            double d;
            if (e->sat != sat || e->svh) continue;
            d = fabs(timediff(t, e->toe));
            if (d <= 1800.0 && d < bd) { bd = d; best = e; }
        }
    }
    return best;
}

static int plausible_radius(const double *rs)
{
    double r = sqrt(rs[0] * rs[0] + rs[1] * rs[1] + rs[2] * rs[2]);
    return r > 2.0e7 && r < 4.5e7;
}

/* 1 keep, 0 drop; *checked=1 when compared with a reference */
static int check_eph(const eph_t *e, const nav_t *const *refs, int nref, int *checked, double *worst)
{
    static const double dt[3] = {0.0, -3600.0, 3600.0};
    int k, sys = satsys(e->sat, NULL);
    *checked = 0;
    *worst = 0.0;
    if (fabs(timediff(e->toe, e->toc)) > 86400.0) return 0;      /* Toe and Toc a week (or more) apart */
    for (k = 0; k < 3; k++) {
        gtime_t t = timeadd(e->toe, sys == SYS_CMP ? dt[k] / 2 : dt[k]);
        double rs[6], dts[2], var, rs2[6], dts2[2], var2, d, c;
        const eph_t *r;
        eph2pos(t, e, rs, dts, &var);
        if (!plausible_radius(rs)) return 0;
        if (!(r = ref_eph(refs, nref, e->sat, t))) continue;
        eph2pos(t, r, rs2, dts2, &var2);
        d = sqrt((rs[0] - rs2[0]) * (rs[0] - rs2[0]) + (rs[1] - rs2[1]) * (rs[1] - rs2[1]) +
                 (rs[2] - rs2[2]) * (rs[2] - rs2[2]));
        c = fabs(dts[0] - dts2[0]) * CLIGHT;
        *checked = 1;
        if (d > *worst) *worst = d;
        if (c > *worst) *worst = c;
        if (d > LIM_POS_M || c > LIM_CLK_M) return 0;
    }
    return 1;
}

static int check_geph(const geph_t *e, const nav_t *const *refs, int nref, int *checked, double *worst)
{
    static const double dt[3] = {0.0, -900.0, 900.0};
    int k;
    *checked = 0;
    *worst = 0.0;
    for (k = 0; k < 3; k++) {
        gtime_t t = timeadd(e->toe, dt[k]);
        double rs[6], dts[2], var, rs2[6], dts2[2], var2, d, c;
        const geph_t *r;
        geph2pos(t, e, rs, dts, &var);
        if (!plausible_radius(rs)) return 0;
        if (!(r = ref_geph(refs, nref, e->sat, t))) continue;
        geph2pos(t, r, rs2, dts2, &var2);
        d = sqrt((rs[0] - rs2[0]) * (rs[0] - rs2[0]) + (rs[1] - rs2[1]) * (rs[1] - rs2[1]) +
                 (rs[2] - rs2[2]) * (rs[2] - rs2[2]));
        c = fabs(dts[0] - dts2[0]) * CLIGHT;
        *checked = 1;
        if (d > *worst) *worst = d;
        if (c > *worst) *worst = c;
        if (d > LIM_POS_M || c > LIM_CLK_M) return 0;
    }
    return 1;
}

static void add_sat(char *list, size_t n, int sat)
{
    char id[8];
    satno2id(sat, id);
    if (strstr(list, id)) return;
    if (*list && strlen(list) + 2 < n) strcat(list, ",");
    if (strlen(list) + strlen(id) + 1 < n) strcat(list, id);
}

int cmd_navcheck(int argc, char **argv)
{
    const char *outdir = NULL, *refp[MAXIN], *inp[MAXIN];
    int nref = 0, nin = 0, i, j, f;
    nav_t *refnav = calloc(MAXIN, sizeof(nav_t)), *navp = calloc(1, sizeof(nav_t));
    const nav_t *refs[MAXIN];
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--out") && i + 1 < argc) outdir = argv[++i];
        else if (!strcmp(argv[i], "--ref") && i + 1 < argc) { if (nref < MAXIN) refp[nref++] = argv[++i]; else i++; }
        else if (nin < MAXIN) inp[nin++] = argv[i];
    }
    if (!refnav || !navp) return pp_fail("out of memory");
    if (!outdir || !nin) return pp_fail("usage: navcheck --out DIR --ref ref.nav [...] file.nav [...]");
    j = 0;
    for (i = 0; i < nref; i++) {
        memset(refnav + j, 0, sizeof(nav_t));
        if (read_nav(refp[i], refnav + j)) { refs[j] = refnav + j; j++; }
        else pp_warn("reference navigation file %s could not be read", refp[i]);
    }
    nref = j;
    for (f = 0; f < nin; f++) {
        nav_t *nv = navp;
        char key[64], out[1100], dropped[2048] = "";
        int nchk = 0, ndrop = 0, nunchk = 0, keep;
        double worst_all = 0.0;
        FILE *fp;
        rnxopt_t opt;
        memset(nv, 0, sizeof(nav_t));
        snprintf(key, sizeof(key), "nav%d_in", f);
        pp_result(key, "%s", inp[f]);
        snprintf(out, sizeof(out), "%s/navcheck_%d.rnx", outdir, f);
        if (!read_nav(inp[f], nv)) {
            pp_warn("navigation file %s could not be read: it is not used", inp[f]);
            snprintf(key, sizeof(key), "nav%d_out", f);
            pp_result(key, "%s", "");
            freenav(nv, 0xFF);
            continue;
        }
        if (!(fp = fopen(out, "w"))) { freenav(nv, 0xFF); return pp_fail("cannot write %s", out); }
        memset(&opt, 0, sizeof(opt));
        opt.rnxver = 304;
        opt.navsys = SYS_ALL;
        strcpy(opt.prog, "PosPro navcheck");
        outrnxnavh(fp, &opt, nv);
        for (i = 0; i < nv->n; i++) {
            int chk;
            double w;
            keep = check_eph(nv->eph + i, refs, nref, &chk, &w);
            if (chk) nchk++; else nunchk++;
            if (!keep) { ndrop++; add_sat(dropped, sizeof(dropped), nv->eph[i].sat); if (w > worst_all) worst_all = w; continue; }
            outrnxnavb(fp, &opt, nv->eph + i);
        }
        for (i = 0; i < nv->ng; i++) {
            int chk;
            double w;
            keep = check_geph(nv->geph + i, refs, nref, &chk, &w);
            if (chk) nchk++; else nunchk++;
            if (!keep) { ndrop++; add_sat(dropped, sizeof(dropped), nv->geph[i].sat); if (w > worst_all) worst_all = w; continue; }
            outrnxgnavb(fp, &opt, nv->geph + i);
        }
        fclose(fp);
        snprintf(key, sizeof(key), "nav%d_out", f); pp_result(key, "%s", out);
        snprintf(key, sizeof(key), "nav%d_checked", f); pp_result(key, "%d", nchk);
        snprintf(key, sizeof(key), "nav%d_dropped", f); pp_result(key, "%d", ndrop);
        snprintf(key, sizeof(key), "nav%d_unchecked", f); pp_result(key, "%d", nunchk);
        snprintf(key, sizeof(key), "nav%d_dropped_sats", f); pp_result(key, "%s", dropped);
        snprintf(key, sizeof(key), "nav%d_worst_m", f); pp_result(key, "%.1f", worst_all);
        freenav(nv, 0xFF);
    }
    for (i = 0; i < nref; i++) freenav(refnav + i, 0xFF);
    free(refnav); free(navp);
    return 0;
}
