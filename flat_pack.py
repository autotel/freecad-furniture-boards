"""Lay boards flat, one sheet per thickness.

FlatPack  -- (App::FeaturePython) takes any objects with solids as Sources:
             Boards, exploded compound pieces, fusions of pieces... Every
             solid is a piece. Each piece is turned so its biggest flat side
             faces up and its longest edge runs along X, then pieces are
             grouped by thickness and laid out in rows in front of the model.
FlatSheet -- (Part::FeaturePython) the laid-out pieces of one thickness.

Both recompute when the sources change. If the set of thicknesses changes,
FlatPack.Warnings says so; running LayFlat.FCMacro again adds/removes the
sheets (it reuses the same FlatPack, never duplicates it).
"""

import FreeCAD as App
import Part

V = App.Vector
NOT_FLAT = -1.0  # thickness key for pieces that are not flat boards


def _label(t):
    return "Not flat" if t == NOT_FLAT else "Flat %g mm" % t


def _round_t(t):
    return round(t, 1)


def _canon(v):
    """Unit normal with a fixed sign, so opposite faces share a key."""
    v = V(v)
    v.normalize()
    for c in (v.x, v.y, v.z):
        if abs(c) > 1e-9:
            return v if c > 0 else v * -1
    return v


def flatten(solid):
    """Return (flat solid at the origin, warning or None), or (None, reason)."""
    solid = solid.removeSplitter()  # merge faces split where other boards touched
    areas = {}
    for f in solid.Faces:
        if isinstance(f.Surface, Part.Plane):
            n = _canon(f.Surface.Axis)
            k = tuple(round(c, 4) for c in n)
            total, _ = areas.get(k, (0.0, n))
            areas[k] = (total + f.Area, n)
    if not areas:
        return None, "no flat faces"
    n = max(areas.values(), key=lambda a: a[0])[1]

    u, longest = None, 0.0
    for e in solid.Edges:
        if isinstance(e.Curve, Part.Line):
            d = e.Vertexes[-1].Point - e.Vertexes[0].Point
            if d.Length > longest and abs(d.normalize().dot(n)) < 1e-6:
                u, longest = V(d), e.Length
    if u is None:
        u = n.cross(V(1, 0, 0)) if abs(n.x) < 0.9 else n.cross(V(0, 1, 0))
        u.normalize()
    v = n.cross(u)

    rot = App.Rotation(u, v, n, "ZXY").inverted()
    s = solid.transformed(App.Placement(V(), rot).toMatrix())
    bb = s.BoundBox
    if bb.YLength > bb.XLength + 1e-6:
        s = s.transformed(App.Placement(V(), App.Rotation(V(0, 0, 1), 90)).toMatrix())
        bb = s.BoundBox
    s.translate(V(-bb.XMin, -bb.YMin, -bb.ZMin))

    t = s.BoundBox.ZLength
    face = max((f for f in s.Faces if isinstance(f.Surface, Part.Plane)
                and abs(abs(_canon(f.Surface.Axis).z) - 1) < 1e-6),
               key=lambda f: f.Area, default=None)
    flat = face is not None and abs(s.Volume - face.Area * t) <= 0.01 * s.Volume
    return s, None if flat else "not a flat board, laid on its biggest side"


def leaf_sources(objs):
    """Drop objects that another listed object is built from (no double counting)."""
    objs = list(dict.fromkeys(o for o in objs if o is not None))
    return [o for o in objs
            if not any(o in p.OutListRecursive for p in objs if p is not o)]


def is_owned(obj):
    """True for FlatPack/FlatSheet objects (never used as sources)."""
    return type(getattr(obj, "Proxy", None)).__name__ in ("FlatPack", "FlatSheet")


def compute(pack):
    """Lay out every piece. Returns {thickness: (shape, piece lines)} and warnings."""
    pieces, warnings = [], []
    model_bb = App.BoundBox()
    for obj in leaf_sources(pack.Sources):
        solids = Part.getShape(obj).Solids
        for i, solid in enumerate(solids):
            model_bb.add(solid.BoundBox)
            name = obj.Label if len(solids) == 1 else "%s.%d" % (obj.Label, i + 1)
            flat, warn = flatten(solid)
            if flat is None:
                warnings.append("%s: %s, skipped" % (name, warn))
                continue
            if warn:
                warnings.append("%s: %s" % (name, warn))
            pieces.append((name, flat, NOT_FLAT if warn else _round_t(flat.BoundBox.ZLength)))

    groups = {}
    for name, s, t in pieces:
        groups.setdefault(t, []).append((name, s))

    row_length, gap = pack.RowLength.Value, pack.Gap.Value
    x0 = model_bb.XMin if model_bb.isValid() else 0.0
    y_top = (model_bb.YMin if model_bb.isValid() else 0.0) - pack.GroupGap.Value
    result = {}
    for t in sorted(groups, key=lambda t: (t == NOT_FLAT, t)):
        items = sorted(groups[t], key=lambda p: (-p[1].BoundBox.YLength,
                                                 -p[1].BoundBox.XLength, p[0]))
        placed, lines = [], []
        x = y = row_h = 0.0
        for name, s in items:
            bb = s.BoundBox
            if x > 0 and x + bb.XLength > row_length:
                x, y, row_h = 0.0, y + row_h + gap, 0.0
            placed.append((name, s, x, y))
            x += bb.XLength + gap
            row_h = max(row_h, bb.YLength)
        height = y + row_h
        origin = V(x0, y_top - height, 0)
        shapes = []
        for k, (name, s, px, py) in enumerate(placed, 1):
            shapes.append(s.translated(origin + V(px, py, 0)))
            bb = s.BoundBox
            lines.append("%d: %s  %.1f x %.1f  at (%.0f, %.0f)"
                         % (k, name, bb.XLength, bb.YLength, px, py))
        result[t] = (Part.Compound(shapes), lines)
        y_top -= height + pack.GroupGap.Value
    return result, warnings, len(pieces)


def sheets_of(pack):
    return [o for o in pack.InList if type(getattr(o, "Proxy", None)).__name__ == "FlatSheet"]


class FlatPack:
    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkListGlobal", "Sources", "FlatPack",
                        "Objects whose solids are the pieces")
        obj.addProperty("App::PropertyLength", "RowLength", "FlatPack",
                        "Start a new row when a row gets longer than this")
        obj.addProperty("App::PropertyLength", "Gap", "FlatPack", "Space between pieces")
        obj.addProperty("App::PropertyLength", "GroupGap", "FlatPack",
                        "Space between the model and each thickness group")
        obj.addProperty("App::PropertyFloatList", "Thicknesses", "Result",
                        "Thicknesses found (mm); -1 = pieces that are not flat boards", 1)
        obj.addProperty("App::PropertyInteger", "PieceCount", "Result", "Number of pieces", 1)
        obj.addProperty("App::PropertyStringList", "Warnings", "Result",
                        "Pieces that need attention, missing/obsolete sheets", 1)
        obj.RowLength = 2500
        obj.Gap = 20
        obj.GroupGap = 300

    def execute(self, obj):
        self.result, warnings, count = compute(obj)
        obj.Thicknesses = sorted(self.result)
        obj.PieceCount = count
        have = {_round_t(s.Thickness) for s in sheets_of(obj)}
        for t in sorted(set(self.result) - have):
            warnings.append("%s: no sheet yet, run LayFlat again" % _label(t))
        for t in sorted(have - set(self.result)):
            warnings.append("%s: no pieces left, run LayFlat again to remove that sheet" % _label(t))
        obj.Warnings = warnings

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None


class FlatSheet:
    def __init__(self, obj, pack, thickness):
        obj.Proxy = self
        obj.addProperty("App::PropertyLink", "Pack", "FlatSheet", "FlatPack this sheet belongs to")
        obj.addProperty("App::PropertyFloat", "Thickness", "FlatSheet",
                        "Board thickness (mm); -1 = pieces that are not flat boards", 1)
        obj.addProperty("App::PropertyInteger", "PieceCount", "Result", "Number of pieces", 1)
        obj.addProperty("App::PropertyStringList", "Pieces", "Result",
                        "n: source  length x width  at (x, y) from the sheet corner", 1)
        obj.Pack = pack
        obj.Thickness = thickness

    def execute(self, obj):
        pack = obj.Pack
        result = getattr(pack.Proxy, "result", None)
        if result is None:
            result = compute(pack)[0]
        shape, lines = result.get(_round_t(obj.Thickness), (None, []))
        obj.Shape = shape if shape is not None else Part.Shape()
        obj.PieceCount = len(lines)
        obj.Pieces = lines or ["no pieces of this thickness: run LayFlat again"]

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None


def lay_flat(sources):
    """Create or update the document's FlatPack and its sheets. Safe to run repeatedly."""
    doc = sources[0].Document
    pack = next((o for o in doc.Objects
                 if type(getattr(o, "Proxy", None)).__name__ == "FlatPack"), None)
    if pack is None:
        pack = doc.addObject("App::FeaturePython", "FlatPack")
        FlatPack(pack)
        group = doc.addObject("App::DocumentObjectGroup", "FlatPackGroup")
        group.Label = "Flat pack"
        group.addObject(pack)
    group = pack.getParentGroup()

    pack.Sources = leaf_sources([s for s in sources if not is_owned(s)])
    pack.recompute()

    wanted = set(pack.Thicknesses)
    for sheet in sheets_of(pack):
        t = _round_t(sheet.Thickness)
        if t in wanted:
            wanted.discard(t)
        else:
            doc.removeObject(sheet.Name)
    for t in sorted(wanted):
        sheet = doc.addObject("Part::FeaturePython", "FlatSheet")
        FlatSheet(sheet, pack, t)
        sheet.Label = _label(t)
        if group is not None:
            group.addObject(sheet)
        if App.GuiUp:
            sheet.ViewObject.Proxy = 0
    pack.touch()
    doc.recompute()
    return pack
