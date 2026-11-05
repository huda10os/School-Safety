"""Build data/reference/districts.csv from the Texas Education Agency (TEA) district file.

Source: TEA's public ArcGIS layer of districts (one point per district, with
TEA's 6-digit id, name and website). The first three digits of the id are
TEA's county number, which is the county's alphabetical position; the Census
county FIPS code is 2*n - 1. A fourth digit of 8 marks an open-enrollment
charter, which is out of scope.
"""
import csv
import io
import re

from config import REFERENCE
from fetch import Fetcher

TEA_LAYER = ("https://services2.arcgis.com/5MVN2jsqIrNZD4tP/arcgis/rest/services/"
             "districts_schools_20261005_D/FeatureServer/0/query")
CENSUS_COUNTIES = "https://www2.census.gov/geo/docs/reference/codes2020/cou/st48_tx_cou2020.txt"
REGULAR_SUFFIX = re.compile(r"\b(ISD|CISD|CSD|MSD)$")
FIELDS = ["tea_id", "school_district", "county_name", "county_fips", "website", "tea_source"]


def tea_districts(fetcher):
    rows, offset = [], 0
    while True:
        params = (f"?where=1%3D1&outFields=id,district_name,name,website&returnGeometry=false"
                  f"&orderByFields=id&resultOffset={offset}&resultRecordCount=1000&f=json")
        page = fetcher.get(TEA_LAYER + params, refresh=True).json()
        features = page.get("features", [])
        rows += [f["attributes"] for f in features]
        if not page.get("exceededTransferLimit") or not features:
            return rows
        offset += len(features)


def county_names(fetcher):
    text = fetcher.get(CENSUS_COUNTIES).text
    return {row["COUNTYFP"]: row["COUNTYNAME"] for row in csv.DictReader(io.StringIO(text), delimiter="|")}


def build():
    fetcher = Fetcher()
    counties = county_names(fetcher)
    districts, skipped = [], []
    for row in tea_districts(fetcher):
        tea_id = str(row["id"]).zfill(6)
        name = (row.get("district_name") or row.get("name") or "").strip()
        if tea_id[3] == "8" or not REGULAR_SUFFIX.search(name):
            skipped.append((tea_id, name))
            continue
        fips = f"{2 * int(tea_id[:3]) - 1:03d}"
        website = (row.get("website") or "").strip()
        if website and not website.startswith("http"):
            website = "https://" + website
        districts.append(dict(tea_id=tea_id, school_district=name, county_name=counties[fips],
                              county_fips="48" + fips, website=website, tea_source=TEA_LAYER))
    REFERENCE.mkdir(parents=True, exist_ok=True)
    with open(REFERENCE / "districts.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(sorted(districts, key=lambda d: d["tea_id"]))
    return districts, skipped


if __name__ == "__main__":
    districts, skipped = build()
    print(f"{len(districts)} regular districts written; {len(skipped)} skipped")
    for tea_id, name in skipped:
        if tea_id[3] != "8":
            print("skipped non-charter:", tea_id, name)
