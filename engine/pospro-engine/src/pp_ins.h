/* PosPro engine: INS filter shared definitions */
#ifndef PP_INS_H
#define PP_INS_H
#include "pp.h"

#define NC 21                 /* core error states */
#define MAXAMB (MAXOBS * 2)   /* ambiguity states (sat x freq) */
#define NMAX (NC + MAXAMB)

/* core error state indices (error = estimate - truth) */
#define IR 0                  /* position, ECEF */
#define IV 3                  /* velocity, ECEF */
#define IA 6                  /* attitude error phi, ECEF */
#define IBA 9                 /* accelerometer bias */
#define IBG 12                /* gyro bias */
#define ISA 15                /* accelerometer scale factor */
#define ISG 18                /* gyro scale factor */

/* extra states kept in the ambiguity table ("frequency" codes; 40-44 are
 * used by pp_tc.c for atmosphere and lever-arm states) */
#define NHC_MIS_P 45          /* vehicle frame vs IMU frame: pitch offset, rad */
#define NHC_MIS_Y 46          /* vehicle frame vs IMU frame: yaw offset, rad */

typedef struct {
    /* IMU noise (SI units) */
    double arw;               /* rad/sqrt(s) */
    double vrw;               /* m/s/sqrt(s) */
    double bg_sigma, bg_tau;  /* gyro bias Gauss-Markov rad/s, s */
    double ba_sigma, ba_tau;  /* accel bias m/s^2, s */
    double bg0, ba0;          /* initial bias std */
    double sg0, sa0;          /* initial scale factor std (fraction) */
    double sg_rw, sa_rw;      /* scale factor random walk 1/sqrt(s) */
    /* initial state std */
    double pos0, vel0, rp0, hdg0;
    /* installation */
    double lever_ant[3];      /* IMU -> primary antenna, body (fwd, stbd, down) m */
    double lever_ant2[3];     /* IMU -> secondary antenna (tight coupling only) */
    double lever_out[3];      /* IMU -> output point, body m */
    double lever_nhc[3];      /* IMU -> NHC point (rear axle centre), body m */
    double time_offset;       /* added to IMU times, s */
    /* constraints */
    int nhc, zupt;
    double nhc_sigma;         /* m/s */
    double nhc_rate;          /* Hz */
    int nhc_mis;              /* estimate the IMU-to-vehicle pitch/yaw offset */
    double nhc_mis_sd;        /* its initial std, rad */
    double zupt_sigma;        /* m/s */
    double zupt_gyro, zupt_acc; /* stationary detection thresholds (std over 1 s) */
    double zupt_speed;        /* max INS speed for a zero-velocity update */
    /* GNSS (LC) */
    int lc_use_float, lc_use_dgps, lc_use_single, lc_use_vel;
    double lc_min_sd[2];      /* floor std for fixed/other m */
    double lc_float_scale;    /* multiply float covariance */
    double gate_chi2;         /* innovation test (per dof) */
    /* alignment */
    int align_mode;           /* 0 auto, 1 RT attitude, 2 GNSS course */
    double align_speed;       /* m/s */
    /* output */
    int out_decim;            /* every n-th IMU sample */
    /* time span */
    double t_start, t_end;    /* GPS s; 0 = all */
    double excl[256][2];      /* GNSS not used in these GPS-time windows (Trajectory Review) */
    int nexcl;
    double gap_short_s;       /* IMU steps longer than this are lost data (s) */
    double gap_acc, gap_rate; /* what the vehicle may do in a gap: m/s^2, rad/s */
} insopt_t;

/* navigation state */
typedef struct {
    double t;
    double r[3], v[3], C[9];  /* ECEF position, velocity, body->ECEF DCM */
    double ba[3], bg[3], sa[3], sg[3];
} insstate_t;

/* Kalman filter: core error states + ambiguity states (full values kept
 * separately in amb[]) */
typedef struct {
    int n;                    /* NC + namb */
    double *P;                /* NMAX x NMAX row-major */
    double x[NMAX];           /* error estimate since last feedback */
    /* ambiguity bookkeeping */
    int namb;
    int amb_sat[MAXAMB], amb_f[MAXAMB];
    double amb[MAXAMB];       /* SD ambiguity, cycles */
    double amb_lam[MAXAMB];
    int amb_lock[MAXAMB];
    double amb_tlast[MAXAMB];
    int amb_half[MAXAMB];
    int amb_fixed[MAXAMB];    /* last fixed DD integer relation valid flag */
} kf_t;

#define PP_(kf, i, j) ((kf)->P[(size_t)(i) * NMAX + (j)])

void kf_init(kf_t *kf);
void kf_free(kf_t *kf);
/* y = H dx + v ; H m x n (row-major, n = kf->n); R m x m. Returns 0 ok */
int kf_update(kf_t *kf, const double *H, const double *y, const double *R, int m);
int kf_add_amb(kf_t *kf, int sat, int f, double value, double var, double lam);
void kf_del_amb(kf_t *kf, int k);
int kf_find_amb(const kf_t *kf, int sat, int f);
void ins_feedback(insstate_t *s, kf_t *kf);

/* lever arm position of the antenna (ECEF) */
void ins_lever_pos(const insstate_t *s, const double *lever, double *r);

/* TC GNSS processing (pp_tc.c) */
typedef struct tcctx_s tcctx_t;
tcctx_t *tc_open(const cfg_t *cfg, char *err, int errn);
void tc_close(tcctx_t *tc);
int tc_count(const tcctx_t *tc);
double tc_time(const tcctx_t *tc, int i);
int tc_rx(const tcctx_t *tc, int i);            /* 0 primary antenna, 1 secondary */      /* rover epoch true time, GPS s */
int tc_spp(tcctx_t *tc, int i, double *r, double *v, double *dt); /* single point */
/* run the update for epoch i with INS state at s->t (extrapolated by dt to
 * the epoch). Returns number of satellites used, sets *status, *ratio */
int tc_update(tcctx_t *tc, int i, insstate_t *s, kf_t *kf, const insopt_t *o,
              int *status, double *ratio, int direction);

#endif
