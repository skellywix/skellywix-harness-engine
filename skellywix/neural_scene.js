// The canvas presents the same component IDs and directed relationships as the inspector.
const HOME = {yaw: -.27, pitch: .18, zoom: 1};
const PULSE_MS = 900;
const TURN = Math.PI * 2;
const CLUSTERS = {
  model: [.16, -.93, -.43, '#aab0dc'], memory: [-.93, -.62, .48, '#a8c0ed'],
  vector: [1.02, -.53, .42, '#88bdad'], mcp: [.94, .29, -.45, '#92aec8'],
  tool: [.42, .74, .46, '#83a3b5'], hook: [-1.05, .39, .36, '#c3a4bd'],
  ttsr: [-.42, .87, -.59, '#ae9ec4'], checks: [.52, .57, -.92, '#85afa9'],
  automation: [-.84, .18, -.86, '#a9ada0'], runtime: [.28, -.16, -.72, '#a8bbc9'],
};
const ORBITS = [-.58, .58].map((height) => Array.from({length: 64}, (_, index) => {
  const angle = index * TURN / 64;
  return {x: 1.18 * Math.cos(angle), y: height, z: .86 * Math.sin(angle)};
}));
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const moduleNode = (node) => node.kind === 'vector' || /^memory:(native|mcp):/.test(node.id);

function position(id, core, module, cluster) {
  if (core) return {x: 0, y: 0, z: 0};
  let hash = 2166136261;
  for (let i = 0; i < id.length; i++) hash = Math.imul(hash ^ id.charCodeAt(i), 16777619);
  const scatter = module ? .24 : .55;
  const shift = (offset) => ((hash >>> offset & 255) / 255 - .5) * scatter;
  return {x: cluster[0] + shift(0), y: cluster[1] + shift(8), z: cluster[2] + 1.55 * shift(16)};
}

export class NeuralScene {
  constructor(canvas, controls, reducedMotion, select) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.reduced = reducedMotion;
    this.controls = controls;
    this.select = select;
    this.camera = {...HOME};
    this.points = [];
    this.links = [];
    this.labels = [];
    this.pulses = new Map();
    this.active = new Map();
    this.selected = null;
    this.rotating = false;
    this.paused = false;
    this.width = this.height = this.lastTime = this.pending = 0;
    this.frame = (time) => this.draw(time);
    this.drag = null;

    for (const button of controls.querySelectorAll('button')) button.disabled = !this.ctx;
    if (!this.ctx) return;
    new ResizeObserver(() => this.request()).observe(canvas);
    this.reduced.addEventListener('change', () => this.syncMotion());
    controls.addEventListener('click', (event) => {
      switch (event.target.closest('button')?.dataset.camera) {
        case 'left': this.orbit(-.32, 0); break;
        case 'right': this.orbit(.32, 0); break;
        case 'up': this.orbit(0, -.24); break;
        case 'down': this.orbit(0, .24); break;
        case 'in': this.zoom(1.19); break;
        case 'out': this.zoom(1 / 1.19); break;
        case 'reset':
          this.camera = {...HOME};
          this.rotating = false;
          this.syncMotion();
          break;
        case 'rotate':
          this.rotating = !this.rotating;
          this.syncMotion();
          break;
        case 'pause':
          this.paused = !this.paused;
          this.syncMotion();
          break;
      }
    });
    canvas.addEventListener('pointerdown', (event) => {
      if (!event.isPrimary || event.button !== 0) return;
      this.drag = {id: event.pointerId, x: event.clientX, y: event.clientY, moved: false};
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', (event) => {
      const drag = this.drag;
      if (drag?.id !== event.pointerId) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 5) return;
      drag.moved = true;
      drag.x = event.clientX;
      drag.y = event.clientY;
      canvas.classList.add('dragging');
      this.orbit(dx * .009, dy * .008);
    });
    canvas.addEventListener('pointerup', (event) => {
      if (this.drag?.id !== event.pointerId) return;
      if (!this.drag.moved) {
        const {left, top} = canvas.getBoundingClientRect();
        this.pick(event.clientX - left, event.clientY - top);
      }
      this.endDrag();
    });
    canvas.addEventListener('lostpointercapture', () => this.endDrag());
    this.syncMotion();
  }

  endDrag() {
    this.drag = null;
    this.canvas.classList.remove('dragging');
  }

  pick(x, y) {
    const label = this.labels.find((box) => x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h);
    if (label) { this.select(label.point.node.id); return; }
    for (let i = this.points.length - 1; i >= 0; i--) {
      const point = this.points[i];
      const radius = Math.max(7, point.radius + 4);
      if ((x - point.sx) ** 2 + (y - point.sy) ** 2 <= radius ** 2) {
        this.select(point.node.id);
        return;
      }
    }
  }

  syncMotion() {
    const blocked = this.paused || this.reduced.matches;
    if (blocked) {
      this.rotating = false;
      this.pulses.clear();
    }
    const rotate = this.controls.querySelector('[data-camera=rotate]');
    const pause = this.controls.querySelector('[data-camera=pause]');
    rotate.disabled = blocked;
    rotate.setAttribute('aria-pressed', String(this.rotating));
    pause.disabled = this.reduced.matches;
    pause.setAttribute('aria-pressed', String(blocked));
    this.lastTime = 0;
    this.request();
  }

  orbit(yaw, pitch) {
    this.camera.yaw += yaw;
    this.camera.pitch = clamp(this.camera.pitch + pitch, -1.1, 1.1);
    if (this.rotating) {
      this.rotating = false;
      this.syncMotion();
    } else {
      this.request();
    }
  }

  zoom(amount) {
    this.camera.zoom = clamp(this.camera.zoom * amount, .65, 1.8);
    this.request();
  }

  focus(id) {
    this.selected = id;
    this.request();
  }

  show(nodes, edges, active, fresh, groupForKind, trace) {
    if (!this.ctx) return;
    if (trace !== this.trace) this.pulses.clear();
    this.trace = trace;
    this.active = active;
    const points = new Map();
    for (const node of nodes.values()) {
      const cluster = CLUSTERS[groupForKind.get(node.kind)] ?? CLUSTERS[node.kind] ?? CLUSTERS.runtime;
      const core = node.id === 'runtime:OMP';
      const module = moduleNode(node);
      points.set(node.id, {...position(node.id, core, module, cluster), node, core, module, color: cluster[3]});
    }
    this.points = [...points.values()];
    const live = new Set();
    this.links = [];
    for (const [key, edge] of edges) {
      const a = points.get(edge.source);
      const b = points.get(edge.target);
      if (!a || !b) continue;
      this.links.push({key, a, b, state: edge.state, bend: {
        x: (a.x + b.x) * .5 - (b.y - a.y) * .17,
        y: (a.y + b.y) * .5 + (b.x - a.x) * .17,
        z: (a.z + b.z) * .5 + .22,
      }});
      if (edge.state) live.add(key);
    }
    for (const key of this.pulses.keys()) if (!live.has(key)) this.pulses.delete(key);
    if (!this.reduced.matches && !this.paused) {
      let delay = 0;
      const now = performance.now();
      for (const key of fresh) {
        if (!live.has(key)) continue;
        this.pulses.set(key, now + delay);
        delay = Math.min(delay + 25, 200);
      }
    }
    this.request();
  }

  request() {
    if (this.ctx && !this.pending) this.pending = requestAnimationFrame(this.frame);
  }

  draw(time) {
    this.pending = 0;
    const {canvas, ctx} = this;
    const {width, height} = canvas.getBoundingClientRect();
    if (!width || !height) return;
    const ratio = Math.min(devicePixelRatio || 1, 2);
    const px = Math.ceil(width * ratio);
    const py = Math.ceil(height * ratio);
    if (canvas.width !== px || canvas.height !== py) {
      canvas.width = px;
      canvas.height = py;
    }
    ctx.setTransform(px / width, 0, 0, py / height, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (this.rotating && this.lastTime) this.camera.yaw += Math.min(35, time - this.lastTime) * .00036;
    this.lastTime = time;
    this.width = width;
    this.height = height;
    const {yaw, pitch, zoom} = this.camera;
    const sy = Math.sin(yaw), cy = Math.cos(yaw), sp = Math.sin(pitch), cp = Math.cos(pitch);
    const scale = Math.min(width / 3.75, height / 2.65) * zoom;
    const project = (point) => {
      const across = point.x * cy - point.z * sy;
      const away = point.x * sy + point.z * cy;
      point.depth = point.y * sp + away * cp;
      point.lens = 4.6 / (4.6 - point.depth);
      point.sx = width * .5 + scale * across * point.lens;
      point.sy = height * .5 + scale * (point.y * cp - away * sp) * point.lens;
    };

    // Calibration rings sit behind the topology, not among its relationships.
    ctx.lineWidth = .8;
    ctx.strokeStyle = '#7895b32a';
    ctx.setLineDash([2, 5]);
    for (const ring of ORBITS) {
      ctx.beginPath();
      for (const [i, point] of ring.entries()) {
        project(point);
        if (i === 0) ctx.moveTo(point.sx, point.sy);
        else ctx.lineTo(point.sx, point.sy);
      }
      ctx.closePath();
      ctx.stroke();
    }
    for (const point of this.points) project(point);
    for (const link of this.links) project(link.bend);
    ctx.strokeStyle = '#688ba151';
    ctx.setLineDash([3, 4]);
    this.strokeLinks(undefined);
    ctx.setLineDash([]);
    ctx.lineCap = 'round';
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = '#89daed';
    this.strokeLinks('recorded');
    ctx.strokeStyle = '#f4a3b9';
    this.strokeLinks('problem');
    for (const link of this.links) {
      const since = time - (this.pulses.get(link.key) ?? Infinity);
      if (since >= PULSE_MS) this.pulses.delete(link.key);
      else if (since >= 0) this.impulse(link, since / PULSE_MS);
    }
    this.points.sort((a, b) => a.depth - b.depth);
    for (const point of this.points) this.node(point);
    this.drawLabels();
    if (this.rotating || this.pulses.size) this.request();
    else this.lastTime = 0;
  }

  strokeLinks(state) {
    const {ctx} = this;
    ctx.beginPath();
    for (const {a, b, bend, state: evidence} of this.links) {
      if (evidence !== state) continue;
      ctx.moveTo(a.sx, a.sy);
      ctx.quadraticCurveTo(bend.sx, bend.sy, b.sx, b.sy);
    }
    ctx.stroke();
  }

  impulse({a, b, bend, state}, t) {
    const {ctx} = this;
    const point = (value) => {
      const tail = 1 - value;
      const mid = 2 * value * tail;
      return [tail * tail * a.sx + mid * bend.sx + value * value * b.sx,
        tail * tail * a.sy + mid * bend.sy + value * value * b.sy];
    };
    const [x, y] = point(t);
    ctx.save();
    ctx.globalAlpha = Math.min(1, 7 * (1 - t));
    ctx.strokeStyle = state === 'problem' ? '#ffcfdd' : '#c7f5ff';
    ctx.lineWidth = 3.4;
    ctx.beginPath();
    ctx.moveTo(...point(Math.max(0, t - .12)));
    ctx.lineTo(x, y);
    ctx.stroke();
    ctx.fillStyle = '#f4fcff';
    ctx.beginPath();
    ctx.arc(x, y, 2.7, 0, TURN);
    ctx.fill();
    ctx.restore();
  }

  node(point) {
    const {ctx} = this;
    const {sx, sy, lens, core, module, node, color} = point;
    const activity = this.active.get(node.id);
    const visible = activity || node.id === this.selected;
    const problem = activity?.problem;
    const radius = (core ? 11 : module ? 6.1 : visible ? 4 : 2.2) * lens;
    point.radius = radius;
    const shine = problem ? '#f4a3b9' : '#91e1f1';
    if (core || visible || module) {
      const halo = ctx.createRadialGradient(sx, sy, radius * .35, sx, sy, radius * 3.1);
      halo.addColorStop(0, `${visible ? shine : color}61`);
      halo.addColorStop(1, `${visible ? shine : color}00`);
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(sx, sy, radius * 3.1, 0, TURN);
      ctx.fill();
    }
    ctx.fillStyle = visible ? shine : color;
    ctx.globalAlpha = node.state === 'disabled' ? .29 : clamp((core || visible || module ? 1 : .75) * lens, .46, 1);
    ctx.beginPath();
    if (core || problem) {
      ctx.moveTo(sx, sy - radius);
      ctx.lineTo(sx + radius, sy);
      ctx.lineTo(sx, sy + radius);
      ctx.lineTo(sx - radius, sy);
      ctx.closePath();
    } else {
      ctx.arc(sx, sy, radius, 0, TURN);
    }
    ctx.fill();
    ctx.globalAlpha = 1;
    if (node.id === this.selected) {
      ctx.strokeStyle = '#ffb6c8';
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.arc(sx, sy, radius + 4, 0, TURN);
      ctx.stroke();
    }
  }

  drawLabels() {
    const {ctx, width, height} = this;
    const rank = (p) => p.node.id === this.selected ? 0 : p.core ? 1 : p.module ? 2 : this.active.has(p.node.id) ? 3 : 4;
    const limit = width < 420 ? 6 : 12;
    const boxes = [];
    ctx.font = '600 12px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    for (const point of this.points.filter((p) => rank(p) < 4).sort((a, b) => rank(a) - rank(b) || b.depth - a.depth)) {
      if (boxes.length >= limit) break;
      const {sx, sy, radius, node} = point;
      if (sx < 0 || sx > width || sy < 0 || sy > height) continue;
      const w = Math.min(235, width - 16, Math.ceil(ctx.measureText(node.name).width) + 14);
      const h = 22;
      const left = clamp(sx < width * .5 ? sx - radius - w - 9 : sx + radius + 9, 8, width - w - 8);
      for (const shift of [0, -24, 24, -48, 48, -72, 72]) {
        const y = clamp(sy + shift - h / 2, 8, height - h - 8);
        if (boxes.some((box) => left < box.x + box.w + 4 && left + w + 4 > box.x &&
          y < box.y + box.h + 4 && y + h + 4 > box.y)) continue;
        boxes.push({point, x: left, y, w, h});
        ctx.strokeStyle = '#7399b68c';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(clamp(sx, left, left + w), y + h * .5);
        ctx.stroke();
        ctx.fillStyle = '#0e1927';
        ctx.fillRect(left, y, w, h);
        ctx.strokeStyle = this.active.has(node.id) ? '#578b9a' : '#395268';
        ctx.strokeRect(left + .5, y + .5, w - 1, h - 1);
        ctx.fillStyle = '#edf3fb';
        ctx.fillText(node.name, left + 7, y + h / 2 + .4, w - 14);
        break;
      }
    }
    this.labels = boxes;
  }
}
