from flask import Flask, request, jsonify
import requests
from shapely.geometry import shape
from shapely.ops import transform
import pyproj
import re
import os

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

@app.route("/estimate", methods=["GET"])
def estimate():
    address = request.args.get("address")
    county = request.args.get("county", "fortbend").lower()

    if not address:
        return jsonify({"error": "Missing address"}), 400

    if county not in COUNTY_CONFIG:
        return jsonify({"error": f"Unsupported county: {county}"}), 400

    config = COUNTY_CONFIG[county]
    endpoint = config["endpoint"]
    fields = config["fields"]

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
    else:
        return jsonify({"error": "No parcels found"}), 404

    feature = matches[0]
    props = feature["properties"]
    legal = props.get(fields["legal"], "N/A")
    deed = props.get(fields["deed"], "")
    owner = props.get(fields["owner"], "N/A")
    acres = props.get(fields["acres"], "N/A")
    market_val = props.get(fields["market"], "N/A")
    address_full = f"{props.get(fields['street_num'], '')} {props.get(fields['street_name'], '')} {props.get(fields['street_type'], '')}".strip()

    geom = shape(feature["geometry"])
    project = pyproj.Transformer.from_crs("EPSG:4326", CRS_TARGET, always_xy=True).transform
    geom_proj = transform(project, geom)
    perimeter_ft = geom_proj.length
    area_ft2 = geom_proj.area
    area_acres = area_ft2 / 43560

    return jsonify({
        "owner": owner,
        "address": address_full,
        "legal_description": legal,
        "deed": deed,
        "called_acreage": acres,
        "market_value": market_val,
        "parcel_size_acres": round(area_acres, 2),
        "perimeter_ft": round(perimeter_ft, 2)
    })

@app.route("/openapi.json")
def openapi_spec():
    return jsonify({
        "openapi": "3.0.0",
        "info": {
            "title": "Tejas Estimator API",
            "version": "1.0.0",
            "description": "Retrieve parcel estimate details based on address and county."
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
                            "required": True,
                            "schema": { "type": "string" },
                            "description": "The full address to search."
                        },
                        {
                            "name": "county",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "enum": ["fortbend", "harris"]
                            },
                            "description": "The county to search in."
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
                                            "deed": { "type": "string" },
                                            "called_acreage": { "type": "string" },
                                            "market_value": { "type": "string" },
                                            "parcel_size_acres": { "type": "number" },
                                            "perimeter_ft": { "type": "number" }
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
