"""H2 v7: run.sh base settings followed by render-v7.sh overrides.

No environment or feed card override changes this composition.
"""
H2 = dict(
    FLAT=1, STANDOFF=0.10, HDEP=0.16, HDEPTH=1.3, HLO=0.14,
    HHI=0.35, WHAT27=1, HSIDE=1, NOPANEL=1, WOFF=4, H=840,
    FX0=16, FX1=584, FY0=34, FYB=-4, PILL=20, TAGF=13,
    WKFADE=0.55, WKFADE2=0.75, WKHI=0.12, WKLINK=0.35,
    HSLO=0.0, HSHI=1.0, HW=6.0, HWOFF=4.0, DASH_ON=7.2,
    DASH_OFF=3.0, DOT_OFF=2.8, HTAPER=2.0, HWTK=0.45, HSHI_T=0.42,
    CUBES=0.115, CUBEG=0.022, CROWS=3, CCOLS=3, WCUBE=2,
    # README sample's whole-tube fit, part of the approved composition.
    FITUP=-1,
)

# One thin voxel row per How, grouped by Why and stacked in birth order.
H2.update(NOSLAB=1, HTILE_R=0.052, HTILE_L=0.1, HTILE_H=0.060,
          HTILE_GAP=0.006, HTILE_SEAM=0.008, HTILE_DROP=0.0)

# FDGN-600 owner pick 2026-10-31 08:39 (How read too weak): thicker, brighter How rows.
H2.update(HTILE_H=0.13, HTILE_R=0.12, HLIT=0.35, HDIM=0.10, HPAST=0.0)
