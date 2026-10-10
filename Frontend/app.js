/**
 * app.js
 * Core application logic for Orbital Intelligence:
 * Flood damage mapping, triage table filtering, route inspection, and Copernicus EMS export.
 */

document.addEventListener('DOMContentLoaded', () => {
  // 1. Initialize Hero Background Canvas
  let bgEngine = null;
  if (document.getElementById('flood-radar-bg')) {
    bgEngine = new FloodRadarBackground('flood-radar-bg');
    window.bgEngine = bgEngine;
  }

  // 2. Initialize Interactive Map Studio
  let studio = null;
  if (document.getElementById('studio-canvas')) {
    studio = new MapStudio('studio-canvas');
    window.studio = studio;
  }

  // 3. Connect Layer Toggles in Map Studio
  const layerCheckboxes = document.querySelectorAll('.layer-toggle-item input[type="checkbox"]');
  layerCheckboxes.forEach(cb => {
    cb.addEventListener('change', (e) => {
      const layer = e.target.dataset.layer;
      if (studio) {
        studio.setLayer(layer, e.target.checked);
        showToast(`LAYER UPDATED: ${layer.toUpperCase()} -> ${e.target.checked ? 'ENABLED' : 'DISABLED'}`);
      }
    });
  });

  // 4. Connect Before/After Split Slider
  const splitRange = document.getElementById('split-range');
  if (splitRange) {
    splitRange.addEventListener('input', (e) => {
      const ratio = parseFloat(e.target.value) / 100;
      if (studio) studio.setSplitRatio(ratio);
    });
  }

  // 5. Connect Damage Triage Table Filters & Search
  const filterBtns = document.querySelectorAll('.filter-btn');
  const searchInput = document.getElementById('triage-search');
  const tableRows = document.querySelectorAll('#triage-tbody tr');

  function filterTriage() {
    const activeFilter = document.querySelector('.filter-btn.active')?.dataset.filter || 'all';
    const query = searchInput ? searchInput.value.toLowerCase().trim() : '';

    tableRows.forEach(row => {
      const status = row.dataset.status;
      const text = row.innerText.toLowerCase();

      const matchesFilter = activeFilter === 'all' || status === activeFilter;
      const matchesSearch = query === '' || text.includes(query);

      if (matchesFilter && matchesSearch) {
        row.style.display = '';
      } else {
        row.style.display = 'none';
      }
    });
  }

  filterBtns.forEach(btn => {
    btn.addEventListener('click', (e) => {
      filterBtns.forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      filterTriage();
    });
  });

  if (searchInput) {
    searchInput.addEventListener('input', filterTriage);
  }

  // Table row click -> Focus in Map Studio
  tableRows.forEach(row => {
    row.addEventListener('click', () => {
      const bldId = row.dataset.id;
      if (studio) {
        studio.focusFeature(bldId);
        showToast(`LOCATING FEATURE IN SAR VIEWER: ${bldId}`);
        // Smooth scroll up to map if on mobile/small screen
        const mapSection = document.getElementById('live-map');
        if (mapSection && window.innerWidth < 1000) {
          mapSection.scrollIntoView({ behavior: 'smooth' });
        }
      }
    });
  });

  // Callback when feature clicked directly on canvas
  window.onFeatureSelected = (feat) => {
    showToast(`SELECTED: ${feat.name} (${feat.id})`);
  };

  // 6. Connect Responder Route Buttons
  const routeButtons = document.querySelectorAll('.btn-inspect-route');
  routeButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
      const routeId = e.currentTarget.dataset.route;
      showToast(`CALCULATING EVAC CORRIDOR: ${routeId}`);
      if (studio) {
        const mapSection = document.getElementById('live-map');
        if (mapSection) mapSection.scrollIntoView({ behavior: 'smooth' });
      }
    });
  });

  // 7. Connect Copernicus EMS Export Suite
  const btnExportGeoJSON = document.getElementById('btn-export-geojson');
  if (btnExportGeoJSON) {
    btnExportGeoJSON.addEventListener('click', exportGeoJSON);
  }

  const btnExportCSV = document.getElementById('btn-export-csv');
  if (btnExportCSV) {
    btnExportCSV.addEventListener('click', exportCSV);
  }

  const btnOpenSitrep = document.getElementById('btn-open-sitrep');
  const sitrepModal = document.getElementById('sitrep-modal');
  const sitrepClose = document.getElementById('sitrep-close');

  if (btnOpenSitrep && sitrepModal) {
    btnOpenSitrep.addEventListener('click', () => {
      sitrepModal.classList.add('active');
    });
  }

  if (sitrepClose && sitrepModal) {
    sitrepClose.addEventListener('click', () => {
      sitrepModal.classList.remove('active');
    });
  }

  if (sitrepModal) {
    sitrepModal.addEventListener('click', (e) => {
      if (e.target === sitrepModal) {
        sitrepModal.classList.remove('active');
      }
    });
  }

  // 8. Global Hero Trigger Actions
  window.triggerManualSweep = () => {
    if (bgEngine) bgEngine.triggerRadarSweep();
    showToast('SENTINEL-1 SAR BEAM TRIGGERED (C-BAND 5.405 GHz)');
  };

  window.toggleBeforeAfter = () => {
    if (bgEngine) {
      const isVisible = bgEngine.toggleDivider();
      showToast(`BEFORE/AFTER DIVIDER: ${isVisible ? 'ACTIVE' : 'HIDDEN'}`);
    }
  };
});

/**
 * Toast alert notification
 */
function showToast(message) {
  let toast = document.getElementById('toast-notice');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast-notice';
    toast.className = 'toast-notice';
    document.body.appendChild(toast);
  }

  toast.innerHTML = `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#00E5FF" stroke-width="2.5">
      <circle cx="12" cy="12" r="9"/>
      <path d="M12 8v4M12 16h.01"/>
    </svg>
    <span>${message}</span>
  `;
  toast.classList.add('show');

  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => {
    toast.classList.remove('show');
  }, 3200);
}

/**
 * Real GeoJSON File Generator & Exporter
 */
function exportGeoJSON() {
  const geojson = {
    type: "FeatureCollection",
    metadata: {
      mission: "Sentinel-1A SAR Rapid Flood Mapping",
      program: "Copernicus Emergency Management Service (EMS)",
      event: "Major River Basin Flood Event",
      timestamp: new Date().toISOString(),
      polarization: "VV+VH",
      frequency_ghz: 5.405
    },
    features: [
      {
        type: "Feature",
        properties: {
          id: "FLOOD-ZONE-01",
          classification: "Inundated Water Surface",
          confidence: 0.96,
          area_sq_km: 148.6,
          backscatter_db: -23.4
        },
        geometry: {
          type: "Polygon",
          coordinates: [[
            [11.312, 44.482],
            [11.335, 44.501],
            [11.365, 44.524],
            [11.392, 44.512],
            [11.360, 44.478],
            [11.312, 44.482]
          ]]
        }
      },
      {
        type: "Feature",
        properties: {
          id: "BLD-102",
          name: "Valley Primary School",
          damage_state: "Severely Submerged (Cut-off)",
          water_depth_m: 1.40,
          triage_priority: "P1 Critical"
        },
        geometry: {
          type: "Point",
          coordinates: [11.341, 44.498]
        }
      },
      {
        type: "Feature",
        properties: {
          id: "EVAC-A1",
          name: "High Ridge Heliport & Evacuation Node",
          elevation_m: 320,
          status: "Operational Clear"
        },
        geometry: {
          type: "Point",
          coordinates: [11.385, 44.530]
        }
      }
    ]
  };

  const blob = new Blob([JSON.stringify(geojson, null, 2)], { type: "application/geo+json" });
  downloadBlob(blob, `sentinel1_flood_extent_${Date.now()}.geojson`);
  showToast('DOWNLOADED: sentinel1_flood_extent.geojson');
}

/**
 * Real CSV File Generator & Exporter
 */
function exportCSV() {
  const csvContent = 
`Feature_ID,Name,Type,Damage_Status,Est_Water_Depth,Triage_Priority,Latitude,Longitude
BLD-101,Riverview Medical Clinic,Hospital,Moderate Inundation,0.85m,P2 Urgent,44.4938,11.3387
BLD-102,Valley Primary School,School,Severely Submerged,1.40m,P1 Critical,44.4975,11.3412
BLD-103,North Power Substation,Utility,Moderate Inundation,0.62m,P2 Urgent,44.5020,11.3285
BLD-104,Municipal Water Plant,Water Facility,Severely Submerged,1.75m,P1 Critical,44.4912,11.3456
BLD-105,East Ridge Depot,Logistics,Dry / Safe,0.00m,P4 Normal,44.5150,11.3680
BLD-106,St. Jude Community Hall,Shelter Hub,Dry / Safe,0.00m,P4 Normal,44.5210,11.3740
BLD-107,West Valley Grain Silo,Agriculture,Severely Submerged,1.20m,P2 Urgent,44.4850,11.3190
BLD-108,Fire Station #4,Emergency Hub,Moderate Inundation,0.35m,P3 Monitored,44.5060,11.3520
RD-01,Route 101 North Highway,Transport,Bridge Submerged (Cut-off),1.10m,P1 Impassable,44.4980,11.3400
RD-02,East Ridge Escarpment Way,Transport,Clear & High Ground,0.00m,P0 Evac Corridor,44.5180,11.3710`;

  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  downloadBlob(blob, `infrastructure_damage_triage_${Date.now()}.csv`);
  showToast('DOWNLOADED: infrastructure_damage_triage.csv');
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
