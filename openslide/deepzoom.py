#
# openslide-python - Python bindings for the OpenSlide library
#
# Copyright (c) 2010-2011 Carnegie Mellon University
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of version 2.1 of the GNU Lesser General Public License
# as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public
# License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

"""Support for Deep Zoom images.

This module provides functionality for generating Deep Zoom images from
OpenSlide objects.
"""

from __future__ import division
import cStringIO as StringIO
import math
import openslide
from PIL import Image
from xml.etree.ElementTree import ElementTree, Element, SubElement

class DeepZoomGenerator(object):
    """Generates Deep Zoom tiles and metadata."""

    def __init__(self, osr, tile_size=256, overlap=1):
        """Create a DeepZoomGenerator wrapping an OpenSlide object.

        osr:       a slide object.
        tile_size: the width and height of a single tile.
        overlap:   the number of extra pixels to add to each interior edge
                   of a tile."""

        # We have four coordinate planes:
        # - Row and column of the tile within the Deep Zoom level (t_)
        # - Pixel coordinates within the Deep Zoom level (z_)
        # - Pixel coordinates within the slide layer (l_)
        # - Pixel coordinates within slide layer 0 (l0_)

        self._osr = osr
        self._z_t_downsample = tile_size
        self._z_overlap = overlap

        # Precompute dimensions
        # Layer
        self._l_dimensions = osr.layer_dimensions
        self._l0_dimensions = self._l_dimensions[0]
        # Level
        z_size = self._l0_dimensions
        z_dimensions = [z_size]
        while z_size[0] > 1 or z_size[1] > 1:
            z_size = tuple(max(1, int(math.ceil(z / 2))) for z in z_size)
            z_dimensions.append(z_size)
        self._z_dimensions = tuple(reversed(z_dimensions))
        # Tile
        tiles = lambda z_lim: int(math.ceil(z_lim / self._z_t_downsample))
        self._t_dimensions = tuple((tiles(z_w), tiles(z_h))
                    for z_w, z_h in self._z_dimensions)

        # Level count
        self._levels = len(self._z_dimensions)

        # Total downsamples for each level
        l0_z_downsamples = tuple(2 ** (self._levels - level - 1)
                    for level in xrange(self._levels))

        # Preferred layers for each level
        self._layer_from_level = tuple(
                    self._osr.get_best_layer_for_downsample(d)
                    for d in l0_z_downsamples)

        # Piecewise downsamples
        self._l0_l_downsamples = self._osr.layer_downsamples
        self._l_z_downsamples = tuple(
                    l0_z_downsamples[level] /
                    self._l0_l_downsamples[self._layer_from_level[level]]
                    for level in range(self._levels))

        # Slide background color
        self._bg_color = '#' + self._osr.properties.get(
                        openslide.PROPERTY_NAME_BACKGROUND_COLOR, 'ffffff')

    @property
    def level_count(self):
        """The number of Deep Zoom levels in the image."""
        return self._levels

    @property
    def level_tiles(self):
        """A list of (tiles_x, tiles_y) tuples for each Deep Zoom level."""
        return self._t_dimensions

    @property
    def level_dimensions(self):
        """A list of (pixels_x, pixels_y) tuples for each Deep Zoom level."""
        return self._z_dimensions

    @property
    def tile_count(self):
        """The total number of Deep Zoom tiles in the image."""
        return sum(t_cols * t_rows for t_cols, t_rows in self._t_dimensions)

    def get_tile(self, level, address):
        """Return an RGB PIL.Image for a tile.

        level:     the Deep Zoom level.
        address:   the address of the tile within the level as a (col, row)
                   tuple."""

        # Read tile
        args, z_size = self._get_tile_info(level, address)
        tile = self._osr.read_region(*args)

        # Apply on solid background
        bg = Image.new('RGB', tile.size, self._bg_color)
        tile = Image.composite(tile, bg, tile)

        # Scale to the correct size
        if tile.size != z_size:
            tile.thumbnail(z_size, Image.ANTIALIAS)

        return tile

    def _get_tile_info(self, level, t_location):
        # Check parameters
        if level < 0 or level >= self._levels:
            raise ValueError("Invalid level")
        for t, t_lim in zip(t_location, self._t_dimensions[level]):
            if t < 0 or t >= t_lim:
                raise ValueError("Invalid address")

        # Get preferred layer
        layer = self._layer_from_level[level]

        # Calculate top/left and bottom/right overlap
        z_overlap_tl = tuple(self._z_overlap * int(t != 0)
                    for t in t_location)
        z_overlap_br = tuple(self._z_overlap * int(t != t_lim - 1)
                    for t, t_lim in zip(t_location, self.level_tiles[level]))

        # Get final size of the tile
        z_size = tuple(min(self._z_t_downsample,
                    z_lim - self._z_t_downsample * t) + z_tl + z_br
                    for t, z_lim, z_tl, z_br in
                    zip(t_location, self._z_dimensions[level], z_overlap_tl,
                    z_overlap_br))

        # Obtain the region coordinates
        z_location = [self._z_from_t(t) for t in t_location]
        l_location = [self._l_from_z(level, z) - z_tl
                    for z, z_tl in zip(z_location, z_overlap_tl)]
        # Round location down and size up
        l0_location = [int(self._l0_from_l(layer, l)) for l in l_location]
        l_size = [int(min(math.ceil(self._l_from_z(level, dz)),
                    l_lim - math.ceil(l)))
                    for l, dz, l_lim in
                    zip(l_location, z_size, self._l_dimensions[layer])]

        # Return read_region() parameters plus tile size for final scaling
        return ((l0_location, layer, l_size), z_size)

    def _l0_from_l(self, layer, l):
        return self._l0_l_downsamples[layer] * l

    def _l_from_z(self, level, z):
        return self._l_z_downsamples[level] * z

    def _z_from_t(self, t):
        return self._z_t_downsample * t

    def get_dzi(self, format):
        """Return a string containing the XML metadata for the .dzi file.

        format:    the format of the individual tiles ('png' or 'jpeg')"""
        image = Element('Image', TileSize=str(self._z_t_downsample),
                        Overlap=str(self._z_overlap), Format=format,
                        xmlns='http://schemas.microsoft.com/deepzoom/2008')
        w, h = self._l0_dimensions
        SubElement(image, 'Size', Width=str(w), Height=str(h))
        tree = ElementTree(element=image)
        buf = StringIO.StringIO()
        tree.write(buf, encoding='UTF-8')
        return unicode(buf.getvalue(), 'UTF-8')