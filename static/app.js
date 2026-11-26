// Job Search UI. Plain JS, no build step.

const state = {
    status: '',          // '' = all statuses
    searchId: null,      // filter jobs to one saved search
    companyId: null,     // ...or to one company (only one of the two at a time)
    selectedCompanies: new Set(),  // ticked in the company list, for "Check selected"
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

const WORK_MODE_LABELS = { remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site' };

// Right side of the job card, under the posted date: the work style tag (Remote / Hybrid / On-site /
// Unknown), then the job's location for anything that isn't remote. A "?" means the work style was
// read from the description rather than a field on the listing.
function workModeBlock(job) {
    const mode = job.work_mode || 'unknown';
    const guessed = job.work_mode && job.work_mode_source === 'description';
    const tip = !job.work_mode ? 'Not stated on the listing' : guessed ? 'Mentioned in the description; check the listing to confirm' : 'Listed on the posting';
    const label = WORK_MODE_LABELS[mode] || 'Unknown';
    return `<div class="work-mode ${esc(mode)}" title="${tip}">${esc(label)}${guessed ? '?' : ''}</div>` +
        (mode === 'remote' ? '' : `<div class="job-location">${esc(job.location || 'Location not listed')}</div>`);
}

// Other sites listing the same job (merged into this card): "Also on ZipRecruiter ↗, Indeed ↗".
function alsoFoundHtml(job) {
    let sources = [];
    try { sources = JSON.parse(job.also_found || '[]'); } catch (e) { /* ignore bad data */ }
    if (!sources.length) return '';
    const links = sources.map(s => {
        const url = safeUrl(s.link);
        const name = esc(s.publisher || 'another site');
        return url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${name} ↗</a>` : name;
    });
    return `Also on ${links.join(', ')} · `;
}

function shortDate(iso) {
    if (!iso) return '';
    // Plain dates ("2026-11-20", or midnight UTC as sites send posting dates) are calendar days,
    // so show that day rather than converting to local time (which would show the day before).
    const dateOnly = /^\d{4}-\d{2}-\d{2}(T00:00(:00(\.0+)?)?(Z|[+-]00:?00)?)?$/.test(iso);
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric',
                                                         ...(dateOnly ? { timeZone: 'UTC' } : {}) });
}

function closesText(iso) {
    if (!iso) return '';
    const days = Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000);
    if (days < 0) return `closed ${shortDate(iso)}`;
    if (days === 0) return 'closes today';
    return days <= 14 ? `closes in ${days} day${days === 1 ? '' : 's'}` : `closes ${shortDate(iso)}`;
}

const INTEREST_LABELS = { high: 'High', medium: 'Medium', low: 'Low' };

// High / Medium / Low for a job marked Interested or Applied. Clicking the current level clears it.
function interestChooser(job) {
    return `<div class="interest-chooser" onclick="event.stopPropagation()">
        <span>Interest:</span>
        ${Object.entries(INTEREST_LABELS).map(([key, label]) => `<button class="interest-opt ${key} ${job.interest_level === key ? 'on' : ''}"
            onclick="setInterest(event, ${job.id}, '${key}', '${esc(job.interest_level || '')}')">${label}</button>`).join('')}
    </div>`;
}

async function setInterest(event, jobId, level, current) {
    event.stopPropagation();
    await updateJob(jobId, { interest_level: current === level ? null : level });
}

// One-click flags on the job card (Interested / Applied / Not a fit). Clicking the active one clears it back to "new".
async function toggleFlag(event, jobId, status, current) {
    event.stopPropagation();
    await updateJob(jobId, { status: current === status ? 'new' : status });
}

// --- meta / usage ------------------------------------------------------------

async function loadMeta() {
    const meta = await api('/api/meta');
    state.statuses = meta.statuses;
    state.atsLabels = meta.ats_labels;
    state.atsHints = meta.ats_token_hints;
    state.priorities = meta.priorities;
    state.companyDefaults = meta.company_defaults;
    state.defaultKeywords = meta.default_keywords;
    const dk = document.getElementById('defaultKeywords');
    if (document.activeElement !== dk) dk.value = meta.default_keywords || '';
    state.area = meta.area;
    const home = document.getElementById('areaHome'), radius = document.getElementById('areaRadius');
    if (document.activeElement !== home) home.value = meta.area.home;
    if (document.activeElement !== radius) radius.value = meta.area.radius_miles;
    state.benefitScales = meta.benefit_scales;
    state.benefitTopicOrder = meta.benefit_topics.map(([key]) => key);
    state.benefitTopics = Object.fromEntries(meta.benefit_topics);
    state.aiName = meta.ai_name || 'ChatGPT';
    state.hasBenefitsAI = meta.has_benefits_ai;
    state.benefitsAI = meta.benefits_ai_name || 'ChatGPT';
    state.benefitsAIKey = meta.benefits_ai_key || 'OPENAI_API_KEY';
    state.aiLookups = meta.ai_lookups_this_month;
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
                <button class="small secondary" onclick="event.stopPropagation(); editSearch(${s.id})">Edit</button>
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

// After a Check or search, bring the results (message + job list) into view.
function scrollToResults() {
    document.querySelector('main').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function reportRun(result) {
    renderUsage(result.pages_used_this_month);
    const skipped = result.outside_area
        ? ` Skipped ${result.outside_area} more than ${state.area.radius_miles} miles from ${state.area.home} (or not really remote).` : '';
    showMessage(`Found ${result.found} jobs: ${result.new} new, ${result.already_saved} already saved.${skipped}`);
    scrollToResults();
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
        scrollToResults();
    } finally {
        buttons.forEach(b => b.disabled = false);
        if (button) button.textContent = original;
        updateCheckSelectedButton();  // stays disabled when nothing is ticked
    }
}

// --- collapsible sidebar cards ---------------------------------------------------------
// Open/closed is remembered in this browser only (a viewing preference).

function togglePanel(id, open) {
    const panel = document.getElementById(id);
    const collapse = open === undefined ? !panel.classList.contains('collapsed') : !open;
    panel.classList.toggle('collapsed', collapse);
    try { localStorage.setItem(`collapsed:${id}`, collapse ? '1' : ''); } catch (e) { /* private window */ }
}

function restorePanels() {
    for (const id of ['jsearchPanel', 'companiesPanel', 'profilePanel', 'skillsPanel']) {
        let collapsed = false;
        try { collapsed = localStorage.getItem(`collapsed:${id}`) === '1'; } catch (e) { /* ignore */ }
        document.getElementById(id).classList.toggle('collapsed', collapsed);
    }
}

// Editing a saved search reuses the search form: fill it in and swap the buttons.
function editSearch(searchId) {
    togglePanel('jsearchPanel', true);
    const s = state.searches.find(x => x.id === searchId);
    if (!s) return;
    document.getElementById('fEditId').value = s.id;
    document.getElementById('fQuery').value = s.query || '';
    document.getElementById('fLocation').value = s.location || '';
    document.getElementById('fRemote').checked = !!s.remote_only;
    document.getElementById('fDatePosted').value = s.date_posted || 'all';
    document.getElementById('fType').value = s.employment_types || '';
    document.getElementById('fPages').value = String(s.num_pages || 1);
    document.getElementById('fName').value = s.name || '';
    setSearchFormMode(true, s.name);
    const form = document.getElementById('searchForm').closest('.panel');
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    document.getElementById('fQuery').focus();
}

function setSearchFormMode(editing, name = '') {
    document.getElementById('searchFormTitle').textContent = editing ? `Edit saved search: ${name}` : 'New JSearch';
    document.getElementById('fNameLabel').textContent = editing ? 'Name' : 'Save as (optional)';
    document.getElementById('newSearchButtons').classList.toggle('hidden', editing);
    document.getElementById('editSearchButtons').classList.toggle('hidden', !editing);
    document.getElementById('searchForm').closest('.panel').classList.toggle('editing', editing);
}

function cancelSearchEdit() {
    document.getElementById('fEditId').value = '';
    document.getElementById('searchForm').reset();
    setSearchFormMode(false);
}

async function saveSearchEdits(button, runAfter) {
    const id = document.getElementById('fEditId').value;
    const params = formParams();
    if (!params.query) return showMessage('Enter something to search for.', true);
    if (runAfter) {
        await withBusy(button, async () => {
            await api(`/api/searches/${id}`, { method: 'PATCH', body: params });
            const result = await api(`/api/searches/${id}/run`, { method: 'POST' });
            cancelSearchEdit();
            state.searchId = +id;
            state.companyId = null;
            reportRun(result);
            await refreshAll();
        });
        return;
    }
    try {
        const s = await api(`/api/searches/${id}`, { method: 'PATCH', body: params });
        cancelSearchEdit();
        showMessage(`Saved changes to "${s.name}". Click Run to search with them.`);
        await loadSearches();
    } catch (e) {
        showMessage(e.message, true);
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

// Company list back to its defaults: no search text, all priorities, sorted by priority.
// (Ticked companies are left alone; use "Select: none" to clear those.)
function resetCompanyFilters() {
    document.getElementById('companyFilter').value = '';
    document.getElementById('priorityFilter').value = '';
    setCompanySort('priority');
}

// Job list back to its defaults: All tab, no text/work style/interest filter, not-a-fit hidden,
// newest found first, and no company or saved-search filter.
function resetJobFilters() {
    state.status = '';
    state.searchId = null;
    state.companyId = null;
    state.skill = null;
    document.getElementById('textFilter').value = '';
    document.getElementById('workModeFilter').value = '';
    document.getElementById('interestFilter').value = '';
    document.getElementById('hideNotFit').checked = true;
    document.getElementById('noDescFilter').checked = false;
    document.getElementById('sortJobs').value = 'found';
    refreshAll();
}

function filterBySearch(searchId) {
    state.searchId = state.searchId === searchId ? null : searchId;
    state.companyId = null;
    state.skill = null;
    renderSkills();
    loadSearches();
    loadCompanies();
    loadJobs();
}

// --- companies ---------------------------------------------------------------

// Company list order; remembered in this browser only (a viewing preference, not data).
function getCompanySort() {
    try { return localStorage.getItem('companySort') || 'priority'; } catch (e) { return 'priority'; }
}

function setCompanySort(value) {
    try { localStorage.setItem('companySort', value); } catch (e) { /* private window etc. */ }
    loadCompanies();
}

// Retirement + PTO + Bonus, her three benefit ratings (commute depends on the job, not the company).
function benefitsTotal(c) {
    const b = c.benefits || {};
    const parts = [b.retirement_score, b.pto_score, b.bonus_score].filter(v => v !== null && v !== undefined);
    return parts.length ? parts.reduce((a, v) => a + v, 0) : null;
}

function sortCompanies(companies, how) {
    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    if (how === 'name') return companies.sort(byName);
    if (how === 'score') {
        return companies.sort((a, b) => {
            const sa = benefitsTotal(a), sb = benefitsTotal(b);
            if (sa === null || sb === null) return (sa === null) - (sb === null) || byName(a, b);
            return sb - sa || byName(a, b);
        });
    }
    return companies;  // the server already sorts by priority, then name
}

async function loadCompanies() {
    const how = getCompanySort();
    document.getElementById('companySort').value = how;
    state.companies = sortCompanies(await api('/api/companies'), how);
    renderCompanies();
}

// Company search box: the name (or priority) contains what's typed, ignoring case and punctuation,
// so "childrens" finds Nationwide Children's and "jp" finds JPMorgan Chase.
function companyMatches(name, query) {
    const clean = s => s.toLowerCase().replace(/[^a-z0-9]/g, '');
    return !query || clean(name).includes(clean(query));
}

// Priority choices for the dropdown, built once from the app's priority list.
function fillPriorityFilter() {
    const select = document.getElementById('priorityFilter');
    if (select.options.length > 1 || !state.priorities) return;
    select.innerHTML = '<option value="">All priorities</option>' +
        Object.entries(state.priorities).map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`).join('') +
        '<option value="none">No priority</option>';
}

function renderCompanies() {
    const list = document.getElementById('companyList');
    if (state.companies.length === 0) {
        list.innerHTML = '<li class="empty">No companies yet. Click + Add.</li>';
        return;
    }
    updateCheckSelectedButton();
    fillPriorityFilter();
    const query = document.getElementById('companyFilter').value.trim();
    const priority = document.getElementById('priorityFilter').value;
    // The text box matches the name or the priority label ("m" finds Liberty Mutual and Medium companies);
    // the dropdown narrows to one priority.
    const shown = state.companies.filter(c => {
        const label = c.priority ? (state.priorities[c.priority] || '') : '';
        if (priority === 'none' ? c.priority : priority && String(c.priority) !== priority) return false;
        return companyMatches(c.name, query) || (label && companyMatches(label, query));
    });
    if (shown.length === 0) {
        list.innerHTML = `<li class="empty">No company matches${query ? ` “${esc(query)}”` : ''}${priority ? ' at that priority' : ''}.</li>`;
        return;
    }
    list.innerHTML = shown.map(c => {
        const bits = [
            c.priority ? (state.priorities[c.priority] || `P${c.priority}`) : '',
            c.rating ? `★ ${c.rating}` : '',
            c.keywords ? `+ ${c.keywords}` : '',
        ].filter(Boolean).join(' · ');
        return `
        <li class="${c.id === state.companyId ? 'active' : ''}" onclick="filterByCompany(${c.id})" title="${esc(c.notes || '')}">
            <div class="saved-name"><input type="checkbox" class="pick" ${state.selectedCompanies.has(c.id) ? 'checked' : ''}
                onclick="event.stopPropagation(); toggleCompanySelected(${c.id}, this.checked)" aria-label="Select ${esc(c.name)}">${esc(c.name)}<span class="ats-tag ${c.ats === 'search' ? 'quota' : ''}">${esc(state.atsLabels[c.ats] || c.ats)}</span></div>
            ${scoreLine(c.benefits)}
            <div class="saved-meta">${esc(bits)}<br>
                ${c.job_count} job${c.job_count === 1 ? '' : 's'} · ${c.last_checked_at ? 'checked ' + esc(relativeDate(c.last_checked_at)) : 'never checked'}</div>
            <div class="saved-actions">
                <button class="small" onclick="event.stopPropagation(); checkCompany(${c.id}, this)">Check</button>
                <button class="small secondary" onclick="event.stopPropagation(); openBenefits(${esc(JSON.stringify(c.name))})">Benefits</button>
                <button class="small secondary" onclick="event.stopPropagation(); editCompany(${c.id})">Edit</button>
                <button class="small danger" onclick="event.stopPropagation(); deleteCompany(${c.id})">Delete</button>
            </div>
        </li>`;
    }).join('');
}

function filterByCompany(companyId) {
    state.companyId = state.companyId === companyId ? null : companyId;
    state.searchId = null;
    state.skill = null;
    renderSkills();
    loadSearches();
    loadCompanies();
    loadJobs();
}

function showCompanyForm(company = null) {
    togglePanel('companiesPanel', true);
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
    document.getElementById('cKeywordsHint').textContent =
        `Searched in addition to the default: ${state.defaultKeywords || '(none set)'}`;
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

async function saveArea() {
    try {
        const r = await api('/api/settings/area', { method: 'PUT', body: {
            home: document.getElementById('areaHome').value, radius_miles: document.getElementById('areaRadius').value } });
        state.area = r;
        showMessage(`On-site and hybrid jobs will be kept within ${r.radius_miles} miles of ${r.home}. Remote jobs are always kept.`);
    } catch (e) {
        showMessage(e.message, true);
    }
}

async function saveDefaultKeywords() {
    const input = document.getElementById('defaultKeywords');
    try {
        const r = await api('/api/settings/default_keywords', { method: 'PUT', body: { default_keywords: input.value } });
        state.defaultKeywords = input.value = r.default_keywords;
        showMessage(`Every company will now be searched for: ${r.default_keywords}.`);
        await loadCompanies();
    } catch (e) {
        showMessage(e.message, true);
    }
}

function toggleCompanySelected(companyId, checked) {
    if (checked) state.selectedCompanies.add(companyId);
    else state.selectedCompanies.delete(companyId);
    updateCheckSelectedButton();
}

function selectCompanies(which) {
    state.selectedCompanies = new Set(state.companies
        .filter(c => which === 'all' || (which === 'free' && c.ats !== 'search'))
        .map(c => c.id));
    loadCompanies();
}

function updateCheckSelectedButton() {
    // Drop ticks for companies that were deleted.
    const ids = new Set(state.companies.map(c => c.id));
    state.selectedCompanies.forEach(id => { if (!ids.has(id)) state.selectedCompanies.delete(id); });
    const btn = document.getElementById('checkSelectedBtn');
    const n = state.selectedCompanies.size;
    btn.textContent = n ? `Check selected (${n})` : 'Check selected';
    btn.disabled = n === 0;
}

function checkAllCompanies(button) {
    if (state.companies.length === 0) return showMessage('Add a company first.', true);
    return checkCompanies(state.companies, button, true);
}

function checkSelectedCompanies(button) {
    const picked = state.companies.filter(c => state.selectedCompanies.has(c.id));
    if (picked.length === 0) return showMessage('Tick the companies to check first.', true);
    return checkCompanies(picked, button, false);
}

async function checkCompanies(companies, button, askAboutQuota) {
    // Direct career-site checks are free; JSearch ones cost a request each.
    const quotaCompanies = companies.filter(c => c.ats === 'search');
    let includeQuota = true;
    if (quotaCompanies.length && askAboutQuota) {
        includeQuota = confirm(
            `${companies.length - quotaCompanies.length} companies are checked directly on their career sites (free).\n\n` +
            `${quotaCompanies.length} more can only be checked through JSearch, about 1 API request each.\n\n` +
            `OK = check all of them   ·   Cancel = only the free ones`);
    } else if (quotaCompanies.length) {
        // She picked these on purpose; just confirm the cost.
        includeQuota = confirm(`${quotaCompanies.length} of the selected companies use JSearch ` +
            `(about 1 API request each): ${quotaCompanies.map(c => c.name).join(', ')}.\n\nOK = include them   ·   Cancel = skip them`);
    }
    const toCheck = companies.filter(c => c.ats !== 'search' || includeQuota);
    if (toCheck.length === 0) return;
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
        scrollToResults();
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
    if (state.skill) params.append('skill', state.skill.key);
    const text = document.getElementById('textFilter').value.trim();
    if (text) params.append('q', text);
    const workMode = document.getElementById('workModeFilter').value;
    if (workMode) params.append('work_mode', workMode);
    const interest = document.getElementById('interestFilter').value;
    if (interest) params.append('interest', interest);
    if (document.getElementById('hideNotFit').checked) params.append('hide_not_fit', '1');
    const noDesc = document.getElementById('noDescFilter').checked;
    if (noDesc) params.append('no_description', '1');
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
    } else if (state.skill) {
        chip.textContent = `Asks for: ${state.skill.name} ✕`;
        chip.onclick = () => filterBySkill(state.skill.key);
        chip.classList.remove('hidden');
    } else {
        chip.classList.add('hidden');
    }

    const jobs = await api(`/api/jobs?${params}`);
    const list = document.getElementById('jobList');
    state.listedJobs = jobs;
    const fillBtn = document.getElementById('fillDescBtn');
    const reloadable = jobs.filter(j => !j.has_description && j.apply_link).length;
    fillBtn.classList.toggle('hidden', !noDesc || !reloadable || !!fillBtn.dataset.busy);
    if (!fillBtn.dataset.busy) fillBtn.textContent = `Reload these ${reloadable} from their listings`;
    // The ranking button shows on the Interested tab; its counts change as jobs are marked Interested.
    if (state.status === 'interested') loadRankStatus().catch(() => {}); else updateRankButton();
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
    const interested = job.status === 'interested', applied = job.status === 'applied', notFit = job.status === 'not_a_fit';
    return `
    <div class="job status-${esc(job.status)}" id="job-${job.id}">
        <div class="job-summary" onclick="toggleJob(${job.id})">
            <div>
                <div class="job-title">${esc(job.title)}</div>
                <div class="job-sub">${esc(job.company || 'Unknown company')}</div>
                ${job.employment_type ? `<span class="badge">${esc(job.employment_type)}</span> ` : ''}
                ${job.req_number ? `<span class="badge req" title="Requisition / posting number">Req ${esc(job.req_number)}</span> ` : ''}
                ${job.added_by_hand ? '<span class="badge by-hand" title="Added from its listing link">Added by hand</span> ' : ''}
                ${job.found_where ? `<span class="badge found" title="Where you found this job">Found: ${esc(job.found_where)}</span> ` : ''}
                ${job.has_description ? '' : '<span class="badge no-desc" title="No job description saved yet">No description</span> '}
                <span class="badge status-badge ${esc(job.status)}">${esc(STATUS_LABELS[job.status] || job.status)}${applied && job.applied_at ? ' ' + esc(shortDate(job.applied_at)) : ''}${job.interest_level && (interested || applied) ? ' · ' + esc(INTEREST_LABELS[job.interest_level]) : ''}</span>
                <div class="flag-buttons">
                    <button class="flag ${interested ? 'on interested' : ''}" onclick="toggleFlag(event, ${job.id}, 'interested', '${esc(job.status)}')">★ Interested</button>
                    <button class="flag ${applied ? 'on applied' : ''}" onclick="toggleFlag(event, ${job.id}, 'applied', '${esc(job.status)}')">✓ Applied</button>
                    <button class="flag ${notFit ? 'on not-fit' : ''}" onclick="toggleFlag(event, ${job.id}, 'not_a_fit', '${esc(job.status)}')">✕ Not a fit</button>
                    ${job.company && !job.saved_company_id ? `<button class="flag save-co-btn" title="Add ${esc(job.company)} to your saved companies" onclick="saveJobCompany(event, ${job.id})">+ Save company</button>` : ''}
                </div>
                ${interested || applied ? interestChooser(job) : ''}
            </div>
            <div class="job-side">
                ${job.match_score !== null && job.match_score !== undefined
                    ? `<div class="match-badge ${matchClass(job.match_score)}" title="Match score (open the job for the breakdown)">${job.match_score} match</div>` : ''}
                ${salary ? `<div class="salary" title="${esc(job.salary_text || '')}">${esc(salary)}</div>` : '<div class="no-pay">pay not listed</div>'}
                <div>${job.posted_at ? 'posted ' + esc(shortDate(job.posted_at)) : ''}</div>
                ${closes ? `<div class="${closes.startsWith('closed') ? 'closed' : 'closes'}">${esc(closes)}</div>` : ''}
                ${workModeBlock(job)}
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
            ${apply ? `<a class="apply" href="${apply}" target="_blank" rel="noopener noreferrer">Go to listing ↗</a>` : ''}
            ${apply ? `<button class="small secondary" onclick="reloadJob(${job.id}, this)" title="Read the listing page again to fill in the description and details">Reload from listing</button>` : ''}
        </div>
        ${job.status === 'interested' ? `<div class="match-box" id="match-${job.id}"><div class="hint">Loading match score…</div></div>` : ''}
        ${jobEditHtml(job)}
        ${jobBenefitsHtml(job)}
        ${descriptionHtml(job)}
        <div class="detail-meta">
            ${job.req_number ? `Req # ${esc(job.req_number)} · ` : ''}
            ${job.posted_at ? `Posted ${esc(shortDate(job.posted_at))} · ` : ''}
            ${job.closes_at ? `${esc(closesText(job.closes_at))} · ` : ''}
            ${job.applied_at ? `Applied ${esc(shortDate(job.applied_at))} · ` : ''}
            ${job.salary_text ? `Pay as posted: “${esc(job.salary_text)}” · ` : ''}
            ${job.publisher ? `Found via ${esc(job.publisher)} · ` : ''}
            ${alsoFoundHtml(job)}
            First saved ${esc(relativeDate(job.first_seen_at))} · last seen in results ${esc(relativeDate(job.last_seen_at))}
        </div>
    `;
    if (job.status === 'interested') loadMatch(job.id).catch(e => showMessage(e.message, true));
    // No description yet: try the listing once on its own; if that fails she can paste it.
    if (!hasDescription(job) && apply && !state.autoReloaded.has(job.id)) {
        state.autoReloaded.add(job.id);
        autoFillDescription(job.id);
    }
}

// --- job descriptions -------------------------------------------------------------------------
// A job with no description gets one Reload from listing automatically when opened; if the listing
// can't be read (LinkedIn, Indeed... refuse), she pastes the text into the box instead.

const MIN_DESCRIPTION = 200;  // same as the server: shorter counts as no description
state.autoReloaded = new Set();

function hasDescription(job) {
    return (job.description || '').trim().length >= MIN_DESCRIPTION;
}

function descriptionHtml(job) {
    if (hasDescription(job)) return `<div class="description">${esc(job.description)}</div>`;
    return `
        <div class="desc-missing" id="descbox-${job.id}">
            <div class="desc-status" id="descstatus-${job.id}">${job.apply_link
                ? 'No description yet.' : 'No description yet, and there is no listing link to read it from.'}</div>
            <label>Paste the job description from the listing
                <textarea id="descpaste-${job.id}" rows="8"
                    placeholder="On the listing page press Ctrl+A, then Ctrl+C, and paste here (Ctrl+V)">${esc(job.description || '')}</textarea>
            </label>
            <button class="small" onclick="saveDescription(${job.id}, this)">Save description</button>
        </div>`;
}

async function autoFillDescription(jobId) {
    const status = document.getElementById(`descstatus-${jobId}`);
    if (status) status.textContent = 'No description yet. Reading the listing for it…';
    try {
        const r = await api(`/api/jobs/${jobId}/reload`, { method: 'POST' });
        if (hasDescription(r.job)) {
            await refreshAfterDescription(jobId, `Filled in the description for “${r.job.title}” from the listing.`);
            return;
        }
        if (status) status.textContent = "The listing didn't include a description. Paste it below.";
    } catch (e) {
        if (status) {
            status.textContent = "Couldn't get the description from the listing (the site may block automatic reading, "
                + 'or the job may be closed). Paste it below.';
            status.title = e.message;
        }
    }
}

// Redraw after a description is filled in. With the "No description" filter on, the job leaves
// the list, so it's only reopened if it's still showing.
async function refreshAfterDescription(jobId, message) {
    await loadJobs();
    if (state.openJobId === jobId && document.getElementById(`detail-${jobId}`)) await openJob(jobId, false);
    showMessage(message);
    loadSkills().catch(() => {});
}

async function saveDescription(jobId, button) {
    const text = document.getElementById(`descpaste-${jobId}`).value;
    if (text.trim().length < MIN_DESCRIPTION) {
        showMessage('That looks too short to be the whole description. Copy the full listing text and paste it in.', true);
        return;
    }
    button.disabled = true;
    try {
        const job = await api(`/api/jobs/${jobId}`, { method: 'PATCH', body: { description: text } });
        await refreshAfterDescription(jobId, `Description saved for “${job.title}”. Reload from listing won’t replace it.`);
    } catch (e) {
        showMessage(e.message, true);
        button.disabled = false;
    }
}

// Reload from listing for every listed job that has no description, a few at a time.
async function fillMissingDescriptions(button) {
    const todo = (state.listedJobs || []).filter(j => !j.has_description && j.apply_link);
    if (!todo.length) return;
    button.dataset.busy = '1';
    button.disabled = true;
    let done = 0, filled = 0;
    const failed = [];
    const worker = async () => {
        while (todo.length) {
            const job = todo.shift();
            try {
                const r = await api(`/api/jobs/${job.id}/reload`, { method: 'POST' });
                if (hasDescription(r.job)) filled++; else failed.push(job.title);
            } catch (e) {
                failed.push(job.title);
            }
            done++;
            button.textContent = `Reading listings… ${done} done, ${filled} filled in`;
        }
    };
    try {
        await Promise.all([worker(), worker(), worker()]);
        showMessage(`Filled in ${filled} description${filled === 1 ? '' : 's'}.` + (failed.length
            ? ` ${failed.length} couldn't be read from the listing; open those jobs to paste the text in.` : ''));
    } finally {
        delete button.dataset.busy;
        button.disabled = false;
        await loadJobs();
        await loadSkills();
    }
}

async function updateJob(jobId, fields) {
    try {
        await api(`/api/jobs/${jobId}`, { method: 'PATCH', body: fields });
        if ('status' in fields || 'interest_level' in fields) {
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
        showMessage(`Backed up to OneDrive: ${result.path}`);
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
    await loadSkills();
    await loadProfile();
}

// --- ranking Interested jobs ------------------------------------------------------------------

function matchClass(score) {
    return score >= 75 ? 'high' : score >= 55 ? 'mid' : 'low';
}

async function loadProfile() {
    const p = await api('/api/profile');
    document.getElementById('pResume').value = p.resume || '';
    document.getElementById('pInterests').value = p.interests || '';
    await loadRankStatus();
}

async function loadRankStatus() {
    const r = await api('/api/ranking');
    state.rankInfo = r;
    const n = Object.keys(r.jobs).length;
    const btn = document.getElementById('scoreJobsBtn');
    if (!btn.dataset.busy) {
        btn.disabled = !r.profile_ready || !r.needs_ai;
        btn.textContent = !r.profile_ready ? 'Score Interested jobs' : r.needs_ai ? `Score ${r.needs_ai} job${r.needs_ai === 1 ? '' : 's'}` : 'All jobs scored';
    }
    document.getElementById('rankStatus').textContent = !r.profile_ready
        ? 'Add her resume, then Save, to score skills and interests.'
        : `${n} Interested job${n === 1 ? '' : 's'}${r.needs_ai ? `, ${r.needs_ai} to score` : ', all scored'}.`;
    updateRankButton();
}

// The ranking button on the Interested tab (next to the filters), so scoring is where the jobs are.
function updateRankButton() {
    const btn = document.getElementById('rankBtn');
    const r = state.rankInfo;
    const show = state.status === 'interested' && r && Object.keys(r.jobs).length > 0;
    btn.classList.toggle('hidden', !show);
    if (!show || btn.dataset.busy) return;
    if (!r.profile_ready) {
        btn.textContent = 'Score Interested jobs';
        btn.title = 'Add her resume first (Ranking Interested jobs, in the sidebar)';
    } else if (r.needs_ai) {
        btn.textContent = `Score ${r.needs_ai} Interested job${r.needs_ai === 1 ? '' : 's'}`;
        btn.title = 'Have ChatGPT compare these listings with her resume and interests';
    } else {
        btn.textContent = document.getElementById('sortJobs').value === 'match' ? 'Ranked by best match ✓' : 'Rank by best match';
        btn.title = 'All Interested jobs are scored; sort them best match first';
    }
}

async function rankButtonClick(button) {
    const r = state.rankInfo;
    if (!r || !r.profile_ready) {
        togglePanel('profilePanel', true);
        document.getElementById('profilePanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
        document.getElementById('pResume').focus();
        showMessage("Add her resume (and interests) in the Ranking card, click Save, then score.", true);
        return;
    }
    if (r.needs_ai) {
        button.dataset.busy = '1';
        button.disabled = true;
        try {
            await scoreJobs(button);
        } finally {
            delete button.dataset.busy;
            button.disabled = false;
        }
    }
    document.getElementById('sortJobs').value = 'match';
    await loadJobs();
    updateRankButton();
}

async function saveProfile(button) {
    button.disabled = true;
    try {
        await api('/api/profile', { method: 'POST', body: {
            resume: document.getElementById('pResume').value, interests: document.getElementById('pInterests').value } });
        showMessage('Saved. Click Score to score jobs with the new resume or interests.');
        await loadRankStatus();
    } catch (e) {
        showMessage(e.message, true);
    } finally {
        button.disabled = false;
    }
}

async function loadResumeFile(input) {
    const file = input.files[0];
    input.value = '';
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    try {
        const res = await fetch('/api/profile/resume_file', { method: 'POST', body: form });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Could not read the file');
        document.getElementById('pResume').value = data.text;
        showMessage(`Loaded ${file.name}. Check it, then click Save.`);
    } catch (e) {
        showMessage(e.message, true);
    }
}

// Scores in small batches so the button can show progress.
async function scoreJobs(button) {
    button.dataset.busy = '1';
    button.disabled = true;
    let done = 0;
    try {
        while (true) {
            button.textContent = `Scoring… ${done} done`;
            const r = await api('/api/ranking/score', { method: 'POST' });
            done += r.read;
            if (r.errors.length) showMessage(`Some jobs couldn't be scored (they'll be tried again): ${r.errors[0]}`, true);
            if (!r.remaining || !r.read) break;
        }
        showMessage(`Scored ${done} job${done === 1 ? '' : 's'}. Sort by Best match to rank them.`);
    } catch (e) {
        showMessage(e.message, true);
    } finally {
        delete button.dataset.busy;
        await loadRankStatus();
        await loadJobs();
    }
}

async function loadMatch(jobId) {
    const box = document.getElementById(`match-${jobId}`);
    if (!box) return;
    const m = await api(`/api/jobs/${jobId}/match`);
    if (!m) { box.innerHTML = ''; return; }
    const rows = m.criteria.map(c => `
        <tr class="${c.source}">
            <th>${esc(c.label)} <span class="weight">×${c.weight}</span></th>
            <td class="num"><input type="number" min="0" max="100" id="mo-${c.name}-${jobId}"
                value="${c.source === 'yours' ? c.score : ''}" placeholder="${c.score === null ? '–' : c.score}"
                title="Type your own score (0-100) to replace this one"></td>
            <td class="why">${esc(c.reason || '')}${c.source === 'yours'
                ? ` <a class="small-link" onclick="clearMatchScore(${jobId}, '${c.name}')">use calculated</a>` : ''}</td>
        </tr>`).join('');
    const chips = (title, items, cls) => items.length
        ? `<div class="match-chips"><span class="chips-title">${title}</span>${items.map(i => `<span class="chip-s ${cls}">${esc(i)}</span>`).join('')}</div>` : '';
    box.innerHTML = `
        <div class="match-head">
            <h4>Match score</h4>
            ${m.score === null ? '<span class="hint">Not enough information yet</span>'
                : `<span class="match-badge big ${matchClass(m.score)}">${m.score}</span>
                   <span class="hint">based on ${m.based_on} of 5</span>`}
            ${m.needs_ai ? '<span class="hint">· resume, interests or listing changed: click Score in the sidebar</span>' : ''}
        </div>
        <table class="match-table">${rows}</table>
        ${chips('Meets', m.matched, 'ok')}${chips('Partly', m.partial, 'part')}${chips('Missing', m.missing, 'miss')}
        <button class="small" onclick="saveMatchScores(${jobId})">Save my scores</button>
        <span class="hint">Blank boxes use the calculated score shown in grey. Anything missing counts as 50.</span>`;
}

async function saveMatchScores(jobId) {
    const body = {};
    for (const name of ['skills', 'interests', 'salary', 'benefits', 'commute']) {
        body[name] = document.getElementById(`mo-${name}-${jobId}`).value;
    }
    try {
        await api(`/api/jobs/${jobId}/match`, { method: 'PATCH', body });
        await loadMatch(jobId);
        await loadJobs();
        showMessage('Scores saved.');
    } catch (e) {
        showMessage(e.message, true);
    }
}

async function clearMatchScore(jobId, name) {
    await api(`/api/jobs/${jobId}/match`, { method: 'PATCH', body: { [name]: '' } });
    await loadMatch(jobId);
    await loadJobs();
}

// --- skills & certifications ---------------------------------------------------------
// ChatGPT reads each listing once for the certifications and tools it asks for; this shows
// how often each comes up across every saved job, so she can see what training pays off.

async function loadSkills() {
    state.skillsSummary = await api('/api/skills');
    renderSkills();
}

function renderSkills() {
    const s = state.skillsSummary;
    if (!s) return;
    const btn = document.getElementById('skillsAnalyzeBtn');
    if (!btn.dataset.busy) {
        btn.disabled = s.unread === 0;
        btn.textContent = s.unread ? `Analyze ${s.unread} listing${s.unread === 1 ? '' : 's'}` : 'All listings analyzed';
    }
    const noText = s.no_description
        ? ` ${s.no_description} job${s.no_description === 1 ? ' has' : 's have'} no description yet (Reload from listing can fill it in).` : '';
    document.getElementById('skillsStatus').textContent = s.read
        ? `Based on ${s.read} of ${s.total_jobs} jobs.${noText}`
        : `No listings analyzed yet.${noText}`;
    document.getElementById('skillsTables').innerHTML =
        skillTable('Tools & software', s.tools, 'tools') + skillTable('Certifications', s.certs, 'certs');
}

function skillTable(title, items, id) {
    if (!items.length) return state.skillsSummary.read ? `<h3 class="skills-heading">${title}</h3><p class="hint">None found.</p>` : '';
    const showAll = state.skillsShowAll && state.skillsShowAll[id];
    const shown = showAll ? items : items.slice(0, 12);
    const rows = shown.map(g => {
        const split = ['required', 'preferred', 'mentioned'].filter(n => g[n]).map(n => `${g[n]} ${n}`).join(' · ');
        const active = state.skill && state.skill.key === g.key;
        return `<li class="skill-row ${active ? 'active' : ''}" title="${esc(split)} — click to see these jobs"
                    data-key="${esc(g.key)}" data-name="${esc(g.name)}" onclick="filterBySkill(this.dataset.key, this.dataset.name)">
            <span class="skill-name">${esc(g.name)}</span>
            <span class="skill-bar"><span style="width:${Math.max(2, g.pct)}%"></span></span>
            <span class="skill-pct">${g.pct}%</span>
            <span class="skill-split">${g.jobs} job${g.jobs === 1 ? '' : 's'}${g.required ? ` · ${g.required} req` : ''}${g.preferred ? ` · ${g.preferred} pref` : ''}</span>
        </li>`;
    }).join('');
    const more = items.length > 12
        ? `<a class="small-link" onclick="toggleSkillsAll('${id}')">${showAll ? 'Show fewer' : `Show all ${items.length}`}</a>` : '';
    return `<h3 class="skills-heading">${title}</h3><ul class="skill-list">${rows}</ul>${more}`;
}

function toggleSkillsAll(id) {
    state.skillsShowAll = { ...(state.skillsShowAll || {}), [id]: !(state.skillsShowAll || {})[id] };
    renderSkills();
}

function filterBySkill(key, name) {
    state.skill = state.skill && state.skill.key === key ? null : { key, name };
    state.searchId = null;
    state.companyId = null;
    if (state.skill) {
        // Show every job that asks for it, Not a fit included (the counts include them).
        state.status = '';
        document.getElementById('hideNotFit').checked = false;
    }
    renderSkills();
    loadSearches();
    loadCompanies();
    loadMeta().then(loadJobs).then(scrollToResults);
}

// Reads listings in small batches so the button can show progress.
async function analyzeSkills(button) {
    button.dataset.busy = '1';
    button.disabled = true;
    let read = 0;
    try {
        while (true) {
            button.textContent = `Reading listings… ${read} done`;
            const r = await api('/api/skills/analyze', { method: 'POST', body: { limit: 8 } });
            read += r.read;
            state.skillsSummary = r.summary;
            renderSkills();
            if (r.errors.length) showMessage(`Some listings couldn't be read (they'll be tried again): ${r.errors[0]}`, true);
            if (!r.remaining || !r.read) break;
        }
        showMessage(`Analyzed ${read} listing${read === 1 ? '' : 's'}.`);
    } catch (e) {
        showMessage(e.message, true);
    } finally {
        delete button.dataset.busy;
        renderSkills();
    }
}

restorePanels();
refreshAll().catch(e => showMessage(e.message, true));


async function saveJobCompany(event, jobId) {
    event.stopPropagation();
    const button = event.currentTarget;
    button.disabled = true;
    try {
        const company = await api(`/api/jobs/${jobId}/save_company`, { method: 'POST' });
        const how = company.ats === 'search'
            ? 'It will be checked through JSearch (about 1 request per check). If you find its careers page, click Edit and paste the link to check it directly for free.'
            : `It will be checked directly on its ${state.atsLabels[company.ats] || company.ats} careers site (free).`;
        showMessage(`Added ${company.name} to your companies. ${how}`);
        await refreshAll();
    } catch (e) {
        showMessage(e.message, true);
        button.disabled = false;
    }
}

// --- job details she can add or correct ---------------------------------------------

// Listing details (filled in from the site, editable) and her own application fields.
// Listing fields are sent only when changed, so ones she hasn't touched keep updating from the site.
const LISTING_FIELDS = ['req_number', 'pay', 'work_mode', 'location', 'posted_at', 'description'];
const OWN_TEXT_FIELDS = ['resume_submitted', 'response_date', 'resume_link', 'listing_copy_location', 'callback_chances', 'notes'];
const OWN_CHECK_FIELDS = ['cover_letter', 'interviewed'];

function jobEditHtml(job) {
    const id = job.id, v = k => esc(job[k] || '');
    const day = iso => esc((iso || '').slice(0, 10));
    const payNow = job.salary_text || salaryText(job);
    const edited = JSON.parse(job.edited_fields || '[]');
    const mark = k => edited.includes(k) ? ' <span class="edited" title="You changed this; checks won’t overwrite it">edited</span>' : '';
    const field = (k, label, input) => `<label>${label}${mark(k)} ${input}</label>`;
    const initial = { req_number: job.req_number || '', pay: payNow || '', work_mode: job.work_mode || '',
                      location: job.location || '', posted_at: (job.posted_at || '').slice(0, 10),
                      description: job.description || '' };
    return `
        <div class="job-edit" data-initial="${esc(JSON.stringify(initial))}" id="edit-${id}">
            <div class="job-edit-head">
                <h4>Job details</h4>
                <button class="small secondary edit-btn" onclick="setJobEditing(${id}, true)">Edit</button>
            </div>
            ${jobDetailsView(job, payNow)}
            <div class="edit-form">
            <div class="edit-grid">
                ${field('req_number', 'Reference Number', `<input id="e-req_number-${id}" maxlength="100" value="${v('req_number')}">`)}
                ${field('posted_at', 'Date Posted', `<input type="date" id="e-posted_at-${id}" value="${day(job.posted_at)}">`)}
                ${field('pay', 'Pay', `<input id="e-pay-${id}" maxlength="100" value="${esc(payNow || '')}" placeholder="e.g. $85,000 - $100,000">`)}
                ${field('work_mode', 'Work Style', `<select id="e-work_mode-${id}">
                    ${[['', 'Unknown'], ['remote', 'Remote'], ['hybrid', 'Hybrid'], ['onsite', 'On-site']].map(([val, label]) =>
                        `<option value="${val}" ${(job.work_mode || '') === val ? 'selected' : ''}>${label}</option>`).join('')}
                </select>`)}
                ${field('location', 'Location', `<input id="e-location-${id}" maxlength="200" value="${v('location')}">`)}
                <label>Resume Submitted <input type="date" id="e-resume_submitted-${id}" value="${day(job.resume_submitted)}"></label>
                <label>Response to Resume <input type="date" id="e-response_date-${id}" value="${day(job.response_date)}"></label>
                <div class="checks">
                    <label class="check"><input type="checkbox" id="e-cover_letter-${id}" ${job.cover_letter ? 'checked' : ''}> Cover letter included</label>
                    <label class="check"><input type="checkbox" id="e-interviewed-${id}" ${job.interviewed ? 'checked' : ''}> Interview</label>
                </div>
                <label class="wide">Resume Update page
                    <span class="link-row"><input id="e-resume_link-${id}" value="${v('resume_link')}" placeholder="https://…">
                    ${safeUrl(job.resume_link) ? `<a href="${safeUrl(job.resume_link)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : ''}</span></label>
                <label class="wide">Job listing copy location <input id="e-listing_copy_location-${id}" maxlength="100" value="${v('listing_copy_location')}"></label>
                ${field('description', 'Job description', `<textarea id="e-description-${id}" class="desc-edit" rows="8">${v('description')}</textarea>`).replace('<label>', '<label class="wide">')}
                <label class="wide">Chances of a call back &amp; why <textarea id="e-callback_chances-${id}" maxlength="250">${v('callback_chances')}</textarea></label>
                <label class="wide">Notes <textarea id="e-notes-${id}">${v('notes')}</textarea></label>
            </div>
            <button class="small" onclick="saveJobDetails(${id})">Save details</button>
            <button class="small secondary" onclick="openJob(${id}, false)">Cancel</button>
            </div>
        </div>`;
}

// Read-only view of the details: only the ones that have something in them.
function jobDetailsView(job, payNow) {
    const yesNo = val => (val === null || val === undefined || val === '') ? '' : (val ? 'Yes' : 'No');
    const modes = { remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site' };
    const link = safeUrl(job.resume_link);
    const items = [
        ['Reference Number', esc(job.req_number || '')],
        ['Date Posted', esc(shortDate(job.posted_at) || '')],
        ['Pay', esc(payNow || '')],
        ['Work Style', esc(modes[job.work_mode] || '')],
        ['Location', esc(job.location || '')],
        ['Resume Submitted', esc(shortDate(job.resume_submitted) || '')],
        ['Response to Resume', esc(shortDate(job.response_date) || '')],
        ['Cover letter included', yesNo(job.cover_letter)],
        ['Interview', yesNo(job.interviewed)],
        ['Resume Update page', link ? `<a href="${link}" target="_blank" rel="noopener noreferrer">${esc(job.resume_link)}</a>` : esc(job.resume_link || ''), 'wide'],
        ['Job listing copy location', esc(job.listing_copy_location || ''), 'wide'],
        ['Chances of a call back & why', esc(job.callback_chances || ''), 'wide'],
        ['Notes', esc(job.notes || ''), 'wide'],
    ].filter(([, value]) => value);
    if (!items.length) return '<div class="details-view empty-details">Nothing filled in yet. Click Edit to add details.</div>';
    return `<dl class="details-view">${items.map(([label, value, cls]) =>
        `<div class="${cls || ''}"><dt>${esc(label)}</dt><dd>${value}</dd></div>`).join('')}</dl>`;
}

function setJobEditing(jobId, editing) {
    document.getElementById(`edit-${jobId}`).classList.toggle('editing', editing);
}

async function saveJobDetails(jobId) {
    const el = name => document.getElementById(`e-${name}-${jobId}`);
    const initial = JSON.parse(document.getElementById(`edit-${jobId}`).dataset.initial);
    const body = {};
    OWN_TEXT_FIELDS.forEach(n => { body[n] = el(n).value; });
    OWN_CHECK_FIELDS.forEach(n => { body[n] = el(n).checked; });
    // Only send listing fields she changed (spacing and line endings don't count as a change).
    const same = (a, b) => String(a).replace(/\s+/g, ' ').trim() === String(b).replace(/\s+/g, ' ').trim();
    LISTING_FIELDS.forEach(n => { if (!same(el(n).value, initial[n])) body[n] = el(n).value; });
    try {
        await api(`/api/jobs/${jobId}`, { method: 'PATCH', body });
        await loadJobs();
        await openJob(jobId, false);
        showMessage('Job details saved.');
    } catch (e) {
        showMessage(e.message, true);
    }
}

// Re-read one job's listing page (fills a missing description; edited fields stay as she set them).
async function reloadJob(jobId, button) {
    button.disabled = true;
    button.textContent = 'Reloading…';
    try {
        const r = await api(`/api/jobs/${jobId}/reload`, { method: 'POST' });
        await loadJobs();
        await openJob(jobId, false);
        const names = { description: 'description', location: 'location', is_remote: 'work style', employment_type: 'job type',
                        posted_at: 'date posted', closes_at: 'closing date', salary_min: 'pay', salary_max: 'pay',
                        salary_period: 'pay', salary_text: 'pay', req_number: 'reference number',
                        work_mode: 'work style', work_mode_source: 'work style' };
        const changed = [...new Set(r.changed.map(c => names[c] || c))];
        const ai = (r.filled_by_chatgpt || []).filter(f => changed.includes(f));
        showMessage(changed.length
            ? `Updated from the listing: ${changed.join(', ')}.` + (ai.length ? ` (${state.aiName} read ${ai.join(', ')} from the description.)` : '')
            : 'The listing has nothing new for this job.');
    } catch (e) {
        showMessage(e.message, true);
        button.disabled = false;
        button.textContent = 'Reload from listing';
    }
}

// Add or update jobs from her Job Hunt spreadsheet.
async function importSpreadsheet(input) {
    const file = input.files[0];
    input.value = '';
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    showMessage(`Importing ${file.name}…`);
    try {
        const response = await fetch('/api/jobs/import/spreadsheet', { method: 'POST', body: form });
        const r = await response.json();
        if (!response.ok) throw new Error(r.error || 'Import failed');
        state.status = ''; state.searchId = null; state.companyId = null;
        await refreshAll();
        showMessage(`Imported ${r.rows} jobs from ${file.name}: ${r.added} added, ${r.updated} already in the app and updated.`);
        scrollToResults();
    } catch (e) {
        showMessage(e.message, true);
    }
}

// --- add a job from its listing link ----------------------------------------------

const addJobState = { job: null };

function toggleAddJob(show) {
    const panel = document.getElementById('addJobPanel');
    const open = show === undefined ? panel.classList.contains('hidden') : show;
    panel.classList.toggle('hidden', !open);
    document.getElementById('addJobToggle').classList.toggle('hidden', open);
    if (open) {
        document.getElementById('ajUrl').focus();
    } else {
        ['ajUrl', 'ajText'].forEach(id => document.getElementById(id).value = '');
        document.getElementById('ajTextWrap').classList.add('hidden');
        document.getElementById('ajPreview').classList.add('hidden');
        addJobState.job = null;
    }
}

async function previewJob(button) {
    const url = document.getElementById('ajUrl').value.trim();
    const text = document.getElementById('ajText').value.trim();
    if (!url && !text) return showMessage('Paste the link to the job listing.', true);
    button.disabled = true;
    button.textContent = 'Reading…';
    try {
        const r = await api('/api/jobs/import/preview', { method: 'POST', body: { url, text } });
        addJobState.job = r.job;
        const p = r.preview;
        const facts = [
            p.work_mode ? (WORK_MODE_LABELS[p.work_mode] || p.work_mode) : 'Work style not stated',
            p.employment_type, p.salary_text, p.req_number ? `Req ${p.req_number}` : '',
            p.posted_at ? `Posted ${shortDate(p.posted_at)}` : '', `${p.description_chars.toLocaleString()} characters of description`,
        ].filter(Boolean).join(' · ');
        const box = document.getElementById('ajPreview');
        box.innerHTML = `
            <h3>Check the details, then save</h3>
            <label>Job title <input id="ajTitle" value="${esc(p.title || '')}"></label>
            <div class="row">
                <label>Company <input id="ajCompany" value="${esc(p.company || '')}"></label>
                <label>Location <input id="ajLocation" value="${esc(p.location || '')}" placeholder="e.g. Columbus, OH"></label>
            </div>
            <div class="hint">${esc(facts)}${p.publisher ? ` · from ${esc(p.publisher)}` : ''}</div>
            <label>Where did you find this job? (optional)
                <input id="ajFound" maxlength="50" placeholder="e.g. LinkedIn alert, Indeed, friend's referral">
            </label>
            <div class="aj-interest">
                <span>Interest level (optional):</span>
                ${Object.entries(INTEREST_LABELS).map(([key, label]) =>
                    `<label class="check"><input type="radio" name="ajInterest" value="${key}"> ${label}</label>`).join('')}
                <label class="check"><input type="radio" name="ajInterest" value="" checked> Skip</label>
            </div>
            <div class="buttons"><button onclick="saveImportedJob(this)">Save job</button></div>
            <div class="hint">It's saved as <strong>Interested</strong>.</div>`;
        box.classList.remove('hidden');
    } catch (e) {
        showMessage(e.message, true);
        if (/paste/i.test(e.message)) document.getElementById('ajTextWrap').classList.remove('hidden');
    } finally {
        button.disabled = false;
        button.textContent = 'Read listing';
    }
}

async function saveImportedJob(button) {
    if (!addJobState.job) return;
    button.disabled = true;
    try {
        const r = await api('/api/jobs/import/save', { method: 'POST', body: { job: addJobState.job, edits: {
            title: document.getElementById('ajTitle').value, company: document.getElementById('ajCompany').value,
            location: document.getElementById('ajLocation').value },
            found_where: document.getElementById('ajFound').value,
            interest_level: (document.querySelector('input[name="ajInterest"]:checked') || {}).value || null } });
        toggleAddJob(false);
        // Show it: clear filters that could hide it, then open the job.
        state.status = ''; state.searchId = null; state.companyId = null;
        document.getElementById('textFilter').value = '';
        await refreshAll();
        showMessage(r.merged ? 'That job was already saved; this listing was added to it.' : 'Job added.');
        if (r.id && document.getElementById(`job-${r.id}`)) {
            await openJob(r.id, true);
            document.getElementById(`job-${r.id}`).scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    } catch (e) {
        showMessage(e.message, true);
        button.disabled = false;
    }
}

// --- benefits ------------------------------------------------------------------

const SCORE_KEYS = ['retirement', 'pto', 'commute', 'bonus'];
const SCORE_SHORT = { retirement: 'Ret', pto: 'PTO', commute: 'Commute', bonus: 'Bonus' };

function fmtScore(v) {
    return v === null || v === undefined ? '' : String(+v);
}

// "Ret 4 · PTO 2 · Commute 1 · Bonus 3" under a company name; nothing if no scores yet.
// "Ret 4 · PTO 1 · Commute 1 · Bonus 2"; unrated scores show as "–" so it's clear they haven't been set.
function scoreLine(b) {
    b = b || {};
    const parts = SCORE_KEYS.map(k => {
        const v = b[`${k}_score`];
        return `${SCORE_SHORT[k]} ${v === null || v === undefined ? '–' : fmtScore(v)}`;
    });
    const unrated = SCORE_KEYS.every(k => b[`${k}_score`] === null || b[`${k}_score`] === undefined);
    return `<div class="score-line ${unrated ? 'unrated' : ''}">${esc(parts.join(' · '))}</div>`;
}

function jobBenefitsHtml(job) {
    const tags = job.benefit_tags || [];
    const b = job.company_benefits;
    const chips = b ? SCORE_KEYS.filter(k => b[`${k}_score`] !== null && b[`${k}_score`] !== undefined)
        .map(k => `<span class="score-chip">${SCORE_SHORT[k]} ${esc(fmtScore(b[`${k}_score`]))}</span>`).join('') : '';
    const facts = b && b.lookup ? b.lookup.facts.length : 0;
    const company = job.company || '';
    return `
        <div class="tag-row">
            <strong style="font-size:13px">In this posting:</strong>
            ${tags.length ? tags.map(t => `<span class="benefit-tag" title="${esc(t.quote)}">${esc(t.label)}</span>`).join('')
                          : '<span class="hint-inline" style="margin:0">no benefits mentioned</span>'}
        </div>
        ${company ? `<div class="benefit-summary">
            <strong>${esc(company)} benefits:</strong>
            ${chips || '<span class="hint-inline" style="margin:0">no scores yet</span>'}
            ${facts ? `<span class="hint-inline" style="margin:0">${facts} fact${facts === 1 ? '' : 's'} found</span>` : ''}
            <button class="small secondary" onclick="openBenefits(${esc(JSON.stringify(company))}, ${job.id})">Benefits…</button>
        </div>` : ''}`;
}

const benefitsState = { company: null, jobId: null, record: null };

function bMessage(text, isError = false) {
    const el = document.getElementById('bMessage');
    el.textContent = text;
    el.classList.toggle('error', isError);
    el.classList.toggle('hidden', !text);
}

async function openBenefits(company, jobId = null) {
    benefitsState.company = company;
    benefitsState.jobId = jobId;
    document.getElementById('bTitle').textContent = `${company} — benefits`;
    bMessage('');
    document.getElementById('benefitsModal').classList.remove('hidden');
    renderBenefits(await api(`/api/benefits?company=${encodeURIComponent(company)}`));
}

function closeBenefits() {
    document.getElementById('benefitsModal').classList.add('hidden');
    refreshAll().then(() => { if (state.openJobId) openJob(state.openJobId, false); });
}

function renderBenefits(record) {
    benefitsState.record = record;
    const lookup = record.lookup || {};
    const suggested = lookup.scores || {}, reasons = lookup.score_reasons || {};
    document.getElementById('bScores').innerHTML = SCORE_KEYS.map(k => {
        const scale = state.benefitScales[k];
        const levels = Object.entries(scale.levels).sort((a, b) => b[0] - a[0]).map(([n, d]) => `${n} ${d}`).join(' · ');
        const sug = suggested[k];
        return `<label>${esc(scale.label)}
            <input type="number" id="bScore-${k}" min="0" max="4" step="0.25" value="${esc(fmtScore(record[`${k}_score`]))}">
            <div class="scale">${esc(levels)}</div>
            ${sug !== undefined && sug !== null ? `<div class="suggest">Calculated: <strong>${esc(fmtScore(sug))}</strong>
                ${+sug !== +record[`${k}_score`] ? `<a onclick="document.getElementById('bScore-${k}').value='${esc(fmtScore(sug))}'">use</a>` : '✓'}
                <div class="reason">${esc(reasons[k] || '')}</div></div>`
              : (k === 'commute' && record.looked_up_at ? '<div class="suggest"><div class="reason">Hybrid/on-site depends on the drive — rate it yourself</div></div>' : '')}
        </label>`;
    }).join('');
    document.getElementById('bNotes').value = record.notes || '';
    document.getElementById('bUrl').value = record.benefits_url || '';

    const btn = document.getElementById('bLookupBtn');
    btn.disabled = !state.hasBenefitsAI;
    document.getElementById('bLookupHint').textContent = state.hasBenefitsAI
        ? `Uses ${state.benefitsAI} (a few cents with web search) · ${state.aiLookups} lookup${state.aiLookups === 1 ? '' : 's'} this month`
        : `Add ${state.benefitsAIKey} to .env to enable lookups`;

    const facts = lookup.facts || [];
    if (!record.looked_up_at) {
        document.getElementById('bFacts').innerHTML = '<p class="hint-inline" style="margin:12px 0 0">Not looked up yet.</p>';
        return;
    }
    const groups = {};
    facts.forEach(f => (groups[f.topic] = groups[f.topic] || []).push(f));
    const order = state.benefitTopicOrder;
    document.getElementById('bFacts').innerHTML = `
        <h3>Found ${esc(shortDate(record.looked_up_at))} <span class="hint-inline">via ${esc(lookup.method || '')}</span></h3>
        ${facts.length ? '' : '<p class="hint-inline" style="margin:0">Nothing specific found. Try adding the company\'s benefits page link above and look up again.</p>'}
        ${order.filter(t => groups[t]).map(t => `
            <div class="fact-group"><h4>${esc(state.benefitTopics[t])}</h4>
            ${groups[t].map(f => {
                const url = safeUrl(f.source_url);
                return `<div class="fact ${f.verified ? '' : 'unverified'}">
                    ${esc(f.summary)}
                    ${f.quote ? `<blockquote>“${esc(f.quote)}”</blockquote>` : ''}
                    <div class="src">${url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${esc(new URL(f.source_url).hostname)}</a>` : esc(f.source_url || '')}
                        · ${f.verified ? 'quote checked against the page' : 'from web search — verify'}</div>
                </div>`;
            }).join('')}</div>`).join('')}`;
}

function benefitsForm() {
    const body = { company: benefitsState.company, notes: document.getElementById('bNotes').value.trim(),
                   benefits_url: document.getElementById('bUrl').value.trim() };
    SCORE_KEYS.forEach(k => { body[`${k}_score`] = document.getElementById(`bScore-${k}`).value; });
    return body;
}

async function saveBenefits(button) {
    button.disabled = true;
    try {
        renderBenefits(await api('/api/benefits', { method: 'PUT', body: benefitsForm() }));
        bMessage('Saved.');
    } catch (e) {
        bMessage(e.message, true);
    } finally {
        button.disabled = false;
    }
}

async function lookupBenefits(button) {
    button.disabled = true;
    button.textContent = 'Looking up… (up to a minute)';
    bMessage('');
    try {
        // Save typed scores/notes first so the lookup result doesn't overwrite them on screen.
        await api('/api/benefits', { method: 'PUT', body: benefitsForm() });
        const record = await api('/api/benefits/lookup', { method: 'POST', body: {
            company: benefitsState.company, job_id: benefitsState.jobId,
            benefits_url: document.getElementById('bUrl').value.trim() } });
        state.aiLookups = record.ai_lookups_this_month;
        renderBenefits(record);
        const filled = (record.filled || []).map(k => state.benefitScales[k].label);
        const n = record.lookup.facts.length;
        bMessage(`Found ${n} fact${n === 1 ? '' : 's'}. ` + (filled.length
            ? `Filled in ${filled.join(', ')} from what was found. `
            : '') + 'Scores you had already set were left as they were; calculated values are shown under each box.');
    } catch (e) {
        bMessage(e.message, true);
    } finally {
        button.disabled = !state.hasBenefitsAI;
        button.textContent = 'Look up benefits';
    }
}
