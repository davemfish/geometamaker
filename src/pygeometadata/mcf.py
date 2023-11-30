from datetime import datetime
import os
import yaml

# import jsonschema
import pygeometa.core
from pygeometa.schemas.iso19139 import ISO19139OutputSchema
from pygeometa.schemas.iso19139_2 import ISO19139_2OutputSchema
import pygeoprocessing
from osgeo import gdal
from osgeo import ogr
from osgeo import osr

# https://stackoverflow.com/questions/13518819/avoid-references-in-pyyaml
yaml.Dumper.ignore_aliases = lambda *args: True

MCF_SCHEMA_FILE = os.path.join(
    pygeometa.core.SCHEMAS, 'mcf', 'core.yaml')
with open(MCF_SCHEMA_FILE, 'r') as schema_file:
    MCF_SCHEMA = pygeometa.core.yaml_load(schema_file)

# modify the core MCF schema
MCF_SCHEMA['required'].append('content_info')
MCF_SCHEMA['properties']['content_info']['required'].append(
    'attributes')
MCF_SCHEMA['properties']['identification']['properties'][
    'keywords']['patternProperties']['^.*'][
    'required'] = ['keywords']
# It's not clear to me why 'spatial' is type 'array' instead of
# 'object', since it contains 'properties'.
MCF_SCHEMA['properties']['identification']['properties'][
    'extents']['properties']['spatial']['type'] = 'object'
MCF_SCHEMA['properties']['identification']['properties'][
    'extents']['properties']['temporal']['type'] = 'object'

# TODO: read types from the #/definitions found in MCF_SCHEMA
# instead of hardcoding values here
DEFAULT_VALUES = {
    'string': '',
    'int': 0,
    'integer': 0,
    'number': 0,
    'array': [],
    'list': [],
    'tuple': [],
    'dict': {},
    'object': {},
    'boolean': 'false',
    '#/definitions/date_or_datetime_string': '',
    '#/definitions/i18n_string': '',
    '#/definitions/i18n_array': [],
    '#/definitions/any_type': '',
}


def get_default(item):
    try:
        t = item['type']
    except KeyError:
        # When 'type' is missing, a $ref to another schema is present
        try:
            t = item['$ref']
        except KeyError:
            raise KeyError(
                f'schema has no type and no reference to a type definition\n'
                f'{item}')
    if t == 'object':
        return get_template(item)
    else:
        return DEFAULT_VALUES[t]


def get_template(schema):
    """
    schema: a jsonschema dict with a 'properties' key
    """
    template = {}
    for prop, sch in schema['properties'].items():
        if prop not in schema['required']:
            continue
        if 'patternProperties' in sch:
            # this item's properties can have any name matching the pattern.
            # assign the name 'default' and overwite the current schema
            # with a new one that explicitly includes the 'default' property.
            example_sch = {
                'type': 'object',
                'required': ['default'],
                'properties': {
                    'default': sch['patternProperties']['^.*']
                }
            }
            sch = example_sch

        if 'type' in sch and sch['type'] == 'object':
            if 'anyOf' in sch['properties']:
                template[prop] = {
                    p: get_default(s)
                    for p, s in sch['properties']['anyOf'].items()
                }
            else:
                template[prop] = get_template(sch)

        elif 'type' in sch and sch['type'] == 'array':
            template[prop] = [get_default(sch)]
        else:
            template[prop] = get_default(sch)
    return template


class MCF:

    def __init__(self, source_dataset_path, profile_list=None):
        self.datasource = source_dataset_path
        self.mcf = get_template(MCF_SCHEMA)
        self.mcf['mcf']['version'] = \
            MCF_SCHEMA['properties']['mcf']['properties']['version']['const']

        self.mcf['attributes'] = {}  # arbitrary extras

        # fill all values that can be derived from the dataset
        self.get_spatial_info()

    def keywords(self, keywords, group='default', language='en',
                 keywords_type='theme', vocabulary=None):
        keywords_dict = {
            'keywords': {language: keywords},
            'keywords_type': keywords_type
        }
        if vocabulary:
            keywords_dict['vocabulary']: vocabulary
        self.mcf['identification']['keywords'][group] = keywords_dict

    def describe_band(self, index, name, title=None, abstract=None,
                      type=None, units=None):
        """Define metadata for a raster band."""

    def describe_field(self, name, title=None, abstract=None,
                       type=None, units=None):
        """Define metadata for a tabular field."""

    def write(self):
        with open(f'{self.datasource}.yml', 'w') as file:
            file.write(yaml.dump(self.mcf))

    def validate(self):
        pygeometa.core.validate_mcf(self.mcf)

    def to_string(self):
        pass

    def get_spatial_info(self):
        gis_type = pygeoprocessing.get_gis_type(self.datasource)
        if gis_type == pygeoprocessing.UNKNOWN_TYPE:
            self.mcf['metadata']['hierarchylevel'] = 'nonGeographicDataset'
            return

        if gis_type == pygeoprocessing.VECTOR_TYPE:
            self.mcf['metadata']['hierarchylevel'] = 'dataset'
            self.mcf['spatial']['datatype'] = 'vector'
            self.mcf['content_info']['type'] = 'coverage'

            vector = gdal.OpenEx(self.datasource, gdal.OF_VECTOR)
            layer = vector.GetLayer()
            layer_defn = layer.GetLayerDefn()
            geomname = ogr.GeometryTypeToName(layer_defn.GetGeomType())
            geomtype = ''
            # https://www.fgdc.gov/nap/metadata/register/codelists.html
            if 'Point' in geomname:
                geomtype = 'point'
            if 'Polygon' in geomname:
                geomtype = 'surface'
            if 'Line' in geomname:
                geomtype = 'curve'
            if 'Collection' in geomname:
                geomtype = 'complex'
            self.mcf['spatial']['geomtype'] = geomtype

            attributes = []
            for field in layer.schema:
                attribute = {}
                attribute['name'] = field.name
                attribute['type'] = field.GetTypeName().lower()
                attribute['units'] = ''
                attribute['title'] = ''
                attribute['abstract'] = ''
                attributes.append(attribute)
            self.mcf['content_info']['attributes'] = attributes
            vector = None
            layer = None

            gis_info = pygeoprocessing.get_vector_info(self.datasource)

        if gis_type == pygeoprocessing.RASTER_TYPE:
            self.mcf['metadata']['hierarchylevel'] = 'dataset'
            self.mcf['spatial']['datatype'] = 'grid'
            self.mcf['spatial']['geomtype'] = 'surface'
            self.mcf['content_info']['type'] = 'image'

            raster = gdal.OpenEx(self.datasource, gdal.OF_RASTER)
            attributes = []
            for i in range(raster.RasterCount):
                b = i + 1
                band = raster.GetRasterBand(b)
                attribute = {}
                attribute['name'] = f'band{b}'
                attribute['type'] = 'integer' if band.DataType < 6 else 'number'
                attribute['units'] = ''
                attribute['title'] = ''
                attribute['abstract'] = band.GetDescription()
                attributes.append(attribute)
            self.mcf['content_info']['attributes'] = attributes
            raster = None

            gis_info = pygeoprocessing.get_raster_info(self.datasource)

        srs = osr.SpatialReference()
        srs.ImportFromWkt(gis_info['projection_wkt'])
        epsg = srs.GetAttrValue('AUTHORITY', 1)
        # for human-readable values after yaml dump, use python types
        # instead of numpy types
        bbox = [float(x) for x in gis_info['bounding_box']]
        spatial_info = [
            {'bbox': bbox},
            {'crs': epsg}  # MCF does not support WKT here
        ]
        self.mcf['identification']['extents']['spatial'] = spatial_info