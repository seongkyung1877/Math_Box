# -*- coding: utf-8 -*-
# =============================================================================
#  UNOCC Web Basemap Platform  —  PHASE 0 PROOF-OF-CONCEPT
#  File: phase0_basemap_poc.py
#
#  WHAT THIS IS (한글 설명은 HOW_TO_RUN.md 참고):
#  This script runs INSIDE the QGIS Python Console. It proves the whole idea
#  end-to-end WITHOUT ArcGIS:
#     1) downloads country boundaries (Natural Earth, open data)
#     2) downloads live OSM roads + rivers (Overpass API)
#     3) reprojects the view to a suitable UTM zone (CRS standardization)
#     4) applies UNOCC symbology  ← SOP is the SOURCE OF TRUTH for all values
#     5) stacks layers in UNOCC draw order and zooms to the country
#
#  After running, you visually tune each layer in QGIS and then export each
#  layer's style as .qml  (Layer ▸ right-click ▸ Export ▸ Save as QGIS Layer
#  Style File...). Those .qml files feed Phase 1 (the FastAPI/PyQGIS backend).
#
#  IMPORTANT: values here follow the SOP "STYLING OF OUTPUTS" section.
#  Where the old ArcGIS script disagreed with the SOP, the SOP wins.
# =============================================================================

import os  # needed one line below (EXPORT_DIR) — full import list is further
           # down in its own "imports" section; os is harmless to import twice

# ----------------------------------------------------------------------------
#  ①  USER CONFIG  — edit ONLY this block
# ----------------------------------------------------------------------------
COUNTRY_NAME = "Ukraine"   # must match Natural Earth 'ADM0NAME' / 'ADMIN'
ISO3_CODE    = "UKR"       # 3-letter ISO code (Natural Earth 'ADM0_A3')

# Tip: for your FIRST test pick a SMALL country so OSM download is fast,
#      e.g.  COUNTRY_NAME="Lebanon", ISO3_CODE="LBN".
#      Big countries (Russia/Canada) can make Overpass slow or time out.

FETCH_OSM = True   # set False to skip live OSM roads/rivers (boundaries only)

# Use OFFICIAL UN boundaries (UN Geospatial "Clear Map" web service) instead of
# Natural Earth. This is the UN cartographic position — e.g. Crimea is shown as
# part of Ukraine. Falls back to Natural Earth automatically if the service is
# unreachable. Country polygons + international/admin/coastline lines come from
# the UN; capitals, ocean fill and the globe-locator world stay Natural Earth.
USE_UN_BOUNDARIES = True

# Provincial (admin-1) capitals: only TRIM countries that have a lot.
#   * count <= THRESHOLD  -> show them all (e.g. Syria 10, Lebanon 5).
#   * count >  THRESHOLD  -> keep only the KEEP most important (e.g. Ukraine
#     23 -> 7, Iraq 17 -> 7, Korea 11 -> 7).
# Importance = Natural Earth SCALERANK ascending, then population descending.
# Set THRESHOLD to 0 to always trim, or very high to never trim.
PROVINCIAL_CAP_THRESHOLD = 10
PROVINCIAL_CAP_KEEP      = 7

# Boundary-line snapping tolerance (metres, in the UTM-projected CRS). Pulls
# UN ClearMap's boundary-LINE features (intl/admin1/disputed) onto its own
# country-AREA polygon edges, since the two are independently digitized and
# don't perfectly coincide otherwise (see snap_to_polygons() docstring).
BOUNDARY_SNAP_TOLERANCE_M = 300

# How far GADM's own coastline/border can diverge from the UN ClearMap
# subject polygon's edge before we still want to call an admin1 line segment
# "the same" outer edge and drop it (see admin1_lines_from_gadm()). This is
# a MUCH looser tolerance than BOUNDARY_SNAP_TOLERANCE_M on purpose: GADM and
# UN ClearMap are independently digitized/generalized datasets, and at
# country-wide scale their coastline representations diverged by well over
# 300m in testing (Brazil: state-boundary duplicate lines were still clearly
# visible hugging the whole coast at that tolerance — confirmed visually,
# 2026-07-22). 300m is tuned for snapping two datasets that are SUPPOSED to
# trace the same line; this is tuned for "is this GADM edge just tracing the
# coast", a coarser question.
#
# This buffer is ONLY ever applied to GADM-derived admin1 lines (inside
# admin1_lines_from_gadm(), below) — genuine UN BNDL admin1/autonomous-region
# lines (BDYTYP 6/8) never trace the coastline in the first place, so no
# exclusion buffer is needed or applied when UN data is available.
#
# A single flat 10km distance is fine for a country the size of Brazil (the
# case it was tuned against — negligible relative to the country's own
# ~4,000km span) but catastrophic for a small, narrow country: for Lebanon
# (~50km wide), a 10km buffer around the ENTIRE outer edge swallows a large
# fraction of the country's own interior, deleting real admin1 lines near
# the coast, not just the coastal duplicate (2026-07-22 user report — "admin
# boundaries are disappearing near coastline"). ADMIN1_COAST_EXCLUDE_BUFFER_M
# is now a CEILING, not a flat value — the actual per-country buffer is
# scaled down for small countries (see admin1_lines_from_gadm()).
ADMIN1_COAST_EXCLUDE_BUFFER_M = 10000
ADMIN1_COAST_EXCLUDE_BUFFER_MIN_M = 500     # floor, just above BOUNDARY_SNAP_TOLERANCE_M
ADMIN1_COAST_EXCLUDE_BUFFER_FRAC = 0.03     # else scale to 3% of the subject's own narrower extent

# --- Print layout (A3) -------------------------------------------------------
MAKE_LAYOUT   = True         # build an A3 print layout with all map furniture
EXPORT_LAYOUT = True         # also export it to PDF + PNG (300 DPI) when True
# PDF/PNG output folder. Overridable via env var — the hardcoded Windows path
# is only valid for the manual QGIS-desktop workflow (HOW_TO_RUN.md); a Linux
# Docker container has no C:\ drive, so backend/Dockerfile sets
# UNOCC_EXPORT_DIR to a container-local path instead.
EXPORT_DIR = os.environ.get(
    "UNOCC_EXPORT_DIR", r"C:\unocc_workspace\04_Maps_Outputs\01_Exports")
# Subfolder under both EXPORT_DIR and 02_GIS_Projects/qpt — keeps this
# script's outputs separate from the wb_mpm / mpi variants' outputs.
OUTPUT_TAG    = "basemap"
MAP_CATEGORY  = "BASEMAP"    # grey subtitle under the country name
AGENCY_TITLE  = "UNOCC"      # cyan agency label (top-left)
DISCLAIMER_TEXT = (
    "The boundaries and names shown and the designations used on this map do "
    "not imply official endorsement or acceptance by the United Nations.")

# ----------------------------------------------------------------------------
#  imports (all available inside QGIS's bundled Python)
# ----------------------------------------------------------------------------
import os, json, re, time, tempfile, urllib.request, urllib.parse
from datetime import date

from qgis.core import (
    Qgis,
    QgsProject, QgsVectorLayer, QgsRasterLayer,
    QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsSingleSymbolRenderer,
    QgsSimpleMarkerSymbolLayer, QgsSimpleMarkerSymbolLayerBase,
    QgsMarkerLineSymbolLayer,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings,
    QgsVectorLayerSimpleLabeling,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsUnitTypes, QgsRectangle,
    # print-layout classes
    QgsApplication, QgsPrintLayout, QgsLayoutItem, QgsLayoutItemMap,
    QgsLayoutItemLabel, QgsLayoutItemScaleBar, QgsLayoutItemLegend,
    QgsLayoutItemShape,
    QgsLayoutPoint, QgsLayoutSize, QgsLayoutMeasurement, QgsLayoutExporter,
    QgsReadWriteContext, QgsLegendStyle, QgsSymbolLegendNode,
)
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtCore import Qt, QSizeF
import glob

try:
    iface  # provided by QGIS when run in the console
except NameError:
    iface = None

# ----------------------------------------------------------------------------
#  ②  UNOCC SYMBOLOGY  — boundary widths, label sizes and colours all follow
#  the SOP "STYLING OF OUTPUTS" (base map) section (2026-07-07 per user).
#      (colors as HEX, widths/sizes in POINTS; pt→mm handled by PT below)
# ----------------------------------------------------------------------------
PT = 0.352777778  # 1 point = 0.3528 mm  (QGIS simple symbols use mm)

STYLE = {
    # fills -------------------------------------------------------------------
    "ocean_fill":        "#D8EBF3",   # SOP colour
    "country_bg_fill":   "#FAF4DB",   # cream mask for neighbouring countries
    # subject country: solid white (was transparent, letting World_Hillshade
    # show through) — hillshade removed from the subject per user request
    # 2026-07-22 (also sidesteps a headless/Docker rendering quirk where the
    # hillshade tiles sometimes don't finish loading before layout export).
    "subject_fill":      "#FFFFFF",
    # slight transparency so World_Hillshade's terrain texture faintly shows
    # through neighbouring countries — basemap-only per user request
    # 2026-07-16. (The subject country itself no longer has a hillshade hole
    # at all — see subject_fill above, 2026-07-22.)
    "country_bg_opacity": 0.88,
    # lines  (widths = SOP base-map values) ----------------------------------
    "coastline":         ("#6F8DB9", 1.2),   # SOP: 1.2pt
    "intl_boundary":     ("#A99779", 4.0),   # SOP: AOI 4pt (subject country only)
    # non-subject international boundaries (other countries' borders in view):
    # same hue as intl_boundary but alpha baked in, so they read as faded next
    # to the subject's border. #A99779 = rgb(169,151,121).
    "intl_boundary_other_rgba": "169,151,121,90",   # ~0.35 opacity
    "intl_boundary_other_pt":   4.0,
    # Disputed / undetermined boundaries (UN BDYTYP 2 'Special boundary
    # line', 3 'Armistice, undetermined or administrative line', 4 'Other
    # line of separation' — e.g. the India-Pakistan Line of Control in
    # Jammu & Kashmir, or the Sudan-South Sudan boundary). UN's own ClearMap
    # renders these dotted rather than the solid 'International boundary'
    # (BDYTYP 1) style, since — per the UN's own disclaimer wording — final
    # status hasn't been agreed/determined. Same weight as intl_boundary so
    # it doesn't read as a lesser/weaker line, just a differently-drawn one.
    "disputed_boundary": ("#A99779", 4.0),
    "admin_boundary":    ("#A99779", 2.0),   # SOP: 2pt, dash
    "roads":             ("#D79E9E", 1.15),
    "roads_opacity":     0.28,
    # rivers: TWO tiers via rule-based renderer. #6F8DB9 = rgb(111,141,185).
    # alpha (last number, 0-255) is baked into the colour, kept low = faint.
    "river_minor_rgba":  "111,141,185,26",   # ~0.10 opacity
    "river_minor_pt":    0.8,
    "river_major_rgba":  "111,141,185,38",   # ~0.15 opacity (ceiling)
    "river_major_pt":    1.0,
    # labels (Arial). sizes = SOP base-map values ----------------------------
    "lbl_country":       ("Arial", False, 25, "#B2B2B2"),  # SOP: 36pt, UPPER
    "country_halo_pt":   0.3,   # subtle white halo so the name lifts off the fill
    "lbl_ocean":     ("Times New Roman", 22, "#7AB6F5"),   # SOP: 48pt Italic, UPPER
    "lbl_natl_capital":  ("Arial", True,  28, "#000000"),  # SOP: 28pt Bold
    "lbl_prov_capital":  ("Arial", False, 20, "#343434"),  # SOP: 16pt + halo
    "prov_halo_pt":      1.2,   # SOP: 0.5pt white halo — enlarged per user for legibility
    # national-capital marker (circle + star) — enlarged per user
    "natl_circle_mm":    12.0,   # outer white disc diameter (was 5.4)
    "natl_star_mm":      9.6,   # inner black star (≈0.8 of circle)
    # provincial-capital marker = two concentric circles (ring + centre dot)
    "prov_outer_mm":     6.5,   # outer ring diameter
    "prov_inner_mm":     3.5,   # inner filled dot diameter
    # label offsets from the marker (points) — larger so labels clear symbols
    "natl_label_offset_pt": 16.0,
    "prov_label_offset_pt": 10.0,
}

# World Hillshade (same tile service ArcGIS Pro uses by default), sitting at
# the very BOTTOM of the stack — now only shows faintly through neighbouring
# countries' slightly-transparent fill, not through the subject (see
# subject_fill in STYLE, 2026-07-22).
HILLSHADE_XYZ = ("type=xyz&url=https://services.arcgisonline.com/ArcGIS/rest/"
                 "services/Elevation/World_Hillshade/MapServer/tile/"
                 "%7Bz%7D/%7By%7D/%7Bx%7D&zmax=19&zmin=0")
SHOW_HILLSHADE = True

# ----------------------------------------------------------------------------
#  ③  DATA SOURCES  (open data — no ArcGIS, no internal GDB)
#      Natural Earth 50m via the public nvkelso mirror; OSM via Overpass.
# ----------------------------------------------------------------------------
NE_BASE = ("https://raw.githubusercontent.com/nvkelso/"
           "natural-earth-vector/master/geojson/")
NE = {
    "ocean":       NE_BASE + "ne_50m_ocean.geojson",
    "countries":   NE_BASE + "ne_50m_admin_0_countries.geojson",
    # NOTE: no "coastline" entry — Ocean's own polygon outline IS the
    # coastline now (see style_fill()'s outline_color param), so a separate
    # coastline-lines file isn't fetched any more.
    "intl_lines":  NE_BASE + "ne_50m_admin_0_boundary_lines_land.geojson",
    # admin-1 lines: use 10m — the 50m file only covers ~9 large countries,
    # so most countries (incl. Ukraine) would come back EMPTY at 50m.
    "admin1_lines":NE_BASE + "ne_10m_admin_1_states_provinces_lines.geojson",
    # populated places: use 10m too — the 50m file is far too sparse (e.g.
    # Syria has only Damascus+Aleppo at 50m, ZERO admin-1 capitals). 10m has
    # the full set of governorate/province capitals (Syria: 10, Ukraine: 23).
    "places":      NE_BASE + "ne_10m_populated_places.geojson",
    # named seas/oceans/gulfs (e.g. "Black Sea") — for ocean-name labels
    "marine":      NE_BASE + "ne_10m_geography_marine_polys.geojson",
}
# GADM admin-1 polygons — used to derive admin1 boundary LINES for countries
# where UN ClearMap has zero admin1 coverage AND Natural Earth's admin1_lines
# fallback turns out too coarse (confirmed: Brazil has some state-boundary
# segments represented as bare 2-8 point straight lines in that file — a
# genuine source-data fidelity gap, not a bug in this script's snap/simplify
# logic). Same URL/layer this project's wb_mpm/mpi scripts already use.
GADM_GPKG_URL_TMPL = "https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg/gadm41_{iso3}.gpkg"
# Multiple Overpass mirrors — we rotate through these on timeout/504.
# Order matters: overpass-api.de measured most reliable for polygon-filtered
# queries in testing (kumi.systems repeatedly stalled to the full timeout).
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
CACHE_DIR = os.path.join(tempfile.gettempdir(), "unocc_poc_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# =============================================================================
#  HELPERS
# =============================================================================
def log(msg):
    print(f"[UNOCC-POC] {msg}")

def download(url, filename):
    """Download to a local cache (skip if already there). Sends a
    browser-like User-Agent — plain urlretrieve()'s default 'Python-urllib'
    UA gets a 403 from some sites' bot protection (e.g. ophi.org.uk)."""
    path = os.path.join(CACHE_DIR, filename)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        log(f"내려받는 중: {filename} …")
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "UNOCC-Basemap-POC/0.1"),
            "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            f.write(r.read())
    return path

def field_lookup(layer, candidates):
    """Return the real field name matching any candidate (case-insensitive)."""
    names = {f.name().lower(): f.name() for f in layer.fields()}
    for c in candidates:
        if c.lower() in names:
            return names[c.lower()]
    return None

def add_vector(path, name, subset=None):
    layer = QgsVectorLayer(path, name, "ogr")
    if not layer.isValid():
        log(f"⚠ 레이어 로드 실패: {name}")
        return None
    if subset:
        layer.setSubsetString(subset)
    QgsProject.instance().addMapLayer(layer)   # adds to TOP of layer tree
    log(f"레이어 추가: {name}  (features: {layer.featureCount()})")
    return layer

def cap_top_n(layer, threshold, keep, base_subset):
    """Only trim when the layer has MORE than `threshold` features; then keep
    the `keep` most important ones (Natural Earth SCALERANK ascending = more
    important first, tie-break by population descending). We refine the subset
    string to the chosen NE_IDs so the layer really only contains those."""
    if not layer or layer.featureCount() <= threshold:
        return  # few enough -> show them all
    names = {f.name().lower(): f.name() for f in layer.fields()}
    pick = lambda cands: next((names[c.lower()] for c in cands
                               if c.lower() in names), None)
    id_f  = pick(["NE_ID", "GEONAMESID"])
    sr_f  = pick(["SCALERANK", "LABELRANK"])
    pop_f = pick(["POP_MAX", "POP_MIN"])
    if not id_f:
        return  # no stable unique id -> leave as-is
    def rank_key(feat):
        sr  = feat[sr_f]  if sr_f  else 0
        pop = feat[pop_f] if pop_f else 0
        return (sr if sr is not None else 99,
                -(pop if pop is not None else 0))
    feats = sorted(layer.getFeatures(), key=rank_key)
    ids = [str(int(f[id_f])) for f in feats[:keep] if f[id_f] is not None]
    if not ids:
        return
    layer.setSubsetString(f'{base_subset} AND "{id_f}" IN ({",".join(ids)})')
    log(f"  주도 {layer.featureCount()}개로 제한 (중요도 상위 {keep})")

# --- symbology setters -------------------------------------------------------
def style_fill(layer, hex_color=None, transparent=False,
               outline_color=None, outline_width_pt=None):
    """outline_color/outline_width_pt: draw the polygon's own boundary as a
    stroke (e.g. the coastline, drawn from a layer that reuses Ocean's own
    geometry so it's pixel-exact against the ocean fill — see the Coastline
    layer in main()). transparent=True + outline given draws ONLY the
    stroke (no fill) — used for that dedicated Coastline layer."""
    if transparent and outline_color is None:
        sym = QgsFillSymbol.createSimple({"style": "no", "outline_style": "no"})
    else:
        props = {"outline_style": "no"}
        props["style"] = "no" if transparent else "solid"
        if not transparent:
            props["color"] = hex_color
        if outline_color is not None:
            props["outline_style"] = "solid"
            props["outline_color"] = outline_color
            props["outline_width"] = f"{outline_width_pt * PT:.3f}"
        sym = QgsFillSymbol.createSimple(props)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()

def style_line(layer, hex_color, width_pt, dashed=False, dotted=False):
    if dashed:
        # Round dots via a QPen customdash + capstyle="round" (the previous
        # approach) proved unreliable ACROSS RENDER BACKENDS — it rendered
        # correctly (round) from QGIS Desktop on Windows but flat in the
        # headless Docker/Linux export, and switching that container from
        # xvfb-run to QT_QPA_PLATFORM=offscreen (2026-07-23) didn't fix it
        # either, so it isn't specifically an Xvfb/X11 quirk. Draw the dots
        # as literal small filled circles spaced along the line instead
        # (QgsMarkerLineSymbolLayer) — an actual circle geometry renders
        # identically everywhere, independent of the platform's QPen/pen-cap
        # support. dash_pt/gap_pt set the same cadence the user tuned by eye
        # (1pt dot : 4.5pt gap). Only admin1 currently passes dashed=True,
        # so this doesn't touch intl (solid) or disputed (still dotted via
        # the old QPen dot style below — short/simple enough not to have
        # shown the same flat-cap symptom).
        dash_pt, gap_pt = 1.0, 4.5
        dot = QgsSimpleMarkerSymbolLayer()
        dot.setShape(QgsSimpleMarkerSymbolLayerBase.Circle)
        dot.setSize(width_pt * PT)          # dot diameter == line width
        dot.setColor(QColor(hex_color))
        # setStrokeWidth(0.0) alone is NOT "no outline" — Qt treats a
        # 0-width pen as a "cosmetic" hairline that still renders (~1
        # device pixel) regardless of width, so the default black stroke
        # colour was still visibly ringing every dot (2026-07-24 user
        # report). setStrokeStyle(NoPen) actually disables the outline.
        dot.setStrokeStyle(Qt.PenStyle.NoPen)
        marker_sym = QgsMarkerSymbol()
        marker_sym.deleteSymbolLayer(0)
        marker_sym.appendSymbolLayer(dot)
        mline = QgsMarkerLineSymbolLayer(True, (dash_pt + gap_pt) * PT)
        mline.setPlacements(Qgis.MarkerLinePlacement.Interval)
        mline.setSubSymbol(marker_sym)
        sym = QgsLineSymbol()
        sym.deleteSymbolLayer(0)
        sym.appendSymbolLayer(mline)
    else:
        props = {"line_color": hex_color, "line_width": f"{width_pt * PT:.3f}"}
        if dotted:
            props["line_style"] = "dot"
        sym = QgsLineSymbol.createSimple(props)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()

def style_marker(layer, shape, hex_color, size_mm):
    sym = QgsMarkerSymbol.createSimple(
        {"name": shape, "color": hex_color, "size": str(size_mm),
         "outline_style": "no"})
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()

def style_national_capital(layer):
    """National capital = a black STAR sitting inside a white CIRCLE
    (two stacked symbol layers, mirroring the old ArcGIS gallery symbol)."""
    sym = QgsMarkerSymbol()
    sym.deleteSymbolLayer(0)                     # drop the default layer
    circle = QgsSimpleMarkerSymbolLayer()        # bottom: white disc + dark ring
    circle.setShape(QgsSimpleMarkerSymbolLayerBase.Circle)
    circle.setSize(STYLE["natl_circle_mm"])
    circle.setColor(QColor("#FFFFFF"))
    circle.setStrokeColor(QColor("#000000"))
    circle.setStrokeWidth(1.25)
    star = QgsSimpleMarkerSymbolLayer()          # top: black star (larger vs ring)
    star.setShape(QgsSimpleMarkerSymbolLayerBase.Star)
    star.setSize(STYLE["natl_star_mm"])
    star.setColor(QColor("#000000"))
    star.setStrokeWidth(0.0)
    sym.appendSymbolLayer(circle)
    sym.appendSymbolLayer(star)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()

def style_provincial_capital(layer):
    """Provincial capital = two concentric circles: a black centre dot inside
    a thin black ring (white-filled), matching the reference symbol."""
    sym = QgsMarkerSymbol()
    sym.deleteSymbolLayer(0)
    ring = QgsSimpleMarkerSymbolLayer()          # outer ring (white fill)
    ring.setShape(QgsSimpleMarkerSymbolLayerBase.Circle)
    ring.setSize(STYLE["prov_outer_mm"])
    ring.setColor(QColor("#FFFFFF"))
    ring.setStrokeColor(QColor("#000000"))
    ring.setStrokeWidth(0.85)
    dot = QgsSimpleMarkerSymbolLayer()           # inner filled dot
    dot.setShape(QgsSimpleMarkerSymbolLayerBase.Circle)
    dot.setSize(STYLE["prov_inner_mm"])
    dot.setColor(QColor("#000000"))
    dot.setStrokeWidth(0.0)
    sym.appendSymbolLayer(ring)
    sym.appendSymbolLayer(dot)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()

def admin1_lines_from_gadm(iso3_code, subject_layer):
    """Derive admin-1 boundary LINES from GADM's admin-1 POLYGONS. Used as a
    middle tier between UN ClearMap (best, but genuinely absent for some
    countries — Ukraine/Lebanon/Brazil all confirmed zero rows) and Natural
    Earth's admin1_lines (works, but its own line geometry is sometimes too
    coarse for a big country — Brazil again: some state-boundary segments
    are bare 2-8 point straight lines in that file, confirmed by direct
    inspection of the reprojected data, not a snap/simplify bug on our side).
    GADM's admin-1 polygons are already used elsewhere in this project
    (phase0_basemap_poc_wb_mpm.py) and are much more detailed.
    native:polygonstolines turns each polygon's own boundary into a line
    feature — adjacent states' shared borders end up duplicated (one copy
    from each side), which is harmless for display (they simply overlap).

    Unlike genuine boundary-LINE datasets (UN's own BDYTYP=6/8, or Natural
    Earth's admin1_lines), polygonstolines also includes each state's OUTER
    edge — the part that runs along the country's own coastline/international
    border, not just its interior divisions. Left in, that reads as an
    admin1 dash pattern drawn right on top of the coastline (2026-07-22 user
    report). Removed here by subtracting a thin buffer around the subject
    country's own boundary before returning.

    Returns None (never raises) on any failure so the caller can fall
    through to the Natural Earth tier."""
    try:
        gpkg = download(GADM_GPKG_URL_TMPL.format(iso3=iso3_code),
                        f"gadm41_{iso3_code}.gpkg")
        gadm = QgsVectorLayer(f"{gpkg}|layername=ADM_ADM_1", "gadm1_polys", "ogr")
        if not gadm.isValid() or gadm.featureCount() == 0:
            log(f"⚠ GADM admin-1 폴리곤 로드 실패/비어있음({iso3_code})")
            return None
        import processing
        res = processing.run("native:polygonstolines", {
            "INPUT": gadm, "OUTPUT": "TEMPORARY_OUTPUT"})
        out = res["OUTPUT"]
        if isinstance(out, str):
            out = QgsVectorLayer(out, "gadm1_lines", "ogr")
        try:
            metric_crs = QgsProject.instance().crs()
            out_metric = _to_crs(out, metric_crs)
            subj_metric = _to_crs(subject_layer, metric_crs)
            outer_edge = processing.run("native:polygonstolines", {
                "INPUT": subj_metric, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
            # Scale the exclusion buffer to the subject's own size (see
            # ADMIN1_COAST_EXCLUDE_BUFFER_M's comment) — 3% of the narrower
            # bbox dimension, floored/capped so it never goes below a
            # sensible minimum or above the flat value this was originally
            # tuned to (Brazil, whose bbox is so large the cap always wins,
            # so its behaviour is unchanged by this).
            _subj_ext = subj_metric.extent()
            _narrow_dim_m = min(_subj_ext.width(), _subj_ext.height())
            _buffer_dist_m = min(
                ADMIN1_COAST_EXCLUDE_BUFFER_M,
                max(ADMIN1_COAST_EXCLUDE_BUFFER_MIN_M,
                    _narrow_dim_m * ADMIN1_COAST_EXCLUDE_BUFFER_FRAC))
            outer_buffer = processing.run("native:buffer", {
                "INPUT": outer_edge, "DISTANCE": _buffer_dist_m,
                "DISSOLVE": True, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
            interior_only = processing.run("native:difference", {
                "INPUT": out_metric, "OVERLAY": outer_buffer,
                "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
            out = _to_crs(interior_only, QgsCoordinateReferenceSystem("EPSG:4326"))
        except Exception as ex:
            log(f"⚠ GADM admin-1 해안선 구간 제거 실패(그대로 진행): {ex}")
        return out
    except Exception as ex:
        log(f"⚠ GADM admin-1 처리 실패({iso3_code}): {ex}")
        return None

def clip_to_bbox(raw_layer, bbox_4326, out_name):
    """Clip a WORLD-spanning layer (Ocean, Ocean_Labels/marine polys) down to
    a local (s,w,n,e) EPSG:4326 bbox BEFORE it gets reprojected to the
    project's local UTM CRS.

    Why this matters: Ocean/marine-label source files cover the ENTIRE
    PLANET and were previously added to the map un-clipped, relying on the
    print layout's own extent to just not draw the rest. That's harmless for
    small/medium countries, but for a country wide enough to span many UTM
    zones (confirmed: Brazil, ~40° of longitude), QGIS's on-the-fly transform
    of geometry from the OPPOSITE SIDE OF THE WORLD through a UTM formula
    that's only valid within ~a few degrees of its own central meridian
    produces wildly distorted coordinates — verified by seeing real Indonesian
    sea names (e.g. "Molucca Sea", "Gulf of Buli") rendered on top of Brazil,
    and thin near-vertical sliver-polygon artifacts across the whole map.
    Clipping to the local bbox in EPSG:4326 FIRST means data from the other
    side of the globe never reaches the UTM transform at all."""
    try:
        import processing
        s, w, n, e = bbox_4326
        res = processing.run("native:extractbyextent", {
            "INPUT": raw_layer,
            "EXTENT": f"{w},{e},{s},{n} [EPSG:4326]",
            "CLIP": True,
            "OUTPUT": "TEMPORARY_OUTPUT"})
        out = res["OUTPUT"]
        if isinstance(out, str):
            out = QgsVectorLayer(out, out_name, "ogr")
        out.setName(out_name)
        return out
    except Exception as ex:
        log(f"⚠ bbox 클립 실패({out_name}) — 원본(전세계) 사용: {ex}")
        raw_layer.setName(out_name)
        return raw_layer

def clip_to_subject(raw_layer, subject_layer, out_name):
    """Clip an OSM layer to the subject-country polygon so features do NOT
    spill into neighbouring countries. Falls back to the raw layer if the
    Processing framework is unavailable."""
    try:
        import processing
        res = processing.run("native:clip", {
            "INPUT": raw_layer, "OVERLAY": subject_layer,
            "OUTPUT": "TEMPORARY_OUTPUT"})
        out = res["OUTPUT"]
        if isinstance(out, str):
            out = QgsVectorLayer(out, out_name, "ogr")
        out.setName(out_name)
        return out
    except Exception as ex:
        log(f"⚠ 클립 실패({out_name}) — 원본 사용: {ex}")
        raw_layer.setName(out_name)
        return raw_layer

def _to_crs(layer, target_crs):
    """Reproject layer to target_crs via Processing; no-op if already there.
    native:snapgeometries / native:simplifygeometries interpret their
    TOLERANCE parameter in the INPUT layer's own CRS units, NOT the QGIS
    project's display CRS — a layer loaded straight from GeoJSON is still
    EPSG:4326 (degrees) even after project.setCrs(UTM) has been called
    elsewhere. Passing a "metres" tolerance to a degrees-CRS layer means a
    300-unit tolerance is actually ~300 degrees (most of the globe): for
    snapgeometries' "closest point" search this happens to be harmless
    (nearest-point search finds the true nearest point regardless of how
    large the cutoff is), but for simplifygeometries' Douglas-Peucker it is
    catastrophic — verified empirically to collapse a 493-vertex boundary
    line down to 2 points, and an entire background-country reference layer
    down to empty geometry. Always reproject to a metric CRS before either
    algorithm."""
    if layer is None:
        return layer
    try:
        if layer.crs() == target_crs:
            return layer
        import processing
        res = processing.run("native:reprojectlayer", {
            "INPUT": layer, "TARGET_CRS": target_crs,
            "OUTPUT": "TEMPORARY_OUTPUT"})
        return res["OUTPUT"]
    except Exception:
        return layer

def snap_to_polygons(line_layer, ref_polygon_layers, tolerance_m, name):
    """Snap line_layer's vertices onto the nearest edge of ref_polygon_layers
    within tolerance_m (metres — see _to_crs for why both inputs are
    explicitly reprojected to the project's metric CRS first). Fixes
    topological drift between independently-digitized line/polygon datasets
    that are supposed to trace the SAME real-world boundary — e.g. UN
    ClearMap's boundary-LINE layer (BNDL) vs its own country-AREA layer
    (BNDA) aren't perfectly co-located even though both come from the UN,
    because they're maintained/generalized somewhat independently. Without
    this, the international-boundary line can visibly cross into the
    subject polygon's interior, drift outside it into the sea, or leave a
    sliver gap along admin-1 boundaries. Returns a layer in EPSG:4326 (to
    match the rest of the pipeline's convention), falling back to the
    un-snapped input if Processing is unavailable."""
    try:
        import processing
        refs = [r for r in ref_polygon_layers if r is not None]
        if not refs:
            return line_layer
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        metric_crs = QgsProject.instance().crs()
        line_metric = _to_crs(line_layer, metric_crs)
        refs_metric = [_to_crs(r, metric_crs) for r in refs]
        merged = refs_metric[0]
        if len(refs_metric) > 1:
            merged = processing.run("native:mergevectorlayers", {
                "LAYERS": refs_metric, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
        res = processing.run("native:snapgeometries", {
            "INPUT": line_metric, "REFERENCE_LAYER": merged,
            "TOLERANCE": tolerance_m,
            # BEHAVIOR=3: prefer closest point, do NOT insert new vertices.
            # BEHAVIOR=1 (insert extra vertices) was tried first but near
            # sharp turns/tripoints (e.g. Lebanon's NE corner) it spliced in
            # a reference vertex from an unrelated nearby edge (an adjacent
            # country's admin1 line, also within tolerance) out of order
            # with the line's existing vertices, producing a self-crossing
            # spike. Only moving EXISTING vertices to their nearest
            # reference point can't reorder the line, so it can't create
            # that kind of bowtie/spike artifact.
            "BEHAVIOR": 3,
            "OUTPUT": "TEMPORARY_OUTPUT"})
        out = res["OUTPUT"]
        if isinstance(out, str):
            out = QgsVectorLayer(out, name, "ogr")
        out = _to_crs(out, wgs84)
        out.setName(name)
        return out
    except Exception as ex:
        log(f"⚠ 경계선 스냅 실패({name}) — 원본 사용: {ex}")
        line_layer.setName(name)
        return line_layer

def simplify_line_for_display(line_layer, tolerance_m, name):
    """Generalize line_layer for cartographic display at this print scale,
    via Douglas-Peucker simplification (metres — see _to_crs for why the
    input is explicitly reprojected to the project's metric CRS first: this
    step is what actually surfaced the CRS-units bug, since an oversized
    degrees-tolerance visibly collapses the line instead of merely being a
    harmless no-op the way it was for snap_to_polygons). UN ClearMap
    boundary lines are digitized at full survey precision — real
    terrain-following zigzags only a few km across (verified against the
    raw UN source geometry — this is NOT a topology bug from
    snap_to_polygons or anywhere else in this script) render, at A3
    country-wide scale, as an apparent self-crossing 'spike' once the true
    detail is finer than the line's own stroke width (Lebanon's NE border,
    following a narrow river/ridge, is one example). tolerance_m reuses
    BOUNDARY_SNAP_TOLERANCE_M so both steps agree on 'how much positional
    wiggle is invisible at this print scale.' Returns a layer in EPSG:4326,
    falling back to the un-simplified input if Processing is unavailable."""
    try:
        import processing
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        metric_crs = QgsProject.instance().crs()
        line_metric = _to_crs(line_layer, metric_crs)
        res = processing.run("native:simplifygeometries", {
            "INPUT": line_metric, "METHOD": 0,   # Douglas-Peucker
            "TOLERANCE": tolerance_m,
            "OUTPUT": "TEMPORARY_OUTPUT"})
        out = res["OUTPUT"]
        if isinstance(out, str):
            out = QgsVectorLayer(out, name, "ogr")
        out = _to_crs(out, wgs84)
        out.setName(name)
        return out
    except Exception as ex:
        log(f"⚠ 국경선 단순화 실패({name}) — 원본 사용: {ex}")
        line_layer.setName(name)
        return line_layer

def rivers_layers_tiered(raw_path, subject_layer):
    """Rivers as up to TWO SEPARATE layers (Major_rivers_OSM / Minor_rivers_OSM)
    split via a definition-query subset string, instead of one layer with a
    rule-based renderer's 2 rules:
      * 'important' rivers (carry a wikidata/wikipedia tag -> notable rivers
        like the Dnieper) are drawn a little darker and thicker;
      * everything else (minor rivers) is only very slightly darker than the
        previous near-invisible setting.
    A rule-based layer's rules always render under a redundant parent "layer
    name" heading row in the legend (QGIS's Subgroup style) — two plain
    single-symbol layers instead each collapse straight to their own single
    row like every other layer here, with no extra header row to throw off
    the legend's column layout (2026-07-23 user report: blanking that
    heading's text left an empty row that misaligned everything below it).
    Same "split into two via definition query" pattern already used
    elsewhere in this script (Subject_/Countries_background,
    Intl_boundary_subject/other).
    Returns (major_layer, minor_layer) — minor_layer is None if the OSM data
    has neither tag field to split on (falls back to ONE 'Rivers_OSM' layer,
    all rivers, minor style, returned as major_layer)."""
    minor_sym = QgsLineSymbol.createSimple(
        {"line_color": STYLE["river_minor_rgba"],
         "line_width": f'{STYLE["river_minor_pt"] * PT:.3f}'})
    probe = QgsVectorLayer(raw_path, "rivers_probe", "ogr")
    actual = {f.name().lower(): f.name() for f in probe.fields()}
    have = [actual[c] for c in ("wikidata", "wikipedia") if c in actual]
    if not have:                       # no importance signal available
        lyr = clip_to_subject(probe, subject_layer, "Rivers_OSM")
        lyr.setRenderer(QgsSingleSymbolRenderer(minor_sym))
        lyr.triggerRepaint()
        return lyr, None

    expr = " or ".join(f'"{name}" is not null' for name in have)
    major_sym = QgsLineSymbol.createSimple(
        {"line_color": STYLE["river_major_rgba"],
         "line_width": f'{STYLE["river_major_pt"] * PT:.3f}'})
    _major_raw = QgsVectorLayer(raw_path, "rivers_major_raw", "ogr")
    _major_raw.setSubsetString(expr)
    lyr_major = clip_to_subject(_major_raw, subject_layer, "Major_rivers_OSM")
    lyr_major.setRenderer(QgsSingleSymbolRenderer(major_sym))
    lyr_major.triggerRepaint()

    _minor_raw = QgsVectorLayer(raw_path, "rivers_minor_raw", "ogr")
    _minor_raw.setSubsetString(f"NOT ({expr})")   # the old rule's setIsElse(True)
    lyr_minor = clip_to_subject(_minor_raw, subject_layer, "Minor_rivers_OSM")
    lyr_minor.setRenderer(QgsSingleSymbolRenderer(minor_sym))
    lyr_minor.triggerRepaint()
    return lyr_major, lyr_minor

def split_by_location(raw_layer, subject_layer, name_touching, name_other):
    """Split raw_layer into two layers by whether each CONNECTED PART
    touches subject_layer:
      * name_touching = parts that intersect/touch the subject country
        (i.e. the subject's OWN international boundary)
      * name_other     = every other part (other countries' boundaries
        caught inside the map extent)
    Either return value is None (and never added to the map/legend) if it
    ends up with zero features.

    Explodes multi-part features into single connected LineStrings FIRST
    (native:multiparttosingleparts), then classifies each PART as a whole
    via native:extractbylocation (intersects / disjoint):
      - Per-PART rather than per-raw-FEATURE, because UN BNDL occasionally
        stores one country-pair's border as a MultiLineString with several
        disconnected segments (e.g. ROU_UKR legitimately has two: the
        Maramureș border in the north AND a separate short stretch near
        the Danube Delta) — exploding first keeps each real segment intact
        for classification.
      - Whole-PART, NOT a buffer+clip geometry slice (tried first, then
        reverted): buffering the subject and clipping fragmented the
        subject's own genuine, continuous boundary line into many short
        alternating touching/non-touching pieces wherever its distance
        from the subject wobbled near the buffer threshold — producing a
        speckled/mottled line instead of one solid one. Since
        snap_to_polygons() already pulled the line's vertices onto the
        subject/background polygons' own edges, an exact intersects test
        on the whole (connected) part is both correct and stable — it
        won't slice a line mid-run the way distance-based clipping does.

    Runs in the project's metric CRS (see _to_crs) so the two
    intersects/disjoint tests are evaluated against consistently
    transformed geometry. Falls back to (raw_layer as name_touching, None)
    if Processing is unavailable, so the caller can still render a single
    uniform style."""
    try:
        import processing
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        metric_crs = QgsProject.instance().crs()
        raw_metric = _to_crs(raw_layer, metric_crs)
        subj_metric = _to_crs(subject_layer, metric_crs)
        parts = processing.run("native:multiparttosingleparts", {
            "INPUT": raw_metric, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
        res_touch = processing.run("native:extractbylocation", {
            "INPUT": parts, "PREDICATE": [0],   # 0 = intersects
            "INTERSECT": subj_metric, "OUTPUT": "TEMPORARY_OUTPUT"})
        res_other = processing.run("native:extractbylocation", {
            "INPUT": parts, "PREDICATE": [2],   # 2 = disjoint
            "INTERSECT": subj_metric, "OUTPUT": "TEMPORARY_OUTPUT"})
        touch, other = res_touch["OUTPUT"], res_other["OUTPUT"]
        if isinstance(touch, str):
            touch = QgsVectorLayer(touch, name_touching, "ogr")
        if isinstance(other, str):
            other = QgsVectorLayer(other, name_other, "ogr")
        touch = _to_crs(touch, wgs84)
        other = _to_crs(other, wgs84)
        if touch.featureCount() == 0:
            touch = None
        else:
            touch.setName(name_touching)
            QgsProject.instance().addMapLayer(touch)
        if other.featureCount() == 0:
            other = None
        else:
            other.setName(name_other)
            QgsProject.instance().addMapLayer(other)
        log(f"레이어 추가: {name_touching} "
            f"(features: {touch.featureCount() if touch else 0})")
        log(f"레이어 추가: {name_other} "
            f"(features: {other.featureCount() if other else 0})")
        return touch, other
    except Exception as ex:
        log(f"⚠ 경계선 분리 실패({name_touching}) — 단일 레이어로 유지: {ex}")
        raw_layer.setName(name_touching)
        QgsProject.instance().addMapLayer(raw_layer)
        return raw_layer, None

def set_labels(layer, field_or_expr, font_family, bold, size_pt, hex_color,
               is_expression=False, halo=False, halo_pt=0.5,
               point_offset_pt=None, priority=5, polygon_visible=False,
               italic=False):
    """point_offset_pt: for POINT layers only — pushes the label a small
    distance away from the marker (instead of centering on top of it) and
    marks the marker as an obstacle, so PAL (QGIS's label engine) routes
    labels around both the symbol and other nearby labels instead of
    stacking them on top of each other.
    polygon_visible: for big POLYGON layers (country names) — place the label
    on the VISIBLE part of the polygon rather than its whole-feature centroid.
    Without this, e.g. Russia's centroid is in Siberia (off the map) so its
    label never appears on a Ukraine-focused map."""
    fmt = QgsTextFormat()
    font = QFont(font_family)
    font.setBold(bold)
    font.setItalic(italic)
    fmt.setFont(font)
    fmt.setSize(size_pt)
    fmt.setSizeUnit(QgsUnitTypes.RenderPoints)   # sizes are in points
    fmt.setColor(QColor(hex_color))
    if halo:
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setColor(QColor("#FFFFFF"))
        buf.setSizeUnit(QgsUnitTypes.RenderPoints)
        buf.setSize(halo_pt)                       # white halo, size in points
        buf.setOpacity(1.0)
        fmt.setBuffer(buf)
    s = QgsPalLayerSettings()
    s.setFormat(fmt)
    s.fieldName = field_or_expr
    s.isExpression = is_expression
    s.priority = priority          # higher = less likely to be dropped/moved
    if point_offset_pt is not None:
        s.placement = QgsPalLayerSettings.OrderedPositionsAroundPoint
        s.predefinedPositionOrder = [
            QgsPalLayerSettings.QuadrantRight, QgsPalLayerSettings.QuadrantLeft,
            QgsPalLayerSettings.QuadrantAboveRight, QgsPalLayerSettings.QuadrantBelowRight,
            QgsPalLayerSettings.QuadrantAboveLeft, QgsPalLayerSettings.QuadrantBelowLeft,
        ]
        s.dist = point_offset_pt
        s.distUnits = QgsUnitTypes.RenderPoints
        s.obstacleSettings().setIsObstacle(True)   # the marker itself blocks labels
    if polygon_visible:
        s.placement = QgsPalLayerSettings.Horizontal
        s.centroidWhole = False          # centroid of the VISIBLE part only
        s.fitInPolygonOnly = False       # still show even if it doesn't fully fit
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()

def mark_as_obstacle(layer):
    """Make this layer's geometry act as an obstacle for OTHER layers'
    labels, without drawing any label text of its own (QgsPalLayerSettings
    .drawLabels=False keeps the obstacle bookkeeping active while suppressing
    the label itself). Used on the subject-country fill so a neighbouring
    country's name label (e.g. 'MOLDOVA' near Ukraine) gets
    pushed away from the subject's own territory instead of drifting across
    its border — PAL only avoids obstacles it knows about, and the subject
    fill wasn't registered as one until now."""
    s = QgsPalLayerSettings()
    s.fieldName = "''"       # constant empty-string expression — never shown
    s.isExpression = True
    s.drawLabels = False
    s.obstacleSettings().setIsObstacle(True)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()

# --- geometry / CRS ----------------------------------------------------------
def _walk_coords(coords, acc):
    """Recursively collect [lon,lat] pairs from any GeoJSON coordinate array."""
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            acc.append(coords)
        else:
            for c in coords:
                _walk_coords(c, acc)

def subject_feature(countries_path, iso3):
    """Return (bbox, exterior_rings) for the subject country.
    bbox = (south, west, north, east); rings = list of [lon,lat] rings
    (one per polygon part, e.g. mainland + islands)."""
    with open(countries_path, encoding="utf-8") as f:
        gj = json.load(f)
    for feat in gj.get("features", []):
        p = feat.get("properties", {})
        code = p.get("ADM0_A3") or p.get("adm0_a3") or p.get("ISO_A3")
        if code == iso3:
            geom = feat["geometry"]
            pts = []
            _walk_coords(geom["coordinates"], pts)
            lons = [c[0] for c in pts]; lats = [c[1] for c in pts]
            bbox = (min(lats), min(lons), max(lats), max(lons))
            t = geom["type"]
            rings = ([geom["coordinates"][0]] if t == "Polygon"
                     else [part[0] for part in geom["coordinates"]])
            return bbox, rings
    return None, None

def _ring_area(ring):
    """Shoelace formula — used to find the largest (mainland) ring."""
    a = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]; x2, y2 = ring[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0

def _feature_area(feat):
    """Total exterior-ring area of one GeoJSON feature (degree-space shoelace,
    holes ignored) — only used to compare RELATIVE size between a country's
    own polygon fragments, not for real-world area."""
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords:
        return 0.0
    if geom.get("type") == "Polygon":
        rings = [coords[0]]
    elif geom.get("type") == "MultiPolygon":
        rings = [part[0] for part in coords]
    else:
        return 0.0
    return sum(_ring_area(r) for r in rings)

def keep_largest_fragment(path):
    """Some ISO3CD codes cover several DISCONNECTED polygon fragments in UN
    ClearMap's BNDA layer, each with its own (different) ROMNAM — e.g. PRT =
    mainland Portugal + Azores Islands + Madeira Island as 3 separate rows.
    Treating all of them as "the subject country" breaks two things at once:
    the map extent stretches to cover every fragment (confirmed: Portugal's
    bbox spanned mainland-to-Azores, ~1500km into the Atlantic), and the
    displayed title/filename becomes whichever fragment the service happens
    to list first (confirmed: came out "Azores Islands" instead of
    "Portugal"). Keep only the largest fragment — the mainland/main landmass
    is always the biggest part — and overwrite the cache so every downstream
    reader (geojson_bbox_rings, add_vector, official_name lookup) sees just
    it. No-op for the ~95% of countries that are a single fragment."""
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    feats = gj.get("features", [])
    if len(feats) <= 1:
        return path
    best = max(feats, key=_feature_area)
    name = best.get("properties", {}).get("ROMNAM")
    log(f"  국가 폴리곤 조각 {len(feats)}개 중 최대 면적만 사용 → {name}")
    gj["features"] = [best]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(gj, f)
    return path

def simplify_ring(ring, max_points=20):
    """Down-sample a ring to ~max_points. We do NOT need precision here —
    this polygon is only used to tell Overpass 'search roughly here, not in
    the neighbouring country'; the EXACT country shape is applied afterwards
    in QGIS via clip_to_subject(). Overpass gets noticeably slower/flakier
    as the polygon grows (tested: 21 pts ≈ reliable, 40+ pts ≈ frequent
    504 Gateway Timeout on public mirrors) — so we deliberately keep it tiny."""
    if len(ring) <= max_points:
        return ring
    step = len(ring) / max_points
    idx = sorted(set(int(i * step) for i in range(max_points)))
    out = [ring[i] for i in idx]
    if out[0] != out[-1]:
        out.append(out[0])
    return out

def poly_filter_string(ring):
    """Overpass QL poly filter format: "lat1 lon1 lat2 lon2 ..." """
    return " ".join(f"{lat:.4f} {lon:.4f}" for lon, lat in ring)

def utm_epsg_from_bbox(bbox):
    """Pick a UTM zone EPSG from the bbox centre (CRS standardization)."""
    s, w, n, e = bbox
    lon_c = (w + e) / 2.0; lat_c = (s + n) / 2.0
    zone = int((lon_c + 180) // 6) + 1
    return (32600 if lat_c >= 0 else 32700) + zone

# --- OSM / Overpass ----------------------------------------------------------
def overpass(query, timeout=80):
    """Try each Overpass mirror in turn; retry on timeout/504/network error.
    We keep client-side timeout moderate — public mirrors either answer in
    ~30-40s or stall, so waiting minutes only delays failover. Rivers and
    roads are fetched as separate (lighter) requests with a short pause
    between them (see main) to avoid the per-IP rate limit that makes a 2nd
    back-to-back request fail fast with a 504/429; successful results are
    cached to disk so re-runs don't hit the network again."""
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": "UNOCC-Basemap-POC/0.1 (internal UNOCC test)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                log(f"  Overpass 성공: {url}")
                return json.load(r)
        except Exception as ex:
            last = ex
            log(f"  Overpass 실패({url}) → 다음 서버 시도: {ex}")
    raise last if last else RuntimeError("모든 Overpass 서버 실패")

def _elements_to_geojson_file(elements, filename):
    """Convert a list of Overpass elements (ways with geometry / nodes) to a
    GeoJSON file in the cache. Returns (path, feature_count)."""
    feats = []
    for el in elements:
        if el.get("type") == "way" and "geometry" in el:
            coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
            if len(coords) >= 2:
                feats.append({"type": "Feature", "properties": el.get("tags", {}),
                              "geometry": {"type": "LineString",
                                           "coordinates": coords}})
        elif el.get("type") == "node":
            feats.append({"type": "Feature", "properties": el.get("tags", {}),
                          "geometry": {"type": "Point",
                                       "coordinates": [el["lon"], el["lat"]]}})
    path = os.path.join(CACHE_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    return path, len(feats)

def _area_clause(poly_str, bbox):
    """Overpass area filter: prefer the simplified country polygon; fall back
    to the bounding rectangle if we could not build a polygon."""
    if poly_str:
        return f'(poly:"{poly_str}")'
    s, w, n, e = bbox
    return f'({s},{w},{n},{e})'

def fetch_osm_cached(poly_str, bbox, key, value, extra, out_file):
    """Fetch ONE OSM feature group, restricted to the country polygon.
    Results are cached to disk (keyed per country in out_file) and REUSED on
    the next run — so once a fetch succeeds you never wait for Overpass again
    for that country. Returns (geojson_path, count, from_cache).

    We fetch rivers and roads as SEPARATE requests (not one combined union):
    each smaller query is far less likely to hit the public mirror's gateway
    timeout than one big ~30k-element query. The per-IP rate limit between the
    two requests is handled by a short pause in main()."""
    path = os.path.join(CACHE_DIR, out_file)
    if os.path.exists(path) and os.path.getsize(path) > 2:
        try:
            with open(path, encoding="utf-8") as f:
                n = len(json.load(f).get("features", []))
            log(f"  OSM 캐시 재사용: {out_file} (features: {n})")
            return path, n, True
        except Exception:
            pass  # corrupt cache -> re-fetch below
    area = _area_clause(poly_str, bbox)
    q = f'[out:json][timeout:120];way["{key}"~"{value}"]{extra}{area};out geom;'
    log(f"OSM 요청: {key}={value} … (큰 국가는 1분+ 걸릴 수 있습니다)")
    osm = overpass(q, timeout=110)
    path, n = _elements_to_geojson_file(osm.get("elements", []), out_file)
    return path, n, False

# =============================================================================
#  UN OFFICIAL BOUNDARIES  (UN Geospatial "Clear Map" ArcGIS REST service)
#  Same authoritative data the old ArcGIS script used (fields ISO3CD/ROMNAM/
#  BDYTYP). Country areas include territories per the UN position (e.g. Crimea
#  is part of Ukraine). We query GeoJSON in EPSG:4326 within a bbox envelope.
# =============================================================================
UN_MAPSERVER  = ("https://geoservices.un.org/arcgis/rest/services/"
                 "ClearMap_Plain/MapServer")
UN_BNDA_LAYER = 105   # country/area polygons (detailed, L01-L04)
UN_BNDL_LAYER = 94    # boundary LINES (L06) — has BDYTYP: 0 coastline,
                      # 1 international, 6 admin-1, 8 autonomous-region (Crimea)

def fetch_un(layer_id, where, out_file, bbox=None, out_fields="ISO3CD,ROMNAM"):
    """Query a UN Clear Map layer as GeoJSON (EPSG:4326), cached per country.
    bbox=(s,w,n,e) restricts to an envelope. Returns the geojson path.

    Paginates via resultOffset/resultRecordCount — this service caps any
    single response at maxRecordCount=2000 features (confirmed via its own
    `?f=json` layer metadata). For most countries the bbox-restricted query
    never gets close to that, but for a country as wide as Brazil the padded
    bbox pulls in enough OTHER countries' boundary-line segments to actually
    hit the cap: verified directly — a Brazil fetch silently came back with
    exactly 2000 features, and 3 real segments (Brazil's own borders with
    Venezuela, French Guiana and Uruguay) were missing from it, sitting just
    past the cutoff on a second page. Without pagination this fails SILENTLY
    (no error, just an incomplete result), which is why it went unnoticed
    until a real Brazil map was visually checked against the country's actual
    borders."""
    path = os.path.join(CACHE_DIR, out_file)
    if os.path.exists(path) and os.path.getsize(path) > 2:
        try:
            with open(path, encoding="utf-8") as f:
                n = len(json.load(f).get("features", []))
            log(f"  UN 캐시 재사용: {out_file} (features: {n})")
            return path
        except Exception:
            pass  # corrupt -> re-fetch
    base_params = {"where": where, "outFields": out_fields, "f": "geojson",
                   "outSR": "4326", "returnGeometry": "true"}
    if bbox:
        s, w, n, e = bbox
        base_params.update({"geometry": f"{w},{s},{e},{n}",
                            "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                            "spatialRel": "esriSpatialRelIntersects"})
    log(f"UN 경계 요청: layer {layer_id} ({where}) …")

    page_size = 2000
    offset = 0
    combined = None
    last = None
    while True:
        params = dict(base_params, resultOffset=offset, resultRecordCount=page_size)
        url = f"{UN_MAPSERVER}/{layer_id}/query?" + urllib.parse.urlencode(params)
        page = None
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "UNOCC-Basemap-POC/0.1"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    page = json.loads(r.read())
                break
            except Exception as ex:
                last = ex
                log(f"  UN 요청 실패(재시도 {attempt+1}, offset={offset}): {ex}")
        if page is None:
            raise last if last else RuntimeError("UN 경계 요청 실패")
        feats = page.get("features", [])
        if combined is None:
            combined = page
            combined["features"] = list(feats)
        else:
            combined["features"].extend(feats)
        if len(feats) < page_size:
            break  # last page — got fewer than a full page back
        offset += page_size
        log(f"  UN 응답이 {page_size}개(서비스 한도)로 꽉 참 — 다음 페이지 요청 "
            f"(offset={offset})")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(combined, f)
    n = len(combined["features"])
    log(f"  UN 경계 수신: {out_file} (features: {n})")
    return path

def geojson_bbox_rings(path):
    """(bbox, exterior_rings) from a GeoJSON file's features (used for the UN
    subject file, which contains only the subject country)."""
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    allpts, rings = [], []
    for feat in gj.get("features", []):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords:
            continue
        _walk_coords(coords, allpts)
        if geom.get("type") == "Polygon":
            rings.append(coords[0])
        elif geom.get("type") == "MultiPolygon":
            rings.extend(part[0] for part in coords)
    if not allpts:
        return None, None
    lons = [p[0] for p in allpts]; lats = [p[1] for p in allpts]
    return (min(lats), min(lons), max(lats), max(lons)), rings

def expand_bbox(bbox, frac=0.25, min_deg=2.0):
    """Pad a (s,w,n,e) bbox so a query also picks up neighbouring countries."""
    s, w, n, e = bbox
    py = max((n - s) * frac, min_deg)
    px = max((e - w) * frac, min_deg)
    return (s - py, w - px, n + py, e + px)

def aspect_pad_bbox(bbox, aspect=408.0 / 246.0):
    """Widen a (s,w,n,e) degrees bbox to roughly the print layout's landscape
    map-frame aspect ratio (408x246mm — must match the QgsLayoutItemMap size
    in build_print_layout). QgsLayoutItemMap.zoomToExtent() widens whichever
    dimension is short to fit that aspect ratio when framing the final map —
    for a country whose own shape is taller/squarer than the page (e.g.
    Kenya), that widening happens EAST-WEST at print time, well past this
    bbox's own edges. Without pre-widening here too, the Countries_background
    / boundary-lines fetch envelope (expand_bbox, applied on top of this)
    stops short of that final view, leaving neighbouring countries that only
    become visible after the aspect-fit (e.g. Uganda, western Tanzania around
    Kenya) unfetched — they render as blank white gaps instead of the cream
    background fill."""
    s, w, n, e = bbox
    width, height = e - w, n - s
    if height <= 0 or width <= 0:
        return bbox
    if width / height < aspect:
        pad = (height * aspect - width) / 2.0
        w, e = w - pad, e + pad
    else:
        pad = (width / aspect - height) / 2.0
        s, n = s - pad, n + pad
    return (s, w, n, e)

# =============================================================================
#  PRINT LAYOUT (A3)  — title, category, agency, date, legend, scale bar,
#  north arrow, globe locator, disclaimer.  Values follow SOP "In Layout".
# =============================================================================
MM = QgsUnitTypes.LayoutMillimeters

def _add_label(layout, text, x, y, w, h, font_family, size_pt, hex_color,
               bold=False, align="left"):
    lbl = QgsLayoutItemLabel(layout)
    lbl.setText(text)
    font = QFont(font_family)
    font.setBold(bold)
    font.setPointSizeF(size_pt)
    lbl.setFont(font)
    lbl.setFontColor(QColor(hex_color))
    lbl.setHAlign({"left": Qt.AlignLeft, "center": Qt.AlignHCenter,
                   "right": Qt.AlignRight}[align])
    lbl.setVAlign(Qt.AlignVCenter)
    layout.addLayoutItem(lbl)
    lbl.attemptMove(QgsLayoutPoint(x, y, MM))
    lbl.attemptResize(QgsLayoutSize(w, h, MM))
    return lbl

def _frame(item, hex_color="#686868", width_mm=0.35):
    item.setFrameEnabled(True)
    item.setFrameStrokeColor(QColor(hex_color))
    item.setFrameStrokeWidth(QgsLayoutMeasurement(width_mm, MM))

def _rect_in_crs(bbox_4326, dst_crs, project, pad_frac=0.06):
    """(s,w,n,e) in EPSG:4326 -> padded QgsRectangle in dst_crs."""
    s, w, n, e = bbox_4326
    src = QgsCoordinateReferenceSystem("EPSG:4326")
    tr = QgsCoordinateTransform(src, dst_crs, project)
    r = tr.transformBoundingBox(QgsRectangle(w, s, e, n))
    dx, dy = r.width() * pad_frac, r.height() * pad_frac
    return QgsRectangle(r.xMinimum() - dx, r.yMinimum() - dy,
                        r.xMaximum() + dx, r.yMaximum() + dy)

def _layer_extent_in_crs(layer, dst_crs, project, pad_frac=0.06):
    """Transform a layer's own (QGIS-computed) extent into dst_crs and pad
    it. Used instead of rebuilding the bbox from raw GeoJSON coordinates so
    the padded extent is always exact for whatever country is loaded."""
    tr = QgsCoordinateTransform(layer.crs(), dst_crs, project)
    r = tr.transformBoundingBox(layer.extent())
    dx, dy = r.width() * pad_frac, r.height() * pad_frac
    return QgsRectangle(r.xMinimum() - dx, r.yMinimum() - dy,
                        r.xMaximum() + dx, r.yMaximum() + dy)

def build_print_layout(project, main_layers, locator_layers, bbox_4326,
                       country_name, subject_layer, has_disputed=False,
                       admin1_source="UN"):
    """Assemble an A3-landscape print layout and register it with the project
    (visible under Project ▸ Layouts). Returns the layout. has_disputed adds
    a sentence to the disclaimer explaining the dotted boundary style, only
    when the map actually has a disputed/undetermined line in view.
    admin1_source ("UN"/"GADM"/"Natural Earth") adds a sentence naming the
    fallback source whenever admin-1 lines didn't come from UN ClearMap.

    Layout: a bottom info STRIP (title/category/agency/date/scale bar, legend,
    sources/disclaimer side by side) below a nearly full-page map — matching
    the reference layout the user supplied 2026-07-22
    (04_Maps_Outputs/02_Examples/reference-Lebanon-30x24in-20260721.png),
    replacing the previous "title banner on top + full-width disclaimer strip
    at the very bottom" arrangement."""
    proj_crs = project.crs()
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(f"{country_name} Basemap A3")
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(420, 297, MM))

    # Bottom info strip occupies y=258..291 (33mm); the map fills everything
    # above it instead of a top title banner. (Widened from 29mm — even at
    # 3 legend columns, 29mm cut off 2 of ~9 rows past the footer's own
    # bottom edge, 2026-07-22.)
    FOOTER_TOP = 258

    # --- main map ------------------------------------------------------------
    m = QgsLayoutItemMap(layout)
    layout.addLayoutItem(m)
    m.attemptMove(QgsLayoutPoint(6, 6, MM))
    m.attemptResize(QgsLayoutSize(408, FOOTER_TOP - 8, MM))
    m.setCrs(proj_crs)
    m.setLayers(main_layers)                       # lock to the basemap layers
    # Zoom to the subject layer's own extent (not setExtent with a hand-built
    # bbox) — zoomToExtent() explicitly recalculates to fit the map item's
    # aspect ratio, so the whole country is always fully visible regardless
    # of its shape (fixes elongated/narrow countries getting cropped, e.g.
    # Lebanon, in the exported PNG).
    m.zoomToExtent(_layer_extent_in_crs(subject_layer, proj_crs, project))
    m.setBackgroundColor(QColor("#FFFFFF"))
    _frame(m, "#686868", 0.35)                     # SOP: #686868, ~1pt

    # --- globe locator (inset, upper-left) -----------------------------------
    loc = QgsLayoutItemMap(layout)
    layout.addLayoutItem(loc)
    # Same (x, y) as the main map — left/top edges flush.
    loc.attemptMove(QgsLayoutPoint(6, 6, MM))
    loc.attemptResize(QgsLayoutSize(64, 64, MM))
    loc.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    loc.setLayers(locator_layers)
    loc.setExtent(_rect_in_crs(bbox_4326,
                  QgsCoordinateReferenceSystem("EPSG:3857"), project,
                  pad_frac=6.0))                   # wide context view
    loc.setBackgroundColor(QColor("#EAF1F6"))
    _frame(loc, "#686868", 0.3)

    # --- north arrow (bottom-right corner of the MAP, not the footer) --------
    # A plain bold "N" over a solid triangle, matching the reference map
    # (04_Maps_Outputs/02_Examples/reference-Lebanon-30x24in-20260721.png) —
    # not the bundled QGIS NorthArrow_*.svg set, which are all far more
    # ornate (full circular compass roses or two-tone N-S diamond arrows)
    # and don't match that simple style. Two stacked labels instead of an
    # SVG keeps this exact and independent of which SVGs a given QGIS
    # install/Docker image happens to ship.
    na_y = FOOTER_TOP - 26
    # The triangle glyph ("▲") doesn't fill the whole bottom edge of its own
    # label box (font glyphs sit with some padding above/below their em
    # box), so even with the shaft's TOP positioned exactly at the
    # triangle's box BOTTOM (na_y + 16, no gap in box coordinates), the
    # visible glyph and the visible shaft still had a gap between them
    # (2026-07-23 user report). Drop N+triangle down by _NA_DROP_MM,
    # WITHOUT moving the shaft, so the triangle's box — and with it the
    # glyph inside — pushes down far enough to visibly overlap the shaft's
    # fixed top edge (2026-07-24 user request: "삼각형과 직사각형이 살짝
    # 겹치도록").
    _NA_DROP_MM = 2.5
    _add_label(layout, "N", 396, na_y + _NA_DROP_MM, 12, 7, "Arial", 13.0,
               "#000000", bold=True, align="center")
    _add_label(layout, "▲", 396, na_y + 7 + _NA_DROP_MM, 12, 9, "Arial", 18.0,
               "#000000", bold=True, align="center")
    # Shaft below the triangle (2026-07-23 user request — "지금 삼각형 밑에
    # 작대기 추가하면 될 것 같아") so the whole thing reads as an arrow, not
    # just a bare triangle. A thin filled QgsLayoutItemShape rather than a
    # 3rd text glyph — same reasoning as the triangle/N above about staying
    # independent of any particular font's glyph rendering, but a shaft has
    # no good single-character Unicode equivalent anyway. Position is
    # deliberately NOT shifted by _NA_DROP_MM — see comment above.
    na_shaft = QgsLayoutItemShape(layout)
    na_shaft.setShapeType(QgsLayoutItemShape.Rectangle)
    layout.addLayoutItem(na_shaft)
    _shaft_w, _shaft_h = 1.4, 6.0
    na_shaft.attemptMove(QgsLayoutPoint(396 + (12 - _shaft_w) / 2, na_y + 16, MM))
    na_shaft.attemptResize(QgsLayoutSize(_shaft_w, _shaft_h, MM))
    na_shaft.setSymbol(QgsFillSymbol.createSimple(
        {"color": "#000000", "outline_style": "no"}))

    # --- bottom info strip: outer frame ---------------------------------------
    footer = QgsLayoutItemShape(layout)
    footer.setShapeType(QgsLayoutItemShape.Rectangle)
    layout.addLayoutItem(footer)
    footer.attemptMove(QgsLayoutPoint(6, FOOTER_TOP, MM))
    footer.attemptResize(QgsLayoutSize(408, 33, MM))
    footer.setSymbol(QgsFillSymbol.createSimple({
        "color": "255,255,255,255", "outline_color": "#686868",
        "outline_width": "0.35"}))

    # --- footer, left third: title / category / agency / date / scale bar ----
    # Footer is only 29mm tall — these 5 rows are deliberately compact
    # (7/5/4/4mm) so the scale bar still gets a full 8mm at the bottom
    # instead of overflowing past the footer's own bottom edge, which is
    # what a taller stack did on the first pass (2026-07-22).
    _add_label(layout, country_name.upper(), 10, FOOTER_TOP + 1, 140, 7,
               "Arial", 16.0, "#000000", bold=True, align="left")
    _add_label(layout, MAP_CATEGORY.upper(), 10, FOOTER_TOP + 8, 140, 5,
               "Arial", 11.0, "#828282", bold=True, align="left")
    _add_label(layout, AGENCY_TITLE.upper(), 10, FOOTER_TOP + 13, 140, 4,
               "Arial", 9.0, "#1FBBEE", bold=True, align="left")
    # %-d (no leading zero) is a Linux/glibc-only strftime extension — this
    # script also runs on Windows (HOW_TO_RUN.md desktop workflow), where it
    # raises ValueError. str(day) sidesteps the platform difference.
    _today = date.today()
    _add_label(layout,
               f"Publication Date: {_today.day} {_today.strftime('%B %Y')}",
               10, FOOTER_TOP + 17, 140, 4, "Tahoma", 7.5, "#000000", align="left")

    # Scale bar length is derived from the SUBJECT's own real-world size, not
    # QgsLayoutItemScaleBar.applyDefaultSize() — that sizes off the full
    # linked map's visible extent, and this A3-landscape frame is padded
    # much wider (east-west) than a north-south-elongated country like
    # Lebanon actually is, so it always rounded up to a generic 100km
    # regardless of how small the country truly is (confirmed: Lebanon and
    # Brazil both rendered "100km"/"3000km" scale max before this change).
    # User-specified rule (2026-07-22): shorter for a smaller country
    # (Lebanon ≈ 18km, per the reference map); extended (2026-07-23) to
    # candidates up to 1000km so large countries (e.g. Brazil) don't get
    # clamped to a too-short bar relative to their real-world size.
    _subj_extent_m = QgsCoordinateTransform(
        subject_layer.crs(), proj_crs, project).transformBoundingBox(
        subject_layer.extent())
    _subj_narrow_m = min(_subj_extent_m.width(), _subj_extent_m.height())
    _SCALEBAR_CANDIDATES_KM = [
        1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90, 100,
        150, 200, 250, 300, 400, 500, 600, 750, 900, 1000]
    _scalebar_total_km = min(
        _SCALEBAR_CANDIDATES_KM,
        key=lambda c: abs(c - (_subj_narrow_m / 1000.0) * 0.3))

    sb = QgsLayoutItemScaleBar(layout)
    sb.setStyle("Line Ticks Up")   # simple tick line, matches the reference map
    sb.setLinkedMap(m)
    sb.setUnits(QgsUnitTypes.DistanceKilometers)
    sb.setUnitLabel("km")
    sb.setNumberOfSegments(2)
    sb.setNumberOfSegmentsLeft(0)
    sb.setUnitsPerSegment(_scalebar_total_km / 2)
    sb.setHeight(2.5)   # thin bar — the tight footer has ~8mm total to work with
    layout.addLayoutItem(sb)
    sb.attemptMove(QgsLayoutPoint(10, FOOTER_TOP + 21, MM))
    sb.attemptResize(QgsLayoutSize(120, 8, MM))

    # --- footer, middle third: legend ------------------------------------------
    try:
        leg = QgsLayoutItemLegend(layout)
        leg.setLinkedMap(m)
        leg.setTitle("Legend")
        leg.setAutoUpdateModel(False)              # snapshot, then prune
        root = leg.model().rootGroup()
        # "_other" entries are the faint 'other countries' copies — no legend entry
        drop = ("Ocean", "World_Hillshade", "Countries_background", "Subject_",
                "Intl_boundary_other", "Disputed_other")
        for tl in list(root.findLayers()):
            if any(k in tl.name() for k in drop):
                root.removeChildNode(tl)
            else:
                tl.setName(tl.name().replace("_", " "))  # legend-only display name
        leg.setResizeToContents(True)
        # 3 columns (not 2) — with ~10 rows, 2 columns needed ~5 rows of
        # vertical space and visibly overflowed past the footer's own
        # bottom edge, clipped in the export (2026-07-22, first attempt at
        # this layout). 3 columns needs only ~4 rows.
        leg.setColumnCount(4)
        _title_font = QFont("Arial")
        _title_font.setPointSizeF(7.0)
        leg.setStyleFont(QgsLegendStyle.Title, _title_font)
        # QFont's constructor point size is an int — a float (6.5) throws
        # "arguments did not match any overloaded call" and silently
        # skipped the whole legend (caught by this function's own
        # try/except). setPointSizeF() afterward gets the fractional size.
        _symbol_label_font = QFont("Arial")
        _symbol_label_font.setPointSizeF(6.0)
        leg.setStyleFont(QgsLegendStyle.SymbolLabel, _symbol_label_font)
        # Group/Subgroup rows (shown for any layer with a multi-rule/multi-
        # category renderer — e.g. Rivers used to be one before it was split
        # into separate Major/Minor layers, see rivers_layers_tiered())
        # default to a much larger built-in style than Title/SymbolLabel —
        # left unstyled it rendered ~2x every other row's text height
        # (2026-07-22, spotted alongside the National-capital fix). Match it
        # to SymbolLabel for visual consistency, defensively, in case a
        # future layer here ever needs a multi-rule renderer again.
        _group_font = QFont("Arial")
        _group_font.setPointSizeF(6.0)
        leg.setStyleFont(QgsLegendStyle.Group, _group_font)
        leg.setStyleFont(QgsLegendStyle.Subgroup, _group_font)
        # Cap every symbol swatch to the SAME box — without this, rows whose
        # true on-map symbol size differs (National capital's compound
        # star+circle vs. Provincial capitals' ring+dot) render at different
        # apparent sizes in the legend (2026-07-22 user report), and because
        # each row's symbol column was then a different width, the labels
        # themselves started at different x offsets and read as not
        # left-aligned. A uniform box fixes both at once — but the box must
        # be sized to fit the LARGEST symbol row (National capital, below)
        # or that one row's icon overflows the box and its label alone
        # shifts right, breaking alignment again (2026-07-22 user report,
        # second pass — this exact symptom returned once National capital's
        # legend symbol was made bigger than the box that had matched the
        # older, smaller target).
        LEGEND_SYMBOL_BOX_MM = 4.2
        leg.setSymbolWidth(LEGEND_SYMBOL_BOX_MM)
        leg.setSymbolHeight(LEGEND_SYMBOL_BOX_MM)
        layout.addLayoutItem(leg)
        # Per-row legend-only symbol size override. QGIS legend previews
        # render a marker at its TRUE relative size (deliberate QGIS
        # behaviour, matching the Layers panel) — setSymbolWidth/Height and
        # QgsSymbolLegendNode.setUserPatchSize() both only reserve/request a
        # box, neither rescales the symbol drawn inside it (confirmed by
        # testing both, 2026-07-22). The actual per-row override is
        # setCustomSymbol(): clone the real symbol, scale each symbol
        # layer's size, and substitute the clone for JUST this legend row —
        # zero effect on the real on-map marker.
        #
        # Sizes are relative adjustments from the previous legend-only
        # target (3.5mm, i.e. LEGEND_SYMBOL_BOX_MM before this pass), per
        # explicit user request 2026-07-22: National capital 20% bigger,
        # Provincial capitals 30% smaller — both scaled from that same
        # 3.5mm baseline so the two remain proportionate to each other, and
        # the box above is sized to the larger of the two (National
        # capital) so no row's icon overflows it.
        _LEGEND_ONLY_TARGETS_MM = {
            "National capital": 3.5 * 1.2,   # 4.2mm — must equal LEGEND_SYMBOL_BOX_MM
            "Provincial capitals": 3.5 * 0.7,  # 2.45mm
        }
        _LEGEND_ONLY_BASE_MM = {
            "National capital": STYLE["natl_circle_mm"],
            "Provincial capitals": STYLE["prov_outer_mm"],
        }
        # Fixed, absolute (NOT proportionally-scaled) legend-only ring
        # thickness. First attempt scaled strokeWidth by the same factor as
        # the symbol's size (so the ring:diameter RATIO matched the on-map
        # symbol) — still read as too thick (2026-07-23 user report, a
        # second time, after that pass). A small icon apparently needs a
        # visually thinner ring than straight ratio-preservation gives, so
        # this now just pins the legend ring to one small absolute width
        # regardless of the row's own scale factor.
        _LEGEND_ONLY_STROKE_MM = 0.18
        for tl in root.findLayers():
            if tl.name() in _LEGEND_ONLY_TARGETS_MM:
                for node in leg.model().layerLegendNodes(tl):
                    if isinstance(node, QgsSymbolLegendNode):
                        base_symbol = node.symbol()
                        if base_symbol is not None:
                            small_symbol = base_symbol.clone()
                            scale = (_LEGEND_ONLY_TARGETS_MM[tl.name()] /
                                     _LEGEND_ONLY_BASE_MM[tl.name()])
                            for i in range(small_symbol.symbolLayerCount()):
                                sl = small_symbol.symbolLayer(i)
                                sl.setSize(sl.size() * scale)
                                if (hasattr(sl, "setStrokeWidth")
                                        and sl.strokeWidth() > 0):
                                    sl.setStrokeWidth(_LEGEND_ONLY_STROKE_MM)
                            node.setCustomSymbol(small_symbol)
        # Anchor by the legend's UPPER-left corner inside the footer's
        # middle third (150..300mm) rather than resizeToContents' natural
        # top-left, so it lines up with the title block's own top edge.
        leg.setReferencePoint(QgsLayoutItem.UpperLeft)
        leg.attemptMove(QgsLayoutPoint(155, FOOTER_TOP + 1, MM))
        leg.setBackgroundColor(QColor(255, 255, 255, 0))
    except Exception as ex:
        log(f"⚠ 범례 생성 건너뜀: {ex}")

    # --- footer, right third: sources + disclaimer -----------------------------
    disclaimer_text = DISCLAIMER_TEXT
    if has_disputed:
        disclaimer_text += (
            " Dotted boundary lines indicate a line that has not yet been "
            "finally determined, or an armistice/administrative line "
            "(UN ClearMap classification).")
    if admin1_source != "UN":
        disclaimer_text += (
            f" Admin-1 (provincial) boundary lines do not exist in UN "
            f"ClearMap for {country_name}, so {admin1_source} data was "
            f"used instead.")
    _add_label(layout, disclaimer_text, 308, FOOTER_TOP + 1, 104, 27,
               "Tahoma", 7.0, "#828282", align="left")

    # register with the project so it shows in Project ▸ Layouts
    lm = project.layoutManager()
    existing = lm.layoutByName(layout.name())
    if existing:
        lm.removeLayout(existing)
    lm.addLayout(layout)
    return layout

def _filename_slug(name):
    """Official country name -> filesystem-safe slug for the SOP filename
    pattern: spaces become underscores; characters Windows forbids in a
    filename (<>:"/\\|?*) are dropped. Accents/apostrophes are kept as-is
    (NTFS handles them fine, and this matches names already used elsewhere
    in this project, e.g. "Côte d'Ivoire")."""
    slug = re.sub(r'[<>:"/\\|?*]', "", name)
    return slug.strip().replace(" ", "_")

def _next_sop_version(out_dir, yymmdd, slug):
    """SOP filenames are '<YYMMDD>_<Country>_Basemap_vNN' (GIS Workflow.md
    §9) — NN restarts at 1 each day per country and bumps by 1 every time
    the SAME country is regenerated on the SAME day, so re-runs never
    overwrite a previous day's output."""
    pattern = os.path.join(out_dir, f"{yymmdd}_{slug}_Basemap_v*.pdf")
    versions = [int(m.group(1)) for p in glob.glob(pattern)
                if (m := re.search(r"_v(\d+)\.pdf$", p, re.IGNORECASE))]
    return max(versions, default=0) + 1

def save_and_export_layout(layout, base_dir, official_name):
    """Save the layout as a .qpt template; optionally export PDF + PNG(300)."""
    # 02_GIS_Projects/qpt is the canonical spot for print-layout templates
    # per the project's folder taxonomy (sits next to 02_GIS_Projects/qml/).
    # OUTPUT_TAG subfolder keeps this variant's templates/exports separate
    # from the other phase0_basemap_poc_*.py variants.
    qpt_dir = os.path.join(base_dir, "..", "..", "02_GIS_Projects", "qpt",
                           OUTPUT_TAG)
    os.makedirs(qpt_dir, exist_ok=True)
    qpt = os.path.abspath(os.path.join(qpt_dir, "unocc_basemap_A3.qpt"))
    ok = layout.saveAsTemplate(qpt, QgsReadWriteContext())
    log(f"QPT 템플릿 저장: {qpt}  ({'성공' if ok else '실패'})")
    if EXPORT_LAYOUT:
        out_dir = os.path.join(EXPORT_DIR, OUTPUT_TAG)
        os.makedirs(out_dir, exist_ok=True)
        # SOP 파일명 규칙 (GIS Workflow.md §9): <YYMMDD>_<Country>_Basemap_vNN
        # 예: 260706_Ukraine_Basemap_v01  (레이아웃 자체의 이름/제목은 그대로
        # "{official_name} Basemap A3" — QGIS의 Project ▸ Layouts 메뉴와
        # HOW_TO_RUN.md 안내는 그 이름을 그대로 씀. 파일명만 SOP 규칙 적용)
        yymmdd = date.today().strftime("%y%m%d")
        slug = _filename_slug(official_name)
        version = _next_sop_version(out_dir, yymmdd, slug)
        filename_base = f"{yymmdd}_{slug}_Basemap_v{version:02d}"
        exporter = QgsLayoutExporter(layout)
        pdf = os.path.abspath(os.path.join(out_dir, f"{filename_base}.pdf"))
        exporter.exportToPdf(pdf, QgsLayoutExporter.PdfExportSettings())
        img = QgsLayoutExporter.ImageExportSettings()
        img.dpi = 300
        png = os.path.abspath(os.path.join(out_dir, f"{filename_base}.png"))
        exporter.exportToImage(png, img)
        log(f"내보내기 완료: {pdf}")
        log(f"내보내기 완료(300DPI): {png}")

# =============================================================================
#  MAIN
# =============================================================================
def main():
    project = QgsProject.instance()
    project.removeAllMapLayers()
    log(f"대상 국가: {COUNTRY_NAME} ({ISO3_CODE})")

    # --- 0. base open data (always needed) -----------------------------------
    p_ocean   = download(NE["ocean"],     "ne_ocean.geojson")
    p_ctry    = download(NE["countries"], "ne_countries.geojson")  # locator + fallback
    p_places  = download(NE["places"],    "ne_places_10m.geojson")  # capitals (10m)
    p_marine  = download(NE["marine"],    "ne_marine_polys.geojson")  # sea/ocean names

    # --- 1. boundaries source: UN official (preferred) or Natural Earth ------
    # UN source variables (set when use_un): p_subj, p_bg, p_bounds
    p_subj = p_bg = p_bounds = None
    p_intl = p_adm1 = None
    use_un = False
    if USE_UN_BOUNDARIES:
        try:
            _t_un = time.time()
            p_subj = fetch_un(UN_BNDA_LAYER, f"ISO3CD='{ISO3_CODE}'",
                              f"un_subj_{ISO3_CODE}.geojson")
            p_subj = keep_largest_fragment(p_subj)
            bbox, rings = geojson_bbox_rings(p_subj)
            if not bbox:
                raise RuntimeError("UN 서비스에서 대상국을 못 찾음 (ISO3 확인)")
            env = expand_bbox(aspect_pad_bbox(bbox))
            log(f"  UN fetch bbox: {[round(v, 2) for v in env]} "
                f"(가로 {env[3]-env[1]:.1f}° x 세로 {env[2]-env[0]:.1f}°)")
            p_bg = fetch_un(UN_BNDA_LAYER, f"ISO3CD<>'{ISO3_CODE}'",
                            f"un_bg_{ISO3_CODE}.geojson", bbox=env)
            p_bounds = fetch_un(UN_BNDL_LAYER, "1=1",
                                f"un_bounds_{ISO3_CODE}.geojson", bbox=env,
                                out_fields="BDYTYP,ISO3CD")
            use_un = True
            log(f"✅ UN 공식 경계 사용 (크림반도 = 우크라이나 등 UN 입장 반영) "
                f"— fetch {time.time() - _t_un:.1f}초")
        except Exception as ex:
            log(f"⚠ UN 경계 사용 불가 → Natural Earth로 대체: {ex}")
            use_un = False
    if not use_un:
        p_intl  = download(NE["intl_lines"],  "ne_intl_lines.geojson")
        p_adm1  = download(NE["admin1_lines"],"ne_admin1_lines_10m.geojson")
        bbox, rings = subject_feature(p_ctry, ISO3_CODE)
        if not bbox:
            log(f"⚠ '{ISO3_CODE}'를 countries에서 못 찾음. ISO3_CODE를 확인하세요.")
            return

    # --- CRS standardization to UTM -----------------------------------------
    epsg = utm_epsg_from_bbox(bbox)
    project.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{epsg}"))
    log(f"프로젝트 CRS → EPSG:{epsg} (자동 선택 UTM)")

    # Build a small (~20 point) polygon filter for OSM requests. This keeps
    # Overpass from also fetching data in the big neighbouring-country slice
    # that sits inside the country's bounding RECTANGLE but outside its
    # actual shape (this was the main cause of slow/failing river queries
    # for elongated countries like Ukraine).
    poly_str = None
    if rings:
        main_ring = max(rings, key=_ring_area)
        poly_str = poly_filter_string(simplify_ring(main_ring, max_points=20))

    # --- 2. add layers  (added BOTTOM → TOP; each new layer goes on top) -----
    # Final stack (top→bottom):
    #   National capital / Provincial capitals / Intl(subject, other) / Admin1
    #   / Roads / Rivers / Coastline (outline only, from Ocean's own geometry)
    #   / Countries_background(cream) / Subject(solid white) / Ocean (fill)
    #   / World Hillshade
    # The subject country is opaque white (no hillshade hole — see
    # subject_fill, 2026-07-22). The bottom hillshade only shows faintly
    # through neighbouring countries' slightly-transparent cream fill
    # (country_bg_opacity), not through the subject itself.

    # (bottom) World Hillshade — only shows through neighbouring countries'
    # slightly-transparent fill now (see subject_fill above)
    lyr_hs = None
    if SHOW_HILLSHADE:
        lyr_hs = QgsRasterLayer(HILLSHADE_XYZ, "World_Hillshade", "wms")
        if lyr_hs.isValid():
            QgsProject.instance().addMapLayer(lyr_hs)
            log("레이어 추가: World_Hillshade (DEM 음영, 바닥)")
        else:
            log("⚠ World_Hillshade 로드 실패(인터넷/프록시?) — 지형 없이 진행")
            lyr_hs = None

    # Ocean (fill) + Ocean_Labels (named seas/gulfs, e.g. "Black Sea" — label
    # only, no fill, so it doesn't change the ocean's appearance). Both source
    # files cover the WHOLE PLANET, so clip to a local bbox FIRST — see
    # clip_to_bbox()'s docstring for why this is required (not optional) for
    # wide countries like Brazil.
    render_bbox = expand_bbox(aspect_pad_bbox(bbox))
    lyr_ocean = lyr_marine = None
    _raw_ocean = QgsVectorLayer(p_ocean, "Ocean_raw", "ogr")
    if _raw_ocean.isValid():
        lyr_ocean = clip_to_bbox(_raw_ocean, render_bbox, "Ocean")
        QgsProject.instance().addMapLayer(lyr_ocean)
        log(f"레이어 추가: Ocean  (features: {lyr_ocean.featureCount()})")
    _raw_marine = QgsVectorLayer(p_marine, "Ocean_Labels_raw", "ogr")
    if _raw_marine.isValid():
        lyr_marine = clip_to_bbox(_raw_marine, render_bbox, "Ocean_Labels")
        QgsProject.instance().addMapLayer(lyr_marine)
        log(f"레이어 추가: Ocean_Labels  (features: {lyr_marine.featureCount()})")
    # Subject (transparent) + neighbours (cream). Field/label names and source
    # files differ between UN (ISO3CD/ROMNAM) and Natural Earth (ADM0_A3/ADMIN).
    if use_un:
        fld_name = "ROMNAM"
        lyr_subj = add_vector(p_subj, f"Subject_{ISO3_CODE}")   # UN: only subject
        lyr_bg   = add_vector(p_bg,   "Countries_background")   # UN: excludes subj
    else:
        _probe = QgsVectorLayer(p_ctry, "_probe", "ogr")
        fld_code = field_lookup(_probe, ["ADM0_A3", "ISO_A3", "SOV_A3"])
        fld_name = field_lookup(_probe, ["ADMIN", "ADM0NAME", "NAME_LONG", "NAME"])
        lyr_subj = add_vector(p_ctry, f"Subject_{ISO3_CODE}",
                              subset=f'"{fld_code}" = \'{ISO3_CODE}\'')
        lyr_bg = add_vector(p_ctry, "Countries_background",
                            subset=f'"{fld_code}" <> \'{ISO3_CODE}\'')
    # Unify the displayed country name to the OFFICIAL name: UN's ROMNAM when
    # using UN boundaries, else Natural Earth's ADMIN/ADM0NAME as best-effort
    # fallback — NOT necessarily what the user typed into COUNTRY_NAME (typos/
    # alternate spellings like "Korea" vs "South Korea" shouldn't leak into
    # the printed title). Falls back to COUNTRY_NAME if the layer is empty.
    official_name = COUNTRY_NAME
    if lyr_subj and lyr_subj.featureCount() > 0:
        val = next(lyr_subj.getFeatures())[fld_name]
        if val:
            official_name = str(val)
    # PSE's largest fragment is "West Bank" (bigger than Gaza) — correct by
    # keep_largest_fragment()'s area rule, but a poor country-level label.
    # Same override the country-picker list uses, kept in sync on purpose.
    if ISO3_CODE == "PSE":
        official_name = "Palestine"
    # Coastline: a SECOND layer loaded from the SAME source file as Ocean
    # (p_ocean), styled as an outline-only stroke (see symbology section
    # below). Reusing Ocean's own geometry guarantees the line is pixel-exact
    # against the ocean fill — no longer sourced from a separate dataset (UN
    # BDYTYP=0 lines, or NE's own coastline file) that never quite matched
    # ne_50m_ocean.geojson's fill boundary. It's a SEPARATE layer (not just
    # an outline baked into lyr_ocean's own symbol) and placed near the TOP
    # of the draw stack (see main_layers below) on purpose: lyr_ocean itself
    # sits near the BOTTOM of the stack, so an outline drawn only as part of
    # its own symbol would get painted OVER by the opaque Countries_background
    # fill everywhere except the subject country's own (transparent) coast —
    # i.e. neighbouring countries' coastlines would silently have no visible
    # line. This top-level copy stays visible everywhere.
    # Clipped the same way as lyr_ocean/lyr_marine (fresh load, not reusing
    # lyr_ocean directly since add_vector() takes a path) — this was the
    # actual remaining source of the vertical sliver-line artifact on wide
    # countries (Brazil): this layer is a SEPARATE raw (whole-planet) load
    # from lyr_ocean, so clipping lyr_ocean alone didn't fix it. Confirmed by
    # its position near the TOP of the stack, above even the (now opaque
    # white) subject fill — matching where the artifact was visible.
    lyr_coast = None
    _raw_coast = QgsVectorLayer(p_ocean, "Coastline_raw", "ogr")
    if _raw_coast.isValid():
        lyr_coast = clip_to_bbox(_raw_coast, render_bbox, "Coastline")
        QgsProject.instance().addMapLayer(lyr_coast)
        log(f"레이어 추가: Coastline  (features: {lyr_coast.featureCount()})")

    # Rivers + roads (OSM) — SEPARATE cached requests, clip each & add -------
    #   * Rivers = NAMED rivers only (cartographic practice + smaller volume).
    #   * Roads  = motorway|trunk only ('primary' ~doubles data & caused
    #     timeouts; roads here are just a faint backdrop).
    #   * Each group is a separate request (lighter -> less likely to 504),
    #     cached per-country and reused on re-run, with a short pause between
    #     the two live requests to dodge the per-IP rate limit.
    lyr_rivers_major = lyr_rivers_minor = lyr_roads = None
    if FETCH_OSM and lyr_subj:
        try:
            pr, nr, cached = fetch_osm_cached(
                poly_str, bbox, "waterway", "river", '["name"]',
                f"osm_rivers_{ISO3_CODE}.geojson")
            log(f"  강 features(원본): {nr}")
            lyr_rivers_major, lyr_rivers_minor = rivers_layers_tiered(pr, lyr_subj)
            if lyr_rivers_major: QgsProject.instance().addMapLayer(lyr_rivers_major)
            if lyr_rivers_minor: QgsProject.instance().addMapLayer(lyr_rivers_minor)
        except Exception as ex:
            log(f"⚠ 강 수집 실패(무시하고 진행): {ex}")
            cached = True  # don't pause before roads if rivers didn't hit net
        if not cached:
            time.sleep(5)   # be polite between two live Overpass requests
        try:
            pr, nr, _ = fetch_osm_cached(
                poly_str, bbox, "highway", "^(motorway|trunk)$", "",
                f"osm_roads_{ISO3_CODE}.geojson")
            log(f"  도로 features(원본): {nr}")
            lyr_roads = clip_to_subject(QgsVectorLayer(pr, "roads_raw", "ogr"),
                                        lyr_subj, "Roads_primary_OSM")
            QgsProject.instance().addMapLayer(lyr_roads)
        except Exception as ex:
            log(f"⚠ 도로 수집 실패(무시하고 진행): {ex}")

    # Admin-1 boundary lines (dashed) + international boundary lines.
    # Both are snapped onto the subject/background AREA polygons' own edges
    # first (snap_to_polygons) — the boundary-LINE dataset (UN BNDL, or NE's
    # own admin1/intl line files) is digitized independently of the
    # country-AREA polygons, so without this a line can visibly cross into
    # the polygon's interior, or its endpoint can drift past the coast into
    # the ocean.
    # Pre-merge subject+background ONCE here rather than letting
    # snap_to_polygons() re-merge them on every call (it's invoked up to 3x
    # below, for admin1/intl/disputed), and SIMPLIFY the merged reference —
    # UN BNDA country polygons are digitized at full coastline/border detail
    # (tens of thousands of vertices per country), but snapping is only
    # meaningful within BOUNDARY_SNAP_TOLERANCE_M (300m) anyway, so building
    # native:snapgeometries' spatial index against that full-resolution
    # geometry (done fresh on every one of the 3 calls) was the actual cost
    # — measured at ~200s for Lebanon despite only ~10 background countries
    # / 84 line features (i.e. the slowdown is vertex density, not feature
    # count or fetch-bbox size). Simplifying to 50m — well under the 300m
    # snap tolerance — collapses that vertex density without changing which
    # edge anything snaps to.
    _t_snap_prep = time.time()
    _snap_refs = [r for r in (lyr_subj, lyr_bg) if r is not None]
    if _snap_refs:
        try:
            import processing
            # Reproject to the project's metric CRS BEFORE simplifying —
            # these layers are still EPSG:4326 (degrees) here, and
            # native:simplifygeometries interprets TOLERANCE in the input's
            # own CRS units (see _to_crs docstring). Getting this wrong
            # previously collapsed every background country to an EMPTY
            # geometry (a "50" degrees tolerance is ~5500km) — silently, since
            # this reference is only used internally by snap_to_polygons'
            # "closest point" search, which degrades gracefully to a no-op
            # when the reference is empty, rather than visibly breaking.
            metric_crs = QgsProject.instance().crs()
            refs_metric = [_to_crs(r, metric_crs) for r in _snap_refs]
            merged = refs_metric[0]
            if len(refs_metric) > 1:
                merged = processing.run("native:mergevectorlayers", {
                    "LAYERS": refs_metric, "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
            simplified = processing.run("native:simplifygeometries", {
                "INPUT": merged, "METHOD": 0,   # Douglas-Peucker
                "TOLERANCE": 50,                # metres, << 300m snap tolerance
                "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
            _snap_refs = [simplified]   # already metric_crs; snap_to_polygons' own _to_crs() is then a no-op
        except Exception as ex:
            log(f"⚠ 스냅 기준 레이어 사전 병합/단순화 실패(그대로 진행): {ex}")
    log(f"  스냅 기준 레이어 준비: {time.time() - _t_snap_prep:.1f}초")
    if use_un:
        # UN: admin-1 (6) + autonomous-region (8, e.g. Crimea) clipped to the
        # subject so neighbours' internal boundaries don't clutter the map.
        # Check ISO3CD directly (attribute, not geometry) FIRST to decide
        # UN-vs-NE — confirmed by direct inspection that UN ClearMap's BNDL
        # layer has ZERO BDYTYP 6/8 rows tagged ISO3CD to Ukraine or
        # Lebanon at all (while Poland/Romania or Syria/Turkey DO have
        # theirs), so this is a genuine per-country coverage gap, not a
        # filter/query bug. Relying on clip_to_subject()'s post-snap
        # featureCount() here was tried first and DIDN'T reliably detect
        # the gap: a neighbouring country's admin1 line often has an
        # endpoint sitting exactly at the shared international border
        # (e.g. a Syrian governorate boundary terminating at the
        # Lebanon-Syria line), and snap_to_polygons' "closest point" can
        # snap that endpoint onto the SUBJECT's edge instead of the
        # neighbour's — clip_to_subject then keeps a tiny, visually
        # invisible sliver, so featureCount() > 0 even though there's no
        # real admin1 coverage to show.
        _a1_iso_probe = QgsVectorLayer(p_bounds, "adm1_iso_probe", "ogr")
        _a1_iso_probe.setSubsetString(
            f'"BDYTYP" IN (6, 8) AND "ISO3CD" = \'{ISO3_CODE}\'')
        admin1_source = "UN"
        if _a1_iso_probe.featureCount() > 0:
            _a1_raw = QgsVectorLayer(p_bounds, "adm1_raw", "ogr")
            _a1_raw.setSubsetString('"BDYTYP" IN (6, 8)')
            _a1_raw = snap_to_polygons(_a1_raw, _snap_refs,
                                       BOUNDARY_SNAP_TOLERANCE_M, "adm1_snapped")
            _a1_raw = simplify_line_for_display(
                _a1_raw, BOUNDARY_SNAP_TOLERANCE_M, "adm1_simplified")
            lyr_adm1 = clip_to_subject(_a1_raw, lyr_subj, "Admin1_boundaries")
        else:
            lyr_adm1 = None
        if lyr_adm1 is None or lyr_adm1.featureCount() == 0:
            log(f"⚠ UN admin1 데이터 없음({ISO3_CODE}) → GADM admin-1 폴리곤에서 "
                f"경계선 추출 시도")
            admin1_source = "GADM"
            _gadm_lines = admin1_lines_from_gadm(ISO3_CODE, lyr_subj)
            if _gadm_lines is not None and _gadm_lines.featureCount() > 0:
                _gadm_lines = snap_to_polygons(
                    _gadm_lines, _snap_refs, BOUNDARY_SNAP_TOLERANCE_M,
                    "adm1_gadm_snapped")
                _gadm_lines = simplify_line_for_display(
                    _gadm_lines, BOUNDARY_SNAP_TOLERANCE_M, "adm1_gadm_simplified")
                lyr_adm1 = clip_to_subject(_gadm_lines, lyr_subj,
                                          "Admin1_boundaries")
                log(f"  GADM admin-1 경계선 사용 (features: "
                    f"{lyr_adm1.featureCount() if lyr_adm1 else 0})")
        if lyr_adm1 is None or lyr_adm1.featureCount() == 0:
            log("⚠ GADM도 실패/비어있음 → Natural Earth admin1로 대체")
            admin1_source = "Natural Earth"
            p_adm1_ne = download(NE["admin1_lines"], "ne_admin1_lines_10m.geojson")
            _adm1_ne_probe = QgsVectorLayer(p_adm1_ne, "_a1_ne", "ogr")
            a1_ne_code = field_lookup(_adm1_ne_probe,
                                      ["ADM0_A3", "adm0_a3", "sr_adm0_a3"])
            _a1_ne_raw = QgsVectorLayer(p_adm1_ne, "adm1_ne_raw", "ogr")
            if a1_ne_code:
                _a1_ne_raw.setSubsetString(f'"{a1_ne_code}" = \'{ISO3_CODE}\'')
            _a1_ne_raw = snap_to_polygons(_a1_ne_raw, _snap_refs,
                                          BOUNDARY_SNAP_TOLERANCE_M,
                                          "adm1_ne_snapped")
            lyr_adm1 = simplify_line_for_display(
                _a1_ne_raw, BOUNDARY_SNAP_TOLERANCE_M, "Admin1_boundaries")
        if lyr_adm1 is not None and lyr_adm1.featureCount() > 0:
            QgsProject.instance().addMapLayer(lyr_adm1)
        else:
            lyr_adm1 = None
        # Subject vs other split done DIRECTLY by attribute (ISO3CD LIKE
        # pattern) rather than a spatial intersects test. Confirmed by direct
        # inspection: BDYTYP=1 rows' ISO3CD is reliably populated as a
        # "XXX_YYY" country-PAIR code (e.g. "BRA_VEN", "BRA_GUF") — a LIKE
        # match on the subject's own ISO3 code is simpler and more robust
        # than geometry intersection, which struggled precisely for a
        # country the size of Brazil (see the UTM/reprojection notes
        # elsewhere in this file — the same distortion that broke Ocean/
        # Coastline could just as easily nudge a snapped line off a
        # "touches" test). User request 2026-07-22. Disputed/undetermined
        # lines below keep the OLD spatial split — no attribute on that
        # BDYTYP range reliably identifies "the subject's own" segment the
        # way ISO3CD does for BDYTYP=1.
        _intl_subj_raw = QgsVectorLayer(p_bounds, "intl_subj_raw", "ogr")
        _intl_subj_raw.setSubsetString(
            f'"BDYTYP" = 1 AND "ISO3CD" LIKE \'%{ISO3_CODE}%\'')
        _intl_subj_raw = snap_to_polygons(_intl_subj_raw, _snap_refs,
                                          BOUNDARY_SNAP_TOLERANCE_M,
                                          "intl_subj_snapped")
        lyr_intl_subj = simplify_line_for_display(
            _intl_subj_raw, BOUNDARY_SNAP_TOLERANCE_M, "Intl_boundary_subject")
        if lyr_intl_subj.featureCount() == 0:
            lyr_intl_subj = None
        else:
            # split_by_location() used to do this addMapLayer() internally
            # for both halves of the old spatial split — replacing it with
            # this attribute-based split dropped that registration, so the
            # legend (which mirrors the PROJECT's own registered layers, not
            # just whatever QgsLayoutItemMap.setLayers() was given) silently
            # never showed "Intl boundary subject" even though it still
            # rendered fine on the map itself (2026-07-22, found by counting
            # legend rows against the expected list).
            QgsProject.instance().addMapLayer(lyr_intl_subj)

        _intl_other_raw = QgsVectorLayer(p_bounds, "intl_other_raw", "ogr")
        _intl_other_raw.setSubsetString(
            f'"BDYTYP" = 1 AND "ISO3CD" NOT LIKE \'%{ISO3_CODE}%\'')
        _intl_other_raw = snap_to_polygons(_intl_other_raw, _snap_refs,
                                           BOUNDARY_SNAP_TOLERANCE_M,
                                           "intl_other_snapped")
        lyr_intl_other = simplify_line_for_display(
            _intl_other_raw, BOUNDARY_SNAP_TOLERANCE_M, "Intl_boundary_other")
        if lyr_intl_other.featureCount() == 0:
            lyr_intl_other = None
        else:
            QgsProject.instance().addMapLayer(lyr_intl_other)  # dropped from
            # the legend by name further down, same as before this rewrite
        # Disputed / undetermined boundary segments — BDYTYP 2 'Special
        # boundary line', 3 'Armistice, undetermined or administrative
        # line', 4 'Other line of separation'. Without these, spots like the
        # India-Pakistan Line of Control around Jammu & Kashmir, or the
        # Sudan-South Sudan boundary, simply have NO line at all (BDYTYP=1
        # alone doesn't cover them), which reads as a gap/error on the map.
        # UN-only: Natural Earth's fallback file doesn't carry this coding.
        _disputed_raw = QgsVectorLayer(p_bounds, "disputed_raw", "ogr")
        _disputed_raw.setSubsetString('"BDYTYP" IN (2, 3, 4)')
        _disputed_raw = snap_to_polygons(_disputed_raw, _snap_refs,
                                         BOUNDARY_SNAP_TOLERANCE_M,
                                         "disputed_snapped")
        _disputed_raw = simplify_line_for_display(
            _disputed_raw, BOUNDARY_SNAP_TOLERANCE_M, "disputed_simplified")
    else:
        admin1_source = "Natural Earth"   # whole-country UN fallback — no UN data at all
        _adm1_probe = QgsVectorLayer(p_adm1, "_a1", "ogr")
        a1_code = field_lookup(_adm1_probe, ["ADM0_A3", "adm0_a3", "sr_adm0_a3"])
        a1_subset = f'"{a1_code}" = \'{ISO3_CODE}\'' if a1_code else None
        lyr_adm1 = add_vector(p_adm1, "Admin1_boundaries", subset=a1_subset)
        lyr_adm1 = snap_to_polygons(lyr_adm1, _snap_refs,
                                    BOUNDARY_SNAP_TOLERANCE_M, "Admin1_boundaries")
        lyr_adm1 = simplify_line_for_display(lyr_adm1, BOUNDARY_SNAP_TOLERANCE_M,
                                             "Admin1_boundaries")
        QgsProject.instance().addMapLayer(lyr_adm1)
        _intl_raw = QgsVectorLayer(p_intl, "intl_raw", "ogr")
        _intl_raw = snap_to_polygons(_intl_raw, _snap_refs,
                                     BOUNDARY_SNAP_TOLERANCE_M, "intl_snapped")
        _intl_raw = simplify_line_for_display(_intl_raw, BOUNDARY_SNAP_TOLERANCE_M,
                                              "intl_simplified")
        # Natural Earth's boundary-lines file has no ISO3CD-pair attribute
        # to filter on (that's a UN ClearMap-specific convention), so this
        # fallback path keeps the spatial split (see split_by_location
        # docstring).
        lyr_intl_subj, lyr_intl_other = split_by_location(
            _intl_raw, lyr_subj, "Intl_boundary_subject", "Intl_boundary_other")
        _disputed_raw = None
    log(f"  국경선 스냅(admin1/intl/disputed) 총 소요: "
        f"{time.time() - _t_snap_prep:.1f}초")
    # NOTE: previously tried snapping Countries_background's own polygon
    # edge onto the finalized international-boundary LINE here (to make the
    # cream fill's edge match the line exactly). Reverted — it crashed the
    # script (QGIS canvas showed unstyled default-colour layers, meaning
    # execution never reached the styling section below). Suspected cause:
    # when snap_to_polygons() fails internally for a POLYGON input (never
    # verified whether native:snapgeometries actually supports that; it's
    # only ever been used on LINE layers elsewhere in this script) it falls
    # back to returning the SAME lyr_bg object unchanged, and the
    # add-new/remove-old swap that followed then deleted that one-and-only
    # registered copy of lyr_bg out from under itself, leaving a dangling
    # reference that blew up the first time anything touched it. Needs a
    # from-scratch, carefully-guarded reattempt (or a different approach
    # entirely) rather than reusing snap_to_polygons() on a polygon input
    # blind.
    # lyr_intl_subj / lyr_intl_other are already set above — by attribute
    # (ISO3CD LIKE) when using UN data, or by the spatial split when using
    # the Natural Earth fallback.
    # Disputed/undetermined lines: split the SAME way as Intl boundary, so a
    # disputed segment that doesn't touch the subject country reads as
    # background context (faded, matching Intl_boundary_other's colour)
    # rather than as prominently as the subject's own border.
    lyr_disputed_subj = lyr_disputed_other = None
    if _disputed_raw is not None and _disputed_raw.featureCount() > 0:
        lyr_disputed_subj, lyr_disputed_other = split_by_location(
            _disputed_raw, lyr_subj, "Disputed_subject", "Disputed_other")
    # Provincial (admin-1) + national capitals.
    # Filter by ISO3 CODE (ADM0_A3), NOT by country name: Natural Earth's
    # ADM0NAME may differ from what the user typed (e.g. user 'Korea' vs NE
    # 'South Korea'), which silently returned 0 capitals. ISO3 is what the
    # user reliably enters. We also OR in the name match as a safety net.
    places_probe = QgsVectorLayer(p_places, "_pl", "ogr")
    pl_iso   = field_lookup(places_probe, ["ADM0_A3", "SOV_A3", "ISO_A3"])
    pl_adm0  = field_lookup(places_probe, ["ADM0NAME", "SOV0NAME", "ADMIN"])
    pl_class = field_lookup(places_probe, ["FEATURECLA"])
    pl_name  = field_lookup(places_probe, ["NAME", "NAMEASCII"])
    country_match = []
    if pl_iso:
        country_match.append(f'"{pl_iso}" = \'{ISO3_CODE}\'')
    if pl_adm0:
        country_match.append(f'"{pl_adm0}" = \'{COUNTRY_NAME}\'')
    country_clause = "(" + " OR ".join(country_match) + ")"
    # Provincial capitals = 'Admin-1 %' PLUS 'Admin-0 region capital'.
    # Natural Earth files devolved/constituent-country capitals (e.g. UK's
    # Edinburgh, Cardiff, Belfast; Bosnia's Banja Luka; Serbia's Novi Sad;
    # Portugal's Funchal) under 'Admin-0 region capital', NOT 'Admin-1 %' —
    # without this OR, those countries silently get zero provincial capitals.
    prov_class_clause = ('("{c}" LIKE \'Admin-1 %\' OR '
                         '"{c}" = \'Admin-0 region capital\')').format(c=pl_class)
    prov_subset = f'{prov_class_clause} AND {country_clause}'
    lyr_prov = add_vector(p_places, "Provincial_capitals", subset=prov_subset)
    cap_top_n(lyr_prov, PROVINCIAL_CAP_THRESHOLD, PROVINCIAL_CAP_KEEP,
              prov_subset)   # trim only countries with too many
    # National capital (top)
    lyr_natl = add_vector(
        p_places, "National_capital",
        subset=f'"{pl_class}" LIKE \'Admin-0 capital%\' AND {country_clause}')

    # --- 3. apply UNOCC symbology --------------------------------------------
    log("UNOCC 심볼로지 적용 중 …")
    if lyr_ocean: style_fill(lyr_ocean, STYLE["ocean_fill"])
    if lyr_marine:
        style_fill(lyr_marine, transparent=True)   # label-only, no visible fill
        fam, sz, col = STYLE["lbl_ocean"]
        set_labels(lyr_marine, 'upper("name")', fam, False, sz, col,
                   is_expression=True, italic=True, polygon_visible=True,
                   priority=2)   # low priority: yields to boundaries/capitals
    if lyr_bg:
        style_fill(lyr_bg, STYLE["country_bg_fill"])
        lyr_bg.setOpacity(STYLE["country_bg_opacity"])
        f, b, sz, col = STYLE["lbl_country"]
        set_labels(lyr_bg, f'upper("{fld_name}")', f, b, sz, col,
                   is_expression=True, polygon_visible=True,
                   halo=True, halo_pt=STYLE["country_halo_pt"])  # fixes RUSSIA label
    if lyr_subj:
        style_fill(lyr_subj, STYLE["subject_fill"])
        mark_as_obstacle(lyr_subj)   # keep neighbour labels off the subject
    if lyr_coast:
        coast_color, coast_pt = STYLE["coastline"]
        style_fill(lyr_coast, transparent=True,
                   outline_color=coast_color, outline_width_pt=coast_pt)
    # rivers already styled in rivers_layers_tiered() — nothing to do here
    if lyr_roads:
        style_line(lyr_roads, *STYLE["roads"])
        lyr_roads.setOpacity(STYLE["roads_opacity"])     # slightly more visible
    # dashed=True (not dotted) — SOP calls for "4pt, dash", and dotted=True
    # was rendering via QGIS's built-in dot preset, which at this line width/
    # vertex density reads as a near-solid line (2026-07-22 user report:
    # "dash가 너무 길어서 실선처럼 보여"). dashed=True uses this file's own
    # custom short dash/gap pattern (see style_line docstring), built
    # specifically for this.
    if lyr_adm1: style_line(lyr_adm1, *STYLE["admin_boundary"], dashed=True)
    if lyr_intl_subj: style_line(lyr_intl_subj, *STYLE["intl_boundary"])
    if lyr_intl_other:
        style_line(lyr_intl_other, STYLE["intl_boundary_other_rgba"],
                   STYLE["intl_boundary_other_pt"])
    if lyr_disputed_subj:
        style_line(lyr_disputed_subj, *STYLE["disputed_boundary"], dotted=True)
    if lyr_disputed_other:
        style_line(lyr_disputed_other, STYLE["intl_boundary_other_rgba"],
                   STYLE["intl_boundary_other_pt"], dotted=True)
    if lyr_prov:
        style_provincial_capital(lyr_prov)               # concentric circles
        f, b, sz, col = STYLE["lbl_prov_capital"]
        set_labels(lyr_prov, pl_name, f, b, sz, col,
                   halo=True, halo_pt=STYLE["prov_halo_pt"],
                   point_offset_pt=STYLE["prov_label_offset_pt"], priority=5)
    if lyr_natl:
        style_national_capital(lyr_natl)                 # circle + star
        f, b, sz, col = STYLE["lbl_natl_capital"]
        set_labels(lyr_natl, f'upper("{pl_name}")', f, b, sz, col,
                   is_expression=True,
                   point_offset_pt=STYLE["natl_label_offset_pt"], priority=7)

    # --- 4. zoom to the subject country --------------------------------------
    if iface and lyr_subj:
        iface.setActiveLayer(lyr_subj)
        iface.zoomToActiveLayer()
        iface.mapCanvas().refresh()

    # --- 5. build the A3 print layout ----------------------------------------
    if MAKE_LAYOUT:
        log("인쇄 레이아웃(A3) 구성 중 …")
        # main map layers, top → bottom (only the ones that exist)
        main_layers = [l for l in (lyr_natl, lyr_prov, lyr_intl_subj,
                                    lyr_intl_other, lyr_disputed_subj,
                                    lyr_disputed_other, lyr_adm1,
                                    lyr_roads, lyr_rivers_major, lyr_rivers_minor,
                                    lyr_coast, lyr_bg,
                                    lyr_subj, lyr_marine, lyr_ocean, lyr_hs) if l]
        # globe-locator layers: subject in red over all-countries grey.
        # Added to the project WITHOUT the layer tree so they don't clutter
        # the main canvas; the locator map references them via setLayers().
        loc_world = QgsVectorLayer(p_ctry, "locator_world", "ogr")  # NE world
        if use_un:                                 # UN subject (incl. Crimea)
            loc_subj = QgsVectorLayer(p_subj, "locator_subject", "ogr")
        else:
            loc_subj = QgsVectorLayer(p_ctry, "locator_subject", "ogr")
            loc_subj.setSubsetString(f'"{fld_code}" = \'{ISO3_CODE}\'')
        style_fill(loc_world, "#CCCCCC")          # SOP: other countries grey
        style_fill(loc_subj,  "#FF0000")          # SOP: AOI red
        project.addMapLayer(loc_world, False)
        project.addMapLayer(loc_subj,  False)

        layout = build_print_layout(
            project, main_layers, [loc_subj, loc_world], bbox, official_name,
            lyr_subj, lyr_disputed_subj is not None or lyr_disputed_other is not None,
            admin1_source=admin1_source)
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            base_dir = CACHE_DIR   # script pasted, not run from a file
        save_and_export_layout(layout, base_dir, official_name)
        log("레이아웃 완료 → QGIS 상단 Project ▸ Layouts ▸ "
            f"'{official_name} Basemap A3' 로 열어 확인/조정하세요.")

    log("완료 ✅  레이어 스타일은 Layer ▸ 우클릭 ▸ Export ▸ Save as QGIS "
        "Layer Style File (.qml), 레이아웃이 qpt로 Export 되었습니다.")

main()
