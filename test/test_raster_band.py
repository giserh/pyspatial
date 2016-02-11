import os
#Scipy stack
import numpy as np

#Spatial
from pyspatial.raster import rasterize
from pyspatial.raster import RasterBand
from pyspatial.vector import read_layer


class TestRasterBand:
    @classmethod
    def setup_class(cls):

        base = os.path.abspath(os.path.dirname(__file__))
        f = lambda x: os.path.join(base, x)
        # Let's add some CLUs for Washington County, IL

        vl, _ = read_layer(f("data/vector/clu/clu_public_a_il189.shp"),
                           index="uniqueid")

        # For speed, let's just use one shape.
        one_shape_id = "3850036893056913813r"
        cls.vl_one_shape = vl[[one_shape_id]]

        # Create a RasterBand for the raster. Raster data is stored and read from there.
        cls.rb = RasterBand(f("data/raster/95000_45000.tif"))

    @classmethod
    def teardown_class(cls):
        pass

    def get_counts_for_shape(self, shp):
        mask = rasterize(shp, ext_outline=0, int_outline=1).T
        minx, miny, maxx, maxy = shp.bounds
        pts = (np.argwhere(mask > 0) + np.array([minx, miny])).astype(int)
        return np.bincount(self.rb[pts[:, 1], pts[:, 0]], minlength=256)

    # Test that RasterBand internally converts coordinates to raster space,
    # so that the caller shouldn't need to do the conversion.
    def test_raster_band_should_convert_coordinates_single_shape(self):
        # Read in raster projection (applies to all raster tiles).
        rast_proj = self.rb.proj

        # Transform all vector shapes into raster projection, and convert to
        # pixels.
        vl_transformed = self.vl_one_shape.transform(rast_proj, inplace=False)
        px_shps_transformed = self.rb.to_pixels(vl_transformed)

        # Also convert CLUs to pixels without transforming into raster
        # projection.
        px_shps_not_transformed = self.rb.to_pixels(self.vl_one_shape)

        # Compute counts for a single shape using vl_transformed for reference.
        shp = px_shps_transformed[0]
        counts_transformed = self.get_counts_for_shape(shp)

        # Now, compute counts for a single shape using vl (which has not been
        # transformed), and compare against vl_transformed computation.
        shp = px_shps_not_transformed[0]
        counts_not_transformed = self.get_counts_for_shape(shp)
        assert(np.array_equal(counts_transformed, counts_not_transformed))