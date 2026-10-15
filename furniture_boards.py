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

BoardPiece -- one board of a Boards object, made by ExplodeBoards.FCMacro.
          It finds its board by key (the tool surfaces enclosing it), not
          by position, so adding/removing boards never mixes pieces up.

All of them recompute when any tool changes.
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


def _tool_faces(tools, bb_out):
    """Yield (face or None, key, kind, data) for every tool; collects bounds in bb_out."""
    out = []
    for t in tools:
        if _is_plane(t):
            pl = _global_placement(t)
            out.append(("plane", t.Name, (pl.Base, pl.Rotation.multVec(App.Vector(0, 0, 1)))))
            bb_out.add(pl.Base)
        elif t.isDerivedFrom("Sketcher::SketchObject"):
            gp = _global_placement(t)
            n = gp.Rotation.multVec(App.Vector(0, 0, 1))
            for i, g in enumerate(t.Geometry):
                if t.getConstruction(i) or isinstance(g, Part.Point):
                    continue
                e = g.toShape()
                e.Placement = gp
                # Geometry ids survive edits and deletions of other lines.
                gid = t.getGeometryId(i) if hasattr(t, "getGeometryId") else i
                out.append(("edge", "%s:%s" % (t.Name, gid), (e, n)))
                bb_out.add(e.BoundBox)
        else:
            for k, f in enumerate(Part.getShape(t).Faces):
                out.append(("face", "%s:F%d" % (t.Name, k + 1), f))
                bb_out.add(f.BoundBox)
    return out


def compute_cells(tools):
    """Return (closed solids enclosed by the tools, one key per solid).

    A key lists the tool surfaces that enclose the solid, e.g.
    "BackPlane|Sketch:12|Sketch:13|Sketch:7|Sketch:9|XZ_Plane". It stays the
    same when lines move or other lines are added, so it identifies a board.
    """
    bb = App.BoundBox()
    tools_ = _tool_faces(tools, bb)
    if not bb.isValid():
        return [], []
    margin = max(bb.DiagonalLength, 1.0)
    bb.enlarge(margin)
    size = bb.DiagonalLength
    center = bb.Center

    # Unbounded tools become big finite faces covering the box.
    faces, keys = [], []
    for kind, key, data in tools_:
        if kind == "edge":
            e, n = data
            faces.append(e.translated(n * -size).extrude(n * (2 * size)))
        elif kind == "plane":
            p, n = data
            c = center - n * (center - p).dot(n)  # project box center onto plane
            u = n.cross(App.Vector(1, 0, 0))
            if u.Length < 1e-6:
                u = n.cross(App.Vector(0, 1, 0))
            u.normalize()
            faces.append(Part.makePlane(2 * size, 2 * size, c - u * size - n.cross(u) * size, n, u))
        else:
            faces.append(data)
        keys.append(key)

    box = Part.makeBox(bb.XLength, bb.YLength, bb.ZLength,
                       App.Vector(bb.XMin, bb.YMin, bb.ZMin))
    result, pieces_map = box.generalFuse(faces, 1e-7)
    owner = {}
    for key, pieces in zip(keys, pieces_map[1:]):
        for piece in pieces:
            for f in piece.Faces:
                owner[f.hashCode()] = key

    tol = margin * 1e-6
    cells, cell_keys = [], []
    for s in result.Solids:
        sb = s.BoundBox
        open_ = (sb.XMin <= bb.XMin + tol or sb.XMax >= bb.XMax - tol or
                 sb.YMin <= bb.YMin + tol or sb.YMax >= bb.YMax - tol or
                 sb.ZMin <= bb.ZMin + tol or sb.ZMax >= bb.ZMax - tol)
        if not open_:
            cells.append(s)
            cell_keys.append("|".join(sorted({owner.get(f.hashCode(), "?") for f in s.Faces})))

    # Rare: two volumes enclosed by the same surfaces. Number them by position.
    for key in {k for k in cell_keys if cell_keys.count(k) > 1}:
        idx = [i for i, k in enumerate(cell_keys) if k == key]
        idx.sort(key=lambda i: tuple(round(c, 3) for c in cells[i].CenterOfMass))
        for n, i in enumerate(idx, 1):
            cell_keys[i] = "%s#%d" % (key, n)
    return cells, cell_keys


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
        self.add_new_props(obj)
        obj.Tools = tools

    def add_new_props(self, obj):
        if "CellKeys" not in obj.PropertiesList:
            obj.addProperty("App::PropertyStringList", "CellKeys", "Result",
                            "Tool surfaces enclosing each volume (its identity)", 1)

    def execute(self, obj):
        cells, keys = compute_cells(obj.Tools)
        obj.Shape = Part.Compound(cells) if cells else Part.Shape()
        obj.CellKeys = keys
        obj.CellCount = len(cells)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.add_new_props(obj)

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
        self.add_new_props(obj)
        obj.Source = cells
        obj.Base = ["Thin", "All", "None"]
        obj.Base = "Thin"
        obj.MaxThickness = 30

    def add_new_props(self, obj):
        if "BoardKeys" not in obj.PropertiesList:
            obj.addProperty("App::PropertyStringList", "BoardKeys", "Result",
                            "Identity of each board (see Cells.CellKeys)", 1)
        if "Warnings" not in obj.PropertiesList:
            obj.addProperty("App::PropertyStringList", "Warnings", "Result",
                            "Boards that have no BoardPiece yet", 1)

    def in_base(self, obj, solid):
        if obj.Base == "All":
            return True
        if obj.Base == "Thin":
            return _dims(solid)[2] <= obj.MaxThickness.Value + 1e-6
        return False

    def execute(self, obj):
        src = obj.Source
        cells = src.Shape.Solids if src and not src.Shape.isNull() else []
        cell_keys = list(getattr(src, "CellKeys", []))
        if len(cell_keys) != len(cells):
            cell_keys = [""] * len(cells)
        kept, keys, cuts = [], [], []
        for s, key in zip(cells, cell_keys):
            # On-face counts too: if a toggled board is split through its
            # point, both halves stay toggled.
            flipped = sum(1 for p in obj.Toggled if s.isInside(p, 1e-6, True)) % 2
            if self.in_base(obj, s) != bool(flipped):
                kept.append(s)
                keys.append(key)
                cuts.append("%d: %.1f x %.1f x %.1f" % (len(kept), *_dims(s)))
        obj.Shape = Part.Compound(kept) if kept else Part.Shape()
        obj.BoardKeys = keys
        obj.BoardCount = len(kept)
        obj.CutList = cuts

        # Pieces recompute after this, and a piece whose key vanished re-attaches
        # to the board around its anchor, so don't count those boards as missing.
        pieces = board_pieces(obj)
        have = {p.Key for p in pieces}
        anchors = [p.Anchor for p in pieces if p.Key not in keys]
        missing = sum(1 for k, s in zip(keys, kept)
                      if k not in have and not any(s.isInside(a, 1e-6, True) for a in anchors))
        obj.Warnings = (["%d board(s) have no piece: run ExplodeBoards again" % missing]
                        if missing else [])
        if missing:
            App.Console.PrintWarning("%s: %s\n" % (obj.Label, obj.Warnings[0]))

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.add_new_props(obj)

    def dumps(self):
        return None

    def loads(self, state):
        return None


class BoardPiece:
    """One board of a Boards object, found by its key (not by its position)."""

    def __init__(self, obj, boards, key, anchor):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Source", "BoardPiece", "Boards object")
        obj.addProperty("App::PropertyString", "Key", "BoardPiece",
                        "Identity of the board (tool surfaces enclosing it)")
        obj.addProperty("App::PropertyVector", "Anchor", "BoardPiece",
                        "Point inside the board, used if the key disappears", 1)
        obj.addProperty("App::PropertyString", "Status", "Result", "", 1)
        obj.Source = boards
        obj.Key = key
        obj.Anchor = anchor

    def execute(self, obj):
        src = obj.Source
        solids = src.Shape.Solids if src and not src.Shape.isNull() else []
        keys = list(src.BoardKeys) if src else []
        i = keys.index(obj.Key) if obj.Key in keys else None
        if i is None:
            # Key gone (e.g. a line was deleted and redrawn): follow the anchor,
            # unless another piece already owns that board.
            # First strictly inside, then also on the surface (e.g. the board
            # was split exactly through the anchor).
            taken = {p.Key for p in board_pieces(src) if p is not obj}
            for on_face in (False, True):
                i = next((j for j, s in enumerate(solids)
                          if j < len(keys) and keys[j] not in taken
                          and s.isInside(obj.Anchor, 1e-6, on_face)), None)
                if i is not None:
                    obj.Key = keys[i]
                    break
        if i is None:
            obj.Shape = Part.Shape()
            obj.Status = "Board no longer exists"
            App.Console.PrintWarning("%s: board no longer exists in %s\n" % (obj.Label, src.Label))
            return
        obj.Shape = solids[i].copy()
        obj.Anchor = interior_point(solids[i])
        obj.Status = "OK"

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None


def board_pieces(boards):
    return [o for o in boards.InList if type(getattr(o, "Proxy", None)).__name__ == "BoardPiece"]


def _replace_links(old, new):
    """Point every Link/LinkList property that uses `old` to `new`. Returns False if some can't."""
    ok = True
    for user in old.InList:
        if user is new or user.isDerivedFrom("App::DocumentObjectGroup"):
            continue
        for prop in user.PropertiesList:
            t = user.getTypeIdOfProperty(prop)
            val = getattr(user, prop)
            if t.startswith("App::PropertyLinkList"):
                if old in val:
                    setattr(user, prop, [new if v == old else v for v in val])
            elif t.startswith("App::PropertyLink") and "Sub" not in t:
                if val == old:
                    setattr(user, prop, new)
        if old in user.OutList and user is not new:
            ok = False
    return ok


def explode(boards):
    """Create a BoardPiece for every board that has none. Safe to run repeatedly.

    Also converts pieces made by Part > Explode compound (which track boards
    by position and get mixed up when boards are added or removed).
    Returns (created, converted, lost) piece lists.
    """
    doc = boards.Document
    if len(boards.BoardKeys) != len(boards.Shape.Solids):  # file from an older version
        boards.Source.touch()
        doc.recompute()
    keys, solids = list(boards.BoardKeys), boards.Shape.Solids
    pieces = board_pieces(boards)
    group = next((g for g in (p.getParentGroup() for p in pieces) if g), None)
    if group is None:
        group = doc.addObject("App::DocumentObjectGroup", "BoardPieces")
        group.Label = "%s pieces" % boards.Label

    def add(key, solid):
        piece = doc.addObject("Part::FeaturePython", "BoardPiece")
        BoardPiece(piece, boards, key, interior_point(solid))
        piece.Label = "%s.%d" % (boards.Label, len(board_pieces(boards)))
        group.addObject(piece)
        if App.GuiUp:
            piece.ViewObject.Proxy = 0
        return piece

    # Convert Explode-compound children (CompoundFilter on boards).
    converted = []
    have = {p.Key for p in pieces}
    for old in [o for o in boards.InList if getattr(o, "FilterType", None) == "specific items"
                and getattr(o, "Base", None) == boards]:
        sol = old.Shape.Solids
        j = next((j for j, s in enumerate(solids)
                  if sol and s.isInside(interior_point(sol[0]), 1e-7, False)), None)
        if j is None or keys[j] in have:
            continue
        piece = add(keys[j], solids[j])
        piece.Label = old.Label
        have.add(keys[j])
        if _replace_links(old, piece):
            old_group = old.getParentGroup()
            doc.removeObject(old.Name)
            if old_group is not None and not old_group.Group:
                doc.removeObject(old_group.Name)
        converted.append(piece)

    created = [add(k, s) for k, s in zip(keys, solids) if k not in have]
    if App.GuiUp:
        boards.ViewObject.Visibility = False
    boards.touch()
    doc.recompute()
    lost = [p for p in board_pieces(boards) if p.Status != "OK"]
    return created, converted, lost


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
