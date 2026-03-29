#!/usr/bin/env python3
"""
Fetch Cape Cod (Barnstable County) socioeconomic data from US Census ACS 5-Year
and TIGER/Web geometry. Saves: tracts.geojson, towns.geojson
"""
import json
import urllib.request
import urllib.parse
from pathlib import Path

STATIC = Path(__file__).parent / "static"
STATIC.mkdir(parents=True, exist_ok=True)

STATE  = "25"   # Massachusetts
COUNTY = "001"  # Barnstable County

# ── ACS batch 1: main socioeconomic variables ─────────────────────────────
# ~44 vars, well within the 50-var API limit
ACS_VARS = {
    # Income / cost of living
    "B19013_001E": "med_income",        # Median household income
    "B19083_001E": "gini",              # Gini coefficient (float 0-1)
    "B25077_001E": "med_home_value",    # Median home value
    "B25064_001E": "median_rent",       # Median gross rent
    # Poverty / assistance
    "B17001_002E": "poverty_pop",
    "B17001_001E": "poverty_universe",
    "B22010_002E": "snap_hh",           # Households receiving SNAP
    "B22010_001E": "snap_universe",
    # Employment
    "B23025_005E": "unemployed",
    "B23025_003E": "labor_force",
    "B08301_021E": "work_from_home",    # Workers who WFH
    "B08301_001E": "wfh_universe",
    "B08303_011E": "commute_45_59",     # Commute 45-59 min
    "B08303_012E": "commute_60_89",
    "B08303_013E": "commute_90plus",
    "B08303_001E": "commute_universe",
    # Education
    "B15003_022E": "edu_bachelors",
    "B15003_023E": "edu_masters",
    "B15003_024E": "edu_professional",
    "B15003_025E": "edu_doctorate",
    "B15003_001E": "edu_universe",
    # Demographics
    "B01002_001E": "median_age",
    "B01003_001E": "total_pop",
    "B05002_013E": "foreign_born",
    "B05002_001E": "nativity_universe",
    # Age 65+ (male cells)
    "B01001_020E": "age65_m1",          # M 65-66
    "B01001_021E": "age65_m2",          # M 67-69
    "B01001_022E": "age65_m3",          # M 70-74
    "B01001_023E": "age65_m4",          # M 75-79
    "B01001_024E": "age65_m5",          # M 80-84
    "B01001_025E": "age65_m6",          # M 85+
    # Age 65+ (female cells)
    "B01001_044E": "age65_f1",          # F 65-66
    "B01001_045E": "age65_f2",          # F 67-69
    "B01001_046E": "age65_f3",          # F 70-74
    "B01001_047E": "age65_f4",          # F 75-79
    "B01001_048E": "age65_f5",          # F 80-84
    "B01001_049E": "age65_f6",          # F 85+
    # Housing
    "B25003_002E": "owner_occupied",
    "B25003_001E": "tenure_total",
    "B25004_006E": "seasonal_vacant",   # Vacant for seasonal/recreational use
    "B25001_001E": "total_housing",     # All housing units (occupied + vacant)
    # Internet
    "B28002_004E": "broadband_hh",      # Broadband of any type
    "B28002_001E": "broadband_universe",
    # Name
    "NAME": "name",
}

# ── ACS batch 2: health insurance (B27001) ────────────────────────────────
# Uninsured cells: every 3rd starting at _005 (male) and _033 (female)
ACS_VARS_INSURANCE = {
    "B27001_001E": "insured_universe",
    # Male uninsured by age
    "B27001_005E": "unins_m0",
    "B27001_008E": "unins_m1",
    "B27001_011E": "unins_m2",
    "B27001_014E": "unins_m3",
    "B27001_017E": "unins_m4",
    "B27001_020E": "unins_m5",
    "B27001_023E": "unins_m6",
    "B27001_026E": "unins_m7",
    "B27001_029E": "unins_m8",
    # Female uninsured by age
    "B27001_033E": "unins_f0",
    "B27001_036E": "unins_f1",
    "B27001_039E": "unins_f2",
    "B27001_042E": "unins_f3",
    "B27001_045E": "unins_f4",
    "B27001_048E": "unins_f5",
    "B27001_051E": "unins_f6",
    "B27001_054E": "unins_f7",
    "B27001_057E": "unins_f8",
}

FLOAT_VARS = {"B19083_001E"}  # variables that should be stored as float not int


def _parse_val(api_key, val):
    """Convert Census API string value to int/float/None."""
    if val is None:
        return None
    try:
        if api_key in FLOAT_VARS:
            v = float(val)
        else:
            v = int(val)
        return None if v < 0 else v
    except (ValueError, TypeError):
        return None


def _fetch_batch(var_dict, geo_level):
    """Fetch one set of ACS variables, return dict keyed by GEOID."""
    var_keys = [k for k in var_dict if k != "NAME"]
    has_name = "NAME" in var_dict
    get_str = ("NAME," if has_name else "") + ",".join(var_keys)

    if geo_level == "tract":
        for_param = "tract:*"
        in_param  = f"state:{STATE} county:{COUNTY}"
    else:
        for_param = "county subdivision:*"
        in_param  = f"state:{STATE} county:{COUNTY}"

    url = (
        "https://api.census.gov/data/2023/acs/acs5?"
        + urllib.parse.urlencode({"get": get_str, "for": for_param, "in": in_param})
    )
    print(f"  GET {url[:110]}...")
    with urllib.request.urlopen(url, timeout=30) as r:
        rows = json.loads(r.read())

    headers = rows[0]
    result = {}
    for row in rows[1:]:
        record = dict(zip(headers, row))
        if geo_level == "tract":
            geoid = f"{record['state']}{record['county']}{record['tract']}"
        else:
            geoid = f"{record['state']}{record['county']}{record['county subdivision']}"

        props = {}
        for api_key, our_key in var_dict.items():
            val = record.get(api_key)
            if api_key == "NAME":
                props[our_key] = val
            else:
                props[our_key] = _parse_val(api_key, val)
        result[geoid] = props

    return result


def fetch_acs(geo_level="tract"):
    """Fetch all ACS variables (two batches) and merge by GEOID."""
    print(f"\n  Batch 1 ({len(ACS_VARS)} vars)...")
    data = _fetch_batch(ACS_VARS, geo_level)
    print(f"  Batch 2 ({len(ACS_VARS_INSURANCE)} vars, health insurance)...")
    ins  = _fetch_batch(ACS_VARS_INSURANCE, geo_level)
    for geoid, props in ins.items():
        if geoid in data:
            data[geoid].update(props)
    print(f"  Got {len(data)} {geo_level} records total")
    return data


def compute_rates(props):
    """Compute derived percentages and clean up raw intermediate fields."""
    def pct(num, den):
        if num is None or den is None or den == 0:
            return None
        return round(100.0 * num / den, 1)

    def sumkeys(*keys):
        return sum(props.get(k) or 0 for k in keys)

    # Existing
    props["poverty_rate"]       = pct(props.get("poverty_pop"), props.get("poverty_universe"))
    props["unemployment_rate"]  = pct(props.get("unemployed"), props.get("labor_force"))
    props["pct_bachelors_plus"] = pct(
        sumkeys("edu_bachelors","edu_masters","edu_professional","edu_doctorate"),
        props.get("edu_universe"))
    props["pct_owner_occupied"] = pct(props.get("owner_occupied"), props.get("tenure_total"))

    # New
    props["pct_65plus"] = pct(
        sumkeys("age65_m1","age65_m2","age65_m3","age65_m4","age65_m5","age65_m6",
                "age65_f1","age65_f2","age65_f3","age65_f4","age65_f5","age65_f6"),
        props.get("total_pop"))

    props["pct_seasonal"] = pct(props.get("seasonal_vacant"), props.get("total_housing"))

    props["pct_broadband"] = pct(props.get("broadband_hh"), props.get("broadband_universe"))

    props["pct_snap"] = pct(props.get("snap_hh"), props.get("snap_universe"))

    props["pct_wfh"] = pct(props.get("work_from_home"), props.get("wfh_universe"))

    props["pct_foreign_born"] = pct(props.get("foreign_born"), props.get("nativity_universe"))

    props["pct_long_commute"] = pct(
        sumkeys("commute_45_59","commute_60_89","commute_90plus"),
        props.get("commute_universe"))

    uninsured = sumkeys(*[f"unins_m{i}" for i in range(9)],
                         *[f"unins_f{i}" for i in range(9)])
    props["pct_uninsured"] = pct(uninsured, props.get("insured_universe"))

    # Remove raw intermediate fields to keep GeoJSON lean
    for k in list(props.keys()):
        if k.startswith(("age65_", "unins_", "edu_", "commute_4", "commute_6", "commute_9")):
            del props[k]
    for k in ["poverty_pop","poverty_universe","unemployed","labor_force",
              "owner_occupied","tenure_total","seasonal_vacant","total_housing",
              "broadband_hh","broadband_universe","snap_hh","snap_universe",
              "work_from_home","wfh_universe","foreign_born","nativity_universe",
              "commute_universe","insured_universe"]:
        props.pop(k, None)

    return props


def fetch_tiger_tracts():
    url = (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        f"tigerWMS_ACS2022/MapServer/6/query"
        f"?where=STATE%3D'{STATE}'+AND+COUNTY%3D'{COUNTY}'"
        "&outFields=GEOID,BASENAME&returnGeometry=true&f=geojson&outSR=4326"
        "&resultRecordCount=200"
    )
    print("  Fetching TIGER tracts...")
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.loads(r.read())
    features = data.get("features", [])
    print(f"  Got {len(features)} tract geometries")
    return features


def fetch_tiger_cousubs():
    url = (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        f"tigerWMS_ACS2022/MapServer/18/query"
        f"?where=STATE%3D'{STATE}'+AND+COUNTY%3D'{COUNTY}'"
        "&outFields=GEOID,BASENAME,NAME&returnGeometry=true&f=geojson&outSR=4326"
        "&resultRecordCount=50"
    )
    print("  Fetching TIGER county subdivisions...")
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.loads(r.read())
    features = data.get("features", [])
    print(f"  Got {len(features)} town geometries")
    return features


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
    geojson = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(geojson))
    print(f"Saved {path.name} ({path.stat().st_size // 1024} KB, {len(features)} features)")


def main():
    print("\n── Tract data ──────────────────────────────────")
    tract_data = fetch_acs("tract")
    for geoid in tract_data:
        compute_rates(tract_data[geoid])

    print("\n── Town data ───────────────────────────────────")
    town_data = fetch_acs("county subdivision")
    for geoid in town_data:
        compute_rates(town_data[geoid])

    print("\n── TIGER geometry ──────────────────────────────")
    tract_features  = fetch_tiger_tracts()
    town_features   = fetch_tiger_cousubs()

    # Join tracts
    tract_out, unmatched = [], 0
    for f in tract_features:
        geoid = f["properties"].get("GEOID", "")
        acs   = tract_data.get(geoid)
        if acs:
            f["properties"] = {"geoid": geoid, **acs}
            tract_out.append(f)
        else:
            unmatched += 1
    if unmatched:
        print(f"  Warning: {unmatched} tract geometries had no ACS match")

    # Join towns
    town_out = []
    for f in town_features:
        geoid    = f["properties"].get("GEOID", "")
        basename = f["properties"].get("BASENAME") or f["properties"].get("NAME") or "Unknown"
        acs      = town_data.get(geoid)
        f["properties"] = {"geoid": geoid, "basename": basename, **(acs or {})}
        town_out.append(f)

    print("\n── Saving ──────────────────────────────────────")
    save(tract_out, STATIC / "tracts.geojson")
    save(town_out,  STATIC / "towns.geojson")
    print("\nDone.")


if __name__ == "__main__":
    main()
