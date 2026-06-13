import './style.css'
import { renderSiteNav, initSiteNav, injectSiteNavStyles } from '@figureskatingtools/shared-ui';

// Inject the shared figureskatingtools.com nav styles once at startup
injectSiteNavStyles();

/** Escape HTML special characters to prevent XSS */
function escapeHtml(str: string): string {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

interface ClientPrincipal {
    userId: string;
    userRoles: string[];
    identityProvider: string;
    userDetails: string;
}

interface GenerateResult {
    id: string;
    name: string;
    fileName: string;
    downloadUrl: string | null;
    pages: number;
    expiration: string | null;
    htmlFileName?: string;
    htmlUrl?: string | null;
}

type Tool = 'perskater' | 'results';
let activeTool: Tool = 'perskater';

const appElement = document.querySelector<HTMLDivElement>('#app')!;

// The shared site nav (across all figureskatingtools.com apps) is rendered into
// #site-nav-container by init() once the auth state is known.
appElement.innerHTML = `
  <div id="site-nav-container"></div>

  <main>
    <div id="loading-view" class="loading-screen">
      <h2>Authenticating...</h2>
      <p>Please wait while we verify your credentials.</p>
    </div>

    <div id="landing-view" class="hidden">
      <div class="card landing-card reveal">
        <span class="micro-label">Score Modifier</span>
        <h2>Reshape your competition score papers</h2>
        <p class="lead">
          Split a <strong>Figure Skating Manager</strong> "Judges Details Per Skater" export so
          every skater or team gets their own page — ready to hand out.
        </p>
        <div class="landing-contact">
          <p>To access the application, please sign in with your figureskatingtools.com account.</p>
        </div>
        <div style="margin-top: 2rem;">
          <a href="/.auth/login/aad?post_login_redirect_url=/" class="btn btn-primary">Sign In to Continue</a>
        </div>
      </div>
    </div>

    <div id="main-content" class="hidden">
      <div id="tool-view" class="card reveal" style="max-width: 860px; margin: 0 auto;">
        <span class="micro-label">Score Modifier</span>

        <div class="tool-tabs" role="tablist">
          <button class="tool-tab is-active" id="tab-perskater" type="button" role="tab">One skater per page</button>
          <button class="tool-tab" id="tab-results" type="button" role="tab">Results summary</button>
        </div>

        <div id="upload-area" class="upload-area" style="margin: 1.25rem 0;">
          <p class="upload-title">Drag &amp; drop the "Judges Details Per Skater" PDF here</p>
          <p class="upload-or">or</p>
          <button id="browse-files-btn" class="btn btn-sm btn-primary" type="button">Browse Files</button>
          <input type="file" id="file-input" accept="application/pdf,.pdf" style="display: none;">
          <div id="selected-file" class="upload-status"></div>
        </div>

        <section id="opts-perskater" class="tool-panel">
          <p class="lead">
            Each skater/team is placed on its own page (rank order), with the report header repeated
            and a clean footer. Currently used for the <strong>Tulokkaat&nbsp;(Beginners)</strong> category.
          </p>
          <label class="checkbox-row" style="display: flex; align-items: center; gap: 0.6rem; margin: 1rem 0;">
            <input type="checkbox" id="include-ranks">
            <span>Include ranks <span class="text-muted">(off by default — non-podium ranks are hidden)</span></span>
          </label>
        </section>

        <section id="opts-results" class="tool-panel hidden">
          <p class="lead">
            Build a polished one-page <strong>Tulokset</strong> results sheet (PDF) — podium with
            scores plus everyone else in skating order — and a podium-only
            <strong>CAT###RS.htm</strong> page.
          </p>
          <div class="field">
            <label class="form-label" for="idx-url">Competition <strong>index.htm</strong> URL
              <span class="text-muted">(auto-fills the fields below)</span></label>
            <div style="display: flex; gap: 0.5rem;">
              <input type="url" id="idx-url" class="form-input"
                     placeholder="https://www.figureskatingresults.fi/results/…/index.htm">
              <button id="idx-fetch" class="btn btn-sm btn-secondary" type="button">Fetch</button>
            </div>
            <div id="idx-status" class="upload-status"></div>
          </div>
          <div class="field-grid">
            <div class="field"><label class="form-label" for="r-competition">Competition</label>
              <input id="r-competition" class="form-input"></div>
            <div class="field"><label class="form-label" for="r-date">Date</label>
              <input id="r-date" class="form-input"></div>
            <div class="field"><label class="form-label" for="r-venue">Venue</label>
              <input id="r-venue" class="form-input"></div>
            <div class="field"><label class="form-label" for="r-catpage">Category page (HTML filename)</label>
              <select id="r-catpage" class="form-input"><option value="">— auto-match from index —</option></select></div>
            <div class="field"><label class="form-label" for="r-category">Category label (badge)</label>
              <input id="r-category" class="form-input" value="TULOKKAAT"></div>
            <div class="field"><label class="form-label" for="r-supertitle">Supertitle</label>
              <input id="r-supertitle" class="form-input" value="MUODOSTELMALUISTELU · VAPAAOHJELMA"></div>
          </div>
        </section>

        <div class="form-actions" style="justify-content: flex-start;">
          <button id="btn-generate" class="btn btn-primary" type="button" disabled>Generate</button>
        </div>

        <div id="result-area" style="margin-top: 1.5rem;"></div>

        <div class="card-footnote" style="margin-top: 2rem;">
          <p>
            <strong>Feedback &amp; Support:</strong><br>
            Please report bugs or send feature requests to:
            <a href="mailto:markus@lintuala.fi">markus@lintuala.fi</a>
          </p>
        </div>
      </div>
    </div>
  </main>
`;

let selectedFile: File | null = null;

function setupUserMenu(container: HTMLElement, user: ClientPrincipal) {
    container.innerHTML = `
        <div class="user-menu-container">
            <button id="user-menu-btn" class="user-btn">
                <span>${escapeHtml(user.userDetails)}</span>
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
                    <path fill-rule="evenodd" d="M1.646 4.646a.5.5 0 0 1 .708 0L8 10.293l5.646-5.647a.5.5 0 0 1 .708.708l-6 6a.5.5 0 0 1-.708 0l-6-6a.5.5 0 0 1 0-.708z"/>
                </svg>
            </button>
            <div id="user-dropdown" class="dropdown-menu">
                <div class="dropdown-header">
                    Signed in as <br> <strong>${escapeHtml(user.userDetails)}</strong>
                </div>
                <a href="/.auth/logout?post_logout_redirect_uri=/" class="dropdown-item">Sign Out</a>
            </div>
        </div>
    `;

    const btn = document.getElementById('user-menu-btn')!;
    const dropdown = document.getElementById('user-dropdown')!;

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        dropdown.classList.toggle('show');
    });
    document.addEventListener('click', () => dropdown.classList.remove('show'));
    dropdown.addEventListener('click', (e) => e.stopPropagation());
}

function setSelectedFile(file: File | null) {
    const status = document.getElementById('selected-file')!;
    const generateBtn = document.getElementById('btn-generate') as HTMLButtonElement;
    if (file && file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
        status.innerHTML = `<span class="status-error">Please choose a PDF file.</span>`;
        selectedFile = null;
        generateBtn.disabled = true;
        return;
    }
    selectedFile = file;
    if (file) {
        const sizeKb = Math.round(file.size / 1024);
        status.innerHTML = `Selected: <strong>${escapeHtml(file.name)}</strong> (${sizeKb} KB)`;
        generateBtn.disabled = false;
    } else {
        status.textContent = '';
        generateBtn.disabled = true;
    }
}

function showError(msg: string) {
    document.getElementById('result-area')!.innerHTML =
        `<div class="result-card result-card--error"><p class="status-error">${escapeHtml(msg)}</p></div>`;
}

function downloadLink(url: string, fileName: string, label: string, primary = true): string {
    const cls = primary ? 'btn btn-primary' : 'btn btn-secondary';
    return `<a class="${cls}" href="${escapeHtml(url)}" download="${escapeHtml(fileName)}">${escapeHtml(label)}</a>`;
}

async function runPerSkater() {
    const resultArea = document.getElementById('result-area')!;
    const includeRanks = (document.getElementById('include-ranks') as HTMLInputElement).checked;
    const resp = await fetch(`/api/generate?includeRanks=${includeRanks}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/pdf' },
        body: selectedFile,
    });
    if (!resp.ok) {
        showError((await resp.text()) || `Error ${resp.status}`);
        return;
    }
    const data: GenerateResult = await resp.json();
    if (!data.downloadUrl) {
        showError('Generated, but no download link was returned.');
        return;
    }
    resultArea.innerHTML = `
        <div class="result-card">
            <p class="status-success">Done — ${data.pages} page${data.pages === 1 ? '' : 's'} created.</p>
            <p class="text-muted">${escapeHtml(data.name)}</p>
            ${downloadLink(data.downloadUrl, data.fileName, `Download ${data.fileName}`)}
        </div>`;
}

async function runResults() {
    const resultArea = document.getElementById('result-area')!;
    const val = (id: string) => (document.getElementById(id) as HTMLInputElement | HTMLSelectElement).value.trim();
    const params = new URLSearchParams({
        competition: val('r-competition'),
        date: val('r-date'),
        venue: val('r-venue'),
        category: val('r-category'),
        supertitle: val('r-supertitle'),
        catFile: val('r-catpage'),
        indexUrl: val('idx-url'),
    });
    const resp = await fetch(`/api/generate_results?${params.toString()}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/pdf' },
        body: selectedFile,
    });
    if (!resp.ok) {
        showError((await resp.text()) || `Error ${resp.status}`);
        return;
    }
    const data: GenerateResult = await resp.json();
    if (!data.downloadUrl) {
        showError('Generated, but no download link was returned.');
        return;
    }
    const htmlBtn = data.htmlUrl && data.htmlFileName
        ? downloadLink(data.htmlUrl, data.htmlFileName, `Download ${data.htmlFileName}`, false)
        : '';
    resultArea.innerHTML = `
        <div class="result-card">
            <p class="status-success">Results page created.</p>
            <p class="text-muted">${escapeHtml(data.name)}</p>
            <div class="result-actions">
                ${downloadLink(data.downloadUrl, data.fileName, `Download ${data.fileName}`)}
                ${htmlBtn}
            </div>
        </div>`;
}

async function runGenerate() {
    if (!selectedFile) return;
    const generateBtn = document.getElementById('btn-generate') as HTMLButtonElement;
    const resultArea = document.getElementById('result-area')!;

    generateBtn.disabled = true;
    const originalLabel = generateBtn.textContent;
    generateBtn.textContent = 'Generating…';
    resultArea.innerHTML = `<p class="text-muted">Processing your PDF…</p>`;

    try {
        if (activeTool === 'results') {
            await runResults();
        } else {
            await runPerSkater();
        }
    } catch (e) {
        showError('Something went wrong. Please try again.');
        console.error(e);
    } finally {
        generateBtn.textContent = originalLabel;
        generateBtn.disabled = !selectedFile;
    }
}

async function fetchIndex() {
    const url = (document.getElementById('idx-url') as HTMLInputElement).value.trim();
    const status = document.getElementById('idx-status')!;
    if (!url) {
        status.innerHTML = `<span class="status-error">Enter the competition index.htm URL first.</span>`;
        return;
    }
    status.textContent = 'Fetching…';
    try {
        const resp = await fetch(`/api/parse_index?url=${encodeURIComponent(url)}`);
        if (!resp.ok) {
            status.innerHTML = `<span class="status-error">${escapeHtml((await resp.text()) || `Error ${resp.status}`)}</span>`;
            return;
        }
        const data = await resp.json();
        const set = (id: string, v: string) => {
            const el = document.getElementById(id) as HTMLInputElement;
            if (v) el.value = v;
        };
        set('r-competition', data.competition || '');
        set('r-date', data.date || '');
        set('r-venue', data.venue || '');
        const sel = document.getElementById('r-catpage') as HTMLSelectElement;
        sel.innerHTML = `<option value="">— auto-match from index —</option>`;
        for (const c of (data.categories || []) as Array<{ name: string; catFile: string }>) {
            const opt = document.createElement('option');
            opt.value = c.catFile;
            opt.textContent = `${c.name} (${c.catFile})`;
            sel.appendChild(opt);
        }
        const n = (data.categories || []).length;
        status.innerHTML = `<span class="status-success">Loaded${n ? ` — ${n} categor${n === 1 ? 'y' : 'ies'}` : ''}.</span>`;
    } catch (e) {
        status.innerHTML = `<span class="status-error">Could not fetch the index page.</span>`;
        console.error(e);
    }
}

function switchTool(tool: Tool) {
    activeTool = tool;
    document.getElementById('tab-perskater')!.classList.toggle('is-active', tool === 'perskater');
    document.getElementById('tab-results')!.classList.toggle('is-active', tool === 'results');
    document.getElementById('opts-perskater')!.classList.toggle('hidden', tool !== 'perskater');
    document.getElementById('opts-results')!.classList.toggle('hidden', tool !== 'results');
    document.getElementById('result-area')!.innerHTML = '';
    const btn = document.getElementById('btn-generate')!;
    btn.textContent = tool === 'results' ? 'Generate results page' : 'Generate';
}

function wireTool() {
    const input = document.getElementById('file-input') as HTMLInputElement;
    const browseBtn = document.getElementById('browse-files-btn')!;
    const dropZone = document.getElementById('upload-area')!;

    browseBtn.addEventListener('click', () => input.click());
    input.addEventListener('change', () => setSelectedFile(input.files?.[0] ?? null));

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        const file = e.dataTransfer?.files?.[0];
        if (file) setSelectedFile(file);
    });

    document.getElementById('btn-generate')!.addEventListener('click', runGenerate);

    document.getElementById('tab-perskater')!.addEventListener('click', () => switchTool('perskater'));
    document.getElementById('tab-results')!.addEventListener('click', () => switchTool('results'));
    document.getElementById('idx-fetch')!.addEventListener('click', fetchIndex);
}

async function init() {
    const loadingView = document.getElementById('loading-view')!;
    const landingView = document.getElementById('landing-view')!;
    const mainContent = document.getElementById('main-content')!;
    const navContainer = document.getElementById('site-nav-container')!;

    // 1. Auth info (server-side endpoint reads Easy Auth headers, no tokens exposed)
    let clientPrincipal: ClientPrincipal | null = null;
    try {
        const response = await fetch('/userinfo');
        const userInfo = await response.json();
        if (userInfo && userInfo.authenticated) {
            clientPrincipal = {
                userId: userInfo.userId || '',
                identityProvider: userInfo.identityProvider || 'aad',
                userDetails: userInfo.userDetails || '',
                userRoles: userInfo.userRoles || ['authenticated', 'anonymous'],
            };
        }
    } catch (_e) {
        // parsing failed, assume unauthenticated
    }

    // 2. Render the shared site nav
    navContainer.innerHTML = renderSiteNav({
        activeApp: 'scoremodifier',
        logoUrl: '/logo.png',
    });
    initSiteNav();
    const userSection = document.getElementById('fst-nav-right')!;

    if (!clientPrincipal) {
        userSection.innerHTML = `<a href="/.auth/login/aad" class="btn btn-primary btn-sm">Sign In</a>`;
        loadingView.classList.add('hidden');
        landingView.classList.remove('hidden');
        return;
    }

    // 3. User menu + show the tool
    setupUserMenu(userSection, clientPrincipal);
    loadingView.classList.add('hidden');
    mainContent.classList.remove('hidden');
    wireTool();
}

init();
