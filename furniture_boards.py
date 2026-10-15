"""Parametric boards: split space with surfaces, keep the volumes you want.

Cells  -- takes a list of "tools" and computes every closed volume they enclose:
          * a sketch    -> its lines are extruded as (unbounded) surfaces
                           along the sketch normal
          * a plane     -> (XZ_Plane, datum/offset plane...) unbounded plane
          * anything else with faces (Part Extrude, etc.) -> its faces as-is
          Volumes not fully enclosed by tools are discarded.
Boards -- picks volumes out of a Cells object:
          Base = Thin  -> volumes whose smallest side <= MaxThickness
          Base = All   -> every volume
          Base = None  -> nothing
          Volumes toggled with ToggleBoard.FCMacro are flipped in/out of
          that base set. Toggles are stored as 3D points, so they survive
          design changes as long as the point stays inside the same board.

Both recompute when any tool changes.
"""

import FreeCAD as App
import Part

PLANE_TYPES = ("App::Plane", "PartDesign::Plane", "Part::DatumPlane")


def _is_plane(obj):
    return any(obj.isDerivedFrom(t) for t in PLANE_TYPES)


def _global_placement(obj):
    try:
        return obj.getGlobalPlacement()
    except Exception:
        return obj.Placement


def compute_cells(tools):
    """Return the list of closed solids enclosed by the given tool objects."""
    planes, sketches, faces = [], [], []
    for t in tools:
        if _is_plane(t):
            pl = _global_placement(t)
            planes.append((pl.Base, pl.Rotation.multVec(App.Vector(0, 0, 1))))
        elif t.isDerivedFrom("Sketcher::SketchObject"):
            shape = Part.getShape(t)
            n = _global_placement(t).Rotation.multVec(App.Vector(0, 0, 1))
            sketches.append((shape.Edges, n))
        else:
            faces.extend(Part.getShape(t).Faces)

    # Bounding box of everything finite, plus plane origins.
    bb = App.BoundBox()
    for edges, _ in sketches:
        for e in edges:
            bb.add(e.BoundBox)
    for f in faces:
        bb.add(f.BoundBox)
    for p, _ in planes:
        bb.add(p)
    if not bb.isValid():
        return []
    margin = max(bb.DiagonalLength, 1.0)
    bb.enlarge(margin)
    size = bb.DiagonalLength
    center = bb.Center

    # Unbounded tools become big finite faces covering the box.
    for edges, n in sketches:
        for e in edges:
            faces.append(e.translated(n * -size).extrude(n * (2 * size)))
    for p, n in planes:
        c = center - n * (center - p).dot(n)  # project box center onto plane
        u = n.cross(App.Vector(1, 0, 0))
        if u.Length < 1e-6:
            u = n.cross(App.Vector(0, 1, 0))
        u.normalize()
        faces.append(Part.makePlane(2 * size, 2 * size, c - u * size - n.cross(u) * size, n, u))

    box = Part.makeBox(bb.XLength, bb.YLength, bb.ZLength,
                       App.Vector(bb.XMin, bb.YMin, bb.ZMin))
    result, _ = box.generalFuse(faces, 1e-7)

    tol = margin * 1e-6
    cells = []
    for s in result.Solids:
        sb = s.BoundBox
        open_ = (sb.XMin <= bb.XMin + tol or sb.XMax >= bb.XMax - tol or
                 sb.YMin <= bb.YMin + tol or sb.YMax >= bb.YMax - tol or
                 sb.ZMin <= bb.ZMin + tol or sb.ZMax >= bb.ZMax - tol)
        if not open_:
            cells.append(s)
    return cells


def interior_point(solid):
    """A point strictly inside the solid."""
    com = solid.CenterOfMass
    if solid.isInside(com, 1e-7, False):
        return com
    eps = min(solid.BoundBox.XLength, solid.BoundBox.YLength, solid.BoundBox.ZLength) / 4
    for f in solid.Faces:
        u, v = f.Surface.parameter(f.CenterOfMass)
        p = f.valueAt(u, v)
        n = f.normalAt(u, v)
        for q in (p - n * eps, p + n * eps):
            if solid.isInside(q, 1e-7, False):
                return q
    return com


def _dims(solid):
    b = solid.BoundBox
    return sorted([b.XLength, b.YLength, b.ZLength], reverse=True)


class Cells:
    def __init__(self, obj, tools):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkListGlobal", "Tools", "Cells",
                        "Sketches, planes and surfaces that split space")
        obj.addProperty("App::PropertyInteger", "CellCount", "Result",
                        "Number of closed volumes found", 1)
        obj.Tools = tools

    def execute(self, obj):
        cells = compute_cells(obj.Tools)
        obj.Shape = Part.Compound(cells) if cells else Part.Shape()
        obj.CellCount = len(cells)

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None


class Boards:
    def __init__(self, obj, cells):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Source", "Boards", "Cells object to pick from")
        obj.addProperty("App::PropertyEnumeration", "Base", "Boards",
                        "Starting set before toggles are applied")
        obj.addProperty("App::PropertyLength", "MaxThickness", "Boards",
                        "Base=Thin keeps volumes whose smallest side is <= this")
        obj.addProperty("App::PropertyVectorList", "Toggled", "Boards",
                        "Points inside volumes flipped in/out of the base set (use ToggleBoard)")
        obj.addProperty("App::PropertyInteger", "BoardCount", "Result", "Number of boards", 1)
        obj.addProperty("App::PropertyStringList", "CutList", "Result",
                        "Board dimensions (mm), one line per board", 1)
        obj.Source = cells
        obj.Base = ["Thin", "All", "None"]
        obj.Base = "Thin"
        obj.MaxThickness = 30

    def in_base(self, obj, solid):
        if obj.Base == "All":
            return True
        if obj.Base == "Thin":
            return _dims(solid)[2] <= obj.MaxThickness.Value + 1e-6
        return False

    def execute(self, obj):
        src = obj.Source
        cells = src.Shape.Solids if src and not src.Shape.isNull() else []
        kept, cuts = [], []
        for s in cells:
            flipped = sum(1 for p in obj.Toggled if s.isInside(p, 1e-7, False)) % 2
            if self.in_base(obj, s) != bool(flipped):
                kept.append(s)
                cuts.append("%d: %.1f x %.1f x %.1f" % (len(kept), *_dims(s)))
        obj.Shape = Part.Compound(kept) if kept else Part.Shape()
        obj.BoardCount = len(kept)
        obj.CutList = cuts

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None


def make(tools):
    doc = tools[0].Document
    cells = doc.addObject("Part::FeaturePython", "Cells")
    Cells(cells, tools)
    boards = doc.addObject("Part::FeaturePython", "Boards")
    Boards(boards, cells)
    if App.GuiUp:
        cells.ViewObject.Proxy = 0
        boards.ViewObject.Proxy = 0
        cells.ViewObject.Transparency = 85
    doc.recompute()
    return cells, boards


def toggle(obj, face_name):
    """Flip the volume owning `face_name` of `obj` (a Cells or Boards object)."""
    face = obj.Shape.getElement(face_name)
    owners = [s for s in obj.Shape.Solids if any(f.isSame(face) for f in s.Faces)]
    if len(owners) != 1:
        App.Console.PrintWarning("ToggleBoard: %s is shared by %d volumes, "
                                 "click an outside face instead\n" % (face_name, len(owners)))
        return
    solid = owners[0]
    if obj.Proxy.__class__.__name__ == "Boards":
        targets = [obj]
    else:
        targets = [o for o in obj.InList if getattr(o, "Source", None) == obj]
    for b in targets:
        pts = list(b.Toggled)
        inside = [p for p in pts if solid.isInside(p, 1e-7, False)]
        if inside:
            pts = [p for p in pts if p not in inside]
        else:
            pts.append(interior_point(solid))
        b.Toggled = pts
