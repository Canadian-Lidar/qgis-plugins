/* PosPro engine: configuration, messages, maths, geodesy, time, .pos reader */
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <ctype.h>
#include "pp.h"

/* ------------------------------------------------------------------------ */
/* configuration                                                             */
/* ------------------------------------------------------------------------ */
static char *trim(char *s)
{
    char *e;
    while (*s && isspace((unsigned char)*s)) s++;
    e = s + strlen(s);
    while (e > s && isspace((unsigned char)e[-1])) *--e = '\0';
    return s;
}

int cfg_load(cfg_t *c, const char *path)
{
    FILE *fp;
    char line[16384], *p, *k, *v;
    memset(c, 0, sizeof(*c));
    if (!(fp = fopen(path, "r"))) return 0;
    while (fgets(line, sizeof(line), fp)) {
        p = trim(line);
        if (!*p || *p == '#') continue;
        if (!(v = strchr(p, '='))) continue;
        *v++ = '\0';
        k = trim(p);
        v = trim(v);
        c->key = realloc(c->key, sizeof(char *) * (c->n + 1));
        c->val = realloc(c->val, sizeof(char *) * (c->n + 1));
        c->key[c->n] = strdup(k);
        c->val[c->n] = strdup(v);
#ifdef _WIN32
        if (!strncmp(k, "file.", 5)) {
            char *q;
            for (q = c->val[c->n]; *q; q++) if (*q == '/') *q = '\\';
        }
#endif
        c->n++;
    }
    fclose(fp);
    return 1;
}

void cfg_free(cfg_t *c)
{
    int i;
    for (i = 0; i < c->n; i++) { free(c->key[i]); free(c->val[i]); }
    free(c->key); free(c->val);
    memset(c, 0, sizeof(*c));
}

const char *cfg_str(const cfg_t *c, const char *key, const char *def)
{
    int i;
    for (i = c->n - 1; i >= 0; i--)   /* last definition wins */
        if (!strcmp(c->key[i], key)) return c->val[i];
    return def;
}

double cfg_num(const cfg_t *c, const char *key, double def)
{
    const char *s = cfg_str(c, key, NULL);
    char *e;
    double v;
    if (!s || !*s) return def;
    v = strtod(s, &e);
    return (e == s || !isfinite(v)) ? def : v;
}

int cfg_int(const cfg_t *c, const char *key, int def)
{
    return (int)floor(cfg_num(c, key, def) + 0.5);
}

int cfg_vec(const cfg_t *c, const char *key, double *v, int n)
{
    const char *s = cfg_str(c, key, NULL);
    char buf[1024], *p, *tok;
    int i = 0;
    if (!s) return 0;
    snprintf(buf, sizeof(buf), "%s", s);
    for (p = buf; i < n && (tok = strtok(p, ", \t")); p = NULL) v[i++] = atof(tok);
    return i;
}

/* repeated keys "key = value" collected in order */
int cfg_list(const cfg_t *c, const char *key, char out[][1024], int nmax)
{
    int i, n = 0;
    for (i = 0; i < c->n && n < nmax; i++)
        if (!strcmp(c->key[i], key)) snprintf(out[n++], 1024, "%s", c->val[i]);
    return n;
}

/* ------------------------------------------------------------------------ */
/* messages                                                                  */
/* ------------------------------------------------------------------------ */
double pp_prog_lo = 0.0, pp_prog_hi = 1.0;

static void vline(const char *tag, const char *fmt, va_list ap)
{
    char buf[2048], *p;
    vsnprintf(buf, sizeof(buf), fmt, ap);
    for (p = buf; *p; p++) if (*p == '\n' || *p == '\r') *p = ' ';
    fprintf(stdout, "%s %s\n", tag, buf);
    fflush(stdout);
}

void pp_progress(double frac, const char *fmt, ...)
{
    static double last = -1.0;
    static char lastmsg[256] = "";
    char msg[256];
    va_list ap;
    double f;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    if (frac < 0) frac = 0;
    if (frac > 1) frac = 1;
    f = pp_prog_lo + (pp_prog_hi - pp_prog_lo) * frac;
    if (fabs(f - last) < 0.002 && !strcmp(msg, lastmsg) && frac < 1.0) return;
    last = f;
    snprintf(lastmsg, sizeof(lastmsg), "%s", msg);
    fprintf(stdout, "PROGRESS %.4f %s\n", f, msg);
    fflush(stdout);
}

void pp_info(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt); vline("INFO", fmt, ap); va_end(ap);
}

void pp_warn(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt); vline("WARN", fmt, ap); va_end(ap);
}

void pp_result(const char *key, const char *fmt, ...)
{
    char buf[2048], *p;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    for (p = buf; *p; p++) if (*p == '\n' || *p == '\r') *p = ' ';
    fprintf(stdout, "RESULT %s=%s\n", key, buf);
    fflush(stdout);
}

int pp_fail(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt); vline("ERROR", fmt, ap); va_end(ap);
    return 1;
}

/* ------------------------------------------------------------------------ */
/* 3x3 algebra (row-major)                                                   */
/* ------------------------------------------------------------------------ */
void m3_mul(const double *a, const double *b, double *c)
{
    double t[9]; int i, j;
    for (i = 0; i < 3; i++) for (j = 0; j < 3; j++)
        t[i*3+j] = a[i*3]*b[j] + a[i*3+1]*b[3+j] + a[i*3+2]*b[6+j];
    memcpy(c, t, sizeof(t));
}

void m3_mulT(const double *a, const double *b, double *c)
{
    double t[9]; int i, j;
    for (i = 0; i < 3; i++) for (j = 0; j < 3; j++)
        t[i*3+j] = a[i*3]*b[j*3] + a[i*3+1]*b[j*3+1] + a[i*3+2]*b[j*3+2];
    memcpy(c, t, sizeof(t));
}

void m3_Tmul(const double *a, const double *b, double *c)
{
    double t[9]; int i, j;
    for (i = 0; i < 3; i++) for (j = 0; j < 3; j++)
        t[i*3+j] = a[i]*b[j] + a[3+i]*b[3+j] + a[6+i]*b[6+j];
    memcpy(c, t, sizeof(t));
}

void m3_vec(const double *a, const double *v, double *r)
{
    double t[3];
    t[0] = a[0]*v[0] + a[1]*v[1] + a[2]*v[2];
    t[1] = a[3]*v[0] + a[4]*v[1] + a[5]*v[2];
    t[2] = a[6]*v[0] + a[7]*v[1] + a[8]*v[2];
    r[0] = t[0]; r[1] = t[1]; r[2] = t[2];
}

void m3_Tvec(const double *a, const double *v, double *r)
{
    double t[3];
    t[0] = a[0]*v[0] + a[3]*v[1] + a[6]*v[2];
    t[1] = a[1]*v[0] + a[4]*v[1] + a[7]*v[2];
    t[2] = a[2]*v[0] + a[5]*v[1] + a[8]*v[2];
    r[0] = t[0]; r[1] = t[1]; r[2] = t[2];
}

void m3_skew(const double *v, double *s)
{
    s[0] = 0;     s[1] = -v[2]; s[2] = v[1];
    s[3] = v[2];  s[4] = 0;     s[5] = -v[0];
    s[6] = -v[1]; s[7] = v[0];  s[8] = 0;
}

void m3_exp(const double *phi, double *R)
{
    double th = norm3(phi), S[9], S2[9], a, b;
    int i;
    m3_skew(phi, S);
    m3_mul(S, S, S2);
    if (th < 1e-8) { a = 1.0 - th*th/6.0; b = 0.5 - th*th/24.0; }
    else { a = sin(th) / th; b = (1.0 - cos(th)) / (th*th); }
    for (i = 0; i < 9; i++) R[i] = (i % 4 == 0 ? 1.0 : 0.0) + a*S[i] + b*S2[i];
}

/* re-orthonormalise a DCM: C <- C (C'C)^-1/2 approximated by one step */
void m3_orth(double *C)
{
    double E[9], CtC[9]; int i;
    m3_Tmul(C, C, CtC);
    for (i = 0; i < 9; i++) E[i] = (i % 4 == 0 ? 1.5 : 0.0) - 0.5*CtC[i];
    m3_mul(C, E, C);
}

double norm3(const double *a) { return sqrt(dot3(a, a)); }

double pp_wrap_pi(double a)
{
    while (a > PI) a -= 2*PI;
    while (a < -PI) a += 2*PI;
    return a;
}

/* ------------------------------------------------------------------------ */
/* geodesy                                                                   */
/* ------------------------------------------------------------------------ */
void pp_llh2ecef(const double *llh, double *r) { pos2ecef(llh, r); }
void pp_ecef2llh(const double *r, double *llh) { ecef2pos(r, llh); }

/* C_n^e : NED to ECEF */
void pp_Cne(double lat, double lon, double *C)
{
    double sl = sin(lat), cl = cos(lat), so = sin(lon), co = cos(lon);
    C[0] = -sl*co; C[1] = -so; C[2] = -cl*co;
    C[3] = -sl*so; C[4] = co;  C[5] = -cl*so;
    C[6] = cl;     C[7] = 0;   C[8] = -sl;
}

/* body -> NED from roll, pitch, yaw (aerospace 3-2-1) */
void pp_euler2C(double r, double p, double y, double *C)
{
    double cr = cos(r), sr = sin(r), cp = cos(p), sp = sin(p), cy = cos(y), sy = sin(y);
    C[0] = cp*cy; C[1] = -cr*sy + sr*sp*cy; C[2] = sr*sy + cr*sp*cy;
    C[3] = cp*sy; C[4] = cr*cy + sr*sp*sy;  C[5] = -sr*cy + cr*sp*sy;
    C[6] = -sp;   C[7] = sr*cp;             C[8] = cr*cp;
}

void pp_C2euler(const double *C, double *rpy)
{
    double s = -C[6];
    if (s > 1) s = 1;
    if (s < -1) s = -1;
    rpy[0] = atan2(C[7], C[8]);
    rpy[1] = asin(s);
    rpy[2] = atan2(C[3], C[0]);
}

void pp_gravity_ecef(const double *r, double *g)
{
    double rr = norm3(r), z2 = r[2]*r[2]/(rr*rr), k = PP_GM/(rr*rr*rr);
    double a2 = (PP_WGS84_A/rr)*(PP_WGS84_A/rr), w2 = PP_OMEGA_E*PP_OMEGA_E;
    g[0] = -k*r[0]*(1.0 + 1.5*PP_J2*a2*(1.0 - 5.0*z2)) + w2*r[0];
    g[1] = -k*r[1]*(1.0 + 1.5*PP_J2*a2*(1.0 - 5.0*z2)) + w2*r[1];
    g[2] = -k*r[2]*(1.0 + 1.5*PP_J2*a2*(3.0 - 5.0*z2));
}

/* ------------------------------------------------------------------------ */
/* time                                                                      */
/* ------------------------------------------------------------------------ */
double pp_gtime2gps(gtime_t t)
{
    return timediff(t, gpst2time(0, 0.0));
}

gtime_t pp_gps2gtime(double s)
{
    int week = (int)floor(s / 604800.0);
    return gpst2time(week, s - week * 604800.0);
}

int pp_leap_for_unix(double unix_s)
{
    gtime_t t;
    t.time = (time_t)floor(unix_s);
    t.sec = unix_s - floor(unix_s);
    return (int)floor(timediff(utc2gpst(t), t) + 0.5);
}

/* ------------------------------------------------------------------------ */
/* RTKLIB .pos (ECEF, GPST week/tow) reader                                  */
/* ------------------------------------------------------------------------ */
static double sqv(double x) { return x < 0 ? -x*x : x*x; }

int read_pos_file(const char *path, gsolbuf_t *buf)
{
    FILE *fp;
    char line[1024];
    double v[32];
    int n, week;
    memset(buf, 0, sizeof(*buf));
    if (!(fp = fopen(path, "r"))) return 0;
    while (fgets(line, sizeof(line), fp)) {
        char *p = line, *e;
        gsol_t s;
        if (line[0] == '%' || line[0] == '\n' || line[0] == '\r') continue;
        week = (int)strtol(p, &e, 10);
        if (e == p) continue;
        p = e;
        for (n = 0; n < 32; n++) {
            v[n] = strtod(p, &e);
            if (e == p) break;
            p = e;
        }
        if (n < 14) continue;
        memset(&s, 0, sizeof(s));
        s.t = week * 604800.0 + v[0];
        s.r[0] = v[1]; s.r[1] = v[2]; s.r[2] = v[3];
        s.q = (int)v[4]; s.ns = (int)v[5];
        s.Q[0] = v[6]*v[6]; s.Q[1] = v[7]*v[7]; s.Q[2] = v[8]*v[8];
        s.Q[3] = sqv(v[9]); s.Q[4] = sqv(v[10]); s.Q[5] = sqv(v[11]);
        s.ratio = v[13];
        if (n >= 20) {
            s.has_vel = 1;
            s.v[0] = v[14]; s.v[1] = v[15]; s.v[2] = v[16];
            s.Qv[0] = v[17]*v[17]; s.Qv[1] = v[18]*v[18]; s.Qv[2] = v[19]*v[19];
        }
        if (buf->n >= buf->nmax) {
            buf->nmax = buf->nmax ? buf->nmax * 2 : 65536;
            buf->s = realloc(buf->s, sizeof(gsol_t) * buf->nmax);
            if (!buf->s) { fclose(fp); return 0; }
        }
        buf->s[buf->n++] = s;
    }
    fclose(fp);
    return 1;
}

void free_gsol(gsolbuf_t *buf)
{
    free(buf->s);
    memset(buf, 0, sizeof(*buf));
}
