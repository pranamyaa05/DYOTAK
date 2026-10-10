/**
 * FloodRadarBackground.js
 * 
 * Standalone Canvas 2D Animated Background for:
 * "Orbital Intelligence for Ground-Level Survival" (Satellite Flood Damage Mapping)
 * 
 * Features:
 * - 60fps GPU-friendly requestAnimationFrame pipeline
 * - Deep space-to-night gradient base with 3-depth parallax twinkling starfield
 * - Top-down river basin terrain with contour isolines, roads, and building footprints
 * - Periodic ~20s flood rise/recede loop with detected edge line & shimmering water
 * - Automated damage triage: flooded buildings turn amber/red with pulse alerts; cut-off roads turn red
 * - Pulsing green rescue nodes (#3DFF9A) at strategic safe elevations
 * - Sentinel-1 SAR satellite with realistic solar panels & C-band antenna boom
 * - 6-8s cyan radar pulse cone (#00E5FF) and ground swath scanline
 * - Drifting storm cloud layer penetrated by radar microwaves
 * - Subdued tactical GIS HUD overlay (<12% opacity) & gliding Before/After divider line
 * - Text-area contrast protection vignette (ensures 4.5:1 WCAG contrast)
 * - Full support for 'prefers-reduced-motion' & responsive auto-resize
 */

class FloodRadarBackground {
  constructor(canvasId, options = {}) {
    this.canvas = typeof canvasId === 'string' ? document.getElementById(canvasId) : canvasId;
    if (!this.canvas) {
      console.error(`FloodRadarBackground: Canvas element "${canvasId}" not found.`);
      return;
    }

    this.ctx = this.canvas.getContext('2d');
    this.options = Object.assign({
      palette: {
        bgBase: '#02040A',
        bgDarkBlue: '#0A1224',
        floodBlue: '#2D9CFF',
        radarCyan: '#00E5FF',
        damageAmber: '#FFB347',
        dangerRed: '#FF4D4D',
        safeGreen: '#3DFF9A',
        neutralBuilding: '#1B2A3D',
        roadNeutral: '#19283D',
        contour: 'rgba(45, 156, 255, 0.05)'
      },
      floodLoopDuration: 22000, // 22 second natural loop
      radarPulseInterval: 7500, // 7.5 seconds between radar sweeps
      showHUD: true,
      showDivider: true,
      starCount: 65,
      buildingCount: 52
    }, options);

    this.width = 0;
    this.height = 0;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);

    this.isPaused = false;
    this.prefersReducedMotion = false;
    this.animationFrameId = null;
    this.startTime = performance.now();
    this.lastRadarTime = performance.now();
    this.manualRadarTrigger = 0;

    // Simulation entities
    this.stars = [];
    this.riverPath = [];
    this.tributaries = [];
    this.contours = [];
    this.buildings = [];
    this.roads = [];
    this.rescueNodes = [];
    this.clouds = [];
    this.satellite = {
      x: 0.78, // Normalized 0-1
      y: 0.12,
      speedX: 0.0015,
      angle: -0.25, // rad
      scale: 1.0
    };

    this.radarState = {
      active: false,
      progress: 0, // 0 to 1
      duration: 2800, // ms
      startTime: 0
    };

    this.dividerState = {
      xRatio: 0.58,
      speed: 0.015,
      visible: this.options.showDivider
    };

    this.checkMotionPreference();
    this.initEntities();
    this.resize();
    this.bindEvents();

    if (!this.prefersReducedMotion) {
      this.start();
    } else {
      this.renderStaticFrame();
    }
  }

  checkMotionPreference() {
    if (window.matchMedia) {
      const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
      this.prefersReducedMotion = mediaQuery.matches;
      mediaQuery.addEventListener('change', (e) => {
        this.prefersReducedMotion = e.matches;
        if (this.prefersReducedMotion) {
          this.pause();
          this.renderStaticFrame();
        } else {
          this.start();
        }
      });
    }
  }

  bindEvents() {
    this.resizeHandler = () => this.resize();
    window.addEventListener('resize', this.resizeHandler);
  }

  resize() {
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.width = window.innerWidth;
    this.height = window.innerHeight;

    this.canvas.width = Math.floor(this.width * this.dpr);
    this.canvas.height = Math.floor(this.height * this.dpr);
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;

    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);

    // Re-generate map paths scaled to new viewport
    this.generateMapGeography();

    if (this.prefersReducedMotion || this.isPaused) {
      this.renderStaticFrame();
    }
  }

  initEntities() {
    // 1. Stars with 3 parallax depths
    this.stars = [];
    for (let i = 0; i < this.options.starCount; i++) {
      const depth = (i % 3) + 1; // 1, 2, or 3
      this.stars.push({
        x: Math.random(),
        y: Math.random() * 0.7, // Upper 70% of screen
        radius: depth === 1 ? 0.6 : (depth === 2 ? 1.0 : 1.4),
        baseAlpha: depth === 1 ? 0.25 : (depth === 2 ? 0.45 : 0.7),
        twinkleSpeed: 0.0015 + Math.random() * 0.003,
        twinklePhase: Math.random() * Math.PI * 2,
        depth
      });
    }

    // 2. Clouds layer
    this.clouds = [
      { x: 0.15, y: 0.35, rx: 220, ry: 75, speed: 0.004, opacity: 0.12 },
      { x: 0.55, y: 0.28, rx: 340, ry: 110, speed: 0.006, opacity: 0.16 },
      { x: 0.82, y: 0.45, rx: 280, ry: 90, speed: 0.005, opacity: 0.14 },
      { x: 0.35, y: 0.62, rx: 260, ry: 85, speed: 0.003, opacity: 0.10 }
    ];
  }

  generateMapGeography() {
    const W = this.width;
    const H = this.height;

    // A. Main River spline (Flows from top-right down through bottom-center/left)
    this.riverPath = [
      { x: W * 0.95, y: H * 0.25, width: 14 },
      { x: W * 0.82, y: H * 0.36, width: 22 },
      { x: W * 0.72, y: H * 0.48, width: 28 },
      { x: W * 0.58, y: H * 0.58, width: 34 },
      { x: W * 0.46, y: H * 0.68, width: 44 },
      { x: W * 0.32, y: H * 0.78, width: 56 },
      { x: W * 0.18, y: H * 0.88, width: 68 },
      { x: W * 0.05, y: H * 0.98, width: 85 }
    ];

    // B. Tributaries branching off main river
    this.tributaries = [
      // Branch 1: North-east mountain stream
      [
        { x: W * 0.92, y: H * 0.12 },
        { x: W * 0.86, y: H * 0.24 },
        { x: W * 0.82, y: H * 0.36 }
      ],
      // Branch 2: Center-east tributary
      [
        { x: W * 0.94, y: H * 0.55 },
        { x: W * 0.80, y: H * 0.56 },
        { x: W * 0.72, y: H * 0.48 }
      ],
      // Branch 3: South-east valley creek
      [
        { x: W * 0.70, y: H * 0.82 },
        { x: W * 0.56, y: H * 0.75 },
        { x: W * 0.46, y: H * 0.68 }
      ]
    ];

    // C. Topographic Contour Isolines (faint organic topographic rings)
    this.contours = [];
    const contourCenters = [
      { cx: W * 0.88, cy: H * 0.30, maxR: Math.min(W, H) * 0.35 },
      { cx: W * 0.65, cy: H * 0.75, maxR: Math.min(W, H) * 0.38 },
      { cx: W * 0.25, cy: H * 0.45, maxR: Math.min(W, H) * 0.30 }
    ];

    contourCenters.forEach(c => {
      for (let r = 50; r < c.maxR; r += 45) {
        const points = [];
        const steps = 14;
        for (let s = 0; s < steps; s++) {
          const theta = (s / steps) * Math.PI * 2;
          const wobble = 1 + 0.12 * Math.sin(theta * 3 + r);
          points.push({
            x: c.cx + Math.cos(theta) * r * wobble,
            y: c.cy + Math.sin(theta) * (r * 0.65) * wobble
          });
        }
        this.contours.push(points);
      }
    });

    // D. Roads crossing the landscape
    this.roads = [
      // Highway crossing East-West over river via bridge
      {
        points: [
          { x: W * 0.05, y: H * 0.62 },
          { x: W * 0.35, y: H * 0.64 },
          { x: W * 0.58, y: H * 0.58 }, // Bridge point
          { x: W * 0.82, y: H * 0.60 },
          { x: W * 0.98, y: H * 0.62 }
        ],
        type: 'primary',
        submergedThreshold: 0.48 // gets cut off when flood >= 0.48
      },
      // Secondary road along North-South valley
      {
        points: [
          { x: W * 0.68, y: H * 0.20 },
          { x: W * 0.66, y: H * 0.42 },
          { x: W * 0.52, y: H * 0.66 },
          { x: W * 0.42, y: H * 0.85 },
          { x: W * 0.38, y: H * 0.98 }
        ],
        type: 'secondary',
        submergedThreshold: 0.32
      },
      // East ridge road (dry escape route)
      {
        points: [
          { x: W * 0.75, y: H * 0.25 },
          { x: W * 0.88, y: H * 0.45 },
          { x: W * 0.85, y: H * 0.72 }
        ],
        type: 'secondary',
        submergedThreshold: 0.88 // rarely cut off
      }
    ];

    // E. Settlement buildings & structures
    this.buildings = [];
    const settlementClusters = [
      // Town A (Lowland river bend - heavily flooded)
      { cx: W * 0.52, cy: H * 0.62, spread: 95, count: 20, floodRisk: 0.28 },
      // Town B (Delta settlement - moderately flooded)
      { cx: W * 0.36, cy: H * 0.74, spread: 80, count: 16, floodRisk: 0.42 },
      // Village C (East upper terrace - partially flooded)
      { cx: W * 0.75, cy: H * 0.42, spread: 70, count: 12, floodRisk: 0.62 },
      // Hillside settlement (safe high ground)
      { cx: W * 0.85, cy: H * 0.28, spread: 50, count: 8, floodRisk: 0.95 }
    ];

    settlementClusters.forEach(cluster => {
      for (let i = 0; i < cluster.count; i++) {
        const angle = Math.random() * Math.PI * 2;
        const dist = Math.pow(Math.random(), 0.7) * cluster.spread;
        const bx = cluster.cx + Math.cos(angle) * dist;
        const by = cluster.cy + Math.sin(angle) * (dist * 0.7);

        // Building orientation aligned slightly with terrain
        const rot = Math.sin(bx * 0.01) * 0.3;
        const sizeW = 7 + Math.random() * 8;
        const sizeH = 6 + Math.random() * 7;

        this.buildings.push({
          x: bx,
          y: by,
          w: sizeW,
          h: sizeH,
          rot,
          floodThreshold: cluster.floodRisk + (Math.random() * 0.2 - 0.1),
          state: 'dry', // 'dry', 'damaged', 'cutoff'
          damagePulse: 0
        });
      }
    });

    // F. Safe Rescue Nodes / Emergency Evac Hubs (#3DFF9A)
    this.rescueNodes = [
      { x: W * 0.86, y: H * 0.26, label: 'EVAC-A1 [ELEV: 320m]' },
      { x: W * 0.78, y: H * 0.68, label: 'BOAT-LAUNCH 02' },
      { x: W * 0.28, y: H * 0.52, label: 'AIR-MEDEVAC HUB' }
    ];
  }

  start() {
    this.isPaused = false;
    const loop = (currentTime) => {
      if (this.isPaused) return;
      this.update(currentTime);
      this.draw(currentTime);
      this.animationFrameId = requestAnimationFrame(loop);
    };
    this.animationFrameId = requestAnimationFrame(loop);
  }

  pause() {
    this.isPaused = true;
    if (this.animationFrameId) {
      cancelAnimationFrame(this.animationFrameId);
      this.animationFrameId = null;
    }
  }

  togglePause() {
    if (this.isPaused) {
      this.start();
      return false;
    } else {
      this.pause();
      return true;
    }
  }

  toggleHUD() {
    this.options.showHUD = !this.options.showHUD;
    if (this.isPaused) this.renderStaticFrame();
    return this.options.showHUD;
  }

  toggleDivider() {
    this.dividerState.visible = !this.dividerState.visible;
    if (this.isPaused) this.renderStaticFrame();
    return this.dividerState.visible;
  }

  triggerRadarSweep() {
    this.radarState.active = true;
    this.radarState.startTime = performance.now();
    this.radarState.progress = 0;
  }

  update(now) {
    // 1. Time progression for 22s flood loop
    const elapsed = now - this.startTime;
    const loopPhase = (elapsed % this.options.floodLoopDuration) / this.options.floodLoopDuration;
    // Smooth sinusoidal breathing curve: 0 -> 1 -> 0
    this.currentFloodLevel = 0.5 - 0.5 * Math.cos(loopPhase * Math.PI * 2);

    // 2. Update building and road damage states based on flood rise
    this.buildings.forEach(b => {
      const isFlooded = this.currentFloodLevel >= b.floodThreshold;
      if (isFlooded && b.state === 'dry') {
        b.state = (this.currentFloodLevel > b.floodThreshold + 0.18) ? 'cutoff' : 'damaged';
        b.damagePulse = 1.0; // Trigger alert ring
      } else if (!isFlooded && b.state !== 'dry') {
        b.state = 'dry';
        b.damagePulse = 0;
      }
      if (b.damagePulse > 0) {
        b.damagePulse = Math.max(0, b.damagePulse - 0.02);
      }
    });

    // 3. Satellite slow orbital drift across top
    this.satellite.x += this.satellite.speedX * 0.05;
    if (this.satellite.x > 1.15) {
      this.satellite.x = -0.1;
    }

    // 4. Automatic periodic radar sweep every 7.5s
    if (!this.radarState.active && (now - this.lastRadarTime > this.options.radarPulseInterval)) {
      this.triggerRadarSweep();
      this.lastRadarTime = now;
    }

    if (this.radarState.active) {
      const radarElapsed = now - this.radarState.startTime;
      this.radarState.progress = Math.min(1.0, radarElapsed / this.radarState.duration);
      if (this.radarState.progress >= 1.0) {
        this.radarState.active = false;
      }
    }

    // 5. Cloud horizontal drift
    this.clouds.forEach(cloud => {
      cloud.x += cloud.speed * 0.02;
      if (cloud.x > 1.2) cloud.x = -0.2;
    });

    // 6. Before/After divider gliding motion
    if (this.dividerState.visible) {
      this.dividerState.xRatio += Math.sin(now * 0.0004) * 0.0004;
    }
  }

  draw(now) {
    const ctx = this.ctx;
    const W = this.width;
    const H = this.height;

    ctx.clearRect(0, 0, W, H);

    // -------------------------------------------------------------
    // LAYER 1: Deep Space-to-Night Gradient & Stars
    // -------------------------------------------------------------
    const bgGrad = ctx.createLinearGradient(0, 0, W * 0.8, H);
    bgGrad.addColorStop(0, this.options.palette.bgBase);
    bgGrad.addColorStop(0.55, '#040916');
    bgGrad.addColorStop(1, this.options.palette.bgDarkBlue);
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, W, H);

    // Stars
    this.stars.forEach(star => {
      const twinkle = Math.sin(now * star.twinkleSpeed + star.twinklePhase);
      const alpha = Math.max(0.08, star.baseAlpha + twinkle * 0.25);
      ctx.fillStyle = `rgba(215, 235, 255, ${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(star.x * W, star.y * H, star.radius, 0, Math.PI * 2);
      ctx.fill();
    });

    // -------------------------------------------------------------
    // LAYER 2: Terrain - Contours, Rivers, Roads, Buildings
    // -------------------------------------------------------------
    // A. Topographic Contours
    ctx.save();
    ctx.strokeStyle = this.options.palette.contour;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 6]);
    this.contours.forEach(pts => {
      ctx.beginPath();
      pts.forEach((p, idx) => {
        if (idx === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      });
      ctx.closePath();
      ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.restore();

    // B. River Basin Base Channels
    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    // Tributaries
    ctx.strokeStyle = '#0E1C30';
    ctx.lineWidth = 4;
    this.tributaries.forEach(ribbon => {
      ctx.beginPath();
      ribbon.forEach((pt, i) => {
        if (i === 0) ctx.moveTo(pt.x, pt.y);
        else ctx.lineTo(pt.x, pt.y);
      });
      ctx.stroke();
    });

    // Main River Channel (Deep baseline water)
    for (let i = 0; i < this.riverPath.length - 1; i++) {
      const p1 = this.riverPath[i];
      const p2 = this.riverPath[i + 1];
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p2.x, p2.y);
      ctx.strokeStyle = '#0d1d33';
      ctx.lineWidth = (p1.width + p2.width) * 0.5;
      ctx.stroke();
    }
    ctx.restore();

    // C. Road Network
    this.roads.forEach(road => {
      const isCutOff = this.currentFloodLevel >= road.submergedThreshold;
      ctx.save();
      ctx.lineWidth = road.type === 'primary' ? 2.5 : 1.2;
      ctx.strokeStyle = isCutOff ? this.options.palette.dangerRed : this.options.palette.roadNeutral;
      if (isCutOff) {
        ctx.shadowColor = this.options.palette.dangerRed;
        ctx.shadowBlur = 6;
      }
      ctx.beginPath();
      road.points.forEach((p, idx) => {
        if (idx === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      });
      ctx.stroke();
      ctx.restore();
    });

    // -------------------------------------------------------------
    // LAYER 3: Flood Rise & Shimmering Inundation Polygon
    // -------------------------------------------------------------
    this.drawFloodPolygon(now);

    // -------------------------------------------------------------
    // LAYER 4: Buildings with Triage States & Rescue Nodes
    // -------------------------------------------------------------
    this.buildings.forEach(b => {
      ctx.save();
      ctx.translate(b.x, b.y);
      ctx.rotate(b.rot);

      if (b.state === 'dry') {
        ctx.fillStyle = this.options.palette.neutralBuilding;
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
        ctx.lineWidth = 1;
        ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
        ctx.strokeRect(-b.w / 2, -b.h / 2, b.w, b.h);
      } else if (b.state === 'damaged') {
        // Submerged / Damaged: Amber
        ctx.fillStyle = this.options.palette.damageAmber;
        ctx.shadowColor = this.options.palette.damageAmber;
        ctx.shadowBlur = 8;
        ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
      } else if (b.state === 'cutoff') {
        // Deeply flooded / Cut-off: Danger Red
        ctx.fillStyle = this.options.palette.dangerRed;
        ctx.shadowColor = this.options.palette.dangerRed;
        ctx.shadowBlur = 10;
        ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
      }

      // Damage alert pulse wave when first flooded
      if (b.damagePulse > 0) {
        ctx.strokeStyle = b.state === 'cutoff' ? this.options.palette.dangerRed : this.options.palette.damageAmber;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        const pulseR = Math.max(b.w, b.h) * (1 + (1 - b.damagePulse) * 1.8);
        ctx.arc(0, 0, pulseR, 0, Math.PI * 2);
        ctx.stroke();
      }

      ctx.restore();
    });

    // Green Safe Rescue Nodes (#3DFF9A)
    this.rescueNodes.forEach(node => {
      ctx.save();
      const pulseTime = (now % 1800) / 1800;
      const ringRadius = 5 + pulseTime * 18;
      const ringAlpha = (1 - pulseTime) * 0.8;

      // Outer expanding radar ping
      ctx.strokeStyle = `rgba(61, 255, 154, ${ringAlpha})`;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(node.x, node.y, ringRadius, 0, Math.PI * 2);
      ctx.stroke();

      // Center solid core
      ctx.fillStyle = this.options.palette.safeGreen;
      ctx.shadowColor = this.options.palette.safeGreen;
      ctx.shadowBlur = 10;
      ctx.beginPath();
      ctx.arc(node.x, node.y, 4, 0, Math.PI * 2);
      ctx.fill();

      // Small tactical marker flag
      if (this.options.showHUD) {
        ctx.fillStyle = 'rgba(61, 255, 154, 0.7)';
        ctx.font = '9px "JetBrains Mono", monospace';
        ctx.fillText(node.label, node.x + 10, node.y + 3);
      }
      ctx.restore();
    });

    // -------------------------------------------------------------
    // LAYER 5: Drifting Storm Clouds Layer
    // -------------------------------------------------------------
    this.drawCloudLayer(now);

    // -------------------------------------------------------------
    // LAYER 6: Sentinel-1 Satellite & Microwave Radar Swath
    // -------------------------------------------------------------
    this.drawSentinelSatelliteAndRadar(now);

    // -------------------------------------------------------------
    // LAYER 7: Tactical HUD & Before/After Divider
    // -------------------------------------------------------------
    if (this.options.showHUD) {
      this.drawHUD(now);
    }

    if (this.dividerState.visible) {
      this.drawBeforeAfterDivider();
    }

    // -------------------------------------------------------------
    // LAYER 8: Content Contrast Vignette
    // Protects center-left headline area to maintain >4.5:1 text contrast
    // -------------------------------------------------------------
    this.drawContrastVignette();
  }

  drawFloodPolygon(now) {
    const ctx = this.ctx;
    const floodExpansion = this.currentFloodLevel; // 0 to 1
    if (floodExpansion < 0.05) return;

    ctx.save();

    // Construct left and right banks of the expanding flood polygon
    const leftBank = [];
    const rightBank = [];
    const waveOffset = Math.sin(now * 0.002) * 4;

    for (let i = 0; i < this.riverPath.length; i++) {
      const pt = this.riverPath[i];
      // Perpendicular vector along river flow
      const next = this.riverPath[Math.min(this.riverPath.length - 1, i + 1)];
      const prev = this.riverPath[Math.max(0, i - 1)];
      const dx = next.x - prev.x;
      const dy = next.y - prev.y;
      const len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len;
      const ny = dx / len;

      const baseSpread = pt.width * 0.8;
      const maxExtraSpread = (pt.width * 3.8) * floodExpansion;
      const spread = baseSpread + maxExtraSpread + waveOffset;

      leftBank.push({ x: pt.x + nx * spread, y: pt.y + ny * spread });
      rightBank.push({ x: pt.x - nx * spread * 1.15, y: pt.y - ny * spread * 1.15 });
    }

    // Draw Translucent Water Inundation Polygon (#2D9CFF, 25-35% opacity)
    ctx.beginPath();
    leftBank.forEach((p, idx) => {
      if (idx === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    });
    for (let i = rightBank.length - 1; i >= 0; i--) {
      ctx.lineTo(rightBank[i].x, rightBank[i].y);
    }
    ctx.closePath();

    const floodAlpha = 0.22 + floodExpansion * 0.12;
    ctx.fillStyle = `rgba(45, 156, 255, ${floodAlpha.toFixed(2)})`;
    ctx.fill();

    // Detected Vector Edge Line (Bright GIS segmentation border)
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.85)';
    ctx.lineWidth = 1.8;
    ctx.shadowColor = this.options.palette.radarCyan;
    ctx.shadowBlur = 8;
    ctx.stroke();

    // Internal Shimmering Water Texture
    ctx.save();
    ctx.clip(); // Clip within the flood extent
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.lineWidth = 1.2;
    for (let wave = 0; wave < 5; wave++) {
      const waveShift = ((now * 0.04 + wave * 40) % (this.width * 0.5));
      ctx.beginPath();
      ctx.moveTo(this.width * 0.2 + waveShift, 0);
      ctx.lineTo(this.width * 0.05 + waveShift, this.height);
      ctx.stroke();
    }
    ctx.restore();

    ctx.restore();
  }

  drawCloudLayer(now) {
    const ctx = this.ctx;
    const W = this.width;
    const H = this.height;

    ctx.save();
    this.clouds.forEach(cloud => {
      const cx = cloud.x * W;
      const cy = cloud.y * H;
      const grad = ctx.createRadialGradient(cx, cy, cloud.rx * 0.15, cx, cy, cloud.rx);
      grad.addColorStop(0, `rgba(16, 28, 48, ${cloud.opacity})`);
      grad.addColorStop(0.6, `rgba(14, 24, 42, ${cloud.opacity * 0.5})`);
      grad.addColorStop(1, 'transparent');

      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.ellipse(cx, cy, cloud.rx, cloud.ry, -0.1, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.restore();
  }

  drawSentinelSatelliteAndRadar(now) {
    const ctx = this.ctx;
    const W = this.width;
    const H = this.height;

    // Satellite position in screen space
    const satX = this.satellite.x * W;
    const satY = this.satellite.y * H;

    // 1. Radar Pulse Cone & Ground Swath (Passes through clouds!)
    if (this.radarState.active) {
      const progress = this.radarState.progress; // 0 to 1
      ctx.save();

      // Target footprint on the river floodplain
      const targetX = W * (0.35 + (1 - progress) * 0.35);
      const targetY = H * (0.55 + progress * 0.25);
      const beamSpread = 160 + progress * 240;

      // Volumetric Radar Cone Gradient from Satellite to Ground
      const coneGrad = ctx.createRadialGradient(satX, satY, 10, targetX, targetY, H * 0.85);
      coneGrad.addColorStop(0, 'rgba(0, 229, 255, 0.45)');
      coneGrad.addColorStop(0.3, 'rgba(0, 229, 255, 0.18)');
      coneGrad.addColorStop(1, 'rgba(0, 229, 255, 0.01)');

      ctx.fillStyle = coneGrad;
      ctx.beginPath();
      ctx.moveTo(satX, satY);
      ctx.lineTo(targetX - beamSpread, targetY);
      ctx.lineTo(targetX + beamSpread, targetY);
      ctx.closePath();
      ctx.fill();

      // Ground Scan Line ("Analysis in Progress")
      const scanAlpha = Math.sin(progress * Math.PI);
      ctx.strokeStyle = `rgba(0, 229, 255, ${scanAlpha.toFixed(2)})`;
      ctx.lineWidth = 2.5;
      ctx.shadowColor = '#00E5FF';
      ctx.shadowBlur = 12;

      ctx.beginPath();
      ctx.moveTo(targetX - beamSpread, targetY);
      ctx.lineTo(targetX + beamSpread, targetY);
      ctx.stroke();

      // SAR footprint bounding ticks
      ctx.fillStyle = '#00E5FF';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.fillText(`SAR C-BAND SWATH // ${(progress * 100).toFixed(0)}%`, targetX - 70, targetY - 12);

      ctx.restore();
    }

    // 2. Sentinel-1 Satellite Silhouette
    ctx.save();
    ctx.translate(satX, satY);
    ctx.rotate(this.satellite.angle);

    // Subtle Satellite Glow
    ctx.shadowColor = 'rgba(0, 229, 255, 0.4)';
    ctx.shadowBlur = 15;

    // A. Main satellite central bus (chassis)
    ctx.fillStyle = '#A0B4C8';
    ctx.fillRect(-8, -5, 16, 10);
    ctx.fillStyle = '#101B2B';
    ctx.fillRect(-6, -3, 12, 6);

    // B. Large Planar SAR Radar Antenna Boom (10m class Sentinel-1 look angle)
    ctx.fillStyle = '#00E5FF';
    ctx.fillRect(-18, 5, 36, 4);
    // Antenna phased array pattern
    ctx.fillStyle = '#02040A';
    for (let ap = -15; ap <= 15; ap += 5) {
      ctx.fillRect(ap, 6, 2, 2);
    }

    // C. Solar Array Wings (Extended left and right)
    ctx.fillStyle = '#162C4A';
    ctx.strokeStyle = '#2D9CFF';
    ctx.lineWidth = 1;

    // Left solar wing
    ctx.fillRect(-52, -4, 40, 8);
    ctx.strokeRect(-52, -4, 40, 8);
    // Left solar panel grid lines
    for (let gx = -44; gx < -14; gx += 8) {
      ctx.beginPath();
      ctx.moveTo(gx, -4);
      ctx.lineTo(gx, 4);
      ctx.stroke();
    }

    // Right solar wing
    ctx.fillRect(12, -4, 40, 8);
    ctx.strokeRect(12, -4, 40, 8);
    for (let gx = 20; gx < 50; gx += 8) {
      ctx.beginPath();
      ctx.moveTo(gx, -4);
      ctx.lineTo(gx, 4);
      ctx.stroke();
    }

    // Beacon blinker LED on satellite bus
    const beaconAlpha = 0.5 + 0.5 * Math.sin(now * 0.008);
    ctx.fillStyle = `rgba(0, 229, 255, ${beaconAlpha})`;
    ctx.beginPath();
    ctx.arc(0, -6, 2, 0, Math.PI * 2);
    ctx.fill();

    ctx.restore();
  }

  drawHUD(now) {
    const ctx = this.ctx;
    const W = this.width;
    const H = this.height;

    ctx.save();
    ctx.fillStyle = 'rgba(100, 180, 255, 0.09)';
    ctx.strokeStyle = 'rgba(100, 180, 255, 0.08)';
    ctx.lineWidth = 1;
    ctx.font = '10px "JetBrains Mono", monospace';

    // 1. Tactical Corner Brackets
    const bLen = 24;
    const pad = 28;

    // Top-left
    ctx.beginPath();
    ctx.moveTo(pad, pad + bLen); ctx.lineTo(pad, pad); ctx.lineTo(pad + bLen, pad);
    ctx.stroke();

    // Top-right
    ctx.beginPath();
    ctx.moveTo(W - pad - bLen, pad); ctx.lineTo(W - pad, pad); ctx.lineTo(W - pad, pad + bLen);
    ctx.stroke();

    // Bottom-left
    ctx.beginPath();
    ctx.moveTo(pad, H - pad - bLen); ctx.lineTo(pad, H - pad); ctx.lineTo(pad + bLen, H - pad);
    ctx.stroke();

    // Bottom-right
    ctx.beginPath();
    ctx.moveTo(W - pad - bLen, H - pad); ctx.lineTo(W - pad, H - pad); ctx.lineTo(W - pad, H - pad - bLen);
    ctx.stroke();

    // 2. Telemetry and Orbit Coordinates (kept subtle <10% opacity)
    ctx.fillText('SENTINEL-1A SAR // C-BAND 5.405 GHz // COPERNICUS EMS RAPID MAPPING', pad + 10, pad + 14);
    ctx.fillText(`ORBIT: 693 km // INCIDENCE: 38.4° // LAT: 44.49°N  LON: 11.34°E`, pad + 10, pad + 30);

    // Bottom telemetry
    ctx.fillText('GROUND RESOLUTION: 10m // POL: VV+VH // AI-SEGMENTED FLOOD EXTENT', pad + 10, H - pad - 8);

    ctx.restore();
  }

  drawBeforeAfterDivider() {
    const ctx = this.ctx;
    const W = this.width;
    const H = this.height;

    const divX = W * this.dividerState.xRatio;

    ctx.save();
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.25)';
    ctx.lineWidth = 1.2;
    ctx.setLineDash([6, 6]);

    ctx.beginPath();
    ctx.moveTo(divX, 60);
    ctx.lineTo(divX, H - 60);
    ctx.stroke();
    ctx.setLineDash([]);

    // Divider handle slider widget
    ctx.fillStyle = 'rgba(10, 20, 36, 0.8)';
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.5)';
    ctx.beginPath();
    ctx.arc(divX, H * 0.5, 14, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    // Left/Right arrows
    ctx.fillStyle = '#00E5FF';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText('◄ ►', divX, H * 0.5 + 4);

    // Labels
    ctx.fillStyle = 'rgba(140, 180, 220, 0.35)';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText('PRE-EVENT OPTICAL', divX - 22, H * 0.5 + 3);
    ctx.textAlign = 'left';
    ctx.fillText('SAR FLOOD MASK', divX + 22, H * 0.5 + 3);

    ctx.restore();
  }

  drawContrastVignette() {
    const ctx = this.ctx;
    const W = this.width;
    const H = this.height;

    ctx.save();
    // Smooth radial darkening centered at (W * 0.28, H * 0.48) where hero headline sits
    const heroVignette = ctx.createRadialGradient(
      W * 0.28, H * 0.48, 80,
      W * 0.35, H * 0.50, Math.max(W * 0.65, 500)
    );
    heroVignette.addColorStop(0, 'rgba(2, 4, 10, 0.78)');
    heroVignette.addColorStop(0.55, 'rgba(2, 4, 10, 0.50)');
    heroVignette.addColorStop(1, 'rgba(2, 4, 10, 0.0)');

    ctx.fillStyle = heroVignette;
    ctx.fillRect(0, 0, W, H);

    // Left edge subtle linear shade
    const leftEdge = ctx.createLinearGradient(0, 0, W * 0.35, 0);
    leftEdge.addColorStop(0, 'rgba(2, 4, 10, 0.65)');
    leftEdge.addColorStop(1, 'transparent');
    ctx.fillStyle = leftEdge;
    ctx.fillRect(0, 0, W * 0.35, H);

    ctx.restore();
  }

  renderStaticFrame() {
    // Used when prefers-reduced-motion is enabled or when paused
    this.currentFloodLevel = 0.65; // Fixed flood extent
    this.draw(12000);
  }

  destroy() {
    this.pause();
    window.removeEventListener('resize', this.resizeHandler);
  }
}

// Global export for vanilla scripts or modules
if (typeof module !== 'undefined' && module.exports) {
  module.exports = FloodRadarBackground;
}
