"""
Redsail RS720C Studio v4.0
Interfaz profesional tipo FlexiSign 10
- Canvas interactivo con selección y movimiento de objetos
- Vista con relleno real de colores
- Soporte SVG y PDF
- Corte limpio HPGL/2 de alta precisión
- Reglas, grilla, zoom, pan
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
import threading, os, math, re, time, io, copy
import xml.etree.ElementTree as ET

try:
    from PIL import Image, ImageTk, ImageDraw
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import cairosvg
    CAIRO_OK = True
except ImportError:
    CAIRO_OK = False

try:
    import fitz  # PyMuPDF
    PYMUPDF_OK = True
except ImportError:
    PYMUPDF_OK = False

try:
    import serial, serial.tools.list_ports
    SERIAL_OK = True
except ImportError:
    SERIAL_OK = False

# ═══════════════════════════════════════════════════════
#  CONSTANTES FÍSICAS
# ═══════════════════════════════════════════════════════
MAX_MM         = 20_000.0
RS720C_W_MM    = 720.0
MM_TO_HPGL     = 40
INCH           = 25.4

def mm_hpgl(v): return int(round(v * MM_TO_HPGL))
def px_mm(px, dpi=96): return px * INCH / dpi
def to_mm(v, u): return {"mm":float(v),"cm":float(v)*10,"pulg":float(v)*INCH}.get(u, float(v))
def fr_mm(v, u): return {"mm":v,"cm":v/10,"pulg":v/INCH}.get(u, v)

# ═══════════════════════════════════════════════════════
#  PALETA — TEMA CLARO PROFESIONAL (FlexiSign style)
# ═══════════════════════════════════════════════════════
BG           = "#ECEEF2"
TOOLBAR_BG   = "#2C3E50"
TOOLBAR_BTN  = "#34495E"
TOOLBAR_ACT  = "#2980B9"
SIDEBAR_BG   = "#F7F8FA"
SIDEBAR_HDR  = "#2C3E50"
CANVAS_BG    = "#B0B8C8"
CANVAS_MAT   = "#FFFFFF"
RULER_BG     = "#D8DCE5"
RULER_FG     = "#5A6478"
PANEL_HDR    = "#3D4F63"
ACCENT       = "#2980B9"
ACCENT_H     = "#1A6090"
ACCENT2      = "#27AE60"
WARN         = "#E67E22"
DANGER       = "#C0392B"
SEL_COLOR    = "#2980B9"
SEL_HANDLE   = "#FFFFFF"
GRID_MAJOR   = "#D5DAE3"
GRID_MINOR   = "#EAECF0"
TEXT_DARK    = "#1A2332"
TEXT_MED     = "#4A5870"
TEXT_LIGHT   = "#8A95A8"
WHITE        = "#FFFFFF"
BORDER       = "#C8CDD8"
STATUS_BG    = "#2C3E50"

FN           = "Segoe UI"
FONT_UI      = (FN, 9)
FONT_BOLD    = (FN, 9, "bold")
FONT_SM      = (FN, 8)
FONT_TITLE   = (FN, 10, "bold")
FONT_MONO    = ("Consolas", 8)

# ═══════════════════════════════════════════════════════
#  PERFILES DE CUCHILLA
# ═══════════════════════════════════════════════════════
BLADE_PROFILES = {
    "Vinilo estándar 45°": {"speed":400,"force":80, "offset":0.25,"passes":1,"overcut":1.0,"mat":"Vinilo adhesivo 0.05–0.08 mm"},
    "Vinilo grueso 45°":   {"speed":300,"force":130,"offset":0.25,"passes":1,"overcut":1.2,"mat":"Vinilo piso 0.10–0.15 mm"},
    "Cuchilla 60°":        {"speed":350,"force":100,"offset":0.175,"passes":1,"overcut":0.8,"mat":"Detalles finos, texto pequeño"},
    "Textil / Flock":      {"speed":200,"force":200,"offset":0.30,"passes":2,"overcut":1.5,"mat":"Flock y transfer textil"},
    "Goma eva / Foam":     {"speed":150,"force":250,"offset":0.35,"passes":2,"overcut":2.0,"mat":"Foam hasta 2 mm"},
    "Papel / Cartulina":   {"speed":500,"force":60, "offset":0.20,"passes":1,"overcut":0.5,"mat":"Papel hasta 200 g/m²"},
    "Personalizada":       {"speed":400,"force":80, "offset":0.25,"passes":1,"overcut":1.0,"mat":"Config. manual"},
}

# ═══════════════════════════════════════════════════════
#  OBJETO DE DISEÑO
# ═══════════════════════════════════════════════════════
class DesignObject:
    """Representa un objeto en el canvas: posición, tamaño, color, paths."""
    _id_counter = 0

    def __init__(self, paths_mm, stroke="#000000", fill=None, name="Objeto"):
        DesignObject._id_counter += 1
        self.id        = DesignObject._id_counter
        self.paths_mm  = paths_mm      # lista de [(x,y),...]
        self.stroke    = stroke
        self.fill      = fill
        self.name      = f"{name} {self.id}"
        self.visible   = True
        self.locked    = False
        self.selected  = False
        # Bounding box en mm
        self._update_bbox()
        # Offset de posición (para mover)
        self.offset_x  = 0.0
        self.offset_y  = 0.0

    def _update_bbox(self):
        all_pts = [(x,y) for p in self.paths_mm for x,y in p]
        if all_pts:
            xs=[p[0] for p in all_pts]; ys=[p[1] for p in all_pts]
            self.bbox = (min(xs),min(ys),max(xs),max(ys))
        else:
            self.bbox = (0,0,0,0)

    def width_mm(self):  return self.bbox[2]-self.bbox[0]
    def height_mm(self): return self.bbox[3]-self.bbox[1]

    def translated_paths(self):
        return [[(x+self.offset_x, y+self.offset_y) for x,y in p]
                for p in self.paths_mm]

    def hit_test(self, mx, my, tolerance=2.0):
        """True si (mx,my) en mm cae dentro del bbox del objeto."""
        x0,y0,x1,y1 = self.bbox
        return (x0+self.offset_x-tolerance <= mx <= x1+self.offset_x+tolerance and
                y0+self.offset_y-tolerance <= my <= y1+self.offset_y+tolerance)

# ═══════════════════════════════════════════════════════
#  PARSER SVG COMPLETO
# ═══════════════════════════════════════════════════════
def _norm_color(c):
    if not c or c in ("none","transparent",""): return None
    c=c.strip().lower()
    if c.startswith("#"):
        if len(c)==4: c="#"+c[1]*2+c[2]*2+c[3]*2
        return c.upper()
    m=re.match(r"rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",c)
    if m: return "#{:02X}{:02X}{:02X}".format(*map(int,m.groups()))
    css={"black":"#000000","white":"#FFFFFF","red":"#FF0000","green":"#008000",
         "blue":"#0000FF","yellow":"#FFFF00","cyan":"#00FFFF","magenta":"#FF00FF",
         "orange":"#FFA500","gray":"#808080","grey":"#808080","navy":"#000080"}
    return css.get(c,"#000000")

def _parse_style_attr(style,attr_s,attr_f):
    stroke=fill=None
    for part in (style or "").split(";"):
        p=part.strip()
        if p.startswith("stroke:"): stroke=_norm_color(p[7:].strip())
        elif p.startswith("fill:"): fill=_norm_color(p[5:].strip())
    if not stroke and attr_s: stroke=_norm_color(attr_s)
    if not fill   and attr_f: fill  =_norm_color(attr_f)
    return stroke, fill

class SVGLoader:
    """Carga SVG y devuelve lista de DesignObject con paths en mm."""

    def __init__(self, path, dpi=96.0):
        self.path=path; self.dpi=dpi
        self.objects=[]
        self.svg_w_mm=0; self.svg_h_mm=0
        self._load()

    def _load(self):
        tree=ET.parse(self.path); root=tree.getroot()
        vb=root.get("viewBox","").split()
        vb_w=float(vb[2]) if len(vb)==4 else None
        vb_h=float(vb[3]) if len(vb)==4 else None
        sw=self._dim(root.get("width",  str(vb_w or 744)))
        sh=self._dim(root.get("height", str(vb_h or 1052)))
        sx=sw/(vb_w or sw); sy=sh/(vb_h or sh)
        self.svg_w_mm=px_mm(sw,self.dpi)
        self.svg_h_mm=px_mm(sh,self.dpi)
        self._parse_elem(root, sx, sy, {})

    def _parse_elem(self, node, sx, sy, parent_style):
        tag=node.tag.split("}")[-1]
        # Heredar estilos
        style=node.get("style","")
        stroke,fill=_parse_style_attr(style,node.get("stroke",""),node.get("fill",""))

        pts=[]
        if   tag=="path":    pts=self._path(node.get("d",""))
        elif tag in("polyline","polygon"): pts=self._pts_attr(node.get("points",""))
        elif tag=="rect":    pts=self._rect(node)
        elif tag=="line":    pts=self._line(node)
        elif tag=="circle":  pts=self._circ(node)
        elif tag=="ellipse": pts=self._ellip(node)

        if pts:
            # Escalar px → mm
            scaled=[[(px_mm(x*sx,self.dpi), px_mm(y*sy,self.dpi)) for x,y in pts]]
            col=stroke or "#000000"
            fil=fill
            obj=DesignObject(scaled, stroke=col, fill=fil)
            self.objects.append(obj)

        # Recursivo para grupos
        if tag in("g","svg","symbol"):
            for child in node:
                self._parse_elem(child, sx, sy, {})

    def _dim(self,v):
        v=str(v).strip().lower()
        for u in("px","mm","pt","em","%"): v=v.replace(u,"")
        try: return float(v)
        except: return 744.0

    def _pts_attr(self,s):
        n=list(map(float,re.findall(r"[-+]?\d*\.?\d+",s)))
        return [(n[i],n[i+1]) for i in range(0,len(n)-1,2)]

    def _rect(self,e):
        x,y=float(e.get("x",0)),float(e.get("y",0))
        w,h=float(e.get("width",0)),float(e.get("height",0))
        rx=float(e.get("rx",0)); ry=float(e.get("ry",rx))
        if rx==0 and ry==0:
            return[(x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y)]
        # Rect redondeado aproximado
        pts=[]; n=8
        corners=[(x+rx,y+ry),(x+w-rx,y+ry),(x+w-rx,y+h-ry),(x+rx,y+h-ry)]
        angles=[180,270,0,90]
        for (cx,cy),a0 in zip(corners,angles):
            for i in range(n+1):
                a=math.radians(a0+i*90/n)
                pts.append((cx+rx*math.cos(a),cy+ry*math.sin(a)))
        pts.append(pts[0])
        return pts

    def _line(self,e):
        return[(float(e.get("x1",0)),float(e.get("y1",0))),
               (float(e.get("x2",0)),float(e.get("y2",0)))]

    def _circ(self,e,n=64):
        cx,cy,r=float(e.get("cx",0)),float(e.get("cy",0)),float(e.get("r",10))
        return[(cx+r*math.cos(2*math.pi*i/n),cy+r*math.sin(2*math.pi*i/n))for i in range(n+1)]

    def _ellip(self,e,n=64):
        cx,cy=float(e.get("cx",0)),float(e.get("cy",0))
        rx,ry=float(e.get("rx",10)),float(e.get("ry",10))
        return[(cx+rx*math.cos(2*math.pi*i/n),cy+ry*math.sin(2*math.pi*i/n))for i in range(n+1)]

    def _path(self,d,step=2.0):
        """Parser completo de paths SVG con alta precisión (step=2px)."""
        pts=[]; tokens=re.findall(
            r"[MmLlHhVvZzCcSsQqTtAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?",d)
        cx=cy=sx=sy=0.0; px2=py2=0.0; cmd="M"; i=0
        def rd(n):
            nonlocal i
            v=[]
            for _ in range(n):
                if i<len(tokens):
                    try: v.append(float(tokens[i]))
                    except: v.append(0.0)
                    i+=1
            return v
        while i<len(tokens):
            t=tokens[i]
            if re.match(r"[A-Za-z]",t): cmd=t; i+=1; continue
            if   cmd in"Mm":
                v=rd(2); cx,cy=(cx+v[0],cy+v[1])if cmd=="m"else(v[0],v[1])
                sx,sy=cx,cy; pts.append((cx,cy)); cmd="l"if cmd=="m"else"L"
            elif cmd in"Ll":
                v=rd(2); cx,cy=(cx+v[0],cy+v[1])if cmd=="l"else(v[0],v[1]); pts.append((cx,cy))
            elif cmd=="H": v=rd(1); cx=v[0]; pts.append((cx,cy))
            elif cmd=="h": v=rd(1); cx+=v[0]; pts.append((cx,cy))
            elif cmd=="V": v=rd(1); cy=v[0]; pts.append((cx,cy))
            elif cmd=="v": v=rd(1); cy+=v[0]; pts.append((cx,cy))
            elif cmd in"Cc":
                v=rd(6)
                if len(v)<6: break
                if cmd=="c": x1,y1,x2,y2,x3,y3=cx+v[0],cy+v[1],cx+v[2],cy+v[3],cx+v[4],cy+v[5]
                else:         x1,y1,x2,y2,x3,y3=v
                n2=max(8,int(math.hypot(x3-cx,y3-cy)/step))
                for s in range(1,n2+1):
                    t2=s/n2; mt=1-t2
                    pts.append((mt**3*cx+3*mt**2*t2*x1+3*mt*t2**2*x2+t2**3*x3,
                                 mt**3*cy+3*mt**2*t2*y1+3*mt*t2**2*y2+t2**3*y3))
                px2,py2=x2,y2; cx,cy=x3,y3
            elif cmd in"Ss":
                v=rd(4)
                if len(v)<4: break
                if cmd=="s": x2,y2,x3,y3=cx+v[0],cy+v[1],cx+v[2],cy+v[3]
                else:         x2,y2,x3,y3=v
                x1,y1=2*cx-px2,2*cy-py2
                n2=max(8,int(math.hypot(x3-cx,y3-cy)/step))
                for s in range(1,n2+1):
                    t2=s/n2; mt=1-t2
                    pts.append((mt**3*cx+3*mt**2*t2*x1+3*mt*t2**2*x2+t2**3*x3,
                                 mt**3*cy+3*mt**2*t2*y1+3*mt*t2**2*y2+t2**3*y3))
                px2,py2=x2,y2; cx,cy=x3,y3
            elif cmd in"Qq":
                v=rd(4)
                if len(v)<4: break
                if cmd=="q": x1,y1,x2,y2=cx+v[0],cy+v[1],cx+v[2],cy+v[3]
                else:         x1,y1,x2,y2=v
                n2=max(8,int(math.hypot(x2-cx,y2-cy)/step))
                for s in range(1,n2+1):
                    t2=s/n2; mt=1-t2
                    pts.append((mt**2*cx+2*mt*t2*x1+t2**2*x2,
                                 mt**2*cy+2*mt*t2*y1+t2**2*y2))
                px2,py2=x1,y1; cx,cy=x2,y2
            elif cmd in"Tt":
                v=rd(2)
                if len(v)<2: break
                x1,y1=2*cx-px2,2*cy-py2
                x2,y2=(cx+v[0],cy+v[1])if cmd=="t"else(v[0],v[1])
                n2=max(8,int(math.hypot(x2-cx,y2-cy)/step))
                for s in range(1,n2+1):
                    t2=s/n2; mt=1-t2
                    pts.append((mt**2*cx+2*mt*t2*x1+t2**2*x2,
                                 mt**2*cy+2*mt*t2*y1+t2**2*y2))
                px2,py2=x1,y1; cx,cy=x2,y2
            elif cmd in"Aa":
                v=rd(7)
                if len(v)<7: break
                rx,ry,xrot,laf,sf,ex,ey=v
                if cmd=="a": ex,ey=cx+ex,cy+ey
                n2=max(8,int(math.hypot(ex-cx,ey-cy)/step))
                for s in range(1,n2+1):
                    t2=s/n2
                    pts.append((cx+(ex-cx)*t2, cy+(ey-cy)*t2))
                cx,cy=ex,ey
            elif cmd in"Zz":
                pts.append((sx,sy)); cx,cy=sx,sy
                i+=0; break
            else: i+=1
        return pts

# ═══════════════════════════════════════════════════════
#  GENERADOR HPGL DE ALTA PRECISIÓN
# ═══════════════════════════════════════════════════════
class HPGLGenerator:
    def __init__(self,speed=400,force=80,offset=0.25,passes=1,
                 overcut=1.0,origin_x=5,origin_y=5,mirror=False):
        self.speed=max(10,min(speed,800)); self.force=max(10,min(force,350))
        self.offset=offset; self.passes=passes; self.overcut=overcut
        self.ox=origin_x; self.oy=origin_y; self.mirror=mirror

    def generate(self, objects, mat_w_mm=720.0):
        c=["IN;","PA;",f"VS{self.speed};",f"FS{self.force};",
           f"BF{int(self.offset*100)};","PU;"]
        for _ in range(self.passes):
            for obj in objects:
                if not obj.visible: continue
                for poly in obj.translated_paths():
                    if len(poly)<2: continue
                    # Limpiar duplicados adyacentes
                    clean=[poly[0]]
                    for p in poly[1:]:
                        if math.hypot(p[0]-clean[-1][0],p[1]-clean[-1][1])>0.01:
                            clean.append(p)
                    if len(clean)<2: continue
                    x0,y0=self._t(clean[0],mat_w_mm)
                    c.append(f"PU{x0},{y0};")
                    pts=",".join(f"{self._t(p,mat_w_mm)[0]},{self._t(p,mat_w_mm)[1]}"
                                  for p in clean[1:])
                    c.append(f"PD{pts};")
                    # Overcut para cierre limpio
                    if self.overcut>0 and len(clean)>=2:
                        lx,ly=clean[-1]; px2,py2=clean[-2]
                        dx,dy=lx-px2,ly-py2; d=math.hypot(dx,dy)
                        if d>0:
                            hx,hy=self._t((lx+(dx/d)*self.overcut,
                                           ly+(dy/d)*self.overcut),mat_w_mm)
                            c.append(f"PD{hx},{hy};")
                    c.append("PU;")
        c.append(f"PU{mm_hpgl(self.ox)},{mm_hpgl(self.oy)};")
        c.append("SP0;")
        return "\n".join(c)

    def _t(self,pt,mat_w=720.0):
        x=pt[0] if not self.mirror else mat_w-pt[0]
        return mm_hpgl(x+self.ox), mm_hpgl(pt[1]+self.oy)

# ═══════════════════════════════════════════════════════
#  COMUNICACIÓN PLOTTER
# ═══════════════════════════════════════════════════════
class PlotterComm:
    def __init__(self,port,baud=9600): self.port=port; self.baud=baud; self._s=None
    def connect(self):
        if not SERIAL_OK: raise RuntimeError("Instala pyserial: pip install pyserial")
        self._s=serial.Serial(self.port,self.baud,timeout=5)
    def send(self,hpgl,cb=None):
        if not self._s or not self._s.is_open: raise RuntimeError("Sin conexión")
        lines=hpgl.split("\n"); n=len(lines)
        for i,l in enumerate(lines):
            self._s.write((l+"\n").encode()); self._s.flush(); time.sleep(0.005)
            if cb: cb(int((i+1)/n*100))
    def stop(self):
        if self._s and self._s.is_open:
            try: self._s.write(b"\x1B")
            except: pass
    def disconnect(self):
        if self._s and self._s.is_open: self._s.close()
    @staticmethod
    def list_ports():
        if not SERIAL_OK: return ["(instalar pyserial)"]
        return [p.device for p in serial.tools.list_ports.comports()] or ["(sin puertos)"]

# ═══════════════════════════════════════════════════════════════════════════════
#  CANVAS PRINCIPAL — interactivo con selección, movimiento, zoom
# ═══════════════════════════════════════════════════════════════════════════════
class DesignCanvas(tk.Canvas):
    RULER  = 24
    PAD    = 20
    HANDLE = 6

    def __init__(self, parent, app, **kw):
        super().__init__(parent, bg=CANVAS_BG, highlightthickness=0,
                         cursor="crosshair", **kw)
        self.app       = app
        self._zoom     = 1.0
        self._pan_x    = float(self.RULER + self.PAD)
        self._pan_y    = float(self.RULER + self.PAD)
        self._drag_mode= None   # "pan" | "move" | "select_rect"
        self._drag_start     = None
        self._drag_obj_offsets = []
        self._sel_rect = None   # (x0,y0,x1,y1) canvas coords
        self._tool     = "select"   # "select" | "pan"
        self._preview_img = None   # PhotoImage del SVG renderizado

        self.bind("<Configure>",      self._on_resize)
        self.bind("<MouseWheel>",     self._on_wheel)
        self.bind("<ButtonPress-1>",  self._on_ldown)
        self.bind("<B1-Motion>",      self._on_lmove)
        self.bind("<ButtonRelease-1>",self._on_lup)
        self.bind("<ButtonPress-2>",  self._on_mdown)
        self.bind("<B2-Motion>",      self._on_mpan)
        self.bind("<ButtonPress-3>",  self._on_mdown)
        self.bind("<B3-Motion>",      self._on_mpan)
        self.bind("<Delete>",         self._on_delete)
        self.focus_set()

    # ── Coordenadas ──────────────────────────────────────────────────────────
    def mm_to_cv(self, x, y):
        return self._pan_x + x*self._zoom, self._pan_y + y*self._zoom
    def cv_to_mm(self, cx, cy):
        return (cx-self._pan_x)/self._zoom, (cy-self._pan_y)/self._zoom
    def set_tool(self, tool): self._tool=tool

    # ── Zoom / pan ────────────────────────────────────────────────────────────
    def _on_wheel(self,e):
        factor=1.15 if e.delta>0 else 1/1.15
        # Zoom centrado en cursor
        mx,my=e.x,e.y
        self._pan_x = mx - (mx-self._pan_x)*factor
        self._pan_y = my - (my-self._pan_y)*factor
        self._zoom *= factor
        self.redraw()

    def _on_mdown(self,e):   self._drag_start=(e.x,e.y); self._drag_mode="pan"
    def _on_mpan(self,e):
        if self._drag_start:
            self._pan_x += e.x-self._drag_start[0]
            self._pan_y += e.y-self._drag_start[1]
            self._drag_start=(e.x,e.y); self.redraw()

    def zoom_in(self):
        cx,cy=self.winfo_width()/2,self.winfo_height()/2
        self._pan_x=cx-(cx-self._pan_x)*1.25
        self._pan_y=cy-(cy-self._pan_y)*1.25
        self._zoom*=1.25; self.redraw()
    def zoom_out(self):
        cx,cy=self.winfo_width()/2,self.winfo_height()/2
        self._pan_x=cx-(cx-self._pan_x)/1.25
        self._pan_y=cy-(cy-self._pan_y)/1.25
        self._zoom/=1.25; self.redraw()
    def zoom_fit(self):
        W=self.winfo_width() or 800; H=self.winfo_height() or 600
        aw=self.app.mat_w; ah=min(self.app.mat_h,800)
        if aw<=0 or ah<=0: return
        cw=W-self.RULER-self.PAD*2; ch=H-self.RULER-self.PAD*2
        self._zoom=min(cw/aw, ch/ah)*0.95
        self._pan_x=self.RULER+self.PAD+(cw-aw*self._zoom)/2
        self._pan_y=self.RULER+self.PAD+(ch-ah*self._zoom)/2
        self.redraw()
    def zoom_100(self):
        # 1mm = 3.78 px a 96dpi
        self._zoom=3.78; self._pan_x=self.RULER+self.PAD; self._pan_y=self.RULER+self.PAD
        self.redraw()

    # ── Selección y movimiento ────────────────────────────────────────────────
    def _on_ldown(self,e):
        self.focus_set()
        mx,my=self.cv_to_mm(e.x,e.y)
        if self._tool=="pan":
            self._drag_start=(e.x,e.y); self._drag_mode="pan"; return

        # Click en objeto
        hit=None
        for obj in reversed(self.app.objects):
            if obj.visible and obj.hit_test(mx,my):
                hit=obj; break

        if hit:
            if not (e.state & 0x4):  # sin Ctrl: deselect todo
                for o in self.app.objects: o.selected=False
            hit.selected=True
            self._drag_mode="move"
            self._drag_start=(e.x,e.y)
            self._drag_obj_offsets=[(o, o.offset_x, o.offset_y)
                                     for o in self.app.objects if o.selected]
        else:
            # Deselect + iniciar rect de selección
            if not (e.state & 0x4):
                for o in self.app.objects: o.selected=False
            self._drag_mode="select_rect"
            self._drag_start=(e.x,e.y)
            self._sel_rect=(e.x,e.y,e.x,e.y)

        self.redraw()
        self.app._update_properties()

    def _on_lmove(self,e):
        if not self._drag_start: return
        dx=(e.x-self._drag_start[0])/self._zoom
        dy=(e.y-self._drag_start[1])/self._zoom

        if self._drag_mode=="pan":
            self._pan_x+=e.x-self._drag_start[0]
            self._pan_y+=e.y-self._drag_start[1]
            self._drag_start=(e.x,e.y)
        elif self._drag_mode=="move":
            for obj,ox,oy in self._drag_obj_offsets:
                obj.offset_x=ox+dx; obj.offset_y=oy+dy
            self.app._update_properties()
        elif self._drag_mode=="select_rect":
            self._sel_rect=(self._drag_start[0],self._drag_start[1],e.x,e.y)
        self.redraw()

    def _on_lup(self,e):
        if self._drag_mode=="select_rect" and self._sel_rect:
            # Seleccionar objetos dentro del rect
            sx0,sy0,sx1,sy1=self._sel_rect
            if sx0>sx1: sx0,sx1=sx1,sx0
            if sy0>sy1: sy0,sy1=sy1,sy0
            mx0,my0=self.cv_to_mm(sx0,sy0); mx1,my1=self.cv_to_mm(sx1,sy1)
            for obj in self.app.objects:
                if not obj.locked:
                    bx0=obj.bbox[0]+obj.offset_x; bx1=obj.bbox[2]+obj.offset_x
                    by0=obj.bbox[1]+obj.offset_y; by1=obj.bbox[3]+obj.offset_y
                    if bx0>=mx0 and by0>=my0 and bx1<=mx1 and by1<=my1:
                        obj.selected=True
            self._sel_rect=None
        self._drag_mode=None; self._drag_obj_offsets=[]
        self.redraw(); self.app._update_properties()

    def _on_resize(self,e): self.after_idle(self.zoom_fit)
    def _on_delete(self,e):
        self.app.objects=[o for o in self.app.objects if not o.selected]
        self.redraw(); self.app._update_obj_list()

    # ── Dibujado ──────────────────────────────────────────────────────────────
    def redraw(self):
        self.delete("all")
        W=self.winfo_width() or 800; H=self.winfo_height() or 600
        z=self._zoom

        # Fondo
        self.create_rectangle(0,0,W,H,fill=CANVAS_BG,outline="")

        # Material (hoja blanca)
        mat_h_vis=min(self.app.mat_h,800)
        mx0,my0=self.mm_to_cv(0,0)
        mx1,my1=self.mm_to_cv(self.app.mat_w,mat_h_vis)
        # Sombra
        self.create_rectangle(mx0+4,my0+4,mx1+4,my1+4,fill="#8A9AAA",outline="")
        # Hoja
        self.create_rectangle(mx0,my0,mx1,my1,fill=WHITE,outline=BORDER,width=1)

        # Grilla
        self._draw_grid(mx0,my0,mx1,my1)

        # Imagen de preview SVG si existe
        if self._preview_img:
            px0,py0=self.mm_to_cv(0,0)
            self.create_image(px0,py0,anchor="nw",image=self._preview_img)

        # Objetos
        for obj in self.app.objects:
            if not obj.visible: continue
            self._draw_object(obj)

        # Rect de selección
        if self._sel_rect:
            x0,y0,x1,y1=self._sel_rect
            self.create_rectangle(x0,y0,x1,y1,outline=SEL_COLOR,
                                   fill="",dash=(4,3),width=1)

        # Reglas
        self._draw_rulers(W,H)

        # Info zoom
        pct=int(z*px_mm(1,96)*100)/100
        self.create_text(W-6,H-4,
            text=f"Zoom {z:.2f}×  |  Mat: {self.app.mat_w:.0f}×{mat_h_vis:.0f} mm",
            fill=TEXT_LIGHT,font=("Segoe UI",7),anchor="se")

    def _draw_grid(self,mx0,my0,mx1,my1):
        z=self._zoom
        step=5 if z>4 else 10 if z>1.5 else 25 if z>0.5 else 50
        # Vertical
        x=0
        while x<=self.app.mat_w:
            cx,_=self.mm_to_cv(x,0); _,cy0=self.mm_to_cv(0,0)
            _,cy1=self.mm_to_cv(0,min(self.app.mat_h,800))
            col=GRID_MAJOR if x%50==0 else GRID_MINOR
            self.create_line(cx,cy0,cx,cy1,fill=col,width=1); x+=step
        # Horizontal
        y=0
        while y<=min(self.app.mat_h,800):
            _,cy=self.mm_to_cv(0,y); cx0,_=self.mm_to_cv(0,0)
            cx1,_=self.mm_to_cv(self.app.mat_w,0)
            col=GRID_MAJOR if y%50==0 else GRID_MINOR
            self.create_line(cx0,cy,cx1,cy,fill=col,width=1); y+=step

    def _draw_rulers(self,W,H):
        # Fondo reglas
        self.create_rectangle(self.RULER,0,W,self.RULER,fill=RULER_BG,outline=BORDER)
        self.create_rectangle(0,self.RULER,self.RULER,H,fill=RULER_BG,outline=BORDER)
        self.create_rectangle(0,0,self.RULER,self.RULER,fill=RULER_BG,outline=BORDER)

        z=self._zoom
        step=5 if z>6 else 10 if z>2 else 25 if z>0.8 else 50 if z>0.3 else 100

        for mm in range(0,int(self.app.mat_w)+step*2,step):
            cx,_=self.mm_to_cv(mm,0)
            if not (self.RULER<cx<W): continue
            major=mm%50==0
            self.create_line(cx,self.RULER-(9 if major else 4),cx,self.RULER,
                              fill=RULER_FG,width=1)
            if major:
                self.create_text(cx,9,text=str(mm),fill=RULER_FG,
                                  font=("Segoe UI",6),anchor="n")

        for mm in range(0,int(min(self.app.mat_h,800))+step*2,step):
            _,cy=self.mm_to_cv(0,mm)
            if not (self.RULER<cy<H): continue
            major=mm%50==0
            self.create_line(self.RULER-(9 if major else 4),cy,self.RULER,cy,
                              fill=RULER_FG,width=1)
            if major:
                self.create_text(9,cy,text=str(mm),fill=RULER_FG,
                                  font=("Segoe UI",6),anchor="e",angle=90)

    def _draw_object(self,obj):
        z=self._zoom
        stroke=obj.stroke or "#000000"
        fill  =obj.fill
        lw=max(1.0, z*0.35)

        for poly in obj.translated_paths():
            if len(poly)<2: continue
            coords=[]
            for x,y in poly:
                cx,cy=self.mm_to_cv(x,y); coords+=[cx,cy]
            if len(coords)<4: continue

            # Relleno si hay fill definido
            if fill and fill!="#FFFFFF" and len(coords)>=6:
                try:
                    self.create_polygon(*coords,fill=fill,outline="",
                                         smooth=False)
                except: pass

            # Trazo
            sel_lw=lw+1.5 if obj.selected else lw
            sel_col=SEL_COLOR if obj.selected else stroke
            try:
                self.create_line(*coords,fill=sel_col,width=sel_lw,
                                  capstyle="round",joinstyle="round",smooth=False)
            except: pass

        # Handles de selección
        if obj.selected:
            x0,y0,x1,y1=obj.bbox
            for px_,py_ in [(x0+obj.offset_x,y0+obj.offset_y),
                             (x1+obj.offset_x,y0+obj.offset_y),
                             (x1+obj.offset_x,y1+obj.offset_y),
                             (x0+obj.offset_x,y1+obj.offset_y),
                             ((x0+x1)/2+obj.offset_x,y0+obj.offset_y),
                             (x1+obj.offset_x,(y0+y1)/2+obj.offset_y),
                             ((x0+x1)/2+obj.offset_x,y1+obj.offset_y),
                             (x0+obj.offset_x,(y0+y1)/2+obj.offset_y)]:
                cx,cy=self.mm_to_cv(px_,py_)
                h=self.HANDLE
                self.create_rectangle(cx-h,cy-h,cx+h,cy+h,
                    fill=WHITE,outline=SEL_COLOR,width=1.5)

    def set_preview_image(self, pil_img):
        """Muestra imagen PNG del SVG como fondo de referencia."""
        if not PIL_OK or pil_img is None:
            self._preview_img=None; return
        mat_w_px=int(self.app.mat_w*self._zoom)
        mat_h_px=int(min(self.app.mat_h,800)*self._zoom)
        if mat_w_px<1 or mat_h_px<1: return
        try:
            resized=pil_img.resize((mat_w_px,mat_h_px),Image.LANCZOS)
            self._preview_img=ImageTk.PhotoImage(resized)
        except: self._preview_img=None
        self.redraw()

    def clear_preview(self):
        self._preview_img=None; self.redraw()

# ═══════════════════════════════════════════════════════════════════════════════
#  APLICACIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Redsail RS720C Studio")
        self.geometry("1440x860")
        self.minsize(1100,700)
        self.configure(bg=BG)
        try: self.state("zoomed")
        except: pass

        # Estado
        self.objects    = []    # lista de DesignObject
        self.hpgl_data  = ""
        self.connected  = False
        self.comm       = None
        self.mat_w      = 720.0
        self.mat_h      = 500.0
        self._src_path  = ""
        self._svg_render= None  # PIL Image del SVG

        # Variables
        self.file_var   = tk.StringVar()
        self.port_var   = tk.StringVar()
        self.baud_var   = tk.IntVar(value=9600)
        self.blade_var  = tk.StringVar(value="Vinilo estándar 45°")
        self.speed_var  = tk.IntVar(value=400)
        self.force_var  = tk.IntVar(value=80)
        self.offset_var = tk.DoubleVar(value=0.25)
        self.passes_var = tk.IntVar(value=1)
        self.overcut_var= tk.DoubleVar(value=1.0)
        self.ori_x_var  = tk.DoubleVar(value=5.0)
        self.ori_y_var  = tk.DoubleVar(value=5.0)
        self.dpi_var    = tk.DoubleVar(value=96.0)
        self.unit_var   = tk.StringVar(value="mm")
        self.width_var  = tk.StringVar(value="0.0")
        self.height_var = tk.StringVar(value="0.0")
        self.lock_var   = tk.BooleanVar(value=True)
        self.mirror_var = tk.BooleanVar(value=False)
        self.weed_var   = tk.BooleanVar(value=False)
        self.show_fill_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Listo  —  Abra un SVG o PDF para comenzar")
        self.progress   = tk.DoubleVar(value=0)
        self.pos_var    = tk.StringVar(value="X: 0.0  Y: 0.0 mm")

        self._build()
        self._styles()
        self._load_blade()
        self._refresh_ports()
        self.canvas.bind("<Motion>",self._on_mouse_move)

    # ── Estilos ───────────────────────────────────────────────────────────────
    def _styles(self):
        s=ttk.Style(self); s.theme_use("clam")
        s.configure("Horizontal.TProgressbar",troughcolor=BG,
                    background=ACCENT,thickness=5,borderwidth=0)
        s.configure("TCombobox",fieldbackground=WHITE,background=WHITE,
                    foreground=TEXT_DARK,selectbackground="#E8F0FE",
                    bordercolor=BORDER)
        s.map("TCombobox",fieldbackground=[("readonly",WHITE)],
              bordercolor=[("focus",ACCENT)])
        s.configure("TNotebook",background=BG,borderwidth=0)
        s.configure("TNotebook.Tab",background=BG,foreground=TEXT_MED,
                    font=FONT_SM,padding=[12,5])
        s.map("TNotebook.Tab",background=[("selected",WHITE)],
              foreground=[("selected",ACCENT)])
        s.configure("TCheckbutton",background=SIDEBAR_BG,foreground=TEXT_DARK,font=FONT_SM)
        s.map("TCheckbutton",background=[("active",BG)])
        s.configure("Treeview",background=WHITE,foreground=TEXT_DARK,
                    fieldbackground=WHITE,font=FONT_SM,rowheight=22)
        s.configure("Treeview.Heading",background=BG,foreground=TEXT_MED,font=FONT_SM)
        s.map("Treeview",background=[("selected",SEL_COLOR)],
              foreground=[("selected",WHITE)])

    # ═══════════════════════════════════════════════════
    #  CONSTRUCCIÓN UI
    # ═══════════════════════════════════════════════════
    def _build(self):
        self._build_menu()
        self._build_toolbar()
        # PanedWindow principal
        paned=tk.PanedWindow(self,orient="horizontal",bg=BG,
                              sashwidth=5,sashrelief="flat",
                              handlesize=0)
        paned.pack(fill="both",expand=True)

        # Panel izquierdo
        left=tk.Frame(paned,bg=SIDEBAR_BG,width=280)
        paned.add(left,minsize=220)
        self._build_left(left)

        # Canvas central
        center=tk.Frame(paned,bg=CANVAS_BG)
        paned.add(center,minsize=400)
        self._build_canvas(center)

        # Panel derecho
        right=tk.Frame(paned,bg=SIDEBAR_BG,width=250)
        paned.add(right,minsize=210)
        self._build_right(right)

        self._build_statusbar()

    # ── Menú ──────────────────────────────────────────
    def _build_menu(self):
        mb=tk.Menu(self,bg=TOOLBAR_BG,fg=WHITE,
                   activebackground=ACCENT,activeforeground=WHITE,
                   relief="flat",bd=0)
        self.configure(menu=mb)
        def M(label,items):
            m=tk.Menu(mb,tearoff=0,bg=WHITE,fg=TEXT_DARK,
                      activebackground="#E8F0FE",activeforeground=TEXT_DARK,
                      relief="flat",bd=1)
            mb.add_cascade(label=label,menu=m,background=TOOLBAR_BG,foreground=WHITE)
            for it in items:
                if it=="-": m.add_separator()
                else: m.add_command(label=it[0],command=it[1],
                                     accelerator=it[2] if len(it)>2 else "")
        M("Archivo",[("Abrir SVG…",self._open_svg,"Ctrl+O"),
                      ("Abrir PDF…",self._open_pdf,"Ctrl+P"),
                      "-",
                      ("Guardar HPGL…",self._save_plt,"Ctrl+S"),
                      ("Exportar SVG…",self._export_svg),
                      "-",("Salir",self.quit)])
        M("Editar",[("Seleccionar todo",self._sel_all,"Ctrl+A"),
                     ("Deseleccionar",self._desel_all,"Esc"),
                     ("Eliminar selección","<Delete>"),
                     "-",("Duplicar",self._duplicate,"Ctrl+D"),
                     ("Espejo horizontal",self._flip_h),
                     ("Espejo vertical",self._flip_v)])
        M("Corte",[("Generar HPGL",self._generate,"F5"),
                    ("Enviar a plotter",self._send,"F6"),
                    "-",
                    ("Test de corte 10×10",self._test_cut),
                    ("Mover a origen",self._home),
                    ("Parar corte",self._stop_cut,"F7")])
        M("Plotter",[("Conectar / Desconectar",self._toggle_connect),
                      ("Calibrar origen",self._calibrate)])
        M("Ver",[("Zoom +",self.canvas.zoom_in if hasattr(self,"canvas") else lambda:None,"+"),
                  ("Zoom -",self.canvas.zoom_out if hasattr(self,"canvas") else lambda:None,"-"),
                  ("Ajustar",self._zoom_fit,"F"),
                  ("100%",self._zoom_100,"1")])
        M("Ayuda",[("Acerca de",self._about)])
        self.bind_all("<Control-o>",lambda e:self._open_svg())
        self.bind_all("<Control-p>",lambda e:self._open_pdf())
        self.bind_all("<Control-s>",lambda e:self._save_plt())
        self.bind_all("<Control-a>",lambda e:self._sel_all())
        self.bind_all("<F5>",lambda e:self._generate())
        self.bind_all("<F6>",lambda e:self._send())
        self.bind_all("<F7>",lambda e:self._stop_cut())
        self.bind_all("<Escape>",lambda e:self._desel_all())

    # ── Toolbar ───────────────────────────────────────
    def _build_toolbar(self):
        tb=tk.Frame(self,bg=TOOLBAR_BG,height=48)
        tb.pack(fill="x"); tb.pack_propagate(False)

        # Logo
        tk.Label(tb,text="  ✦ RS720C STUDIO",bg=TOOLBAR_BG,fg=WHITE,
                 font=(FN,11,"bold")).pack(side="left",padx=8)
        self._sep(tb)

        # Herramientas de archivo
        self._tbtn(tb,"📂 Abrir",self._open_svg,"Abrir SVG")
        self._tbtn(tb,"📄 PDF",  self._open_pdf,"Abrir PDF")
        self._tbtn(tb,"💾 Guardar",self._save_plt,"Guardar HPGL")
        self._sep(tb)

        # Herramientas de selección
        self.tool_sel_btn=self._tbtn(tb,"↖ Selección",
            lambda:self._set_tool("select"),"Herramienta selección",active=True)
        self.tool_pan_btn=self._tbtn(tb,"✋ Mano",
            lambda:self._set_tool("pan"),"Herramienta mano")
        self._sep(tb)

        # Edición
        self._tbtn(tb,"⬛ Duplicar",self._duplicate,"Duplicar objeto(s)")
        self._tbtn(tb,"↔ Espejo H",self._flip_h,"Espejo horizontal")
        self._tbtn(tb,"↕ Espejo V",self._flip_v,"Espejo vertical")
        self._sep(tb)

        # Vista
        self._tbtn(tb,"🔍+",self._zoom_in,"Zoom +")
        self._tbtn(tb,"🔍-",self._zoom_out,"Zoom -")
        self._tbtn(tb,"⊡ Ajustar",self._zoom_fit,"Ajustar a ventana")
        self._sep(tb)

        # Corte
        self._tbtn(tb,"⟳ Generar",self._generate,"Generar HPGL (F5)",accent=True)
        self.send_tb=self._tbtn(tb,"▶ CORTAR",self._send,"Enviar a plotter (F6)",accent=True)
        self._tbtn(tb,"⏹ Parar",self._stop_cut,"Parar corte",danger=True)
        self._sep(tb)

        # Puerto (derecha)
        tk.Label(tb,text="Puerto:",bg=TOOLBAR_BG,fg="#99A8B8",font=FONT_SM).pack(side="right",padx=(0,4))
        self.port_quick=ttk.Combobox(tb,textvariable=self.port_var,width=8,state="readonly")
        self.port_quick.pack(side="right",padx=(0,4))
        tk.Button(tb,text="↺",bg=TOOLBAR_BG,fg=WHITE,relief="flat",bd=0,
                  cursor="hand2",font=(FN,10),padx=4,
                  activebackground=TOOLBAR_ACT,command=self._refresh_ports).pack(side="right")
        self.conn_badge=tk.Label(tb,text="⬤ DESCONECTADO",
                                  bg=TOOLBAR_BG,fg="#FF7070",font=(FN,8,"bold"))
        self.conn_badge.pack(side="right",padx=12)

    def _tbtn(self,parent,text,cmd,tip="",active=False,accent=False,danger=False):
        bg=ACCENT if accent else DANGER if danger else TOOLBAR_BTN
        abg=ACCENT_H if accent else "#922B21" if danger else TOOLBAR_ACT
        b=tk.Button(parent,text=text,bg=bg,fg=WHITE,relief="flat",bd=0,
                     font=(FN,8,"bold" if accent else "normal"),padx=10,pady=5,
                     cursor="hand2",activebackground=abg,activeforeground=WHITE,
                     command=cmd)
        b.pack(side="left",padx=1,pady=4)
        return b

    def _sep(self,parent):
        tk.Frame(parent,bg="#445060",width=1,height=28).pack(side="left",padx=6,pady=10)

    def _set_tool(self,tool):
        self.canvas.set_tool(tool)
        self.tool_sel_btn.configure(bg=ACCENT if tool=="select" else TOOLBAR_BTN)
        self.tool_pan_btn.configure(bg=ACCENT if tool=="pan"    else TOOLBAR_BTN)

    # ── Panel izquierdo ───────────────────────────────
    def _build_left(self,p):
        # Header
        h=tk.Frame(p,bg=SIDEBAR_HDR,height=34); h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h,text="  OBJETOS / CAPAS",bg=SIDEBAR_HDR,fg=WHITE,
                 font=FONT_BOLD).pack(side="left",pady=8)

        # Lista de objetos
        frm=tk.Frame(p,bg=SIDEBAR_BG); frm.pack(fill="both",expand=True,padx=6,pady=6)
        cols=("vis","nombre","color","tamaño")
        self.obj_tree=ttk.Treeview(frm,columns=cols,show="headings",height=12,
                                    selectmode="extended")
        self.obj_tree.heading("vis",    text="👁")
        self.obj_tree.heading("nombre", text="Nombre")
        self.obj_tree.heading("color",  text="Color")
        self.obj_tree.heading("tamaño", text="Tamaño")
        self.obj_tree.column("vis",    width=28,  stretch=False,anchor="center")
        self.obj_tree.column("nombre", width=110, stretch=True)
        self.obj_tree.column("color",  width=60,  stretch=False,anchor="center")
        self.obj_tree.column("tamaño", width=80,  stretch=False,anchor="center")
        vsb=ttk.Scrollbar(frm,orient="vertical",command=self.obj_tree.yview)
        self.obj_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",fill="y")
        self.obj_tree.pack(fill="both",expand=True)
        self.obj_tree.bind("<<TreeviewSelect>>",self._on_tree_select)
        self.obj_tree.bind("<Double-1>",        self._on_tree_double)

        # Botones de capa
        bf=tk.Frame(p,bg=SIDEBAR_BG); bf.pack(fill="x",padx=6,pady=(0,4))
        for txt,cmd in [("▲",self._obj_up),("▼",self._obj_down),
                         ("👁",self._obj_toggle_vis),("🗑",self._obj_delete)]:
            tk.Button(bf,text=txt,bg=BG,fg=TEXT_DARK,relief="flat",
                       font=(FN,10),padx=8,pady=3,cursor="hand2",
                       activebackground=BORDER,command=cmd).pack(side="left",padx=2)

        # Propiedades del objeto seleccionado
        ph=tk.Frame(p,bg=SIDEBAR_HDR,height=28); ph.pack(fill="x"); ph.pack_propagate(False)
        tk.Label(ph,text="  PROPIEDADES",bg=SIDEBAR_HDR,fg=WHITE,
                 font=FONT_BOLD).pack(side="left",pady=6)

        props=tk.Frame(p,bg=SIDEBAR_BG); props.pack(fill="x",padx=8,pady=6)
        rows=[("X:",self._prop_entry("x")),("Y:",self._prop_entry("y")),
              ("Ancho:",self._prop_entry("w")),("Alto:",self._prop_entry("h"))]

        for i,(label,entry) in enumerate(rows):
            tk.Label(props,text=label,bg=SIDEBAR_BG,fg=TEXT_MED,
                     font=FONT_SM,width=6,anchor="w").grid(row=i//2,column=(i%2)*2,
                                                             padx=(0,2),pady=2,sticky="w")
            entry.grid(row=i//2,column=(i%2)*2+1,padx=(0,8),pady=2,sticky="ew")
        props.columnconfigure(1,weight=1); props.columnconfigure(3,weight=1)

        # Color de objeto
        cf=tk.Frame(p,bg=SIDEBAR_BG); cf.pack(fill="x",padx=8,pady=(0,6))
        tk.Label(cf,text="Trazo:",bg=SIDEBAR_BG,fg=TEXT_MED,font=FONT_SM).pack(side="left")
        self.stroke_swatch=tk.Label(cf,bg="#000000",width=4,relief="solid",
                                     cursor="hand2",bd=1)
        self.stroke_swatch.pack(side="left",padx=4,ipady=6)
        self.stroke_swatch.bind("<Button-1>",self._change_stroke)
        tk.Label(cf,text="Relleno:",bg=SIDEBAR_BG,fg=TEXT_MED,font=FONT_SM).pack(side="left",padx=(8,0))
        self.fill_swatch=tk.Label(cf,bg="#CCCCCC",width=4,relief="solid",
                                   cursor="hand2",bd=1)
        self.fill_swatch.pack(side="left",padx=4,ipady=6)
        self.fill_swatch.bind("<Button-1>",self._change_fill)

    def _prop_entry(self,key):
        var=tk.StringVar()
        e=tk.Entry(None,textvariable=var,bg=BG,fg=TEXT_DARK,
                    relief="flat",font=FONT_SM,width=8,bd=1)
        setattr(self,f"_prop_{key}",var)
        return e

    # ── Canvas central ────────────────────────────────
    def _build_canvas(self,p):
        # Mini toolbar canvas
        ctb=tk.Frame(p,bg=BG,height=28); ctb.pack(fill="x"); ctb.pack_propagate(False)
        tk.Label(ctb,text=" Vista de diseño",bg=BG,fg=TEXT_MED,font=FONT_SM).pack(side="left",padx=8,pady=5)
        ttk.Checkbutton(ctb,text="Mostrar relleno",variable=self.show_fill_var,
                         command=self._toggle_fill).pack(side="left",padx=8)
        tk.Label(ctb,textvariable=self.pos_var,bg=BG,fg=TEXT_LIGHT,
                 font=FONT_SM).pack(side="right",padx=10)

        self.canvas=DesignCanvas(p,self)
        self.canvas.pack(fill="both",expand=True)
        self.after(100,self.canvas.zoom_fit)

    # ── Panel derecho ─────────────────────────────────
    def _build_right(self,p):
        h=tk.Frame(p,bg=SIDEBAR_HDR,height=34); h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h,text="  PARÁMETROS DE CORTE",bg=SIDEBAR_HDR,fg=WHITE,
                 font=FONT_BOLD).pack(side="left",pady=8)

        sc_out=tk.Frame(p,bg=SIDEBAR_BG); sc_out.pack(fill="both",expand=True)
        vsb=tk.Scrollbar(sc_out); vsb.pack(side="right",fill="y")
        cv=tk.Canvas(sc_out,bg=SIDEBAR_BG,highlightthickness=0,yscrollcommand=vsb.set)
        cv.pack(fill="both",expand=True); vsb.configure(command=cv.yview)
        inner=tk.Frame(cv,bg=SIDEBAR_BG)
        cv.create_window((0,0),window=inner,anchor="nw",width=248)
        inner.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        self._build_right_inner(inner)

    def _build_right_inner(self,p):
        # ── Conexión ──
        self._sec(p,"CONEXIÓN RS720C")
        cf=tk.Frame(p,bg=SIDEBAR_BG); cf.pack(fill="x",padx=10,pady=(0,4))
        for row,(lbl,wid,vals) in enumerate([
            ("Puerto:",9,None),("Baud:",9,[9600,19200,38400,57600,115200])]):
            tk.Label(cf,text=lbl,bg=SIDEBAR_BG,fg=TEXT_MED,font=FONT_SM,
                     width=7,anchor="w").grid(row=row,column=0,pady=2,sticky="w")
            if vals:
                ttk.Combobox(cf,textvariable=self.baud_var,values=vals,
                              state="readonly",width=wid).grid(row=row,column=1,sticky="w")
            else:
                self.port_combo=ttk.Combobox(cf,textvariable=self.port_var,
                                               width=wid,state="readonly")
                self.port_combo.grid(row=row,column=1,sticky="w")
                tk.Button(cf,text="↺",bg=BG,fg=TEXT_DARK,relief="flat",font=FONT_SM,
                           padx=4,cursor="hand2",command=self._refresh_ports).grid(
                               row=row,column=2,padx=4)

        self.conn_btn=tk.Button(p,text="⬤  CONECTAR",bg=ACCENT2,fg=WHITE,
                                 font=(FN,9,"bold"),relief="flat",cursor="hand2",
                                 pady=6,activebackground="#1e8449",
                                 command=self._toggle_connect)
        self.conn_btn.pack(fill="x",padx=10,pady=(4,8))

        # ── Material ──
        self._sec(p,"MATERIAL")
        mf=tk.Frame(p,bg=SIDEBAR_BG); mf.pack(fill="x",padx=10,pady=(0,8))
        for col,(lbl,var,mx) in enumerate([("Ancho mm:",self.mat_w,RS720C_W_MM),
                                            ("Largo mm:",self.mat_h,MAX_MM)]):
            tk.Label(mf,text=lbl,bg=SIDEBAR_BG,fg=TEXT_MED,
                     font=FONT_SM).grid(row=0,column=col*2,padx=(0,2),sticky="w")
            e=tk.Spinbox(mf,from_=10,to=mx,increment=10,width=7,
                          relief="flat",bg=BG,fg=TEXT_DARK,font=FONT_SM,bd=1)
            if col==0:
                e.delete(0,"end"); e.insert(0,"720")
                e.configure(command=lambda:self._set_mat(float(e.get()),self.mat_h))
                self._mat_w_spin=e
            else:
                e.delete(0,"end"); e.insert(0,"500")
                e.configure(command=lambda:self._set_mat(self.mat_w,float(e.get())))
                self._mat_h_spin=e
            e.grid(row=0,column=col*2+1,padx=(0,8))

        # ── Perfil de cuchilla ──
        self._sec(p,"PERFIL DE CUCHILLA")
        pf=tk.Frame(p,bg=SIDEBAR_BG); pf.pack(fill="x",padx=10,pady=(0,4))
        bc=ttk.Combobox(pf,textvariable=self.blade_var,
                         values=list(BLADE_PROFILES.keys()),state="readonly",font=FONT_SM)
        bc.pack(fill="x",pady=(0,3)); bc.bind("<<ComboboxSelected>>",lambda e:self._load_blade())
        self.blade_desc=tk.Label(pf,text="",bg=SIDEBAR_BG,fg=TEXT_LIGHT,
                                  font=("Segoe UI",7),wraplength=210,anchor="w")
        self.blade_desc.pack(anchor="w",pady=(0,4))

        # ── Parámetros ──
        self._sec(p,"PARÁMETROS DE CORTE")
        params=[("Velocidad","mm/s",self.speed_var,10,800,1),
                ("Fuerza",   "g",   self.force_var,10,350,1),
                ("Pasadas",  "",    self.passes_var,1,4,1),
                ("Overcut",  "mm",  self.overcut_var,0.0,3.0,0.1),
                ("Offset",   "mm",  self.offset_var,0.10,0.80,0.025)]
        for lbl,unit,var,lo,hi,res in params:
            self._param_row(p,lbl,unit,var,lo,hi,res)

        # ── Origen ──
        self._sec(p,"ORIGEN DE CORTE")
        of=tk.Frame(p,bg=SIDEBAR_BG); of.pack(fill="x",padx=10,pady=(0,8))
        for col,(lbl,var) in enumerate([("X mm:",self.ori_x_var),("Y mm:",self.ori_y_var)]):
            tk.Label(of,text=lbl,bg=SIDEBAR_BG,fg=TEXT_MED,font=FONT_SM).grid(
                row=0,column=col*2,padx=(0,2),sticky="w")
            tk.Spinbox(of,textvariable=var,from_=0,to=100,increment=0.5,
                        width=7,relief="flat",bg=BG,fg=TEXT_DARK,font=FONT_SM,
                        bd=1).grid(row=0,column=col*2+1,padx=(0,8))

        # ── Opciones ──
        self._sec(p,"OPCIONES")
        of2=tk.Frame(p,bg=SIDEBAR_BG); of2.pack(fill="x",padx=10,pady=(0,8))
        for txt,var in [("Espejo horizontal",self.mirror_var),
                         ("Modo desmalezado",  self.weed_var)]:
            ttk.Checkbutton(of2,text=txt,variable=var).pack(anchor="w",pady=1)

        # ── Acciones ──
        self._sec(p,"ACCIONES DE CORTE")
        af=tk.Frame(p,bg=SIDEBAR_BG); af.pack(fill="x",padx=10,pady=(0,4))

        tk.Button(af,text="⟳  Generar HPGL  (F5)",bg=ACCENT,fg=WHITE,
                   font=(FN,9,"bold"),relief="flat",cursor="hand2",pady=6,
                   activebackground=ACCENT_H,command=self._generate).pack(fill="x",pady=(0,4))

        self.send_btn=tk.Button(af,text="▶  ENVIAR A PLOTTER  (F6)",
                                 bg="#1A6B1A",fg=WHITE,
                                 font=(FN,10,"bold"),relief="flat",cursor="hand2",pady=8,
                                 activebackground="#145214",command=self._send)
        self.send_btn.pack(fill="x",pady=(0,4))

        tk.Button(af,text="⏹  PARAR CORTE",bg=DANGER,fg=WHITE,
                   font=(FN,9),relief="flat",cursor="hand2",pady=5,
                   activebackground="#922B21",command=self._stop_cut).pack(fill="x",pady=(0,4))

        # Herramientas
        self._sec(p,"HERRAMIENTAS")
        hf=tk.Frame(p,bg=SIDEBAR_BG); hf.pack(fill="x",padx=10,pady=(0,12))
        for txt,cmd in [("🏠 Mover a origen",self._home),
                         ("📐 Test corte 10×10",self._test_cut),
                         ("💾 Guardar HPGL",self._save_plt)]:
            tk.Button(hf,text=txt,bg=BG,fg=TEXT_DARK,relief="flat",
                       font=FONT_SM,cursor="hand2",pady=4,
                       activebackground=BORDER,anchor="w",
                       command=cmd).pack(fill="x",pady=1)

    def _sec(self,parent,text):
        f=tk.Frame(parent,bg=BG,height=24); f.pack(fill="x",pady=(6,0)); f.pack_propagate(False)
        tk.Label(f,text=f"  {text}",bg=BG,fg=TEXT_MED,
                 font=(FN,7,"bold")).pack(side="left",pady=4)

    def _param_row(self,parent,label,unit,var,lo,hi,res):
        f=tk.Frame(parent,bg=SIDEBAR_BG); f.pack(fill="x",padx=10,pady=1)
        tk.Label(f,text=label,bg=SIDEBAR_BG,fg=TEXT_MED,
                 font=FONT_SM,width=10,anchor="w").pack(side="left")
        vl=tk.Label(f,bg=BG,fg=TEXT_DARK,font=(FN,8,"bold"),
                     width=7,anchor="e",relief="flat",padx=4)
        vl.pack(side="right",ipady=2)
        if unit: tk.Label(f,text=unit,bg=SIDEBAR_BG,fg=TEXT_LIGHT,
                           font=("Segoe UI",7)).pack(side="right",padx=(0,2))
        def upd(*_):
            try: vl.configure(text=f"{var.get()}")
            except: pass
        tk.Scale(f,variable=var,from_=lo,to=hi,resolution=res,orient="horizontal",
                  bg=SIDEBAR_BG,fg=TEXT_DARK,troughcolor=BG,highlightthickness=0,
                  showvalue=False,activebackground=ACCENT,
                  command=upd,length=95).pack(side="left",padx=(0,4))
        upd()

    # ── Barra de estado ───────────────────────────────
    def _build_statusbar(self):
        sb=tk.Frame(self,bg=STATUS_BG,height=26); sb.pack(fill="x",side="bottom")
        sb.pack_propagate(False)
        self.pb=ttk.Progressbar(sb,variable=self.progress,maximum=100,
                                  style="Horizontal.TProgressbar",length=180)
        self.pb.pack(side="right",padx=10,pady=5)
        tk.Label(sb,text="RS720C STUDIO",bg=STATUS_BG,fg="#445566",
                 font=(FN,7,"bold")).pack(side="right",padx=(0,8),pady=6)
        tk.Frame(sb,bg="#3D4F62",width=1,height=14).pack(side="right",pady=6)
        tk.Label(sb,textvariable=self.status_var,bg=STATUS_BG,fg="#8AABBF",
                 font=(FN,8)).pack(side="left",padx=10,pady=6)

    # ═══════════════════════════════════════════════════
    #  LÓGICA
    # ═══════════════════════════════════════════════════
    def _on_mouse_move(self,e):
        mx,my=self.canvas.cv_to_mm(e.x,e.y)
        self.pos_var.set(f"X: {mx:.1f}  Y: {my:.1f} mm")

    def _set_mat(self,w,h):
        self.mat_w=max(10,min(w,RS720C_W_MM))
        self.mat_h=max(10,min(h,MAX_MM))
        self.canvas.redraw()

    # ── Apertura de archivos ──────────────────────────
    def _open_svg(self):
        path=filedialog.askopenfilename(title="Abrir SVG",
            filetypes=[("SVG","*.svg"),("Todos","*.*")])
        if path: self._load_file(path)

    def _open_pdf(self):
        if not PYMUPDF_OK:
            messagebox.showerror("Error","Instala PyMuPDF: pip install PyMuPDF"); return
        path=filedialog.askopenfilename(title="Abrir PDF",
            filetypes=[("PDF","*.pdf"),("Todos","*.*")])
        if path: self._load_file(path)

    def _load_file(self,path):
        self._src_path=path
        self.file_var.set(path)
        self._set_status(f"Cargando  {os.path.basename(path)} …")
        self.progress.set(15)
        threading.Thread(target=self._load_thread,args=(path,),daemon=True).start()

    def _load_thread(self,path):
        try:
            ext=os.path.splitext(path)[1].lower()
            if ext==".svg":
                loader=SVGLoader(path,dpi=self.dpi_var.get())
                new_objs=loader.objects
                self.mat_w=loader.svg_w_mm or RS720C_W_MM
                self.mat_h=loader.svg_h_mm or 500.0
                # Render visual del SVG
                self._svg_render=self._render_svg(path)
            elif ext==".pdf":
                new_objs,w,h=self._parse_pdf(path)
                self.mat_w=w or RS720C_W_MM; self.mat_h=h or 500.0
                self._svg_render=self._render_pdf(path)
            else:
                self.after(0,lambda:messagebox.showerror("Error","Formato no soportado"))
                return

            self.objects=new_objs
            self.after(0,self._update_obj_list)
            self.after(0,lambda: self.canvas.zoom_fit())
            if self._svg_render:
                self.after(0,lambda: self.canvas.set_preview_image(self._svg_render))
            self.after(0,lambda: self.progress.set(100))
            n=len(new_objs)
            self.after(0,lambda: self._set_status(
                f"✔  {os.path.basename(path)}  —  {n} objeto(s)  "
                f"|  {self.mat_w:.0f}×{self.mat_h:.0f} mm"))
        except Exception as e:
            self.after(0,lambda: self._set_status(f"Error: {e}"))
            self.after(0,lambda: messagebox.showerror("Error al cargar",str(e)))
            self.after(0,lambda: self.progress.set(0))

    def _render_svg(self,path):
        """Renderiza SVG a imagen PIL de alta res para mostrar en canvas."""
        if not PIL_OK: return None
        try:
            if CAIRO_OK:
                scale=4  # alta resolución
                png=cairosvg.svg2png(url=path,
                    output_width=int(self.mat_w*scale),
                    output_height=int(self.mat_h*scale))
                img=Image.open(io.BytesIO(png)).convert("RGBA")
                # Fondo blanco
                bg=Image.new("RGBA",img.size,(255,255,255,255))
                bg.paste(img,mask=img.split()[3])
                return bg.convert("RGB")
        except: pass
        return None

    def _render_pdf(self,path):
        """Renderiza primera página PDF a imagen PIL."""
        if not PYMUPDF_OK or not PIL_OK: return None
        try:
            doc=fitz.open(path)
            page=doc[0]
            mat=fitz.Matrix(4,4)  # 4x zoom = alta res
            pix=page.get_pixmap(matrix=mat,alpha=False)
            img=Image.frombytes("RGB",[pix.width,pix.height],pix.samples)
            doc.close()
            return img
        except: return None

    def _parse_pdf(self,path):
        """Extrae paths vectoriales de un PDF."""
        objects=[]; w_mm=0; h_mm=0
        if not PYMUPDF_OK: return objects,w_mm,h_mm
        try:
            doc=fitz.open(path)
            page=doc[0]
            w_mm=page.rect.width/72*INCH
            h_mm=page.rect.height/72*INCH
            paths=page.get_drawings()
            for drw in paths:
                pts=[]
                for item in drw.get("items",[]):
                    if item[0]=="l":  # línea
                        pts.append((item[1].x/72*INCH, item[1].y/72*INCH))
                        pts.append((item[2].x/72*INCH, item[2].y/72*INCH))
                    elif item[0]=="c":  # curva
                        p0,p1,p2,p3=item[1],item[2],item[3],item[4]
                        n2=16
                        for s in range(n2+1):
                            t=s/n2; mt=1-t
                            bx=mt**3*p0.x+3*mt**2*t*p1.x+3*mt*t**2*p2.x+t**3*p3.x
                            by=mt**3*p0.y+3*mt**2*t*p1.y+3*mt*t**2*p2.y+t**3*p3.y
                            pts.append((bx/72*INCH, by/72*INCH))
                if pts:
                    col=drw.get("color",None)
                    fc=drw.get("fill",None)
                    stroke="#000000"
                    fill=None
                    if col and len(col)>=3:
                        stroke="#{:02X}{:02X}{:02X}".format(
                            int(col[0]*255),int(col[1]*255),int(col[2]*255))
                    if fc and len(fc)>=3:
                        fill="#{:02X}{:02X}{:02X}".format(
                            int(fc[0]*255),int(fc[1]*255),int(fc[2]*255))
                    obj=DesignObject([pts],stroke=stroke,fill=fill,name="PDF")
                    objects.append(obj)
            doc.close()
        except Exception as e:
            print(f"PDF parse error: {e}")
        return objects, w_mm, h_mm

    # ── Lista de objetos ──────────────────────────────
    def _update_obj_list(self):
        self.obj_tree.delete(*self.obj_tree.get_children())
        for obj in self.objects:
            vis="👁" if obj.visible else "○"
            w=f"{obj.width_mm():.1f}"
            h=f"{obj.height_mm():.1f}"
            self.obj_tree.insert("","end",iid=str(obj.id),
                values=(vis,obj.name,obj.stroke or "-",f"{w}×{h}"))
            if obj.selected:
                self.obj_tree.selection_add(str(obj.id))
        self.canvas.redraw()

    def _on_tree_select(self,e):
        sel=self.obj_tree.selection()
        sel_ids={int(s) for s in sel}
        for obj in self.objects:
            obj.selected=obj.id in sel_ids
        self.canvas.redraw()
        self._update_properties()

    def _on_tree_double(self,e):
        sel=self.obj_tree.selection()
        if sel:
            obj_id=int(sel[0])
            obj=next((o for o in self.objects if o.id==obj_id),None)
            if obj: self._change_stroke_obj(obj)

    def _obj_up(self):
        sel=[o for o in self.objects if o.selected]
        for obj in sel:
            i=self.objects.index(obj)
            if i>0: self.objects[i],self.objects[i-1]=self.objects[i-1],self.objects[i]
        self._update_obj_list()

    def _obj_down(self):
        sel=[o for o in self.objects if o.selected]
        for obj in reversed(sel):
            i=self.objects.index(obj)
            if i<len(self.objects)-1:
                self.objects[i],self.objects[i+1]=self.objects[i+1],self.objects[i]
        self._update_obj_list()

    def _obj_toggle_vis(self):
        for obj in self.objects:
            if obj.selected: obj.visible=not obj.visible
        self._update_obj_list()

    def _obj_delete(self):
        self.objects=[o for o in self.objects if not o.selected]
        self._update_obj_list()

    # ── Propiedades del objeto ────────────────────────
    def _update_properties(self):
        sel=[o for o in self.objects if o.selected]
        if sel:
            o=sel[0]
            self._prop_x.set(f"{o.bbox[0]+o.offset_x:.1f}")
            self._prop_y.set(f"{o.bbox[1]+o.offset_y:.1f}")
            self._prop_w.set(f"{o.width_mm():.1f}")
            self._prop_h.set(f"{o.height_mm():.1f}")
            self.stroke_swatch.configure(bg=o.stroke or "#000000")
            self.fill_swatch.configure(bg=o.fill if o.fill else "#ECECEC")

    def _change_stroke(self,e=None):
        sel=[o for o in self.objects if o.selected]
        if sel: self._change_stroke_obj(sel[0])

    def _change_stroke_obj(self,obj):
        res=colorchooser.askcolor(color=obj.stroke,title="Color de trazo")
        if res and res[1]:
            for o in self.objects:
                if o.selected: o.stroke=res[1].upper()
            self._update_properties(); self.canvas.redraw(); self._update_obj_list()

    def _change_fill(self,e=None):
        sel=[o for o in self.objects if o.selected]
        if not sel: return
        res=colorchooser.askcolor(color=sel[0].fill or "#FFFFFF",title="Color de relleno")
        if res and res[1]:
            for o in self.objects:
                if o.selected: o.fill=res[1].upper()
            self._update_properties(); self.canvas.redraw()

    def _toggle_fill(self):
        for obj in self.objects:
            if not self.show_fill_var.get(): obj._temp_fill=obj.fill; obj.fill=None
            else: obj.fill=getattr(obj,"_temp_fill",obj.fill)
        self.canvas.redraw()

    # ── Edición ───────────────────────────────────────
    def _sel_all(self):
        for o in self.objects: o.selected=True
        self._update_obj_list()

    def _desel_all(self):
        for o in self.objects: o.selected=False
        self._update_obj_list()

    def _duplicate(self):
        new=[]
        for o in self.objects:
            if o.selected:
                c=copy.deepcopy(o)
                DesignObject._id_counter+=1
                c.id=DesignObject._id_counter
                c.name=f"Copia de {o.name}"
                c.offset_x+=10; c.offset_y+=10
                new.append(c)
        self.objects.extend(new)
        self._update_obj_list()

    def _flip_h(self):
        for o in self.objects:
            if o.selected:
                cx=(o.bbox[0]+o.bbox[2])/2
                o.paths_mm=[[(2*cx-x,y) for x,y in p] for p in o.paths_mm]
                o._update_bbox()
        self.canvas.redraw()

    def _flip_v(self):
        for o in self.objects:
            if o.selected:
                cy=(o.bbox[1]+o.bbox[3])/2
                o.paths_mm=[[(x,2*cy-y) for x,y in p] for p in o.paths_mm]
                o._update_bbox()
        self.canvas.redraw()

    # ── Zoom ──────────────────────────────────────────
    def _zoom_in(self):  self.canvas.zoom_in()
    def _zoom_out(self): self.canvas.zoom_out()
    def _zoom_fit(self): self.canvas.zoom_fit()
    def _zoom_100(self): self.canvas.zoom_100()

    # ── Generación HPGL ──────────────────────────────
    def _generate(self):
        vis=[o for o in self.objects if o.visible]
        if not vis:
            messagebox.showwarning("Aviso","No hay objetos visibles. Carga un SVG o PDF."); return
        self._set_status("Generando HPGL de alta precisión…"); self.progress.set(10)
        threading.Thread(target=self._gen_thread,daemon=True).start()

    def _gen_thread(self):
        try:
            vis=[o for o in self.objects if o.visible]
            gen=HPGLGenerator(
                speed=self.speed_var.get(), force=self.force_var.get(),
                offset=self.offset_var.get(), passes=self.passes_var.get(),
                overcut=self.overcut_var.get(),
                origin_x=self.ori_x_var.get(), origin_y=self.ori_y_var.get(),
                mirror=self.mirror_var.get())
            self.hpgl_data=gen.generate(vis,mat_w_mm=self.mat_w)
            n=len(self.hpgl_data.split("\n"))
            self.after(0,lambda: self.progress.set(100))
            self.after(0,lambda: self._set_status(
                f"✔  HPGL generado  —  {n} comandos  |  "
                f"{len(vis)} objeto(s)  |  "
                f"Mat: {self.mat_w:.0f}×{self.mat_h:.0f} mm"))
        except Exception as e:
            self.after(0,lambda: messagebox.showerror("Error",str(e)))
            self.after(0,lambda: self._set_status(f"Error: {e}"))

    # ── Plotter ───────────────────────────────────────
    def _refresh_ports(self):
        ports=PlotterComm.list_ports()
        for cb in (getattr(self,"port_combo",None),
                   getattr(self,"port_quick",None)):
            if cb: cb["values"]=ports
        if ports: self.port_var.set(ports[0])

    def _toggle_connect(self):
        if not self.connected:
            try:
                self.comm=PlotterComm(self.port_var.get(),self.baud_var.get())
                self.comm.connect(); self.connected=True
                self.conn_btn.configure(text="⬤  DESCONECTAR",bg=DANGER)
                self.conn_badge.configure(text="⬤ CONECTADO",fg=ACCENT2)
                self._set_status(f"Conectado a {self.port_var.get()}")
            except Exception as e:
                messagebox.showerror("Error de conexión",str(e))
        else:
            if self.comm: self.comm.disconnect()
            self.connected=False
            self.conn_btn.configure(text="⬤  CONECTAR",bg=ACCENT2)
            self.conn_badge.configure(text="⬤ DESCONECTADO",fg="#FF7070")
            self._set_status("Desconectado")

    def _send(self):
        if not self.hpgl_data:
            messagebox.showwarning("Aviso","Genera el HPGL primero (F5)."); return
        if not self.connected:
            messagebox.showwarning("Aviso","Conecta la plotter primero."); return
        vis=[o for o in self.objects if o.visible]
        if not messagebox.askyesno("Confirmar corte",
            f"¿Iniciar corte en {self.port_var.get()}?\n\n"
            f"Objetos: {len(vis)}\n"
            f"Material: {self.mat_w:.0f} × {self.mat_h:.0f} mm\n"
            f"Perfil: {self.blade_var.get()}\n"
            f"Velocidad: {self.speed_var.get()} mm/s  |  Fuerza: {self.force_var.get()} g\n"
            f"Pasadas: {self.passes_var.get()}"): return
        self.progress.set(0); self._set_status("Enviando a plotter…")
        self.send_btn.configure(state="disabled",text="Enviando…")
        threading.Thread(target=self._send_thread,daemon=True).start()

    def _send_thread(self):
        try:
            def cb(pct):
                self.after(0,lambda: self.progress.set(pct))
                bar="█"*int(pct/5)+"░"*(20-int(pct/5))
                self.after(0,lambda: self._set_status(f"Cortando… {pct}%  ▐{bar}▌"))
            self.comm.send(self.hpgl_data,cb=cb)
            self.after(0,lambda: self.progress.set(100))
            self.after(0,lambda: self._set_status("✔  Corte completado"))
            self.after(0,lambda: self.send_btn.configure(
                state="normal",text="▶  ENVIAR A PLOTTER  (F6)"))
            self.after(0,lambda: messagebox.showinfo("✔ Listo","Corte enviado correctamente."))
        except Exception as e:
            self.after(0,lambda: messagebox.showerror("Error",str(e)))
            self.after(0,lambda: self.send_btn.configure(
                state="normal",text="▶  ENVIAR A PLOTTER  (F6)"))

    def _stop_cut(self):
        if self.comm and self.connected: self.comm.stop()
        self._set_status("⏹  Corte detenido")

    def _home(self):
        if not self.connected:
            messagebox.showwarning("Aviso","Conecta primero."); return
        try:
            ox,oy=mm_hpgl(self.ori_x_var.get()),mm_hpgl(self.ori_y_var.get())
            self.comm._s.write(f"PU{ox},{oy};\n".encode())
            self._set_status("Movido al origen")
        except Exception as e: messagebox.showerror("Error",str(e))

    def _test_cut(self):
        if not self.connected:
            messagebox.showwarning("Aviso","Conecta primero."); return
        gen=HPGLGenerator(speed=self.speed_var.get(),force=self.force_var.get(),
                           offset=self.offset_var.get(),passes=1,overcut=0.5,
                           origin_x=self.ori_x_var.get(),origin_y=self.ori_y_var.get())
        test_obj=DesignObject([[(0,0),(10,0),(10,10),(0,10),(0,0)]])
        hpgl=gen.generate([test_obj])
        try:
            self.comm._s.write(hpgl.encode())
            self._set_status("Test 10×10 mm enviado")
        except Exception as e: messagebox.showerror("Error",str(e))

    def _calibrate(self):
        messagebox.showinfo("Calibrar origen",
            "1. Posiciona la cuchilla en el punto de inicio\n"
            "2. Ajusta Origen X e Y en el panel derecho\n"
            "3. Usa 'Mover a origen' para verificar\n"
            "4. Usa 'Test 10×10' para confirmar el corte")

    def _save_plt(self):
        if not self.hpgl_data:
            messagebox.showwarning("Aviso","Genera primero el HPGL (F5)."); return
        path=filedialog.asksaveasfilename(defaultextension=".plt",
            filetypes=[("PLT/HPGL","*.plt *.hpgl"),("Texto","*.txt")])
        if path:
            with open(path,"w") as f: f.write(self.hpgl_data)
            self._set_status(f"Guardado: {os.path.basename(path)}")

    def _export_svg(self):
        messagebox.showinfo("Exportar SVG","Usa CorelDRAW para exportar SVG optimizado para corte.")

    def _load_blade(self):
        p=BLADE_PROFILES.get(self.blade_var.get(),{})
        self.speed_var.set(p.get("speed",400))
        self.force_var.set(p.get("force",80))
        self.offset_var.set(p.get("offset",0.25))
        self.passes_var.set(p.get("passes",1))
        self.overcut_var.set(p.get("overcut",1.0))
        if hasattr(self,"blade_desc"):
            self.blade_desc.configure(text=p.get("mat",""))

    def _about(self):
        messagebox.showinfo("Acerca de",
            "Redsail RS720C Studio  v4.0\n\n"
            "• SVG y PDF con vista de relleno real\n"
            "• Selección y movimiento de objetos\n"
            "• Canvas interactivo con reglas y grilla\n"
            "• HPGL/2 de alta precisión\n"
            "• Capacidad: 720 mm × 20 m\n\n"
            "Compatible con CorelDRAW 2026")

    def _set_status(self,msg): self.after(0,lambda: self.status_var.set(msg))


if __name__=="__main__":
    app=App()
    app.mainloop()
