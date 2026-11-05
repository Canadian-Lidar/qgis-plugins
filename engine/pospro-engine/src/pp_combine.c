/* PosPro engine: "combine" — merge the forward and backward passes, weighted
 * by their covariances (two-filter smoother), and write the final trajectory,
 * a 1 Hz quality-control table for the plugin and a summary.
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "pp.h"

typedef struct {
    FILE *fp;
    trj_hdr_t h;
    trj_rec_t *blk;
    long long n, pos, b0, bn;    /* pos = next logical index */
    int reverse;                 /* read the file from its end */
} trjreader_t;

#define RBLK 32768

static int tr_open(trjreader_t *r, const char *path, int reverse)
{
    memset(r, 0, sizeof(*r));
    if (!(r->fp = fopen(path, "rb"))) return 0;
    if (fread(&r->h, sizeof(r->h), 1, r->fp) != 1 || memcmp(r->h.magic, TRJ_MAGIC, 8)) {
        fclose(r->fp); r->fp = NULL; return 0;
    }
    r->n = (long long)r->h.count;
    r->reverse = reverse;
    r->blk = malloc(sizeof(trj_rec_t) * RBLK);
    r->b0 = -1;
    return 1;
}

static void seek_rec(FILE *fp, long long i)
{
    long long off = (long long)sizeof(trj_hdr_t) + i * (long long)sizeof(trj_rec_t);
#ifdef _WIN32
    _fseeki64(fp, off, SEEK_SET);
#else
    fseeko(fp, (off_t)off, SEEK_SET);
#endif
}

/* logical index i (0 = earliest time) */
static const trj_rec_t *tr_get(trjreader_t *r, long long i)
{
    long long phys;
    if (!r->fp || i < 0 || i >= r->n) return NULL;
    phys = r->reverse ? r->n - 1 - i : i;
    if (r->b0 < 0 || phys < r->b0 || phys >= r->b0 + r->bn) {
        long long s = (phys / RBLK) * RBLK;
        seek_rec(r->fp, s);
        r->bn = (long long)fread(r->blk, sizeof(trj_rec_t), RBLK, r->fp);
        r->b0 = s;
        if (phys >= r->b0 + r->bn) return NULL;
    }
    return &r->blk[phys - r->b0];
}

static void tr_close(trjreader_t *r) { if (r->fp) fclose(r->fp); free(r->blk); }

static double wsum(double a, double va, double b, double vb, double *v)
{
    double wa = 1.0 / fmax(va, 1e-12), wb = 1.0 / fmax(vb, 1e-12);
    *v = 1.0 / (wa + wb);
    return (a * wa + b * wb) * (*v);
}

static void combine_rec(const trj_rec_t *f, const trj_rec_t *b, trj_rec_t *o)
{
    double M, Nn, sl = sin(f->lat), d[3], v;
    double e2 = PP_WGS84_E2, a = PP_WGS84_A, w;
    int i;
    *o = *f;
    Nn = a / sqrt(1 - e2 * sl * sl);
    M = a * (1 - e2) / pow(1 - e2 * sl * sl, 1.5);
    d[0] = (b->lat - f->lat) * (M + f->h);
    d[1] = (b->lon - f->lon) * (Nn + f->h) * cos(f->lat);
    d[2] = -(b->h - f->h);
    for (i = 0; i < 3; i++) {
        double vf = (double)f->sdp[i] * f->sdp[i], vb = (double)b->sdp[i] * b->sdp[i];
        w = vf / fmax(vf + vb, 1e-12);       /* weight of the backward pass */
        o->sdp[i] = (float)sqrt(vf * vb / fmax(vf + vb, 1e-12));
        if (i == 0) o->lat = f->lat + w * d[0] / (M + f->h);
        if (i == 1) o->lon = f->lon + w * d[1] / ((Nn + f->h) * cos(f->lat));
        if (i == 2) o->h = f->h - w * d[2];
        o->sep[i] = (float)d[i];
        o->vel[i] = (float)wsum(f->vel[i], (double)f->sdv[i] * f->sdv[i], b->vel[i], (double)b->sdv[i] * b->sdv[i], &v);
        o->sdv[i] = (float)sqrt(v);
        {
            double da = pp_wrap_pi((double)b->att[i] - f->att[i]);
            double vf2 = (double)f->sda[i] * f->sda[i], vb2 = (double)b->sda[i] * b->sda[i];
            double wa = vf2 / fmax(vf2 + vb2, 1e-18);
            double at = f->att[i] + wa * da;
            if (i == 2) { while (at < 0) at += 2 * PI; while (at >= 2 * PI) at -= 2 * PI; }
            o->att[i] = (float)at;
            o->sda[i] = (float)sqrt(vf2 * vb2 / fmax(vf2 + vb2, 1e-18));
        }
        o->ba[i] = 0.5f * (f->ba[i] + b->ba[i]);
        o->bg[i] = 0.5f * (f->bg[i] + b->bg[i]);
    }
    o->status = f->status > b->status ? f->status : b->status;
    o->nsat = f->nsat > b->nsat ? f->nsat : b->nsat;
    o->ratio10 = f->ratio10 > b->ratio10 ? f->ratio10 : b->ratio10;
    o->gap_s = f->gap_s < b->gap_s ? f->gap_s : b->gap_s;
    o->flags = (uint8_t)(f->flags | b->flags);
    if (f->gap_s <= 1.5 || b->gap_s <= 1.5) o->flags &= (uint8_t)~2;
}

static void write_qc_header(FILE *fp)
{
    fprintf(fp, "gps_time,week,sow,lat_deg,lon_deg,h_m,roll_deg,pitch_deg,heading_deg,"
                "vn,ve,vd,speed,sd_n,sd_e,sd_d,sd_roll_deg,sd_pitch_deg,sd_heading_deg,"
                "sep_n,sep_e,sep_d,sep_h,status,fwd_status,bwd_status,nsat,ratio,gap_s,"
                "bax_mg,bay_mg,baz_mg,bgx_dh,bgy_dh,bgz_dh,zupt\n");
}

static void write_qc(FILE *fp, const trj_rec_t *r, int fst, int bst)
{
    int week = (int)floor(r->t / 604800.0);
    double sow = r->t - week * 604800.0;
    double sph = sqrt((double)r->vel[0] * r->vel[0] + (double)r->vel[1] * r->vel[1]);
    double seph = sqrt((double)r->sep[0] * r->sep[0] + (double)r->sep[1] * r->sep[1]);
    fprintf(fp, "%.3f,%d,%.3f,%.9f,%.9f,%.4f,%.4f,%.4f,%.4f,%.3f,%.3f,%.3f,%.3f,"
                "%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f,%d,%d,%d,%d,%.1f,%.2f,"
                "%.4f,%.4f,%.4f,%.3f,%.3f,%.3f,%d\n",
            r->t, week, sow, r->lat * R2D, r->lon * R2D, r->h,
            r->att[0] * R2D, r->att[1] * R2D, r->att[2] * R2D,
            r->vel[0], r->vel[1], r->vel[2], sph,
            r->sdp[0], r->sdp[1], r->sdp[2], r->sda[0] * R2D, r->sda[1] * R2D, r->sda[2] * R2D,
            r->sep[0], r->sep[1], r->sep[2], seph, r->status, fst, bst, r->nsat, r->ratio10 / 10.0,
            r->gap_s > 99999 ? 99999 : r->gap_s,
            r->ba[0] / 9.80665e-3, r->ba[1] / 9.80665e-3, r->ba[2] / 9.80665e-3,
            r->bg[0] * R2D * 3600, r->bg[1] * R2D * 3600, r->bg[2] * R2D * 3600, r->flags & 1);
}

int cmd_combine(int argc, char **argv)
{
    const char *fwd = NULL, *bwd = NULL, *out = NULL, *qc = NULL;
    double qc_dt = 1.0, next_qc = -1;
    trjreader_t F, B;
    int hasF, hasB, i;
    FILE *fo, *fq = NULL;
    trj_hdr_t h;
    long long iF = 0, iB = 0, n = 0;
    long long cnt[8] = {0};
    double sum_sep2 = 0, max_seph = 0, max_sepv = 0, max_sdh = 0, max_sdv = 0;
    long long nsep = 0, n_seph_over[3] = {0};
    double *seph_list = NULL;
    long long nsl = 0, maxsl = 0;
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--fwd") && i + 1 < argc) fwd = argv[++i];
        else if (!strcmp(argv[i], "--bwd") && i + 1 < argc) bwd = argv[++i];
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
        else if (!strcmp(argv[i], "--qc") && i + 1 < argc) qc = argv[++i];
        else if (!strcmp(argv[i], "--qc-interval") && i + 1 < argc) qc_dt = atof(argv[++i]);
    }
    if (!out || (!fwd && !bwd)) return pp_fail("usage: combine --fwd f.bin --bwd b.bin --out traj.bin --qc qc.csv");
    hasF = fwd && tr_open(&F, fwd, 0);
    hasB = bwd && tr_open(&B, bwd, 1);
    if (!hasF && !hasB) return pp_fail("no pass results to combine");
    if (!(fo = fopen(out, "wb"))) return pp_fail("cannot write %s", out);
    if (qc && !(fq = fopen(qc, "w"))) return pp_fail("cannot write %s", qc);
    if (fq) write_qc_header(fq);
    memset(&h, 0, sizeof(h));
    h = hasF ? F.h : B.h;
    h.direction = 0;
    h.count = 0;
    fwrite(&h, sizeof(h), 1, fo);
    for (;;) {
        const trj_rec_t *f = hasF ? tr_get(&F, iF) : NULL;
        const trj_rec_t *b = hasB ? tr_get(&B, iB) : NULL;
        trj_rec_t o;
        int fst = -1, bst = -1;
        if (!f && !b) break;
        if (f && b && fabs(f->t - b->t) < 1e-6) {
            combine_rec(f, b, &o);
            fst = f->status; bst = b->status;
            iF++; iB++;
            {
                double sh = sqrt((double)o.sep[0] * o.sep[0] + (double)o.sep[1] * o.sep[1]);
                sum_sep2 += sh * sh; nsep++;
                if (sh > max_seph) max_seph = sh;
                if (fabs(o.sep[2]) > max_sepv) max_sepv = fabs(o.sep[2]);
                if (sh > 0.05) n_seph_over[0]++;
                if (sh > 0.10) n_seph_over[1]++;
                if (sh > 0.30) n_seph_over[2]++;
                if ((n % 20) == 0) {
                    if (nsl >= maxsl) { maxsl = maxsl ? maxsl * 2 : 65536; seph_list = realloc(seph_list, sizeof(double) * maxsl); }
                    seph_list[nsl++] = sh;
                }
            }
        } else if (f && (!b || f->t < b->t)) {
            o = *f; fst = f->status; iF++;
            o.sep[0] = o.sep[1] = o.sep[2] = 0;
        } else {
            o = *b; bst = b->status; iB++;
            o.sep[0] = o.sep[1] = o.sep[2] = 0;
        }
        fwrite(&o, sizeof(o), 1, fo);
        n++;
        if (o.status < 8) cnt[o.status]++;
        {
            double sdh = sqrt((double)o.sdp[0] * o.sdp[0] + (double)o.sdp[1] * o.sdp[1]);
            if (sdh > max_sdh) max_sdh = sdh;
            if (o.sdp[2] > max_sdv) max_sdv = o.sdp[2];
        }
        if (fq && (next_qc < 0 || o.t >= next_qc - 1e-6)) {
            write_qc(fq, &o, fst, bst);
            next_qc = (next_qc < 0 ? floor(o.t / qc_dt) * qc_dt : next_qc) + qc_dt;
            while (next_qc <= o.t) next_qc += qc_dt;
        }
        if ((n & 65535) == 0) {
            double tot = (double)((hasF ? F.n : 0) + (hasB ? B.n : 0));
            pp_progress(tot > 0 ? (iF + iB) / tot : 1, "combining");
        }
    }
    h.count = (uint64_t)n;
    fseek(fo, 0, SEEK_SET);
    fwrite(&h, sizeof(h), 1, fo);
    if (ferror(fo) | fclose(fo)) return pp_fail("could not write %s (disk full?)", out);
    if (fq && (ferror(fq) | fclose(fq))) return pp_fail("could not write %s (disk full?)", qc);
    pp_result("epochs", "%lld", n);
    pp_result("fixed_pct", "%.2f", n ? 100.0 * cnt[PPST_FIX] / n : 0);
    pp_result("float_pct", "%.2f", n ? 100.0 * cnt[PPST_FLOAT] / n : 0);
    pp_result("ppp_pct", "%.2f", n ? 100.0 * cnt[PPST_PPP] / n : 0);
    pp_result("dgps_pct", "%.2f", n ? 100.0 * cnt[PPST_DGPS] / n : 0);
    pp_result("single_pct", "%.2f", n ? 100.0 * cnt[PPST_SPP] / n : 0);
    pp_result("ins_only_pct", "%.2f", n ? 100.0 * cnt[PPST_INS] / n : 0);
    pp_result("sep_h_rms_m", "%.4f", nsep ? sqrt(sum_sep2 / nsep) : 0);
    pp_result("sep_h_max_m", "%.4f", max_seph);
    pp_result("sep_v_max_m", "%.4f", max_sepv);
    pp_result("sep_h_over_5cm_pct", "%.2f", nsep ? 100.0 * n_seph_over[0] / nsep : 0);
    pp_result("sep_h_over_10cm_pct", "%.2f", nsep ? 100.0 * n_seph_over[1] / nsep : 0);
    pp_result("sep_h_over_30cm_pct", "%.2f", nsep ? 100.0 * n_seph_over[2] / nsep : 0);
    if (nsl > 0) {
        long long a;
        {
            /* simple counting histogram in mm */
            static long long hist[100001];
            long long acc = 0, target = (long long)(0.95 * nsl);
            memset(hist, 0, sizeof(hist));
            for (a = 0; a < nsl; a++) { long long mm = (long long)(seph_list[a] * 1000); if (mm > 100000) mm = 100000; hist[mm]++; }
            for (a = 0; a <= 100000; a++) { acc += hist[a]; if (acc >= target) break; }
            pp_result("sep_h_p95_m", "%.4f", a / 1000.0);
        }
    }
    pp_result("max_sd_h_m", "%.4f", max_sdh);
    pp_result("max_sd_v_m", "%.4f", max_sdv);
    free(seph_list);
    if (hasF) tr_close(&F);
    if (hasB) tr_close(&B);
    pp_progress(1.0, "done");
    return 0;
}

/* ------------------------------------------------------------------------ */
/* "pos2trj": a GNSS-only solution (RTKLIB .pos, ECEF) as a trajectory file, */
/* so GNSS-only processing goes through combine, quality and export too.     */
/*   pos2trj --pos fwd.pos --out fwd.bin --dir fwd|bwd                      */
/* Attitude: heading from the velocity when moving (> 0.5 m/s), roll and    */
/* pitch 0, all with a large standard deviation (not observed).             */
/* ------------------------------------------------------------------------ */
static int cmp_gsol(const void *a, const void *b)
{
    double d = ((const gsol_t *)a)->t - ((const gsol_t *)b)->t;
    return d < 0 ? -1 : d > 0;
}

int cmd_pos2trj(int argc, char **argv)
{
    const char *pos = NULL, *out = NULL;
    int dir = 1, i, k;
    gsolbuf_t g;
    FILE *fo;
    trj_hdr_t h;
    double hdg = 0, tlast = -1;
    long long cnt[8] = {0};
    for (i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--pos") && i + 1 < argc) pos = argv[++i];
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
        else if (!strcmp(argv[i], "--dir") && i + 1 < argc) dir = !strcmp(argv[++i], "bwd") ? -1 : 1;
    }
    if (!pos || !out) return pp_fail("usage: pos2trj --pos in.pos --out out.bin [--dir fwd|bwd]");
    if (!read_pos_file(pos, &g) || g.n == 0) return pp_fail("no GNSS solutions in %s", pos);
    qsort(g.s, g.n, sizeof(gsol_t), cmp_gsol);
    if (!(fo = fopen(out, "wb"))) { free_gsol(&g); return pp_fail("cannot write %s", out); }
    memset(&h, 0, sizeof(h));
    memcpy(h.magic, TRJ_MAGIC, 8);
    h.mode = 0;
    h.direction = dir;
    h.count = (uint64_t)g.n;
    fwrite(&h, sizeof(h), 1, fo);
    for (k = 0; k < g.n; k++) {
        /* backward passes are stored latest first (the reader reverses them) */
        const gsol_t *s = &g.s[dir < 0 ? g.n - 1 - k : k];
        trj_rec_t r;
        double llh[3], P[9], Qe[9], ve[3], sp;
        memset(&r, 0, sizeof(r));
        ecef2pos(s->r, llh);
        r.t = s->t;
        r.lat = llh[0]; r.lon = llh[1]; r.h = llh[2];
        P[0] = s->Q[0]; P[4] = s->Q[1]; P[8] = s->Q[2];
        P[1] = P[3] = s->Q[3]; P[5] = P[7] = s->Q[4]; P[2] = P[6] = s->Q[5];
        covenu(llh, P, Qe);                        /* E N U */
        r.sdp[0] = (float)sqrt(fmax(Qe[4], 0)); r.sdp[1] = (float)sqrt(fmax(Qe[0], 0));
        r.sdp[2] = (float)sqrt(fmax(Qe[8], 0));
        if (s->has_vel) {
            ecef2enu(llh, s->v, ve);
            r.vel[0] = (float)ve[1]; r.vel[1] = (float)ve[0]; r.vel[2] = (float)-ve[2];
            r.sdv[0] = (float)sqrt(fmax(s->Qv[1], 0)); r.sdv[1] = (float)sqrt(fmax(s->Qv[0], 0));
            r.sdv[2] = (float)sqrt(fmax(s->Qv[2], 0));
        } else {
            r.sdv[0] = r.sdv[1] = r.sdv[2] = 1.0f;
        }
        sp = sqrt((double)r.vel[0] * r.vel[0] + (double)r.vel[1] * r.vel[1]);
        if (sp > 0.5) hdg = atan2(r.vel[1], r.vel[0]);
        if (hdg < 0) hdg += 2 * PI;
        r.att[2] = (float)hdg;
        r.sda[0] = r.sda[1] = (float)(10 * D2R);
        r.sda[2] = (float)(sp > 0.5 ? fmin(PI, atan2(fmax(r.sdv[0], r.sdv[1]), sp)) : PI);
        switch (s->q) {
        case 1: r.status = PPST_FIX; break;
        case 2: r.status = PPST_FLOAT; break;
        case 3: case 4: r.status = PPST_DGPS; break;
        case 6: r.status = PPST_PPP; break;
        default: r.status = PPST_SPP; break;
        }
        r.nsat = (uint8_t)(s->ns > 255 ? 255 : s->ns);
        r.ratio10 = (uint8_t)fmin(255, fmax(0, s->ratio * 10));
        r.gap_s = (float)(tlast < 0 ? 0 : fmax(0, fabs(s->t - tlast) - 1.0));
        tlast = s->t;
        if (r.status < 8) cnt[r.status]++;
        fwrite(&r, sizeof(r), 1, fo);
    }
    if (ferror(fo) | fclose(fo)) { free_gsol(&g); return pp_fail("could not write %s (disk full?)", out); }
    pp_result("epochs", "%d", g.n);
    pp_result("fixed_pct", "%.2f", 100.0 * cnt[PPST_FIX] / g.n);
    free_gsol(&g);
    pp_progress(1.0, "done");
    return 0;
}
