"""
color.py
-------------

Hold and deal with visual information about meshes.

There are lots of ways to encode visual information, and the goal of this
architecture is to make it possible to define one, and then transparently
get the others. The two general categories are:

1) colors, defined for a face, vertex, or material
2) textures, defined as an image and UV coordinates for each vertex

This module only implements diffuse colors at the moment.

Rules
----------
1) If nothing is defined sane defaults should be returned
2) If a user alters or sets a value, that is considered user data
   and should be saved and treated as such.
3) Only one mode of visual is allowed at a time, and setting or altering
   a value should transparently change the mode. Color modes are:
     * vertex colors
     * face colors
"""

import numpy as np

import copy
import colorsys

from .. import util
from .. import caching
from .. import grouping


class ColorVisuals(object):
    """
    Store color information about a mesh.
    """

    def __init__(self,
                 mesh=None,
                 face_colors=None,
                 vertex_colors=None):
        """
        Store color information about a mesh.

        Parameters
        ----------
        mesh : Trimesh
          Object that these visual properties
          are associated with
        face_ colors :  (n,3|4) or (3,) or (4,) uint8
          Colors per-face
        vertex_colors : (n,3|4) or (3,) or (4,) uint8
          Colors per-vertex
        """
        self.mesh = mesh
        self._data = caching.DataStore()
        self._cache = caching.Cache(id_function=self.crc)

        self.defaults = {
            'material_diffuse': np.array([102, 102, 102, 255],
                                         dtype=np.uint8),
            'material_ambient': np.array([64, 64, 64, 255],
                                         dtype=np.uint8),
            'material_specular': np.array([197, 197, 197, 255],
                                          dtype=np.uint8),
            'material_shine': 77.0}

        if face_colors is not None:
            self.face_colors = face_colors

        if vertex_colors is not None:
            self.vertex_colors = vertex_colors

    @caching.cache_decorator
    def transparency(self):
        """
        Does the current object contain any transparency.

        Returns
        ----------
        transparency: bool, does the current visual contain transparency
        """
        if 'vertex_colors' in self._data:
            a_min = self._data['vertex_colors'][:, 3].min()
        elif 'face_colors' in self._data:
            a_min = self._data['face_colors'][:, 3].min()
        else:
            return False

        return bool(a_min < 255)

    @property
    def defined(self):
        """
        Are any colors defined for the current mesh.

        Returns
        ---------
        defined: bool, are colors defined or not.
        """
        return self.kind is not None

    @property
    def kind(self):
        """
        What color mode has been set.

        Returns
        ----------
        mode: 'face', 'vertex', or None
        """
        self._verify_crc()
        if 'vertex_colors' in self._data:
            mode = 'vertex'
        elif 'face_colors' in self._data:
            mode = 'face'
        else:
            mode = None

        return mode

    def crc(self):
        """
        A checksum for the current visual object and its parent mesh.

        Returns
        ----------
        crc: int, checksum of data in visual object and its parent mesh
        """
        # will make sure everything has been transferred
        # to datastore that needs to be before returning crc

        result = self._data.fast_hash()
        if hasattr(self.mesh, 'crc'):
            # bitwise xor combines hashes better than a sum
            result ^= self.mesh.crc()
        return result

    def copy(self):
        """
        Return a copy of the current ColorVisuals object.

        Returns
        ----------
        copied : ColorVisuals
          Contains the same information as self
        """
        copied = ColorVisuals()
        copied._data.data = copy.deepcopy(self._data.data)
        return copied

    @property
    def face_colors(self):
        """
        Colors defined for each face of a mesh.

        If no colors are defined, defaults are returned.

        Returns
        ----------
        colors: (len(mesh.faces), 4) uint8, RGBA color for each face
        """
        return self._get_colors(name='face')

    @face_colors.setter
    def face_colors(self, values):
        """
        Set the colors for each face of a mesh.

        This will apply these colors and delete any previously specified
        color information.

        Parameters
        ------------
        colors: (len(mesh.faces), 3), set each face to the specified color
                (len(mesh.faces), 4), set each face to the specified color
                (3,) int, set the whole mesh this color
                (4,) int, set the whole mesh this color
        """
        if values is None:
            if 'face_colors' in self._data:
                self._data.data.pop('face_colors')
            return

        colors = to_rgba(values)

        if (self.mesh is not None and
                colors.shape == (4,)):
            count = len(self.mesh.faces)
            colors = np.tile(colors, (count, 1))

        # if we set any color information, clear the others
        self._data.clear()
        self._data['face_colors'] = colors
        self._cache.verify()

    @property
    def vertex_colors(self):
        """
        Return the colors for each vertex of a mesh

        Returns
        ------------
        colors: (len(mesh.vertices), 4) uint8, color for each vertex
        """
        return self._get_colors(name='vertex')

    @vertex_colors.setter
    def vertex_colors(self, values):
        """
        Set the colors for each vertex of a mesh

        This will apply these colors and delete any previously specified
        color information.

        Parameters
        ------------
        colors: (len(mesh.vertices), 3), set each face to the color
                (len(mesh.vertices), 4), set each face to the color
                (3,) int, set the whole mesh this color
                (4,) int, set the whole mesh this color
        """
        if values is None:
            if 'vertex_colors' in self._data:
                self._data.data.pop('vertex_colors')
            return

        # make sure passed values are numpy array
        values = np.asanyarray(values)
        # Ensure the color shape is sane
        if (self.mesh is not None and not
                (values.shape == (len(self.mesh.vertices), 3) or
                 values.shape == (len(self.mesh.vertices), 4) or
                 values.shape == (3,) or
                 values.shape == (4,))):
            return

        colors = to_rgba(values)
        if (self.mesh is not None and
                colors.shape == (4,)):
            count = len(self.mesh.vertices)
            colors = np.tile(colors, (count, 1))

        # if we set any color information, clear the others
        self._data.clear()
        self._data['vertex_colors'] = colors
        self._cache.verify()

    def _get_colors(self,
                    name):
        """
        A magical function which maintains the sanity of vertex and face colors.

        * If colors have been explicitly stored or changed, they are considered
        user data, stored in self._data (DataStore), and are returned immediately
        when requested.
        * If colors have never been set, a (count,4) tiled copy of the default diffuse
        color will be stored in the cache
        ** the CRC on creation for these cached default colors will also be stored
        ** if the cached color array is altered (different CRC than when it was
        created) we consider that now to be user data and the array is moved from
        the cache to the DataStore.

        Parameters
        -----------
        name: str, 'face', or 'vertex'

        Returns
        -----------
        colors: (count, 4) uint8, RGBA colors
        """

        count = None
        try:
            if name == 'face':
                count = len(self.mesh.faces)
            elif name == 'vertex':
                count = len(self.mesh.vertices)
        except BaseException:
            pass

        # the face or vertex colors
        key_colors = str(name) + '_colors'
        # the initial crc of the
        key_crc = key_colors + '_crc'

        if key_colors in self._data:
            # if a user has explicitly stored or changed the color it
            # will be in data
            return self._data[key_colors]

        elif key_colors in self._cache:
            # if the colors have been autogenerated already they
            # will be in the cache
            colors = self._cache[key_colors]
            # if the cached colors have been changed since creation we move
            # them to data
            if colors.crc() != self._cache[key_crc]:
                # call the setter on the property using exec
                # this avoids having to pass a setter to this function
                if name == 'face':
                    self.face_colors = colors
                elif name == 'vertex':
                    self.vertex_colors = colors
                else:
                    raise ValueError('unsupported name!!!')
                self._cache.verify()
        else:
            # colors have never been accessed
            if self.kind is None:
                # no colors are defined, so create a (count, 4) tiled
                # copy of the default color
                colors = np.tile(self.defaults['material_diffuse'],
                                 (count, 1))
            elif (self.kind == 'vertex' and
                  name == 'face'):
                colors = vertex_to_face_color(
                    vertex_colors=self.vertex_colors,
                    faces=self.mesh.faces)
            elif (self.kind == 'face' and
                  name == 'vertex'):
                colors = face_to_vertex_color(
                    mesh=self.mesh,
                    face_colors=self.face_colors)
            else:
                raise ValueError('self.kind not accepted values!!')

        if (count is not None and
                colors.shape != (count, 4)):
            raise ValueError('face colors incorrect shape!')

        # subclass the array to track for changes using a CRC
        colors = caching.tracked_array(colors)
        # put the generated colors and their initial checksum into cache
        self._cache[key_colors] = colors
        self._cache[key_crc] = colors.crc()

        return colors

    def _verify_crc(self):
        """
        Verify the checksums of cached face and vertex color, to verify
        that a user hasn't altered them since they were generated from
        defaults.

        If the colors have been altered since creation, move them into
        the DataStore at self._data since the user action has made them
        user data.
        """
        if not hasattr(self, '_cache') or len(self._cache) == 0:
            return

        for name in ['face', 'vertex']:
            # the face or vertex colors
            key_colors = str(name) + '_colors'
            # the initial crc of the
            key_crc = key_colors + '_crc'

            if key_colors not in self._cache:
                continue

            colors = self._cache[key_colors]
            # if the cached colors have been changed since creation
            # move them to data
            if colors.crc() != self._cache[key_crc]:
                if name == 'face':
                    self.face_colors = colors
                elif name == 'vertex':
                    self.vertex_colors = colors
                else:
                    raise ValueError('unsupported name!!!')
                self._cache.verify()

    def update_vertices(self, mask):
        """
        Apply a mask to remove or duplicate vertex properties.
        """
        self._update_key(mask, 'vertex_colors')

    def update_faces(self, mask):
        """
        Apply a mask to remove or duplicate face properties
        """
        self._update_key(mask, 'face_colors')

    def face_subset(self, face_index):
        """
        Given a mask of face indices, return a sliced version.

        Parameters
        ----------
        face_index: (n,) int, mask for faces
                    (n,) bool, mask for faces

        Returns
        ----------
        visual: ColorVisuals object containing a subset of faces.
        """
        if self.defined:
            result = ColorVisuals(
                face_colors=self.face_colors[face_index])
        else:
            result = ColorVisuals()

        return result

    @property
    def main_color(self):
        """
        What is the most commonly occurring color.

        Returns
        ------------
        color: (4,) uint8, most common color
        """
        if self.kind is None:
            return DEFAULT_COLOR
        elif self.kind == 'face':
            colors = self.face_colors
        elif self.kind == 'vertex':
            colors = self.vertex_colors
        else:
            raise ValueError('color kind incorrect!')

        # find the unique colors
        unique, inverse = grouping.unique_rows(colors)
        # the most commonly occurring color, or mode
        # this will be an index of inverse, not colors
        mode_index = np.bincount(inverse).argmax()
        color = colors[unique[mode_index]]

        return color

    def concatenate(self, other, *args):
        """
        Concatenate two or more ColorVisuals objects into a single object.

        Parameters
        -----------
        other : ColorVisuals
          Object to append
        *args: ColorVisuals objects

        Returns
        -----------
        result: ColorVisuals object containing information from current
                object and others in the order it was passed.
        """
        # avoid a circular import
        from . import objects
        result = objects.concatenate(self, other, *args)
        return result

    def __add__(self, other):
        """
        Concatenate two ColorVisuals objects into a single object.

        Parameters
        -----------
        other: ColorVisuals object

        Returns
        -----------
        result: ColorVisuals object containing information from current
                object and other in the order (self, other)
        """
        return self.concatenate(other)

    def _update_key(self, mask, key):
        """
        Mask the value contained in the DataStore at a specified key.

        Parameters
        -----------
        mask: (n,) int
              (n,) bool
        key: hashable object, in self._data
        """
        mask = np.asanyarray(mask)
        if key in self._data:
            self._data[key] = self._data[key][mask]


def to_rgba(colors, dtype=np.uint8):
    """
    Convert a single or multiple RGB colors to RGBA colors.

    Parameters
    ----------
    colors : (n, 3) or (n, 4) array
      RGB or RGBA colors

    Returns
    ----------
    colors : (n, 4) list of RGBA colors
             (4,)  single RGBA color
    """
    if colors is None or not util.is_sequence(colors):
        return DEFAULT_COLOR

    # colors as numpy array
    colors = np.asanyarray(colors)

    # integer value for opaque alpha given our datatype
    opaque = np.iinfo(dtype).max

    if (colors.dtype.kind == 'f' and colors.max() < (1.0 + 1e-8)):
        colors = (colors * opaque).round().astype(dtype)
    elif (colors.max() <= opaque):
        colors = colors.astype(dtype)
    else:
        raise ValueError('colors non- convertible!')

    if util.is_shape(colors, (-1, 3)):
        # add an opaque alpha for RGB colors
        colors = np.column_stack((
            colors,
            opaque * np.ones(len(colors)))).astype(dtype)
    elif util.is_shape(colors, (3,)):
        # if passed a single RGB color add an alpha
        colors = np.append(colors, opaque)

    if not (util.is_shape(colors, (4,)) or
            util.is_shape(colors, (-1, 4))):
        raise ValueError('Colors not of appropriate shape!')

    return colors


def to_float(colors):
    """
    Convert integer colors to 0.0 - 1.0 floating point colors

    Parameters
    -------------
    colors : (n, d) int
      Integer colors

    Returns
    -------------
    as_float : (n, d) float
      Float colors 0.0 - 1.0
    """

    # colors as numpy array
    colors = np.asanyarray(colors)
    if colors.dtype.kind == 'f':
        return colors
    elif colors.dtype.kind in 'iu':
        # integer value for opaque alpha given our datatype
        opaque = np.iinfo(colors.dtype).max
        return colors.astype(np.float64) / opaque
    else:
        raise ValueError('only works on int or float colors!')


def hex_to_rgba(color):
    """
    Turn a string hex color to a (4,) RGBA color.

    Parameters
    -----------
    color: str, hex color

    Returns
    -----------
    rgba: (4,) np.uint8, RGBA color
    """
    value = str(color).lstrip('#').strip()
    if len(value) == 6:
        rgb = [int(value[i:i + 2], 16) for i in (0, 2, 4)]
        rgba = np.append(rgb, 255).astype(np.uint8)
    else:
        raise ValueError('Only RGB supported')

    return rgba


def random_color(dtype=np.uint8):
    """
    Return a random RGB color using datatype specified.

    Parameters
    ----------
    dtype: numpy dtype of result

    Returns
    ----------
    color: (4,) dtype, random color that looks OK
    """
    hue = np.random.random() + .61803
    hue %= 1.0
    color = np.array(colorsys.hsv_to_rgb(hue, .99, .99))
    if np.dtype(dtype).kind in 'iu':
        max_value = (2**(np.dtype(dtype).itemsize * 8)) - 1
        color *= max_value
    color = np.append(color, max_value).astype(dtype)
    return color


def vertex_to_face_color(vertex_colors, faces):
    """
    Convert a list of vertex colors to face colors.

    Parameters
    ----------
    vertex_colors: (n,(3,4)),  colors
    faces:         (m,3) int, face indexes

    Returns
    -----------
    face_colors: (m,4) colors
    """
    vertex_colors = to_rgba(vertex_colors)
    face_colors = vertex_colors[faces].mean(axis=1)
    return face_colors.astype(np.uint8)


def face_to_vertex_color(mesh, face_colors, dtype=np.uint8):
    """
    Convert a list of face colors into a list of vertex colors.

    Parameters
    -----------
    mesh : trimesh.Trimesh object
    face_colors: (n, (3,4)) int, face colors
    dtype:       data type of output

    Returns
    -----------
    vertex_colors: (m,4) dtype, colors for each vertex
    """
    rgba = to_rgba(face_colors)
    vertex_colors = mesh.faces_sparse.dot(
        rgba.astype(np.float64))
    vertex_colors /= mesh.faces_sparse.sum(axis=1)
    vertex_colors = vertex_colors.astype(dtype)

    return vertex_colors


def colors_to_materials(colors, count=None):
    """
    Convert a list of colors into a list of unique materials
    and material indexes.

    Parameters
    -----------
    colors : (n, 3) or (n, 4) float
      RGB or RGBA colors
    count : int
      Number of entities to apply color to

    Returns
    -----------
    diffuse : (m, 4) int
      Colors
    index : (count,) int
      Index of each color
    """

    # convert RGB to RGBA
    rgba = to_rgba(colors)

    # if we were only passed a single color
    if util.is_shape(rgba, (4,)) and count is not None:
        diffuse = rgba.reshape((-1, 4))
        index = np.zeros(count, dtype=np.int)
    elif util.is_shape(rgba, (-1, 4)):
        # we were passed multiple colors
        # find the unique colors in the list to save as materials
        unique, index = grouping.unique_rows(rgba)
        diffuse = rgba[unique]
    else:
        raise ValueError('Colors not convertible!')

    return diffuse, index


def linear_color_map(values, color_range=None):
    """
    Linearly interpolate between two colors.

    If colors are not specified the function will
    interpolate between  0.0 values as red and 1.0 as green.

    Parameters
    --------------
    values : (n, ) float
      Values to interpolate
    color_range : None, or (2, 4) uint8
      What colors should extrema be set to

    Returns
    ---------------
    colors : (n, 4) uint8
      RGBA colors for interpolated values
    """

    if color_range is None:
        color_range = np.array([[255, 0, 0, 255],
                                [0, 255, 0, 255]],
                               dtype=np.uint8)
    else:
        color_range = np.asanyarray(color_range,
                                    dtype=np.uint8)

    if color_range.shape != (2, 4):
        raise ValueError('color_range must be RGBA (2, 4)')

    # float 1D array clamped to 0.0 - 1.0
    values = np.clip(np.asanyarray(
        values, dtype=np.float64).ravel(),
        0.0, 1.0).reshape((-1, 1))

    # the stacked component colors
    color = [np.ones((len(values), 4)) * c
             for c in color_range.astype(np.float64)]

    # interpolated colors
    colors = (color[1] * values) + (color[0] * (1.0 - values))

    # rounded and set to correct data type
    colors = np.round(colors).astype(np.uint8)

    return colors


def interpolate(values, color_map=None, dtype=np.uint8):
    """
    Given a 1D list of values, return interpolated colors
    for the range.

    Parameters
    ---------------
    values : (n, ) float
      Values to be interpolated over
    color_map : None, or str
      Key to a colormap contained in:
      matplotlib.pyplot.colormaps()
      e.g: 'viridis'

    Returns
    -------------
    interpolated : (n, 4) dtype
      Interpolated RGBA colors
    """

    # get a color interpolation function
    if color_map is None:
        cmap = linear_color_map
    else:
        from matplotlib.pyplot import get_cmap
        cmap = get_cmap(color_map)

    # make input always float
    values = np.asanyarray(values, dtype=np.float64).ravel()
    # scale values to 0.0 - 1.0 and get colors
    colors = cmap((values - values.min()) / values.ptp())
    # convert to 0-255 RGBA
    rgba = to_rgba(colors, dtype=dtype)

    return rgba


def uv_to_color(uv, image):
    """
    Get the color in a texture image.

    Parameters
    -------------
    uv : (n, 2) float
      UV coordinates on texture image
    image : PIL.Image
      Texture image

    Returns
    ----------
    colors : (n, 4) float
      RGBA color at each of the UV coordinates
    """
    if image is None or uv is None:
        return None

    # UV coordinates should be (n, 2) float
    uv = np.asanyarray(uv, dtype=np.float64)

    # get texture image pixel positions of UV coordinates
    x = (uv[:, 0] * (image.width - 1))
    y = ((1 - uv[:, 1]) * (image.height - 1))

    # convert to int and wrap to image
    # size in the manner of GL_REPEAT
    x = x.round().astype(np.int64) % image.width
    y = y.round().astype(np.int64) % image.height

    # access colors from pixel locations
    # make sure image is RGBA before getting values
    colors = np.asanyarray(image.convert('RGBA'))[y, x]

    # conversion to RGBA should have corrected shape
    assert colors.ndim == 2 and colors.shape[1] == 4

    return colors


DEFAULT_COLOR = np.array([102, 102, 102, 255], dtype=np.uint8)
