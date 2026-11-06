#!/usr/bin/env python3
"""Rebuild the app's vendored geography from public sources.

    python code/app/reference_build.py TEA_DIR CCD_JSON TX_GEOJSON

TEA_DIR holds the JSON pages of the TEA district layer (district office
points), fetched from the `tea_source` URL in data/reference/districts.csv:
    <url>/query?where=1%3D1&outFields=id,district_name,latitude,longitude,address
        &returnGeometry=false&f=json&resultOffset=0&resultRecordCount=1000
CCD_JSON is the NCES Common Core of Data district directory for Texas via the
Urban Institute Education Data API (enrollment and NCES id):
    https://educationdata.urban.org/api/v1/school-districts/ccd/directory/2022/?fips=48&page_size=2000
TX_GEOJSON is the Texas state outline from the US Census TIGERweb service
(public domain), simplified to 0.02 degrees:
    https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/0/query
        ?where=STUSAB%3D%27TX%27&outFields=NAME&returnGeometry=true&outSR=4326
        &maxAllowableOffset=0.02&f=geojson
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent


def main(tea_dir: str, ccd_json: str, tx_geojson: str) -> None:
    points = {}
    for page in sorted(Path(tea_dir).glob("tea_*.json")):
        for feature in json.loads(page.read_text())["features"]:
            a = feature["attributes"]
            if a.get("id") and a.get("latitude") is not None:
                points[a["id"]] = a
    ccd = {}
    for row in json.loads(Path(ccd_json).read_text())["results"]:
        sid = (row.get("state_leaid") or "").removeprefix("TX-")
        if sid:
            ccd[sid] = row
    out = APP / "reference" / "district_points.csv"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["tea_id", "lat", "lon", "city", "nces_id", "enrollment", "enrollment_year"])
        for tea_id in sorted(points):
            a, c = points[tea_id], ccd.get(tea_id, {})
            city = (a.get("address") or "").split(". ")[-2:-1]
            enrollment = c.get("enrollment")
            w.writerow([tea_id, round(a["latitude"], 5), round(a["longitude"], 5), city[0] if city else "",
                        c.get("leaid") or "", enrollment if isinstance(enrollment, int) and enrollment >= 0 else "",
                        2022 if c else ""])
    geo = json.loads(Path(tx_geojson).read_text())
    rings = []
    for feature in geo["features"]:
        g = feature["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            rings.append([[round(x, 3), round(y, 3)] for x, y in poly[0]])
    (APP / "static" / "texas.json").write_text(json.dumps({
        "source": "US Census Bureau TIGERweb, State_County layer, Texas outline (public domain), simplified to 0.02 degrees",
        "rings": rings}, separators=(",", ":")))
    print(f"{len(points)} district points, {sum(1 for t in points if t in ccd)} with enrollment; {len(rings)} outline rings")


if __name__ == "__main__":
    main(*sys.argv[1:4])
