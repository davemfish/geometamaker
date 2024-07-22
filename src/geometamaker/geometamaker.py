import dataclasses
import logging
import os
import uuid
from datetime import datetime

import frictionless
import fsspec
import numpy
from osgeo import gdal
from osgeo import ogr
from osgeo import osr
import pygeoprocessing
import yaml

from . import models


LOGGER = logging.getLogger(__name__)


def detect_file_type(filepath):
    # TODO: zip, or other archives. Can they be represented as a Resource?
    # or do they need to be a Package?

    # TODO: guard against classifying netCDF, HDF5, etc as GDAL rasters,
    # we'll want a different data model for multi-dimensional arrays.

    # GDAL considers CSV a vector, so check against frictionless
    # first.
    desc = frictionless.describe(filepath)
    if desc.type == 'table':
        return 'table'
    if desc.compression:
        return 'archive'
    gis_type = pygeoprocessing.get_gis_type(filepath)
    if gis_type == pygeoprocessing.VECTOR_TYPE:
        return 'vector'
    if gis_type == pygeoprocessing.RASTER_TYPE:
        return 'raster'
    raise ValueError()


def describe_archive(source_dataset_path):
    description = frictionless.describe(
        source_dataset_path, stats=True).to_dict()
    return description


def describe_vector(source_dataset_path):
    description = frictionless.describe(
        source_dataset_path, stats=True).to_dict()
    fields = []
    vector = gdal.OpenEx(source_dataset_path, gdal.OF_VECTOR)
    layer = vector.GetLayer()
    description['rows'] = layer.GetFeatureCount()
    for fld in layer.schema:
        fields.append(
            models.FieldSchema(name=fld.name, type=fld.GetTypeName()))
    vector = layer = None
    description['schema'] = models.TableSchema(fields=fields)
    description['fields'] = len(fields)

    info = pygeoprocessing.get_vector_info(source_dataset_path)
    spatial = {
        'bounding_box': info['bounding_box'],
        'crs': info['projection_wkt']
    }
    description['spatial'] = models.SpatialSchema(**spatial)
    description['sources'] = info['file_list']
    return description


def describe_raster(source_dataset_path):
    description = frictionless.describe(
        source_dataset_path, stats=True).to_dict()

    bands = []
    info = pygeoprocessing.get_raster_info(source_dataset_path)
    # Some values of raster info are numpy types, which the
    # yaml dumper doesn't know how to represent.
    for i in range(info['n_bands']):
        b = i + 1
        bands.append(models.BandSchema(
            index=b,
            gdal_type=info['datatype'],
            numpy_type=numpy.dtype(info['numpy_type']).name,
            nodata=info['nodata'][i]))
    description['schema'] = models.RasterSchema(
        bands=bands,
        pixel_size=info['pixel_size'],
        raster_size=info['raster_size'])
    description['spatial'] = models.SpatialSchema(
        bounding_box=[float(x) for x in info['bounding_box']],
        crs=info['projection_wkt'])
    description['sources'] = info['file_list']
    return description


def describe_table(source_dataset_path):
    description = frictionless.describe(
        source_dataset_path, stats=True).to_dict()
    description['schema'] = models.TableSchema(**description['schema'])
    return description


DESRCIBE_FUNCS = {
    'archive': describe_archive,
    'table': describe_table,
    'vector': describe_vector,
    'raster': describe_raster
}

RESOURCE_MODELS = {
    'archive': models.ArchiveResource,
    'table': models.TableResource,
    'vector': models.VectorResource,
    'raster': models.RasterResource
}


def describe(source_dataset_path):
    """Create a metadata resource instance with properties of the dataset.

    Properties of the dataset are used to populate as many metadata
    properties as possible. Default/placeholder
    values are used for properties that require user input.

    Args:
        source_dataset_path (string): path or URL to dataset to which the
            metadata applies

    Returns
        instance of
            ArchiveResource, TableResource,
            VectorResource, RasterResource
    """

    data_package_path = f'{source_dataset_path}.yml'

    # Despite naming, this does not open a file that must be closed
    of = fsspec.open(source_dataset_path)
    if not of.fs.exists(source_dataset_path):
        raise FileNotFoundError(f'{source_dataset_path} does not exist')

    resource_type = detect_file_type(source_dataset_path)
    description = DESRCIBE_FUNCS[resource_type](source_dataset_path)

    # Load existing metadata file
    try:
        with fsspec.open(data_package_path, 'r') as file:
            yaml_string = file.read()

        existing_resource = RESOURCE_MODELS[resource_type](
            **yaml.safe_load(yaml_string))
        if 'schema' in description:
            if isinstance(description['schema'], models.RasterSchema):
                # If existing band metadata still matches schema of the file
                # carry over metadata from the existing file because it could
                # include human-defined properties.
                new_bands = []
                for band in description['schema'].bands:
                    try:
                        eband = existing_resource.get_band_description(band.index)
                        # TODO: rewrite this as __eq__ of BandSchema?
                        if (band.numpy_type, band.gdal_type, band.nodata) == (
                                eband.numpy_type, eband.gdal_type, eband.nodata):
                            band = dataclasses.replace(band, **eband.__dict__)
                    except IndexError:
                        pass
                    new_bands.append(band)
                description['schema'].bands = new_bands
            if isinstance(description['schema'], models.TableSchema):
                # If existing field metadata still matches schema of the file
                # carry over metadata from the existing file because it could
                # include human-defined properties.
                new_fields = []
                for field in description['schema'].fields:
                    try:
                        efield = existing_resource.get_field_description(
                            field.name)
                        # TODO: rewrite this as __eq__ of FieldSchema?
                        if field.type == efield.type:
                            field = dataclasses.replace(field, **efield.__dict__)
                    except KeyError:
                        pass
                    new_fields.append(field)
                description['schema'].fields = new_fields
        # overwrite properties that are intrinsic to the dataset
        # TODO: any other checks that the resources represent the same data?
        resource = dataclasses.replace(
            existing_resource, **description)

    # Common path: metadata file does not already exist
    except FileNotFoundError as err:
        resource = RESOURCE_MODELS[resource_type](**description)

    return resource

