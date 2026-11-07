/* PosPro engine: shared declarations.
 *
 * Copyright (c) 2026 Canadian Lidar. GPL-2.0-or-later (QGIS plugin licence).
 * Uses RTKLIB (BSD-2) and Zstandard (BSD-3); see ../rtklib/LICENSE.txt and
 * ../zstd/LICENSE.txt.
 */
#ifndef PP_H
#define PP_H

#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include "rtklib.h"

#define PP_VERSION "2.0.5"

/* -------------------------------------------------------------------------
 * Constants
 * ---------------------------------------------------------------------- */
#define GPS_EPOCH_UNIX 315964800.0   /* 1980-01-06 00:00:00 UTC in unix s */
#define PP_WGS84_A 6378137.0
#define PP_WGS84_F (1.0 / 298.257223563)
#define PP_WGS84_E2 (PP_WGS84_F * (2.0 - PP_WGS84_F))
#define PP_GM 3.986004418e14
#define PP_OMEGA_E 7.2921151467e-5
#define PP_J2 1.082627e-3

/* -------------------------------------------------------------------------
 * Files written by "extract"
 * ---------------------------------------------------------------------- */
#define IMU_MAGIC "PPIMU001"
#define RT_MAGIC "PPRT0001"
#define TRJ_MAGIC "PPTRJ001"

#pragma pack(push, 1)
typedef struct {            /* 64-byte header of imu.bin */
    char magic[8];
    uint64_t count;
    double rate_hz;         /* median sample rate */
    double t0, t1;          /* GPS seconds since 1980-01-06 */
    int32_t lag_samples;    /* applied packet 20/28 pairing shift */
    int32_t leap;           /* GPS-UTC leap seconds used */
    uint8_t spare[16];
} imu_hdr_t;

typedef struct {            /* 40-byte IMU record (Certus aligned body frame) */
    double t;               /* GPS seconds since 1980-01-06 */
    float acc[3];           /* specific force, m/s^2 (X fwd, Y stbd, Z down) */
    float gyr[3];           /* angular rate, rad/s */
    float temp;             /* deg C */
    uint32_t flags;
} imu_rec_t;

typedef struct {            /* 64-byte header of rt.bin */
    char magic[8];
    uint64_t count;
    uint8_t spare[48];
} rt_hdr_t;

typedef struct {            /* 88-byte real-time (packet 20) record */
    double t;               /* GPS seconds */
    double lat, lon, h;     /* rad, rad, m (ellipsoidal) */
    float vel[3];           /* N, E, D m/s */
    float att[3];           /* roll, pitch, heading rad */
    float sd[3];            /* lat, lon, height std m */
    float gyr[3];           /* filtered angular velocity rad/s */
    uint16_t sys_status, filt_status;
} rt_rec_t;

/* Trajectory record (fwd.bin, bwd.bin, traj.bin). 160 bytes. */
typedef struct {
    double t;               /* GPS seconds */
    double lat, lon, h;     /* reference point, rad rad m */
    float vel[3];           /* N E D m/s */
    float att[3];           /* roll pitch heading rad (body w.r.t. local NED) */
    float sdp[3];           /* N E D position std m */
    float sdv[3];           /* N E D velocity std m/s */
    float sda[3];           /* roll pitch heading std rad */
    float acc[3];           /* bias-corrected specific force, body, m/s^2 */
    float gyr[3];           /* bias-corrected angular rate, body, rad/s */
    float ba[3];            /* accelerometer bias m/s^2 */
    float bg[3];            /* gyro bias rad/s */
    float sep[3];           /* combined only: fwd-bwd separation N E D m */
    uint8_t status;         /* see PPST_* */
    uint8_t nsat;           /* satellites used at the last GNSS update */
    uint8_t ratio10;        /* AR ratio * 10 (clamped) */
    uint8_t flags;          /* bit0 zupt, bit1 gnss gap, bit2 nhc, bit3 aligned */
    float gap_s;            /* seconds since the last good GNSS update */
} trj_rec_t;

typedef struct {
    char magic[8];
    uint64_t count;
    double lever_out[3];    /* output point from IMU, body frame */
    int32_t mode;           /* 1 LC, 2 TC */
    int32_t direction;      /* 1 fwd, -1 bwd, 0 combined */
    uint8_t spare[24];
} trj_hdr_t;
#pragma pack(pop)

/* Solution status, best first */
enum {
    PPST_NONE = 0, PPST_INS = 1, PPST_SPP = 2, PPST_DGPS = 3,
    PPST_PPP = 4, PPST_FLOAT = 5, PPST_FIX = 6
};

/* -------------------------------------------------------------------------
 * Configuration (key = value file)
 * ---------------------------------------------------------------------- */
typedef struct {
    int n;
    char **key, **val;
} cfg_t;

int cfg_load(cfg_t *c, const char *path);
void cfg_free(cfg_t *c);
const char *cfg_str(const cfg_t *c, const char *key, const char *def);
double cfg_num(const cfg_t *c, const char *key, double def);
int cfg_int(const cfg_t *c, const char *key, int def);
int cfg_vec(const cfg_t *c, const char *key, double *v, int n);
int cfg_list(const cfg_t *c, const char *key, char out[][1024], int nmax);

/* -------------------------------------------------------------------------
 * Progress / messages (stdout protocol read by the plugin)
 *   PROGRESS <0..1> <text>
 *   INFO <text>
 *   WARN <text>
 *   RESULT <key>=<value>
 * ---------------------------------------------------------------------- */
void pp_progress(double frac, const char *fmt, ...);
void pp_info(const char *fmt, ...);
void pp_warn(const char *fmt, ...);
void pp_result(const char *key, const char *fmt, ...);
int pp_fail(const char *fmt, ...);   /* prints ERROR, returns 1 */
extern double pp_prog_lo, pp_prog_hi; /* sub-range mapping for progress */

/* -------------------------------------------------------------------------
 * Small linear algebra (row-major)
 * ---------------------------------------------------------------------- */
void m3_mul(const double *a, const double *b, double *c);       /* c=a*b */
void m3_mulT(const double *a, const double *b, double *c);      /* c=a*b' */
void m3_Tmul(const double *a, const double *b, double *c);      /* c=a'*b */
void m3_vec(const double *a, const double *v, double *r);       /* r=a*v */
void m3_Tvec(const double *a, const double *v, double *r);      /* r=a'*v */
void m3_skew(const double *v, double *s);
void m3_exp(const double *phi, double *R);                      /* Rodrigues */
void m3_orth(double *C);
double norm3(const double *a);

/* geodesy */
void pp_llh2ecef(const double *llh, double *r);
void pp_ecef2llh(const double *r, double *llh);
void pp_Cne(double lat, double lon, double *C);   /* NED -> ECEF */
void pp_euler2C(double roll, double pitch, double yaw, double *Cbn);
void pp_C2euler(const double *Cbn, double *rpy);
void pp_gravity_ecef(const double *r, double *g);  /* gravity incl. centrifugal */
double pp_wrap_pi(double a);

/* time */
double pp_gtime2gps(gtime_t t);            /* seconds since GPS epoch */
gtime_t pp_gps2gtime(double s);
int pp_leap_for_unix(double unix_s);       /* GPS-UTC */

/* subcommands */
int cmd_extract(int argc, char **argv);
int cmd_gnss(int argc, char **argv);
int cmd_ins(int argc, char **argv);
int cmd_combine(int argc, char **argv);
int cmd_pos2trj(int argc, char **argv);
int cmd_export(int argc, char **argv);
int cmd_navcheck(int argc, char **argv);
int cmd_ppp_summary(int argc, char **argv);

/* mcap reader (extract) */
typedef int (*mcap_msg_cb)(void *user, const char *topic, uint64_t log_time,
                           uint64_t pub_time, const uint8_t *data, size_t n);
typedef int (*mcap_att_cb)(void *user, const char *name, const uint8_t *data,
                           size_t n);
int mcap_read(const char *path, mcap_msg_cb on_msg, mcap_att_cb on_att,
              void *user, uint64_t *bytes_done, char *err, size_t errn);

/* GNSS solution file (.pos) reader used by ins/combine */
typedef struct {
    double t;            /* GPS seconds */
    double r[3];         /* ECEF m */
    double Q[6];         /* xx yy zz xy yz zx variances m^2 */
    double v[3];         /* ECEF velocity, if present */
    double Qv[3];
    int has_vel;
    int q;               /* RTKLIB Q (1 fix 2 float 3 sbas 4 dgps 5 single 6 ppp) */
    int ns;
    double ratio;
} gsol_t;

typedef struct {
    int n, nmax;
    gsol_t *s;
} gsolbuf_t;

int read_pos_file(const char *path, gsolbuf_t *buf);
void free_gsol(gsolbuf_t *buf);

#endif
