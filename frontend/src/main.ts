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
}

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
      <div id="tool-view" class="card reveal" style="max-width: 820px; margin: 0 auto;">
        <span class="micro-label">Score Modifier</span>
        <h2 style="margin-bottom: 1rem;">Judges Details Per Skater → one skater per page</h2>

        <p class="lead">
          Upload the <strong>"Judges Details Per Skater"</strong> PDF exported from Figure Skating
          Manager. Each skater/team is placed on its own page (in rank order), with the report
          header repeated and a clean footer. Currently used for the
          <strong>Tulokkaat&nbsp;(Beginners)</strong> category.
        </p>

        <ol class="howto-list">
          <li>Choose the "Judges Details Per Skater" PDF.</li>
          <li>Tick <strong>Include ranks</strong> if you want every rank number shown. Off by default,
              which hides the rank for everyone outside the top 3.</li>
          <li>Click <strong>Generate</strong> and download the result.</li>
        </ol>

        <div id="upload-area" class="upload-area" style="margin: 1.5rem 0;">
          <p class="upload-title">Drag &amp; drop the PDF here</p>
          <p class="upload-or">or</p>
          <button id="browse-files-btn" class="btn btn-sm btn-primary" type="button">Browse Files</button>
          <input type="file" id="file-input" accept="application/pdf,.pdf" style="display: none;">
          <div id="selected-file" class="upload-status"></div>
        </div>

        <label class="checkbox-row" style="display: flex; align-items: center; gap: 0.6rem; margin: 1rem 0;">
          <input type="checkbox" id="include-ranks">
          <span>Include ranks <span class="text-muted">(off by default — non-podium ranks are hidden)</span></span>
        </label>

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

async function runGenerate() {
    if (!selectedFile) return;
    const generateBtn = document.getElementById('btn-generate') as HTMLButtonElement;
    const resultArea = document.getElementById('result-area')!;
    const includeRanks = (document.getElementById('include-ranks') as HTMLInputElement).checked;

    generateBtn.disabled = true;
    const originalLabel = generateBtn.textContent;
    generateBtn.textContent = 'Generating…';
    resultArea.innerHTML = `<p class="text-muted">Processing your PDF…</p>`;

    try {
        const resp = await fetch(`/api/generate?includeRanks=${includeRanks}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/pdf' },
            body: selectedFile,
        });

        if (!resp.ok) {
            const msg = (await resp.text()) || `Error ${resp.status}`;
            resultArea.innerHTML = `<div class="result-card result-card--error"><p class="status-error">${escapeHtml(msg)}</p></div>`;
            return;
        }

        const data: GenerateResult = await resp.json();
        if (!data.downloadUrl) {
            resultArea.innerHTML = `<div class="result-card result-card--error"><p class="status-error">Generated, but no download link was returned.</p></div>`;
            return;
        }

        resultArea.innerHTML = `
            <div class="result-card">
                <p class="status-success">Done — ${data.pages} page${data.pages === 1 ? '' : 's'} created.</p>
                <p class="text-muted">${escapeHtml(data.name)}</p>
                <a class="btn btn-primary" href="${escapeHtml(data.downloadUrl)}" download="${escapeHtml(data.fileName)}">
                    Download ${escapeHtml(data.fileName)}
                </a>
            </div>
        `;
    } catch (e) {
        resultArea.innerHTML = `<div class="result-card result-card--error"><p class="status-error">Something went wrong. Please try again.</p></div>`;
        console.error(e);
    } finally {
        generateBtn.textContent = originalLabel;
        generateBtn.disabled = !selectedFile;
    }
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
