// Job Search UI. Plain JS, no build step.

const state = {
    status: '',          // '' = all statuses
    searchId: null,      // filter jobs to one saved search
    companyId: null,     // ...or to one company (only one of the two at a time)
    statuses: [],
    searches: [],
    companies: [],
    atsLabels: {},
    openJobId: null,
};

const STATUS_LABELS = {
    new: 'New',
    interested: 'Interested',
    applied: 'Applied',
    interviewing: 'Interviewing',
    offer: 'Offer',
    rejected: 'Rejected',
    not_a_fit: 'Not a fit',
};

// Escape text for safe insertion into HTML.
function esc(text) {
    if (text === null || text === undefined) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// Only allow http(s) URLs into href attributes.
function safeUrl(url) {
    if (!url) return '';
    const s = String(url).trim();
    if (/^https?:\/\//i.test(s)) return esc(s);
    return '';
}

async function api(path, options = {}) {
    const opts = { ...options };
    if (opts.body && typeof opts.body !== 'string') {
        opts.body = JSON.stringify(opts.body);
        opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    }
    const response = await fetch(path, opts);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
}

function showMessage(text, isError = false) {
    const el = document.getElementById('message');
    el.textContent = text;
    el.classList.toggle('error', isError);
    el.classList.remove('hidden');
    clearTimeout(showMessage.timer);
    showMessage.timer = setTimeout(() => el.classList.add('hidden'), isError ? 10000 : 6000);
}

function relativeDate(iso) {
    if (!iso) return '';
    const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return '1 day ago';
    if (days < 30) return `${days} days ago`;
    return new Date(iso).toLocaleDateString();
}

// Pay from the parsed numbers when there are any; otherwise the site's own wording.
function salaryText(job) {
    if (!job.salary_min && !job.salary_max) return job.salary_text || '';
    const hourly = job.salary_period === 'HOUR';
    const fmt = n => hourly || n < 1000 ? `$${n.toFixed(2)}` : `$${(n / 1000).toFixed(n % 1000 ? 1 : 0)}k`;
    const range = job.salary_min && job.salary_max && job.salary_min !== job.salary_max
        ? `${fmt(job.salary_min)}–${fmt(job.salary_max)}`
        : fmt(job.salary_min || job.salary_max);
    const period = { YEAR: '/yr', HOUR: '/hr', MONTH: '/mo' }[job.salary_period] || '';
    return range + period;
}

function shortDate(iso) {
    return iso ? new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '';
}

function closesText(iso) {
    if (!iso) return '';
    const days = Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000);
    if (days < 0) return `closed ${shortDate(iso)}`;
    if (days === 0) return 'closes today';
    return days <= 14 ? `closes in ${days} day${days === 1 ? '' : 's'}` : `closes ${shortDate(iso)}`;
}

// One-click flags on the job card. Clicking an active flag clears it back to "interested".
async function toggleFlag(event, jobId, status, current) {
    event.stopPropagation();
    await updateJob(jobId, { status: current === status ? 'interested' : status });
}

// --- meta / usage ------------------------------------------------------------

async function loadMeta() {
    const meta = await api('/api/meta');
    state.statuses = meta.statuses;
    state.atsLabels = meta.ats_labels;
    state.atsHints = meta.ats_token_hints;
    state.priorities = meta.priorities;
    state.companyDefaults = meta.company_defaults;
    renderStatusTabs(meta.status_counts);
    renderUsage(meta.pages_used_this_month);
    if (!meta.has_api_key) showMessage('No JSEARCH_API_KEY in .env, so searches will fail until it is added.', true);
}

function renderUsage(pages) {
    document.getElementById('usage').textContent = `${pages} API page${pages === 1 ? '' : 's'} used this month`;
}

function renderStatusTabs(counts) {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const tabs = [{ key: '', label: 'All', n: total }]
        .concat(state.statuses.map(s => ({ key: s, label: STATUS_LABELS[s] || s, n: counts[s] || 0 })));
    document.getElementById('statusTabs').innerHTML = tabs.map(t => `
        <span class="status-tab ${t.key === state.status ? 'active' : ''}" onclick="setStatusFilter('${t.key}')">
            ${esc(t.label)} (${t.n})
        </span>`).join('');
}

function setStatusFilter(status) {
    state.status = status;
    loadMeta();
    loadJobs();
}

// --- saved searches ------------------------------------------------------------

async function loadSearches() {
    state.searches = await api('/api/searches');
    const list = document.getElementById('savedSearches');
    if (state.searches.length === 0) {
        list.innerHTML = '<li class="empty">No saved searches yet.</li>';
        return;
    }
    list.innerHTML = state.searches.map(s => {
        const bits = [s.location, s.remote_only ? 'remote' : '', s.date_posted !== 'all' ? `posted: ${s.date_posted}` : '', s.employment_types]
            .filter(Boolean).join(' · ');
        return `
        <li class="${s.id === state.searchId ? 'active' : ''}" onclick="filterBySearch(${s.id})">
            <div class="saved-name">${esc(s.name)}</div>
            <div class="saved-meta">${esc(bits || 'any location')}<br>
                ${s.job_count} job${s.job_count === 1 ? '' : 's'} · ${s.last_run_at ? 'last run ' + esc(relativeDate(s.last_run_at)) : 'never run'}</div>
            <div class="saved-actions">
                <button class="small" onclick="event.stopPropagation(); runSearch(${s.id}, this)">Run</button>
                <button class="small danger" onclick="event.stopPropagation(); deleteSearch(${s.id})">Delete</button>
            </div>
        </li>`;
    }).join('');
}

function formParams() {
    return {
        query: document.getElementById('fQuery').value.trim(),
        location: document.getElementById('fLocation').value.trim(),
        remote_only: document.getElementById('fRemote').checked,
        date_posted: document.getElementById('fDatePosted').value,
        employment_types: document.getElementById('fType').value || null,
        num_pages: parseInt(document.getElementById('fPages').value, 10),
        name: document.getElementById('fName').value.trim(),
    };
}

function reportRun(result) {
    renderUsage(result.pages_used_this_month);
    showMessage(`Found ${result.found} jobs: ${result.new} new, ${result.already_saved} already saved.`);
}

async function withBusy(button, fn) {
    const buttons = document.querySelectorAll('button');
    buttons.forEach(b => b.disabled = true);
    const original = button ? button.textContent : '';
    if (button) button.textContent = 'Searching...';
    try {
        await fn();
    } catch (e) {
        showMessage(e.message, true);
    } finally {
        buttons.forEach(b => b.disabled = false);
        if (button) button.textContent = original;
    }
}

async function saveAndRun(button) {
    const params = formParams();
    if (!params.query) return showMessage('Enter something to search for.', true);
    await withBusy(button, async () => {
        const search = await api('/api/searches', { method: 'POST', body: params });
        const result = await api(`/api/searches/${search.id}/run`, { method: 'POST' });
        state.searchId = search.id;
        state.companyId = null;
        document.getElementById('fName').value = '';
        reportRun(result);
        await refreshAll();
    });
}

async function quickSearch(button) {
    const params = formParams();
    if (!params.query) return showMessage('Enter something to search for.', true);
    await withBusy(button, async () => {
        const result = await api('/api/search', { method: 'POST', body: params });
        state.searchId = null;
        state.companyId = null;
        state.status = 'new';
        reportRun(result);
        await refreshAll();
    });
}

async function runSearch(searchId, button) {
    await withBusy(button, async () => {
        const result = await api(`/api/searches/${searchId}/run`, { method: 'POST' });
        state.searchId = searchId;
        state.companyId = null;
        reportRun(result);
        await refreshAll();
    });
}

async function deleteSearch(searchId) {
    const search = state.searches.find(s => s.id === searchId);
    if (!confirm(`Delete saved search "${search ? search.name : ''}"? Jobs it found stay in your list.`)) return;
    await api(`/api/searches/${searchId}`, { method: 'DELETE' });
    if (state.searchId === searchId) state.searchId = null;
    await refreshAll();
}

function filterBySearch(searchId) {
    state.searchId = state.searchId === searchId ? null : searchId;
    state.companyId = null;
    loadSearches();
    loadCompanies();
    loadJobs();
}

// --- companies ---------------------------------------------------------------

async function loadCompanies() {
    state.companies = await api('/api/companies');
    const list = document.getElementById('companyList');
    if (state.companies.length === 0) {
        list.innerHTML = '<li class="empty">No companies yet. Click + Add.</li>';
        return;
    }
    list.innerHTML = state.companies.map(c => {
        const bits = [
            c.priority ? (state.priorities[c.priority] || `P${c.priority}`) : '',
            c.rating ? `★ ${c.rating}` : '',
            c.keywords || 'all openings',
        ].filter(Boolean).join(' · ');
        return `
        <li class="${c.id === state.companyId ? 'active' : ''}" onclick="filterByCompany(${c.id})" title="${esc(c.notes || '')}">
            <div class="saved-name">${esc(c.name)}<span class="ats-tag ${c.ats === 'search' ? 'quota' : ''}">${esc(state.atsLabels[c.ats] || c.ats)}</span></div>
            <div class="saved-meta">${esc(bits)}<br>
                ${c.job_count} job${c.job_count === 1 ? '' : 's'} · ${c.last_checked_at ? 'checked ' + esc(relativeDate(c.last_checked_at)) : 'never checked'}</div>
            <div class="saved-actions">
                <button class="small" onclick="event.stopPropagation(); checkCompany(${c.id}, this)">Check</button>
                <button class="small secondary" onclick="event.stopPropagation(); editCompany(${c.id})">Edit</button>
                <button class="small danger" onclick="event.stopPropagation(); deleteCompany(${c.id})">Delete</button>
            </div>
        </li>`;
    }).join('');
}

function filterByCompany(companyId) {
    state.companyId = state.companyId === companyId ? null : companyId;
    state.searchId = null;
    loadSearches();
    loadCompanies();
    loadJobs();
}

function showCompanyForm(company = null) {
    const defaults = state.companyDefaults || {};
    const ats = document.getElementById('cAts');
    ats.innerHTML = '<option value="">Auto-detect from URL</option>' + Object.entries(state.atsLabels)
        .map(([key, label]) => `<option value="${esc(key)}">${esc(label)}${key === 'search' ? ' (uses API quota)' : ''}</option>`).join('');
    const priority = document.getElementById('cPriority');
    priority.innerHTML = '<option value="">—</option>' + Object.entries(state.priorities)
        .map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`).join('');

    document.getElementById('cId').value = company ? company.id : '';
    document.getElementById('cName').value = company ? company.name : '';
    document.getElementById('cUrl').value = company ? (company.careers_url || '') : '';
    ats.value = company ? company.ats : '';
    document.getElementById('cToken').value = company ? (company.ats_token || '') : '';
    priority.value = company && company.priority ? company.priority : '';
    document.getElementById('cRating').value = company && company.rating ? company.rating : '';
    document.getElementById('cKeywords').value = company ? (company.keywords || '') : (defaults.keywords || '');
    document.getElementById('cLocations').value = company ? (company.locations || '') : (defaults.locations || '');
    document.getElementById('cNotes').value = company ? (company.notes || '') : '';
    updateTokenHint();
    document.getElementById('cSaveBtn').textContent = company ? 'Save changes' : 'Add company';
    document.getElementById('companyForm').classList.remove('hidden');
    document.getElementById('cName').focus();
}

function updateTokenHint() {
    const ats = document.getElementById('cAts').value;
    const token = document.getElementById('cToken');
    token.disabled = !ats || ats === 'search' || ats === 'amazon';
    if (token.disabled && !ats) token.value = '';
    document.getElementById('cTokenHint').textContent = ats
        ? `Board ID: ${state.atsHints[ats] || ''}`
        : 'Job board and board ID are filled in from the URL when you save.';
}

function hideCompanyForm() {
    document.getElementById('companyForm').classList.add('hidden');
}

function editCompany(companyId) {
    showCompanyForm(state.companies.find(c => c.id === companyId));
}

async function saveCompany(button) {
    const id = document.getElementById('cId').value;
    const body = {
        name: document.getElementById('cName').value.trim(),
        careers_url: document.getElementById('cUrl').value.trim(),
        keywords: document.getElementById('cKeywords').value.trim(),
        locations: document.getElementById('cLocations').value.trim(),
        notes: document.getElementById('cNotes').value.trim(),
        priority: document.getElementById('cPriority').value,
        rating: document.getElementById('cRating').value,
        // Blank job board = detect it from the URL on the server.
        ats: document.getElementById('cAts').value,
        ats_token: document.getElementById('cToken').value.trim(),
    };
    if (!body.name) return showMessage('Enter a company name.', true);
    button.disabled = true;
    try {
        const company = await api(id ? `/api/companies/${id}` : '/api/companies', {
            method: id ? 'PATCH' : 'POST', body,
        });
        hideCompanyForm();
        showMessage(`${company.name} saved (reads openings from ${state.atsLabels[company.ats] || company.ats}).`);
        await loadCompanies();
    } catch (e) {
        showMessage(e.message, true);
    } finally {
        button.disabled = false;
    }
}

async function deleteCompany(companyId) {
    const company = state.companies.find(c => c.id === companyId);
    if (!confirm(`Remove ${company ? company.name : 'this company'}? Jobs already saved stay in your list.`)) return;
    await api(`/api/companies/${companyId}`, { method: 'DELETE' });
    if (state.companyId === companyId) state.companyId = null;
    await refreshAll();
}

async function checkCompany(companyId, button) {
    await withBusy(button, async () => {
        const result = await api(`/api/companies/${companyId}/check`, { method: 'POST' });
        state.companyId = companyId;
        state.searchId = null;
        reportRun(result);
        await refreshAll();
    });
}

async function checkAllCompanies(button) {
    if (state.companies.length === 0) return showMessage('Add a company first.', true);
    // Direct career-site checks are free; JSearch ones cost a request each, so they're opt-in.
    const quotaCompanies = state.companies.filter(c => c.ats === 'search');
    const includeQuota = quotaCompanies.length > 0 && confirm(
        `${state.companies.length - quotaCompanies.length} companies are checked directly on their career sites (free).\n\n` +
        `${quotaCompanies.length} more can only be checked through JSearch, about 1 API request each.\n\n` +
        `OK = check all of them   ·   Cancel = only the free ones`);
    const toCheck = state.companies.filter(c => c.ats !== 'search' || includeQuota);
    await withBusy(button, async () => {
        let found = 0, added = 0, failed = [];
        for (const c of toCheck) {
            button.textContent = `Checking ${c.name}...`;
            try {
                const r = await api(`/api/companies/${c.id}/check`, { method: 'POST' });
                found += r.found;
                added += r.new;
                renderUsage(r.pages_used_this_month);
            } catch (e) {
                failed.push(`${c.name} (${e.message})`);
            }
        }
        state.companyId = null;
        state.searchId = null;
        state.status = 'new';
        await refreshAll();
        showMessage(`Checked ${toCheck.length} companies: ${found} matching jobs, ${added} new.` +
            (failed.length ? ` Failed: ${failed.join('; ')}` : ''), failed.length > 0);
    });
}

// --- jobs ------------------------------------------------------------------

let loadTimer = null;
function debouncedLoadJobs() {
    clearTimeout(loadTimer);
    loadTimer = setTimeout(loadJobs, 250);
}

async function loadJobs() {
    const params = new URLSearchParams();
    if (state.status) params.append('status', state.status);
    if (state.searchId) params.append('search_id', state.searchId);
    if (state.companyId) params.append('company_id', state.companyId);
    const text = document.getElementById('textFilter').value.trim();
    if (text) params.append('q', text);
    if (document.getElementById('remoteFilter').checked) params.append('remote', '1');
    if (document.getElementById('hideNotFit').checked) params.append('hide_not_fit', '1');
    params.append('sort', document.getElementById('sortJobs').value);

    const chip = document.getElementById('searchFilterChip');
    const search = state.searches.find(s => s.id === state.searchId);
    const company = state.companies.find(c => c.id === state.companyId);
    if (search) {
        chip.textContent = `Search: ${search.name} ✕`;
        chip.onclick = () => filterBySearch(search.id);
        chip.classList.remove('hidden');
    } else if (company) {
        chip.textContent = `Company: ${company.name} ✕`;
        chip.onclick = () => filterByCompany(company.id);
        chip.classList.remove('hidden');
    } else {
        chip.classList.add('hidden');
    }

    const jobs = await api(`/api/jobs?${params}`);
    const list = document.getElementById('jobList');
    if (jobs.length === 0) {
        list.innerHTML = '<div class="panel empty">No jobs match. Run a search to add some.</div>';
        return;
    }
    list.innerHTML = jobs.map(renderJobSummary).join('');
    if (state.openJobId && jobs.some(j => j.id === state.openJobId)) openJob(state.openJobId, false);
}

function renderJobSummary(job) {
    const salary = salaryText(job);
    const closes = closesText(job.closes_at);
    const applied = job.status === 'applied', notFit = job.status === 'not_a_fit';
    return `
    <div class="job status-${esc(job.status)}" id="job-${job.id}">
        <div class="job-summary" onclick="toggleJob(${job.id})">
            <div>
                <div class="job-title">${esc(job.title)}</div>
                <div class="job-sub">${esc(job.company || 'Unknown company')} · ${esc(job.location || 'Location not listed')}</div>
                ${job.is_remote ? '<span class="badge remote">Remote</span> ' : ''}
                ${job.employment_type ? `<span class="badge">${esc(job.employment_type)}</span> ` : ''}
                ${job.req_number ? `<span class="badge req" title="Requisition / posting number">Req ${esc(job.req_number)}</span> ` : ''}
                <span class="badge">${esc(STATUS_LABELS[job.status] || job.status)}${applied && job.applied_at ? ' ' + esc(shortDate(job.applied_at)) : ''}</span>
                <div class="flag-buttons">
                    <button class="flag ${applied ? 'on applied' : ''}" onclick="toggleFlag(event, ${job.id}, 'applied', '${esc(job.status)}')">✓ Applied</button>
                    <button class="flag ${notFit ? 'on not-fit' : ''}" onclick="toggleFlag(event, ${job.id}, 'not_a_fit', '${esc(job.status)}')">✕ Not a fit</button>
                </div>
            </div>
            <div class="job-side">
                ${salary ? `<div class="salary" title="${esc(job.salary_text || '')}">${esc(salary)}</div>` : '<div class="no-pay">pay not listed</div>'}
                <div>${job.posted_at ? 'posted ' + esc(shortDate(job.posted_at)) : ''}</div>
                ${closes ? `<div class="${closes.startsWith('closed') ? 'closed' : 'closes'}">${esc(closes)}</div>` : ''}
                <div>${esc(job.publisher || '')}</div>
            </div>
        </div>
        <div class="job-detail hidden" id="detail-${job.id}"></div>
    </div>`;
}

function toggleJob(jobId) {
    const detail = document.getElementById(`detail-${jobId}`);
    if (!detail.classList.contains('hidden')) {
        detail.classList.add('hidden');
        state.openJobId = null;
        return;
    }
    openJob(jobId, true);
}

async function openJob(jobId, closeOthers) {
    if (closeOthers) document.querySelectorAll('.job-detail').forEach(d => d.classList.add('hidden'));
    state.openJobId = jobId;
    const detail = document.getElementById(`detail-${jobId}`);
    detail.classList.remove('hidden');
    detail.innerHTML = '<div class="empty">Loading...</div>';
    const job = await api(`/api/jobs/${jobId}`);
    const apply = safeUrl(job.apply_link);
    detail.innerHTML = `
        <div class="detail-controls">
            <label>Status
                <select onchange="updateJob(${job.id}, { status: this.value })">
                    ${state.statuses.map(s => `<option value="${s}" ${s === job.status ? 'selected' : ''}>${esc(STATUS_LABELS[s] || s)}</option>`).join('')}
                </select>
            </label>
            ${apply ? `<a class="apply" href="${apply}" target="_blank" rel="noopener noreferrer">Open listing ↗</a>` : ''}
        </div>
        <div class="notes-row">
            <label>Notes
                <textarea id="notes-${job.id}">${esc(job.notes || '')}</textarea>
            </label>
            <button class="small" onclick="updateJob(${job.id}, { notes: document.getElementById('notes-${job.id}').value })">Save notes</button>
        </div>
        <div class="description">${esc(job.description || 'No description provided.')}</div>
        <div class="detail-meta">
            ${job.req_number ? `Req # ${esc(job.req_number)} · ` : ''}
            ${job.posted_at ? `Posted ${esc(shortDate(job.posted_at))} · ` : ''}
            ${job.closes_at ? `${esc(closesText(job.closes_at))} · ` : ''}
            ${job.applied_at ? `Applied ${esc(shortDate(job.applied_at))} · ` : ''}
            ${job.salary_text ? `Pay as posted: “${esc(job.salary_text)}” · ` : ''}
            First saved ${esc(relativeDate(job.first_seen_at))} · last seen in results ${esc(relativeDate(job.last_seen_at))}
        </div>
    `;
}

async function updateJob(jobId, fields) {
    try {
        await api(`/api/jobs/${jobId}`, { method: 'PATCH', body: fields });
        if ('status' in fields) {
            await loadMeta();
            await loadJobs();
        } else {
            showMessage('Notes saved.');
        }
    } catch (e) {
        showMessage(e.message, true);
    }
}

// --- backup ------------------------------------------------------------------

async function backup() {
    const button = document.getElementById('backupBtn');
    button.disabled = true;
    button.textContent = 'Backing up...';
    try {
        const result = await api('/api/backup', { method: 'POST' });
        showMessage(result.committed ? 'Backed up to GitHub.' : 'No changes since the last backup; GitHub is up to date.');
    } catch (e) {
        showMessage(e.message, true);
    } finally {
        button.disabled = false;
        button.textContent = 'Back up';
    }
}

async function refreshAll() {
    await loadMeta();
    await loadSearches();
    await loadCompanies();
    await loadJobs();
}

refreshAll().catch(e => showMessage(e.message, true));
