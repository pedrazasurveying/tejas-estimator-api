from flask import Flask, request, jsonify, send_file, redirect, url_for
import requests
from shapely.geometry import shape
from shapely.ops import transform
import pyproj
import re
import os
import simplekml
import tempfile

app = Flask(__name__)

COUNTY_CONFIG = {
    "fortbend": {
        "endpoint": "https://gisweb.fbcad.org/arcgis/rest/services/Hosted/FBCAD_Public_Data/FeatureServer/0/query",
        "fields": {
            "street_num": "situssno",
            "street_name": "situssnm",
            "street_type": "situsstp",
            "owner": "ownername",
            "legal": "legal",
            "deed": "instrunum",
            "parcel_id": "propnumber",
            "quickrefid": "quickrefid",
            "acres": "landsizeac",
            "market": "totalvalue"
        }
    },
    "harris": {
        "endpoint": "https://services.arcgis.com/su8ic9KbA7PYVxPS/ArcGIS/rest/services/Harris_County_Parcels/FeatureServer/1/query",
        "fields": {
            "street_num": "site_str_num",
            "street_name": "site_str_name",
            "street_type": "site_str_sfx",
            "owner": "owner_name_1",
            "legal": "legal_desc",
            "deed": "deed_ref",
            "parcel_id": "HCAD_NUM",
            "quickrefid": "LOWPARCELID",
            "acres": "Acreage",
            "market": "MKT_VAL"
        }
    }
}

CRS_TARGET = "EPSG:2278"

def parse_address_loose(address):
    pattern = re.compile(r'^(\d+)?\s*([\w\s]+?)(\s+(RD|ST|DR|LN|BLVD|CT|AVE|HWY|WAY|TRAIL|PKWY|CIR))?$', re.IGNORECASE)
    match = pattern.search(address.strip().upper())
    if match:
        number = match.group(1) or ''
        name = match.group(2).strip()
        st_type = match.group(4).strip() if match.group(4) else ''
        return number.strip(), name, st_type
    return None, None, None

def parse_legal_description(legal):
    subdivision = block = lot = None
    subdivision_match = re.match(r'^(.*?)(BLOCK|LOT|RESERVE|ACRES)', legal, re.IGNORECASE)
    if subdivision_match:
        subdivision = subdivision_match.group(1).strip(", ").title()
    block_match = re.search(r'BLOCK\s+(\w+)', legal, re.IGNORECASE)
    if block_match:
        block = block_match.group(1)
    lot_match = re.search(r'(LOT|RESERVE)\s+["\w]+', legal, re.IGNORECASE)
    if lot_match:
        lot = lot_match.group(0).strip()
    return subdivision, block, lot

def query_parcels(endpoint, where_clause):
    params = {
        "where": where_clause,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson"
    }
    r = requests.get(endpoint, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("features", [])

def generate_kmz(geom, metadata=None):
    kml = simplekml.Kml()
    poly = None
    if geom.geom_type == "Polygon":
        coords = [(x, y) for x, y in list(geom.exterior.coords)]
        poly = kml.newpolygon(name="Parcel", outerboundaryis=coords)
    elif geom.geom_type == "MultiPolygon":
        for poly_geom in geom.geoms:
            coords = [(x, y) for x, y in list(poly_geom.exterior.coords)]
            poly = kml.newpolygon(name="Parcel Part", outerboundaryis=coords)
    if poly:
        poly.style.polystyle.fill = 0
        poly.style.linestyle.color = simplekml.Color.red
        poly.style.linestyle.width = 5
        if metadata:
            html = ''.join([f"<b>{k}:</b> {v}<br>" for k, v in metadata.items()])
            poly.description = html
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".kmz")
    kml.savekmz(tmp.name)
    return tmp.name

@app.route("/estimate", methods=["GET"])
def estimate():
    address = request.args.get("address")
    county = request.args.get("county", "fortbend").lower()
    quickref = request.args.get("quickref")

    if not (address or quickref):
        return jsonify({"error": "Missing address or quickref"}), 400

    if county not in COUNTY_CONFIG:
        return jsonify({"error": f"Unsupported county: {county}"}), 400

    config = COUNTY_CONFIG[county]
    endpoint = config["endpoint"]
    fields = config["fields"]

    matches = []

    if quickref:
        where_clause = f"{fields['quickrefid']} = '{quickref}'"
        matches = query_parcels(endpoint, where_clause)
    elif address:
        number, name, st_type = parse_address_loose(address)
        if not name:
            return jsonify({"error": "Invalid address format"}), 400

        clauses = []
        if number and st_type:
            clauses.append(f"{fields['street_num']} = '{number}' AND UPPER({fields['street_name']}) LIKE '%{name.upper()}%' AND UPPER({fields['street_type']}) = '{st_type.upper()}'")
        if number:
            clauses.append(f"{fields['street_num']} = '{number}' AND UPPER({fields['street_name']}) LIKE '%{name.upper()}%'")
        clauses.append(f"UPPER({fields['street_name']}) LIKE '%{name.upper()}%'")

        for clause in clauses:
            matches = query_parcels(endpoint, clause)
            if matches:
                break

    if not matches:
        return jsonify({"error": "No parcels found. Please check the address spelling or try providing a Quick Ref ID."}), 404

    feature = matches[0]
    props = feature["properties"]
    legal = props.get(fields["legal"], "N/A")
    deed = props.get(fields["deed"], "")
    owner = props.get(fields["owner"], "N/A")
    acres = props.get(fields["acres"], "N/A")
    market_val = props.get(fields["market"], "N/A")
    quickrefid = props.get(fields["quickrefid"], "")
    parcel_id = props.get(fields["parcel_id"], "")
    address_full = f"{props.get(fields['street_num'], '')} {props.get(fields['street_name'], '')} {props.get(fields['street_type'], '')}".strip()

    subdivision, block, lot = parse_legal_description(legal)

    geom = shape(feature["geometry"])
    project = pyproj.Transformer.from_crs("EPSG:4326", CRS_TARGET, always_xy=True).transform
    geom_proj = transform(project, geom)
    perimeter_ft = geom_proj.length
    area_ft2 = geom_proj.area
    area_acres = area_ft2 / 43560

    centroid = geom.centroid
    maps_url = f"https://www.google.com/maps/search/?api=1&query={centroid.y},{centroid.x}"

    kmz_metadata = {
        "Owner": owner,
        "Geo ID": parcel_id,
        "Legal": legal,
        "Subdivision": subdivision or "",
        "Block": block or "",
        "Lot/Reserve": lot or "",
        "Deed": deed or "",
        "Area (ac)": f"{area_acres:.2f}",
        "Perimeter (ft)": f"{perimeter_ft:.2f}"
    }

    kmz_path = generate_kmz(geom, metadata=kmz_metadata)
    with open(kmz_path, "rb") as f:
        kmz_bytes = f.read()

    download_kmz_url = url_for("download_kmz", address=address or "", quickref=quickref or "", county=county, _external=True)

    return jsonify({
        "owner": owner,
        "address": address_full,
        "legal_description": legal,
        "subdivision": subdivision,
        "block": block,
        "lot_reserve": lot,
        "deed": deed,
        "called_acreage": acres,
        "market_value": market_val,
        "quickrefid": quickrefid,
        "parcel_id": parcel_id,
        "parcel_size_acres": round(area_acres, 2),
        "perimeter_ft": round(perimeter_ft, 2),
        "maps_link": maps_url,
        "kmz_download_url": download_kmz_url
    })

@app.route("/download_kmz")
def download_kmz():
    address = request.args.get("address")
    county = request.args.get("county", "fortbend").lower()
    quickref = request.args.get("quickref")

    temp_kmz_path = "/tmp/parcel.kmz"
    if not os.path.exists(temp_kmz_path):
        return "KMZ not found. Please run an estimate first.", 404

    return send_file(temp_kmz_path, as_attachment=True, download_name="parcel.kmz")

@app.route("/openapi.json")
def openapi_spec():
    return jsonify({
        "openapi": "3.0.0",
        "info": {
            "title": "Tejas Estimator API",
            "version": "1.0.0",
            "description": "Retrieve parcel estimate details based on address or Quick Ref ID in Fort Bend or Harris County."
        },
        "servers": [
            { "url": "https://tejas-estimator-api.onrender.com" }
        ],
        "paths": {
            "/estimate": {
                "get": {
                    "operationId": "get_survey_estimate",
                    "summary": "Get survey estimate",
                    "parameters": [
                        {
                            "name": "address",
                            "in": "query",
                            "required": False,
                            "schema": { "type": "string" },
                            "description": "The property address to estimate."
                        },
                        {
                            "name": "quickref",
                            "in": "query",
                            "required": False,
                            "schema": { "type": "string" },
                            "description": "The Quick Ref ID to search."
                        },
                        {
                            "name": "county",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "enum": ["fortbend", "harris"]
                            },
                            "description": "The county to search in ('fortbend' or 'harris')."
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Successful response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "owner": { "type": "string" },
                                            "address": { "type": "string" },
                                            "legal_description": { "type": "string" },
                                            "subdivision": { "type": "string" },
                                            "block": { "type": "string" },
                                            "lot_reserve": { "type": "string" },
                                            "deed": { "type": "string" },
                                            "called_acreage": { "type": "string" },
                                            "market_value": { "type": "string" },
                                            "quickrefid": { "type": "string" },
                                            "parcel_id": { "type": "string" },
                                            "parcel_size_acres": { "type": "number" },
                                            "perimeter_ft": { "type": "number" },
                                            "maps_link": { "type": "string" },
                                            "kmz_download_url": { "type": "string" }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
