/**
 * map-studio.js
 * Interactive Tactical Map Studio for Sentinel-1 SAR & Optical Flood Triage
 */

class MapStudio {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    if (!this.canvas) return;

    this.ctx = this.canvas.getContext('2d');
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);

    this.layers = {
      sar: true,
      optical: false,
      floodMask: true,
      roads: true,
      buildings: true,
      contours: true,
      evacNodes: true
    };

    this.splitRatio = 0.5; // Split-screen comparison ratio (0.0 to 1.0)
    this.zoom = 1.0;
    this.pan = { x: 0, y: 0 };
    this.isDragging = false;
    this.lastMouse = { x: 0, y: 0 };
    this.selectedFeature = null;

    // Feature datasets
    this.features = {
      buildings: [
        { id: 'BLD-101', name: 'Riverview Medical Clinic', type: 'Hospital', x: 0.52, y: 0.58, w: 22, h: 16, state: 'damaged', depth: '0.85m', risk: 'High' },
        { id: 'BLD-102', name: 'Valley Primary School', type: 'School', x: 0.48, y: 0.64, w: 26, h: 20, state: 'cutoff', depth: '1.40m', risk: 'Critical' },
        { id: 'BLD-103', name: 'North Power Substation', type: 'Utility', x: 0.38, y: 0.44, w: 20, h: 20, state: 'damaged', depth: '0.62m', risk: 'High' },
        { id: 'BLD-104', name: 'Municipal Water Plant', type: 'Water Facility', x: 0.55, y: 0.69, w: 30, h: 18, state: 'cutoff', depth: '1.75m', risk: 'Critical' },
        { id: 'BLD-105', name: 'East Ridge Depot', type: 'Logistics', x: 0.78, y: 0.38, w: 24, h: 16, state: 'dry', depth: '0.00m', risk: 'Low' },
        { id: 'BLD-106', name: 'St. Jude Community Hall', type: 'Shelter Hub', x: 0.82, y: 0.28, w: 28, h: 22, state: 'dry', depth: '0.00m', risk: 'Safe' },
        { id: 'BLD-107', name: 'West Valley Grain Silo', type: 'Agriculture', x: 0.28, y: 0.72, w: 18, h: 18, state: 'cutoff', depth: '1.20m', risk: 'Critical' },
        { id: 'BLD-108', name: 'Fire Station #4', type: 'Emergency Hub', x: 0.65, y: 0.42, w: 22, h: 18, state: 'damaged', depth: '0.35m', risk: 'Medium' }
      ],
      roads: [
        { id: 'RD-01', name: 'Route 101 North Highway', status: 'Cut-off (Bridge Submerged)', start: { x: 0.1, y: 0.62 }, end: { x: 0.9, y: 0.62 }, flooded: true },
        { id: 'RD-02', name: 'East Ridge Escarpment Way', status: 'Clear & Open', start: { x: 0.72, y: 0.18 }, end: { x: 0.88, y: 0.82 }, flooded: false },
        { id: 'RD-03', name: 'Valley Lowland Lane', status: 'Flooded (1.2m water)', start: { x: 0.45, y: 0.3 }, end: { x: 0.48, y: 0.9 }, flooded: true }
      ],
      evacNodes: [
        { id: 'EVAC-A1', name: 'High Ridge Heliport', x: 0.85, y: 0.25, capacity: '450 evacuees', status: 'Active (Medevac Ready)' },
        { id: 'BOAT-02', name: 'Canal Rescue Staging Point', x: 0.72, y: 0.65, capacity: 'Boat deployment', status: 'Active (Amphibious Unit)' },
        { id: 'SHELTER-03', name: 'Valley Secondary College', x: 0.22, y: 0.42, capacity: '800 beds', status: 'Active (Food & Warmth)' }
      ]
    };

    this.initCanvas();
    this.bindEvents();
    this.render();
  }

  initCanvas() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    this.width = rect.width;
    this.height = rect.height;

    this.canvas.width = Math.floor(this.width * this.dpr);
    this.canvas.height = Math.floor(this.height * this.dpr);
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
  }

  bindEvents() {
    window.addEventListener('resize', () => {
      this.initCanvas();
      this.render();
    });

    this.canvas.addEventListener('mousedown', (e) => {
      this.isDragging = true;
      this.lastMouse = { x: e.clientX, y: e.clientY };
    });

    window.addEventListener('mouseup', () => {
      this.isDragging = false;
    });

    this.canvas.addEventListener('mousemove', (e) => {
      if (this.isDragging) {
        const dx = (e.clientX - this.lastMouse.x) / this.zoom;
        const dy = (e.clientY - this.lastMouse.y) / this.zoom;
        this.pan.x += dx;
        this.pan.y += dy;
        this.lastMouse = { x: e.clientX, y: e.clientY };
        this.render();
      } else {
        this.checkHover(e);
      }
    });

    this.canvas.addEventListener('click', (e) => {
      this.handleClick(e);
    });

    this.canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9;
      this.zoom = Math.max(0.6, Math.min(3.0, this.zoom * zoomFactor));
      this.render();
    });
  }

  setLayer(layerName, isVisible) {
    if (this.layers.hasOwnProperty(layerName)) {
      this.layers[layerName] = isVisible;
      this.render();
    }
  }

  setSplitRatio(ratio) {
    this.splitRatio = Math.max(0, Math.min(1, ratio));
    this.render();
  }

  focusFeature(featureId) {
    const feat = this.features.buildings.find(b => b.id === featureId);
    if (feat) {
      this.selectedFeature = feat;
      // Center map on feature
      this.pan.x = (0.5 - feat.x) * this.width;
      this.pan.y = (0.5 - feat.y) * this.height;
      this.zoom = 1.6;
      this.render();
    }
  }

  checkHover(e) {
    const rect = this.canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left - this.width / 2 - this.pan.x) / this.zoom + this.width / 2;
    const my = (e.clientY - rect.top - this.height / 2 - this.pan.y) / this.zoom + this.height / 2;

    let hovered = false;
    this.features.buildings.forEach(b => {
      const bx = b.x * this.width;
      const by = b.y * this.height;
      if (Math.abs(mx - bx) < b.w && Math.abs(my - by) < b.h) {
        this.canvas.style.cursor = 'pointer';
        hovered = true;
      }
    });

    if (!hovered) {
      this.canvas.style.cursor = this.isDragging ? 'grabbing' : 'grab';
    }
  }

  handleClick(e) {
    const rect = this.canvas.getBoundingClientRect();
    const mx = (e.clientX - rect.left - this.width / 2 - this.pan.x) / this.zoom + this.width / 2;
    const my = (e.clientY - rect.top - this.height / 2 - this.pan.y) / this.zoom + this.height / 2;

    for (const b of this.features.buildings) {
      const bx = b.x * this.width;
      const by = b.y * this.height;
      if (Math.abs(mx - bx) < b.w && Math.abs(my - by) < b.h) {
        this.selectedFeature = b;
        this.render();
        if (window.onFeatureSelected) {
          window.onFeatureSelected(b);
        }
        return;
      }
    }
    this.selectedFeature = null;
    this.render();
  }

  render() {
    const ctx = this.ctx;
    const W = this.width;
    const H = this.height;

    ctx.clearRect(0, 0, W, H);
    ctx.save();

    // Map Pan & Zoom Transformation
    ctx.translate(W / 2 + this.pan.x, H / 2 + this.pan.y);
    ctx.scale(this.zoom, this.zoom);
    ctx.translate(-W / 2, -H / 2);

    // 1. Base Terrain / Optical / SAR Rendering
    this.drawBaseLayer(ctx, W, H);

    // 2. Elevation Contours
    if (this.layers.contours) {
      this.drawContours(ctx, W, H);
    }

    // 3. Roads & Bridges
    if (this.layers.roads) {
      this.drawRoads(ctx, W, H);
    }

    // 4. Sentinel-1 AI Flood Mask
    if (this.layers.floodMask) {
      this.drawFloodMask(ctx, W, H);
    }

    // 5. Buildings & Infrastructure
    if (this.layers.buildings) {
      this.drawBuildings(ctx, W, H);
    }

    // 6. Rescue Evacuation Nodes
    if (this.layers.evacNodes) {
      this.drawEvacNodes(ctx, W, H);
    }

    // 7. Selected Feature Inspector Callout
    if (this.selectedFeature) {
      this.drawSelectionCallout(ctx, W, H);
    }

    ctx.restore();

    // 8. Screen-Space Before/After Split Line
    if (this.splitRatio > 0 && this.splitRatio < 1) {
      this.drawSplitSlider(ctx, W, H);
    }
  }

  drawBaseLayer(ctx, W, H) {
    // Optical vs SAR Background
    if (this.layers.optical) {
      // True Color Pre-flood Basemap (Earthy greens and warm tones)
      const optGrad = ctx.createLinearGradient(0, 0, W, H);
      optGrad.addColorStop(0, '#1c2e22');
      optGrad.addColorStop(0.5, '#283d28');
      optGrad.addColorStop(1, '#1b2a1e');
      ctx.fillStyle = optGrad;
      ctx.fillRect(0, 0, W, H);
    } else {
      // Sentinel-1 SAR Backscatter Basemap (Dark radar reflection palette)
      const sarGrad = ctx.createLinearGradient(0, 0, W, H);
      sarGrad.addColorStop(0, '#040813');
      sarGrad.addColorStop(0.5, '#081226');
      sarGrad.addColorStop(1, '#050c1b');
      ctx.fillStyle = sarGrad;
      ctx.fillRect(0, 0, W, H);

      // Radar Speckle Noise Simulation
      ctx.fillStyle = 'rgba(0, 229, 255, 0.03)';
      for (let i = 0; i < 180; i++) {
        const sx = ((i * 37) % W);
        const sy = ((i * 59) % H);
        ctx.fillRect(sx, sy, 2, 2);
      }
    }

    // Base River Channel
    ctx.strokeStyle = this.layers.optical ? '#1d486e' : '#0a1a30';
    ctx.lineWidth = 36;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(W * 0.95, H * 0.25);
    ctx.bezierCurveTo(W * 0.75, H * 0.45, W * 0.55, H * 0.65, W * 0.1, H * 0.9);
    ctx.stroke();
  }

  drawContours(ctx, W, H) {
    ctx.save();
    ctx.strokeStyle = 'rgba(45, 156, 255, 0.12)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 5]);

    for (let r = 70; r < 360; r += 55) {
      ctx.beginPath();
      ctx.ellipse(W * 0.78, H * 0.35, r, r * 0.7, 0.2, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  drawRoads(ctx, W, H) {
    ctx.save();
    this.features.roads.forEach(r => {
      ctx.lineWidth = 3;
      ctx.strokeStyle = r.flooded ? '#FF4D4D' : '#1e385c';
      if (r.flooded) {
        ctx.shadowColor = '#FF4D4D';
        ctx.shadowBlur = 8;
      }
      ctx.beginPath();
      ctx.moveTo(r.start.x * W, r.start.y * H);
      ctx.lineTo(r.end.x * W, r.end.y * H);
      ctx.stroke();
    });
    ctx.restore();
  }

  drawFloodMask(ctx, W, H) {
    ctx.save();
    // Expanding flood polygon representing Copernicus EMS Rapid Mapping layer
    ctx.beginPath();
    ctx.moveTo(W * 0.98, H * 0.2);
    ctx.bezierCurveTo(W * 0.75, H * 0.32, W * 0.62, H * 0.48, W * 0.48, H * 0.58);
    ctx.bezierCurveTo(W * 0.35, H * 0.68, W * 0.2, H * 0.75, W * 0.02, H * 0.85);
    ctx.lineTo(W * 0.05, H * 0.98);
    ctx.bezierCurveTo(W * 0.25, H * 0.95, W * 0.45, H * 0.85, W * 0.65, H * 0.75);
    ctx.bezierCurveTo(W * 0.82, H * 0.65, W * 0.92, H * 0.45, W * 0.98, H * 0.32);
    ctx.closePath();

    ctx.fillStyle = 'rgba(45, 156, 255, 0.32)';
    ctx.fill();

    // Sharp GIS Detected Edge
    ctx.strokeStyle = '#00E5FF';
    ctx.lineWidth = 2;
    ctx.shadowColor = '#00E5FF';
    ctx.shadowBlur = 10;
    ctx.stroke();
    ctx.restore();
  }

  drawBuildings(ctx, W, H) {
    this.features.buildings.forEach(b => {
      const bx = b.x * W;
      const by = b.y * H;
      this.ctx.save();
      this.ctx.translate(bx, by);

      if (b.state === 'dry') {
        this.ctx.fillStyle = '#1e3048';
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
      } else if (b.state === 'damaged') {
        this.ctx.fillStyle = '#FFB347';
        this.ctx.strokeStyle = '#FFA01C';
        this.ctx.shadowColor = '#FFB347';
        this.ctx.shadowBlur = 10;
      } else if (b.state === 'cutoff') {
        this.ctx.fillStyle = '#FF4D4D';
        this.ctx.strokeStyle = '#FF2222';
        this.ctx.shadowColor = '#FF4D4D';
        this.ctx.shadowBlur = 12;
      }

      this.ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
      this.ctx.lineWidth = 1.5;
      this.ctx.strokeRect(-b.w / 2, -b.h / 2, b.w, b.h);

      // Label
      this.ctx.fillStyle = '#ffffff';
      this.ctx.font = '9px "JetBrains Mono", monospace';
      this.ctx.fillText(b.id, -b.w / 2, -b.h / 2 - 4);

      this.ctx.restore();
    });
  }

  drawEvacNodes(ctx, W, H) {
    this.features.evacNodes.forEach(node => {
      const nx = node.x * W;
      const ny = node.y * H;
      ctx.save();
      ctx.fillStyle = '#3DFF9A';
      ctx.shadowColor = '#3DFF9A';
      ctx.shadowBlur = 14;

      ctx.beginPath();
      ctx.arc(nx, ny, 6, 0, Math.PI * 2);
      ctx.fill();

      ctx.strokeStyle = 'rgba(61, 255, 154, 0.4)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(nx, ny, 14, 0, Math.PI * 2);
      ctx.stroke();

      ctx.fillStyle = '#3DFF9A';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.fillText(node.id, nx + 16, ny + 4);
      ctx.restore();
    });
  }

  drawSelectionCallout(ctx, W, H) {
    const b = this.selectedFeature;
    const bx = b.x * W;
    const by = b.y * H;

    ctx.save();
    // Highlight Reticle
    ctx.strokeStyle = '#00E5FF';
    ctx.lineWidth = 2;
    ctx.strokeRect(bx - b.w, by - b.h, b.w * 2, b.h * 2);

    // Callout Card Box
    const cardX = bx + 30;
    const cardY = by - 50;
    ctx.fillStyle = 'rgba(6, 14, 28, 0.95)';
    ctx.strokeStyle = '#00E5FF';
    ctx.lineWidth = 1;
    ctx.fillRect(cardX, cardY, 190, 80);
    ctx.strokeRect(cardX, cardY, 190, 80);

    // Callout Info Text
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 11px "Plus Jakarta Sans", sans-serif';
    ctx.fillText(b.name, cardX + 10, cardY + 20);

    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillStyle = b.state === 'cutoff' ? '#FF4D4D' : (b.state === 'damaged' ? '#FFB347' : '#3DFF9A');
    ctx.fillText(`STATUS: ${b.state.toUpperCase()}`, cardX + 10, cardY + 38);

    ctx.fillStyle = '#8b9bb4';
    ctx.fillText(`WATER DEPTH: ${b.depth}`, cardX + 10, cardY + 54);
    ctx.fillText(`TYPE: ${b.type}`, cardX + 10, cardY + 68);

    ctx.restore();
  }

  drawSplitSlider(ctx, W, H) {
    const splitX = W * this.splitRatio;

    ctx.save();
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.6)';
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(splitX, 0);
    ctx.lineTo(splitX, H);
    ctx.stroke();
    ctx.setLineDash([]);

    // Split Badges
    ctx.fillStyle = 'rgba(4, 9, 20, 0.85)';
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.4)';
    ctx.font = '10px "JetBrains Mono", monospace';

    // Left label
    ctx.strokeRect(splitX - 140, 16, 130, 26);
    ctx.fillRect(splitX - 140, 16, 130, 26);
    ctx.fillStyle = '#ffffff';
    ctx.fillText('PRE-EVENT OPTICAL', splitX - 130, 33);

    // Right label
    ctx.strokeRect(splitX + 10, 16, 150, 26);
    ctx.fillRect(splitX + 10, 16, 150, 26);
    ctx.fillStyle = '#00E5FF';
    ctx.fillText('SAR BACKSCATTER + MASK', splitX + 18, 33);

    ctx.restore();
  }
}

window.MapStudio = MapStudio;
