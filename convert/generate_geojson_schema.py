import json
from lxml import etree
from collections import defaultdict
from pathlib import Path

def parse_ea_xmi_to_geojson_schema(xmi_file: str, xsd_file: str, output_file: str, split_defs: bool = False):
    """
    Parse an EA-exported XMI model and XSD file to generate JSON Schema(s).

    If split_defs is False (default), generate a single GeoJSON schema
    for ObservingFacility FeatureCollection, including $defs for Equipment and Observation.

    If split_defs is True, generate three separate schema files for ObservingFacility,
    Equipment, and Observation, plus a wrapper and geometry schema.

    Parameters:
        xmi_file (str): Path to EA-exported UML model in XMI format
        xsd_file (str): Path to the supporting XSD file (used for type inference)
        output_file (str): Output path for schema (or base folder if split_defs=True)
        split_defs (bool): Whether to split definitions into separate schema files
    """
    tree = etree.parse(xmi_file)
    root = tree.getroot()

    class_ids = {}
    attributes_by_id = {}
    inheritance = {}
    class_names_by_id = {}
    documentation_by_id = {}
    cardinality_map = {}

    # Parse UML classes and owned attributes from XMI
    for elem in root.iter():
        tag_local = etree.QName(elem.tag).localname
        if tag_local == "packagedElement" and elem.get("{http://www.omg.org/XMI}type") == "uml:Class":
            class_id = elem.get("{http://www.omg.org/XMI}id")
            class_name = elem.get("name")
            if not class_name:
                continue
            class_ids[class_name] = class_id
            class_names_by_id[class_id] = class_name
            attributes_by_id[class_id] = []
            for sub in elem:
                if etree.QName(sub.tag).localname == "ownedAttribute":
                    attr_name = sub.get("name")
                    if attr_name is None:
                        continue
                    attr_type = sub.get("type")
                    attr_agg = sub.get("aggregation")
                    lower = sub.get("lowerValue") or sub.get("lower")
                    upper = sub.get("upperValue") or sub.get("upper")
                    attributes_by_id[class_id].append({
                        "name": attr_name,
                        "type": attr_type,
                        "aggregation": attr_agg,
                        "id": sub.get("{http://www.omg.org/XMI}id"),
                        "lower": sub.get("lower"),
                        "upper": sub.get("upper")
                    })
                elif etree.QName(sub.tag).localname == "ownedComment":
                    body = sub.find(".//body")
                    if body is not None and body.text:
                        documentation_by_id[class_id] = body.text.strip()

    # Parse generalization relationships (inheritance)
    for elem in root.iter():
        if etree.QName(elem.tag).localname == "generalization":
            subclass_id = elem.get("owner")
            if not subclass_id:
                parent = elem.getparent()
                if parent is not None:
                    subclass_id = parent.get("{http://www.omg.org/XMI}id")
            superclass_id = elem.get("general")
            if subclass_id and superclass_id:
                inheritance[subclass_id] = superclass_id

    def collect_all_attributes(class_name):
        collected = []
        visited = set()
        cid = class_ids.get(class_name)
        while cid and cid not in visited:
            visited.add(cid)
            collected.extend(attributes_by_id.get(cid, []))
            cid = inheritance.get(cid)
        return collected

    def build_type_map_from_xsd(xsd_file):
        type_map = {}
        xsd_tree = etree.parse(xsd_file)
        for elem in xsd_tree.iter():
            if elem.tag.endswith("element") and "name" in elem.attrib:
                t = elem.get("type")
                if t and ":" in t:
                    t = t.split(":", 1)[1]
                name = elem.get("name")
                if t:
                    type_map[name] = t
        return type_map

    xsd_to_json_type = {
        "boolean": {"type": "boolean"},
        "string": {"type": "string"},
        "decimal": {"type": "number"},
        "float": {"type": "number"},
        "double": {"type": "number"},
        "int": {"type": "integer"},
        "integer": {"type": "integer"},
        "date": {"type": "string", "format": "date"},
        "dateTime": {"type": "string", "format": "date-time"}
    }

    xsd_type_map = build_type_map_from_xsd(xsd_file)

    def infer_json_type(attr):
        attr_type_id = attr.get("type")
        attr_name = attr.get("name")
        if attr["aggregation"] == "composite" and attr_type_id in class_names_by_id:
            ref = {"$ref": f"#/$defs/{class_names_by_id[attr_type_id]}"}
            return {"type": "array", "items": ref} if attr.get("upper") != "1" else ref
        elif attr_name in xsd_type_map:
            xsd_type = xsd_type_map[attr_name]
            base_type = xsd_to_json_type.get(xsd_type, {"type": "string"})
            if attr.get("upper") != "1":
                return {"type": "array", "items": base_type}
            return base_type
        return {"type": "string"}

    def attrs_to_json_schema(attr_list, exclude=None):
        schema_props = {}
        required = []
        for attr in attr_list:
            name = attr.get("name")
            if name and (exclude is None or name not in exclude):
                prop = infer_json_type(attr)
                doc = documentation_by_id.get(attr.get("id"))
                if doc:
                    prop["description"] = doc
                schema_props[name] = prop
                if attr.get("lower") not in (None, "0"):
                    required.append(name)
        return schema_props, required

    def write_schema_to_file(schema_dict, out_path):
        with open(out_path, "w") as f:
            json.dump(schema_dict, f, indent=2)

    observing_attrs = collect_all_attributes("ObservingFacility")
    equipment_attrs = collect_all_attributes("Equipment")
    observation_attrs = collect_all_attributes("Observation")

    if split_defs:
        base_path = Path(output_file).parent
        defs = {
            "ObservingFacility": attrs_to_json_schema(observing_attrs, exclude={"geospatialLocation"}),
            "Equipment": attrs_to_json_schema(equipment_attrs),
            "Observation": attrs_to_json_schema(observation_attrs)
        }
        for name, (props, req) in defs.items():
            schema = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"https://schemas.wmo.int/wmdr/json-schema/{name}.schema.json",
                "title": name,
                "type": "object",
                "properties": props,
                "required": req
            }
            write_schema_to_file(schema, base_path / f"{name}.schema.json")

        wrapper_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.wmo.int/wmdr/json-schema/FeatureCollection.schema.json",
            "title": "ObservingFacility FeatureCollection",
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["FeatureCollection"]},
                "features": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["Feature"]},
                            "geometry": {"$ref": "geometry.schema.json"},
                            "properties": {"$ref": "ObservingFacility.schema.json"}
                        },
                        "required": ["type", "geometry", "properties"]
                    }
                }
            },
            "required": ["type", "features"]
        }
        write_schema_to_file(wrapper_schema, base_path / "FeatureCollection.schema.json")

        geom_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.wmo.int/wmdr/json-schema/geometry.schema.json",
            "title": "Geometry",
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["Point"]},
                "coordinates": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 3
                }
            },
            "required": ["type", "coordinates"]
        }
        write_schema_to_file(geom_schema, base_path / "geometry.schema.json")

    else:
        obs_props, obs_required = attrs_to_json_schema(observing_attrs, exclude={"geospatialLocation"})
        equip_props, _ = attrs_to_json_schema(equipment_attrs)
        obsrv_props, _ = attrs_to_json_schema(observation_attrs)

        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.wmo.int/wmdr/json-schema/FeatureCollection.schema.json",
            "title": "ObservingFacility FeatureCollection",
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["FeatureCollection"]},
                "features": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["Feature"]},
                            "geometry": {"$ref": "#/$defs/geometry"},
                            "properties": {"$ref": "#/$defs/ObservingFacility"}
                        },
                        "required": ["type", "geometry", "properties"]
                    }
                }
            },
            "required": ["type", "features"],
            "$defs": {
                "geometry": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["Point"]},
                        "coordinates": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 3
                        }
                    },
                    "required": ["type", "coordinates"]
                },
                "Equipment": {
                    "type": "object",
                    "properties": equip_props
                },
                "Observation": {
                    "type": "object",
                    "properties": obsrv_props
                },
                "ObservingFacility": {
                    "type": "object",
                    "properties": obs_props,
                    "required": obs_required
                }
            }
        }

        obs_defs = schema["$defs"]["ObservingFacility"]["properties"]
        if "equipment" in obs_defs:
            obs_defs["equipment"] = {"type": "array", "items": {"$ref": "#/$defs/Equipment"}}
        if "observation" in obs_defs:
            obs_defs["observation"] = {"type": "array", "items": {"$ref": "#/$defs/Observation"}}

        write_schema_to_file(schema, output_file)
