/* PosPro engine: "gnss" — GNSS-only processing with RTKLIB postpos.
 *
 *   gnss -k options.conf -o out.pos [-d fwd|bwd|comb] [--base X Y Z]
 *        [--ts GPSSEC] [--te GPSSEC] rover.obs [base.obs] nav... [sp3 clk ...]
 *
 * The options file uses RTKLIB's own keys, so every RTKLIB setting is
 * available to advanced users. Output is always ECEF with GPS week/seconds
 * and velocity, which the INS pass reads.
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "pp.h"

static gtime_t g_ts, g_te, g_first;
static int g_have_first;

/* RTKLIB callbacks */
int showmsg(const char *format, ...)
{
    (void)format;
    return 0;
}

void settspan(gtime_t ts, gtime_t te)
{
    g_ts = ts; g_te = te; g_have_first = 0;
}

void settime(gtime_t time)
{
    double span = timediff(g_te, g_ts), d;
    if (span <= 0) return;
    if (!g_have_first) { g_first = time; g_have_first = 1; }
    d = fabs(timediff(time, g_first)) / span;
    pp_progress(d > 1 ? 1 : d, "GNSS %.0f%%", (d > 1 ? 1 : d) * 100);
}

int cmd_gnss(int argc, char **argv)
{
    prcopt_t popt = prcopt_default;
    solopt_t sopt = solopt_default;
    filopt_t fopt = {""};
    gtime_t ts = {0}, te = {0};
    const char *infile[64], *outfile = "", *conf = NULL;
    int i, n = 0, dir = 1, have_base = 0, ret, enu = 0;
    double rb[3] = {0};

    for (i = 0; i < argc; i++) if (!strcmp(argv[i], "-k") && i + 1 < argc) conf = argv[i + 1];
    if (conf) {
        resetsysopts();
        if (!loadopts(conf, sysopts)) return pp_fail("cannot read GNSS options %s", conf);
        getsysopts(&popt, &sopt, &fopt);
    }
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "-k") && i + 1 < argc) { i++; continue; }
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) outfile = argv[++i];
        else if (!strcmp(argv[i], "--enu")) enu = 1;       /* baseline E N U from the base (moving base) */
        else if (!strcmp(argv[i], "-d") && i + 1 < argc) {
            const char *d = argv[++i];
            dir = !strcmp(d, "bwd") ? -1 : !strcmp(d, "comb") ? 0 : 1;
        } else if (!strcmp(argv[i], "--base") && i + 3 < argc) {
            rb[0] = atof(argv[++i]); rb[1] = atof(argv[++i]); rb[2] = atof(argv[++i]);
            have_base = 1;
        } else if (!strcmp(argv[i], "--ts") && i + 1 < argc) ts = pp_gps2gtime(atof(argv[++i]));
        else if (!strcmp(argv[i], "--te") && i + 1 < argc) te = pp_gps2gtime(atof(argv[++i]));
        else if (n < 64) infile[n++] = argv[i];
    }
    if (!*outfile || n < 1) return pp_fail("usage: gnss -k conf -o out.pos rover.obs [base.obs] nav ...");
    popt.soltype = dir > 0 ? 0 : (dir < 0 ? 1 : 2);
    if (have_base) {
        popt.refpos = POSOPT_POS_XYZ;
        memcpy(popt.rb, rb, sizeof(rb));
    }
    if (!popt.navsys) popt.navsys = SYS_GPS | SYS_GLO | SYS_GAL | SYS_QZS;
    /* fixed output form read by the INS pass */
    sopt.posf = enu ? SOLF_ENU : SOLF_XYZ;
    sopt.timef = 0;
    sopt.times = TIMES_GPST;
    sopt.outhead = 1;
    sopt.outopt = 1;
    sopt.outvel = 1;
    sopt.sstat = 0;
    sopt.trace = 0;
    strcpy(sopt.sep, " ");
    snprintf(sopt.prog, sizeof(sopt.prog), "PosPro %s / RTKLIB %s %s", PP_VERSION, VER_RTKLIB, PATCH_LEVEL);
    pp_progress(0, "GNSS processing");
    ret = postpos(ts, te, 0.0, 0.0, &popt, &sopt, &fopt, infile, n, outfile, "", "");
    if (ret) return pp_fail("GNSS processing failed (RTKLIB code %d): check the input files", ret);
    {
        gsolbuf_t b;
        int nq[8] = {0};
        if (!enu && read_pos_file(outfile, &b)) {
            for (i = 0; i < b.n; i++) if (b.s[i].q >= 0 && b.s[i].q < 8) nq[b.s[i].q]++;
            pp_result("solutions", "%d", b.n);
            pp_result("fixed", "%d", nq[1]);
            pp_result("float", "%d", nq[2]);
            pp_result("dgps", "%d", nq[4]);
            pp_result("single", "%d", nq[5]);
            pp_result("ppp", "%d", nq[6]);
            if (b.n > 0) {
                gsol_t *l = &b.s[b.n - 1];
                pp_result("last_xyz", "%.4f,%.4f,%.4f", l->r[0], l->r[1], l->r[2]);
                pp_result("last_sd", "%.4f,%.4f,%.4f", sqrt(l->Q[0]), sqrt(l->Q[1]), sqrt(l->Q[2]));
                pp_result("last_q", "%d", l->q);
            }
            free_gsol(&b);
        }
    }
    pp_progress(1.0, "done");
    return 0;
}
