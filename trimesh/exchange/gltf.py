"""
gltf.py
------------

Provides GLTF 2.0 exports of trimesh.Trimesh objects
as GL_TRIANGLES, and trimesh.Path2D/Path3D as GL_LINES
"""

import json
import base64
import collections

import numpy as np

from .. import util
from .. import visual
from .. import rendering
from .. import resources
from .. import transformations

from ..constants import log, tol

# magic numbers which have meaning in GLTF
# most are uint32's of UTF-8 text
_magic = {"gltf": 1179937895,
          "json": 1313821514,
          "bin": 5130562}

# GLTF data type codes: little endian numpy dtypes
_dtypes = {5120: "<i1",
           5121: "<u1",
           5122: "<i2",
           5123: "<u2",
           5125: "<u4",
           5126: "<f4"}

# GLTF data formats: numpy shapes
_shapes = {
    "SCALAR": 1,
    "VEC2": (2),
    "VEC3": (3),
    "VEC4": (4),
    "MAT2": (2, 2),
    "MAT3": (3, 3),
    "MAT4": (4, 4)}

# a default PBR metallic material
_default_material = {
    "pbrMetallicRoughness": {
        "baseColorFactor": [0, 0, 0, 0],
        "metallicFactor": 0,
        "roughnessFactor": 0}}

# specify common dtypes with forced little endian
float32 = np.dtype("<f4")
uint32 = np.dtype("<u4")
uint8 = np.dtype("<u1")


def export_gltf(scene,
                extras=None,
                include_normals=False):
    """
    Export a scene object as a GLTF directory.

    This puts each mesh into a separate file (i.e. a `buffer`)
    as opposed to one larger file.

    Parameters
    -----------
    scene : trimesh.Scene
      Scene to be exported

    Returns
    ----------
    export : dict
      Format: {file name : file data}
    """
    # if we were passed a bare Trimesh or Path3D object
    if (not util.is_instance_named(scene, "Scene")
            and hasattr(scene, "scene")):
        scene = scene.scene()

    # create the header and buffer data
    tree, buffer_items = _create_gltf_structure(
        scene=scene,
        extras=extras,
        include_normals=include_normals)

    # store files as {name : data}
    files = {}
    # make one buffer per buffer_items
    buffers = [None] * len(buffer_items)
    # A bufferView is a slice of a file
    views = [None] * len(buffer_items)
    # create the buffer views
    for i, item in enumerate(buffer_items):
        views[i] = {
            "buffer": i,
            "byteOffset": 0,
            "byteLength": len(item)}

        buffer_data = _byte_pad(bytes().join(buffer_items[i: i + 2]))
        buffer_name = "gltf_buffer_{}.bin".format(i)
        buffers[i] = {
            "uri": buffer_name,
            "byteLength": len(buffer_data)}
        files[buffer_name] = buffer_data

    tree["buffers"] = buffers
    tree["bufferViews"] = views
    files["model.gltf"] = json.dumps(tree).encode("utf-8")
    return files


def export_glb(scene, extras=None, include_normals=False):
    """
    Export a scene as a binary GLTF (GLB) file.

    Parameters
    ------------
    scene: trimesh.Scene
      Input geometry
    extras : JSON serializable
      Will be stored in the extras field
    include_normals : bool
      Include vertex normals in output file?

    Returns
    ----------
    exported : bytes
      Exported result in GLB 2.0
    """
    # if we were passed a bare Trimesh or Path3D object
    if (not util.is_instance_named(scene, "Scene") and
            hasattr(scene, "scene")):
        # generate a scene with just that mesh in it
        scene = scene.scene()

    tree, buffer_items = _create_gltf_structure(
        scene=scene,
        extras=extras,
        include_normals=include_normals)

    # A bufferView is a slice of a file
    views = []
    # create the buffer views
    current_pos = 0
    for current_item in buffer_items:
        views.append(
            {"buffer": 0,
             "byteOffset": current_pos,
             "byteLength": len(current_item)})
        current_pos += len(current_item)
    # combine bytes into a single blob
    buffer_data = bytes().join(buffer_items)
    # add the information about the buffer data
    tree["buffers"] = [{"byteLength": len(buffer_data)}]
    tree["bufferViews"] = views

    # export the tree to JSON for the content of the file
    content = json.dumps(tree)
    # add spaces to content, so the start of the data
    # is 4 byte aligned as per spec
    content += (4 - ((len(content) + 20) % 4)) * " "
    content = content.encode("utf-8")
    # make sure we didn't screw it up
    assert (len(content) % 4) == 0

    # the initial header of the file
    header = _byte_pad(
        np.array([_magic["gltf"],  # magic, turns into glTF
                  2,               # GLTF version
                  # length is the total length of the Binary glTF
                  # including Header and all Chunks, in bytes.
                  len(content) + len(buffer_data) + 28,
                  # contentLength is the length, in bytes,
                  # of the glTF content (JSON)
                  len(content),
                  # magic number which is 'JSON'
                  1313821514],
                 dtype="<u4",
                 ).tobytes())

    # the header of the binary data section
    bin_header = _byte_pad(
        np.array([len(buffer_data), 0x004E4942],
                 dtype="<u4").tobytes())

    exported = bytes().join([header,
                             content,
                             bin_header,
                             buffer_data])

    return exported


def load_gltf(file_obj=None,
              resolver=None,
              **mesh_kwargs):
    """
    Load a GLTF file, which consists of a directory structure
    with multiple files.

    Parameters
    -------------
    file_obj : None or file-like
      Object containing header JSON, or None
    resolver : trimesh.visual.Resolver
      Object which can be used to load other files by name
    **mesh_kwargs : dict
      Passed to mesh constructor

    Returns
    --------------
    kwargs : dict
      Arguments to create scene
    """
    try:
        # see if we've been passed the GLTF header file
        tree = json.load(file_obj)
    except BaseException:
        # otherwise header should be in 'model.gltf'
        data = resolver['model.gltf']
        # old versions of python/json need strings
        try:
            tree = json.loads(data)
        except BaseException:
            tree = json.loads(data.decode('utf-8'))

    # use the URI and resolver to get data from file names
    buffers = [_uri_to_bytes(uri=b['uri'], resolver=resolver)
               for b in tree['buffers']]

    # turn the layout header and data into kwargs
    # that can be used to instantiate a trimesh.Scene object
    kwargs = _read_buffers(header=tree,
                           buffers=buffers,
                           mesh_kwargs=mesh_kwargs,
                           resolver=resolver)
    return kwargs


def load_glb(file_obj, resolver=None, **mesh_kwargs):
    """
    Load a GLTF file in the binary GLB format into a trimesh.Scene.

    Implemented from specification:
    https://github.com/KhronosGroup/glTF/tree/master/specification/2.0

    Parameters
    ------------
    file_obj : file- like object
      Containing GLB data

    Returns
    ------------
    kwargs : dict
      Kwargs to instantiate a trimesh.Scene
    """

    # save the start position of the file for referencing
    # against lengths
    start = file_obj.tell()
    # read the first 20 bytes which contain section lengths
    head_data = file_obj.read(20)
    head = np.frombuffer(head_data, dtype="<u4")

    # check to make sure first index is gltf
    # and second is 2, for GLTF 2.0
    if head[0] != _magic["gltf"] or head[1] != 2:
        raise ValueError("file is not GLTF 2.0")

    # overall file length
    # first chunk length
    # first chunk type
    length, chunk_length, chunk_type = head[2:]

    # first chunk should be JSON header
    if chunk_type != _magic["json"]:
        raise ValueError("no initial JSON header!")

    # uint32 causes an error in read, so we convert to native int
    # for the length passed to read, for the JSON header
    json_data = file_obj.read(int(chunk_length))
    # convert to text
    if hasattr(json_data, "decode"):
        json_data = json_data.decode("utf-8")
    # load the json header to native dict
    header = json.loads(json_data)

    # read the binary data referred to by GLTF as 'buffers'
    buffers = []
    while (file_obj.tell() - start) < length:
        # the last read put us past the JSON chunk
        # we now read the chunk header, which is 8 bytes
        chunk_head = file_obj.read(8)
        if len(chunk_head) != 8:
            # double check to make sure we didn't
            # read the whole file
            break

        chunk_length, chunk_type = np.frombuffer(
            chunk_head, dtype="<u4")
        # make sure we have the right data type
        if chunk_type != _magic["bin"]:
            raise ValueError("not binary GLTF!")
        # read the chunk
        chunk_data = file_obj.read(int(chunk_length))
        if len(chunk_data) != chunk_length:
            raise ValueError("chunk was not expected length!")
        buffers.append(chunk_data)

    # turn the layout header and data into kwargs
    # that can be used to instantiate a trimesh.Scene object
    kwargs = _read_buffers(header=header,
                           buffers=buffers,
                           mesh_kwargs=mesh_kwargs)
    return kwargs


def _uri_to_bytes(uri, resolver):
    """
    Take a URI string and load it as a
    a filename or as base64

    Parameters
    --------------
    uri : string
      Usually a filename or something like:
      "data:object/stuff,base64,AABA112A..."
    resolver : trimesh.visual.Resolver
      A resolver to load referenced assets

    Returns
    ---------------
    data : bytes
      Loaded data from URI
    """
    # see if the URI has base64 data
    index = uri.find('base64,')
    if index < 0:
        # string didn't contain the base64 header
        # so return the result from the resolver
        return resolver[uri]
    # we have a base64 header so strip off
    # leading index and then decode into bytes
    return base64.b64decode(uri[index + 7:])


def _mesh_to_material(mesh, metallic=0.0, rough=0.0):
    """
    Create a simple GLTF material for a mesh using the most
    commonly occurring color in that mesh.

    Parameters
    ------------
    mesh: trimesh.Trimesh
      Mesh to create a material from

    Returns
    ------------
    material: dict
      In GLTF material format
    """

    try:
        # just get the most commonly occurring color
        color = mesh.visual.main_color
    except BaseException:
        color = np.array([100, 100, 100, 255], dtype=np.uint8)

    # convert uint color to 0.0-1.0 float color
    color = color.astype(float32) / np.iinfo(color.dtype).max

    material = {
        "pbrMetallicRoughness": {
            "baseColorFactor": color.tolist(),
            "metallicFactor": metallic,
            "roughnessFactor": rough}}

    return material


def _create_gltf_structure(scene,
                           extras=None,
                           include_normals=None):
    """
    Generate a GLTF header.

    Parameters
    -------------
    scene : trimesh.Scene
      Input scene data
    extras : JSON serializable
      Will be stored in the extras field
    include_normals : bool
      Include vertex normals in output file?

    Returns
    ---------------
    tree : dict
      Contains required keys for a GLTF scene
    buffer_items : list
      Contains bytes of data
    """
    # we are defining a single scene, and will be setting the
    # world node to the 0-index
    tree = {
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "asset": {"version": "2.0",
                  "generator": "https://github.com/mikedh/trimesh"},
        "accessors": [],
        "meshes": [],
        "images": [],
        "textures": [],
        "samplers": [{}],
        "materials": [],
        "cameras": [_convert_camera(scene.camera)]}

    if extras is not None:
        tree['extras'] = extras

    # grab the flattened scene graph in GLTF's format
    nodes = scene.graph.to_gltf(scene=scene)
    tree.update(nodes)

    buffer_items = []
    for name, geometry in scene.geometry.items():
        if util.is_instance_named(geometry, "Trimesh"):
            # add the mesh
            _append_mesh(
                mesh=geometry,
                name=name,
                tree=tree,
                buffer_items=buffer_items,
                include_normals=include_normals)
        elif util.is_instance_named(geometry, "Path"):
            # add Path2D and Path3D objects
            _append_path(
                path=geometry,
                name=name,
                tree=tree,
                buffer_items=buffer_items)
    # cull empty or unpopulated fields here
    if len(tree['textures']) == 0:
        tree.pop('textures')
        tree.pop('samplers')
    if len(tree["materials"]) == 0:
        tree.pop("materials")
    if len(tree['images']) == 0:
        tree.pop('images')

    # in unit tests compare our header against the schema
    if tol.strict:
        validate(tree)

    return tree, buffer_items


def _append_mesh(mesh,
                 name,
                 tree,
                 buffer_items,
                 include_normals):
    """
    Append a mesh to the scene structure and put the
    data into buffer_items.

    Parameters
    -------------
    mesh : trimesh.Trimesh
      Source geometry
    name : str
      Name of geometry
    tree : dict
      Will be updated with data from mesh
    buffer_items
      Will have buffer appended with mesh data
    include_normals : bool
      Include vertex normals in export or not
    """
    # meshes reference accessor indexes
    # mode 4 is GL_TRIANGLES
    tree["meshes"].append({
        "name": name,
        "primitives": [{
            "attributes": {"POSITION": len(tree["accessors"]) + 1},
            "indices": len(tree["accessors"]),
            "mode": 4}]})

    # if units are defined, store them as an extra
    # the GLTF spec says everything is implicit meters
    # we're not doing that as our unit conversions are expensive
    # although that might be better, implicit works for 3DXML
    # https://github.com/KhronosGroup/glTF/tree/master/extensions
    if mesh.units is not None and 'meter' not in mesh.units:
        tree["meshes"][-1]["extras"] = {"units": str(mesh.units)}

    # accessors refer to data locations
    # mesh faces are stored as flat list of integers
    tree["accessors"].append({
        "bufferView": len(buffer_items),
        "componentType": 5125,
        "count": len(mesh.faces) * 3,
        "max": [int(mesh.faces.max())],
        "min": [0],
        "type": "SCALAR"})
    # convert mesh data to the correct dtypes
    # faces: 5125 is an unsigned 32 bit integer
    buffer_items.append(_byte_pad(
        mesh.faces.astype(uint32).tobytes()))

    # the vertex accessor
    tree["accessors"].append({
        "bufferView": len(buffer_items),
        "componentType": 5126,
        "count": len(mesh.vertices),
        "type": "VEC3",
        "byteOffset": 0,
        "max": mesh.vertices.max(axis=0).tolist(),
        "min": mesh.vertices.min(axis=0).tolist()})
    # vertices: 5126 is a float32
    buffer_items.append(_byte_pad(
        mesh.vertices.astype(float32).tobytes()))

    # make sure nothing fell off the truck
    assert len(buffer_items) >= tree['accessors'][-1]['bufferView']

    # check to see if we have vertex or face colors
    if mesh.visual.kind in ['vertex', 'face']:
        # make sure colors are RGBA, this should always be true
        vertex_colors = mesh.visual.vertex_colors
        # add the reference for vertex color
        tree["meshes"][-1]["primitives"][0]["attributes"][
            "COLOR_0"] = len(tree["accessors"])
        # convert color data to bytes
        color_data = _byte_pad(vertex_colors.astype(uint8).tobytes())
        # the vertex color accessor data
        tree["accessors"].append({
            "bufferView": len(buffer_items),
            "componentType": 5121,
            "normalized": True,
            "count": len(vertex_colors),
            "type": "VEC4",
            "byteOffset": 0})
        # the actual color data
        buffer_items.append(color_data)

    elif hasattr(mesh.visual, 'material'):
        # set the material to the last index in the tree
        tree["meshes"][-1]["primitives"][0]["material"] = len(tree["materials"])
        # immediately append the material to materials list
        # will also append necessary images and textures to tree
        _append_material(mat=mesh.visual.material,
                         tree=tree,
                         buffer_items=buffer_items)

        # if mesh has UV coordinates defined export them
        if (hasattr(mesh.visual, 'uv') and
                np.shape(mesh.visual.uv) == (len(mesh.vertices), 2)):

            # add the reference for UV coordinates
            tree["meshes"][-1]["primitives"][0]["attributes"][
                "TEXCOORD_0"] = len(tree["accessors"])

            # reverse the Y for GLTF
            uv = mesh.visual.uv.copy()
            uv[:, 1] = 1.0 - uv[:, 1]
            # convert UV coordinate data to bytes and pad
            uv_data = _byte_pad(uv.astype(float32).tobytes())
            # add an accessor describing the blob of UV's
            tree["accessors"].append({
                "bufferView": len(buffer_items),
                "componentType": 5126,
                "count": len(mesh.visual.uv),
                "type": "VEC2",
                "byteOffset": 0})
            # immediately add UV data so bufferView indices are correct
            buffer_items.append(uv_data)

    if (include_normals or
        (include_normals is None and
         'vertex_normals' in mesh._cache.cache)):
        # add the reference for vertex color
        tree["meshes"][-1]["primitives"][0]["attributes"][
            "NORMAL"] = len(tree["accessors"])
        normal_data = _byte_pad(mesh.vertex_normals.astype(
            float32).tobytes())
        # the vertex color accessor data
        tree["accessors"].append({
            "bufferView": len(buffer_items),
            "componentType": 5126,
            "count": len(mesh.vertices),
            "type": "VEC3",
            "byteOffset": 0})
        # the actual color data
        buffer_items.append(normal_data)


def _byte_pad(data, bound=4):
    """
    GLTF wants chunks aligned with 4 byte boundaries
    so this function will add padding to the end of a
    chunk of bytes so that it aligns with a specified
    boundary size

    Parameters
    --------------
    data : bytes
      Data to be padded
    bound : int
      Length of desired boundary

    Returns
    --------------
    padded : bytes
      Result where: (len(padded) % bound) == 0
    """
    bound = int(bound)
    if len(data) % bound != 0:
        # extra bytes to pad with
        count = bound - (len(data) % bound)
        # bytes(count) only works on Python 3
        pad = (' ' * count).encode('utf-8')
        # combine the padding and data
        result = bytes().join([data, pad])
        # we should always divide evenly
        if (len(result) % bound) != 0:
            raise ValueError(
                'byte_pad failed! ori:{} res:{} pad:{} req:{}'.format(
                    len(data), len(result), count, bound))
        return result

    return data


def _append_path(path, name, tree, buffer_items):
    """
    Append a 2D or 3D path to the scene structure and put the
    data into buffer_items.

    Parameters
    -------------
    path : trimesh.Path2D or trimesh.Path3D
      Source geometry
    name : str
      Name of geometry
    tree : dict
      Will be updated with data from path
    buffer_items
      Will have buffer appended with path data
    """

    # convert the path to the unnamed args for
    # a pyglet vertex list
    vxlist = rendering.path_to_vertexlist(path)

    tree["meshes"].append({
        "name": name,
        "primitives": [{
            "attributes": {"POSITION": len(tree["accessors"])},
            "mode": 1,  # mode 1 is GL_LINES
            "material": len(tree["materials"])}]})

    # if units are defined, store them as an extra:
    # https://github.com/KhronosGroup/glTF/tree/master/extensions
    if path.units is not None and 'meter' not in path.units:
        tree["meshes"][-1]["extras"] = {"units": str(path.units)}

    tree["accessors"].append(
        {
            "bufferView": len(buffer_items),
            "componentType": 5126,
            "count": vxlist[0],
            "type": "VEC3",
            "byteOffset": 0,
            "max": path.vertices.max(axis=0).tolist(),
            "min": path.vertices.min(axis=0).tolist()})

    # TODO add color support to Path object
    # this is just exporting everying as black
    tree["materials"].append(_default_material)

    # data is the second value of the fourth field
    # which is a (data type, data) tuple
    buffer_items.append(_byte_pad(
        vxlist[4][1].astype(float32).tobytes()))


def _parse_materials(header, views, resolver=None):
    """
    Convert materials and images stored in a GLTF header
    and buffer views to PBRMaterial objects.

    Parameters
    ------------
    header : dict
      Contains layout of file
    views : (n,) bytes
      Raw data

    Returns
    ------------
    materials : list
      List of trimesh.visual.texture.Material objects
    """
    try:
        import PIL.Image
    except ImportError:
        log.warning("unable to load textures without pillow!")
        return None

    # load any images
    images = None
    if "images" in header:
        # images are referenced by index
        images = [None] * len(header["images"])
        # loop through images
        for i, img in enumerate(header["images"]):
            # get the bytes representing an image
            if 'bufferView' in img:
                blob = views[img["bufferView"]]
            elif 'uri' in img:
                # will get bytes from filesystem or base64 URI
                blob = _uri_to_bytes(uri=img['uri'], resolver=resolver)
            else:
                log.warning('unable to load image from: {}'.format(
                    img.keys()))
                continue
            # i.e. 'image/jpeg'
            # mime = img['mimeType']
            try:
                # load the buffer into a PIL image
                images[i] = PIL.Image.open(util.wrap_as_stream(blob))
            except BaseException:
                log.error("failed to load image!", exc_info=True)

    # store materials which reference images
    materials = []
    if "materials" in header:
        for mat in header["materials"]:
            # flatten key structure so we can loop it
            loopable = mat.copy()
            # this key stores another dict of crap
            if "pbrMetallicRoughness" in loopable:
                # add keys of keys to top level dict
                loopable.update(loopable.pop("pbrMetallicRoughness"))

            # save flattened keys we can use for kwargs
            pbr = {}
            for k, v in loopable.items():
                if not isinstance(v, dict):
                    pbr[k] = v
                elif "index" in v:
                    # get the index of image for texture
                    idx = header["textures"][v["index"]]["source"]
                    # store the actual image as the value
                    pbr[k] = images[idx]
            # create a PBR material object for the GLTF material
            materials.append(visual.material.PBRMaterial(**pbr))

    return materials


def _read_buffers(header, buffers, mesh_kwargs, resolver=None):
    """
    Given a list of binary data and a layout, return the
    kwargs to create a scene object.

    Parameters
    -----------
    header : dict
      With GLTF keys
    buffers : list of bytes
      Stored data
    passed : dict
      Kwargs for mesh constructors

    Returns
    -----------
    kwargs : dict
      Can be passed to load_kwargs for a trimesh.Scene
    """
    # split buffer data into buffer views
    views = [None] * len(header["bufferViews"])
    for i, view in enumerate(header["bufferViews"]):
        if "byteOffset" in view:
            start = view["byteOffset"]
        else:
            start = 0
        end = start + view["byteLength"]
        views[i] = buffers[view["buffer"]][start:end]

        assert len(views[i]) == view["byteLength"]

    # load data from buffers into numpy arrays
    # using the layout described by accessors
    access = [None] * len(header['accessors'])
    for index, a in enumerate(header["accessors"]):
        # number of items
        count = a['count']
        # what is the datatype
        dtype = _dtypes[a["componentType"]]
        # basically how many columns
        per_item = _shapes[a["type"]]
        # use reported count to generate shape
        shape = np.append(count, per_item)
        # number of items when flattened
        # i.e. a (4, 4) MAT4 has 16
        per_count = np.abs(np.product(per_item))

        if 'bufferView' in a:
            # data was stored in a buffer view so get raw bytes
            data = views[a["bufferView"]]
            # is the accessor offset in a buffer
            if "byteOffset" in a:
                start = a["byteOffset"]
            else:
                # otherwise assume we start at first byte
                start = 0
            # length is the number of bytes per item times total
            length = np.dtype(dtype).itemsize * count * per_count
            # load the bytes data into correct dtype and shape
            access[index] = np.frombuffer(
                data[start:start + length], dtype=dtype).reshape(shape)
        else:
            # a "sparse" accessor should be initialized as zeros
            access[index] = np.zeros(
                count * per_count, dtype=dtype).reshape(shape)

    # load images and textures into material objects
    materials = _parse_materials(
        header, views=views, resolver=resolver)

    mesh_prim = collections.defaultdict(list)
    # load data from accessors into Trimesh objects
    meshes = collections.OrderedDict()
    for index, m in enumerate(header["meshes"]):
        metadata = {}
        try:
            # try loading units from the GLTF extra
            metadata['units'] = str(m["extras"]["units"])
        except BaseException:
            # GLTF spec indicates the default units are meters
            metadata['units'] = 'meters'

        for j, p in enumerate(m["primitives"]):
            # if we don't have a triangular mesh continue
            # if not specified assume it is a mesh
            if "mode" in p and p["mode"] != 4:
                continue

            # store those units
            kwargs = {"metadata": {}}
            kwargs.update(mesh_kwargs)
            kwargs["metadata"].update(metadata)

            # get vertices from accessors
            kwargs["vertices"] = access[p["attributes"]["POSITION"]]

            # get faces from accessors
            if 'indices' in p:
                kwargs["faces"] = access[p["indices"]].reshape((-1, 3))
            else:
                # indices are apparently optional and we are supposed to
                # do the same thing as webGL drawArrays?
                kwargs['faces'] = np.arange(
                    len(kwargs['vertices']),
                    dtype=np.int64).reshape((-1, 3))

            # do we have UV coordinates
            if "material" in p:
                if materials is None:
                    log.warning('no materials! `pip install pillow`')
                else:
                    uv = None
                    if "TEXCOORD_0" in p["attributes"]:
                        # flip UV's top- bottom to move origin to lower-left:
                        # https://github.com/KhronosGroup/glTF/issues/1021
                        uv = access[p["attributes"]["TEXCOORD_0"]].copy()
                        uv[:, 1] = 1.0 - uv[:, 1]
                        # create a texture visual
                    kwargs["visual"] = visual.texture.TextureVisuals(
                        uv=uv, material=materials[p["material"]])

            # create a unique mesh name per- primitive
            if "name" in m:
                name = m["name"]
            else:
                name = "GLTF_geometry"

                # make name unique across multiple meshes
                if len(header["meshes"]) > 1:
                    name += "_{}".format(index)

            # each primitive gets it's own Trimesh object
            if len(m["primitives"]) > 1:
                name += "_{}".format(j)

            meshes[name] = kwargs
            mesh_prim[index].append(name)

    # make it easier to reference nodes
    nodes = header["nodes"]
    # nodes are referenced by index
    # save their string names if they have one
    # node index (int) : name (str)
    names = {}
    for i, n in enumerate(nodes):
        if "name" in n:
            names[i] = n["name"]
        else:
            names[i] = str(i)

    # make sure we have a unique base frame name
    base_frame = "world"
    if base_frame in names:
        base_frame = str(int(np.random.random() * 1e10))
    names[base_frame] = base_frame

    # visited, kwargs for scene.graph.update
    graph = collections.deque()
    # unvisited, pairs of node indexes
    queue = collections.deque()

    if 'scene' in header:
        # specify the index of scenes if specified
        scene_index = header['scene']
    else:
        # otherwise just use the first index
        scene_index = 0

    # start the traversal from the base frame to the roots
    for root in header["scenes"][scene_index]["nodes"]:
        # add transform from base frame to these root nodes
        queue.append([base_frame, root])

    # go through the nodes tree to populate
    # kwargs for scene graph loader
    while len(queue) > 0:
        # (int, int) pair of node indexes
        a, b = queue.pop()

        # dict of child node
        # parent = nodes[a]
        child = nodes[b]
        # add edges of children to be processed
        if "children" in child:
            queue.extend([[b, i] for i in child["children"]])

        # kwargs to be passed to scene.graph.update
        kwargs = {"frame_from": names[a], "frame_to": names[b]}

        # grab matrix from child
        # parent -> child relationships have matrix stored in child
        # for the transform from parent to child
        if "matrix" in child:
            kwargs["matrix"] = np.array(child["matrix"],
                                        dtype=np.float64).reshape((4, 4)).T
        else:
            # if no matrix set identity
            kwargs["matrix"] = np.eye(4)

        # Now apply keyword translations
        # GLTF applies these in order: T * R * S
        if "translation" in child:
            kwargs["matrix"] = np.dot(
                kwargs["matrix"],
                transformations.translation_matrix(child["translation"]))

        if "rotation" in child:
            # GLTF rotations are stored as (4,) XYZW unit quaternions
            # we need to re- order to our quaternion style, WXYZ
            quat = np.reshape(child["rotation"], 4)[[3, 0, 1, 2]]

            # add the rotation to the matrix
            kwargs["matrix"] = np.dot(
                kwargs["matrix"], transformations.quaternion_matrix(quat))

        if "scale" in child:
            # add scale to the matrix
            kwargs["matrix"] = np.dot(
                kwargs["matrix"],
                np.diag(np.concatenate((child['scale'], [1.0]))))

        # append the nodes for connectivity without the mesh
        graph.append(kwargs.copy())
        if "mesh" in child:
            # append a new node per- geometry instance
            geometries = mesh_prim[child["mesh"]]
            for name in geometries:
                kwargs["geometry"] = name
                kwargs["frame_to"] = "{}_{}".format(
                    name, util.unique_id(
                        length=6, increment=len(graph)).upper()
                )
                # append the edge with the mesh frame
                graph.append(kwargs.copy())

    # kwargs to be loaded
    result = {
        "class": "Scene",
        "geometry": meshes,
        "graph": graph,
        "base_frame": base_frame,
    }

    return result


def _convert_camera(camera):
    """
    Convert a trimesh camera to a GLTF camera.

    Parameters
    ------------
    camera : trimesh.scene.cameras.Camera
      Trimesh camera object

    Returns
    -------------
    gltf_camera : dict
      Camera represented as a GLTF dict
    """
    result = {
        "name": camera.name,
        "type": "perspective",
        "perspective": {
            "aspectRatio": camera.fov[0] / camera.fov[1],
            "yfov": np.radians(camera.fov[1]),
            "znear": float(camera.z_near)}}
    return result


def _append_image(img, tree, buffer_items):
    """
    Append a PIL image to a GLTF2.0 tree.

    Parameters
    ------------
    img : PIL.Image
      Image object
    tree : dict
      GLTF 2.0 format tree
    buffer_items : (n,) bytes
      Binary blobs containing data

    Returns
    -----------
    index : int or None
      The index of the image in the tree
      None if image append failed for any reason
    """
    # probably not a PIL image so exit
    if not hasattr(img, 'format'):
        return None

    # don't re-encode JPEGs
    if img.format == 'JPEG':
        # no need to mangle JPEGs
        save_as = 'JPEG'
    else:
        # for everything else just use PNG
        save_as = 'png'

    # get the image data into a bytes object
    with util.BytesIO() as f:
        img.save(f, format=save_as)
        f.seek(0)
        data = f.read()

    # append buffer index and the GLTF-acceptable mimetype
    tree['images'].append({
        'bufferView': len(buffer_items),
        'mimeType': 'image/{}'.format(save_as.lower())})
    # append data so bufferView matches
    buffer_items.append(_byte_pad(data))

    # index is length minus one
    return len(tree['images']) - 1


def _append_material(mat, tree, buffer_items):
    """
    Add passed PBRMaterial as GLTF 2.0 specification JSON
    serializable data:
    - images are added to `tree['images']`
    - texture is added to `tree['texture']`
    - material is added to `tree['materials']`


    Parameters
    ------------
    mat : trimesh.visual.materials.PBRMaterials
      Source material to convert
    tree : dict
      GLTF header blob
    buffer_items : (n,) bytes
      Binary blobs with various data
    """

    # if they have passed a material with
    # a PBR conversion method call it
    # TODO: implement this method on SimpleMaterial
    if hasattr(mat, 'to_pbr'):
        mat = mat.to_pbr()

    # a default PBR metallic material
    pbr = {"pbrMetallicRoughness": {}}
    try:
        # try to convert base color to (4,) float color
        pbr['baseColorFactor'] = visual.color.to_float(
            mat.baseColorFactor).reshape(4).tolist()
    except BaseException:
        pass

    try:
        pbr['emissiveFactor'] = mat.emissiveFactor.reshape(3).tolist()
    except BaseException:
        pass

    # if scalars are defined correctly export
    if isinstance(mat.metallicFactor, float):
        pbr['metallicFactor'] = mat.metallicFactor
    if isinstance(mat.roughnessFactor, float):
        pbr['roughnessFactor'] = mat.roughnessFactor

    # which keys of the PBRMaterial are images
    image_mapping = {
        'baseColorTexture': mat.baseColorTexture,
        'emissiveTexture': mat.emissiveTexture,
        'normalTexture': mat.normalTexture,
        'occlusionTexture': mat.occlusionTexture,
        'metallicRoughnessTexture': mat.metallicRoughnessTexture}

    for key, img in image_mapping.items():
        if img is None:
            continue
        # try adding the base image to the export object
        index = _append_image(
            img=img,
            tree=tree,
            buffer_items=buffer_items)
        # if the image was added successfully it will return index
        # if it failed for any reason, it will return None
        if index is not None:
            # add a reference to the base color texture
            pbr[key] = {'index': len(tree['textures'])}
            # add an object for the texture
            tree['textures'].append({'source': index, 'sampler': 0})

    # for our PBRMaterial object we flatten all keys
    # however GLTF would like some of them under the
    # "pbrMetallicRoughness" key
    pbr_subset = ['baseColorTexture',
                  'baseColorFactor',
                  'roughnessFactor',
                  'metallicRoughnessTexture']
    # move keys down a level
    for key in pbr_subset:
        if key in pbr:
            pbr["pbrMetallicRoughness"][key] = pbr.pop(key)

    # if we didn't have any PBR keys remove the empty key
    if len(pbr['pbrMetallicRoughness']) == 0:
        pbr.pop('pbrMetallicRoughness')

    # append the new material
    tree['materials'].append(pbr)


def validate(header):
    """
    Validate a GLTF 2.0 header against the schema.

    Returns result from:
    `jsonschema.validate(header, schema=get_schema())`

    Parameters
    -------------
    header : dict
      Populated GLTF 2.0 header

    Raises
    --------------
    err : jsonschema.exceptions.ValidationError
      If the tree is an invalid GLTF2.0 header
    """
    # a soft dependency
    import jsonschema
    # will do the reference replacement
    schema = get_schema()
    # validate the passed header against the schema
    return jsonschema.validate(header, schema=schema)


def get_schema():
    """
    Get a copy of the GLTF 2.0 schema with references resolved.

    Returns
    ------------
    schema : dict
      A copy of the GLTF 2.0 schema without external references.
    """
    # replace references
    from ..schemas import resolve_json
    # get zip resolver to access referenced assets
    from ..visual.resolvers import ZipResolver

    # get a blob of a zip file including the GLTF 2.0 schema
    blob = resources.get_resource(
        'gltf_2.0_schema.zip', decode=False)
    # get the zip file as a dict keyed by file name
    archive = util.decompress(
        util.wrap_as_stream(blob), 'zip')
    # get a resolver object for accessing the schema
    resolver = ZipResolver(archive)
    # remove references to other files in the schema and load
    schema = json.loads(
        resolve_json(
            resolver.get('glTF.schema.json').decode('utf-8'),
            resolver=resolver))
    return schema


# exporters
_gltf_loaders = {"glb": load_glb,
                 "gltf": load_gltf}
