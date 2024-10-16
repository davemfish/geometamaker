import os
import shutil
import tempfile
import unittest

from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError
import numpy
from osgeo import gdal
from osgeo import gdal_array
from osgeo import ogr
from osgeo import osr
from pygeometa.core import MCFValidationError
import pygeoprocessing
from pygeoprocessing.geoprocessing_core import DEFAULT_GTIFF_CREATION_TUPLE_OPTIONS
import shapely
import yaml

REGRESSION_DATA = os.path.join(
    os.path.dirname(__file__), 'data')

# This is the complete list of types, but some are
# exceedingly rare and do not match easily to python types
# so I'm not sure if we need to support them all.
# _VALID_OGR_TYPES = (
#     set([getattr(ogr, x) for x in dir(ogr) if 'OFT' in x]))

_OGR_TYPES_VALUES_MAP = {
    ogr.OFTInteger: 0,
    ogr.OFTInteger64: 0,
    ogr.OFTReal: 0.0,
    ogr.OFTString: ''
}


def create_vector(target_filepath, field_map=None):
    attribute_list = None
    if field_map:
        attribute_list = [{
            k: _OGR_TYPES_VALUES_MAP[v]
            for k, v in field_map.items()
        }]
    projection = osr.SpatialReference()
    projection.ImportFromEPSG(3116)
    pygeoprocessing.shapely_geometry_to_vector(
        [shapely.geometry.Point(1, -1)],
        target_filepath,
        projection.ExportToWkt(),
        'GEOJSON',
        fields=field_map,
        attribute_list=attribute_list,
        ogr_geom_type=ogr.wkbPoint)


def create_raster(
        numpy_dtype, target_path,
        pixel_size=(1, 1), projection_epsg=4326,
        origin=(0, 0)):
    driver_name, creation_options = DEFAULT_GTIFF_CREATION_TUPLE_OPTIONS
    raster_driver = gdal.GetDriverByName(driver_name)
    ny, nx = (2, 2)
    n_bands = 2
    gdal_type = gdal_array.NumericTypeCodeToGDALTypeCode(numpy_dtype)
    new_raster = raster_driver.Create(
        target_path, nx, ny, n_bands, gdal_type)
    new_raster.SetGeoTransform(
        [origin[0], pixel_size[0], 0, origin[1], 0, pixel_size[1]])

    projection = osr.SpatialReference()
    projection_wkt = None
    if projection_epsg is not None:
        projection.ImportFromEPSG(projection_epsg)
        projection_wkt = projection.ExportToWkt()
    if projection_wkt is not None:
        new_raster.SetProjection(projection_wkt)

    base_array = numpy.full((2, 2), 1, dtype=numpy_dtype)
    target_nodata = pygeoprocessing.choose_nodata(numpy_dtype)

    band_1 = new_raster.GetRasterBand(1)
    band_1.SetNoDataValue(target_nodata)
    band_1.WriteArray(base_array)
    band_1 = None
    band_2 = new_raster.GetRasterBand(2)
    band_2.SetNoDataValue(target_nodata)
    band_2.WriteArray(base_array)
    band_2 = None
    new_raster = None


class MetadataControlTests(unittest.TestCase):
    """Tests for geometamaker."""

    def setUp(self):
        """Override setUp function to create temp workspace directory."""
        self.workspace_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Override tearDown function to remove temporary directory."""
        shutil.rmtree(self.workspace_dir)

    def test_blank_MetadataControl(self):
        """MetadataControl: template has expected properties."""
        from geometamaker import MetadataControl

        target_filepath = os.path.join(self.workspace_dir, 'mcf.yml')

        mc = MetadataControl()
        mc.validate()
        mc._write_mcf(target_filepath)

        with open(target_filepath, 'r') as file:
            actual = yaml.safe_load(file)
        with open(os.path.join(REGRESSION_DATA, 'template.yml'), 'r') as file:
            expected = yaml.safe_load(file)

        self.assertEqual(actual, expected)

    def test_vector_MetadataControl(self):
        """MetadataControl: validate basic vector MetadataControl."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'vector.geojson')
        field_map = {
            f'field_{k}': k
            for k in _OGR_TYPES_VALUES_MAP}
        create_vector(datasource_path, field_map)

        mc = MetadataControl(datasource_path)
        try:
            mc.validate()
        except (MCFValidationError, SchemaError) as e:
            self.fail(
                'unexpected validation error occurred\n'
                f'{e}')
        mc.write()

    def test_vector_no_fields(self):
        """MetadataControl: validate MetadataControl for basic vector with no fields."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'vector.geojson')
        create_vector(datasource_path, None)

        mc = MetadataControl(datasource_path)
        try:
            mc.validate()
        except (MCFValidationError, SchemaError) as e:
            self.fail(
                'unexpected validation error occurred\n'
                f'{e}')
        mc.write()

    def test_raster_MetadataControl(self):
        """MetadataControl: validate basic raster MetadataControl."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)

        mc = MetadataControl(datasource_path)
        try:
            mc.validate()
        except (MCFValidationError, SchemaError) as e:
            self.fail(
                'unexpected validation error occurred\n'
                f'{e}')
        mc.write()

    def test_vector_attributes(self):
        """MetadataControl: validate vector with extra attribute metadata."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'vector.geojson')
        field_name = 'foo'
        field_map = {
            field_name: list(_OGR_TYPES_VALUES_MAP)[0]}
        create_vector(datasource_path, field_map)

        mc = MetadataControl(datasource_path)
        title = 'title'
        abstract = 'some abstract'
        units = 'mm'
        mc.set_field_description(
            field_name,
            title=title,
            abstract=abstract)
        # To demonstrate that properties can be added while preserving others
        mc.set_field_description(
            field_name,
            units=units)
        try:
            mc.validate()
        except (MCFValidationError, SchemaError) as e:
            self.fail(
                'unexpected validation error occurred\n'
                f'{e}')

        self.assertEqual(
            len(mc.mcf['content_info']['attributes']),
            len(field_map))
        attr = [attr for attr in mc.mcf['content_info']['attributes']
                if attr['name'] == field_name][0]
        self.assertEqual(attr['title'], title)
        self.assertEqual(attr['abstract'], abstract)
        self.assertEqual(attr['units'], units)

    def test_raster_attributes(self):
        """MetadataControl: validate raster with extra attribute metadata."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        band_number = 1

        mc = MetadataControl(datasource_path)
        name = 'name'
        title = 'title'
        abstract = 'some abstract'
        units = 'mm'
        mc.set_band_description(
            band_number,
            name=name,
            title=title,
            abstract=abstract)
        # To demonstrate that properties can be added while preserving others
        mc.set_band_description(
            band_number,
            units=units)
        try:
            mc.validate()
        except (MCFValidationError, SchemaError) as e:
            self.fail(
                'unexpected validation error occurred\n'
                f'{e}')

        self.assertEqual(
            len(mc.mcf['content_info']['attributes']),
            pygeoprocessing.get_raster_info(datasource_path)['n_bands'])
        attr = mc.mcf['content_info']['attributes'][band_number - 1]
        self.assertEqual(attr['name'], name)
        self.assertEqual(attr['title'], title)
        self.assertEqual(attr['abstract'], abstract)
        self.assertEqual(attr['units'], units)

    def test_set_contact(self):
        """MetadataControl: set and get a contact section."""

        from geometamaker import MetadataControl

        org = 'natcap'
        name = 'nat'
        position = 'boss'
        email = 'abc@def'
        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        mc.set_contact(
            organization=org, individualname=name,
            positionname=position, email=email)
        contact_dict = mc.get_contact()
        self.assertEqual(contact_dict['organization'], org)
        self.assertEqual(contact_dict['individualname'], name)
        self.assertEqual(contact_dict['positionname'], position)
        self.assertEqual(contact_dict['email'], email)

    def test_set_contact_from_dict(self):
        """MetadataControl: set a contact section from a dict."""

        from geometamaker import MetadataControl

        contact_dict = {
            'organization': 'natcap',
            'individualname': 'nat',
            'positionname': 'boss',
            'email': 'abc@def',
            'fax': '555-1234',
            'postalcode': '01234'
        }

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        mc.set_contact(**contact_dict)
        actual = mc.get_contact()
        for k, v in contact_dict.items():
            self.assertEqual(actual[k], v)

    def test_set_contact_validates(self):
        """MetadataControl: invalid type raises ValidationError."""

        from geometamaker import MetadataControl

        postalcode = 55555  # should be a string
        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        with self.assertRaises(ValidationError):
            mc.set_contact(postalcode=postalcode)

    def test_set_get_edition(self):
        """MetadataControl: set and get dataset edition."""

        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        version = '3.14'
        mc.set_edition(version)
        self.assertEqual(mc.get_edition(), version)

    def test_set_edition_validates(self):
        """MetadataControl: test set edition raises ValidationError."""

        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        version = 3.14  # should be a string
        with self.assertRaises(ValidationError):
            mc.set_edition(version)

    def test_set_keywords(self):
        """MetadataControl: set keywords to default section."""

        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        mc.set_keywords(['foo', 'bar'])

        self.assertEqual(
            mc.mcf['identification']['keywords']['default']['keywords'],
            ['foo', 'bar'])

    def test_set_keywords_to_section(self):
        """MetadataControl: set keywords to named section."""

        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        mc.set_keywords(['foo', 'bar'], section='first')
        mc.set_keywords(['baz'], section='second')

        self.assertEqual(
            mc.mcf['identification']['keywords']['first']['keywords'],
            ['foo', 'bar'])
        self.assertEqual(
            mc.mcf['identification']['keywords']['second']['keywords'],
            ['baz'])

    def test_overwrite_keywords(self):
        """MetadataControl: overwrite keywords in existing section."""

        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        mc.set_keywords(['foo', 'bar'])
        mc.set_keywords(['baz'])

        self.assertEqual(
            mc.mcf['identification']['keywords']['default']['keywords'],
            ['baz'])

    def test_keywords_raises_validation_error(self):
        """MetadataControl: set keywords validates."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        with self.assertRaises(ValidationError):
            mc.set_keywords('foo', 'bar')

    def test_set_and_get_license(self):
        """MetadataControl: set purpose of dataset."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        name = 'CC-BY-4.0'
        url = 'https://creativecommons.org/licenses/by/4.0/'
        mc.set_license(license_name=name)
        self.assertEqual(mc.get_license(), {'name': name, 'url': ''})
        mc.set_license(license_url=url)
        self.assertEqual(mc.get_license(), {'name': '', 'url': url})
        mc.set_license(license_name=name, license_url=url)
        self.assertEqual(mc.get_license(), {'name': name, 'url': url})

    def test_set_license_validates(self):
        """MetadataControl: test set license raises ValidationError."""

        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        name = 4.0  # should be a string
        with self.assertRaises(ValidationError):
            mc.set_license(license_name=name)
        with self.assertRaises(ValidationError):
            mc.set_license(license_url=name)

    def test_set_and_get_lineage(self):
        """MetadataControl: set lineage of dataset."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        statement = 'a lineage statment'

        mc.set_lineage(statement)
        self.assertEqual(mc.get_lineage(), statement)

    def test_set_lineage_validates(self):
        """MetadataControl: test set lineage raises ValidationError."""

        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        lineage = ['some statement']  # should be a string
        with self.assertRaises(ValidationError):
            mc.set_lineage(lineage)

    def test_set_and_get_purpose(self):
        """MetadataControl: set purpose of dataset."""
        from geometamaker import MetadataControl

        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        purpose = 'foo'
        mc.set_purpose(purpose)
        self.assertEqual(mc.get_purpose(), purpose)

    def test_preexisting_mc(self):
        """MetadataControl: test reading and ammending an existing MetadataControl."""
        from geometamaker import MetadataControl

        title = 'Title'
        keyword = 'foo'
        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        mc.set_title(title)
        mc.write()

        new_mc = MetadataControl(datasource_path)
        new_mc.set_keywords([keyword])

        self.assertEqual(
            new_mc.mcf['identification']['title'], title)
        self.assertEqual(
            new_mc.mcf['identification']['keywords']['default']['keywords'],
            [keyword])

    def test_invalid_preexisting_mcf(self):
        """MetadataControl: test overwriting an existing invalid MetadataControl."""
        from geometamaker import MetadataControl
        title = 'Title'
        datasource_path = os.path.join(self.workspace_dir, 'raster.tif')
        create_raster(numpy.int16, datasource_path)
        mc = MetadataControl(datasource_path)
        mc.set_title(title)

        # delete a required property and ensure invalid MetadataControl
        del mc.mcf['mcf']
        with self.assertRaises(ValidationError):
            mc.validate()
        mc.write()  # intentionally writing an invalid MetadataControl

        new_mc = MetadataControl(datasource_path)

        # The new MetadataControl should not have values from the invalid MetadataControl
        self.assertEqual(
            new_mc.mcf['identification']['title'], '')

        try:
            new_mc.validate()
        except (MCFValidationError, SchemaError) as e:
            self.fail(
                'unexpected validation error occurred\n'
                f'{e}')
        try:
            new_mc.write()
        except Exception as e:
            self.fail(
                'unexpected write error occurred\n'
                f'{e}')
