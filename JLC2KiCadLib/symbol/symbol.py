import json
import logging
import os
import re
from datetime import datetime

import requests
from jlcparts.datatables import extractComponent
from jlcparts.partLib import PartLibraryDb

from .. import helper
from .symbol_handlers import handlers

template_lib_header = """\
(kicad_symbol_lib (version 20210201) (generator TousstNicolas/JLC2KiCad_lib)
"""

template_lib_footer = ")\n"

supported_value_types = [
    "Resistance",
    "Capacitance",
    "Inductance",
    "Frequency",
]  # define which attribute/value from JLCPCB/LCSC will be added in the "value" field

def load_jlcparts_metadata(jlcparts_db: str, component_id: str) -> dict[str, str]:

    props: dict[str, str] = {}
    pLib = PartLibraryDb(filepath=jlcparts_db)

    if pLib.exists(component_id):
        component = pLib.getComponent(component_id)
        schema = (
            "attributes", "datasheet", "price", "description"
        )
        properties = extractComponent(
            component,
            schema,
        )

        for i, schemaItem in enumerate(schema):
            props[schemaItem] = properties[i]
        props["manufacturer"] = props.get("manufacturer") or component.get("manufacturer", "") # Override JLCParts None
        props["mfr"] = component.get("mfr", "")
        if props.get("description", "") == "":
            props["description"] = component.get("extra", {}).get("description", "")
        # component's attributes includes the value with non standard units, properties standardizes them
        use_standard_units = False
        for value_type in supported_value_types:
            if value_type in props.get("attributes", []):
                if use_standard_units:
                    props["value"] = (
                        str(props["attributes"][value_type].get("values", {}).get(value_type.lower(), [""])[0])
                        .rstrip('0').rstrip('.')
                    )
                else:
                    props["value"] = (
                        component.get("extra", {}).get("attributes", {}).get(value_type, "")
                    )

        target_qty = 100
        price = ""
        if isinstance(props.get("price"), list) and all(isinstance(p, dict) for p in props["price"]):
            price = next(
                (
                    p.get("price", "")
                    for p in props["price"]
                    if target_qty >= p.get("qFrom", 1e5)
                    and (not p.get("qTo", True) or target_qty <= p.get("qTo", -1))
                ),
            "")
        try:
            last_on_stock = int(component.get('last_on_stock', "0"))
        except ValueError:
            last_on_stock = 0
        dt = datetime.fromtimestamp(last_on_stock)
        formatted = dt.strftime("%Y-%m-%d %H:%M")
        logging.warning(
            f"{component_id:>10}: ${price:>10} p.u.[x{target_qty}]"
            f"{'(' + component.get('category','') + '/' + component.get('subcategory',''):>20})"
            f" RoHS {component.get('extra', {}).get('rohs', '?')}"
            f" Stock {component.get('stock', 'Unknown'):} ({formatted})"
            f" {'Basic' if component.get('basic', False) else 'Extended':>8}"
        )
    else:
        logging.info(f"Component {component_id} not found in {jlcparts_db}")

    return props

def create_symbol(
    symbol_component_uuid,
    footprint_name,
    datasheet_link: str,
    library_name,
    symbol_path,
    output_dir,
    component_id: str,
    skip_existing,
    jlcparts_db: str,
):
    """
    If `jlcparts_db` is given and datasheet from JLCParts is available,
    override `datasheet_link`
    """
    class kicad_symbol:
        drawing = ""
        pinNamesHide = "(pin_names hide)"
        pinNumbersHide = "(pin_numbers hide)"

    kicad_symbol = kicad_symbol()

    ComponentName = ""
    for component_uuid in symbol_component_uuid:
        response = requests.get(
            f"https://easyeda.com/api/components/{component_uuid}",
            headers={"User-Agent": helper.get_user_agent()},
        )
        if response.status_code == requests.codes.ok:
            data = json.loads(response.content.decode())
        else:
            logging.error(
                f"create_symbol error. Requests returned with error code "
                f"{response.status_code}"
            )
            return ()

        symbol_shape = data["result"]["dataStr"]["shape"]
        symmbol_prefix = data["result"]["packageDetail"]["dataStr"]["head"]["c_para"][
            "pre"
        ].replace("?", "")
        component_title = (
            data["result"]["title"]
            .replace(" ", "_")
            .replace(".", "_")
            .replace("/", "{slash}")
            .replace("\\", "{backslash}")
            .replace("<", "{lt}")
            .replace(">", "{gt}")
            .replace(":", "{colon}")
            .replace('"', "{dblquote}")
        )

        component_types_values: list[tuple[str, str]] = []
        for value_type in supported_value_types:
            if value_type in data["result"]["dataStr"]["head"]["c_para"]:
                component_types_values.append(
                    (
                        value_type,
                        data["result"]["dataStr"]["head"]["c_para"][value_type],
                    )
                )

        if not ComponentName:
            ComponentName = component_title
            component_title += "_0"
        if (
            len(symbol_component_uuid) >= 2
            and component_uuid == symbol_component_uuid[0]
        ):
            continue

        props: dict[str, str] = {}
        if os.path.isfile(jlcparts_db):
            props = load_jlcparts_metadata(jlcparts_db, component_id)

        value = props.get("value", "") or ComponentName
        datasheet_link = props.get("datasheet", datasheet_link)
        component_types_values.extend(
            [
                ("JLCDescription", props.get("description", "")),
                ("Manufacturer", props.get("manufacturer", "")),
                ("MFR.Part.#", props.get("mfr", "")),
            ]
        )

        # if library_name is not defined, use component_title as library name
        if not library_name:
            library_name = ComponentName

        filename = f"{output_dir}/{symbol_path}/{library_name}.kicad_sym"

        logging.info(f"Creating symbol {component_title} in {library_name}")

        kicad_symbol.drawing += f'''\n    (symbol "{component_title}_1"'''

        for line in symbol_shape:
            args = [i for i in line.split("~")]  # split arguments
            model = args[0]
            logging.debug(args)
            if model not in handlers:
                logging.warning("symbol : parsing model not in handler : " + model)
            else:
                handlers.get(model)(
                    data=args[1:],
                    translation=(
                        data["result"]["dataStr"]["head"]["x"],
                        data["result"]["dataStr"]["head"]["y"],
                    ),
                    kicad_symbol=kicad_symbol,
                )
        kicad_symbol.drawing += """\n    )"""

    # ruff: disable [E501]
    template_lib_component = f"""\
  (symbol "{ComponentName}" {kicad_symbol.pinNamesHide} {kicad_symbol.pinNumbersHide} (in_bom yes) (on_board yes)
    (property "Reference" "{symmbol_prefix}" (id 0) (at 0 1.27 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "{value}" (id 1) (at 0 -2.54 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "{footprint_name}" (id 2) (at 0 -10.16 0)
      (effects (font (size 1.27 1.27) italic) hide)
    )
    (property "Datasheet" "{datasheet_link}" (id 3) (at -2.286 0.127 0)
      (effects (font (size 1.27 1.27)) (justify left) hide)
    )
    (property "ki_keywords" "{component_id}" (id 4) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "LCSC" "{component_id}" (id 5) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    {get_type_values_properties(6, component_types_values)}{kicad_symbol.drawing}
  )
"""
    # ruff: enable [E501]

    if not os.path.exists(f"{output_dir}/{symbol_path}"):
        os.makedirs(f"{output_dir}/{symbol_path}")

    if os.path.exists(filename):
        update_library(
            library_name,
            symbol_path,
            ComponentName,
            template_lib_component,
            output_dir,
            skip_existing,
        )
    else:
        with open(filename, "w") as f:
            logging.info(f"writing in {filename} file")
            f.write(template_lib_header)
            f.write(template_lib_footer)
        update_library(
            library_name,
            symbol_path,
            ComponentName,
            template_lib_component,
            output_dir,
            skip_existing,
        )


def get_type_values_properties(start_index, component_types_values):
    # ruff: disable [E501]
    return "\n".join(
        [
            f"""(property "{type_value[0]}" "{type_value[1]}" (id {start_index + index}) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )"""
            for index, type_value in enumerate(component_types_values)
        ]
    )
    # ruff: enable [E501]


def update_library(
    library_name,
    symbol_path,
    component_title,
    template_lib_component,
    output_dir,
    skip_existing,
):
    """
    if component is already in library,
    the library will be updated,
    if not already present in library,
    the component will be added at the end
    """

    with open(
        f"{output_dir}/{symbol_path}/{library_name}.kicad_sym", "rb+"
    ) as lib_file:
        pattern = rf'  \(symbol "{component_title}" (\n|.)*?\n  \)'
        file_content = lib_file.read().decode()

        if f'symbol "{component_title}"' in file_content:
            if skip_existing:
                logging.info(
                    f"component {component_title} already in symbols library, skipping"
                )
                return
            # use regex to find the old component template in the file and
            # replace it with the new one
            logging.info(
                f"found component already in {library_name}, updating {library_name}"
            )
            sub = re.sub(
                pattern=pattern,
                repl=template_lib_component,
                string=file_content,
                flags=re.DOTALL,
                count=1,
            )
            lib_file.seek(0)
            # delete the file content and rewrite it
            lib_file.truncate()
            lib_file.write(sub.encode())
        else:
            # move before the library footer and write the component template
            # see https://github.com/TousstNicolas/JLC2KiCad_lib/issues/46
            new_content = file_content[: file_content.rfind(")")]
            new_content = new_content + template_lib_component + template_lib_footer
            lib_file.seek(0)
            lib_file.write(new_content.encode())
