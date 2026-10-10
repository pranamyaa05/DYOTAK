# Orbital Intelligence for Ground-Level Survival
### Track B: Mapping Flood Damage from Space (Sentinel-1 SAR + Copernicus EMS)

This repository contains both implementations of the hero background:
1. **Version 1 (AI Image Prompts)**: Formatted, fine-tuned prompts for Midjourney v6, DALL-E 3, and Flux with photoreal satellite SAR radar aesthetics.
2. **Version 2 (Code-Built Canvas Background)**: Full-screen, GPU-accelerated 60fps animated canvas background ready for plain HTML/JS or React/Next.js.

---

## 🚀 Quick Start

### 1. View Interactive Demo (HTML/JS)
Simply open [`index.html`](./index.html) in any modern browser:
```bash
# Or start a local static server:
npx serve .
# or
python -m http.server 3000
```

### 2. Using in Vanilla HTML/CSS/JS
Include `FloodRadarBackground.js` and attach it to a background `<canvas>`:
```html
<canvas id="flood-radar-bg" style="position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -1; pointer-events: none;"></canvas>

<script src="FloodRadarBackground.js"></script>
<script>
  const bg = new FloodRadarBackground('flood-radar-bg', {
    floodLoopDuration: 22000,   // 22-second natural rise & fall cycle
    radarPulseInterval: 7500,  // Periodic SAR microwave sweep every 7.5s
    showHUD: true,             // Subdued GIS telemetry (<12% opacity)
    showDivider: true          // Gliding Before/After comparison divider
  });

  // Optional interactive triggers
  bg.triggerRadarSweep();
  bg.toggleDivider();
</script>
```

### 3. Using in React / Next.js / Vite
Drop in `FloodRadarBackground.jsx`:
```jsx
import FloodRadarBackground from './FloodRadarBackground';

export default function HeroSection() {
  return (
    <div className="relative min-h-screen text-white">
      {/* Background layer at z-index: -1 */}
      <FloodRadarBackground isFixed={true} showHUD={true} />

      {/* Your existing hero content */}
      <main className="relative z-10 max-w-5xl mx-auto px-6 py-24">
        <h1 className="text-5xl font-extrabold tracking-tight">
          Orbital Intelligence for Ground-Level Survival
        </h1>
        <p className="mt-6 text-xl text-slate-400 max-w-2xl">
          Mapping flood damage through cloud cover using Sentinel-1 C-band SAR radar.
        </p>
      </main>
    </div>
  );
}
```

---

## 🛰️ Visual Architecture & Layer Stack

| Layer | Component | Implementation |
|---|---|---|
| **Layer 1** | Base Gradient & Stars | `#02040A` to `#0A1224` deep night gradient with 3-depth parallax twinkling starfield. |
| **Layer 2** | River Basin & Terrain | Vector top-down topography: winding river channel, tributaries, contour isolines, and road network. |
| **Layer 3** | Flood Rise & Water | Expanding/receding inundation polygon (`#2D9CFF` at 25-35% opacity) with bright GIS detected vector edge (`#00E5FF`) and water shimmer. |
| **Layer 4** | Automated Damage Triage | Submerged structures shift to amber (`#FFB347`) or red (`#FF4D4D`) with alert expansion rings; cut-off roads turn red; green rescue nodes (`#3DFF9A`) pulse on high ground. |
| **Layer 5** | Storm Clouds | Drifting semi-transparent moisture layer. |
| **Layer 6** | Sentinel-1 SAR Satellite | Realistic satellite bus with solar panels and planar C-band antenna boom emitting cyan radar cones (`#00E5FF`) that pierce through clouds every 7.5s. |
| **Layer 7** | Subdued Tactical HUD | Corner brackets, orbital parameters, coordinate ticks, and a gliding Before/After comparison divider (<12% opacity). |
| **Layer 8** | Contrast Protection | Left-center radial vignette ensuring **4.5:1 WCAG contrast ratio** for text and buttons. |

---

## 🎨 Version 1: Refined AI Image Prompts

### Midjourney v6 Prompt
```text
Ultra-wide cinematic website hero background, 16:9, dark sci-fi tactical GIS aesthetic. View from low Earth orbit looking down at a swollen river basin during a major flood disaster at night under thin translucent storm clouds. A European Space Agency Sentinel-1 style radar satellite with long solar panel wings and flat rectangular SAR antenna boom in the upper right, emitting thin translucent glowing cyan radar microwave pulses (5.405 GHz) that pass directly through the storm clouds and sweep the landscape below. On the Earth below, inundated areas glow with an electric-blue AI-segmented flood-extent mask (#2D9CFF) with crisp vector edges. Submerged buildings and blocked road segments highlighted with amber (#FFB347) and emergency red (#FF4D4D) outlines. Dry high-ground areas remain dark with faint warm streetlights and isolated safe green beacon nodes (#3DFF9A). Subtle tactical HUD overlay with fine coordinate ticks, elevation contours, and corner reticles. Large clean dark negative space on the left and center for website hero headline typography. Photoreal satellite imagery mixed with radar telemetry, volumetric beam glow, faint starfield, 35mm film grain, 8k resolution --ar 16:9 --style raw --v 6.0
```

### DALL-E 3 / Flux Prompt
```text
A cinematic 16:9 ultra-wide dark hero background depicting satellite flood damage mapping. In the upper-right corner of low Earth orbit, a realistic Sentinel-1 synthetic aperture radar (SAR) satellite with rectangular photovoltaic solar wings and a long planar antenna emits a conical cyan microwave radar beam (#00E5FF) that penetrates through drifting storm clouds to the Earth below. The ground shows an aerial top-down view of a river basin flooded with electric-blue water (#2D9CFF) with sharp GIS detection boundaries. Several impacted structures and severed bridges glow with amber and crimson hazard outlines, while high-ground evacuation points display subtle green rescue pings. The center-left portion of the image is preserved as deep dark navy negative space (#02040A) with very low contrast to allow website text to be read clearly. Dark sci-fi satellite telemetry aesthetic, no typography, no watermarks.
```
