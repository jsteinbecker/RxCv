// Form Components — capture workflow · OCR correction · certification · rejection

const REJECTION_REASONS = [
    {value: 'lot_mismatch', label: 'Lot mismatch'},
    {value: 'ndc_mismatch', label: 'NDC mismatch'},
    {value: 'exp_mismatch', label: 'Expiration mismatch'},
    {value: 'therapeutic_not_authorized', label: 'Therapeutic substitution — not authorized'},
    {value: 'wrong_product', label: 'Wrong product entirely'},
    {value: 'unreadable', label: 'Label unreadable'},
    {value: 'other', label: 'Other (specify below)'},
];

const CORRECTION_REASONS = [
    {value: 'ocr_error', label: 'OCR misread character(s)'},
    {value: 'rotation', label: 'Label rotated — fragment joined incorrectly'},
    {value: 'obscured', label: 'Partial label obscured in image'},
    {value: 'different_format', label: 'Different format than expected'},
    {value: 'manual_confirm', label: 'Manually confirmed from physical label'},
];

// ── shared label style ──────────────────────────────────────────
const FormLabel = ({children, sub}) => (
    <div style={{marginBottom: 5}}>
        <span style={{
            fontSize: 11,
            fontWeight: 600,
            color: 'var(--fg-tertiary)',
            textTransform: 'uppercase',
            letterSpacing: '0.06em'
        }}>{children}</span>
        {sub && <span style={{
            fontSize: 11,
            fontWeight: 400,
            textTransform: 'none',
            letterSpacing: 0,
            color: 'var(--fg-muted)',
            marginLeft: 6
        }}>{sub}</span>}
    </div>
);

const FieldInput = ({value, onChange, placeholder, mono, onFocus, onBlur}) => (
    <input value={value} onChange={onChange} placeholder={placeholder}
           style={{
               width: '100%',
               height: 34,
               padding: '0 10px',
               border: '1px solid var(--border-default)',
               borderRadius: 'var(--radius-sm)',
               fontFamily: mono ? 'var(--font-mono)' : 'var(--font-sans)',
               fontSize: 13,
               outline: 'none',
               background: 'var(--bg-input)',
               color: 'var(--fg-primary)'
           }}
           onFocus={e => {
               e.target.style.borderColor = 'var(--border-focus)';
               onFocus && onFocus(e);
           }}
           onBlur={e => {
               e.target.style.borderColor = 'var(--border-default)';
               onBlur && onBlur(e);
           }}
    />
);

// ── NewCaptureForm ──────────────────────────────────────────────
const NewCaptureForm = ({onSubmit}) => {
    const [step, setStep] = React.useState(1);
    const [orderText, setOrderText] = React.useState('');
    const [barcodes, setBarcodes] = React.useState([]);
    const [barcodeIn, setBarcodeIn] = React.useState('');
    const [expected, setExpected] = React.useState([]);
    const [expectedIn, setExpectedIn] = React.useState('');
    const [imgCount, setImgCount] = React.useState(0);
    const [certified, setCertified] = React.useState(false);
    const [done, setDone] = React.useState(false);

    const addBarcode = () => {
        if (barcodeIn.trim()) {
            setBarcodes([...barcodes, barcodeIn.trim()]);
            setBarcodeIn('');
        }
    };
    const addExpected = () => {
        if (expectedIn.trim()) {
            setExpected([...expected, expectedIn.trim()]);
            setExpectedIn('');
        }
    };
    const keyAdd = (adder) => (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            adder();
        }
    };

    const handleRun = () => {
        setDone(true);
        setTimeout(() => {
            setDone(false);
            setStep(1);
            setOrderText('');
            setBarcodes([]);
            setExpected([]);
            setImgCount(0);
            setCertified(false);
            onSubmit && onSubmit();
        }, 1600);
    };

    if (done) return (
        <div style={{
            padding: 32,
            textAlign: 'center',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 12
        }}>
            <div style={{
                width: 40,
                height: 40,
                borderRadius: 99,
                background: 'var(--success-bg)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center'
            }}>
                <i data-lucide="check-circle-2"
                   style={{width: 22, height: 22, strokeWidth: 1.5, color: 'var(--success)'}}/>
            </div>
            <div style={{fontSize: 14, fontWeight: 600, color: 'var(--fg-primary)'}}>Pipeline started</div>
            <div style={{fontSize: 12, color: 'var(--fg-tertiary)'}}>Check Inbox for results · estimated 6–10 s</div>
        </div>
    );

    const STEPS = [{n: 1, label: 'Order'}, {n: 2, label: 'Images'}, {n: 3, label: 'Review & run'}];

    return (
        <div style={{
            background: 'var(--bg-surface)',
            border: '1px solid var(--border-default)',
            borderRadius: 'var(--radius-md)',
            overflow: 'hidden'
        }}>
            {/* Step bar */}
            <div style={{
                padding: '10px 18px',
                background: 'var(--bg-app)',
                borderBottom: '1px solid var(--border-default)',
                display: 'flex',
                alignItems: 'center',
                gap: 0
            }}>
                {STEPS.map((s, i) => (
                    <React.Fragment key={s.n}>
                        <div style={{display: 'flex', alignItems: 'center', gap: 7}}>
                            <div style={{
                                width: 22,
                                height: 22,
                                borderRadius: 99,
                                background: s.n <= step ? 'var(--brand)' : 'var(--bg-input)',
                                border: `2px solid ${s.n <= step ? 'var(--brand)' : 'var(--border-default)'}`,
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                fontSize: 11,
                                fontWeight: 700,
                                color: s.n <= step ? 'white' : 'var(--fg-muted)',
                                flexShrink: 0
                            }}>
                                {s.n < step ? '✓' : s.n}
                            </div>
                            <span style={{
                                fontSize: 12,
                                fontWeight: s.n === step ? 600 : 400,
                                color: s.n === step ? 'var(--fg-primary)' : 'var(--fg-tertiary)'
                            }}>{s.label}</span>
                        </div>
                        {i < STEPS.length - 1 && <div style={{
                            flex: 1,
                            height: 1,
                            background: 'var(--border-default)',
                            margin: '0 10px',
                            maxWidth: 32
                        }}/>}
                    </React.Fragment>
                ))}
            </div>

            {/* Body */}
            <div style={{padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: 14}}>

                {/* ── Step 1 ── */}
                {step === 1 && <>
                    <div>
                        <FormLabel>Order</FormLabel>
                        <FieldInput value={orderText} onChange={e => setOrderText(e.target.value)}
                                    placeholder="Ceftazidime-Avibactam 2.5 g in 250 mL NS"/>
                    </div>

                    <div>
                        <FormLabel sub="— optional, used as priority hints">Expected components</FormLabel>
                        <div style={{display: 'flex', gap: 6}}>
                            <input value={expectedIn} onChange={e => setExpectedIn(e.target.value)}
                                   onKeyDown={keyAdd(addExpected)} placeholder="VIAL, Ceftazidime-Avibactam 2.5 g, #1"
                                   style={{
                                       flex: 1,
                                       height: 32,
                                       padding: '0 10px',
                                       border: '1px solid var(--border-default)',
                                       borderRadius: 'var(--radius-sm)',
                                       fontFamily: 'var(--font-sans)',
                                       fontSize: 12,
                                       outline: 'none',
                                       background: 'var(--bg-input)'
                                   }}
                                   onFocus={e => e.target.style.borderColor = 'var(--border-focus)'}
                                   onBlur={e => e.target.style.borderColor = 'var(--border-default)'}/>
                            <button onClick={addExpected} style={{
                                height: 32,
                                padding: '0 12px',
                                background: 'var(--bg-surface)',
                                border: '1px solid var(--border-default)',
                                borderRadius: 'var(--radius-sm)',
                                fontSize: 12,
                                color: 'var(--fg-secondary)',
                                fontWeight: 500,
                                cursor: 'pointer'
                            }}>Add
                            </button>
                        </div>
                        {expected.length > 0 && (
                            <div style={{marginTop: 6, display: 'flex', flexDirection: 'column', gap: 3}}>
                                {expected.map((c, i) => (
                                    <div key={i} style={{
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: 8,
                                        padding: '3px 8px',
                                        background: 'var(--bg-app)',
                                        borderRadius: 'var(--radius-sm)',
                                        border: '1px solid var(--border-subtle)'
                                    }}>
                                        <span style={{fontSize: 12, flex: 1, color: 'var(--fg-secondary)'}}>{c}</span>
                                        <button onClick={() => setExpected(expected.filter((_, j) => j !== i))} style={{
                                            background: 'none',
                                            border: 'none',
                                            cursor: 'pointer',
                                            color: 'var(--fg-muted)',
                                            fontSize: 14,
                                            padding: 0,
                                            lineHeight: 1
                                        }}>×
                                        </button>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>

                    <div>
                        <FormLabel sub="— optional, high-confidence NDC hints">Pre-scanned barcodes</FormLabel>
                        <div style={{display: 'flex', gap: 6}}>
                            <input value={barcodeIn} onChange={e => setBarcodeIn(e.target.value)}
                                   onKeyDown={keyAdd(addBarcode)} placeholder="0264-7800-10"
                                   style={{
                                       flex: 1,
                                       height: 32,
                                       padding: '0 10px',
                                       border: '1px solid var(--border-default)',
                                       borderRadius: 'var(--radius-sm)',
                                       fontFamily: 'var(--font-mono)',
                                       fontSize: 12,
                                       outline: 'none',
                                       background: 'var(--bg-input)'
                                   }}
                                   onFocus={e => e.target.style.borderColor = 'var(--border-focus)'}
                                   onBlur={e => e.target.style.borderColor = 'var(--border-default)'}/>
                            <button onClick={addBarcode} style={{
                                height: 32,
                                padding: '0 12px',
                                background: 'var(--bg-surface)',
                                border: '1px solid var(--border-default)',
                                borderRadius: 'var(--radius-sm)',
                                fontSize: 12,
                                color: 'var(--fg-secondary)',
                                fontWeight: 500,
                                cursor: 'pointer'
                            }}>Scan
                            </button>
                        </div>
                        {barcodes.length > 0 && (
                            <div style={{marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 4}}>
                                {barcodes.map((b, i) => (
                                    <span key={i} style={{
                                        display: 'inline-flex',
                                        alignItems: 'center',
                                        gap: 4,
                                        padding: '2px 8px',
                                        borderRadius: 'var(--radius-sm)',
                                        background: 'var(--success-bg)',
                                        fontFamily: 'var(--font-mono)',
                                        fontSize: 11,
                                        color: 'var(--success)'
                                    }}>
                    {b}
                                        <button onClick={() => setBarcodes(barcodes.filter((_, j) => j !== i))} style={{
                                            background: 'none',
                                            border: 'none',
                                            cursor: 'pointer',
                                            color: 'inherit',
                                            fontSize: 12,
                                            padding: 0,
                                            lineHeight: 1
                                        }}>×</button>
                  </span>
                                ))}
                            </div>
                        )}
                    </div>
                </>}

                {/* ── Step 2 ── */}
                {step === 2 && <>
                    <div style={{fontSize: 12, color: 'var(--fg-tertiary)', lineHeight: 1.6}}>
                        The <strong style={{color: 'var(--fg-secondary)'}}>first image</strong> is the reference
                        inventory — photograph all containers on the tray. Subsequent images are matched against it.
                    </div>
                    <div
                        style={{display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(100px, 1fr))', gap: 8}}>
                        {Array.from({length: imgCount}).map((_, i) => (
                            <div key={i} style={{
                                aspectRatio: '3/4',
                                background: i === 0 ? 'var(--info-bg)' : 'var(--bg-app)',
                                border: `1px solid ${i === 0 ? 'var(--tier-sku-border)' : 'var(--border-default)'}`,
                                borderRadius: 'var(--radius-sm)',
                                display: 'flex',
                                flexDirection: 'column',
                                alignItems: 'center',
                                justifyContent: 'center',
                                gap: 5
                            }}>
                                <i data-lucide="image" style={{
                                    width: 18,
                                    height: 18,
                                    strokeWidth: 1.5,
                                    color: i === 0 ? 'var(--info)' : 'var(--fg-muted)'
                                }}/>
                                <span style={{
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: 10,
                                    fontWeight: 700,
                                    color: i === 0 ? 'var(--info)' : 'var(--fg-tertiary)'
                                }}>img{i + 1}</span>
                                {i === 0 && <span style={{
                                    fontSize: 9,
                                    color: 'var(--info)',
                                    fontWeight: 600,
                                    textTransform: 'uppercase',
                                    letterSpacing: '0.04em'
                                }}>REF</span>}
                            </div>
                        ))}
                        <button onClick={() => setImgCount(imgCount + 1)} style={{
                            aspectRatio: '3/4',
                            background: 'var(--bg-app)',
                            border: '2px dashed var(--border-strong)',
                            borderRadius: 'var(--radius-sm)',
                            cursor: 'pointer',
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            justifyContent: 'center',
                            gap: 4,
                            color: 'var(--fg-tertiary)',
                            fontFamily: 'var(--font-sans)'
                        }}>
                            <i data-lucide="plus" style={{width: 16, height: 16, strokeWidth: 1.5}}/>
                            <span style={{fontSize: 10}}>Add</span>
                        </button>
                    </div>
                </>}

                {/* ── Step 3 ── */}
                {step === 3 && <>
                    <div style={{
                        background: 'var(--bg-app)',
                        border: '1px solid var(--border-default)',
                        borderRadius: 'var(--radius-md)',
                        padding: '10px 12px',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 5
                    }}>
                        {[['Order', orderText || 'Not specified'], ['Images', `${imgCount} (1 reference, ${Math.max(0, imgCount - 1)} subsequent)`], ['Barcodes', `${barcodes.length} scanned`], ['Expected components', `${expected.length}`]].map(([k, v]) => (
                            <div key={k} style={{display: 'flex', justifyContent: 'space-between', fontSize: 12}}>
                                <span style={{color: 'var(--fg-tertiary)'}}>{k}</span>
                                <span className="mono-sm" style={{color: 'var(--fg-secondary)'}}>{v}</span>
                            </div>
                        ))}
                    </div>
                    <label style={{
                        display: 'flex',
                        alignItems: 'flex-start',
                        gap: 10,
                        cursor: 'pointer',
                        padding: '10px 12px',
                        background: certified ? 'var(--warning-bg)' : 'var(--bg-surface)',
                        border: `1px solid ${certified ? 'var(--tier-therapeutic-border)' : 'var(--border-default)'}`,
                        borderRadius: 'var(--radius-md)',
                        transition: 'all 120ms'
                    }}>
                        <input type="checkbox" checked={certified} onChange={e => setCertified(e.target.checked)}
                               style={{marginTop: 3, flexShrink: 0, accentColor: 'var(--warning)'}}/>
                        <div>
                            <div style={{
                                fontSize: 13,
                                fontWeight: 600,
                                color: certified ? 'var(--warning)' : 'var(--fg-primary)'
                            }}>Certified subset mode
                            </div>
                            <div style={{fontSize: 11, color: 'var(--fg-tertiary)', marginTop: 3, lineHeight: 1.5}}>
                                Every item in subsequent images is contained in the reference inventory. The matcher
                                will bypass the confidence threshold and force-assign all detections.
                            </div>
                        </div>
                    </label>
                </>}
            </div>

            {/* Footer */}
            <div style={{
                padding: '10px 18px',
                borderTop: '1px solid var(--border-default)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center'
            }}>
                <button onClick={() => setStep(Math.max(1, step - 1))} disabled={step === 1} style={{
                    height: 30,
                    padding: '0 12px',
                    background: 'transparent',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-sm)',
                    fontFamily: 'var(--font-sans)',
                    fontSize: 12,
                    fontWeight: 500,
                    color: 'var(--fg-secondary)',
                    cursor: step === 1 ? 'not-allowed' : 'pointer',
                    opacity: step === 1 ? 0.4 : 1
                }}>← Back
                </button>
                <div style={{display: 'flex', gap: 5}}>
                    {[1, 2, 3].map(n => <div key={n} style={{
                        width: 6,
                        height: 6,
                        borderRadius: 99,
                        background: n === step ? 'var(--brand)' : 'var(--border-strong)'
                    }}/>)}
                </div>
                {step < 3
                    ? <button onClick={() => setStep(step + 1)} style={{
                        height: 30,
                        padding: '0 14px',
                        background: 'var(--brand)',
                        border: 'none',
                        borderRadius: 'var(--radius-sm)',
                        fontFamily: 'var(--font-sans)',
                        fontSize: 12,
                        fontWeight: 500,
                        color: 'white',
                        cursor: 'pointer'
                    }}>Continue →</button>
                    : <button onClick={handleRun} disabled={imgCount < 1} style={{
                        height: 30,
                        padding: '0 14px',
                        background: imgCount < 1 ? 'var(--bg-input)' : 'var(--brand)',
                        border: 'none',
                        borderRadius: 'var(--radius-sm)',
                        fontFamily: 'var(--font-sans)',
                        fontSize: 12,
                        fontWeight: 500,
                        color: imgCount < 1 ? 'var(--fg-muted)' : 'white',
                        cursor: imgCount < 1 ? 'not-allowed' : 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6
                    }}>
                        <i data-lucide="play" style={{width: 11, height: 11, strokeWidth: 1.5}}/> Run pipeline
                    </button>
                }
            </div>
        </div>
    );
};

// ── OCRCorrectionForm ───────────────────────────────────────────
const OCRCorrectionForm = ({fieldKey, field, onSave, onCancel}) => {
    const LABELS = {
        ndc: 'NDC',
        lot: 'Lot',
        exp: 'Exp',
        mfg: 'Mfg',
        product: 'Product',
        strength: 'Strength',
        vol_ml: 'Vol (mL)',
        brand: 'Brand',
        mdv: 'MDV/SDV',
        instructions: 'Instructions'
    };
    const [newVal, setNewVal] = React.useState(field?.value || '');
    const [reason, setReason] = React.useState('');
    const [note, setNote] = React.useState('');
    const [saved, setSaved] = React.useState(false);
    const isMono = ['ndc', 'lot', 'exp', 'vol_ml'].includes(fieldKey);
    const changed = newVal !== (field?.value || '');
    const canSave = changed && reason && !saved;

    const handleSave = () => {
        setSaved(true);
        setTimeout(() => {
            setSaved(false);
            onSave && onSave({fieldKey, oldValue: field?.value, newValue: newVal, reason, note});
        }, 1200);
    };

    return (
        <div style={{
            background: 'var(--bg-surface)',
            border: '1px solid var(--border-default)',
            borderRadius: 'var(--radius-md)',
            overflow: 'hidden',
            maxWidth: 460
        }}>
            <div style={{
                padding: '9px 14px',
                background: 'var(--bg-app)',
                borderBottom: '1px solid var(--border-default)',
                display: 'flex',
                alignItems: 'center',
                gap: 8
            }}>
                <i data-lucide="edit-3" style={{width: 14, height: 14, strokeWidth: 1.5, color: 'var(--fg-tertiary)'}}/>
                <span className="h4">Correct OCR field</span>
                <span className="caption">· {LABELS[fieldKey] || fieldKey}</span>
            </div>

            <div style={{padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 12}}>
                {/* Current value */}
                <div style={{
                    padding: '7px 11px',
                    background: 'var(--bg-app)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: 'var(--radius-sm)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10
                }}>
                    <span
                        style={{fontSize: 11, color: 'var(--fg-tertiary)', fontWeight: 500, width: 74, flexShrink: 0}}>Current value</span>
                    <span style={{
                        fontFamily: isMono ? 'var(--font-mono)' : 'var(--font-sans)',
                        fontSize: 12,
                        fontWeight: 500,
                        color: 'var(--fg-primary)',
                        flex: 1
                    }}>{field?.value || '—'}</span>
                    {field?.source && <FieldSourceTag source={field.source}/>}
                    {field?.conf > 0 && <span style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: 10,
                        fontWeight: 600,
                        color: confColor(field.conf)
                    }}>{Math.round(field.conf * 100)} %</span>}
                </div>

                <div>
                    <FormLabel>Corrected value</FormLabel>
                    <input value={newVal} onChange={e => setNewVal(e.target.value)}
                           style={{
                               width: '100%',
                               height: 34,
                               padding: '0 10px',
                               border: `1px solid ${changed ? 'var(--border-focus)' : 'var(--border-default)'}`,
                               borderRadius: 'var(--radius-sm)',
                               fontFamily: isMono ? 'var(--font-mono)' : 'var(--font-sans)',
                               fontSize: 13,
                               outline: 'none',
                               background: 'var(--bg-input)',
                               color: 'var(--fg-primary)'
                           }}
                           onFocus={e => e.target.style.borderColor = 'var(--border-focus)'}
                           onBlur={e => {
                               if (!changed) e.target.style.borderColor = 'var(--border-default)';
                           }}/>
                </div>

                <div>
                    <FormLabel>Reason</FormLabel>
                    <select value={reason} onChange={e => setReason(e.target.value)} style={{
                        width: '100%',
                        height: 34,
                        padding: '0 10px',
                        border: '1px solid var(--border-default)',
                        borderRadius: 'var(--radius-sm)',
                        fontFamily: 'var(--font-sans)',
                        fontSize: 12,
                        outline: 'none',
                        background: 'var(--bg-input)',
                        color: reason ? 'var(--fg-primary)' : 'var(--fg-tertiary)'
                    }}>
                        <option value="">Select a reason…</option>
                        {CORRECTION_REASONS.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
                    </select>
                </div>

                <div>
                    <FormLabel sub="— optional">Note</FormLabel>
                    <textarea value={note} onChange={e => setNote(e.target.value)}
                              placeholder="Additional context for the audit record…" rows={2}
                              style={{
                                  width: '100%',
                                  padding: '7px 10px',
                                  border: '1px solid var(--border-default)',
                                  borderRadius: 'var(--radius-sm)',
                                  fontFamily: 'var(--font-sans)',
                                  fontSize: 12,
                                  outline: 'none',
                                  background: 'var(--bg-input)',
                                  resize: 'vertical',
                                  minHeight: 50,
                                  color: 'var(--fg-primary)'
                              }}
                              onFocus={e => e.target.style.borderColor = 'var(--border-focus)'}
                              onBlur={e => e.target.style.borderColor = 'var(--border-default)'}/>
                </div>

                <div style={{
                    padding: '7px 10px',
                    background: 'var(--bg-app)',
                    border: '1px solid var(--border-subtle)',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: 11,
                    color: 'var(--fg-tertiary)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6
                }}>
                    <i data-lucide="file-clock" style={{width: 12, height: 12, strokeWidth: 1.5, flexShrink: 0}}/>
                    Correction will be recorded in the audit log with your identity and timestamp.
                </div>
            </div>

            <div style={{
                padding: '9px 16px',
                borderTop: '1px solid var(--border-default)',
                display: 'flex',
                justifyContent: 'flex-end',
                gap: 8
            }}>
                {onCancel && <button onClick={onCancel} style={{
                    height: 30,
                    padding: '0 12px',
                    background: 'transparent',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-sm)',
                    fontFamily: 'var(--font-sans)',
                    fontSize: 12,
                    fontWeight: 500,
                    color: 'var(--fg-secondary)',
                    cursor: 'pointer'
                }}>Cancel</button>}
                <button onClick={handleSave} disabled={!canSave}
                        style={{
                            height: 30,
                            padding: '0 14px',
                            background: saved ? 'var(--success)' : (canSave ? 'var(--brand)' : 'var(--bg-input)'),
                            border: 'none',
                            borderRadius: 'var(--radius-sm)',
                            fontFamily: 'var(--font-sans)',
                            fontSize: 12,
                            fontWeight: 500,
                            color: canSave || saved ? 'white' : 'var(--fg-muted)',
                            cursor: canSave ? 'pointer' : 'not-allowed',
                            transition: 'background 120ms'
                        }}>
                    {saved ? '✓ Saved' : 'Save correction'}
                </button>
            </div>
        </div>
    );
};

// ── CertifySubsetModal ──────────────────────────────────────────
const CertifySubsetModal = ({onConfirm, onCancel, user}) => {
    const [confirmed, setConfirmed] = React.useState(false);
    const [certifying, setCertifying] = React.useState(false);
    const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC';

    const handle = () => {
        setCertifying(true);
        setTimeout(() => {
            setCertifying(false);
            onConfirm && onConfirm();
        }, 1000);
    };

    const IMPLICATIONS = [
        {
            title: 'Threshold bypassed',
            detail: 'All Hungarian-paired detections assigned to slots regardless of OCR confidence.'
        },
        {
            title: 'Sub-threshold pairings flagged',
            detail: 'Low-confidence assignments marked forced and surfaced for RPh review.'
        },
        {
            title: 'Over-count flagged',
            detail: 'Any detection beyond the reference slot count marked excess_under_certification — a direct contradiction of your certification.'
        },
    ];

    return (
        <div style={{
            background: 'var(--bg-surface)',
            border: '1px solid var(--tier-therapeutic-border)',
            borderRadius: 'var(--radius-md)',
            overflow: 'hidden',
            maxWidth: 500,
            boxShadow: 'var(--shadow-2)'
        }}>
            <div style={{
                padding: '11px 16px',
                background: 'var(--warning-bg)',
                borderBottom: '1px solid var(--tier-therapeutic-border)',
                display: 'flex',
                alignItems: 'center',
                gap: 10
            }}>
                <i data-lucide="alert-triangle"
                   style={{width: 16, height: 16, strokeWidth: 1.5, color: 'var(--warning)', flexShrink: 0}}/>
                <span style={{fontSize: 14, fontWeight: 600, color: 'var(--warning)'}}>Certified subset mode</span>
            </div>

            <div style={{padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 12}}>
                <p style={{margin: 0, fontSize: 13, color: 'var(--fg-secondary)', lineHeight: 1.6}}>
                    You are certifying that <strong>every item in the subsequent images is contained in the reference
                    inventory image</strong>. This changes how the matcher works:
                </p>

                <div style={{display: 'flex', flexDirection: 'column', gap: 5}}>
                    {IMPLICATIONS.map((item, i) => (
                        <div key={i} style={{
                            padding: '7px 10px',
                            background: 'var(--bg-app)',
                            border: '1px solid var(--border-subtle)',
                            borderRadius: 'var(--radius-sm)',
                            display: 'flex',
                            alignItems: 'flex-start',
                            gap: 9
                        }}>
                            <i data-lucide="chevron-right" style={{
                                width: 12,
                                height: 12,
                                strokeWidth: 2,
                                color: 'var(--warning)',
                                flexShrink: 0,
                                marginTop: 2
                            }}/>
                            <div>
                                <div style={{
                                    fontSize: 12,
                                    fontWeight: 600,
                                    color: 'var(--fg-primary)'
                                }}>{item.title}</div>
                                <div style={{
                                    fontSize: 11,
                                    color: 'var(--fg-tertiary)',
                                    marginTop: 2
                                }}>{item.detail}</div>
                            </div>
                        </div>
                    ))}
                </div>

                <div style={{
                    padding: '9px 11px',
                    background: 'var(--warning-bg)',
                    border: '1px solid var(--tier-therapeutic-border)',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: 12,
                    color: 'var(--fg-secondary)'
                }}>
                    Certifying as <strong>{user?.name || 'J. Steinbecker'}</strong> · {now} · this action is recorded
                    permanently.
                </div>

                <label style={{display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer'}}>
                    <input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}
                           style={{marginTop: 3, flexShrink: 0, accentColor: 'var(--warning)'}}/>
                    <span style={{fontSize: 12, color: 'var(--fg-secondary)', lineHeight: 1.5}}>
            I confirm that all items in the subsequent images are contained in the reference inventory and I accept responsibility for this certification.
          </span>
                </label>
            </div>

            <div style={{
                padding: '9px 16px',
                borderTop: '1px solid var(--border-default)',
                display: 'flex',
                justifyContent: 'flex-end',
                gap: 8
            }}>
                <button onClick={onCancel} style={{
                    height: 30,
                    padding: '0 12px',
                    background: 'transparent',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-sm)',
                    fontFamily: 'var(--font-sans)',
                    fontSize: 12,
                    fontWeight: 500,
                    color: 'var(--fg-secondary)',
                    cursor: 'pointer'
                }}>Cancel
                </button>
                <button onClick={handle} disabled={!confirmed || certifying}
                        style={{
                            height: 30,
                            padding: '0 14px',
                            background: (!confirmed || certifying) ? 'var(--bg-input)' : 'var(--warning)',
                            border: 'none',
                            borderRadius: 'var(--radius-sm)',
                            fontFamily: 'var(--font-sans)',
                            fontSize: 12,
                            fontWeight: 500,
                            color: !confirmed ? 'var(--fg-muted)' : 'white',
                            cursor: !confirmed ? 'not-allowed' : 'pointer',
                            transition: 'background 120ms',
                            display: 'flex',
                            alignItems: 'center',
                            gap: 6
                        }}>
                    {certifying ? 'Certifying…' : <><i data-lucide="alert-triangle"
                                                       style={{width: 11, height: 11, strokeWidth: 1.5}}/> Certify and
                        run</>}
                </button>
            </div>
        </div>
    );
};

// ── RejectComponentForm ─────────────────────────────────────────
const RejectComponentForm = ({detection, onReject, onCancel}) => {
    const [reason, setReason] = React.useState('');
    const [note, setNote] = React.useState('');
    const [confirmed, setConfirmed] = React.useState(false);
    const [rejecting, setRejecting] = React.useState(false);
    const f = detection?.fields || {};
    const canReject = reason && confirmed && !(reason === 'other' && !note) && !rejecting;

    const handle = () => {
        setRejecting(true);
        setTimeout(() => {
            setRejecting(false);
            onReject && onReject({reason, note});
        }, 900);
    };

    return (
        <div style={{
            background: 'var(--bg-surface)',
            border: '1px solid var(--border-default)',
            borderRadius: 'var(--radius-md)',
            overflow: 'hidden',
            maxWidth: 460
        }}>
            <div style={{
                padding: '9px 14px',
                background: 'var(--danger-bg)',
                borderBottom: '1px solid #e8bbb6',
                display: 'flex',
                alignItems: 'center',
                gap: 8
            }}>
                <i data-lucide="x-circle"
                   style={{width: 14, height: 14, strokeWidth: 1.5, color: 'var(--danger)', flexShrink: 0}}/>
                <span style={{fontSize: 14, fontWeight: 600, color: 'var(--danger)'}}>Reject component match</span>
            </div>

            <div style={{padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 12}}>
                {detection && (
                    <div style={{
                        padding: '7px 11px',
                        background: 'var(--bg-app)',
                        border: '1px solid var(--border-subtle)',
                        borderRadius: 'var(--radius-sm)',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 4
                    }}>
                        <div style={{display: 'flex', alignItems: 'center', gap: 8}}>
                            <span className="mono-sm"
                                  style={{fontWeight: 700}}>{detection.source_image}#{detection.instance_id}</span>
                            <span style={{
                                padding: '1px 5px',
                                borderRadius: 'var(--radius-sm)',
                                background: 'var(--bg-input)',
                                fontSize: 10,
                                fontWeight: 600,
                                color: 'var(--fg-secondary)',
                                textTransform: 'uppercase',
                                letterSpacing: '0.04em'
                            }}>{detection.class_label}</span>
                            {detection.assignment?.slot_id != null && <span style={{
                                fontSize: 11,
                                color: 'var(--fg-tertiary)'
                            }}>→ slot {detection.assignment.slot_id} · {Math.round((detection.assignment.score || 0) * 100)} % match</span>}
                        </div>
                        <div style={{display: 'flex', gap: 12}}>
                            {['ndc', 'lot', 'exp'].map(k => {
                                const fv = f[k];
                                return (
                                    <div key={k} style={{display: 'flex', gap: 4, fontSize: 11}}>
                                        <span style={{
                                            color: 'var(--fg-muted)',
                                            textTransform: 'uppercase',
                                            letterSpacing: '0.05em',
                                            fontWeight: 600
                                        }}>{k}</span>
                                        <span style={{
                                            fontFamily: 'var(--font-mono)',
                                            color: fv?.value ? 'var(--fg-secondary)' : 'var(--fg-muted)',
                                            fontStyle: fv?.value ? 'normal' : 'italic'
                                        }}>{fv?.value || 'n/a'}</span>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                )}

                <div>
                    <FormLabel>Reason for rejection</FormLabel>
                    <div style={{display: 'flex', flexDirection: 'column', gap: 3}}>
                        {REJECTION_REASONS.map(r => (
                            <label key={r.value} style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: 8,
                                padding: '6px 10px',
                                borderRadius: 'var(--radius-sm)',
                                background: reason === r.value ? 'var(--danger-bg)' : 'transparent',
                                border: `1px solid ${reason === r.value ? '#e8bbb6' : 'var(--border-subtle)'}`,
                                cursor: 'pointer',
                                transition: 'background 80ms'
                            }}>
                                <input type="radio" name="reject_reason" value={r.value} checked={reason === r.value}
                                       onChange={() => setReason(r.value)}
                                       style={{accentColor: 'var(--danger)', flexShrink: 0}}/>
                                <span style={{
                                    fontSize: 12,
                                    color: reason === r.value ? 'var(--danger)' : 'var(--fg-secondary)'
                                }}>{r.label}</span>
                            </label>
                        ))}
                    </div>
                </div>

                <div>
                    <FormLabel sub={reason === 'other' ? '— required' : '— optional'}>Notes</FormLabel>
                    <textarea value={note} onChange={e => setNote(e.target.value)}
                              placeholder="Describe the issue observed on the physical label…" rows={2}
                              style={{
                                  width: '100%',
                                  padding: '7px 10px',
                                  border: `1px solid ${reason === 'other' && !note ? 'var(--danger)' : 'var(--border-default)'}`,
                                  borderRadius: 'var(--radius-sm)',
                                  fontFamily: 'var(--font-sans)',
                                  fontSize: 12,
                                  outline: 'none',
                                  background: 'var(--bg-input)',
                                  resize: 'vertical',
                                  minHeight: 48,
                                  color: 'var(--fg-primary)'
                              }}
                              onFocus={e => e.target.style.borderColor = 'var(--danger)'}
                              onBlur={e => e.target.style.borderColor = 'var(--border-default)'}/>
                </div>

                <label style={{display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer'}}>
                    <input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}
                           style={{marginTop: 3, flexShrink: 0, accentColor: 'var(--danger)'}}/>
                    <span style={{fontSize: 11, color: 'var(--fg-secondary)', lineHeight: 1.5}}>
            I confirm this rejection. The compound will be flagged and cannot proceed to dispensing without re-verification.
          </span>
                </label>
            </div>

            <div style={{
                padding: '9px 16px',
                borderTop: '1px solid var(--border-default)',
                display: 'flex',
                justifyContent: 'flex-end',
                gap: 8
            }}>
                <button onClick={onCancel} style={{
                    height: 30,
                    padding: '0 12px',
                    background: 'transparent',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-sm)',
                    fontFamily: 'var(--font-sans)',
                    fontSize: 12,
                    fontWeight: 500,
                    color: 'var(--fg-secondary)',
                    cursor: 'pointer'
                }}>Cancel
                </button>
                <button onClick={handle} disabled={!canReject}
                        style={{
                            height: 30,
                            padding: '0 14px',
                            background: canReject ? 'var(--danger)' : 'var(--bg-input)',
                            border: 'none',
                            borderRadius: 'var(--radius-sm)',
                            fontFamily: 'var(--font-sans)',
                            fontSize: 12,
                            fontWeight: 500,
                            color: canReject ? 'white' : 'var(--fg-muted)',
                            cursor: canReject ? 'pointer' : 'not-allowed'
                        }}>
                    {rejecting ? 'Rejecting…' : 'Confirm rejection'}
                </button>
            </div>
        </div>
    );
};

Object.assign(window, {
    NewCaptureForm,
    OCRCorrectionForm,
    CertifySubsetModal,
    RejectComponentForm,
    FormLabel,
    FieldInput
});
