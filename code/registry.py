"""Build data/reference/districts.csv from the Texas Education Agency (TEA) district file.

Source: TEA's public ArcGIS layer of districts (one point per district, with
TEA's 6-digit id, name and website). The first three digits of the id are
TEA's county number: the county's position in plain alphabetical order of its
uppercase name without spaces. That differs from Census FIPS order only for
the Mc counties (TEA puts MCCULLOCH after MAVERICK; the Census files it as
"Mac"), so county names come from TEA's ordering and FIPS from the Census row.
A fourth digit of 8 marks an open-enrollment charter, which is out of scope.
"""
import csv
import io
import re
from urllib.parse import urlencode

from config import REFERENCE
from fetch import Fetcher

TEA_LAYER = ("https://services2.arcgis.com/5MVN2jsqIrNZD4tP/arcgis/rest/services/"
             "districts_schools_20261005_D/FeatureServer/0/query")
CENSUS_COUNTIES = "https://www2.census.gov/geo/docs/reference/codes2020/cou/st48_tx_cou2020.txt"
REGULAR_SUFFIX = re.compile(r"\b(ISD|CISD|CSD|MSD)$")
FIELDS = ["tea_id", "school_district", "county_name", "county_fips", "website", "tea_source",
          "nces_id", "district_lat", "district_lon", "esc_region"]
ARCGIS = "https://services2.arcgis.com/5MVN2jsqIrNZD4tP/arcgis/rest/services/"
NCES_LAYER = ARCGIS + "Current_Districts/FeatureServer/0/query"
ESC_LAYER = ARCGIS + "Districts_ESC_Join_shp/FeatureServer/0/query"


def layer_rows(fetcher, url, fields, order):
    rows, offset = [], 0
    while True:
        params = urlencode(dict(where="1=1", outFields=fields, returnGeometry="false",
                                orderByFields=order, resultOffset=offset,
                                resultRecordCount=1000, f="json"))
        page = fetcher.get(url + "?" + params, refresh=True).json()
        if page.get("error"):
            raise RuntimeError(f"TEA layer error at {url}: {page['error']}")
        features = page.get("features", [])
        rows.extend(f["attributes"] for f in features)
        if not page.get("exceededTransferLimit") or not features:
            return rows
        offset += len(features)


def tea_districts(fetcher):
    return layer_rows(fetcher, TEA_LAYER, "id,district_name,name,website,latitude,longitude", "id")


def tea_counties(fetcher) -> dict[int, tuple[str, str]]:
    """Map TEA county number -> (county name, Census county FIPS)."""
    text = fetcher.get(CENSUS_COUNTIES).text
    rows = list(csv.DictReader(io.StringIO(text), delimiter="|"))
    ordered = sorted(rows, key=lambda r: r["COUNTYNAME"].upper().replace(" ", "").replace(".", ""))
    return {number: (row["COUNTYNAME"], row["COUNTYFP"]) for number, row in enumerate(ordered, 1)}


def build():
    fetcher = Fetcher()
    counties = tea_counties(fetcher)
    nces = {str(r["DISTRICT_C"]).zfill(6): r.get("NCES_DISTR")
            for r in layer_rows(fetcher, NCES_LAYER, "DISTRICT_C,NCES_DISTR", "DISTRICT_C")}
    regions = {str(r["DISTRICT_C"]).zfill(6): r.get("REGION")
               for r in layer_rows(fetcher, ESC_LAYER, "DISTRICT_C,REGION", "DISTRICT_C")}
    districts, skipped = [], []
    for row in tea_districts(fetcher):
        tea_id = str(row["id"]).zfill(6)
        name = (row.get("district_name") or row.get("name") or "").strip()
        if tea_id[3] == "8" or not REGULAR_SUFFIX.search(name):
            skipped.append((tea_id, name))
            continue
        county, fips = counties[int(tea_id[:3])]
        website = (row.get("website") or "").strip()
        if website and not website.startswith("http"):
            website = "https://" + website
        districts.append(dict(tea_id=tea_id, school_district=name, county_name=county,
                              county_fips="48" + fips, website=website, tea_source=TEA_LAYER,
                              nces_id=nces.get(tea_id), district_lat=row.get("latitude"),
                              district_lon=row.get("longitude"), esc_region=regions.get(tea_id)))
    if len(districts) != 1018:
        raise ValueError(f"Expected 1,018 regular districts; got {len(districts)}")
    REFERENCE.mkdir(parents=True, exist_ok=True)
    with open(REFERENCE / "districts.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(districts, key=lambda d: d["tea_id"]))
    return districts, skipped


if __name__ == "__main__":
    districts, skipped = build()
    print(f"{len(districts)} regular districts written; {len(skipped)} skipped")
    for tea_id, name in skipped:
        if tea_id[3] != "8":
            print("skipped non-charter:", tea_id, name)
