"""Regenerate docs/img/*.png and example/vestidor-example.FCStd.

Runs inside the FreeCAD GUI. Headless example (from the repo root), using a
throwaway FreeCAD profile so your own settings are left alone:

    FREECAD_USER_HOME=$(mktemp -d) FB_REPO=$PWD \
        xvfb-run -a -s "-screen 0 1600x1000x24" freecad tools/make_screenshots.py

Result (or traceback) is written to docs/screenshots.log.
"""

import os
import sys
import time

import FreeCAD as App
import FreeCADGui as Gui
import Part
from PySide import QtCore, QtGui

REPO = os.environ["FB_REPO"]
IMG = os.path.join(REPO, "docs", "img")
sys.path.insert(0, REPO)
import furniture_boards  # noqa: E402

V = App.Vector


def pump(seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        QtGui.QApplication.processEvents()
        time.sleep(0.02)


def set_view(view):
    v = Gui.ActiveDocument.ActiveView
    {"iso": v.viewIsometric, "front": v.viewFront}[view]()
    v.fitAll()
    pump()
    return v


def shot(name, view="iso"):
    set_view(view).saveImage(os.path.join(IMG, name), 1200, 800, "White")


def widget_shot(name, widget):
    pump(1.5)
    widget.grab().save(os.path.join(IMG, name))


def run_macro(name):
    with open(os.path.join(REPO, name)) as f:
        exec(compile(f.read(), name, "exec"), {"__name__": "__main__"})


def build_example():
    doc = App.newDocument("Vestidor")
    body = doc.addObject("PartDesign::Body", "Body")
    xz = [f for f in body.Origin.OriginFeatures if f.Role == "XZ_Plane"][0]
    sk = body.newObject("Sketcher::SketchObject", "Sketch")
    sk.AttachmentSupport = [(xz, "")]
    sk.MapMode = "FlatFace"

    def line(x1, y1, x2, y2):
        sk.addGeometry(Part.LineSegment(V(x1, y1, 0), V(x2, y2, 0)))

    # Two 600 x 1900 cabinets drawn "the lazy way": overlapping lines,
    # T-junctions, nothing trimmed.
    t, w, h = 15, 600, 1900
    for x0 in (-1000, 400):
        line(x0, 0, x0 + w, 0)
        line(x0 + w, 0, x0 + w, h)
        line(x0 + w, h, x0, h)
        line(x0, h, x0, 0)
        line(x0 + t, 0, x0 + t, h)
        line(x0 + w - t, 0, x0 + w - t, h)
        for y in (t, h - t, 265, 280, 670, 685, 1075, 1090, 1480, 1495):
            line(x0 + t, y, x0 + w - t, y)

    back = body.newObject("PartDesign::Plane", "BackPlane")
    back.Label = "BackPlane"
    back.AttachmentSupport = [(xz, "")]
    back.MapMode = "FlatFace"
    back.AttachmentOffset = App.Placement(V(0, 0, -500), App.Rotation())
    doc.recompute()
    return doc, body, sk, xz, back


def main():
    status = "did not finish\n"
    try:
        App.ParamGet("User parameter:BaseApp/Preferences/View").SetBool(
            "UseNavigationAnimations", False)
        mw = Gui.getMainWindow()
        mw.resize(1600, 1000)
        mw.showMaximized()
        mdi = mw.findChild(QtGui.QMdiArea)

        doc, body, sk, xz, back = build_example()
        gdoc = Gui.getDocument(doc.Name)
        Gui.activateWorkbench("PartWorkbench")
        dock = [d for d in mw.findChildren(QtGui.QDockWidget) if d.isVisible()
                and d.findChild(QtGui.QTreeWidget) is not None][0]
        mw.resizeDocks([dock], [480], QtCore.Qt.Horizontal)

        back.ViewObject.Visibility = False
        shot("01-sketch.png", "front")

        body.Origin.ViewObject.Visibility = True
        for f in body.Origin.OriginFeatures:
            f.ViewObject.Visibility = f is xz
        back.ViewObject.Visibility = True
        shot("02-planes.png")

        Gui.Selection.clearSelection()
        for o in (sk, xz, back):
            Gui.Selection.addSelection(o)
        run_macro("MakeBoards.FCMacro")
        cells, boards = doc.getObject("Cells"), doc.getObject("Boards")
        Gui.Selection.clearSelection()

        for o in (sk, xz, back, boards, body, body.Origin):
            o.ViewObject.Visibility = False
        cells.ViewObject.Visibility = True
        cells.ViewObject.Transparency = 60
        shot("03-cells.png")
        cells.ViewObject.Transparency = 85

        cells.ViewObject.Visibility = False
        boards.ViewObject.Visibility = True
        shot("04-boards.png")

        # Pick the top face of the second shelf from the bottom, left cabinet.
        shelf = [s for s in boards.Shape.Solids
                 if abs(s.BoundBox.ZMin - 670) < 1e-6 and s.BoundBox.XMin < 0][0]
        idx = [i for i, f in enumerate(boards.Shape.Faces)
               if f.BoundBox.ZLength < 1e-6 and abs(f.BoundBox.ZMin - 685) < 1e-6
               and any(f.isSame(g) for g in shelf.Faces)][0]
        Gui.Selection.addSelection(doc.Name, "Boards", "Face%d" % (idx + 1))
        set_view("iso")
        widget_shot("05-toggle-select.png", mdi)
        run_macro("ToggleBoard.FCMacro")
        Gui.Selection.clearSelection()
        shot("06-toggle-result.png")

        widget_shot("07-window.png", mw)
        Gui.Selection.addSelection(boards)
        widget_shot("08-properties.png", dock)
        Gui.Selection.clearSelection()

        os.makedirs(os.path.join(REPO, "example"), exist_ok=True)
        doc.saveAs(os.path.join(REPO, "example", "vestidor-example.FCStd"))
        status = "OK boards=%d cells=%d\n" % (boards.BoardCount, cells.CellCount)
    except Exception:
        import traceback
        status = traceback.format_exc()
    finally:
        with open(os.path.join(IMG, "..", "screenshots.log"), "w") as f:
            f.write(status)
        os._exit(0)


QtCore.QTimer.singleShot(1500, main)
