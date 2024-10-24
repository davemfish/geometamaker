import logging
import os
import uuid
from datetime import datetime

import jsonschema
from jsonschema.exceptions import ValidationError
import pygeometa.core
from pygeometa.schemas.iso19139 import ISO19139OutputSchema
from pygeometa.schemas.iso19139_2 import ISO19139_2OutputSchema
import pygeoprocessing
from osgeo import gdal
from osgeo import ogr
from osgeo import osr
import yaml


# https://stackoverflow.com/questions/13518819/avoid-references-in-pyyaml
class _NoAliasDumper(yaml.SafeDumper):
    """Keep the yaml human-readable by avoiding anchors and aliases."""

    def ignore_aliases(self, data):
        return True


LOGGER = logging.getLogger(__name__)

MCF_SCHEMA_FILE = os.path.join(
    pygeometa.core.SCHEMAS, 'mcf', 'core.yaml')
with open(MCF_SCHEMA_FILE, 'r') as schema_file:
    MCF_SCHEMA = pygeometa.core.yaml_load(schema_file)

# modify the core MCF schema so that our default
# template MCFs have all the properties we expect
# users to use.
MCF_SCHEMA['required'].append('content_info')
MCF_SCHEMA['properties']['content_info']['required'].append(
    'attributes')
MCF_SCHEMA['required'].append('dataquality')
MCF_SCHEMA['properties']['identification']['properties'][
    'keywords']['patternProperties']['^.*'][
    'required'] = ['keywords', 'keywords_type']

OGR_MCF_ATTR_TYPE_MAP = {
    ogr.OFTInteger: 'integer',
    ogr.OFTInteger64: 'integer',
    ogr.OFTReal: 'number',
    ogr.OFTString: 'string'
}


def _get_default(item):
    """Return a default value for a property.

    Args:
        item (dict): a jsonschema definition of a property with no children.

    Return:
        a value from DEFAULT_VALUES

    Raises:
        KeyError if ``item`` does not include an
        'enum', 'type', or '$ref' property.

    """
    # TODO: read types from the #/definitions found in MCF_SCHEMA
    # instead of hardcoding values here
    # TODO: support i18n properly by using objects
    # keyed by country codes to contain the array of strings
    default_values = {
        'string': str(),
        'int': int(),
        'integer': int(),
        'number': float(),
        'boolean': False,
        '#/definitions/date_or_datetime_string': str(),
        '#/definitions/i18n_string': str(),
        '#/definitions/i18n_array': list(),
        '#/definitions/any_type': str(),
    }

    # If there are enumerated values which must be used
    try:
        fixed_values = item['enum']
        # TODO: find a better way to choose the default
        return fixed_values[0]
    except KeyError:
        pass

    # If no enumerated values, get a default value based on type
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

    return default_values[t]


def _get_template(schema):
    """Create a minimal dictionary that is valid against ``schema``.

    The dict will ontain only the 'required' properties.

    Args:
        schema (dict): a jsonschema definition.

    Return:
        dict that is valid against ``schema``

    Raises:
        KeyError if a penultimate property in a schema branch
        does not include an 'enum', 'type', or '$ref' property.

    """
    template = {}
    if 'type' in schema and schema['type'] == 'object':
        for prop, sch in schema['properties'].items():
            if 'required' in schema and prop not in schema['required']:
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

            if 'properties' in sch and 'anyOf' in sch['properties']:
                # if 'anyOf' is a property, then we effectively want to
                # treat the children of 'anyOf' as the properties instead.
                template[prop] = {
                    p: _get_template(s)
                    for p, s in sch['properties']['anyOf'].items()
                }
            else:
                template[prop] = _get_template(sch)
        return template

    elif 'type' in schema and schema['type'] == 'array':
        if 'properties' in schema:
            # for the weird case where identification.extents.spatial
            # is type: array but contains 'properties' instead of 'items'
            return [{
                p: _get_template(s)
                for p, s in schema['properties'].items()
                if p in schema['required']
            }]
        return [_get_template(schema['items'])]
    else:
        return _get_default(schema)


class MetadataControl(object):
    """Encapsulates the Metadata Control File and methods for populating it.

    A Metadata Control File (MCF) is a YAML file that complies with the
    MCF specification defined by pygeometa.
    https://github.com/geopython/pygeometa

    Attributes:
        datasource (string): path to dataset to which the metadata applies
        mcf (dict): dict representation of the Metadata Control File

    """

    def __init__(self, source_dataset_path=None):
        """Create an MCF instance, populated with properties of the dataset.

        The MCF will be valid according to the pygeometa schema. It has
        all required properties. Properties of the dataset are used to
        populate as many MCF properties as possible. Default/placeholder
        values are used for properties that require user input.

        Instantiating without a ``source_dataset_path`` creates an MCF template.

        Args:
            source_dataset_path (string): path to dataset to which the metadata
                applies

        """
        self.mcf = None
        if source_dataset_path is not None:
            self.datasource = source_dataset_path
            self.mcf_path = f'{self.datasource}.yml'

            if os.path.exists(self.mcf_path):
                try:
                    # pygeometa.core.read_mcf can parse nested MCF documents,
                    # where one MCF refers to another
                    self.mcf = pygeometa.core.read_mcf(self.mcf_path)
                    self.validate()
                except (pygeometa.core.MCFReadError, ValidationError,
                        AttributeError) as err:
                    # AttributeError in read_mcf not caught by pygeometa
                    LOGGER.warning(err)
                    self.mcf = None

            if self.mcf is None:
                self.mcf = _get_template(MCF_SCHEMA)
                self.mcf['metadata']['identifier'] = str(uuid.uuid4())

                # fill all values that can be derived from the dataset
                self._set_spatial_info()
                self.mcf['metadata']['datestamp'] = datetime.utcnow(
                    ).strftime('%Y-%m-%d')

        else:
            self.mcf = _get_template(MCF_SCHEMA)
        self.mcf['mcf']['version'] = \
            MCF_SCHEMA['properties']['mcf'][
                'properties']['version']['const']

    def set_title(self, title):
        """Add a title for the dataset.

        Args:
            title (str)

        """
        self.mcf['identification']['title'] = title

    def set_abstract(self, abstract):
        """Add an abstract for the dataset.

        Args:
            abstract (str)

        """
        self.mcf['identification']['abstract'] = abstract

    def set_contact(self, organization=None, individualname=None, positionname=None,
                    email=None, section='default', **kwargs):
        """Add a contact section.

        Args:
            organization (str): name of the responsible organization
            individualname (str): name of the responsible person
            positionname (str): role or position of the responsible person
            email (str): email address of the responsible organization or individual
            section (str): a header for the contact section under which to
                apply the other args, since there can be more than one.
            kwargs (dict): key-value pairs for any other properties listed in
                the contact section of the core MCF schema.

        """

        if organization:
            self.mcf['contact'][section]['organization'] = organization
        if individualname:
            self.mcf['contact'][section]['individualname'] = individualname
        if positionname:
            self.mcf['contact'][section]['positionname'] = positionname
        if email:
            self.mcf['contact'][section]['email'] = email
        if kwargs:
            for k, v in kwargs.items():
                self.mcf['contact'][section][k] = v

        # TODO: validate just the contact section instead?
        # Not obvious how to do that using the complete schema.
        self.validate()

    def get_contact(self, section='default'):
        """Get metadata from a contact section.

        Args:
            section (str): a header for the contact section under which to
                    apply the other args, since there can be more than one.
        Returns:
            A dict or ``None`` if ``section`` does not exist.

        """
        return self.mcf['contact'].get(section)

    def set_edition(self, edition):
        """Set the edition for the dataset.

        Args:
            edition (str): version of the cited resource

        """
        self.mcf['identification']['edition'] = edition
        self.validate()

    def get_edition(self):
        """Get the edition of the dataset.

        Returns:
            str or ``None`` if ``edition`` does not exist.

        """
        return self.mcf['identification'].get('edition')

    def set_keywords(self, keywords, section='default', keywords_type='theme',
                     vocabulary=None):
        """Describe a dataset with a list of keywords.

        Keywords are grouped into sections for the purpose of complying with
        pre-exising keyword schema. A section will be overwritten if it
        already exists.

        Args:
            keywords (list): sequence of strings
            section (string): the name of a keywords section
            keywords_type (string): subject matter used to group similar
                keywords. Must be one of,
                ('discipline', 'place', 'stratum', 'temporal', 'theme')
            vocabulary (dict): a dictionary with 'name' and 'url' (optional)
                keys. Used to describe the source (thesaurus) of keywords

        Raises:
            ValidationError

        """
        section_dict = {
            'keywords': keywords,
            'keywords_type': keywords_type
        }

        if vocabulary:
            section_dict['vocabulary'] = vocabulary
        self.mcf['identification']['keywords'][section] = section_dict
        self.validate()

    def set_license(self, license_name=None, license_url=None):
        """Add a license for the dataset.

        Args:
            license (str): name of the license of the source dataset

        """
        # One may wish to set these fields to empty strings
        if license_name is None and license_url is None:
            raise ValueError(
                'either `license_name` or `license_url` is required.')

        constraints = ''
        if license_name or license_url:
            constraints = 'license'

        license_dict = {}
        license_dict['name'] = license_name if license_name else ''
        license_dict['url'] = license_url if license_url else ''
        self.mcf['identification']['license'] = license_dict
        self.mcf['identification']['accessconstraints'] = constraints
        self.validate()

    def get_license(self):
        """Get ``license`` for the dataset.

        Returns:
            dict or ``None`` if ``license`` does not exist.

        """
        return self.mcf['identification'].get('license')

    def set_lineage(self, statement):
        """Set the lineage statement for the dataset.

        Args:
            statement (str): general explanation describing the lineage or provenance
                of the dataset

        """
        self.mcf['dataquality']['lineage']['statement'] = statement
        self.validate()

    def get_lineage(self):
        """Get the lineage statement of the dataset.

        Returns:
            str or ``None`` if ``lineage`` does not exist.

        """
        return self.mcf['dataquality']['lineage'].get('statement')

    def set_purpose(self, purpose):
        """Add a purpose for the dataset.

        Args:
            purpose (str): description of the purpose of the source dataset

        """
        # 'Purpose' is not supported in the core MCF spec, probably because
        # `<gmd:purpose>` was added to ISO-19115 in 2014, and MCF still only
        # supports 2015. For now, we can add `purpose` in `identification`.
        # Later we can move it elsewhere if it becomes formally supported.
        self.mcf['identification']['purpose'] = purpose
        self.validate()

    def get_purpose(self):
        """Get ``purpose`` for the dataset.

        Returns:
            str or ``None`` if ``purpose`` does not exist.

        """
        return self.mcf['identification'].get('purpose')

    def set_band_description(self, band_number, name=None, title=None, abstract=None,
                             units=None):
        """Define metadata for a raster band.

        Args:
            band_number (int): a raster band index, starting at 1
            name (str): name for the raster band
            title (str): title for the raster band
            abstract (str): description of the raster band
            units (str): unit of measurement for the band's pixel values
        """
        idx = band_number - 1
        attribute = self.mcf['content_info']['attributes'][idx]
        if name is not None:
            attribute['name'] = name
        if title is not None:
            attribute['title'] = title
        if abstract is not None:
            attribute['abstract'] = abstract
        if units is not None:
            attribute['units'] = units

        self.mcf['content_info']['attributes'][idx] = attribute

    def set_field_description(self, name, title=None, abstract=None,
                              units=None):
        """Define metadata for a tabular field.

        Args:
            name (str): name and unique identifier of the field
            title (str): title for the field
            abstract (str): description of the field
            units (str): unit of measurement for the field's values
        """
        def get_attr(attribute_list):
            for idx, attr in enumerate(attribute_list):
                if attr['name'] == name:
                    return idx, attr
            raise ValueError(
                f'{self.datasource} has no attribute named {name}')

        idx, attribute = get_attr(self.mcf['content_info']['attributes'])

        if title is not None:
            attribute['title'] = title
        if abstract is not None:
            attribute['abstract'] = abstract
        if units is not None:
            attribute['units'] = units

        self.mcf['content_info']['attributes'][idx] = attribute

    def _write_mcf(self, target_path):
        with open(target_path, 'w') as file:
            file.write(yaml.dump(self.mcf, Dumper=_NoAliasDumper))

    def write(self):
        """Write MCF and ISO-19139 XML to disk.

        This creates sidecar files with '.yml' and '.xml' extensions
        appended to the full filename of the data source. For example,

        - 'myraster.tif'
        - 'myraster.tif.yml'
        - 'myraster.tif.xml'

        """
        self._write_mcf(self.mcf_path)
        # TODO: allow user to override the iso schema choice
        # iso_schema = ISO19139_2OutputSchema() # additional req'd properties
        iso_schema = ISO19139OutputSchema()
        xml_string = iso_schema.write(self.mcf)
        with open(f'{self.datasource}.xml', 'w') as xmlfile:
            xmlfile.write(xml_string)

    def validate(self):
        """Validate MCF against a jsonschema object."""
        # validate against our own schema, which could
        # be a superset of the core MCF schema.
        # If we wanted to validate against core MCF,
        # we could use pygeometa.core.validate_mcf
        jsonschema.validate(self.mcf, MCF_SCHEMA)

    def to_string(self):
        pass

    def _set_spatial_info(self):
        """Populate the MCF using properties of the dataset."""
        try:
            gis_type = pygeoprocessing.get_gis_type(self.datasource)
        except ValueError:
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
                try:
                    attribute['type'] = OGR_MCF_ATTR_TYPE_MAP[field.type]
                except KeyError:
                    LOGGER.warning(
                        f'{field.type} is missing in the OGR-to-MCF '
                        f'attribute type map; attribute type for field '
                        f'{field.name} will be "object".')
                attribute['units'] = ''
                attribute['title'] = ''
                attribute['abstract'] = ''
                attributes.append(attribute)
            if len(attributes):
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
                attribute['name'] = ''
                attribute['type'] = 'integer' if band.DataType < 6 else 'number'
                attribute['units'] = ''
                attribute['title'] = ''
                attribute['abstract'] = band.GetDescription()
                attributes.append(attribute)
            if len(attributes):
                self.mcf['content_info']['attributes'] = attributes
            raster = None

            gis_info = pygeoprocessing.get_raster_info(self.datasource)

        srs = osr.SpatialReference()
        srs.ImportFromWkt(gis_info['projection_wkt'])
        epsg = srs.GetAttrValue('AUTHORITY', 1)
        # for human-readable values after yaml dump, use python types
        # instead of numpy types
        bbox = [float(x) for x in gis_info['bounding_box']]
        spatial_info = [{
            'bbox': bbox,
            'crs': epsg  # MCF does not support WKT here
        }]
        self.mcf['identification']['extents']['spatial'] = spatial_info
