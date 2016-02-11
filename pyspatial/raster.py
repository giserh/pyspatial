import json
import os
from smart_open import ParseUri
from boto import connect_s3
import re
from uuid import uuid4
from collections import defaultdict

#Scipy
import numpy as np
from skimage.transform import downscale_local_mean

#Geo
from osgeo import gdal, osr
from osgeo.gdalconst import GA_ReadOnly
from osgeo.osr import SpatialReference
from shapely import wkb, ops
from shapely.affinity import scale
from shapely.geometry import box

from PIL import Image, ImageDraw
import smart_open

from pyspatial import spatiallib as slib
from pyspatial.vector import read_geojson, to_geometry


NP2GDAL_CONVERSION = {
  "uint8": 1,
  "uint16": 2,
  "int16": 3,
  "uint32": 4,
  "int32": 5,
  "float32": 6,
  "float64": 7,
  "complex64": 10,
  "complex128": 11,
}

GDAL2NP_CONVERSION = {v: k for k, v in NP2GDAL_CONVERSION.iteritems()}


def get_path(path):
    uri = ParseUri(path)

    if uri.scheme == "file":
        path = uri.uri_path if os.path.exists(uri.uri_path) else None

    elif uri.scale == "s3":
        conn = connect_s3()
        bucket = conn.get_bucket(uri.bucket_id)
        key = bucket.lookup(uri.key_id)
        path = "/vsicurl/"+key.generate_url(60*60) if key is not None else key

    return path


def rasterize(shp, ext_outline=False, ext_fill=True, int_outline=False,
              int_fill=False, scale_factor=4):

    """Convert a vector shape to a raster. Assumes the shape has already
    been transformed in to a pixel based coordinate system. The algorithm
    checks for the intersection of each point in the shape with
    a pixel grid created by the bounds of the shape. Partial overlaps
    are estimated by scaling the image in X and Y by the scale factor,
    rasterizing the shape, and downscaling (using mean), back to the
    bounds of the original shape.

    Parameters
    ----------

    shp: shapely.Polygon or Multipolygon
        The shape to rasterize

    ext_outline: boolean (default False)
        Include the outline of the shape in the raster

    ext_fill: boolean (default True)
        Fill the shape in the raster

    int_outline: booelan (default False)
        Include the outline of the interior shapes

    int_fill: boolean (default False):
        Fill the interior shapes

    scale_factor: int (default 4)
        The amount to scale the shape in X, Y before downscaling. The
        higher this number, the more precise the estimate of the overlap.


    Returns
    -------
    np.ndarray representing the rasterized shape.
    """
    sf = scale_factor

    minx, miny, maxx, maxy = map(int, shp.bounds)
    if minx == maxx and miny == maxy:
        return np.array([[1.]])

    elif maxy > miny and minx == maxx:
        n = maxy - miny + 1
        return np.zeros([n, 1]) + 1./n

    elif maxy == miny and minx < maxx:
        n = maxx - minx + 1
        return np.zeros([1, n]) + 1./n

    if ((maxx - minx + 1) + (maxy - miny + 1)) <= 2*sf:
        sf = 1.0

    shp = scale(shp, xfact=sf, yfact=sf)
    minx, miny, maxx, maxy = shp.bounds
    width = int(maxx - minx + 1)
    height = int(maxy - miny + 1)

    img = Image.new('L', (width, height), 0)
    _shp = shp.geoms if hasattr(shp, "geoms") else [shp]

    ext_outline = int(ext_outline)
    ext_fill = int(ext_fill)
    int_outline = int(int_outline)
    int_fill = int(int_fill)

    for pg in _shp:
        ext_pg = [(x-minx, y-miny) for x, y in pg.exterior.coords]
        ImageDraw.Draw(img).polygon(ext_pg, outline=ext_outline, fill=ext_fill)
        for s in pg.interiors:
            int_pg = [(x-minx, y-miny) for x, y in s.coords]
            ImageDraw.Draw(img).polygon(int_pg, outline=int_outline,
                                        fill=int_fill)

    return downscale_local_mean(np.array(img), (sf, sf))


class RasterBase(object):
    """
    Stores a coordinate system for a raster.

    Provides methods and attributes common to both RasterBand and
    RasterDataset, particularly for converting shapes to pixels
    in the raster coordinate space.

    Parameters
    ----------
    RasterXSize, RasterYSize: int
        Number of pixels in the width and height respectively.

    geo_transform : list of float
        GDAL coefficients for GeoTransform (defines boundaries and pixel size
        for a raster in lat/lon space).

    proj: osr.SpatialReference
        The spatial projection for the raster.

    Attributes
    ----------
    xsize, ysize: int
        Number of pixels in the width and height respectively.

    geo_transform : list of float
        GDAL coefficients for GeoTransform (defines boundaries and pixel size
        for a raster in lat/lon space).

    min_lon: float
         The minimum longitude in proj coordinates

    min_lat: float
         The minimum latitude in proj coordinates

    max_lat: float
         The maximum latitude in proj coordinates

    lon_px_size: float
         Horizontal size of the pixel

    lat_px_size: float
         Vertical size of the pixel

    proj: osr.SpatialReference
         The spatial projection for the raster.
    """
    def __init__(self, RasterXSize, RasterYSize, geo_transform, proj):
        self.geo_transform = geo_transform
        self.xsize = RasterXSize
        self.ysize = RasterYSize
        self.RasterXSize = self.xsize
        self.RasterYSize = self.ysize
        self.min_lon = self.geo_transform[0]
        self.max_lat = self.geo_transform[3]
        self.min_lat = self.geo_transform[3] + self.geo_transform[5]*self.ysize
        self.lon_px_size = abs(self.geo_transform[1])
        self.lat_px_size = self.geo_transform[5]
        self.proj = proj

    def _to_pixels(self, lon, lat, alt=None):
        """Convert a point from lon/lat to pixel coordinates.  Note,
        the altitude is currently ignored.

        Parameters
        ----------
        lon: float
            Longitude of point

        lat: float
            Latitude of point

        Returns
        -------
        list of int
            (longitude in pixel space, latitude in pixel space).
            Rounded to the nearest pixel.
        """
        lon_px, lat_px = slib.to_pixels(lon, lat, self.min_lon,
                                        self.max_lat, self.lon_px_size,
                                        self.lat_px_size)
        return int(lon_px), int(lat_px)

    def shape_to_pixel(self, feat):
        """Takes a feature and returns a shapely object transformed into the
        pixel coords.

        Parameters
        ----------
        feat : osgeo.ogr.Feature
            Feature to be transformed.

        Returns
        -------
        shapely.Polygon
            Feature in pixel coordinates.
        """
        shp = wkb.loads(feat.geometry().ExportToWkb())
        return ops.transform(self._to_pixels, shp)

    def to_pixels(self, vector_layer):
        """Takes a vector layer and returns list of shapely geometry
        transformed in pixel coordinates. If the projection of the
        vector_layer is different than the raster band projection, it
        transforms the coordinates first to raster projection.

        Parameters
        ----------
        vector_layer : VectorLayer
            Shapes to be transformed.

        Returns
        -------
        list of shapely.Polygon
            Shapes in pixel coordinates.
        """
        if self.proj != vector_layer.proj:
            vector_layer = vector_layer.transform(self.proj)
        return [self.shape_to_pixel(f) for f in vector_layer.features]

    def GetGeoTransform(self):
        """Returns affine transform from GDAL for describing the relationship
        between raster positions (in pixel/line coordinates) and georeferenced
        coordinates.

        Returns
        -------
        min_lon: float
             The minimum longitude in raster coordinates.

        lon_px_size: float
             Horizontal size of each pixel.

        geo_transform[2] : float
            Not used in our case. In general, this would be used if the
            coordinate system had rotation or shearing.

        max_lat: float
             The maximum latitude in raster coordinates.

        lat_px_size: float
             Vertical size of the pixel.

        geo_transform[5] : float
            Not used in our case. In general, this would be used if the
            coordinate system had rotation or shearing.

        References
        ----------
        http://www.gdal.org/gdal_tutorial.html
        """
        return self.geo_transform

    def get_extent(self):
        """Returns extent in raster coordinates.

        Returns
        -------
        xmin : float
            Minimum x-value (lon) of extent in raster coordinates.

        xmax : float
            Maximum x-value (lon) of extent in raster coordinates.

        ymin : float
            Minimum y-value (lat) of extent in raster coordinates.

        ymax : float
            Maximum y-value (lat) of extent in raster coordinates.
        """
        ymax = self.max_lat
        xmin = self.min_lon
        ymin = ymax + self.lat_px_size*self.ysize
        xmax = xmin + self.lon_px_size*self.xsize
        return (xmin, xmax, ymin, ymax)

    def bbox(self):
        """Returns bounding box of raster in raster coordinates.

        Returns
        -------
        shapely.Polygon
            Bounding box in raster coordinates:
                (xmin : float
                    minimum longitude (leftmost)

                 ymin : float
                    minimum latitude (bottom)

                 xmax : float
                    maximum longitude (rightmost)

                 ymax : float
                    maximum latitude (top))
        """
        (xmin, xmax, ymin, ymax) = self.get_extent()
        return to_geometry(box(xmin, ymin, xmax, ymax),
                           proj=self.proj)


class RasterBand(RasterBase, np.ndarray):
    def __new__(cls, ds, band_number=1):
        """
        Create an in-memory representation for a single band in
        a raster. (0,0) in pixel coordinates represents the
        upper left corner of the raster which corresponds to
        (min_lon, max_lat).  Inherits from ndarray, so you can
        use it like a numpy array.

        Parameters
        ----------
        ds: gdal.Dataset

        band_number: int
            The band number to use

        Attributes
        ----------
        data: np.ndarray[xsize, ysize]
             The raster data
        """

        if not isinstance(ds, gdal.Dataset):
            path = get_path(ds)
            ds = gdal.Open(path, GA_ReadOnly)

        band = ds.GetRasterBand(band_number)
        if band is None:
            msg = "Unable to load band %d " % band_number
            msg += "in raster %s" % ds.GetDescription()
            raise ValueError(msg)

        gdal_type = band.DataType
        dtype = np.dtype(GDAL2NP_CONVERSION[gdal_type])

        self = np.asarray(ds.ReadAsArray().astype(dtype)).view(cls)
        proj = SpatialReference()
        proj.ImportFromWkt(ds.GetProjection())
        geo_transform = ds.GetGeoTransform()

        # Initialize the base class with coordinate information.
        RasterBase.__init__(self, ds.RasterXSize, ds.RasterYSize,
                            geo_transform, proj)

        self.nan = band.GetNoDataValue()

        ctable = band.GetColorTable()
        if ctable is not None:
            self.colors = np.array([ctable.GetColorEntry(i)
                                    for i in range(256)],
                                   dtype=np.uint8)
        else:
            self.colors = None

        ds = None
        return self

    def __init__(self, ds, band_number=1):
        pass


class RasterQueryResult:
    """
    Container class to hold the result of a raster query.

    Attributes
    ----------
    id : str or int
        The id of the shape in the vector layer

    values: np.ndarray
        The values of the intersected pixels in the raster

    weights: np.ndarray
        The fraction of the polygon intersecting with the pixel
    """
    def __init__(self, id, values, weights):
        self.id = id
        self.values = values
        self.weights = weights


class RasterDataset(RasterBase):
    """
    Raster representation that supports tiled and untiled datasets, and
    allows querying with a set of shapes.

    All raster file details for initialization are in a json specification
    in dataset_catalog_file. Raster may be tiled or untiled. A RasterDataset
    object may be queried one or multiple times with a set of shapes (in a
    VectorLayer). We also try to match the attribute and method names of
    gdal.Dataset when possible to allow for easy porting of caller code
    to use this class instead of gdal.Dataset, as this class transparently
    works on an untiled gdal.Dataset, in addition to the added functionality
    to handle tiled datasets.

    Parameters
    ----------
    dataset_catalog_file : str
        Path to catalog file for the dataset. May be relative or absolute.
        Catalog files are in json format, and usually represent a type of data
        (e.g. CDL) and a year (e.g. 2014).

    Attributes
    ----------
    path : str
        Path to raster data files.

    color_table : GDALColorTable
        Color table for raster.

    grid_size : int
        Number of pixels in width or height of each grid tile. If set to None,
        that indicates this is an untiled raster.

    raster_arrays : RasterBand, or
                    dict of (list of int): RasterBand
        Dictionary storing raster arrays that have been read from disk.
        If untiled, this is set at initialization to the whole raster. If
        tiled, these are read in lazily as needed. Index is (x_grid, y_grid)
        where x_grid is x coordinate of leftmost pixel in this tile relative
        to minLon (and is a multiple of grid_size), and y_grid is y coordinate
        of uppermost pixel in this tile relative to maxLat (and is also a
        multiple of grid_size). See notes below for more information on how
        data is represented here.

    shapes_in_tiles : dict of (int, int): set of str
        What shapes are left to be processed in each tile. Key is (minx, maxy)
        of tile (upper left corner), and value is set of ids of shapes. This
        is initially set in query(), and shape ids are removed from this data
        structure for a tile once they have been processed. Tiles can be
        cleared from memory when there are no shapes left in their set.

    Methods
    -------
    query(vector_layer) :
        Look up pixel values in the raster for all points in each shape in the
        vector layer.

    See Also
    --------
    test/create_catalog_file.ipynb : How to create a catalog file for a dataset.
    raster_query_test.py : Simple examples of exercising RasterQuery on tiled
        and untiled datasets, and computing stats from results.
    vector.py : Details of VectorLayer.

    Notes
    -----
    Raster representation (tiled and untiled):
    The core functionality of this class is to look up pixel values (for a
    shape or set of shapes) in the raster. To do this, we store the raster in
    a 2D-array of pixels relative to (0,0) being the upper left corner aka
    (min_lon, max_lat) in lon/lat coordinates. We can then convert vector
    shapes into pixel space, and look up their values directly in the raster
    array. For an untiled raster, we can read in the raster directly during
    initialization.

    Tiled representation:
    For a tiled dataset (ie. the data is split into multiple files), we
    still treat the overall raster upper left corner as (0,0), and
    recognize that each tile has a position relative to the overall raster
    pixel array. We store each tile in a 2D array in a dictionary keyed by
    the tile position relative to the overall raster position in pixel space.
    For example, a pixel at (118, 243) in a tiled dataset with grid size = 100
    would be stored in raster_arrays[(100, 200)][18][43]. As a memory
    utilization and performance enhancement, we lazily read tiles from disk
    when they are first needed and store them in raster_arrays{} (for the
    lifetime of the RasterDataset object). If memory turns out to be a
    problem, it might make sense to store these in a LRU cache instead.

    TODO: Added band number
    """

    def __init__(self, path_or_ds, xsize, ysize, geo_transform, proj,
                 color_table=None, grid_size=None, index=None,
                 tile_regex=None):
        ds = None

        if not isinstance(path_or_ds, gdal.Dataset):
            path = path_or_ds
        else:
            ds = path_or_ds
            path = ds.GetDescription()

        self.path = path
        self.proj = proj
        self.raster_arrays = {}
        self.shapes_in_tiles = {}
        self.tile_regex = tile_regex
        self.index = index
        self.grid_size = grid_size
        self.dtype = None

        # Initialize the base class with coordinate information.
        RasterBase.__init__(self, xsize, ysize, geo_transform, proj)

        # Read raster file now if this is an untiled data set.
        if self.grid_size is None:
            if ds is None:
                self.raster_arrays = read_vsimem(self.path)
            else:
                self.raster_arrays = RasterBand(ds)
            self.dtype = self.raster_arrays.dtype

        ds = None
        path_or_ds = None

    def _get_value_for_pixel(self, px):
        """Look up value for a pixel in raster space.

        Parameters
        ----------
        px : np.array
            Pixel coordinates for 1 point: [x_coord, y_coord]

        Returns
        -------
        dtype
            Value in raster at pixel coordinates specified by px.
            Type is determined by GDAL2NP_CONVERSION from RasterBand data
            type.

        """
        # Compute which grid tile to read
        key = self._get_grid_for_pixel(px)
        x_grid, y_grid = key

        # If we haven't already read this grid tile into memory, do so now,
        # and store it in raster_arrays for future queries to access.
        if (x_grid, y_grid) not in self.raster_arrays:
            filename = self.path + "%d_%d.tif" % key
            self.raster_arrays[key] = read_vsimem(filename)
            if self.dtype is None:
                self.dtype = self.raster_arrays[key].dtype

        # Look up the grid tile for this pixel.
        raster = self.raster_arrays[key]

        # Look up the value in the x,y offset in the grid tile we just found
        # or read, and return it.
        x_px = px[0] - x_grid
        y_px = px[1] - y_grid

        return raster[y_px][x_px]

    def _get_grid_for_pixel(self, px):
        """Compute the min_x, min_y of the tile that contains pixel,
        which can also be used for looking up the tile in raster_arrays.

        Parameters
        ----------
        px : np.array
            Pixel coordinates for 1 point: [x_coord, y_coord]

        Returns
        -------
        list of int
            (min_x, min_y) of tile that contains px, in pixel coordinates.

        """
        return slib.grid_for_pixel(self.grid_size, px[0], px[1])

    def get_values_for_pixels(self, pxs):
        """Look up values for a list of pixels in raster space.

        Parameters
        ----------
        pxs : np.array
            Array of pixel coordinates. Each row is [x_coord, y_coord]
            for one point.

        Returns
        -------
        list of dtype
            List of values in raster at pixel coordinates specified in pxs.
            Type is determined by GDAL2NP_CONVERSION from RasterBand data
            type.
        """
        # Untiled case: Use the 1-file raster array we read in at
        # initialization.
        if self.grid_size is None:
            return self.raster_arrays[pxs[:, 1], pxs[:, 0]]
        # Tiled case: Compute the grid tile to read, and the x,y offset in
        # that tile.
        else:
            return np.array([self._get_value_for_pixel(px) for px in pxs], dtype=self.dtype)

    def _key_from_tile_filename(self, filename):
        """Get (x_grid, y_grid) key of upper left corner of tile from filename.

        Parameters
        ----------
        filename : str
            Tile filename. We assume filename is in format
            'arbitrary_path/{x_grid}_{y_grid}'
            e.g. 'data/tiled/2500_2250.tif'
            Path does not matter, but format after last slash is assumed.

        Returns
        -------
        x_grid : int
            minimum value for x for tile in raster pixel coordinates.

        y_grid : int
            minimum value for y for tile in raster pixel coordinates.
        """
        r = self.tile_regex.search(filename)
        if (len(r.groups()) != 2):
            raise ValueError("Tile filenames must end in {x}_{y}.tif")
        x_grid = int(r.group(1))
        y_grid = int(r.group(2))
        return x_grid, y_grid

    def query(self, vector_layer, ext_outline=False, ext_fill=True,
              int_outline=False, int_fill=False, scale_factor=4,
              missing_first=False):
        """
        Query the dataset with a set of shapes (in a VectorLayer). The
        vectors will be reprojected into the projection of the raster. Any
        shapes in the vector layer that are not within the bounds of the
        raster will return as [], np.array([]).

        Parameters
        ----------
        vector_layer : VectorLayer
            Set of shapes in vector format, with ids attached to each.
        ext_outline: boolean (default False)
            Include the outline of the shape in the raster
        ext_fill: boolean (default True)
            Fill the shape in the raster
        int_outline: booelan (default False)
            Include the outline of the interior shapes
        int_fill: boolean (default False):
            Fill the interior shapes
        scale_factor: int (default 4)
            The amount to scale the shape in X, Y before downscaling. The
            higher this number, the more precise the estimate of the overlap.

        Yields
        ------

        RasterQueryResult

        """

        if self.proj.ExportToProj4() != vector_layer.proj.ExportToProj4():
            # Transform all vector shapes into raster projection.
            vl = vector_layer.transform(self.proj)

        # Filter out all shapes outside the raster bounds
        bbox = self.bbox()
        vl = vl.within(bbox)

        ids_to_tiles = None
        tiles_to_ids = None

        # Optimization to minimize memory usage if the RasterDataset contains
        # an index.  This will sort by the upper left corners of all the shapes
        # and process one shape at a time.  It will remove the corresponding
        # entries in self.raster_arrays once all references for shapes in a
        # particular tile have been removed.
        if self.index is not None:
            res = {self._key_from_tile_filename(id): set(vl.intersects(f).ids)
                   for id, f in self.index.iteritems()}

            tiles_to_ids = {k: v for k, v in res.iteritems() if len(v) > 0}
            ids_to_tiles = defaultdict(set)
            for tile, shp_ids in tiles_to_ids.iteritems():
                for id in shp_ids:
                    ids_to_tiles[id].add(tile)

            vl = vl.sort()

        missing = vector_layer.ids.difference(vl.ids)

        if missing_first:
            ids = missing.append(vl.ids)
        else:
            ids = vl.ids.append(missing)

        px_shps = dict(zip(vl.ids, self.to_pixels(vl)))

        for id in ids:
            shp = px_shps.get(id, None)

            if shp is None:
                yield RasterQueryResult(id, [], np.array([]))

            else:
                #Eagerly load tiles
                if ids_to_tiles is not None:
                    for key in list(ids_to_tiles[id]):
                        if key not in self.raster_arrays:
                            filename = self.path + "%d_%d.tif" % key
                            self.raster_arrays[key] = RasterBand(filename)
                        tiles_to_ids[key].remove(id)

                # Rasterize the shape, and find list of all points.
                mask = rasterize(shp, ext_outline=ext_outline,
                                 ext_fill=ext_fill, int_outline=int_outline,
                                 int_fill=int_fill,
                                 scale_factor=scale_factor).T

                minx, miny, maxx, maxy = shp.bounds
                idx = np.argwhere(mask > 0)

                if idx.shape[0] == 0:
                    weights = mask[[0]]

                else:
                    weights = mask[idx[:, 0], idx[:, 1]]

                pts = (idx + np.array([minx, miny])).astype(int)

                values = self.get_values_for_pixels(pts)

                # Look up values for each pixel.
                yield RasterQueryResult(id, values, weights)

            if tiles_to_ids is None:
                continue

            #Remove raster bands that are empty
            empty = [k for k, v in tiles_to_ids.iteritems() if len(v) == 0]
            for e in empty:
                del self.raster_arrays[e]
                del tiles_to_ids[e]


def read_catalog(dataset_catalog_file):
    with open(dataset_catalog_file) as catalog_file:
        decoded = json.load(catalog_file)

    size = map(int, decoded["Size"])
    coordinate_system = str(decoded["CoordinateSystem"])
    transform = decoded["GeoTransform"]

    # Get the projection for the raster
    proj = osr.SpatialReference()
    proj.ImportFromWkt(coordinate_system)

    path = decoded["Path"]
    color_table = decoded.get("ColorTable", None)
    grid_size = decoded.get("GridSize", None)
    index = None
    tile_regex = None

    if "Index" in decoded:
        index, index_df = read_geojson(json.dumps(decoded["Index"]),
                                       index="location")
        index = index.transform(proj)
        tile_regex = re.compile('([0-9]+)_([0-9]+)\.tif')

    return RasterDataset(path, size[0], size[1], transform, proj,
                         color_table=color_table, grid_size=grid_size,
                         index=index, tile_regex=tile_regex)


def read_raster(path, band_number=1):
    """
    Create a raster dataset from a single raster file

    Parameters
    ----------
    path: string
        Path to the raster file.  Can be either local or s3.

    band_number: int
        The band number to use

    Returns
    -------

    RasterDataset
    """

    path = get_path(path)
    ds = gdal.Open(path, GA_ReadOnly)
    xsize = ds.RasterXSize
    ysize = ds.RasterYSize
    proj = SpatialReference()
    proj.ImportFromWkt(ds.GetProjection())
    geo_transform = ds.GetGeoTransform()
    return RasterDataset(ds, xsize, ysize, geo_transform, proj)


def read_band(path, band_number=1):
    """
    Read a single band from a raster into memory.

    Parameters
    ----------
    path: string
        Path to the raster file.  Can be either local or s3.

    band_number: int
        The band number to use

    Returns
    -------

    RasterBand
    """

    path = get_path(path)
    ds = gdal.Open(path, GA_ReadOnly)
    return RasterBand(ds, band_number=band_number)


def read_vsimem(path, band_number=1):
    """
    Read a single band into memory from a raster. This method
    does not support all raster formats, only those that are
    supported by /vsimem

    Parameters
    ----------
    path: string
        Path to the raster file.  Can be either local or s3.

    band_number: int
        The band number to use

    Returns
    -------

    RasterBand
    """
    filename = str(uuid4())
    with smart_open.smart_open(path) as inf:
        gdal.FileFromMemBuffer("/vsimem/%s" % filename,
                               inf.read())

    ds = gdal.Open("/vsimem/%s" % filename)
    gdal.Unlink("/vsimem/%s" % filename)
    return RasterBand(ds, band_number=band_number)