import shutil
import subprocess
import tempfile
from pathlib import Path

import meshio
import numpy


def rotation_matrix(u, theta):
    """Return matrix that implements the rotation around the vector :math:`u`
    by the angle :math:`\\theta`, cf.
    https://en.wikipedia.org/wiki/Rotation_matrix#Rotation_matrix_from_axis_and_angle.

    :param u: rotation vector
    :param theta: rotation angle
    """
    assert numpy.isclose(numpy.inner(u, u), 1.0), "the rotation axis must be unitary"

    # Cross-product matrix.
    cpm = numpy.array([[0.0, -u[2], u[1]], [u[2], 0.0, -u[0]], [-u[1], u[0], 0.0]])
    c = numpy.cos(theta)
    s = numpy.sin(theta)
    R = numpy.eye(3) * c + s * cpm + (1.0 - c) * numpy.outer(u, u)
    return R


def orient_lines(lines):
    """Given a sequence of unordered and unoriented lines defining a closed polygon,
    returns a reordered list of reoriented lines of that polygon.

    :param lines: a sequence of lines defining a closed polygon
    """
    # Categorise graph edges by their vertex pair ids
    point_pair_ids = numpy.array(
        [[line.points[0].id, line.points[1].id] for line in lines]
    )

    # Indices of reordering
    order = numpy.arange(len(point_pair_ids), dtype=int)
    # Compute orientations where oriented[j] == False requires edge j to be reversed
    oriented = numpy.array([True] * len(point_pair_ids), dtype=numpy.bool)

    for j in range(1, len(point_pair_ids)):
        out = point_pair_ids[j - 1, 1]  # edge out from vertex
        inn = point_pair_ids[j:, 0]  # candidates for edge into vertices
        wh = numpy.where(inn == out)[0] + j
        if len(wh) == 0:
            # look for candidates in those which are not correctly oriented
            inn = point_pair_ids[j:, 1]
            wh = numpy.where(inn == out)[0] + j
            # reorient remaining edges
            point_pair_ids[j:] = numpy.flip(point_pair_ids[j:], axis=1)
            oriented[j:] ^= True

        # reorder
        point_pair_ids[[j, wh[0]]] = point_pair_ids[[wh[0], j]]
        order[[j, wh[0]]] = order[[wh[0], j]]

    # Reconstruct an ordered and oriented line loop
    lines = [lines[o] for o in order]
    lines = [lines[j] if oriented[j] else -lines[j] for j in range(len(oriented))]

    return lines


def _get_gmsh_exe():
    macos_gmsh_location = Path("/Applications/Gmsh.app/Contents/MacOS/gmsh")
    return macos_gmsh_location if macos_gmsh_location.is_file() else shutil.which("gmsh")


def get_gmsh_version(gmsh_exe=_get_gmsh_exe()):
    gmsh_exe = Path(gmsh_exe)
    return (
        subprocess.check_output(
            [gmsh_exe.as_posix(), "--version"], stderr=subprocess.STDOUT
        )
        .strip()
        .decode("utf8")
    )


def get_gmsh_major_version(gmsh_exe=_get_gmsh_exe()):
    return int(get_gmsh_version(gmsh_exe=gmsh_exe).split(".")[0])


def generate_mesh(  # noqa: C901
    geo_object,
    verbose=True,
    dim=3,
    prune_vertices=True,
    prune_z_0=False,
    remove_lower_dim_cells=False,
    gmsh_path=None,
    extra_gmsh_arguments=None,
    # for debugging purposes:
    geo_filename=None,
    msh_filename=None,
    mesh_file_type="msh",
):
    """Return a meshio.Mesh, storing the mesh points, cells, and data, generated by Gmsh
    from the `geo_object`, written to a temporary file, and reread by `meshio`.

    Gmsh's native "msh" format is ill-suited to fast I/O.  This can greatly reduce the
    performance of pygmsh.  As alternatives, try `mesh_file_type=`:

    - "vtk"`, though Gmsh doesn't write the physical tags to VTK
      <https://gitlab.onelab.info/gmsh/gmsh/issues/389> or

    - `"mesh"`, though this only supports a few basic elements - "line", "triangle",
      "quad", "tetra", "hexahedron" - and doesn't preserve the `$PhysicalNames`, just
      the `int` tags.
    """
    if extra_gmsh_arguments is None:
        extra_gmsh_arguments = []

    # For format "mesh", ask Gmsh to save the physical tags
    # http://gmsh.info/doc/texinfo/gmsh.html#index-Mesh_002eSaveElementTagType
    if mesh_file_type == "mesh":
        extra_gmsh_arguments += ["-string", "Mesh.SaveElementTagType=2;"]

    preserve_geo = geo_filename is not None
    if geo_filename is None:
        with tempfile.NamedTemporaryFile(suffix=".geo") as f:
            geo_filename = f.name

    with open(geo_filename, "w") as f:
        f.write(geo_object.get_code())

    # As of Gmsh 4.1.3, the mesh format options are
    # ```
    # auto, msh1, msh2, msh3, msh4, msh, unv, vtk, wrl, mail, stl, p3d, mesh, bdf, cgns,
    # med, diff, ir3, inp, ply2, celum, su2, x3d, dat, neu, m, key
    # ```
    # Pick the correct filename suffix.
    filename_suffix = "msh" if mesh_file_type[:3] == "msh" else mesh_file_type

    preserve_msh = msh_filename is not None
    if msh_filename is None:
        with tempfile.NamedTemporaryFile(suffix="." + filename_suffix) as handle:
            msh_filename = handle.name

    gmsh_executable = gmsh_path if gmsh_path is not None else _get_gmsh_exe()

    args = [
        f"-{dim}",
        geo_filename,
        "-format",
        mesh_file_type,
        "-bin",
        "-o",
        msh_filename,
    ] + extra_gmsh_arguments

    # https://stackoverflow.com/a/803421/353337
    try:
        p = subprocess.Popen(
            [gmsh_executable] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
    except FileNotFoundError:
        print("Is gmsh installed?")
        raise

    if verbose:
        while True:
            line = p.stdout.readline()
            if not line:
                break
            print(line.decode("utf-8"), end="")

    p.communicate()
    assert p.returncode == 0, "Gmsh exited with error (return code {}).".format(
        p.returncode
    )

    mesh = meshio.read(msh_filename)

    if remove_lower_dim_cells:
        # Only keep the cells of highest topological dimension; discard faces and such.
        cells_2d = {"triangle", "quad"}
        cells_3d = {
            "tetra",
            "hexahedron",
            "wedge",
            "pyramid",
            "penta_prism",
            "hexa_prism",
        }
        if any(c.type in cells_3d for c in mesh.cells):
            keep_types = cells_3d
        elif any(c.type in cells_2d for c in mesh.cells):
            keep_types = cells_2d
        else:
            keep_types = set(cell_type for cell_type, _ in mesh.cells)

        for name, val in mesh.cell_data.items():
            mesh.cell_data[name] = [
                d for d, c in zip(val, mesh.cells) if c[0] in keep_types
            ]
        mesh.cells = [c for c in mesh.cells if c[0] in keep_types]

    if prune_vertices:
        # Make sure to include only those vertices which belong to a cell.
        ncells = numpy.concatenate([numpy.concatenate(c) for _, c in mesh.cells])
        uvertices, uidx = numpy.unique(ncells, return_inverse=True)

        k = 0
        cells = []
        for key, cellblock in mesh.cells:
            n = numpy.prod(cellblock.shape)
            cells.append(
                meshio.CellBlock(key, uidx[k : k + n].reshape(cellblock.shape))
            )
            k += n
        mesh.cells = cells

        mesh.points = mesh.points[uvertices]
        for key in mesh.point_data:
            mesh.point_data[key] = mesh.point_data[key][uvertices]

    # clean up
    if preserve_msh:
        print(f"\nmsh file: {msh_filename}")
    else:
        Path(msh_filename).unlink()
    if preserve_geo:
        print(f"\ngeo file: {geo_filename}")
    else:
        Path(geo_filename).unlink()

    if (
        prune_z_0
        and mesh.points.shape[1] == 3
        and numpy.all(numpy.abs(mesh.points[:, 2]) < 1.0e-13)
    ):
        mesh.points = mesh.points[:, :2]

    return mesh
