"""Small orthographic voxel renderer. No OpenGL, engine, or Python pixel loops.

World coordinates: x increases to the right in the front projection, y increases
away from a front observer (the front observer is at y < 0), and z is up. The
default camera lies at x < 0, y < 0, z > 0. Larger camera depth is nearer.
Pixel selection uses the same depth buffer as the displayed faces.
"""
from dataclasses import dataclass
import math

import cv2
import numpy as np


def _field(block, key):
    return block[key] if isinstance(block, dict) else getattr(block, key)


def block_values(block):
    """Accept id/x/y/z records, id/pos records, or (id, x, y, z) tuples."""
    if isinstance(block, (tuple, list)):
        if len(block) != 4:
            raise ValueError("A block tuple is (id, x, y, z)")
        return block[0], int(block[1]), int(block[2]), int(block[3])
    bid = _field(block, "id")
    try:
        return bid, int(_field(block, "x")), int(_field(block, "y")), int(_field(block, "z"))
    except (KeyError, AttributeError):
        pos = _field(block, "pos")
        return bid, int(pos[0]), int(pos[1]), int(pos[2])


def camera_basis(yaw_deg, pitch_deg):
    yaw, pitch = np.radians([float(yaw_deg), float(pitch_deg)])
    right = np.array([np.cos(yaw), np.sin(yaw), 0.], dtype=np.float64)
    up = np.array([-np.sin(yaw) * np.sin(pitch), np.cos(yaw) * np.sin(pitch), np.cos(pitch)])
    camera = np.array([np.sin(yaw) * np.cos(pitch), -np.cos(yaw) * np.cos(pitch), np.sin(pitch)])
    return right, up, camera


@dataclass
class VisibleFace:
    block_id: object
    cell: tuple
    normal: tuple
    polygon: np.ndarray
    centroid: tuple
    world_center: tuple
    pixels: int = 0


class Scene:
    """A rendered scene and hit-test data. Buffers remain valid until next render."""
    def __init__(self, viewport, pivot, unit, right, up, camera, ids, id_buffer,
                 face_buffer, depth_buffer, faces, board_size):
        self.viewport = tuple(viewport)
        self.pivot = np.asarray(pivot, dtype=np.float64)
        self.unit = float(unit)
        self.right, self.up, self.camera = right, up, camera
        self.ids = tuple(ids)
        self.id_buffer, self.face_buffer, self.depth_buffer = id_buffer, face_buffer, depth_buffer
        self.faces = faces
        self.board_size = tuple(board_size)
        self.screen_center = ((viewport[0] + viewport[2]) * .5, (viewport[1] + viewport[3]) * .5)

    def project(self, world):
        points = np.asarray(world, dtype=np.float64)
        delta = points - self.pivot
        px = self.screen_center[0] + (delta @ self.right) * self.unit
        py = self.screen_center[1] - (delta @ self.up) * self.unit
        return np.stack((px, py), axis=-1)

    project_points = project

    def _pixel(self, x, y):
        # Rasterization samples pixel centres at integer + .5.
        ix, iy = int(math.floor(x)) - self.viewport[0], int(math.floor(y)) - self.viewport[1]
        if 0 <= ix < self.id_buffer.shape[1] and 0 <= iy < self.id_buffer.shape[0]:
            return iy, ix
        return None

    def pick(self, x, y):
        pixel = self._pixel(x, y)
        if pixel is None:
            return None
        index = int(self.id_buffer[pixel])
        return self.ids[index] if index >= 0 else None

    def face_at(self, x, y):
        pixel = self._pixel(x, y)
        if pixel is None:
            return None
        index = int(self.face_buffer[pixel])
        return self.faces[index] if index >= 0 else None

    def world_floor(self, x, y, z=0.):
        """Intersect the current orthographic screen ray with world z=constant."""
        if abs(self.camera[2]) < 1e-8:
            return None
        rx = (float(x) - self.screen_center[0]) / self.unit
        uy = (self.screen_center[1] - float(y)) / self.unit
        p = self.pivot + self.right * rx + self.up * uy
        return p + self.camera * ((float(z) - p[2]) / self.camera[2])

    def cell(self, x, y, z=0.):
        p = self.world_floor(x, y, z)
        if p is None:
            return None
        xx, yy = int(math.floor(p[0])), int(math.floor(p[1]))
        return (xx, yy) if 0 <= xx < self.board_size[0] and 0 <= yy < self.board_size[1] else None

    def snap_cell(self, x, y, heights=None, radius=0.92):
        """Nearest visible column top, useful for drag placement previews.

        heights may be a mapping (x,y)->integer or an array indexed [x,y].
        This does not change the project's legality/stacking rules.
        """
        best, best_d = None, (self.unit * radius) ** 2
        for xx in range(self.board_size[0]):
            for yy in range(self.board_size[1]):
                height = 0 if heights is None else (heights.get((xx, yy), 0) if hasattr(heights, 'get') else heights[xx, yy])
                p = self.project((xx + .5, yy + .5, float(height)))
                distance = (p[0] - x) ** 2 + (p[1] - y) ** 2
                if distance < best_d:
                    best, best_d = (xx, yy), distance
        return best


# Face corners are translated by each integer cell; shared internal faces vanish.
_FACES = (
    ((-1, 0, 0), ((0,0,0), (0,0,1), (0,1,1), (0,1,0))),
    ((1, 0, 0), ((1,0,0), (1,1,0), (1,1,1), (1,0,1))),
    ((0, -1, 0), ((0,0,0), (1,0,0), (1,0,1), (0,0,1))),
    ((0, 1, 0), ((0,1,0), (0,1,1), (1,1,1), (1,1,0))),
    ((0, 0, -1), ((0,0,0), (0,1,0), (1,1,0), (1,0,0))),
    ((0, 0, 1), ((0,0,1), (1,0,1), (1,1,1), (0,1,1))),
)


class Renderer:
    """Vectorized, local-ROI depth rendering; geometry and last raster are cached."""
    def __init__(self):
        self._mesh_key = None
        self._mesh = []
        self._raster_key = None
        self._scene = None
        self._bgr = None
        self._alpha = None
        self.mesh_face_count = 0
        self.raster_count = 0

    def _geometry(self, values):
        key = tuple(values)
        if key == self._mesh_key:
            return self._mesh
        occupied = {(x,y,z) for _,x,y,z in values}
        if len(occupied) != len(values):
            raise ValueError("Overlapping cells cannot be rendered as a legal structure")
        if len({value[0] for value in values}) != len(values):
            raise ValueError("Block IDs must be unique")
        mesh = []
        for index, (bid,x,y,z) in enumerate(values):
            offset = np.array((x,y,z), dtype=np.float64)
            for normal, corners in _FACES:
                if (x + normal[0], y + normal[1], z + normal[2]) in occupied:
                    continue
                corners = np.asarray(corners, dtype=np.float64) + offset
                mesh.append((index, bid, (x,y,z), np.asarray(normal, dtype=np.float64), corners, np.mean(corners, axis=0)))
        self._mesh_key, self._mesh = key, mesh
        self.mesh_face_count = len(mesh)
        return mesh

    @staticmethod
    def _fit(values, viewport, board_size, grid, height_limit, pivot):
        width, height = viewport[2] - viewport[0], viewport[3] - viewport[1]
        if grid:
            bounds_lo = np.array((0., 0., 0.))
            bounds_hi = np.array((float(board_size[0]), float(board_size[1]), float(height_limit)))
        elif values:
            xyz = np.asarray([v[1:] for v in values], dtype=np.float64)
            bounds_lo, bounds_hi = xyz.min(axis=0), xyz.max(axis=0) + 1.
        else:
            bounds_lo, bounds_hi = np.zeros(3), np.ones(3)
        center = (bounds_lo + bounds_hi) * .5 if pivot is None else np.asarray(pivot, dtype=np.float64)
        # Rotation-independent fit prevents a rotating model from pumping in size.
        radius = max(1., float(np.max(np.linalg.norm(np.asarray([
            (x,y,z) for x in (bounds_lo[0], bounds_hi[0])
            for y in (bounds_lo[1], bounds_hi[1]) for z in (bounds_lo[2], bounds_hi[2])]) - center, axis=1))))
        return center, min(width, height) * .43 / radius

    def render(self, image, blocks, viewport, yaw_deg=-35., pitch_deg=28., scale=1.,
               selected_id=None, grid=False, preview=None, board_size=(5,4),
               pivot=None, height_limit=4, unit_px=None):
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("Renderer expects a uint8 BGR image")
        vx1,vy1,vx2,vy2 = map(int, viewport)
        if not (0 <= vx1 < vx2 <= image.shape[1] and 0 <= vy1 < vy2 <= image.shape[0]):
            raise ValueError("Viewport must lie inside the image")
        viewport = (vx1,vy1,vx2,vy2)
        values = [block_values(b) for b in blocks]
        if len(values) > 24:
            raise ValueError("Maximum supported structure size is 24 blocks")
        if any(min(v[1:]) < 0 for v in values):
            raise ValueError("Negative cell coordinates are invalid")
        if not np.isfinite([yaw_deg,pitch_deg,scale]).all() or scale <= 0:
            raise ValueError("Camera parameters must be finite and scale positive")
        mesh = self._geometry(values)
        center, base_unit = self._fit(values, viewport, board_size, grid, height_limit, pivot)
        unit = (float(unit_px) if unit_px is not None else base_unit) * float(np.clip(scale, .45, 2.2))
        if unit <= 0 or not np.isfinite(unit):
            raise ValueError("Unit size must be finite and positive")
        right, up, camera = camera_basis(yaw_deg, pitch_deg)
        preview_key = tuple(preview) if preview is not None else None
        key = (tuple(values),viewport,float(yaw_deg)%360,float(pitch_deg),float(unit),tuple(center),selected_id,bool(grid),tuple(board_size),preview_key)
        if key != self._raster_key:
            self._raster(mesh, values, viewport, center, unit, right, up, camera,
                         selected_id, grid, preview, board_size)
            self._raster_key = key
            self.raster_count += 1
        roi = image[vy1:vy2,vx1:vx2]
        # cv2.copyTo works on the strided frame ROI directly. A previous RGBA
        # fancy-index copy visited the entire ROI twice and made Nx3 temporaries.
        cv2.copyTo(self._bgr,self._opaque_mask,roi)
        yy,xx = self._trans_pixels
        if len(yy):
            a = self._trans_alpha
            roi[yy,xx] = (roi[yy,xx] * (1-a) + self._bgr[yy,xx] * a).astype(np.uint8)
        return self._scene

    def _raster(self, mesh, values, viewport, pivot, unit, right, up, camera,
                selected_id, grid, preview, board_size):
        width, height = viewport[2]-viewport[0], viewport[3]-viewport[1]
        shape = (height,width)
        if self._bgr is None or self._bgr.shape[:2] != shape:
            self._bgr = np.zeros((height,width,3), dtype=np.uint8)
            self._alpha = np.zeros(shape,dtype=np.uint8)
            self._id_buffer = np.full(shape,-1,dtype=np.int16)
            self._face_buffer = np.full(shape,-1,dtype=np.int16)
            self._depth = np.full(shape,-np.inf,dtype=np.float32)
        else:
            self._bgr.fill(0)
            self._alpha.fill(0)
            self._id_buffer.fill(-1)
            self._face_buffer.fill(-1)
            self._depth.fill(-np.inf)
        scene = Scene(viewport,pivot,unit,right,up,camera,[v[0] for v in values],
                      self._id_buffer,self._face_buffer,self._depth,[],board_size)
        self._scene = scene
        if grid:
            self._draw_grid(scene, board_size)
        faces = scene.faces
        face_data = []
        # Per-face rectangles use broadcast arrays; there are no Python pixel loops.
        for block_index,bid,cell,normal,corners,center in mesh:
            denominator = float(normal @ camera)
            if denominator <= 1e-7:
                continue
            polygon = scene.project(corners)
            local = polygon - np.asarray(viewport[:2])
            x0 = max(0,int(np.floor(local[:,0].min())))
            y0 = max(0,int(np.floor(local[:,1].min())))
            x1 = min(width,int(np.ceil(local[:,0].max()))+1)
            y1 = min(height,int(np.ceil(local[:,1].max()))+1)
            if x0 >= x1 or y0 >= y1:
                continue
            rx = (np.arange(x0,x1,dtype=np.float32)[None,:]+.5-width*.5)/unit
            uy = (height*.5-np.arange(y0,y1,dtype=np.float32)[:,None]-.5)/unit
            depth = ((normal @ (center-pivot)) - (normal @ right)*rx - (normal @ up)*uy) / denominator
            covered = np.ones(depth.shape,dtype=bool)
            # A ray hit must lie on this unit square, not only inside an integer
            # rounded polygon. This makes display/picking precise at boundaries.
            for axis in range(3):
                if normal[axis] != 0:
                    continue
                world = pivot[axis] + right[axis]*rx + up[axis]*uy + camera[axis]*depth
                covered &= (world >= cell[axis]-1e-6) & (world <= cell[axis]+1.+1e-6)
            buf = self._depth[y0:y1,x0:x1]
            update = covered & (depth > buf + 1e-6)
            face_index = len(faces)
            faces.append(VisibleFace(bid,cell,tuple(map(int,normal)),polygon,tuple(np.mean(polygon,axis=0)),tuple(center)))
            light = np.asarray((-.38,-.48,.79))
            shade = int(np.clip(116 + 52 * float(normal @ light),75,185))
            if bid == selected_id:
                shade = min(225,shade+43)
            layer = self._bgr[y0:y1,x0:x1]
            layer[update] = (shade,shade,shade)
            self._alpha[y0:y1,x0:x1][update] = 255
            buf[update] = depth[update]
            self._id_buffer[y0:y1,x0:x1][update] = block_index
            self._face_buffer[y0:y1,x0:x1][update] = face_index
            face_data.append((face_index,block_index,normal,center,local,(x0,y0,x1,y1)))
        # Edges are clipped by actual visible face ownership. Hidden selected
        # blocks cannot produce x-ray outlines through front blocks.
        for face_index,block_index,normal,center,local,bounds in face_data:
            x0,y0,x1,y1 = bounds
            visible = self._face_buffer[y0:y1,x0:x1] == face_index
            pixels = int(np.count_nonzero(visible))
            faces[face_index].pixels = pixels
            if pixels == 0:
                continue
            thickness = 3 if faces[face_index].block_id == selected_id else 1
            pad = thickness + 1
            ex0,ey0,ex1,ey1 = max(0,x0-pad),max(0,y0-pad),min(width,x1+pad),min(height,y1+pad)
            line = np.zeros((ey1-ey0,ex1-ex0),dtype=np.uint8)
            polygon = np.rint(local - np.array((ex0,ey0))).astype(np.int32)
            cv2.polylines(line,[polygon],True,255,thickness,cv2.LINE_AA)
            owner = self._face_buffer[ey0:ey1,ex0:ex1] == face_index
            dilated = cv2.dilate(owner.astype(np.uint8),np.ones((2*pad+1,2*pad+1),np.uint8)) > 0
            block_owners = self._id_buffer[ey0:ey1,ex0:ex1]
            allowed = owner | ((block_owners == -1) & dilated)
            line[~allowed] = 0
            active = line > 0
            target = self._bgr[ey0:ey1,ex0:ex1]
            white = 255 if thickness == 3 else 222
            # Alpha antialias over the already shaded face, without spilling
            # highlights into any other cube's occupied pixels.
            a = line[active].astype(np.float32)[:,None]/255.
            target[active] = (target[active]*(1-a)+white*a).astype(np.uint8)
            alpha = self._alpha[ey0:ey1,ex0:ex1]
            alpha[active] = np.maximum(alpha[active],line[active])
        if preview is not None:
            self._draw_preview(scene,preview)
        self._opaque_mask = cv2.compare(self._alpha,255,cv2.CMP_EQ)
        self._trans_pixels = np.where((self._alpha>0)&(self._alpha<255))
        self._trans_alpha = self._alpha[self._trans_pixels].astype(np.float32)[:,None]/255.

    def _draw_grid(self, scene, board_size):
        ox,oy = scene.viewport[:2]
        for x in range(board_size[0]+1):
            points = np.rint(scene.project(((x,0,0),(x,board_size[1],0))) - (ox,oy)).astype(np.int32)
            cv2.line(self._bgr,tuple(points[0]),tuple(points[1]),(180,180,180),1,cv2.LINE_AA)
            cv2.line(self._alpha,tuple(points[0]),tuple(points[1]),115,1,cv2.LINE_AA)
        for y in range(board_size[1]+1):
            points = np.rint(scene.project(((0,y,0),(board_size[0],y,0))) - (ox,oy)).astype(np.int32)
            cv2.line(self._bgr,tuple(points[0]),tuple(points[1]),(180,180,180),1,cv2.LINE_AA)
            cv2.line(self._alpha,tuple(points[0]),tuple(points[1]),115,1,cv2.LINE_AA)

    def _draw_preview(self, scene, preview):
        # Wire ghost is not in the pick buffer and cannot select a future block.
        x,y,z = map(float,preview[:3])
        valid = bool(preview[3]) if len(preview)>3 else True
        color = (228,235,228,210) if valid else (140,140,210,210)
        offset = np.array((x,y,z))
        origin = np.asarray(scene.viewport[:2])
        height,width = self._depth.shape
        for normal,corners in _FACES:
            normal = np.asarray(normal,dtype=np.float64)
            denominator = float(normal @ scene.camera)
            if denominator <= 1e-7:
                continue
            world_corners = np.asarray(corners)+offset
            local = scene.project(world_corners)-origin
            x0,y0 = max(0,int(local[:,0].min())-3),max(0,int(local[:,1].min())-3)
            x1,y1 = min(width,int(local[:,0].max())+4),min(height,int(local[:,1].max())+4)
            if x0>=x1 or y0>=y1:
                continue
            line = np.zeros((y1-y0,x1-x0),dtype=np.uint8)
            polygon = np.rint(local-(x0,y0)).astype(np.int32)
            cv2.polylines(line,[polygon],True,255,2,cv2.LINE_AA)
            rx = (np.arange(x0,x1,dtype=np.float32)[None,:]+.5-width*.5)/scene.unit
            uy = (height*.5-np.arange(y0,y1,dtype=np.float32)[:,None]-.5)/scene.unit
            depth = ((normal @ (world_corners.mean(axis=0)-scene.pivot))
                     -(normal @ scene.right)*rx-(normal @ scene.up)*uy)/denominator
            # A placement preview also respects occupied foreground geometry.
            line[depth < self._depth[y0:y1,x0:x1]-.04] = 0
            active = line>0
            target = self._bgr[y0:y1,x0:x1]
            a = line[active].astype(np.float32)[:,None]/255.
            target[active] = (target[active]*(1-a)+np.asarray(color[:3])*a).astype(np.uint8)
            alpha = self._alpha[y0:y1,x0:x1]
            alpha[active] = np.maximum(alpha[active],line[active])
