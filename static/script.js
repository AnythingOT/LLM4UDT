document.addEventListener('DOMContentLoaded', () => {

    // ── DOM refs ──────────────────────────────────────────────────────────────
    const body             = document.body;
    const toggleThemeBtn   = document.getElementById('toggleTheme');
    const settingsButton   = document.getElementById('settingsButton');
    const settingsModal    = document.getElementById('settingsModal');
    const closeBtn         = settingsModal.querySelector('.close-button');
    const modelSelect      = document.getElementById('modelSelect');
    const refreshModelsBtn = document.getElementById('refreshModelsButton');
    const saveModelBtn     = document.getElementById('saveModelButton');
    const modelStatus      = document.getElementById('modelStatus');
    const chatWindow       = document.getElementById('chatWindow');
    const inputBox         = document.getElementById('inputBox');
    const enterButton      = document.getElementById('enterButton');
    const attachButton     = document.getElementById('attachButton');
    const fileInput        = document.getElementById('fileInput');
    const warningBanner    = document.getElementById('modelWarningBanner');

    // ── App state machine ─────────────────────────────────────────────────────
    // 'idle'               → chat input active, attach button active
    // 'awaiting_decision'  → file analysed, only action buttons active, input disabled
    // 'manual_command'     → user chose manual mode, input re-enabled with context
    let appState     = 'idle';
    let attachedFile = null;

    function setState(newState) {
        appState = newState;
        const isIdle   = newState === 'idle';
        const isManual = newState === 'manual_command';
        inputBox.disabled    = !(isIdle || isManual);
        attachButton.disabled = !isIdle;
        enterButton.disabled = inputBox.value.trim() === '' || inputBox.disabled;
        if (inputBox.disabled) {
            inputBox.placeholder = 'Complete or dismiss the current analysis first.';
        } else if (isManual) {
            inputBox.placeholder = 'Type your manual command…';
        } else {
            inputBox.placeholder = 'Describe your UDT… e.g. Create a UDT named MotorStatus with: run_status, BOOL, Motor running';
        }
    }

    // ── Theme ─────────────────────────────────────────────────────────────────
    body.classList.add(localStorage.getItem('theme') || 'light-mode');
    toggleThemeBtn.addEventListener('click', () => {
        const t = body.classList.contains('dark-mode') ? 'light-mode' : 'dark-mode';
        body.classList.remove('light-mode', 'dark-mode');
        body.classList.add(t);
        localStorage.setItem('theme', t);
    });

    // ── Model warning banner ──────────────────────────────────────────────────
    async function checkActiveModel() {
        try {
            const d = await (await fetch('/active_model')).json();
            warningBanner.style.display = (!d.is_set || !d.model) ? 'block' : 'none';
        } catch (_) { warningBanner.style.display = 'block'; }
    }
    checkActiveModel();

    // ── Settings modal ────────────────────────────────────────────────────────
    function setModelStatus(msg, type = '') {
        modelStatus.textContent = msg;
        modelStatus.className   = 'modal-status-message' + (type ? ' ' + type : '');
    }
    async function loadOllamaModels() {
        setModelStatus('Fetching models…', 'info');
        refreshModelsBtn.disabled = true;
        try {
            const d      = await (await fetch('/ollama_models')).json();
            const models = d.models || [];
            modelSelect.innerHTML = '';
            if (!models.length) {
                modelSelect.innerHTML = '<option value="">No models found</option>';
                setModelStatus('No Ollama models detected. Is Ollama running?', 'error');
            } else {
                models.forEach(m => {
                    const o = document.createElement('option');
                    o.value = m; o.textContent = m;
                    if (m === d.active_model) o.selected = true;
                    modelSelect.appendChild(o);
                });
                setModelStatus(`${models.length} model(s) found.`, 'success');
            }
        } catch (e) {
            modelSelect.innerHTML = '<option value="">Could not reach Ollama</option>';
            setModelStatus('Error: ' + e.message, 'error');
        } finally { refreshModelsBtn.disabled = false; }
    }
    settingsButton.addEventListener('click', () => { settingsModal.classList.add('show'); loadOllamaModels(); });
    closeBtn.addEventListener('click', () => settingsModal.classList.remove('show'));
    settingsModal.addEventListener('click', e => { if (e.target === settingsModal) settingsModal.classList.remove('show'); });
    refreshModelsBtn.addEventListener('click', loadOllamaModels);
    saveModelBtn.addEventListener('click', async () => {
        const sel = modelSelect.value;
        if (!sel) return;
        try {
            const d = await (await fetch('/set_model', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({model: sel})
            })).json();
            if (d.success) {
                setModelStatus(`Model set to "${sel}".`, 'success');
                warningBanner.style.display = 'none';
                setTimeout(() => settingsModal.classList.remove('show'), 1200);
            } else { setModelStatus('Failed to save.', 'error'); }
        } catch (e) { setModelStatus('Error: ' + e.message, 'error'); }
    });

    // ── Detail modal (click-to-expand) ────────────────────────────────────────
    const detailModal    = document.getElementById('detailModal');
    const detailModalBody = document.getElementById('detailModalBody');
    const detailModalClose = document.getElementById('detailModalClose');

    function openDetailModal(contentEl) {
        detailModalBody.innerHTML = '';
        detailModalBody.appendChild(contentEl);
        detailModal.classList.add('show');
    }
    function closeDetailModal() { detailModal.classList.remove('show'); }
    detailModalClose.addEventListener('click', closeDetailModal);
    detailModal.addEventListener('click', e => { if (e.target === detailModal) closeDetailModal(); });

    // ── Size helpers ──────────────────────────────────────────────────────────
    function bytesLabel(b) {
        if (b === null || b === undefined) return '';
        if (b < 1024) return b + ' B';
        return (b / 1024).toFixed(1) + ' KB';
    }

    function estimateUDTBytes(members) {
        // Rough in-memory size estimate for a UDT: sum of member type sizes
        const sizes = {BIT:0.125, BOOL:1, SINT:1, INT:2, DINT:4, REAL:4, STRING:82, TIMER:12, COUNTER:12};
        let total = 0;
        members.forEach(m => {
            const base = sizes[m.type] || 4;
            const dim  = m.dimension > 0 ? m.dimension : 1;
            total += base * dim;
        });
        // Round up to nearest byte
        return Math.ceil(total);
    }

    // ── Build analysis table for one UDT ─────────────────────────────────────
    function buildUDTDetail(analysis) {
        const wrap = document.createElement('div');

        // Issues
        const issueWrap = document.createElement('div');
        issueWrap.className = 'analysis-issues';
        if (analysis.issues && analysis.issues.length) {
            analysis.issues.forEach(issue => {
                const row = document.createElement('div');
                row.className = `analysis-issue ${issue.severity}`;
                const label = issue.type === 'sort_order'   ? 'Sort order'   :
                              issue.type === 'bool_packing' ? 'BOOL packing' : 'Naming';
                row.innerHTML = `<span class="issue-icon">${issue.severity === 'error' ? '✗' : '⚠'}</span>
                    <span class="issue-label">${label}</span>
                    <span class="issue-detail">${issue.detail}</span>`;
                issueWrap.appendChild(row);
            });
        } else {
            issueWrap.innerHTML = '<div class="analysis-ok">✓ No issues — already optimized.</div>';
        }
        wrap.appendChild(issueWrap);

        // Member table
        const table = document.createElement('table');
        table.className = 'analysis-table';
        table.innerHTML = `<thead><tr><th>#</th><th>Name</th><th>Type</th><th>Category</th><th>Status</th></tr></thead>`;
        const tbody = document.createElement('tbody');
        (analysis.members || []).forEach((m, i) => {
            const tr   = document.createElement('tr');
            tr.className = m.out_of_order ? 'row-warn' : '';
            const dim  = m.dimension > 0 ? `[${m.dimension}]` : '';
            const badge = m.out_of_order
                ? `<span class="badge-warn">Out of order (expected #${m.expected_position + 1})</span>`
                : `<span class="badge-ok">OK</span>`;
            tr.innerHTML = `<td>${i + 1}</td><td>${m.name}</td><td>${m.type}${dim}</td><td>${m.category}</td><td>${badge}</td>`;
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        return wrap;
    }

    // ── Build single-UDT summary card (compact, clickable) ───────────────────
    function buildSingleUDTCard(analysis, confirmationData) {
        const card = document.createElement('div');
        card.className = 'analysis-card clickable-card';

        const n_issues = (analysis.issues || []).length;
        const status   = n_issues === 0 ? '✓ Already optimized' : `${n_issues} issue(s) found`;
        const estBytes = estimateUDTBytes(analysis.members || []);
        card.innerHTML = `
            <div class="analysis-header">
                <span class="analysis-title">${analysis.udt_name}</span>
                <span class="analysis-count">${analysis.member_count} members · ~${bytesLabel(estBytes)} · ${status} — click for details</span>
            </div>`;

        card.addEventListener('click', () => {
            const detail = document.createElement('div');
            const h = document.createElement('h3');
            h.style.cssText = 'margin:0 0 4px; font-size:14px; font-weight:500;';
            h.textContent   = `UDT: ${analysis.udt_name}`;
            const sizeEl = document.createElement('p');
            sizeEl.style.cssText = 'margin:0 0 12px; font-size:11px; color:var(--modal-button-text); opacity:.75;';
            const estBytes = estimateUDTBytes(analysis.members || []);
            sizeEl.innerHTML = `${analysis.member_count} members · est. <strong>${bytesLabel(estBytes)}</strong> in-memory`;
            detail.appendChild(h);
            detail.appendChild(sizeEl);
            detail.appendChild(buildUDTDetail(analysis));
            openDetailModal(detail);
        });

        return card;
    }

    // ── Build full-program accordion card (clickable per-UDT rows) ───────────
    function buildProgramCard(confirmationData) {
        const card = document.createElement('div');
        card.className = 'analysis-card';

        const { analyses, processing_order, cyclic, udt_count, needs_opt_count } = confirmationData;

        // Summary header
        const hdr = document.createElement('div');
        hdr.className = 'analysis-header';
        hdr.innerHTML = `<span class="analysis-title">Program: ${confirmationData.controller_name}</span>
            <span class="analysis-count">click a row for details</span>`;
        card.appendChild(hdr);

        // Compact stat row
        const noAction = udt_count - needs_opt_count;
        const statRow  = document.createElement('div');
        statRow.className = 'program-summary';
        statRow.innerHTML = `
            <div class="program-summary-stat">
                <span class="program-summary-num">${udt_count}</span>
                <span class="program-summary-label">UDTs found</span>
            </div>
            <div class="program-summary-stat">
                <span class="program-summary-num num-warn">${needs_opt_count}</span>
                <span class="program-summary-label">Need optimization</span>
            </div>
            <div class="program-summary-stat">
                <span class="program-summary-num num-ok">${noAction}</span>
                <span class="program-summary-label">No action needed</span>
            </div>`;
        card.appendChild(statRow);

        if (cyclic && cyclic.length) {
            const warn = document.createElement('div');
            warn.className = 'analysis-issue warning';
            warn.style.padding = '8px 12px';
            warn.innerHTML = `<span class="issue-icon">⚠</span><span class="issue-label">Circular refs</span>
                <span class="issue-detail">Skipped: ${cyclic.join(', ')}</span>`;
            card.appendChild(warn);
        }

        // One row per UDT in processing order
        const list = document.createElement('div');
        list.className = 'udt-list';
        processing_order.forEach((name, idx) => {
            const a       = analyses[name];
            if (!a) return;
            const n_iss   = (a.issues || []).length;
            const row     = document.createElement('div');
            row.className = 'udt-list-row clickable-card';
            const rowBytes = estimateUDTBytes(a.members || []);
            row.innerHTML = `
                <span class="udt-list-index">${idx + 1}</span>
                <span class="udt-list-name">${name}</span>
                <span class="udt-list-members">${a.member_count} members</span>
                <span class="udt-list-size">~${bytesLabel(rowBytes)}</span>
                <span class="udt-list-badge ${n_iss === 0 ? 'badge-ok' : 'badge-warn'}">
                    ${n_iss === 0 ? '✓ OK' : `${n_iss} issue(s)`}
                </span>`;
            row.addEventListener('click', () => {
                const detail = document.createElement('div');
                const h = document.createElement('h3');
                h.style.cssText = 'margin:0 0 4px; font-size:14px; font-weight:500;';
                h.textContent   = `UDT: ${name}`;
                const sub = document.createElement('p');
                sub.style.cssText = 'margin:0 0 12px; font-size:11px; color:var(--modal-button-text); opacity:.75;';
                const dBytes = estimateUDTBytes(a.members || []);
                sub.innerHTML = `Position ${idx + 1} of ${processing_order.length} · ${a.member_count} members · est. <strong>${bytesLabel(dBytes)}</strong> in-memory`;
                detail.appendChild(h);
                detail.appendChild(sub);
                detail.appendChild(buildUDTDetail(a));
                openDetailModal(detail);
            });
            list.appendChild(row);
        });
        card.appendChild(list);
        return card;
    }

    // ── addMessage ────────────────────────────────────────────────────────────
    function addMessage(sender, content, type = 'text', duration = null,
                        requiresConfirmation = false, confirmationData = null) {
        const div = document.createElement('div');
        div.classList.add('chat-message', sender);

        if (type === 'loading') {
            const s = document.createElement('span');
            s.className = 'loading-text';
            s.innerHTML = `${content}<span class="loading-dot">.</span><span class="loading-dot">.</span><span class="loading-dot">.</span>`;
            div.appendChild(s);

        } else if (requiresConfirmation && confirmationData) {
            const p = document.createElement('p');
            p.style.margin = '0 0 10px';
            p.textContent  = content;
            div.appendChild(p);

            // Analysis card — single UDT or full program
            if (confirmationData.type === 'udt_attachment' && confirmationData.analysis) {
                div.appendChild(buildSingleUDTCard(confirmationData.analysis, confirmationData));
            } else if (confirmationData.type === 'program_attachment') {
                div.appendChild(buildProgramCard(confirmationData));
            }

            // Action buttons
            const btnRow = document.createElement('div');
            btnRow.className = 'confirmation-options';
            confirmationData.options.forEach(opt => {
                const btn = document.createElement('button');
                btn.textContent = opt.label;
                if (opt.action === 'manual_command') {
                    btn.className = 'action-button action-wip';
                    btn.disabled  = true;
                    btn.title     = 'Manual command — work in progress';
                } else {
                    btn.className = 'action-button';
                    btn.addEventListener('click', e => handleAction(e, opt.action, confirmationData, btnRow));
                }
                btnRow.appendChild(btn);
            });
            div.appendChild(btnRow);

        } else {
            const t = document.createElement('div');
            t.textContent = content;
            div.appendChild(t);
            if (duration !== null) {
                const ds = document.createElement('span');
                ds.className   = 'duration';
                ds.textContent = `⏱️ ${duration.toFixed(2)}s`;
                div.appendChild(ds);
            }
        }

        chatWindow.appendChild(div);
        // Scroll the page (not chatWindow which is overflow:visible)
        requestAnimationFrame(() => window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'}));
        return div;
    }

    // ── Handle action button clicks ───────────────────────────────────────────
    async function handleAction(event, action, confirmationData, btnRow) {
        // Disable all buttons
        Array.from(btnRow.children).forEach(b => {
            b.disabled = true; b.style.opacity = '0.55'; b.style.cursor = 'not-allowed';
        });

        if (action === 'manual_command') {
            // Blocked — WIP, button is disabled in UI
            return;
        }

        addMessage('user', `Action: ${event.target.textContent.replace(' (WIP)', '')}`);
        const loadDiv = addMessage('bot', 'Processing…', 'loading');

        try {
            const body = { action };
            if (action === 'optimize_program') {
                body.filename = confirmationData.filename;
            } else if (action === 'optimize') {
                body.udt_definition = confirmationData.udt_definition;
            }

            const res  = await fetch('/process_udt_attachment', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            const data = await res.json();
            if (chatWindow.contains(loadDiv)) chatWindow.removeChild(loadDiv);

            const { text, duration, download } = data.response;
            addMessage('bot', text, 'text', duration);
            if (download) downloadFile(download);

            // Return to idle after any completed action
            setState('idle');

        } catch (e) {
            if (chatWindow.contains(loadDiv)) chatWindow.removeChild(loadDiv);
            addMessage('bot', `Error: ${e.message}`);
            setState('idle');
        }
    }

    // ── File download ─────────────────────────────────────────────────────────
    function downloadFile(info) {
        if (!info) return;
        const name = info.file_name || 'download';
        const type = info.content_type || 'application/octet-stream';
        let blob;
        if (info.blob instanceof Blob) {
            blob = info.blob;
        } else if (info.encoding === 'hex' && info.file_content_b64) {
            const buf = new Uint8Array(info.file_content_b64.match(/.{1,2}/g).map(b => parseInt(b, 16)));
            blob = new Blob([buf], {type});
        } else if (info.file_content) {
            blob = new Blob([info.encoding === 'base64' ? atob(info.file_content) : info.file_content], {type});
        } else return;
        const url = URL.createObjectURL(blob);
        const a   = Object.assign(document.createElement('a'), {href: url, download: name});
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    // ── sendMessage (Option 1 NLP) ────────────────────────────────────────────
    async function sendMessage() {
        const msg = inputBox.value.trim();
        if (!msg && !attachedFile) return;
        if (appState === 'awaiting_decision') {
            addMessage('bot', 'Please use the Optimize or Manual Command button above first.');
            return;
        }

        addMessage('user', attachedFile ? `Attached: ${attachedFile.name}` : msg);
        inputBox.value = ''; inputBox.style.height = 'auto';
        enterButton.disabled = true;

        const loadDiv = addMessage('bot', 'Processing…', 'loading');

        try {
            let res;
            if (attachedFile) {
                // Block second attach if awaiting decision
                if (appState === 'awaiting_decision') {
                    if (chatWindow.contains(loadDiv)) chatWindow.removeChild(loadDiv);
                    addMessage('bot', 'Please complete or dismiss the current analysis before attaching another file.');
                    attachedFile = null; fileInput.value = '';
                    return;
                }
                const fd = new FormData();
                fd.append('message', msg); fd.append('file', attachedFile);
                res = await fetch('/attach', {method: 'POST', body: fd});
                const data = await res.json();
                if (chatWindow.contains(loadDiv)) chatWindow.removeChild(loadDiv);
                if (!res.ok) throw new Error(data.response?.text || 'Server error.');
                const { text, duration, download, requires_confirmation, confirmation_data } = data.response;
                if (requires_confirmation) {
                    setState('awaiting_decision');
                }
                addMessage('bot', text, 'text', duration, requires_confirmation, confirmation_data);
                if (download && !requires_confirmation) downloadFile(download);
            } else {
                res = await fetch('/chat', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg})
                });
                if (chatWindow.contains(loadDiv)) chatWindow.removeChild(loadDiv);
                if (!res.ok) throw new Error((await res.json()).response?.text || 'Server error.');
                const data = await res.json();
                const { text, duration, download } = data.response;
                addMessage('bot', text, 'text', duration);
                if (download) downloadFile(download);
            }
        } catch (e) {
            if (chatWindow.contains(loadDiv)) chatWindow.removeChild(loadDiv);
            addMessage('bot', `Error: ${e.message || 'Could not reach server.'}`);
            setState('idle');
        } finally {
            attachedFile = null; fileInput.value = '';
            enterButton.disabled = inputBox.value.trim() === '' || inputBox.disabled;
        }
    }

    // ── Input events ──────────────────────────────────────────────────────────
    inputBox.addEventListener('input', () => {
        if (!inputBox.disabled) {
            enterButton.disabled  = inputBox.value.trim() === '';
            inputBox.style.height = 'auto';
            inputBox.style.height = inputBox.scrollHeight + 'px';
        }
    });
    enterButton.addEventListener('click', sendMessage);
    inputBox.addEventListener('keypress', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!enterButton.disabled) sendMessage();
        }
    });
    attachButton.addEventListener('click', () => {
        if (appState === 'awaiting_decision') {
            addMessage('bot', 'Please complete or dismiss the current analysis before attaching another file.');
            return;
        }
        fileInput.click();
    });
    fileInput.addEventListener('change', () => {
        attachedFile = fileInput.files[0] || null;
        if (attachedFile) sendMessage();
        else if (appState === 'idle') enterButton.disabled = inputBox.value.trim() === '';
    });

    // Initialise state
    setState('idle');
});
