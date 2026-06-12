// OCR Components — ocr_fields.py display layer
// OCRFieldsTable · OCRReviewAlert · FieldSourceTag · BarcodeNDCPanel

const FIELD_ORDER = ['ndc','lot','exp','product','mfg','brand','strength','vol_ml','mdv','instructions'];
const FIELD_LABELS = { ndc:'NDC', lot:'Lot', exp:'Exp', product:'Product', mfg:'Mfg', brand:'Brand', strength:'Strength', vol_ml:'Vol (mL)', mdv:'MDV/SDV', instructions:'Instructions', barcode_ndc:'Barcode NDC' };
const MONO_FIELDS  = new Set(['ndc','lot','exp','vol_ml','barcode_ndc']);

function confColor(c) {
  if (c >= 0.90) return 'var(--success)';
  if (c >= 0.70) return 'var(--tier-sku)';
  if (c >= 0.40) return 'var(--warning)';
  return 'var(--danger)';
}

function sourceLabel(src) {
  if (!src) return null;
  const MAP = {
    'extract_ndc:segmented':     'NDC · segmented',
    'extract_ndc:run':           'NDC · digit run',
    'extract_lot:labeled':       'Lot · label match',
    'extract_lot:bare_alnum':    'Lot · unlabeled',
    'extract_lot:bare_digits':   'Lot · digit run',
    'extract_exp:MM/YY':         'Exp · MM/YY',
    'extract_exp:MM/DD/YYYY':    'Exp · MM/DD/YYYY',
    'extract_exp:MM/YYYY':       'Exp · MM/YYYY',
    'extract_exp:MMM YYYY':      'Exp · month abbrev',
    'extract_strength':          'Strength · regex',
    'extract_vol_ml':            'Vol · regex',
    'extract_mdv':               'MDV · keyword',
    'extract_mfg:directory':     'Mfg · OCR + dir',
    'extract_product:directory': 'Product · OCR + dir',
    'ndc_db_enrichment:mfg':     'Mfg · FDA NDC DB',
    'ndc_db_enrichment:brand':   'Brand · FDA NDC DB',
    'ndc_db_enrichment:product': 'Product · FDA NDC DB',
    'ndc_db_enrichment:strength':'Strength · FDA NDC DB',
    'labeler_json:mfg':          'Mfg · labeler dir',
    'barcode:GS1-128':           'GS1-128 barcode',
  };
  if (MAP[src]) return MAP[src];
  if (src.startsWith('extract_instructions:')) return 'Instructions · keyword';
  return src;
}

function sourceKind(src) {
  if (!src) return 'none';
  if (src.startsWith('ndc_db') || src.startsWith('labeler')) return 'db';
  if (src.startsWith('barcode')) return 'barcode';
  return 'ocr';
}

// ── FieldSourceTag ──────────────────────────────────────────────
const FieldSourceTag = ({ source }) => {
  if (!source) return null;
  const label = sourceLabel(source);
  const kind  = sourceKind(source);
  const colors = {
    ocr:     { fg: 'var(--info)',    bg: 'var(--info-bg)' },
    db:      { fg: 'var(--pending)', bg: 'var(--pending-bg)' },
    barcode: { fg: 'var(--success)', bg: 'var(--success-bg)' },
  }[kind] || { fg: 'var(--fg-tertiary)', bg: 'var(--slate-100)' };

  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center',
      padding: '1px 5px', borderRadius: 'var(--radius-sm)',
      fontSize: 10, fontWeight: 500, fontFamily: 'var(--font-mono)',
      color: colors.fg, background: colors.bg,
      whiteSpace: 'nowrap', letterSpacing: '0.02em',
    }}>{label}</span>
  );
};

// ── OCRReviewAlert ──────────────────────────────────────────────
const OCRReviewAlert = ({ fields, threshold = 0.40 }) => {
  const flagged = Object.entries(fields)
    .filter(([k, v]) => k !== 'barcode_ndc' && v.value !== null && v.conf > 0 && v.conf < threshold)
    .map(([k]) => FIELD_LABELS[k] || k);
  if (!flagged.length) return null;
  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: 10,
      padding: '9px 12px',
      background: 'var(--warning-bg)',
      border: '1px solid var(--tier-therapeutic-border)',
      borderRadius: 'var(--radius-md)', marginBottom: 10,
    }}>
      <i data-lucide="alert-triangle" style={{ width: 14, height: 14, strokeWidth: 1.5, color: 'var(--warning)', flexShrink: 0, marginTop: 1 }} />
      <div>
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--warning)' }}>
          Low-confidence field{flagged.length > 1 ? 's' : ''} — human review required
        </div>
        <div style={{ fontSize: 11, color: 'var(--fg-secondary)', marginTop: 2 }}>
          {flagged.join(' · ')} · below {Math.round(threshold * 100)} %
        </div>
      </div>
    </div>
  );
};

// ── BarcodeNDCPanel ─────────────────────────────────────────────
const BarcodeNDCPanel = ({ barcodeNDC, ocrNDC, barcodeConf, ocrConf }) => {
  const match = barcodeNDC && ocrNDC && barcodeNDC === ocrNDC;
  const mismatch = barcodeNDC && ocrNDC && barcodeNDC !== ocrNDC;
  const bg  = barcodeNDC ? 'var(--success-bg)'         : 'var(--bg-app)';
  const bdr = barcodeNDC ? 'var(--tier-identity-border)' : 'var(--border-default)';
  return (
    <div style={{ background: bg, border: `1px solid ${bdr}`, borderRadius: 'var(--radius-md)', padding: '10px 12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span className="section-header">NDC sources</span>
        {barcodeNDC && (
          <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 'var(--radius-sm)', background: 'var(--success)', color: 'white', letterSpacing: '0.04em' }}>
            BARCODE ACTIVE
          </span>
        )}
      </div>
      {[
        { rowLabel: 'BARCODE', val: barcodeNDC, conf: barcodeConf, src: 'barcode:GS1-128', highlight: !!barcodeNDC },
        { rowLabel: 'OCR',     val: ocrNDC,     conf: ocrConf,     src: 'extract_ndc:segmented', highlight: false },
      ].map(({ rowLabel, val, conf, src, highlight }) => (
        <div key={rowLabel} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 0' }}>
          <span style={{ width: 64, fontSize: 10, fontWeight: 600, color: 'var(--fg-tertiary)', letterSpacing: '0.06em' }}>{rowLabel}</span>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: highlight ? 600 : 400, color: val ? (highlight ? 'var(--success)' : 'var(--fg-secondary)') : 'var(--fg-muted)', flex: 1 }}>
            {val || '—'}
          </span>
          {val && <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: confColor(conf), fontWeight: 600, minWidth: 28 }}>{Math.round(conf * 100)} %</span>}
          {val && <FieldSourceTag source={src} />}
        </div>
      ))}
      {(match || mismatch) && (
        <div style={{ marginTop: 6, fontSize: 11, color: match ? 'var(--success)' : 'var(--danger)', fontWeight: 500 }}>
          {match ? '✓ Barcode and OCR agree' : '⚠ Barcode and OCR disagree — barcode value used'}
        </div>
      )}
    </div>
  );
};

// ── OCRFieldRow ─────────────────────────────────────────────────
const OCRFieldRow = ({ fieldKey, field, showSource }) => {
  const [editing, setEditing] = React.useState(false);
  const [val, setVal]         = React.useState(field.value || '');
  React.useEffect(() => { setVal(field.value || ''); }, [field.value]);
  const isMono   = MONO_FIELDS.has(fieldKey);
  const hasValue = field.value !== null && field.value !== undefined;
  const isLow    = hasValue && field.conf > 0 && field.conf < 0.40;
  const cols     = showSource ? '76px 1fr 120px 36px' : '76px 1fr 36px';

  return (
    <div style={{
      display: 'grid', gridTemplateColumns: cols, gap: 8,
      padding: '5px 0', borderBottom: '1px solid var(--border-subtle)',
      alignItems: 'center', opacity: hasValue ? 1 : 0.45,
    }}>
      <div style={{ fontSize: 11, color: 'var(--fg-tertiary)', fontWeight: 500 }}>{FIELD_LABELS[fieldKey] || fieldKey}</div>

      {editing ? (
        <input autoFocus value={val}
          onChange={e => setVal(e.target.value)}
          onBlur={() => setEditing(false)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === 'Escape') setEditing(false); }}
          style={{ height: 24, padding: '0 6px', border: '1px solid var(--border-focus)', borderRadius: 'var(--radius-sm)', fontFamily: isMono ? 'var(--font-mono)' : 'var(--font-sans)', fontSize: 12, outline: 'none', background: 'var(--bg-input)' }}
        />
      ) : (
        <div onClick={() => hasValue && setEditing(true)}
          style={{ fontSize: 12, fontFamily: isMono ? 'var(--font-mono)' : 'var(--font-sans)', fontWeight: isMono ? 500 : 400, cursor: hasValue ? 'pointer' : 'default', padding: '2px 4px', margin: '-2px -4px', borderRadius: 'var(--radius-sm)', color: hasValue ? 'var(--fg-primary)' : 'var(--fg-muted)', fontStyle: hasValue ? 'normal' : 'italic', borderBottom: isLow ? '1px dashed var(--warning)' : 'none', transition: 'background 80ms', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
          onMouseEnter={e => { if (hasValue) e.currentTarget.style.background = 'var(--bg-row-hover)'; }}
          onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}>
          {hasValue ? val : 'Not detected'}
        </div>
      )}

      {showSource && (
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          {hasValue && <FieldSourceTag source={field.source} />}
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
        {hasValue && field.conf > 0 && (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 600, color: confColor(field.conf), minWidth: 26, textAlign: 'right' }}>
            {Math.round(field.conf * 100)}
          </span>
        )}
      </div>
    </div>
  );
};

// ── OCRFieldsTable ──────────────────────────────────────────────
const OCRFieldsTable = ({ fields, showSources = false, showBarcode = true, compact = false }) => {
  const f = fields || {};
  const flagged  = Object.entries(f).filter(([k, v]) => k !== 'barcode_ndc' && v.value !== null && v.conf > 0 && v.conf < 0.40);
  const detected = FIELD_ORDER.filter(k => f[k]?.value !== null && f[k]?.value !== undefined);
  const hasBcode = f.barcode_ndc?.value != null;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span className="section-header">Extracted fields</span>
        <span style={{ fontSize: 10, color: 'var(--fg-muted)' }}>
          {detected.length}/{FIELD_ORDER.length} detected
          {flagged.length > 0 && <span style={{ color: 'var(--warning)', marginLeft: 6 }}>· {flagged.length} flagged</span>}
        </span>
      </div>

      {flagged.length > 0 && <OCRReviewAlert fields={f} />}

      {showBarcode && hasBcode && !compact && (
        <div style={{ marginBottom: 10 }}>
          <BarcodeNDCPanel barcodeNDC={f.barcode_ndc?.value} ocrNDC={f.ndc?.value} barcodeConf={f.barcode_ndc?.conf || 0} ocrConf={f.ndc?.conf || 0} />
        </div>
      )}

      <div>
        {FIELD_ORDER.map(k => (
          <OCRFieldRow key={k} fieldKey={k} field={f[k] || { value: null, conf: 0, source: null }} showSource={showSources} />
        ))}
      </div>
    </div>
  );
};

Object.assign(window, { OCRFieldsTable, OCRReviewAlert, FieldSourceTag, BarcodeNDCPanel, confColor, sourceLabel });
