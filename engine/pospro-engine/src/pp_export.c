/* PosPro engine: "export" — write the final trajectory as SBET / SMRMSG or
 * a configurable text file, at a chosen rate and output point, optionally
 * with projected coordinates (UTM, transverse Mercator or oblique
 * stereographic such as EPSG:2953 New Brunswick).
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "pp.h"

/* ------------------------------------------------------------------------ */
/* projections                                                               */
/* ------------------------------------------------------------------------ */
typedef struct {
    int type;                /* 0 none, 1 tmerc, 2 sterea */
    double a, f, lat0, lon0, k0, x0, y0;
    /* tmerc */
    double n, A, alpha[5], xi0;
    /* sterea */
    double e, R, nn, c, chi0;
} proj_t;

static double pval(const char *s, const char *key, double def)
{
    char k[64];
    const char *p;
    snprintf(k, sizeof(k), "+%s=", key);
    p = strstr(s, k);
    return p ? atof(p + strlen(k)) : def;
}

static void tm_xi_eta(const proj_t *P, double lat, double dlon, double *xi, double *eta)
{
    double n = P->n, s = sin(lat), k = 2 * sqrt(n) / (1 + n);
    double t = sinh(atanh(s) - k * atanh(k * s));
    double xp = atan2(t, cos(dlon)), ep = atanh(sin(dlon) / sqrt(1 + t * t));
    int j;
    *xi = xp; *eta = ep;
    for (j = 1; j <= 4; j++) {
        *xi += P->alpha[j] * sin(2 * j * xp) * cosh(2 * j * ep);
        *eta += P->alpha[j] * cos(2 * j * xp) * sinh(2 * j * ep);
    }
}

static int proj_init(proj_t *P, const char *s)
{
    double rf;
    memset(P, 0, sizeof(*P));
    if (!s || !*s) return 1;
    P->a = 6378137.0;
    rf = 298.257222101;            /* GRS80 */
    if (strstr(s, "+ellps=WGS84")) rf = 298.257223563;
    if (strstr(s, "+a=")) P->a = pval(s, "a", P->a);
    if (strstr(s, "+rf=")) rf = pval(s, "rf", rf);
    P->f = 1.0 / rf;
    P->lat0 = pval(s, "lat_0", 0) * D2R;
    P->lon0 = pval(s, "lon_0", 0) * D2R;
    P->k0 = strstr(s, "+k_0=") ? pval(s, "k_0", 1) : pval(s, "k", 1);
    P->x0 = pval(s, "x_0", 0);
    P->y0 = pval(s, "y_0", 0);
    if (strstr(s, "+proj=utm")) {
        int zone = (int)pval(s, "zone", 0);
        if (zone < 1 || zone > 60) return 0;
        P->type = 1;
        P->lon0 = (zone * 6 - 183) * D2R;
        P->lat0 = 0; P->k0 = 0.9996; P->x0 = 500000;
        P->y0 = strstr(s, "+south") ? 10000000 : 0;
    } else if (strstr(s, "+proj=tmerc")) {
        P->type = 1;
    } else if (strstr(s, "+proj=sterea")) {
        P->type = 2;
    } else return 0;
    if (P->type == 1) {
        double n = P->f / (2 - P->f), xi, eta;
        P->n = n;
        P->A = P->a / (1 + n) * (1 + n*n/4 + n*n*n*n/64);
        P->alpha[1] = n/2 - 2*n*n/3 + 5*n*n*n/16 + 41*n*n*n*n/180;
        P->alpha[2] = 13*n*n/48 - 3*n*n*n/5 + 557*n*n*n*n/1440;
        P->alpha[3] = 61*n*n*n/240 - 103*n*n*n*n/140;
        P->alpha[4] = 49561*n*n*n*n/161280;
        tm_xi_eta(P, P->lat0, 0, &xi, &eta);
        P->xi0 = xi;
    } else {
        double e2 = P->f * (2 - P->f), e = sqrt(e2), s0 = sin(P->lat0), c0 = cos(P->lat0);
        double rho0 = P->a * (1 - e2) / pow(1 - e2 * s0 * s0, 1.5), nu0 = P->a / sqrt(1 - e2 * s0 * s0);
        double S1, S2, w1, sinchi0, w2;
        P->e = e;
        P->R = sqrt(rho0 * nu0);
        P->nn = sqrt(1 + e2 * c0 * c0 * c0 * c0 / (1 - e2));
        S1 = (1 + s0) / (1 - s0);
        S2 = (1 - e * s0) / (1 + e * s0);
        w1 = pow(S1 * pow(S2, e), P->nn);
        sinchi0 = (w1 - 1) / (w1 + 1);
        P->c = (P->nn + s0) * (1 - sinchi0) / ((P->nn - s0) * (1 + sinchi0));
        w2 = P->c * w1;
        P->chi0 = asin((w2 - 1) / (w2 + 1));
    }
    return 1;
}

static void proj_fwd(const proj_t *P, double lat, double lon, double *E, double *N)
{
    if (P->type == 1) {
        double xi, eta;
        tm_xi_eta(P, lat, pp_wrap_pi(lon - P->lon0), &xi, &eta);
        *E = P->x0 + P->k0 * P->A * eta;
        *N = P->y0 + P->k0 * P->A * (xi - P->xi0);
    } else if (P->type == 2) {
        double s = sin(lat), e = P->e;
        double L = P->nn * pp_wrap_pi(lon - P->lon0);
        double Sa = (1 + s) / (1 - s), Sb = (1 - e * s) / (1 + e * s);
        double w = P->c * pow(Sa * pow(Sb, e), P->nn);
        double chi = asin((w - 1) / (w + 1));
        double B = 1 + sin(chi) * sin(P->chi0) + cos(chi) * cos(P->chi0) * cos(L);
        *E = P->x0 + 2 * P->R * P->k0 * cos(chi) * sin(L) / B;
        *N = P->y0 + 2 * P->R * P->k0 * (sin(chi) * cos(P->chi0) - cos(chi) * sin(P->chi0) * cos(L)) / B;
    } else { *E = 0; *N = 0; }
}

/* ------------------------------------------------------------------------ */
static void shift_point(trj_rec_t *r, const double *delta_body)
{
    double Cbn[9], d[3], sl = sin(r->lat);
    double e2 = PP_WGS84_E2, a = PP_WGS84_A, Nn, M;
    if (delta_body[0] == 0 && delta_body[1] == 0 && delta_body[2] == 0) return;
    pp_euler2C(r->att[0], r->att[1], r->att[2], Cbn);
    m3_vec(Cbn, delta_body, d);
    Nn = a / sqrt(1 - e2 * sl * sl);
    M = a * (1 - e2) / pow(1 - e2 * sl * sl, 1.5);
    r->lat += d[0] / (M + r->h);
    r->lon += d[1] / ((Nn + r->h) * cos(r->lat));
    r->h -= d[2];
}

static void utc_string(double gps, int leap, char *buf, size_t n, int sep_date_time)
{
    double ep[6];
    gtime_t t = timeadd(pp_gps2gtime(gps), -leap);
    time2epoch(t, ep);
    if (sep_date_time)
        snprintf(buf, n, "%04d-%02d-%02d%c%02d:%02d:%06.3f", (int)ep[0], (int)ep[1], (int)ep[2],
                 sep_date_time, (int)ep[3], (int)ep[4], ep[5]);
    else
        snprintf(buf, n, "%04d-%02d-%02dT%02d:%02d:%06.3fZ", (int)ep[0], (int)ep[1], (int)ep[2],
                 (int)ep[3], (int)ep[4], ep[5]);
}

int cmd_export(int argc, char **argv)
{
    const char *in = NULL, *out = NULL, *fmt = "csv", *cols = NULL, *projs = NULL, *sep = ",";
    double rate = 0, lever[3] = {0, 0, 0}, ts = 0, te = 0, next = -1;
    int have_lever = 0, leap = 18, header = 1, i;
    FILE *fi, *fo;
    trj_hdr_t h;
    trj_rec_t r;
    proj_t P;
    long long n = 0, w = 0;
    char colbuf[2048], *colv[64];
    int ncol = 0;
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--in") && i + 1 < argc) in = argv[++i];
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
        else if (!strcmp(argv[i], "--format") && i + 1 < argc) fmt = argv[++i];
        else if (!strcmp(argv[i], "--rate") && i + 1 < argc) rate = atof(argv[++i]);
        else if (!strcmp(argv[i], "--lever") && i + 3 < argc) {
            lever[0] = atof(argv[++i]); lever[1] = atof(argv[++i]); lever[2] = atof(argv[++i]); have_lever = 1;
        } else if (!strcmp(argv[i], "--ts") && i + 1 < argc) ts = atof(argv[++i]);
        else if (!strcmp(argv[i], "--te") && i + 1 < argc) te = atof(argv[++i]);
        else if (!strcmp(argv[i], "--cols") && i + 1 < argc) cols = argv[++i];
        else if (!strcmp(argv[i], "--proj") && i + 1 < argc) projs = argv[++i];
        else if (!strcmp(argv[i], "--leap") && i + 1 < argc) leap = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--sep") && i + 1 < argc) { sep = argv[++i]; if (!strcmp(sep, "tab")) sep = "\t"; else if (!strcmp(sep, "space")) sep = " "; }
        else if (!strcmp(argv[i], "--no-header")) header = 0;
    }
    if (!in || !out) return pp_fail("usage: export --in traj.bin --out file --format sbet|smrmsg|csv");
    if (!proj_init(&P, projs)) return pp_fail("unsupported projection: %s (use UTM, tmerc or sterea)", projs);
    if (!(fi = fopen(in, "rb"))) return pp_fail("cannot read %s", in);
    if (fread(&h, sizeof(h), 1, fi) != 1 || memcmp(h.magic, TRJ_MAGIC, 8)) { fclose(fi); return pp_fail("%s is not a trajectory file", in); }
    if (!(fo = fopen(out, !strcmp(fmt, "csv") ? "w" : "wb"))) { fclose(fi); return pp_fail("cannot write %s", out); }
    if (!cols) cols = "time_sow,lat,lon,h,roll,pitch,heading,sd_n,sd_e,sd_d,status";
    snprintf(colbuf, sizeof(colbuf), "%s", cols);
    {
        char *p = colbuf, *tok;
        while ((tok = strtok(p, ",")) && ncol < 64) { colv[ncol++] = tok; p = NULL; }
    }
    if (!strcmp(fmt, "csv") && header) {
        for (i = 0; i < ncol; i++) {
            if (!strcmp(colv[i], "date_time_utc")) fprintf(fo, "%sdate_utc%stime_utc", i ? sep : "", sep);
            else fprintf(fo, "%s%s", i ? sep : "", colv[i]);
        }
        fprintf(fo, "\n");
    }
    {
        double delta[3];
        for (i = 0; i < 3; i++) delta[i] = have_lever ? lever[i] - h.lever_out[i] : 0;
        while (fread(&r, sizeof(r), 1, fi) == 1) {
            n++;
            if (ts > 0 && r.t < ts) continue;
            if (te > 0 && r.t > te) break;
            if (rate > 0) {
                double step = 1.0 / rate;
                if (next < 0) next = ceil(r.t / step - 1e-6) * step;
                if (r.t < next - 1e-6) continue;
                next += step;
                while (next <= r.t + 1e-9) next += step;
            }
            shift_point(&r, delta);
            if (!strcmp(fmt, "sbet")) {
                double v[17];
                double sow = r.t - floor(r.t / 604800.0) * 604800.0;
                v[0] = sow; v[1] = r.lat; v[2] = r.lon; v[3] = r.h;
                v[4] = r.vel[0]; v[5] = r.vel[1]; v[6] = r.vel[2];
                v[7] = r.att[0]; v[8] = r.att[1]; v[9] = r.att[2]; v[10] = 0.0;
                v[11] = r.acc[0]; v[12] = r.acc[1]; v[13] = r.acc[2];
                v[14] = r.gyr[0]; v[15] = r.gyr[1]; v[16] = r.gyr[2];
                fwrite(v, sizeof(double), 17, fo);
            } else if (!strcmp(fmt, "smrmsg")) {
                double v[10];
                double sow = r.t - floor(r.t / 604800.0) * 604800.0;
                v[0] = sow;
                v[1] = r.sdp[0]; v[2] = r.sdp[1]; v[3] = r.sdp[2];
                v[4] = r.sdv[0]; v[5] = r.sdv[1]; v[6] = r.sdv[2];
                v[7] = r.sda[0] * R2D * 60; v[8] = r.sda[1] * R2D * 60; v[9] = r.sda[2] * R2D * 60;
                fwrite(v, sizeof(double), 10, fo);
            } else {
                double E = 0, N = 0;
                if (P.type) proj_fwd(&P, r.lat, r.lon, &E, &N);
                for (i = 0; i < ncol; i++) {
                    const char *c = colv[i];
                    char b[64];
                    int week = (int)floor(r.t / 604800.0);
                    if (i) fputs(sep, fo);
                    if (!strcmp(c, "time_sow")) fprintf(fo, "%.4f", r.t - week * 604800.0);
                    else if (!strcmp(c, "week")) fprintf(fo, "%d", week);
                    else if (!strcmp(c, "time_gps")) fprintf(fo, "%.4f", r.t);
                    else if (!strcmp(c, "time_utc")) { utc_string(r.t, leap, b, sizeof(b), 0); fputs(b, fo); }
                    else if (!strcmp(c, "date_time_utc")) { utc_string(r.t, leap, b, sizeof(b), sep[0]); fputs(b, fo); }
                    else if (!strcmp(c, "unix")) fprintf(fo, "%.4f", r.t + GPS_EPOCH_UNIX - leap);
                    else if (!strcmp(c, "lat")) fprintf(fo, "%.10f", r.lat * R2D);
                    else if (!strcmp(c, "lon")) fprintf(fo, "%.10f", r.lon * R2D);
                    else if (!strcmp(c, "h")) fprintf(fo, "%.4f", r.h);
                    else if (!strcmp(c, "easting")) fprintf(fo, "%.4f", E);
                    else if (!strcmp(c, "northing")) fprintf(fo, "%.4f", N);
                    else if (!strcmp(c, "roll")) fprintf(fo, "%.5f", r.att[0] * R2D);
                    else if (!strcmp(c, "pitch")) fprintf(fo, "%.5f", r.att[1] * R2D);
                    else if (!strcmp(c, "heading")) fprintf(fo, "%.5f", r.att[2] * R2D);
                    else if (!strcmp(c, "vn")) fprintf(fo, "%.4f", r.vel[0]);
                    else if (!strcmp(c, "ve")) fprintf(fo, "%.4f", r.vel[1]);
                    else if (!strcmp(c, "vd")) fprintf(fo, "%.4f", r.vel[2]);
                    else if (!strcmp(c, "speed")) fprintf(fo, "%.4f", sqrt((double)r.vel[0]*r.vel[0] + (double)r.vel[1]*r.vel[1]));
                    else if (!strcmp(c, "sd_n")) fprintf(fo, "%.4f", r.sdp[0]);
                    else if (!strcmp(c, "sd_e")) fprintf(fo, "%.4f", r.sdp[1]);
                    else if (!strcmp(c, "sd_d")) fprintf(fo, "%.4f", r.sdp[2]);
                    else if (!strcmp(c, "sd_roll")) fprintf(fo, "%.5f", r.sda[0] * R2D);
                    else if (!strcmp(c, "sd_pitch")) fprintf(fo, "%.5f", r.sda[1] * R2D);
                    else if (!strcmp(c, "sd_heading")) fprintf(fo, "%.5f", r.sda[2] * R2D);
                    else if (!strcmp(c, "status")) fprintf(fo, "%d", r.status);
                    else if (!strcmp(c, "nsat")) fprintf(fo, "%d", r.nsat);
                    else if (!strcmp(c, "sep_h")) fprintf(fo, "%.4f", sqrt((double)r.sep[0]*r.sep[0] + (double)r.sep[1]*r.sep[1]));
                    else if (!strcmp(c, "sep_v")) fprintf(fo, "%.4f", fabs(r.sep[2]));
                    else if (!strcmp(c, "acc_x")) fprintf(fo, "%.5f", r.acc[0]);
                    else if (!strcmp(c, "acc_y")) fprintf(fo, "%.5f", r.acc[1]);
                    else if (!strcmp(c, "acc_z")) fprintf(fo, "%.5f", r.acc[2]);
                    else if (!strcmp(c, "gyro_x")) fprintf(fo, "%.7f", r.gyr[0]);
                    else if (!strcmp(c, "gyro_y")) fprintf(fo, "%.7f", r.gyr[1]);
                    else if (!strcmp(c, "gyro_z")) fprintf(fo, "%.7f", r.gyr[2]);
                    else fputs("", fo);
                }
                fputc('\n', fo);
            }
            w++;
            if ((w & 65535) == 0) pp_progress(h.count ? (double)n / h.count : 0, "exporting");
        }
    }
    fclose(fi);
    fclose(fo);
    pp_result("written", "%lld", w);
    pp_progress(1.0, "done");
    return 0;
}

