// Pipeline Components — pipeline.py display layer
// PipelineRunTimeline · DetectionSummaryCard · CleanupAuditPanel · NDCEnrichmentPanel

// ── PipelineRunTimeline ─────────────────────────────────────────
const PipelineRunTimeline = ({ run }) => {
  const [expanded, setExpanded] = React.useState(null);
  const stages = run?.stages || [];
  const total  = stages.reduce((s, st) => s + (st.duration_s || 0), 0);

  const runStatus = {
    succeeded: { fg: 'var(--success)', bg: 'var(--success-bg)', dot: true },
    running:   { fg: 'var(--info)',    bg: 'var(--info-bg)',    dot: true },
    failed:    { fg: 'var(--danger)',  bg: 'var(--danger-bg)',  dot: true },
  }[run?.status] || { fg: 'var(--pending)', bg: 'var(--pending-bg)', dot: true };

  const stageColor = (s) => ({
    done:    { fg: 'var(--success)', bg: 'var(--success-bg)',  symbol: '✓' },
    running: { fg: 'var(--info)',    bg: 'var(--info-bg)',     symbol: '…' },
    error:   { fg: 'var(--danger)',  bg: 'var(--danger-bg)',   symbol: '!' },
    pending: { fg: 'var(--pending)', bg: 'var(--pending-bg)', symbol: '–' },
  }[s] || { fg: 'var(--pending)', bg: 'var(--pending-bg)', symbol: '–' });

  return (
    <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-md)', overflow: 'hidden', boxShadow: 'var(--shadow-1)' }}>
      {/* Header */}
      <div style={{ padding: '10px 14px', background: 'var(--bg-app)', borderBottom: '1px solid var(--border-default)', display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className="h4">Pipeline run</span>
        <span className="mono-sm" style={{ color: 'var(--fg-tertiary)' }}>{run?.id}</span>
        <div style={{ flex: 1 }} />
        {run?.certified_subset && (
          <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 'var(--radius-sm)', background: 'var(--warning-bg)', color: 'var(--warning)', letterSpacing: '0.05em' }}>
            CERTIFIED SUBSET
          </span>
        )}
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '2px 8px', borderRadius: 'var(--radius-sm)', background: runStatus.bg, color: runStatus.fg, fontSize: 10, fontWeight: 700, letterSpacing: '0.05em' }}>
          <span style={{ width: 5, height: 5, borderRadius: 99, background: 'currentColor', flexShrink: 0 }} />
          {(run?.status || 'pending').toUpperCase()}
        </span>
        <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{total.toFixed(1)} s</span>
      </div>

      {/* Stage rows */}
      {stages.map((stage, i) => {
        const sc  = stageColor(stage.status);
        const isX = expanded === stage.id;
        return (
          <div key={stage.id}>
            <button onClick={() => setExpanded(isX ? null : stage.id)} style={{ width: '100%', textAlign: 'left', border: 'none', padding: '9px 14px', background: isX ? 'var(--bg-row-hover)' : 'transparent', borderBottom: (i < stages.length - 1 || isX) ? '1px solid var(--border-subtle)' : 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10 }}>
              {/* step circle */}
              <div style={{ width: 22, height: 22, borderRadius: 99, background: sc.bg, border: `1.5px solid ${sc.fg}`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, fontWeight: 700, color: sc.fg, flexShrink: 0 }}>
                {sc.symbol}
              </div>
              {/* icon */}
              <i data-lucide={stage.icon} style={{ width: 14, height: 14, strokeWidth: 1.5, color: sc.fg, flexShrink: 0 }} />
              {/* name */}
              <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--fg-primary)', minWidth: 72 }}>{stage.name}</span>
              {/* summary */}
              <span style={{ fontSize: 12, color: 'var(--fg-tertiary)', flex: 1 }}>{stage.summary}</span>
              {/* timing bar */}
              <div style={{ width: 60, height: 4, background: 'var(--border-subtle)', borderRadius: 2, overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${(stage.duration_s / total) * 100}%`, background: sc.fg, borderRadius: 2 }} />
              </div>
              <span className="mono-sm" style={{ color: 'var(--fg-muted)', minWidth: 38, textAlign: 'right' }}>{stage.duration_s.toFixed(1)} s</span>
              <i data-lucide={isX ? 'chevron-up' : 'chevron-down'} style={{ width: 13, height: 13, strokeWidth: 1.5, color: 'var(--fg-muted)', flexShrink: 0 }} />
            </button>
            {isX && (
              <div style={{ padding: '8px 14px 10px 60px', background: 'var(--bg-app)', borderBottom: i < stages.length - 1 ? '1px solid var(--border-subtle)' : 'none', fontSize: 12, color: 'var(--fg-tertiary)', lineHeight: 1.6 }}>
                {stage.detail}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

// ── AssignmentStatusBadge ───────────────────────────────────────
const AssignmentStatusBadge = ({ assignment }) => {
  let cfg;
  if      (assignment.out_of_inventory) cfg = { fg: 'var(--danger)',  bg: 'var(--danger-bg)',  label: 'Out of inventory' };
  else if (assignment.excess)           cfg = { fg: 'var(--danger)',  bg: 'var(--danger-bg)',  label: 'Excess (certified)' };
  else if (assignment.forced)           cfg = { fg: 'var(--warning)', bg: 'var(--warning-bg)', label: 'Forced' };
  else                                  cfg = { fg: 'var(--success)', bg: 'var(--success-bg)', label: 'Matched' };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 7px', borderRadius: 'var(--radius-sm)', fontSize: 10, fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color: cfg.fg, background: cfg.bg }}>
      <span style={{ width: 5, height: 5, borderRadius: 99, background: 'currentColor', flexShrink: 0 }} />
      {cfg.label}
    </span>
  );
};

// ── DetectionSummaryCard ────────────────────────────────────────
const DetectionSummaryCard = ({ detection, startExpanded = false }) => {
  const [open, setOpen] = React.useState(startExpanded);
  React.useEffect(() => { setOpen(startExpanded); }, [startExpanded]);
  const f = detection.fields;
  const a = detection.assignment;
  const hasReview = Object.entries(f).some(([k, v]) => k !== 'barcode_ndc' && v.value !== null && v.conf > 0 && v.conf < 0.40);

  return (
    <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-md)', overflow: 'hidden', boxShadow: 'var(--shadow-1)' }}>
      {/* Header row */}
      <button onClick={() => setOpen(!open)} style={{ width: '100%', textAlign: 'left', border: 'none', padding: '10px 14px', background: open ? 'var(--bg-row-hover)' : 'transparent', borderBottom: open ? '1px solid var(--border-default)' : 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className="mono-sm" style={{ fontWeight: 700, color: 'var(--fg-primary)', minWidth: 52 }}>{detection.source_image}#{detection.instance_id}</span>
        <span style={{ padding: '2px 6px', borderRadius: 'var(--radius-sm)', background: 'var(--bg-input)', fontSize: 11, fontWeight: 600, color: 'var(--fg-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{detection.class_label}</span>
        <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>det {Math.round(detection.score * 100)} %</span>
        <div style={{ flex: 1 }} />
        <AssignmentStatusBadge assignment={a} />
        {a.slot_id !== null && (
          <span className="mono-sm" style={{ color: 'var(--fg-tertiary)' }}>s{a.slot_id} · {Math.round(a.score * 100)} %</span>
        )}
        {hasReview && (
          <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 5px', borderRadius: 'var(--radius-sm)', background: 'var(--warning-bg)', color: 'var(--warning)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>review</span>
        )}
        <i data-lucide={open ? 'chevron-up' : 'chevron-down'} style={{ width: 13, height: 13, strokeWidth: 1.5, color: 'var(--fg-muted)', flexShrink: 0 }} />
      </button>

      {open && (
        <div style={{ padding: '12px 14px' }}>
          {/* Quick strip */}
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', padding: '8px 12px', background: 'var(--bg-app)', borderRadius: 'var(--radius-sm)', marginBottom: 12 }}>
            {['ndc','lot','exp','strength','product'].map(k => {
              const fv = f[k];
              return (
                <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 9, color: 'var(--fg-muted)', textTransform: 'uppercase', letterSpacing: '0.07em', fontWeight: 600 }}>{k}</span>
                  <span style={{ fontFamily: ['ndc','lot','exp'].includes(k) ? 'var(--font-mono)' : 'var(--font-sans)', fontSize: 12, fontWeight: 500, color: fv?.value ? confColor(fv.conf) : 'var(--fg-muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 140 }}>
                    {fv?.value || '—'}
                  </span>
                </div>
              );
            })}
          </div>
          <OCRFieldsTable fields={f} showSources={true} showBarcode={true} compact={true} />
        </div>
      )}
    </div>
  );
};

// ── CleanupAuditPanel ───────────────────────────────────────────
const CleanupAuditPanel = ({ cleanup }) => {
  const { nms_merges = [], frame_rejects = [], reflection_rejects = [] } = cleanup || {};
  const total = nms_merges.length + frame_rejects.length + reflection_rejects.length;

  if (total === 0) {
    return (
      <div style={{ padding: '14px 16px', background: 'var(--success-bg)', border: '1px solid var(--tier-identity-border)', borderRadius: 'var(--radius-md)', display: 'flex', alignItems: 'center', gap: 10 }}>
        <i data-lucide="check-circle-2" style={{ width: 15, height: 15, strokeWidth: 1.5, color: 'var(--success)', flexShrink: 0 }} />
        <span style={{ fontSize: 13, color: 'var(--success)', fontWeight: 500 }}>No cleanup actions — all detections survived</span>
      </div>
    );
  }

  const badge = (label, color, bg) => (
    <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 5px', borderRadius: 'var(--radius-sm)', background: bg, color, textTransform: 'uppercase', letterSpacing: '0.05em', flexShrink: 0, marginTop: 1 }}>{label}</span>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {nms_merges.map((m, i) => (
        <div key={i} style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-md)', padding: '9px 12px', display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          {badge('NMS', 'var(--info)', 'var(--info-bg)')}
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--fg-primary)' }}>Instance #{m.absorbed_ids.join(', #')} absorbed into #{m.survivor_id}</div>
            <div style={{ fontSize: 11, color: 'var(--fg-tertiary)', marginTop: 2 }}>{m.source_image} · {m.class_survivor} ← {m.class_absorbed} · {m.overlap_pct} % overlap</div>
          </div>
          <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{m.overlap_pct} %</span>
        </div>
      ))}
      {frame_rejects.map((r, i) => (
        <div key={i} style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-md)', padding: '9px 12px', display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          {badge('FRAME', 'var(--warning)', 'var(--warning-bg)')}
          <div>
            <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--fg-primary)' }}>Instance #{r.instance_id} rejected — frame-spanning coverage</div>
            <div style={{ fontSize: 11, color: 'var(--fg-tertiary)', marginTop: 2 }}>Coverage: {r.coverage_pct} %</div>
          </div>
        </div>
      ))}
      {reflection_rejects.map((r, i) => (
        <div key={i} style={{ background: 'var(--bg-surface)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-md)', padding: '9px 12px', display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          {badge('REFLECT', 'var(--warning)', 'var(--warning-bg)')}
          <div>
            <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--fg-primary)' }}>Instance #{r.instance_id} rejected — reflection artifact</div>
          </div>
        </div>
      ))}
    </div>
  );
};

// ── NDCEnrichmentPanel ──────────────────────────────────────────
const NDCEnrichmentPanel = ({ enrichment, ndc }) => {
  if (!enrichment) {
    return (
      <div style={{ padding: '12px 14px', background: 'var(--bg-app)', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)', fontSize: 12, color: 'var(--fg-tertiary)' }}>
        <i data-lucide="database" style={{ width: 13, height: 13, strokeWidth: 1.5, marginRight: 6, color: 'var(--fg-muted)' }} />
        {ndc ? `NDC ${ndc} — not found in FDA NDC directory` : 'No NDC detected — enrichment unavailable'}
      </div>
    );
  }

  const rows = [
    { label: 'Brand',        value: enrichment.brand },
    { label: 'Generic',      value: enrichment.generic },
    { label: 'Manufacturer', value: enrichment.manufacturer },
    { label: 'Dosage form',  value: enrichment.dosage_form },
    { label: 'Strength (DB)', value: enrichment.strength_db },
    { label: 'Source',       value: enrichment.source === 'ndc_directory' ? 'FDA NDC Directory 2026-04' : enrichment.source },
  ].filter(r => r.value);

  return (
    <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--tier-sku-border)', borderRadius: 'var(--radius-md)', overflow: 'hidden' }}>
      <div style={{ padding: '7px 12px', background: 'var(--tier-sku-bg)', borderBottom: '1px solid var(--tier-sku-border)', display: 'flex', alignItems: 'center', gap: 8 }}>
        <i data-lucide="database" style={{ width: 13, height: 13, strokeWidth: 1.5, color: 'var(--tier-sku)', flexShrink: 0 }} />
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--tier-sku)' }}>NDC enrichment</span>
        {ndc && <span className="mono-sm" style={{ color: 'var(--tier-sku)', opacity: 0.75 }}>{ndc}</span>}
      </div>
      <div style={{ padding: '6px 12px' }}>
        {rows.map((r, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'baseline', gap: 10, padding: '4px 0', borderBottom: i < rows.length - 1 ? '1px solid var(--border-subtle)' : 'none' }}>
            <span style={{ width: 94, fontSize: 11, color: 'var(--fg-tertiary)', fontWeight: 500, flexShrink: 0 }}>{r.label}</span>
            <span style={{ fontSize: 12, color: 'var(--fg-secondary)' }}>{r.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

Object.assign(window, { PipelineRunTimeline, DetectionSummaryCard, CleanupAuditPanel, NDCEnrichmentPanel, AssignmentStatusBadge });
