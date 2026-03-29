#!/usr/bin/env python3
"""
Fetch polygon overlay layers for Cape Cod map.
Saves to static/:

Already-completed files are skipped automatically.
Pass --force to re-fetch everything.
"""
import json
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

FORCE = "--force" in sys.argv

STATIC = Path(__file__).parent / "static"
STATIC.mkdir(parents=True, exist_ok=True)

# Cape Cod bounding box (xmin, ymin, xmax, ymax)
CAPE_BBOX = (-70.87, 41.52, -69.93, 42.09)

# ── Helpers ────────────────────────────────────────────────────────────────

def fetch_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_paged_bbox(base_url, extra_params=None, page_size=1000):
    """ArcGIS REST paged query using a bounding-box geometry filter."""
    import urllib.error
    xmin, ymin, xmax, ymax = CAPE_BBOX
    base_params = {
        "geometry":     f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel":   "esriSpatialRelIntersects",
        "where":        "1=1",
        "outFields":    "*",
        "f":            "geojson",
        "outSR":        "4326",
        "resultRecordCount": page_size,
    }
    if extra_params:
        base_params.update(extra_params)
    features, offset = [], 0
    while True:
        base_params["resultOffset"] = offset
        url = base_url + "?" + urllib.parse.urlencode(base_params)
        print(f"    offset {offset}...")
        try:
            data = fetch_json(url)
        except urllib.error.HTTPError as e:
            print(f"    HTTP {e.code} at offset {offset} — saving {len(features)} features collected so far")
            break
        batch = data.get("features", [])
        features.extend(batch)
        print(f"    got {len(batch)} (total {len(features)})")
        if not data.get("exceededTransferLimit") or len(batch) < page_size:
            break
        offset += len(batch)
        time.sleep(0.2)
    return features


def fetch_simple(base_url, where="1=1", out_fields="*", max_records=2000):
    """ArcGIS REST query without bbox — for Cape-specific services."""
    params = {
        "where":     where,
        "outFields": out_fields,
        "f":         "geojson",
        "outSR":     "4326",
    }
    if max_records:
        params["resultRecordCount"] = max_records
    url = base_url + "?" + urllib.parse.urlencode(params)
    print(f"    {url[:110]}...")
    return fetch_json(url).get("features", [])


def _round_coords(obj, precision=5):
    if isinstance(obj, float):
        return round(obj, precision)
    if isinstance(obj, list):
        return [_round_coords(v, precision) for v in obj]
    return obj


def save(features, path):
    for f in features:
        if f.get("geometry") and f["geometry"].get("coordinates"):
            f["geometry"]["coordinates"] = _round_coords(f["geometry"]["coordinates"])
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    kb = path.stat().st_size // 1024
    print(f"  Saved {path.name} ({kb} KB, {len(features)} features)")


def cached(path):
    """Return True if path exists with features and --force was not passed."""
    if FORCE or not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        if data.get("features"):
            print(f"  Skipping {path.name} (cached, {len(data['features'])} features — use --force to re-fetch)")
            return True
    except Exception:
        pass
    return False


# ── FEMA Flood Zones ───────────────────────────────────────────────────────

FLOOD_ZONE_RISK = {
    # Coastal high hazard (wave action)
    "VE": "coastal", "V": "coastal",
    # 100-year / Special Flood Hazard Area
    "AE": "high", "A": "high", "AH": "high", "AO": "high", "AR": "high", "A99": "high",
    # 500-year / moderate
    "X": "moderate",
}

def decode_flood(props):
    zone = (props.get("FLD_ZONE") or "").strip()
    subty = (props.get("ZONE_SUBTY") or "").strip()
    risk = FLOOD_ZONE_RISK.get(zone, "high")  # all fetched features are SFHA=T
    return {
        "zone":    zone,
        "subtype": subty,
        "risk":    risk,
    }


def fetch_flood_zones():
    path = STATIC / "flood_zones.geojson"
    if cached(path): return
    print("\nFetching FEMA flood zones (NFHL layer 28)...")
    base = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
    raw = fetch_paged_bbox(base, extra_params={
        "where": "SFHA_TF='T'",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
    }, page_size=500)
    features = []
    for f in raw:
        props = decode_flood(f["properties"])
        if props:
            f["properties"] = props
            features.append(f)
    save(features, path)


# ── CCC Environmental Justice ──────────────────────────────────────────────

def decode_ej(props):
    return {
        "municipality":   props.get("MUNICIPALITY"),
        "ej":             props.get("EJ"),
        "criteria":       props.get("EJ_CRIT_DESC"),
        "criteria_count": props.get("EJ_CRITERIA_COUNT"),
        "pct_minority":   props.get("PCT_MINORITY"),
        "mhhi":           props.get("BG_MHHI"),
        "geoid":          props.get("GEOID"),
    }


def fetch_ej_communities():
    path = STATIC / "ej_communities.geojson"
    if cached(path): return
    print("\nFetching CCC Environmental Justice communities (layer 6)...")
    base = (
        "https://gis-services.capecodcommission.org/arcgis/rest/services/"
        "EnvironmentalJustice/2020_EJ/MapServer/6/query"
    )
    raw = fetch_simple(base,
        out_fields="GEOID,MUNICIPALITY,EJ,EJ_CRIT_DESC,EJ_CRITERIA_COUNT,PCT_MINORITY,BG_MHHI")
    features = []
    for f in raw:
        f["properties"] = decode_ej(f["properties"])
        features.append(f)
    print(f"  {sum(1 for f in features if f['properties'].get('ej'))} EJ-designated block groups")
    save(features, path)


# ── CCC Sea Level Rise (1–6 ft) ────────────────────────────────────────────
# Source: Cape Cod Commission GIS — SeaLevelRise/MasterSLR MapServer
# Layer IDs: 1ft→2, 2ft→5, 3ft→8, 4ft→11, 5ft→14, 6ft→17

CCC_SLR_LAYERS = {1: 2, 2: 5, 3: 8, 4: 11, 5: 14, 6: 17}

def fetch_sea_level_rise():
    print("\nFetching CCC Sea Level Rise inundation layers (1–6 ft)...")
    for ft, layer_id in CCC_SLR_LAYERS.items():
        path = STATIC / f"slr_{ft}ft.geojson"
        if cached(path): continue
        print(f"  {ft}ft (layer {layer_id})...")
        base = (
            "https://gis-services.capecodcommission.org/arcgis/rest/services/"
            f"SeaLevelRise/MasterSLR/MapServer/{layer_id}/query"
        )
        raw = fetch_simple(base, out_fields="Island,TYPE")
        features = []
        for f in raw:
            f["properties"] = {"level_ft": ft}
            features.append(f)
        save(features, path)


# ── CCC Wastewater / 208 Plan ──────────────────────────────────────────────

CCC_208_BASE = (
    "https://gis-services.capecodcommission.org/arcgis/rest/services/"
    "Reference/208Layers/MapServer"
)


def fetch_sewer_areas():
    path = STATIC / "sewer_areas.geojson"
    if cached(path): return
    print("\nFetching CCC sewered areas (layer 8)...")
    base = f"{CCC_208_BASE}/8/query"
    raw = fetch_simple(base, out_fields="Town,UpdateDate")
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "town":        p.get("Town"),
            "update_date": p.get("UpdateDate"),
            "type":        "existing",
        }
        features.append(f)
    save(features, path)


def fetch_sewer_phasing():
    path = STATIC / "sewer_phasing.geojson"
    if cached(path): return
    print("\nFetching CCC sewer phasing / CWMP (layer 9)...")
    base = f"{CCC_208_BASE}/9/query"
    raw = fetch_simple(base, out_fields="Sewer_shed,Town,Phase_Date,Source")
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "sewershed":  p.get("Sewer_shed"),
            "town":       p.get("Town"),
            "phase_year": p.get("Phase_Date"),
            "type":       "planned",
        }
        features.append(f)
    save(features, path)


def fetch_title5():
    path = STATIC / "title5.geojson"
    if cached(path): return
    print("\nFetching CCC Title 5 systems (layer 2)...")
    base = f"{CCC_208_BASE}/2/query"
    raw = fetch_simple(base, out_fields="T5_Category")
    features = []
    for f in raw:
        f["properties"] = {"category": f["properties"].get("T5_Category")}
        features.append(f)
    save(features, path)


# ── MassGIS Protected Open Space ───────────────────────────────────────────

OWNER_TYPE_LABELS = {
    "F": "Federal", "S": "State", "C": "County",
    "M": "Municipal", "L": "Land Trust", "N": "Non-Profit",
    "P": "Private", "O": "Other", "X": "Unknown",
}

def fetch_open_space():
    path = STATIC / "open_space.geojson"
    if cached(path): return
    print("\nFetching MassGIS Protected Open Space (Cape Cod)...")
    base = (
        "https://gis.eea.mass.gov/server/rest/services/"
        "Protected_and_Recreational_OpenSpace_Polygons/FeatureServer/0/query"
    )
    raw = fetch_paged_bbox(base, extra_params={
        "outFields": "SITE_NAME,OWNER_TYPE,MANAGER,PRIM_PURP,PUB_ACCESS,GIS_ACRES",
        "inSR": "4326",
    })
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "name":       p.get("SITE_NAME"),
            "owner_type": p.get("OWNER_TYPE"),
            "owner_label": OWNER_TYPE_LABELS.get(p.get("OWNER_TYPE"), "Other"),
            "manager":    p.get("MANAGER"),
            "prim_purp":  p.get("PRIM_PURP"),
            "pub_access": p.get("PUB_ACCESS"),
            "acres":      round(p.get("GIS_ACRES") or 0, 1),
        }
        features.append(f)
    save(features, path)


# ── MassDEP Zone II Wellhead Protection ────────────────────────────────────

CAPE_TOWNS_UPPER = (
    "TOWN IN ('BARNSTABLE','BOURNE','BREWSTER','CHATHAM','DENNIS','EASTHAM',"
    "'FALMOUTH','HARWICH','MASHPEE','ORLEANS','PROVINCETOWN','SANDWICH',"
    "'TRURO','WELLFLEET','YARMOUTH')"
)

def fetch_zone2():
    path = STATIC / "zone2.geojson"
    if cached(path): return
    print("\nFetching MassDEP Zone II Wellhead Protection Areas...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/IWPA_Zone2/FeatureServer/0/query"
    )
    raw = fetch_simple(base, where=CAPE_TOWNS_UPPER,
                       out_fields="PWS_ID,SUPPLIER,TOWN,AREA_ACRES")
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "supplier": p.get("SUPPLIER"),
            "town":     p.get("TOWN"),
            "acres":    round(p.get("AREA_ACRES") or 0, 1),
            "pws_id":   p.get("PWS_ID"),
        }
        features.append(f)
    save(features, path)


# ── CCC Regional Zoning ─────────────────────────────────────────────────────

GEN_USE_LABELS = {
    0: "Special/Other", 1: "Residential", 2: "Business",
    3: "Industrial", 4: "Commercial/Prof",
}

def fetch_zoning():
    path = STATIC / "zoning.geojson"
    if cached(path): return
    print("\nFetching CCC regional zoning (layer 20)...")
    base = (
        "https://gis-services.capecodcommission.org/arcgis/rest/services/"
        "Reference/Boundaries/MapServer/20/query"
    )
    # resultRecordCount unsupported on this server — omit it (max_records=None)
    raw = fetch_simple(base, out_fields="ZONECODE,PRIM_USE,GEN_USE,TOWNCODE,ACRES,TOWN_ID",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        gen = p.get("GEN_USE")
        f["properties"] = {
            "zonecode":  p.get("ZONECODE"),
            "prim_use":  p.get("PRIM_USE"),
            "gen_use":   gen,
            "gen_label": GEN_USE_LABELS.get(gen, "Other"),
            "towncode":  p.get("TOWNCODE"),
            "acres":     round(p.get("ACRES") or 0, 1),
        }
        features.append(f)
    save(features, path)


# ── CCC Cranberry Bogs + Groundwater Depth ─────────────────────────────────

def fetch_cranberry_bogs():
    path = STATIC / "cranberry_bogs.geojson"
    if cached(path): return
    print("\nFetching CCC cranberry bogs (208Layers layer 6)...")
    base = f"{CCC_208_BASE}/6/query"
    raw = fetch_simple(base, out_fields="BOG_NAME,OWNER,TOWN,AREASACRES,Basin",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "name":   p.get("BOG_NAME"),
            "owner":  p.get("OWNER"),
            "town":   p.get("TOWN"),
            "acres":  round(p.get("AREASACRES") or 0, 1),
            "basin":  p.get("Basin"),
        }
        features.append(f)
    save(features, path)


def fetch_groundwater_tot():
    path = STATIC / "groundwater_tot.geojson"
    if cached(path): return
    print("\nFetching USGS Groundwater Time of Travel (208Layers layer 4)...")
    base = f"{CCC_208_BASE}/4/query"
    raw = fetch_simple(base, out_fields="TOT", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"tot_years": f["properties"].get("TOT")}
        features.append(f)
    save(features, path)


def fetch_groundwater_depth():
    path = STATIC / "groundwater_depth.geojson"
    if cached(path): return
    print("\nFetching CCC groundwater depth < 20ft (208Layers layer 7)...")
    base = f"{CCC_208_BASE}/7/query"
    raw = fetch_simple(base, out_fields="OBJECTID", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"depth": "lt20ft"}
        features.append(f)
    save(features, path)


# ── CCC Areas of Critical Environmental Concern ────────────────────────────

CCC_BOUNDARIES_BASE = (
    "https://gis-services.capecodcommission.org/arcgis/rest/services/"
    "Reference/Boundaries/MapServer"
)

def fetch_farms():
    path = STATIC / "farms.geojson"
    if cached(path): return
    print("\nFetching CCC Cape Cod Farms (Boundaries layer 7)...")
    base = f"{CCC_BOUNDARIES_BASE}/7/query"
    raw = fetch_simple(base, out_fields="GIS_Acres", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"acres": round(f["properties"].get("GIS_Acres") or 0, 1)}
        features.append(f)
    save(features, path)


def fetch_vernal_pools():
    path = STATIC / "vernal_pools.geojson"
    if cached(path): return
    print("\nFetching MassGIS NHESP Certified Vernal Pools (Cape Cod)...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/NHESP_Certified_Vernal_Pools/MapServer/0/query"
    )
    raw = fetch_paged_bbox(base, extra_params={"outFields": "CVP_NUM,CRITERIA,CERTIFIED", "inSR": "4326"})
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "cvp_num":   p.get("CVP_NUM"),
            "criteria":  p.get("CRITERIA"),
            "certified": p.get("CERTIFIED"),
        }
        features.append(f)
    save(features, path)


def fetch_nhesp():
    path = STATIC / "nhesp.geojson"
    if cached(path): return
    print("\nFetching MassGIS NHESP Priority Habitats (Cape Cod)...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/NHESP_Priority_Habitats/MapServer/0/query"
    )
    raw = fetch_paged_bbox(base, extra_params={"outFields": "PRIHAB_ID,VERSION", "inSR": "4326"})
    features = []
    for f in raw:
        f["properties"] = {"prihab_id": f["properties"].get("PRIHAB_ID")}
        features.append(f)
    save(features, path)


def fetch_biomap():
    path = STATIC / "biomap.geojson"
    if cached(path): return
    print("\nFetching MassGIS BioMap2 Core Habitat (Cape Cod)...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/BioMap2_Core_CNL/MapServer/1/query"
    )
    raw = fetch_paged_bbox(base, extra_params={"outFields": "CH_ID,ACRES", "inSR": "4326"})
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "ch_id": p.get("CH_ID"),
            "acres": round(p.get("ACRES") or 0, 1),
        }
        features.append(f)
    save(features, path)


def fetch_acec():
    path = STATIC / "acec.geojson"
    if cached(path): return
    print("\nFetching CCC Areas of Critical Environmental Concern (layer 12)...")
    base = f"{CCC_BOUNDARIES_BASE}/12/query"
    raw = fetch_simple(base, out_fields="NAME,DES_DATE,ADMIN_BY,REGION,POLY_ACRES",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "name":     p.get("NAME"),
            "des_date": p.get("DES_DATE"),
            "admin_by": p.get("ADMIN_BY"),
            "region":   p.get("REGION"),
            "acres":    round(p.get("POLY_ACRES") or 0, 1),
        }
        features.append(f)
    save(features, path)


# ── CCC Ponds Trophic Status + DEP Wetlands ────────────────────────────────

CCC_WATER_BASE = (
    "https://gis-services.capecodcommission.org/arcgis/rest/services/"
    "Reference/Water/MapServer"
)

TROPHIC_LABELS = {
    "O": "Oligotrophic", "M": "Mesotrophic", "E": "Eutrophic",
}

def fetch_ponds():
    path = STATIC / "ponds.geojson"
    if cached(path): return
    print("\nFetching CCC pond trophic status (208Layers layer 3)...")
    base = f"{CCC_208_BASE}/3/query"
    raw = fetch_simple(base,
                       where="TROPHIC_ST IN ('O','M','E')",
                       out_fields="NAME,TROPHIC_ST,ACREAGE,EmbaymentI",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        ts = p.get("TROPHIC_ST") or ""
        f["properties"] = {
            "name":           p.get("NAME"),
            "trophic_code":   ts,
            "trophic_status": TROPHIC_LABELS.get(ts, ts),
            "acres":          round(p.get("ACREAGE") or 0, 1),
        }
        features.append(f)
    save(features, path)


def fetch_wetlands():
    path = STATIC / "wetlands.geojson"
    if cached(path): return
    print("\nFetching CCC/DEP wetlands (Water layer 27)...")
    base = f"{CCC_WATER_BASE}/27/query"
    raw = fetch_simple(base, out_fields="IT_VALDESC,AREAACRES", max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "type":  p.get("IT_VALDESC"),
            "acres": round(p.get("AREAACRES") or 0, 2),
        }
        features.append(f)
    save(features, path)


# ── CCC Eel Grass + National Seashore Boundary ─────────────────────────────

def fetch_eelgrass():
    path = STATIC / "eelgrass.geojson"
    if cached(path): return
    print("\nFetching CCC eel grass beds (Water layer 11)...")
    base = f"{CCC_WATER_BASE}/11/query"
    raw = fetch_simple(base, out_fields="Area_acres", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"acres": round(f["properties"].get("Area_acres") or 0, 2)}
        features.append(f)
    save(features, path)


def fetch_national_seashore():
    path = STATIC / "national_seashore.geojson"
    if cached(path): return
    print("\nFetching Cape Cod National Seashore boundary (Boundaries layer 6)...")
    base = f"{CCC_BOUNDARIES_BASE}/6/query"
    raw = fetch_simple(base, out_fields="ACREAGE", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"acres": round(f["properties"].get("ACREAGE") or 0, 1)}
        features.append(f)
    save(features, path)


# ── CCC Storm Surge (SLOSH) ────────────────────────────────────────────────

def fetch_storm_surge():
    path = STATIC / "storm_surge.geojson"
    if cached(path): return
    print("\nFetching CCC SLOSH storm surge zones (Inundation layer 1)...")
    base = (
        "https://gis-services.capecodcommission.org/arcgis/rest/services/"
        "Reference/Inundation/MapServer/1/query"
    )
    features = []
    for cat in [1, 2, 3, 4]:
        print(f"  Hurricane Category {cat}...")
        params = {
            "where":              f"HURR_CAT={cat}",
            "outFields":          "HURR_CAT",
            "f":                  "json",
            "returnGeometry":     "true",
            "maxAllowableOffset": "0.0005",
            "outSR":              "4326",
        }
        url = base + "?" + urllib.parse.urlencode(params)
        data = fetch_json(url, timeout=120)
        for item in data.get("features", []):
            rings = (item.get("geometry") or {}).get("rings", [])
            if not rings:
                continue
            geom = ({"type": "Polygon",      "coordinates": rings}      if len(rings) == 1 else
                    {"type": "MultiPolygon", "coordinates": [[r] for r in rings]})
            features.append({
                "type": "Feature",
                "geometry":   geom,
                "properties": {"hurr_cat": cat},
            })
        time.sleep(0.3)
    save(features, path)



# ── MassDEP Chapter 21E Contaminated Sites ──────────────────────────────────

def fetch_contaminated_sites():
    path = STATIC / "contaminated_sites.geojson"
    if cached(path): return
    print("\nFetching MassDEP Chapter 21E contaminated sites (Cape Cod)...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/C21e/MapServer/0/query"
    )
    raw = fetch_simple(base, where=CAPE_TOWNS_UPPER,
                       out_fields="RTN,NAME,ADDRESS,TOWN,STATUS")
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "rtn":     p.get("RTN"),
            "name":    p.get("NAME"),
            "address": p.get("ADDRESS"),
            "town":    p.get("TOWN"),
            "status":  p.get("STATUS"),
        }
        features.append(f)
    save(features, path)


# ── CCC Embayment Nitrogen Monitoring ──────────────────────────────────────

def fetch_nitrate_wells():
    path = STATIC / "nitrate_wells.geojson"
    if cached(path): return
    print("\nFetching CCC nitrate concentration in public supply wells (208Layers layer 1)...")
    base = f"{CCC_208_BASE}/1/query"
    raw = fetch_simple(base, where="YR_AVG IS NOT NULL AND YR_AVG > 0",
                       out_fields="SITE_NAME,PWS_NAME,TOWN,YR_AVG,YEAR")
    features = []
    for f in raw:
        p = f["properties"]
        yr_avg = p.get("YR_AVG")
        try:
            yr_avg = round(float(yr_avg), 2) if yr_avg is not None else None
        except (ValueError, TypeError):
            yr_avg = None
        if yr_avg is None or yr_avg <= 0:
            continue
        f["properties"] = {
            "site_name": p.get("SITE_NAME"),
            "pws_name":  p.get("PWS_NAME"),
            "town":      p.get("TOWN"),
            "nitrate":   yr_avg,
            "year":      p.get("YEAR"),
        }
        features.append(f)
    save(features, path)


def fetch_nitrogen_monitoring():
    path = STATIC / "nitrogen_monitoring.geojson"
    if cached(path): return
    print("\nFetching CCC embayment nitrogen monitoring stations (208Layers layer 0)...")
    base = f"{CCC_208_BASE}/0/query"
    raw = fetch_simple(base, out_fields="Station,Quality,Embayment,EmbaymentID")
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "station":   p.get("Station"),
            "quality":   p.get("Quality"),
            "embayment": p.get("Embayment"),
        }
        features.append(f)
    save(features, path)


# ── CCC DCPCs ───────────────────────────────────────────────────────────────

def fetch_fertilizer_dcpc():
    path = STATIC / "fertilizer_dcpc.geojson"
    if cached(path): return
    print("\nFetching CCC Fertilizer Management DCPC (Boundaries layer 17)...")
    base = f"{CCC_BOUNDARIES_BASE}/17/query"
    raw = fetch_simple(base, out_fields="OBJECTID", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"type": "fertilizer_dcpc"}
        features.append(f)
    save(features, path)



def fetch_historic_districts():
    path = STATIC / "historic_districts.geojson"
    if cached(path): return
    print("\nFetching MHC Historic Inventory areas (Boundaries layer 2)...")
    base = f"{CCC_BOUNDARIES_BASE}/2/query"
    raw = fetch_simple(base,
                       out_fields="HISTORIC_N,COMMON_NAM,DESIGNATIO,TOWN_NAME,CONSTRUCTI,ARCH,USE_TYPE",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "name":        p.get("HISTORIC_N") or p.get("COMMON_NAM"),
            "designation": p.get("DESIGNATIO"),
            "town":        p.get("TOWN_NAME"),
            "built":       p.get("CONSTRUCTI"),
            "arch_style":  p.get("ARCH"),
            "use_type":    p.get("USE_TYPE"),
        }
        features.append(f)
    save(features, path)


def fetch_growth_zones():
    path = STATIC / "growth_zones.geojson"
    if cached(path): return
    print("\nFetching CCC Growth Incentive Zones (Boundaries layer 14)...")
    base = f"{CCC_BOUNDARIES_BASE}/14/query"
    raw = fetch_simple(base, out_fields="OBJECTID", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"type": "growth_incentive_zone"}
        features.append(f)
    save(features, path)


def fetch_all_dcpc():
    path = STATIC / "all_dcpc.geojson"
    if cached(path): return
    print("\nFetching CCC All DCPCs (Boundaries layer 16)...")
    base = f"{CCC_BOUNDARIES_BASE}/16/query"
    raw = fetch_simple(base, out_fields="DCPC_Name,TOWN_ID,ACREAGE", max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "dcpc_name": p.get("DCPC_Name"),
            "town_id":   p.get("TOWN_ID"),
            "acres":     round(p.get("ACREAGE") or 0, 1),
        }
        features.append(f)
    save(features, path)


def fetch_landfills():
    path = STATIC / "landfills.geojson"
    if cached(path): return
    print("\nFetching MassDEP landfill polygons (Cape Cod)...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/DEP_SW_Disposal_Land/MapServer/2/query"
    )
    raw = fetch_simple(base, where=CAPE_TOWNS_UPPER,
                       out_fields="SITE_NAME,ADDRESS,TOWN,STATUS,WASTE_TYPE,ACRES")
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "name":       p.get("SITE_NAME"),
            "address":    p.get("ADDRESS"),
            "town":       p.get("TOWN"),
            "status":     p.get("STATUS"),
            "waste_type": p.get("WASTE_TYPE"),
            "acres":      round(p.get("ACRES") or 0, 1),
        }
        features.append(f)
    save(features, path)


# ── CCC Freshwater Recharge Areas ──────────────────────────────────────────

def fetch_freshwater_recharge():
    path = STATIC / "freshwater_recharge.geojson"
    if cached(path): return
    print("\nFetching CCC Freshwater Recharge Areas (Water layer 7)...")
    base = f"{CCC_WATER_BASE}/7/query"
    raw = fetch_simple(base,
                       out_fields="SUBWATER_N,SUBWATER_D,EMBAY_NAME,Acreage",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "subwatershed": p.get("SUBWATER_N"),
            "description":  p.get("SUBWATER_D"),
            "embayment":    p.get("EMBAY_NAME"),
            "acres":        round(p.get("Acreage") or 0, 1),
        }
        features.append(f)
    save(features, path)

# ── Freshwater Lenses ──────────────────────────────────────────────────────

def fetch_freshwater_lenses():
    path = STATIC / "freshwater_lenses.geojson"
    if cached(path): return
    print("\nFetching CCC freshwater lenses (Water layer 26)...")
    base = f"{CCC_WATER_BASE}/26/query"
    raw = fetch_simple(base, out_fields="LENS", max_records=None)
    features = []
    for f in raw:
        f["properties"] = {"name": f["properties"].get("LENS")}
        features.append(f)
    save(features, path)


# ── NRCS Tidal Wetland Restoration ─────────────────────────────────────────

def fetch_tidal_wetlands():
    path = STATIC / "tidal_wetlands.geojson"
    if cached(path): return
    print("\nFetching CCC NRCS tidal wetland restoration sites (Water layer 17)...")
    base = f"{CCC_WATER_BASE}/17/query"
    raw = fetch_simple(base,
                       out_fields="Name,STATUS,Town_City,Project_Ty,TA_ID,WRBP_Site",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        name = (p.get("Name") or "").strip()
        if not name:
            continue
        f["properties"] = {
            "name":        name,
            "status":      (p.get("STATUS") or "").strip() or None,
            "town":        (p.get("Town_City") or "").strip() or None,
            "project_type": (p.get("Project_Ty") or "").strip() or None,
            "ta_id":       p.get("TA_ID"),
            "wrbp_site":   (p.get("WRBP_Site") or "").strip() or None,
        }
        features.append(f)
    save(features, path)


# ── Ocean Management Plan Zones ─────────────────────────────────────────────

CCC_OMP_BASE = (
    "https://gis-services.capecodcommission.org/arcgis/rest/services/"
    "Reference/OMPLayers/MapServer"
)

OMP_ZONE_TYPES = {2: "prohibited", 3: "exclusionary", 4: "provisional"}

def fetch_omp_zones():
    path = STATIC / "omp_zones.geojson"
    if cached(path): return
    print("\nFetching CCC Ocean Management Plan zones (layers 2-4)...")
    features = []
    for layer_id, zone_type in OMP_ZONE_TYPES.items():
        print(f"  {zone_type} (layer {layer_id})...")
        base = f"{CCC_OMP_BASE}/{layer_id}/query"
        raw = fetch_simple(base, out_fields="OBJECTID", max_records=None)
        for f in raw:
            f["properties"] = {"zone_type": zone_type}
            features.append(f)
    save(features, path)


# ── CCC Restoration Projects ────────────────────────────────────────────────

CCC_LAND_BASE = (
    "https://gis-services.capecodcommission.org/arcgis/rest/services/"
    "Reference/Land/MapServer"
)

def fetch_restoration_projects():
    path = STATIC / "restoration_projects.geojson"
    if cached(path): return
    print("\nFetching CCC restoration projects (Land layer 6)...")
    base = f"{CCC_LAND_BASE}/6/query"
    raw = fetch_simple(base,
                       out_fields="Name,Town,STATUS,Project_Ty,EVRank,TRank,Combo_EV",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "name":         p.get("Name"),
            "town":         p.get("Town"),
            "status":       p.get("STATUS"),
            "project_type": p.get("Project_Ty"),
            "ev_rank":      p.get("EVRank"),
            "t_rank":       p.get("TRank"),
            "combo_ev":     p.get("Combo_EV"),
        }
        features.append(f)
    save(features, path)


# ── CCC Development of Regional Impact Decisions ────────────────────────────

def fetch_dri_decisions():
    path = STATIC / "dri_decisions.geojson"
    if cached(path): return
    print("\nFetching CCC DRI decisions (Boundaries layer 13)...")
    base = f"{CCC_BOUNDARIES_BASE}/13/query"
    raw = fetch_simple(base,
                       out_fields="Name,TYPE_OF_DECISION,APPROVED_DENIED,TOWN,"
                                  "DESCRIPTION,DECISION_DATE,Decade,URL",
                       max_records=None)
    features = []
    for f in raw:
        p = f["properties"]
        date_ms = p.get("DECISION_DATE")
        f["properties"] = {
            "name":        p.get("Name"),
            "decision_type": p.get("TYPE_OF_DECISION"),
            "outcome":     p.get("APPROVED_DENIED"),
            "town":        p.get("TOWN"),
            "description": p.get("DESCRIPTION"),
            "decision_date": date_ms,
            "decade":      p.get("Decade"),
            "url":         p.get("URL"),
        }
        features.append(f)
    save(features, path)


# ── Marine Beaches ──────────────────────────────────────────────────────────

def fetch_marine_beaches():
    path = STATIC / "marine_beaches.geojson"
    if cached(path): return
    print("\nFetching MassGIS marine beaches (Cape Cod)...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/Marine_Beaches/FeatureServer/2/query"
    )
    raw = fetch_paged_bbox(base, extra_params={
        "outFields": "BEACHNAME,TYPE,TOWNNAME,LENINMILES,EPA_ID",
        "inSR": "4326",
    })
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "name":      p.get("BEACHNAME"),
            "type":      p.get("TYPE"),
            "town":      p.get("TOWNNAME"),
            "miles":     round(p.get("LENINMILES") or 0, 3),
            "epa_id":    p.get("EPA_ID"),
        }
        features.append(f)
    save(features, path)


# ── Surface Water Quality Standards ────────────────────────────────────────

def fetch_swqs():
    path = STATIC / "swqs.geojson"
    if cached(path): return
    print("\nFetching MassDEP Surface Water Quality Standards (Cape Cod)...")
    base = (
        "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
        "AGOL/SWQS2013/FeatureServer/1/query"
    )
    raw = fetch_paged_bbox(base, extra_params={
        "outFields": "WATERS,CLASS,QUALIFIER,REG_BASIN",
        "inSR": "4326",
    })
    features = []
    for f in raw:
        p = f["properties"]
        f["properties"] = {
            "waters":    p.get("WATERS"),
            "class":     p.get("CLASS"),
            "qualifier": p.get("QUALIFIER"),
            "basin":     p.get("REG_BASIN"),
        }
        features.append(f)
    save(features, path)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    fetch_flood_zones()
    fetch_ej_communities()
    fetch_sea_level_rise()
    fetch_sewer_areas()
    fetch_sewer_phasing()
    fetch_title5()
    fetch_open_space()
    fetch_zone2()
    fetch_zoning()
    fetch_cranberry_bogs()
    fetch_groundwater_depth()
    fetch_acec()
    fetch_ponds()
    fetch_wetlands()
    fetch_eelgrass()
    fetch_national_seashore()
    fetch_fertilizer_dcpc()
    fetch_historic_districts()
    fetch_growth_zones()
    fetch_all_dcpc()
    fetch_storm_surge()
    fetch_contaminated_sites()
    fetch_nitrate_wells()
    fetch_nitrogen_monitoring()
    fetch_farms()
    fetch_vernal_pools()
    fetch_nhesp()
    fetch_biomap()
    fetch_groundwater_tot()
    fetch_landfills()
    fetch_freshwater_recharge()
    fetch_tribal_lands()
    fetch_freshwater_lenses()
    fetch_tidal_wetlands()
    fetch_omp_zones()
    fetch_restoration_projects()
    fetch_dri_decisions()
    fetch_marine_beaches()
    fetch_swqs()
    print("\nDone.")


if __name__ == "__main__":
    main()
