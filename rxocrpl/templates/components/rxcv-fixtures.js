// RxDoc — Extended pipeline fixtures for component showcase
// Drawn from real linker output + ocr_fields.py / pipeline.py data shapes

window.PIPELINE_FIXTURES = {

  user: { initials: "JTS", name: "J. Steinbecker", role: "Pharmacist", rph: true },

  pipelineRun: {
    id: "RUN-20260503-1923",
    compound_id: "C-04827",
    status: "succeeded",
    started_at: "2026-05-03 19:23:11 UTC",
    duration_total: 8.4,
    certified_subset: false,
    stages: [
      { id: "detect", name: "Detect",  icon: "scan-line",        status: "done", duration_s: 3.2, summary: "5 regions · 2 images", detail: "Grounding DINO + SAM 2 · panoptic v2.1 · prompt: vial · iv bag · bottle · syringe · filter" },
      { id: "clean",  name: "Clean",   icon: "filter",           status: "done", duration_s: 0.3, summary: "4 survivors · 1 NMS merge", detail: "1 NMS merge (87 % overlap, img2) · 0 frame-coverage rejects · 0 reflection rejects" },
      { id: "ocr",    name: "OCR",     icon: "text-cursor-input", status: "done", duration_s: 1.8, summary: "4 crops · 1 needs review", detail: "EasyOCR ensemble · 4 rotations × 4 preprocessing variants per crop · barcode decode (pyzbar)" },
      { id: "enrich", name: "Enrich",  icon: "database",         status: "done", duration_s: 0.1, summary: "2 NDC lookups · 1 labeler", detail: "FDA NDC Directory 2026-04 · 312,419 entries · labelers.json fallback" },
      { id: "embed",  name: "Embed",   icon: "cpu",              status: "done", duration_s: 2.1, summary: "4 crops embedded", detail: "DINOv2 · mask-aware canonical crop (canonicalize_crop)" },
      { id: "match",  name: "Match",   icon: "link-2",           status: "done", duration_s: 0.9, summary: "3/3 slots · 1 OOI · 0 forced", detail: "Hungarian assignment · 3-tier resolution · force_assign=False · 0 priority NDC boosts used" },
    ],
    cleanup: {
      nms_merges: [
        { survivor_id: 2, absorbed_ids: [4], overlap_pct: 87, source_image: "img2", class_survivor: "vial", class_absorbed: "vial" }
      ],
      frame_rejects: [],
      reflection_rejects: [],
    }
  },

  // Four detections covering: matched-high, matched-high, matched-low+needs_review, out-of-inventory
  detections: [
    {
      instance_id: 0,
      source_image: "img1",
      class_label: "bag",
      score: 0.94,
      slot_id: 0,
      assignment: { score: 0.97, forced: false, out_of_inventory: false, excess: false },
      fields: {
        ndc:          { value: "0264-7800-10",   conf: 0.96, source: "extract_ndc:segmented" },
        barcode_ndc:  { value: "0264-7800-10",   conf: 0.99, source: "barcode:GS1-128" },
        lot:          { value: "2JD588",         conf: 0.95, source: "extract_lot:labeled" },
        exp:          { value: "07/27",          conf: 0.90, source: "extract_exp:MM/YY" },
        mfg:          { value: "Baxter Healthcare Corporation", conf: 0.90, source: "ndc_db_enrichment:mfg" },
        product:      { value: "Sodium Chloride", conf: 0.91, source: "extract_product:directory" },
        strength:     { value: "0.9 %",          conf: 0.85, source: "extract_strength" },
        vol_ml:       { value: "250",            conf: 0.90, source: "extract_vol_ml" },
        brand:        { value: null,             conf: 0.00, source: null },
        mdv:          { value: "SDV",            conf: 0.90, source: "extract_mdv" },
        instructions: { value: "For intravenous use only", conf: 0.85, source: "extract_instructions:for intravenous use only" },
      },
      enrichment: { source: "ndc_directory", brand: null, generic: "Sodium Chloride 0.9% Injection", manufacturer: "Baxter Healthcare Corporation", dosage_form: "INJECTION", strength_db: "0.9 g/100 mL" }
    },
    {
      instance_id: 1,
      source_image: "img2",
      class_label: "vial",
      score: 0.97,
      slot_id: 1,
      assignment: { score: 0.98, forced: false, out_of_inventory: false, excess: false },
      fields: {
        ndc:          { value: "0009-0224-20",   conf: 0.98, source: "extract_ndc:segmented" },
        barcode_ndc:  { value: "0009-0224-20",   conf: 0.99, source: "barcode:GS1-128" },
        lot:          { value: "308777251000",   conf: 0.96, source: "extract_lot:labeled" },
        exp:          { value: "05/30/2028",     conf: 0.94, source: "extract_exp:MM/DD/YYYY" },
        mfg:          { value: "Pfizer Injectables", conf: 0.93, source: "extract_mfg:directory" },
        product:      { value: "Ceftazidime-Avibactam Injection", conf: 0.96, source: "extract_product:directory" },
        strength:     { value: "2.5 g",         conf: 0.95, source: "extract_strength" },
        vol_ml:       { value: null,            conf: 0.00, source: null },
        brand:        { value: "Avycaz",        conf: 0.95, source: "ndc_db_enrichment:brand" },
        mdv:          { value: "SDV",           conf: 0.90, source: "extract_mdv" },
        instructions: { value: "For IV use only | Rx only", conf: 0.85, source: "extract_instructions:for iv use only" },
      },
      enrichment: { source: "ndc_directory", brand: "Avycaz", generic: "Ceftazidime-Avibactam Injection", manufacturer: "Pfizer Inc.", dosage_form: "INJECTION, POWDER, FOR SOLUTION", strength_db: "2 g/0.5 g per vial" }
    },
    {
      instance_id: 2,
      source_image: "img2",
      class_label: "vial",
      score: 0.88,
      slot_id: 2,
      assignment: { score: 0.73, forced: false, out_of_inventory: false, excess: false },
      fields: {
        ndc:          { value: null,     conf: 0.00, source: null },
        barcode_ndc:  { value: null,     conf: 0.00, source: null },
        lot:          { value: "HK3092", conf: 0.88, source: "extract_lot:labeled" },
        exp:          { value: "12/26",  conf: 0.86, source: "extract_exp:MM/YY" },
        mfg:          { value: "Pfizer Injectables", conf: 0.35, source: "extract_mfg:directory" }, // LOW — triggers review
        product:      { value: "Sterile Water for Injection, USP", conf: 0.92, source: "extract_product:directory" },
        strength:     { value: null,     conf: 0.00, source: null },
        vol_ml:       { value: "20",     conf: 0.89, source: "extract_vol_ml" },
        brand:        { value: null,     conf: 0.00, source: null },
        mdv:          { value: "SDV",    conf: 0.90, source: "extract_mdv" },
        instructions: { value: null,     conf: 0.00, source: null },
      },
      enrichment: null
    },
    {
      instance_id: 3,
      source_image: "img2",
      class_label: "vial",
      score: 0.61,
      slot_id: null,
      assignment: { score: 0.22, forced: false, out_of_inventory: true, excess: false },
      fields: {
        ndc:          { value: null,        conf: 0.00, source: null },
        barcode_ndc:  { value: null,        conf: 0.00, source: null },
        lot:          { value: "FK240901",  conf: 0.43, source: "extract_lot:bare_alnum" }, // LOW
        exp:          { value: null,        conf: 0.00, source: null },
        mfg:          { value: null,        conf: 0.00, source: null },
        product:      { value: null,        conf: 0.00, source: null },
        strength:     { value: "5000 units", conf: 0.52, source: "extract_strength" },
        vol_ml:       { value: "10",        conf: 0.65, source: "extract_vol_ml" },
        brand:        { value: null,        conf: 0.00, source: null },
        mdv:          { value: "MDV",       conf: 0.90, source: "extract_mdv" },
        instructions: { value: null,        conf: 0.00, source: null },
      },
      enrichment: null
    },
  ],
};
