import React, { useEffect, useRef } from 'react';

/**
 * FloodRadarBackground React Component
 * 
 * Drop-in full-screen or container animated background for:
 * "Orbital Intelligence for Ground-Level Survival"
 * 
 * Props:
 * - isFixed: boolean (default: true) -> position fixed at z-index -1
 * - showHUD: boolean (default: true) -> tactical HUD markings & telemetry
 * - showDivider: boolean (default: true) -> gliding Before/After comparison divider
 * - floodLoopDuration: number (default: 22000) -> ms for flood rise/recede cycle
 * - radarPulseInterval: number (default: 7500) -> ms between satellite radar sweeps
 * - className: string -> optional CSS class
 * - style: React.CSSProperties -> optional inline style overrides
 */
export default function FloodRadarBackground({
  isFixed = true,
  showHUD = true,
  showDivider = true,
  floodLoopDuration = 22000,
  radarPulseInterval = 7500,
  className = '',
  style = {}
}) {
  const canvasRef = useRef(null);
  const instanceRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    let animationFrameId = null;
    let isPaused = false;
    let prefersReducedMotion = false;
    const startTime = performance.now();
    let lastRadarTime = performance.now();

    const palette = {
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
    };

    let width = 0;
    let height = 0;
    let currentFloodLevel = 0;

    // Simulation entities
    let stars = [];
    let riverPath = [];
    let tributaries = [];
    let contours = [];
    let buildings = [];
    let roads = [];
    let rescueNodes = [];
    let clouds = [];

    const satellite = {
      x: 0.78,
      y: 0.12,
      speedX: 0.0015,
      angle: -0.25
    };

    const radarState = {
      active: false,
      progress: 0,
      duration: 2800,
      startTime: 0
    };

    const dividerState = {
      xRatio: 0.58,
      visible: showDivider
    };

    // Initialize stars & clouds
    for (let i = 0; i < 65; i++) {
      const depth = (i % 3) + 1;
      stars.push({
        x: Math.random(),
        y: Math.random() * 0.7,
        radius: depth === 1 ? 0.6 : (depth === 2 ? 1.0 : 1.4),
        baseAlpha: depth === 1 ? 0.25 : (depth === 2 ? 0.45 : 0.7),
        twinkleSpeed: 0.0015 + Math.random() * 0.003,
        twinklePhase: Math.random() * Math.PI * 2
      });
    }

    clouds = [
      { x: 0.15, y: 0.35, rx: 220, ry: 75, speed: 0.004, opacity: 0.12 },
      { x: 0.55, y: 0.28, rx: 340, ry: 110, speed: 0.006, opacity: 0.16 },
      { x: 0.82, y: 0.45, rx: 280, ry: 90, speed: 0.005, opacity: 0.14 }
    ];

    function generateMapGeography() {
      const W = width;
      const H = height;

      // River Spline
      riverPath = [
        { x: W * 0.95, y: H * 0.25, width: 14 },
        { x: W * 0.82, y: H * 0.36, width: 22 },
        { x: W * 0.72, y: H * 0.48, width: 28 },
        { x: W * 0.58, y: H * 0.58, width: 34 },
        { x: W * 0.46, y: H * 0.68, width: 44 },
        { x: W * 0.32, y: H * 0.78, width: 56 },
        { x: W * 0.18, y: H * 0.88, width: 68 },
        { x: W * 0.05, y: H * 0.98, width: 85 }
      ];

      // Tributaries
      tributaries = [
        [{ x: W * 0.92, y: H * 0.12 }, { x: W * 0.86, y: H * 0.24 }, { x: W * 0.82, y: H * 0.36 }],
        [{ x: W * 0.94, y: H * 0.55 }, { x: W * 0.80, y: H * 0.56 }, { x: W * 0.72, y: H * 0.48 }],
        [{ x: W * 0.70, y: H * 0.82 }, { x: W * 0.56, y: H * 0.75 }, { x: W * 0.46, y: H * 0.68 }]
      ];

      // Contours
      contours = [];
      const contourCenters = [
        { cx: W * 0.88, cy: H * 0.30, maxR: Math.min(W, H) * 0.35 },
        { cx: W * 0.65, cy: H * 0.75, maxR: Math.min(W, H) * 0.38 },
        { cx: W * 0.25, cy: H * 0.45, maxR: Math.min(W, H) * 0.30 }
      ];
      contourCenters.forEach(c => {
        for (let r = 50; r < c.maxR; r += 45) {
          const points = [];
          for (let s = 0; s < 14; s++) {
            const theta = (s / 14) * Math.PI * 2;
            const wobble = 1 + 0.12 * Math.sin(theta * 3 + r);
            points.push({
              x: c.cx + Math.cos(theta) * r * wobble,
              y: c.cy + Math.sin(theta) * (r * 0.65) * wobble
            });
          }
          contours.push(points);
        }
      });

      // Roads
      roads = [
        {
          points: [
            { x: W * 0.05, y: H * 0.62 },
            { x: W * 0.35, y: H * 0.64 },
            { x: W * 0.58, y: H * 0.58 },
            { x: W * 0.82, y: H * 0.60 },
            { x: W * 0.98, y: H * 0.62 }
          ],
          type: 'primary',
          submergedThreshold: 0.48
        },
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
        }
      ];

      // Buildings
      buildings = [];
      const clusters = [
        { cx: W * 0.52, cy: H * 0.62, spread: 95, count: 20, floodRisk: 0.28 },
        { cx: W * 0.36, cy: H * 0.74, spread: 80, count: 16, floodRisk: 0.42 },
        { cx: W * 0.75, cy: H * 0.42, spread: 70, count: 12, floodRisk: 0.62 },
        { cx: W * 0.85, cy: H * 0.28, spread: 50, count: 8, floodRisk: 0.95 }
      ];
      clusters.forEach(cluster => {
        for (let i = 0; i < cluster.count; i++) {
          const angle = Math.random() * Math.PI * 2;
          const dist = Math.pow(Math.random(), 0.7) * cluster.spread;
          buildings.push({
            x: cluster.cx + Math.cos(angle) * dist,
            y: cluster.cy + Math.sin(angle) * (dist * 0.7),
            w: 7 + Math.random() * 8,
            h: 6 + Math.random() * 7,
            rot: Math.sin(angle) * 0.25,
            floodThreshold: cluster.floodRisk + (Math.random() * 0.2 - 0.1),
            state: 'dry',
            damagePulse: 0
          });
        }
      });

      // Rescue Nodes
      rescueNodes = [
        { x: W * 0.86, y: H * 0.26, label: 'EVAC-A1' },
        { x: W * 0.78, y: H * 0.68, label: 'BOAT-02' },
        { x: W * 0.28, y: H * 0.52, label: 'MEDEVAC' }
      ];
    }

    function resize() {
      const parent = canvas.parentElement;
      width = isFixed ? window.innerWidth : parent.clientWidth;
      height = isFixed ? window.innerHeight : parent.clientHeight;

      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      generateMapGeography();
      if (prefersReducedMotion) renderStatic();
    }

    function triggerSweep() {
      radarState.active = true;
      radarState.startTime = performance.now();
      radarState.progress = 0;
    }

    function renderStatic() {
      currentFloodLevel = 0.65;
      draw(12000);
    }

    function update(now) {
      const elapsed = now - startTime;
      const loopPhase = (elapsed % floodLoopDuration) / floodLoopDuration;
      currentFloodLevel = 0.5 - 0.5 * Math.cos(loopPhase * Math.PI * 2);

      buildings.forEach(b => {
        const isFlooded = currentFloodLevel >= b.floodThreshold;
        if (isFlooded && b.state === 'dry') {
          b.state = (currentFloodLevel > b.floodThreshold + 0.18) ? 'cutoff' : 'damaged';
          b.damagePulse = 1.0;
        } else if (!isFlooded && b.state !== 'dry') {
          b.state = 'dry';
          b.damagePulse = 0;
        }
        if (b.damagePulse > 0) b.damagePulse = Math.max(0, b.damagePulse - 0.02);
      });

      satellite.x += satellite.speedX * 0.05;
      if (satellite.x > 1.15) satellite.x = -0.1;

      if (!radarState.active && (now - lastRadarTime > radarPulseInterval)) {
        triggerSweep();
        lastRadarTime = now;
      }

      if (radarState.active) {
        radarState.progress = Math.min(1.0, (now - radarState.startTime) / radarState.duration);
        if (radarState.progress >= 1.0) radarState.active = false;
      }

      clouds.forEach(c => {
        c.x += c.speed * 0.02;
        if (c.x > 1.2) c.x = -0.2;
      });

      if (dividerState.visible) {
        dividerState.xRatio += Math.sin(now * 0.0004) * 0.0004;
      }
    }

    function draw(now) {
      const W = width;
      const H = height;
      ctx.clearRect(0, 0, W, H);

      // 1. Base gradient
      const bgGrad = ctx.createLinearGradient(0, 0, W * 0.8, H);
      bgGrad.addColorStop(0, palette.bgBase);
      bgGrad.addColorStop(0.55, '#040916');
      bgGrad.addColorStop(1, palette.bgDarkBlue);
      ctx.fillStyle = bgGrad;
      ctx.fillRect(0, 0, W, H);

      // Stars
      stars.forEach(s => {
        const tw = Math.sin(now * s.twinkleSpeed + s.twinklePhase);
        ctx.fillStyle = `rgba(215, 235, 255, ${Math.max(0.08, s.baseAlpha + tw * 0.25).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(s.x * W, s.y * H, s.radius, 0, Math.PI * 2);
        ctx.fill();
      });

      // 2. Contours
      ctx.save();
      ctx.strokeStyle = palette.contour;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 6]);
      contours.forEach(pts => {
        ctx.beginPath();
        pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
        ctx.closePath();
        ctx.stroke();
      });
      ctx.restore();

      // Rivers
      ctx.strokeStyle = '#0E1C30';
      ctx.lineWidth = 4;
      tributaries.forEach(ribbon => {
        ctx.beginPath();
        ribbon.forEach((pt, i) => (i === 0 ? ctx.moveTo(pt.x, pt.y) : ctx.lineTo(pt.x, pt.y)));
        ctx.stroke();
      });

      for (let i = 0; i < riverPath.length - 1; i++) {
        const p1 = riverPath[i];
        const p2 = riverPath[i + 1];
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.strokeStyle = '#0d1d33';
        ctx.lineWidth = (p1.width + p2.width) * 0.5;
        ctx.stroke();
      }

      // Roads
      roads.forEach(r => {
        const cut = currentFloodLevel >= r.submergedThreshold;
        ctx.save();
        ctx.lineWidth = r.type === 'primary' ? 2.5 : 1.2;
        ctx.strokeStyle = cut ? palette.dangerRed : palette.roadNeutral;
        if (cut) { ctx.shadowColor = palette.dangerRed; ctx.shadowBlur = 6; }
        ctx.beginPath();
        r.points.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
        ctx.stroke();
        ctx.restore();
      });

      // 3. Flood Inundation Polygon
      if (currentFloodLevel > 0.05) {
        ctx.save();
        const leftBank = [];
        const rightBank = [];
        for (let i = 0; i < riverPath.length; i++) {
          const pt = riverPath[i];
          const next = riverPath[Math.min(riverPath.length - 1, i + 1)];
          const prev = riverPath[Math.max(0, i - 1)];
          const dx = next.x - prev.x;
          const dy = next.y - prev.y;
          const len = Math.hypot(dx, dy) || 1;
          const nx = -dy / len;
          const ny = dx / len;
          const spread = pt.width * 0.8 + (pt.width * 3.8) * currentFloodLevel;
          leftBank.push({ x: pt.x + nx * spread, y: pt.y + ny * spread });
          rightBank.push({ x: pt.x - nx * spread * 1.15, y: pt.y - ny * spread * 1.15 });
        }

        ctx.beginPath();
        leftBank.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
        for (let i = rightBank.length - 1; i >= 0; i--) ctx.lineTo(rightBank[i].x, rightBank[i].y);
        ctx.closePath();
        ctx.fillStyle = `rgba(45, 156, 255, ${(0.22 + currentFloodLevel * 0.12).toFixed(2)})`;
        ctx.fill();
        ctx.strokeStyle = 'rgba(0, 229, 255, 0.85)';
        ctx.lineWidth = 1.8;
        ctx.shadowColor = palette.radarCyan;
        ctx.shadowBlur = 8;
        ctx.stroke();
        ctx.restore();
      }

      // 4. Buildings
      buildings.forEach(b => {
        ctx.save();
        ctx.translate(b.x, b.y);
        ctx.rotate(b.rot);
        if (b.state === 'dry') {
          ctx.fillStyle = palette.neutralBuilding;
          ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
        } else if (b.state === 'damaged') {
          ctx.fillStyle = palette.damageAmber;
          ctx.shadowColor = palette.damageAmber;
          ctx.shadowBlur = 8;
          ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
        } else if (b.state === 'cutoff') {
          ctx.fillStyle = palette.dangerRed;
          ctx.shadowColor = palette.dangerRed;
          ctx.shadowBlur = 10;
          ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
        }
        if (b.damagePulse > 0) {
          ctx.strokeStyle = b.state === 'cutoff' ? palette.dangerRed : palette.damageAmber;
          ctx.beginPath();
          ctx.arc(0, 0, Math.max(b.w, b.h) * (1 + (1 - b.damagePulse) * 1.8), 0, Math.PI * 2);
          ctx.stroke();
        }
        ctx.restore();
      });

      // Rescue nodes
      rescueNodes.forEach(node => {
        ctx.save();
        const pulse = (now % 1800) / 1800;
        ctx.strokeStyle = `rgba(61, 255, 154, ${1 - pulse})`;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(node.x, node.y, 5 + pulse * 18, 0, Math.PI * 2);
        ctx.stroke();

        ctx.fillStyle = palette.safeGreen;
        ctx.shadowColor = palette.safeGreen;
        ctx.shadowBlur = 10;
        ctx.beginPath();
        ctx.arc(node.x, node.y, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      });

      // 5. Clouds
      ctx.save();
      clouds.forEach(c => {
        const cx = c.x * W;
        const cy = c.y * H;
        const g = ctx.createRadialGradient(cx, cy, c.rx * 0.15, cx, cy, c.rx);
        g.addColorStop(0, `rgba(16, 28, 48, ${c.opacity})`);
        g.addColorStop(1, 'transparent');
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.ellipse(cx, cy, c.rx, c.ry, -0.1, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();

      // 6. Satellite & Radar
      const satX = satellite.x * W;
      const satY = satellite.y * H;

      if (radarState.active) {
        const p = radarState.progress;
        ctx.save();
        const targetX = W * (0.35 + (1 - p) * 0.35);
        const targetY = H * (0.55 + p * 0.25);
        const spread = 160 + p * 240;

        const cone = ctx.createRadialGradient(satX, satY, 10, targetX, targetY, H * 0.85);
        cone.addColorStop(0, 'rgba(0, 229, 255, 0.45)');
        cone.addColorStop(0.3, 'rgba(0, 229, 255, 0.18)');
        cone.addColorStop(1, 'rgba(0, 229, 255, 0.01)');

        ctx.fillStyle = cone;
        ctx.beginPath();
        ctx.moveTo(satX, satY);
        ctx.lineTo(targetX - spread, targetY);
        ctx.lineTo(targetX + spread, targetY);
        ctx.closePath();
        ctx.fill();

        ctx.strokeStyle = `rgba(0, 229, 255, ${Math.sin(p * Math.PI).toFixed(2)})`;
        ctx.lineWidth = 2.5;
        ctx.shadowColor = '#00E5FF';
        ctx.shadowBlur = 12;
        ctx.beginPath();
        ctx.moveTo(targetX - spread, targetY);
        ctx.lineTo(targetX + spread, targetY);
        ctx.stroke();
        ctx.restore();
      }

      // Satellite bus & antenna
      ctx.save();
      ctx.translate(satX, satY);
      ctx.rotate(satellite.angle);
      ctx.fillStyle = '#A0B4C8';
      ctx.fillRect(-8, -5, 16, 10);
      ctx.fillStyle = '#00E5FF'; // SAR Antenna
      ctx.fillRect(-18, 5, 36, 4);
      ctx.fillStyle = '#162C4A'; // Solar panels
      ctx.strokeStyle = '#2D9CFF';
      ctx.fillRect(-52, -4, 40, 8);
      ctx.strokeRect(-52, -4, 40, 8);
      ctx.fillRect(12, -4, 40, 8);
      ctx.strokeRect(12, -4, 40, 8);
      ctx.restore();

      // 7. HUD
      if (showHUD) {
        ctx.save();
        ctx.strokeStyle = 'rgba(100, 180, 255, 0.08)';
        ctx.fillStyle = 'rgba(100, 180, 255, 0.09)';
        ctx.font = '10px monospace';
        ctx.strokeRect(28, 28, W - 56, H - 56);
        ctx.fillText('SENTINEL-1A SAR // C-BAND 5.405 GHz // COPERNICUS EMS', 40, 44);
        ctx.restore();
      }

      // 8. Text Legibility Vignette
      ctx.save();
      const heroVignette = ctx.createRadialGradient(
        W * 0.28, H * 0.48, 80,
        W * 0.35, H * 0.50, Math.max(W * 0.65, 500)
      );
      heroVignette.addColorStop(0, 'rgba(2, 4, 10, 0.78)');
      heroVignette.addColorStop(0.55, 'rgba(2, 4, 10, 0.50)');
      heroVignette.addColorStop(1, 'rgba(2, 4, 10, 0.0)');
      ctx.fillStyle = heroVignette;
      ctx.fillRect(0, 0, W, H);
      ctx.restore();
    }

    // Media query
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
      prefersReducedMotion = mq.matches;
      mq.addEventListener('change', (e) => {
        prefersReducedMotion = e.matches;
        if (prefersReducedMotion) renderStatic();
      });
    }

    resize();
    window.addEventListener('resize', resize);

    if (!prefersReducedMotion) {
      const loop = (now) => {
        if (!isPaused) {
          update(now);
          draw(now);
        }
        animationFrameId = requestAnimationFrame(loop);
      };
      animationFrameId = requestAnimationFrame(loop);
    } else {
      renderStatic();
    }

    // Expose instance helper methods via ref
    instanceRef.current = {
      triggerRadarSweep: triggerSweep,
      pause: () => { isPaused = true; },
      resume: () => { isPaused = false; }
    };

    return () => {
      if (animationFrameId) cancelAnimationFrame(animationFrameId);
      window.removeEventListener('resize', resize);
    };
  }, [showHUD, showDivider, floodLoopDuration, radarPulseInterval, isFixed]);

  const defaultStyle = isFixed
    ? {
        position: 'fixed',
        top: 0,
        left: 0,
        width: '100vw',
        height: '100vh',
        zIndex: -1,
        pointerEvents: 'none'
      }
    : {
        position: 'absolute',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        zIndex: -1,
        pointerEvents: 'none'
      };

  return (
    <canvas
      ref={canvasRef}
      className={`orbital-flood-radar-bg ${className}`}
      style={{ ...defaultStyle, ...style }}
    />
  );
}
