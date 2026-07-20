document.addEventListener('DOMContentLoaded', () => {
    if (window.lucide) lucide.createIcons();

    // -----------------------------------------------------------------------
    // DOM refs
    // -----------------------------------------------------------------------
    const urlInput = document.getElementById('urlInput');
    const analyzeBtn = document.getElementById('analyzeBtn');
    const errorMsg = document.getElementById('errorMsg');
    const autoModeToggle = document.getElementById('autoModeToggle');
    const v1ToggleContainer = document.getElementById('v1ToggleContainer');

    const modeV1Btn = document.getElementById('modeV1Btn');
    const modeV2Btn = document.getElementById('modeV2Btn');
    const v2Config = document.getElementById('v2Config');
    const v2SettingsToggle = document.getElementById('v2SettingsToggle');
    const v2SettingsPanel = document.getElementById('v2SettingsPanel');
    const v2SettingsBtn = document.getElementById('v2SettingsBtn');
    const v2LlmProvider = document.getElementById('v2LlmProvider');

    const resultsContainer = document.getElementById('resultsContainer');
    const agentStepper = document.getElementById('agentStepper');         // v1
    const v2AgentPanel = document.getElementById('v2AgentPanel');         // v2
    const finalResultsPanel = document.getElementById('finalResultsPanel');
    const manualControls = document.getElementById('manualControls');
    const continueBtn = document.getElementById('continueBtn');

    const finalTitle = document.getElementById('finalTitle');
    const productImage = document.getElementById('productImage');
    const productPrice = document.getElementById('productPrice');
    const keywordContainer = document.getElementById('keywordContainer');
    const urlsContainer = document.getElementById('urlsContainer');
    const copyUrlsBtn = document.getElementById('copyUrlsBtn');

    // v2 specific
    const v2RoundBadge = document.getElementById('v2RoundBadge');
    const v2CostBadge = document.getElementById('v2CostBadge');
    const v2ReasoningLog = document.getElementById('v2ReasoningLog');
    const v2StatDiscovered = document.getElementById('v2StatDiscovered');
    const v2StatChecked = document.getElementById('v2StatChecked');
    const v2StatMissing = document.getElementById('v2StatMissing');
    const v2StatKeywords = document.getElementById('v2StatKeywords');
    const v2StatsBar = document.getElementById('v2StatsBar');

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    let eventSource = null;
    let currentMode = 'v1';   // 'v1' | 'v2'
    let manualState = { step: 0, url: '', product_info: null, keywords: null, browse_pages: null, openai_cost: 0.0 };

    // Load default LLM provider from backend
    if (typeof window.fetch === 'function') {
        fetch('/api/default-provider')
            .then(r => r.json())
            .then(data => { if (data.provider && v2LlmProvider) v2LlmProvider.value = data.provider; })
            .catch(() => {});
    }

    // -----------------------------------------------------------------------
    // Mode switching
    // -----------------------------------------------------------------------
    modeV1Btn.addEventListener('click', () => setMode('v1'));
    modeV2Btn.addEventListener('click', () => setMode('v2'));

    function setMode(mode) {
        currentMode = mode;
        modeV1Btn.classList.toggle('active', mode === 'v1');
        modeV2Btn.classList.toggle('active', mode === 'v2');
        v1ToggleContainer.classList.toggle('hidden', mode === 'v2');
        v2SettingsToggle.classList.toggle('hidden', mode === 'v1');
        if (mode === 'v1') {
            v2SettingsPanel.classList.remove('open');
            v2SettingsPanel.classList.add('hidden');
            v2SettingsBtn.classList.remove('active');
        }
    }

    v2SettingsBtn.addEventListener('click', () => {
        const isOpen = v2SettingsPanel.classList.contains('open');
        v2SettingsBtn.classList.toggle('active', !isOpen);
        if (isOpen) {
            v2SettingsPanel.classList.remove('open');
            setTimeout(() => v2SettingsPanel.classList.add('hidden'), 350);
        } else {
            v2SettingsPanel.classList.remove('hidden');
            requestAnimationFrame(() => v2SettingsPanel.classList.add('open'));
        }
    });

    // Preset hint buttons — click to append/replace textarea content
    document.getElementById('v2PresetBtns')?.addEventListener('click', (e) => {
        const btn = e.target.closest('.v2-preset-btn');
        if (!btn) return;
        const hint = btn.dataset.hint || '';
        const ta = document.getElementById('v2UserInstructions');
        if (!ta) return;

        // Toggle: clicking same active button clears it
        if (btn.classList.contains('active')) {
            btn.classList.remove('active');
            // Remove just the hint text that was added by this button
            ta.value = ta.value.replace(hint, '').trim();
        } else {
            // Deactivate any other active buttons and remove their hints
            document.querySelectorAll('.v2-preset-btn.active').forEach(active => {
                ta.value = ta.value.replace(active.dataset.hint || '', '').trim();
                active.classList.remove('active');
            });
            btn.classList.add('active');
            const existing = ta.value.trim();
            ta.value = existing ? `${existing} ${hint}` : hint;
        }
    });

    // -----------------------------------------------------------------------
    // URL helpers
    // -----------------------------------------------------------------------
    function getValidUrl(item) {
        if (!item) return '#';
        if (typeof item === 'string') return item;
        if (typeof item.url === 'string') return item.url;
        if (typeof item.url === 'object' && item.url !== null && typeof item.url.url === 'string') return item.url.url;
        try { let v = item.url || item; return typeof v === 'object' ? JSON.stringify(v) : String(v); }
        catch (e) { return '#'; }
    }

    // -----------------------------------------------------------------------
    // Analyze button
    // -----------------------------------------------------------------------
    analyzeBtn.addEventListener('click', () => {
        const url = urlInput.value.trim();
        if (!url || !url.startsWith('http')) {
            showError("Please enter a valid URL starting with http:// or https://");
            return;
        }
        hideError();
        if (currentMode === 'v2') {
            startV2Analysis(url);
        } else if (autoModeToggle.checked) {
            startAnalysis(url);
        } else {
            startManualAnalysis(url);
        }
    });

    urlInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') analyzeBtn.click(); });

    continueBtn.addEventListener('click', () => executeNextManualStep());

    document.getElementById('resetBtn').addEventListener('click', () => {
        if (eventSource) { eventSource.close(); eventSource = null; }
        resetUI();
        resultsContainer.classList.add('hidden');
        urlInput.value = '';
        urlInput.focus();
        cleanup();
    });

    document.getElementById('v2ResetBtn')?.addEventListener('click', () => {
        if (eventSource) { eventSource.close(); eventSource = null; }
        resetV2UI();
        resultsContainer.classList.add('hidden');
        urlInput.value = '';
        urlInput.focus();
        cleanup();
    });

    if (copyUrlsBtn) {
        copyUrlsBtn.addEventListener('click', () => {
            if (!window.lastDiscoveredData || !window.lastDiscoveredData.length) return;
            let text = "Recommended Category Pages:\n\n";
            window.lastDiscoveredData.forEach(item => {
                if (item.brandUrl && item.brandUrl !== item.url)
                    text += `${item.title} — Digital Shelf (Brand Filter):\n${item.brandUrl}\n\n`;
                text += `${item.title} — Walmart Digital Shelf:\n${item.url}\n\n`;
            });
            navigator.clipboard.writeText(text).then(() => {
                const span = copyUrlsBtn.querySelector('span');
                if (span) { span.textContent = 'Copied!'; setTimeout(() => span.textContent = 'Copy URLs', 2000); }
            });
        });
    }

    const sendEmailBtn = document.getElementById('sendEmailBtn');
    if (sendEmailBtn) {
        sendEmailBtn.addEventListener('click', async () => {
            if (!window.lastRenderedData) return;
            const emailsText = document.getElementById('emailRecipients').value.trim();
            if (!emailsText) { setEmailStatus('Please enter at least one email.', false); return; }
            const emailArray = emailsText.split(',').map(e => e.trim()).filter(e => e);
            if (!emailArray.length) return;
            setEmailStatus('Sending...', null);
            sendEmailBtn.disabled = true;
            try {
                const enriched = { ...window.lastRenderedData, product_url: urlInput.value.trim(), discovered_urls: window.lastDiscoveredData || [] };
                const res = await fetch('/analyze/email', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ emails: emailArray, data: enriched }) });
                const json = await res.json();
                setEmailStatus(res.ok ? 'Email sent successfully!' : (json.detail || 'Failed.'), res.ok);
                if (res.ok) document.getElementById('emailRecipients').value = '';
            } catch (err) {
                setEmailStatus('Network error: ' + err.message, false);
            } finally {
                sendEmailBtn.disabled = false;
            }
        });
    }

    function setEmailStatus(msg, ok) {
        const el = document.getElementById('emailStatusMsg');
        el.textContent = msg;
        el.style.color = ok === true ? 'var(--success)' : ok === false ? 'var(--error)' : 'var(--text-secondary)';
    }

    // -----------------------------------------------------------------------
    // UI helpers
    // -----------------------------------------------------------------------
    function showError(msg) { errorMsg.textContent = msg; errorMsg.style.display = 'block'; }
    function hideError() { errorMsg.style.display = 'none'; }

    function resetUI() {
        ['scraping', 'keywords', 'search', 'evaluation', 'visibility'].forEach(step => {
            const el = document.getElementById(`step-${step}`);
            if (!el) return;
            el.className = 'step waiting';
            el.querySelector('.step-message').textContent = 'Waiting...';
            const di = el.querySelector('.default-icon');
            if (di) di.style.display = '';
            const sp = el.querySelector('.spinner');
            if (sp) sp.style.display = '';
            const out = document.getElementById(`output-${step}`);
            if (out) { out.classList.add('hidden'); out.innerHTML = ''; }
        });
        finalResultsPanel.classList.add('hidden');
        ['visibilityDashboardPanel', 'organicVisibilityDashboardPanel'].forEach(id => {
            const panel = document.getElementById(id);
            if (panel) panel.classList.add('hidden');
        });
        manualControls.classList.add('hidden');
        continueBtn.classList.add('hidden');
        keywordContainer.innerHTML = '';
        urlsContainer.innerHTML = '';
        analyzeBtn.disabled = true;
        analyzeBtn.querySelector('span').textContent = 'Analyzing...';
        analyzeBtn.querySelector('svg').style.display = 'none';
        urlInput.disabled = true;
        autoModeToggle.disabled = true;
    }

    function resetV2UI() {
        v2ReasoningLog.innerHTML = '<p class="v2-log-placeholder">Agent activity will appear here...</p>';
        v2RoundBadge.textContent = 'Round 0';
        v2CostBadge.textContent = '$0.000000';
        document.querySelectorAll('.v2-preset-btn.active').forEach(b => b.classList.remove('active'));
        const brandedCb = document.getElementById('v2IncludeBranded');
        if (brandedCb) brandedCb.checked = false;
        v2StatDiscovered.textContent = '0';
        v2StatChecked.textContent = '0';
        v2StatMissing.textContent = '0';
        v2StatKeywords.textContent = '0';
        v2StatsBar.classList.remove('processing');
        ['v2StepScraping', 'v2StepKeywords'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.className = 'v2-setup-step';
        });
        setV2SetupMsg('v2MsgScraping', 'Waiting...');
        setV2SetupMsg('v2MsgKeywords', 'Waiting...');
        finalResultsPanel.classList.add('hidden');
        const visPanel = document.getElementById('visibilityDashboardPanel');
        if (visPanel) visPanel.classList.add('hidden');
        const organicPanel = document.getElementById('organicVisibilityDashboardPanel');
        if (organicPanel) organicPanel.classList.add('hidden');
        keywordContainer.innerHTML = '';
        urlsContainer.innerHTML = '';
        analyzeBtn.disabled = true;
        analyzeBtn.querySelector('span').textContent = 'Analyzing...';
        analyzeBtn.querySelector('svg').style.display = 'none';
        urlInput.disabled = true;
    }

    function cleanup() {
        if (eventSource) { eventSource.close(); eventSource = null; }
        analyzeBtn.disabled = false;
        analyzeBtn.querySelector('span').textContent = 'Analyze';
        analyzeBtn.querySelector('svg').style.display = '';
        urlInput.disabled = false;
        autoModeToggle.disabled = false;
        // Stop stats bar processing indicator
        v2StatsBar.classList.remove('processing');
    }

    // =======================================================================
    // Guided feature tour ("How it works")
    // Uses driver.js to walk through every control & result section. Renders a
    // built-in sample report so dashboards/tables are populated, then tears the
    // demo down when the tour ends.
    // =======================================================================
    const TOUR_SAMPLE_URL = 'https://www.walmart.com/ip/Acme-Wireless-Noise-Cancelling-Headphones/123456789';
    const TOUR_DEMO_IMAGE =
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80'%3E%3Crect width='80' height='80' rx='12' fill='%23dbe2ea'/%3E%3Ctext x='40' y='45' font-size='11' text-anchor='middle' fill='%2364748b' font-family='sans-serif'%3EDemo%3C/text%3E%3C/svg%3E";

    const TOUR_SAMPLE_REPORT = {
        product_title: 'Acme Wireless Noise-Cancelling Headphones',
        product_brand: 'Acme',
        product_id: '123456789',
        product_image: TOUR_DEMO_IMAGE,
        product_price: '$79.99',
        keywords_used: [
            'wireless headphones', 'noise cancelling headphones', 'bluetooth headphones',
            'over ear headphones', 'gaming headset', 'workout earbuds',
        ],
        shelf_results: [
            { url: 'https://www.walmart.com/browse/electronics/headphones/3944_96469', keyword: 'wireless headphones', position: 1, product_found: true, found: true, brand_found: true, sponsored: true, organic: true, page_number: 1, visibility: true, discoverability: true, placement_rank: 2, placements: [{ placement_index: 1, placement_rank: 2, visibility: true, discoverability: false, sponsored: true, organic: false }, { placement_index: 2, placement_rank: 11, visibility: true, discoverability: true, sponsored: false, organic: true }] },
            { url: 'https://www.walmart.com/browse/electronics/bluetooth-headphones/3944_96469_1231', keyword: 'bluetooth headphones', position: 2, product_found: true, found: true, brand_found: true, sponsored: false, organic: true, page_number: 1, visibility: true, discoverability: true, placement_rank: 6, placements: [{ placement_index: 1, placement_rank: 6, visibility: true, discoverability: true, sponsored: false, organic: true }] },
            { url: 'https://www.walmart.com/browse/electronics/over-ear-headphones/3944_96469_4561', keyword: 'over ear headphones', position: 3, product_found: false, found: false, brand_found: true, sponsored: true, organic: false, page_number: 0, visibility: true, discoverability: false, placement_rank: 4, placements: [{ placement_index: 1, placement_rank: 4, visibility: true, discoverability: false, sponsored: true, organic: false }] },
            { url: 'https://www.walmart.com/browse/electronics/gaming-headsets/3944_96469_7788', keyword: 'gaming headset', position: 4, product_found: true, found: true, brand_found: true, sponsored: false, organic: false, page_number: 1, visibility: false, discoverability: true },
            { url: 'https://www.walmart.com/browse/electronics/noise-cancelling/3944_96469_9911', keyword: 'noise cancelling headphones', position: 5, product_found: false, found: false, brand_found: false, sponsored: false, organic: false, page_number: 0, visibility: false, discoverability: false },
        ],
        shelf_stats: { score: 60.0, found: 3, missing: 2, total: 5, visible: 3, discoverable: 3, placements: 4, organic: 2, sponsored: 2 },
        openai_cost_usd: 0.0381,
    };

    let tourDemoRendered = false;
    let tourPrevUrl = '';

    function tourOpenSettings() {
        v2SettingsPanel.classList.remove('hidden');
        requestAnimationFrame(() => v2SettingsPanel.classList.add('open'));
        v2SettingsBtn.classList.add('active');
    }

    function tourCloseSettings() {
        v2SettingsPanel.classList.remove('open');
        v2SettingsPanel.classList.add('hidden');
        v2SettingsBtn.classList.remove('active');
    }

    // Populate the Advance-mode processing panel + final results with sample data.
    function tourRenderDemo() {
        if (tourDemoRendered) return;
        tourDemoRendered = true;

        tourCloseSettings();
        resultsContainer.classList.remove('hidden');
        agentStepper.classList.add('hidden');
        v2AgentPanel.classList.remove('hidden');

        // Demo processing panel content
        v2RoundBadge.textContent = 'Round 3';
        v2CostBadge.textContent = '$0.038100';
        v2StatDiscovered.textContent = '21';
        v2StatChecked.textContent = '10';
        v2StatMissing.textContent = '2';
        v2StatKeywords.textContent = '8';
        setV2SetupMsg('v2MsgScraping', 'Acme Wireless Headphones · $79.99');
        setV2SetupMsg('v2MsgKeywords', '8 keywords generated');
        v2ReasoningLog.innerHTML =
            '<div class="v2-log-entry"><strong>Round 1 · search</strong> — searching 5 high-intent keywords for browse pages.</div>' +
            '<div class="v2-log-entry"><strong>Round 2 · evaluate</strong> — ranked 21 discovered pages by similarity.</div>' +
            '<div class="v2-log-entry"><strong>Round 3 · check_shelf</strong> — verifying product presence &amp; placement on top shelves.</div>';

        // Render the full results panel from the sample report
        renderV2FinalResults(TOUR_SAMPLE_REPORT);
        if (window.lucide) lucide.createIcons();
    }

    // Reset everything the tour touched back to a clean initial state.
    function tourTeardown() {
        tourDemoRendered = false;

        finalResultsPanel.classList.add('hidden');
        resultsContainer.classList.add('hidden');
        v2AgentPanel.classList.add('hidden');
        agentStepper.classList.add('hidden');

        keywordContainer.innerHTML = '';
        urlsContainer.innerHTML = '';
        ['visibilityDashboard', 'visibilityDashboardPanel', 'organicVisibilityDashboardPanel'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden');
        });

        // Reset processing panel
        v2ReasoningLog.innerHTML = '<p class="v2-log-placeholder">Agent activity will appear here...</p>';
        v2RoundBadge.textContent = 'Round 0';
        v2CostBadge.textContent = '$0.000000';
        v2StatDiscovered.textContent = '0';
        v2StatChecked.textContent = '0';
        v2StatMissing.textContent = '0';
        v2StatKeywords.textContent = '0';
        setV2SetupMsg('v2MsgScraping', 'Waiting...');
        setV2SetupMsg('v2MsgKeywords', 'Waiting...');

        // Reset product header
        finalTitle.textContent = 'Product Title';
        if (productImage) { productImage.removeAttribute('src'); productImage.classList.add('hidden'); }
        if (productPrice) { productPrice.textContent = ''; productPrice.classList.add('hidden'); }

        tourCloseSettings();
        setMode('v1');
        urlInput.value = tourPrevUrl || '';
    }

    function startFeatureTour() {
        const driverFactory = window.driver && window.driver.js && window.driver.js.driver;
        if (!driverFactory) {
            console.warn('[Tour] driver.js not loaded');
            return;
        }

        // Make sure controls are interactive and start from a clean slate
        const existingUrl = urlInput.value;
        if (eventSource) { eventSource.close(); eventSource = null; }
        cleanup();
        tourTeardown();
        // Preserve a real URL the user already typed; otherwise show a sample
        tourPrevUrl = (existingUrl && existingUrl.startsWith('http')) ? existingUrl : '';
        urlInput.value = tourPrevUrl || TOUR_SAMPLE_URL;

        const pop = (title, description, side, align) => ({
            popover: { title, description, side: side || 'bottom', align: align || 'start' },
        });

        const steps = [
            {
                popover: {
                    title: 'Welcome to Shelves Finder',
                    description: 'Shelves Finder shows where your Walmart product appears across category "shelves" — and where it is missing. This quick tour covers every feature. Use <b>Next</b> / <b>Back</b>, or press <b>Esc</b> to exit anytime.',
                    align: 'center',
                },
            },
            { element: '#urlInput', ...pop('1. Paste a product URL', 'Drop in any Walmart product page URL. This is the product we analyze for shelf discoverability and visibility.') },
            { element: '.mode-selector', ...pop('2. Choose a mode', '<b>Basic</b> runs a fixed 5-step pipeline — fast and predictable. <b>Advance</b> runs an agentic AI loop that searches, evaluates, and digs deeper until it hits your targets.') },
            { element: '#v1ToggleContainer', ...pop('3. Auto vs Manual (Basic)', 'In Basic mode, toggle <b>Auto</b> to run everything end-to-end, or leave it off to step through each stage manually and inspect the output.'), onHighlightStarted: () => { setMode('v1'); } },
            { element: '#analyzeBtn', ...pop('4. Run the analysis', 'Click <b>Analyze</b> (or press Enter) to start. Results stream in live as the agent works.') },
            { element: '#modeV2Btn', ...pop('5. Advance mode', 'Switch to <b>Advance</b> to unlock agent settings and deeper visibility analytics. We\'ll turn it on now to show what it adds.'), onHighlightStarted: () => { setMode('v2'); tourOpenSettings(); } },
            { element: '#v2SettingsToggle', ...pop('6. Agent settings', 'This <b>Settings</b> button expands the agent configuration panel. Let\'s look at each option.') },
            { element: '#v2LlmProvider', ...pop('LLM provider', 'Pick which AI model powers the agent — <b>Claude</b> or <b>OpenAI</b>.', 'right') },
            { element: '#v2MaxRounds', ...pop('Max rounds', 'The maximum number of search → evaluate → check cycles the agent may run before stopping.', 'right') },
            { element: '#v2TargetMissing', ...pop('Target missing', 'The agent keeps working until it finds this many shelves where your product is missing (or runs out of rounds/budget).', 'right') },
            { element: '#v2Budget', ...pop('Budget (USD)', 'A hard spend cap for AI + API calls. The agent stops once this limit is reached.', 'right') },
            { element: '#v2IncludeBranded', ...pop('Include branded shelves', 'When checked, the agent includes shelves discovered using searches that contain your brand name.', 'right') },
            { element: '#v2UserInstructions', ...pop('Agent context (optional)', 'Steer the agent toward the right shelves, e.g. "probiotic supplement — focus on pharmacy & wellness aisles."', 'top') },
            { element: '#v2AgentPanel', ...pop('7. Live processing', 'While running, this panel shows the agent\'s progress in real time — current round and running cost.'), onHighlightStarted: () => { tourRenderDemo(); } },
            { element: '#v2ReasoningLog', ...pop('Agent reasoning log', 'Every decision the agent makes — which tool it chose and why — streams here, so the process is fully transparent.') },
            { element: '#v2StatsBar', ...pop('Live stats', 'At-a-glance counters: pages found, pages checked, missing shelves, and keywords tried.') },
            { element: '.result-header', ...pop('8. Product summary', 'When complete, results open with the product title, image, and price so you know exactly what was analyzed.') },
            { element: '#visibilityDashboard', ...pop('9. Discoverability Dashboard', 'How many shelves your product is found on vs missing from — with an overall discoverability score and risk level.') },
            { element: '#keywordContainer', ...pop('10. Extracted Search Intent', 'The keywords the AI inferred shoppers would use to find this product. Each links to a live Walmart search.') },
            { element: '#urlsContainer', ...pop('11. Recommended Category Pages', 'Compare the brand-filtered and general Walmart shelves, then review discoverability, organic, and sponsored signals together under Placement Mix.') },
            { element: '#copyUrlsBtn', ...pop('Copy URLs', 'One click copies all recommended category page URLs to your clipboard.') },
            { element: '#visibilityDashboardPanel', ...pop('12. Visibility Dashboard (Advance)', 'Across all keyword returns checked, this scores how many place your product <b>On the First Page</b> vs <b>Not on the First Page</b>.') },
            { element: '#organicVisibilityDashboardPanel', ...pop('13. Placement Mix Dashboard (Advance)', 'Shows how many keywords have organic and sponsored placements; a keyword can count in both groups.') },
            { element: '#emailRecipients', ...pop('14. Email the report', 'Send the full report to one or more recipients (comma-separated) for sharing with your team.'), onHighlightStarted: () => { finalResultsPanel.scrollIntoView({ behavior: 'smooth', block: 'end' }); } },
            {
                popover: {
                    title: "You're all set!",
                    description: 'That\'s every feature. Paste a real Walmart URL and hit <b>Analyze</b> to try it for yourself. You can replay this tour anytime from <b>How it works</b>.',
                    align: 'center',
                },
            },
        ];

        const driverObj = driverFactory({
            showProgress: true,
            allowClose: true,
            smoothScroll: true,
            stagePadding: 6,
            stageRadius: 8,
            popoverClass: 'sf-tour-popover',
            overlayColor: 'rgba(8, 10, 18, 0.72)',
            nextBtnText: 'Next →',
            prevBtnText: '← Back',
            doneBtnText: 'Done',
            steps,
            onDestroyed: () => { tourTeardown(); },
        });

        driverObj.drive();
    }

    document.getElementById('howItWorksBtn')?.addEventListener('click', startFeatureTour);

    function updateStepStatus(stepId, status, message) {
        const el = document.getElementById(`step-${stepId}`);
        if (!el) return;
        el.className = `step ${status}`;
        if (message) el.querySelector('.step-message').textContent = message;
        if (status === 'complete' || status === 'warning') {
            const wrap = el.querySelector('.step-icon');
            wrap.innerHTML = '';
            const icon = document.createElement('i');
            icon.setAttribute('data-lucide', status === 'complete' ? 'check-circle' : 'alert-triangle');
            wrap.appendChild(icon);
            lucide.createIcons({ root: wrap });
        }
    }

    function showOutput(stepId, data) {
        const el = document.getElementById(`output-${stepId}`);
        if (!el) return;
        el.innerHTML = formatOutput(stepId, data);
        el.classList.remove('hidden');
    }

    // -----------------------------------------------------------------------
    // v1 Auto Mode (SSE)
    // -----------------------------------------------------------------------
    function startAnalysis(url) {
        resetUI();
        agentStepper.classList.remove('hidden');
        v2AgentPanel.classList.add('hidden');
        resultsContainer.classList.remove('hidden');
        const provider = v2LlmProvider ? v2LlmProvider.value : '';
        let v1Url = `/analyze/stream?url=${encodeURIComponent(url)}`;
        if (provider) v1Url += `&llm_provider=${encodeURIComponent(provider)}`;
        eventSource = new EventSource(v1Url);
        eventSource.onmessage = (e) => {
            try { handleStreamEvent(JSON.parse(e.data)); }
            catch (err) { console.error("Parse error", err); }
        };
        eventSource.onerror = () => {
            showError("Connection to server lost.");
            cleanup();
            document.querySelectorAll('.step.running').forEach(el => updateStepStatus(el.id.replace('step-', ''), 'warning', 'Connection interrupted.'));
        };
    }

    function handleStreamEvent(data) {
        const { step, status, message, data: payload } = data;
        if (step === 'error') { showError(message || "An error occurred."); cleanup(); return; }
        if (step === 'done') { renderFinalResults(payload); saveResultsToCSV(urlInput.value.trim(), payload); cleanup(); return; }
        updateStepStatus(step, status, message);
    }

    // -----------------------------------------------------------------------
    // v1 Manual Mode
    // -----------------------------------------------------------------------
    function startManualAnalysis(url) {
        resetUI();
        agentStepper.classList.remove('hidden');
        v2AgentPanel.classList.add('hidden');
        resultsContainer.classList.remove('hidden');
        manualState = { step: 0, url, product_info: null, keywords: null, browse_pages: null, openai_cost: 0.0 };
        manualControls.classList.remove('hidden');
        executeNextManualStep();
    }

    async function executeNextManualStep() {
        if (manualState.step === 1) {
            const ti = document.getElementById('edit-product-title');
            const fi = document.getElementById('edit-product-features');
            const ii = document.getElementById('edit-product-id');
            const bi = document.getElementById('edit-product-brand');
            if (ti && manualState.product_info) manualState.product_info.title = ti.value.trim();
            if (ii && manualState.product_info) manualState.product_info.id = ii.value.trim();
            if (bi && manualState.product_info) manualState.product_info.brand = bi.value.trim();
            if (fi && manualState.product_info) manualState.product_info.features = fi.value.split('\n').map(f => f.trim()).filter(f => f);
        }
        if (manualState.step === 2) {
            const ua = document.getElementById('edit-unbranded-keywords-input');
            const ba = document.getElementById('edit-branded-keywords-input');
            const ca = document.getElementById('edit-keywords-input');
            if (ba || ua) {
                manualState.branded_keywords = ba ? ba.value.split('\n').map(k => k.trim()).filter(k => k) : [];
                manualState.unbranded_keywords = ua ? ua.value.split('\n').map(k => k.trim()).filter(k => k) : [];
                manualState.keywords = [...manualState.branded_keywords, ...manualState.unbranded_keywords];
            } else if (ca) {
                const kws = ca.value.split('\n').map(k => k.trim()).filter(k => k);
                if (kws.length) { manualState.keywords = kws; manualState.branded_keywords = []; manualState.unbranded_keywords = kws; }
            }
        }
        continueBtn.classList.add('hidden');
        manualState.step++;
        try {
            if (manualState.step === 1) {
                updateStepStatus('scraping', 'running', 'Scraping product data...');
                const res = await fetch('/analyze/step/scrape', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: manualState.url }) });
                if (!res.ok) throw new Error("Scraping failed");
                manualState.product_info = await res.json();
                updateStepStatus('scraping', 'complete', `Found: '${manualState.product_info.title}'`);
                showOutput('scraping', manualState.product_info);
                continueBtn.querySelector('span').textContent = 'Continue to Keyword Analysis';
                continueBtn.classList.remove('hidden');
            } else if (manualState.step === 2) {
                updateStepStatus('keywords', 'running', 'AI Keyword Agent extracting search terms...');
                const res = await fetch('/analyze/step/keywords', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ product_info: manualState.product_info }) });
                if (!res.ok) throw new Error("Keyword extraction failed");
                const data = await res.json();
                manualState.keywords = data.keywords;
                manualState.branded_keywords = data.branded_keywords;
                manualState.unbranded_keywords = data.unbranded_keywords;
                manualState.openai_cost += data.cost || 0.0;
                updateStepStatus('keywords', 'complete', `Generated ${manualState.keywords.length} phrases.`);
                showOutput('keywords', data);
                continueBtn.querySelector('span').textContent = 'Continue to Search Discovery';
                continueBtn.classList.remove('hidden');
            } else if (manualState.step === 3) {
                updateStepStatus('search', 'running', `Searching Walmart for ${manualState.keywords.length} phrases...`);
                const res = await fetch('/analyze/step/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ keywords: manualState.keywords, product_title: manualState.product_info.title }) });
                if (!res.ok) throw new Error("Search failed");
                const data = await res.json();
                manualState.browse_pages = data.browse_pages;
                updateStepStatus('search', 'complete', `Discovered ${manualState.browse_pages.length} URLs.`);
                showOutput('search', { browse_pages: manualState.browse_pages });
                continueBtn.querySelector('span').textContent = 'Continue to Similarity Evaluation';
                continueBtn.classList.remove('hidden');
            } else if (manualState.step === 4) {
                updateStepStatus('evaluation', 'running', `Ranking candidates via embedding similarity...`);
                const res = await fetch('/analyze/step/evaluate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ product_info: manualState.product_info, browse_pages: manualState.browse_pages }) });
                if (!res.ok) throw new Error("Evaluation failed");
                const data = await res.json();
                manualState.openai_cost += data.cost || 0.0;
                manualState.evaluation_data = data;
                data.total_openai_cost = manualState.openai_cost;
                updateStepStatus('evaluation', 'complete', 'Ranking complete.');
                showOutput('evaluation', data);
                continueBtn.querySelector('span').textContent = 'Continue to Shelf Visibility Check';
                continueBtn.classList.remove('hidden');
            } else if (manualState.step === 5) {
                updateStepStatus('visibility', 'running', `Checking ${manualState.evaluation_data.browse_pages.length} pages...`);
                const res = await fetch('/analyze/step/visibility', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ product_id: manualState.product_info.id, product_brand: manualState.product_info.brand, browse_pages: manualState.evaluation_data.browse_pages }) });
                if (!res.ok) throw new Error("Shelf Visibility failed");
                const stats = await res.json();
                manualState.evaluation_data.shelf_stats = stats.shelf_stats;
                updateStepStatus('visibility', 'complete', 'Scan complete.');
                showOutput('visibility', stats);
                continueBtn.querySelector('span').textContent = 'Show Final Results';
                continueBtn.classList.remove('hidden');
            } else if (manualState.step === 6) {
                const finalData = {
                    product_title: manualState.product_info.title, product_id: manualState.product_info.id,
                    product_brand: manualState.product_info.brand,
                    product_image: manualState.product_info.image || '',
                    product_price: manualState.product_info.price || '',
                    keywords: manualState.keywords,
                    branded_keywords: manualState.branded_keywords || [], unbranded_keywords: manualState.unbranded_keywords || [],
                    browse_pages: manualState.evaluation_data.browse_pages,
                    confidence_score: manualState.evaluation_data.confidence_score,
                    openai_cost: manualState.openai_cost, shelf_stats: manualState.evaluation_data.shelf_stats
                };
                renderFinalResults(finalData);
                saveResultsToCSV(manualState.url, finalData);
                manualControls.classList.add('hidden');
                cleanup();
            }
        } catch (err) {
            showError(err.message);
            updateStepStatus(getStepNameByNum(manualState.step), 'error', 'Failed');
            cleanup();
        }
    }

    function getStepNameByNum(n) {
        return ['', 'scraping', 'keywords', 'search', 'evaluation', 'visibility'][n] || 'error';
    }

    // -----------------------------------------------------------------------
    // v2 Agentic Mode
    // -----------------------------------------------------------------------
    function startV2Analysis(url) {
        // Read config BEFORE resetV2UI() clears the form state
        const maxRounds        = parseInt(document.getElementById('v2MaxRounds').value) || 5;
        const targetMissing    = parseInt(document.getElementById('v2TargetMissing').value) || 3;
        const budget           = parseFloat(document.getElementById('v2Budget').value) || 0.50;
        const includeBranded   = document.getElementById('v2IncludeBranded')?.checked || false;
        const userInstructions = (document.getElementById('v2UserInstructions')?.value || '').trim();

        resetV2UI();

        // Restore checkbox state so it reflects what was actually sent
        const brandedCb = document.getElementById('v2IncludeBranded');
        if (brandedCb) brandedCb.checked = includeBranded;

        agentStepper.classList.add('hidden');
        v2AgentPanel.classList.remove('hidden');
        resultsContainer.classList.remove('hidden');
        finalResultsPanel.classList.add('hidden');

        const llmProvider = v2LlmProvider ? v2LlmProvider.value : '';
        let sseUrl = `/v2/analyze/stream?url=${encodeURIComponent(url)}&max_rounds=${maxRounds}&target_missing_count=${targetMissing}&budget_limit=${budget}&include_branded=${includeBranded}`;
        if (llmProvider) sseUrl += `&llm_provider=${encodeURIComponent(llmProvider)}`;
        if (userInstructions) {
            sseUrl += `&user_instructions=${encodeURIComponent(userInstructions)}`;
        }
        eventSource = new EventSource(sseUrl);

        eventSource.onmessage = (e) => {
            try { handleV2StreamEvent(JSON.parse(e.data)); }
            catch (err) { console.error("V2 parse error", err); }
        };
        eventSource.onerror = () => {
            showError("Connection to server lost.");
            cleanup();
        };
    }

    function handleV2StreamEvent(data) {
        const ev = data.event;

        // --- Setup phases ---
        if (ev === 'setup_scraping') {
            const step = document.getElementById('v2StepScraping');
            if (data.status === 'complete') {
                step.classList.add('done');
                setV2SetupMsg('v2MsgScraping', data.data?.title ? `✓ ${data.data.title}` : '✓ Done');
            } else if (data.status === 'warning') {
                step.classList.add('warn');
                setV2SetupMsg('v2MsgScraping', '⚠ ' + data.message);
            } else {
                step.classList.add('running');
                setV2SetupMsg('v2MsgScraping', data.message || 'Scraping...');
            }
            return;
        }

        if (ev === 'setup_keywords') {
            const step = document.getElementById('v2StepKeywords');
            if (data.status === 'complete') {
                step.classList.add('done');
                const kws = data.data?.keywords || [];
                setV2SetupMsg('v2MsgKeywords', `✓ ${kws.length} keywords: ${kws.slice(0, 3).join(', ')}${kws.length > 3 ? '...' : ''}`);
            } else {
                step.classList.add('running');
                setV2SetupMsg('v2MsgKeywords', data.message || 'Generating...');
            }
            return;
        }

        if (ev === 'loop_start') {
            // Start pulsing indicator on stats bar
            v2StatsBar.classList.add('processing');

            let loopMsg = `🔁 Agent loop started — max ${data.config?.max_rounds} rounds, target ${data.config?.target_missing_count} missing pages`;
            if (data.config?.include_branded) {
                loopMsg += ` · <span style="color:var(--accent-primary);font-weight:600;">branded shelves ON</span>`;
            }
            if (data.config?.user_instructions) {
                loopMsg += `<br><span style="opacity:0.75;font-style:italic;">📌 Context: ${data.config.user_instructions}</span>`;
            }
            appendV2Log('loop', loopMsg);
            return;
        }

        // --- ReAct loop events ---
        if (ev === 'agent_reasoning') {
            v2RoundBadge.textContent = `Round ${data.round}`;
            const s = data.state_summary;
            if (s) {
                v2StatDiscovered.textContent = s.pages_discovered || 0;
                v2StatChecked.textContent = (s.pages_discovered || 0) - (s.pages_unchecked || 0);
                v2StatMissing.textContent = s.missing_count || 0;
                v2StatKeywords.textContent = (s.keywords_tried || []).length;
                v2CostBadge.textContent = `$${(s.total_cost_usd || 0).toFixed(6)}`;
            }
            appendV2Log('reasoning', `💭 Round ${data.round}: Agent reasoning...`);
            return;
        }

        if (ev === 'tool_selected') {
            const toolEmoji = { search: '🔍', evaluate: '📊', check_shelf: '👁', expand_keywords: '📈', stop: '🛑' };
            appendV2Log('tool', `${toolEmoji[data.tool] || '🔧'} <strong>${data.tool}</strong>: ${data.reasoning || ''}`);
            return;
        }

        if (ev === 'tool_result') {
            const icon = data.success ? '✅' : '❌';
            appendV2Log('result', `${icon} ${data.tool} → ${data.message || ''}`);
            return;
        }

        if (ev === 'goal_check') {
            const pct = data.target > 0 ? Math.round((data.missing_found / data.target) * 100) : 0;
            appendV2Log('goal', `🎯 Goal: ${data.missing_found}/${data.target} missing pages found (${pct}%)`);
            return;
        }

        if (ev === 'complete') {
            appendV2Log('done', `🏁 <strong>Complete</strong>: ${data.stop_reason || data.message}`);
            if (data.data) {
                renderV2FinalResults(data.data);
            }
            cleanup();
            return;
        }

        if (ev === 'error') {
            appendV2Log('error', `❌ Error: ${data.message}`);
            showError(data.message || "An error occurred.");
            cleanup();
            return;
        }
    }

    function appendV2Log(type, html) {
        const placeholder = v2ReasoningLog.querySelector('.v2-log-placeholder');
        if (placeholder) placeholder.remove();

        const entry = document.createElement('div');
        entry.className = `v2-log-entry v2-log-${type}`;
        entry.innerHTML = html;

        const ts = document.createElement('span');
        ts.className = 'v2-log-ts';
        ts.textContent = new Date().toLocaleTimeString();
        entry.prepend(ts);

        v2ReasoningLog.appendChild(entry);
        v2ReasoningLog.scrollTop = v2ReasoningLog.scrollHeight;
    }

    function setV2SetupMsg(id, msg) {
        const el = document.getElementById(id);
        if (el) el.textContent = msg;
    }

    function renderV2FinalResults(report) {
        // Normalise to the same shape renderFinalResults expects
        const normalised = {
            product_title: report.product_title || '',
            product_brand: report.product_brand || '',
            product_id: report.product_id || '',
            product_image: report.product_image || '',
            product_price: report.product_price || '',
            keywords: report.keywords_used || [],
            unbranded_keywords: report.keywords_used || [],
            branded_keywords: [],
            browse_pages: (report.shelf_results || []).map(sr => ({
                url: sr.url,
                keyword: sr.keyword || '',
                position: sr.position || 0,
                found: sr.discoverability ?? sr.product_found ?? sr.found,
                brand_found: sr.brand_found ?? null,
                // Per-shelf signals so the table columns reflect the right dashboard.
                visibility: sr.visibility ?? sr.found,
                discoverability: sr.discoverability ?? sr.brand_found,
                organic: sr.organic,
                sponsored: sr.sponsored,
                placement_rank: sr.placement_rank ?? firstPlacementRank(sr.placements),
                placements: sr.placements || [],
            })),
            confidence_score: (report.shelf_stats?.score || 0) / 100,
            openai_cost: report.openai_cost_usd || 0,
            shelf_stats: report.shelf_stats || {},
        };
        renderFinalResults(normalised);

        // Advanced dashboards use the per-shelf visibility signals.
        renderVisibilityDashboardPanel(report.shelf_results || []);
        renderOrganicVisibilityDashboardPanel(report.shelf_results || []);
    }

    // Browser-console helper for rendering a locally supplied report without
    // running the analysis pipeline. Intended for UI development and QA.
    window.renderShelvesFinderMockReport = (report) => {
        if (!report || typeof report !== 'object') {
            throw new TypeError('renderShelvesFinderMockReport expects a report object.');
        }
        resultsContainer.classList.remove('hidden');
        agentStepper.classList.add('hidden');
        renderV2FinalResults(report);
        finalResultsPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return report;
    };

    // -----------------------------------------------------------------------
    // Shared: Persist + Render final results
    // -----------------------------------------------------------------------
    async function saveResultsToCSV(url, data) {
        try {
            const payload = {
                url, product_title: data.product_title || 'Unknown', product_brand: data.product_brand || '',
                product_id: data.product_id || '', keywords: data.keywords || [],
                branded_keywords: data.branded_keywords || [], unbranded_keywords: data.unbranded_keywords || [],
                browse_pages: (data.browse_pages || []).map(p => getValidUrl(p)),
                openai_cost: data.openai_cost || 0.0,
            };
            const res = await fetch('/analyze/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            if (!res.ok) console.warn("Failed to persist data.");
        } catch (err) { console.error("Persistence error:", err); }
    }

    // Derive a readable category name from a Walmart browse URL.
    function deriveCategoryName(url) {
        const cleanUrl = String(url || '').split(/[?#]/)[0];
        const parts = cleanUrl.split('/').filter(Boolean);
        let categoryName = (parts[parts.length - 1] || 'Category').replace(/-/g, ' ');
        const bi = parts.indexOf('browse');
        if (bi !== -1 && bi < parts.length - 1) {
            const segs = [];
            for (let i = bi + 1; i < parts.length; i++) {
                let p = parts[i];
                // Numeric-only trailing segments are result/rank identifiers,
                // not part of the shopper-facing category hierarchy.
                if (/^\d+$/.test(p) || p.includes('_') || (p.length > 25 && !p.includes('-'))) break;
                if (p) segs.push(p.replace(/-/g, ' ').replace(/\b\w/g, l => l.toUpperCase()));
            }
            if (segs.length) categoryName = segs.join(' > ');
        }
        return categoryName.replace(/\b\w/g, l => l.toUpperCase());
    }

    // Build the brand-filtered shelf URL for a category page.
    function brandFilterUrl(url, brand) {
        const encodedBrand = brand ? encodeURIComponent(brand) : '';
        const baseUrl = url.split('?')[0];
        return encodedBrand ? `${baseUrl}?facet=brand%3A${encodedBrand}` : url;
    }

    // Text-only placement state used only in Recommended Category Pages.
    // Both pills always remain visible so organic and sponsored status can be
    // scanned independently without implying they are mutually exclusive.
    function placementStatusPill(label, state, title, placementRank = null) {
        const isPresent = state === true;
        const stateClass = isPresent ? 'placement-pill-present' : 'placement-pill-absent';
        const numericRank = Number(placementRank);
        const hasExactRank = isPresent && Number.isInteger(numericRank) && numericRank > 0;
        const detailText = hasExactRank ? `Rank #${numericRank}` : (isPresent ? 'Found' : 'Not Found');
        const detailClass = isPresent ? 'placement-detail-present' : 'placement-detail-absent';
        return `<span class="placement-status-item">
                    <span class="placement-status-pill ${stateClass}"
                        title="${title}: ${detailText}"
                        aria-label="${label}: ${detailText}">${label}</span>
                    <span class="placement-status-detail ${detailClass}">${detailText}</span>
                </span>`;
    }

    function firstPlacementRank(placements, placementType = null) {
        if (!Array.isArray(placements)) return null;
        const ranks = placements
            .filter(placement => !placementType || placement?.[placementType] === true)
            .map(placement => Number(placement?.placement_rank))
            .filter(rank => Number.isInteger(rank) && rank > 0);
        return ranks.length ? Math.min(...ranks) : null;
    }

    function renderFinalResults(data) {
        window.lastRenderedData = data;
        finalResultsPanel.classList.remove('hidden');
        finalTitle.textContent = data.product_title || 'Unknown Product';

        const imageUrl = data.product_image || '';
        if (productImage) {
            if (imageUrl) {
                productImage.src = imageUrl;
                productImage.classList.remove('hidden');
            } else {
                productImage.removeAttribute('src');
                productImage.classList.add('hidden');
            }
        }

        const price = data.product_price || '';
        if (productPrice) {
            if (price) {
                productPrice.textContent = price;
                productPrice.classList.remove('hidden');
            } else {
                productPrice.textContent = '';
                productPrice.classList.add('hidden');
            }
        }

        keywordContainer.innerHTML = '';
        const kws = data.unbranded_keywords?.length ? data.unbranded_keywords : (data.keywords || []);
        if (kws.length) {
            const p = document.createElement('p'); p.className = 'formatted-label'; p.textContent = 'Generic Keywords'; keywordContainer.appendChild(p);
            const ul = document.createElement('ul'); ul.className = 'formatted-list clean-links';
            kws.forEach(kw => { const li = document.createElement('li'); li.innerHTML = `<a href="https://www.google.com/search?q=${encodeURIComponent(kw + ' walmart')}" target="_blank">${kw}</a>`; ul.appendChild(li); });
            keywordContainer.appendChild(ul);
        }

        if (data.shelf_stats) {
            renderVisibilityDashboard(data.shelf_stats);
            document.getElementById('visibilityDashboard').classList.remove('hidden');
        }

        urlsContainer.innerHTML = '';
        window.lastDiscoveredData = [];

        if (!data.browse_pages?.length) {
            urlsContainer.innerHTML = '<p style="color:var(--text-secondary)">No category pages discovered.</p>';
        } else {
            const hdr = document.createElement('div');
            hdr.className = 'recommended-grid recommended-grid-header';
            hdr.innerHTML = `<div>Keyword</div><div class="recommended-rank-header" title="Rank within the Google results for this keyword">Google Rank<br>(per keyword)</div><div>Digital Shelf (Brand Filter)</div><div>Walmart Digital Shelf</div><div class="recommended-placement-header">Placement Mix</div>`;
            urlsContainer.appendChild(hdr);

            data.browse_pages.forEach(item => {
                let url = getValidUrl(item);
                if (typeof url !== 'string') url = String(url);
                const keyword = typeof item === 'string' ? 'Unknown' : (item.keyword || '');
                const posText = (item?.position) ? `<span class="recommended-rank-value" title="Google rank #${item.position}">#${item.position}</span>` : '';

                const categoryName = deriveCategoryName(url);
                const brandUrl = brandFilterUrl(url, data.product_brand);

                window.lastDiscoveredData.push({ title: categoryName, url, brandUrl, keyword, position: item?.position || null });

                // Resolve the three visibility signals for this shelf.
                //   Visibility (including ads) — product on the base/general shelf.
                //   Discoverability  — product on the brand-filtered shelf (Page 1 / not).
                //   Organic          — any placement with Visibility AND Discoverability.
                //   Sponsored        — any placement carrying the sponsored marker.
                // These page summaries are independent and may both be true.
                // v1 reads shelf_stats.details[url] with placement summaries;
                // v2 rows carry booleans on the item, so we derive from those.
                const detail = data.shelf_stats?.details?.[url];
                let visibility, discoverability, organic, sponsored, placements, placementRank;
                if (detail && typeof detail === 'object' && 'visibility' in detail) {
                    // v1 payload: check_shelf_visibility details[url].
                    visibility = !!detail.visibility;
                    discoverability = !!detail.discoverability;
                    organic = !!detail.organic;
                    sponsored = !!detail.sponsored;
                    placements = detail.placements;
                    placementRank = detail.placement_rank ?? firstPlacementRank(detail.placements);
                } else if (typeof item.visibility === 'boolean') {
                    // v2 payload: per-shelf signals mapped onto the item.
                    visibility = item.visibility;
                    discoverability = item.discoverability === true;
                    organic = typeof item.organic === 'boolean'
                        ? item.organic
                        : visibility && discoverability;
                    sponsored = typeof item.sponsored === 'boolean'
                        ? item.sponsored
                        : visibility && !discoverability;
                    placements = item.placements;
                    placementRank = item.placement_rank ?? firstPlacementRank(item.placements);
                } else if (typeof item.found === 'boolean') {
                    // Fallback (legacy/tour rows): approximate from found/brand_found.
                    visibility = item.found;
                    discoverability = (item.brand_found ?? item.found) === true;
                    organic = visibility && discoverability;
                    sponsored = visibility && !discoverability;
                    placements = item.placements;
                    placementRank = item.placement_rank ?? firstPlacementRank(item.placements);
                } else {
                    visibility = !!detail;
                    discoverability = !!detail;
                    organic = visibility && discoverability;
                    sponsored = visibility && !discoverability;
                    placements = [];
                    placementRank = null;
                }

                let organicPlacementRank = firstPlacementRank(placements, 'organic');
                let sponsoredPlacementRank = firstPlacementRank(placements, 'sponsored');
                // Older payloads only expose one aggregate rank. It is safe to
                // associate that rank when exactly one placement type is present.
                if (organic !== sponsored) {
                    if (organic && organicPlacementRank === null) organicPlacementRank = placementRank;
                    if (sponsored && sponsoredPlacementRank === null) sponsoredPlacementRank = placementRank;
                }

                const discoverabilityHtml = placementStatusPill('Discoverability', discoverability, 'Discoverability on the brand-filtered Digital Shelf');
                const placementMixHtml = `<div class="placement-mix-statuses">${discoverabilityHtml}${placementStatusPill('Organic', organic, 'Organic placement present', organicPlacementRank)}${placementStatusPill('Sponsored', sponsored, 'Sponsored placement present', sponsoredPlacementRank)}</div>`;

                const row = document.createElement('div');
                row.className = 'recommended-grid recommended-grid-row';
                row.innerHTML = `
                    <div class="result-keyword-col recommended-keyword-cell"><span class="keyword-badge">${keyword}</span></div>
                    <div class="recommended-rank-cell">${posText}</div>
                    <a href="${brandUrl}" target="_blank" class="url-card recommended-brand-link"><div class="url-info"><span class="url-title">${categoryName} (Brand Filter)</span></div><i data-lucide="external-link"></i></a>
                    <a href="${url}" target="_blank" class="url-card recommended-base-link"><div class="url-info"><span class="url-title">${categoryName}</span></div><i data-lucide="external-link"></i></a>
                    <div class="recommended-placement-cell">${placementMixHtml}</div>`;
                urlsContainer.appendChild(row);
            });
            if (window.lucide) lucide.createIcons({ root: urlsContainer });
        }
        finalResultsPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // -----------------------------------------------------------------------
    // Visibility dashboard (shared)
    // -----------------------------------------------------------------------
    function renderVisibilityDashboard(stats) {
        if (!stats) return;
        const total = stats.total || 1;
        const missPct = ((stats.missing / total) * 100).toFixed(1);
        const foundPct = ((stats.found / total) * 100).toFixed(1);
        const score = stats.score || 0;

        const pill = document.getElementById('dashStatusPill');
        pill.textContent = score < 20 ? 'CRITICAL' : 'HEALTHY';
        pill.className = `dash-pill ${score < 20 ? 'critical' : 'healthy'}`;

        document.getElementById('dashBarMissPct').textContent = `${missPct}%`;
        document.getElementById('dashBarFoundPct').textContent = `${foundPct}%`;
        document.getElementById('dashFillMiss').style.width = `${missPct}%`;
        document.getElementById('dashTextMiss').textContent = `${stats.missing} / ${stats.total}`;
        document.getElementById('dashFillFound').style.width = `${foundPct}%`;
        document.getElementById('dashTextFound').textContent = `${stats.found} / ${stats.total}`;
        document.getElementById('dashBtmMiss').textContent = stats.missing;
        document.getElementById('dashBtmFound').textContent = stats.found;
        document.getElementById('dashBtmTotal').textContent = stats.total;
        document.getElementById('dashScoreHuge').textContent = `${score.toFixed(1)}%`;
        document.getElementById('dashScoreBar').style.width = `${score}%`;

        const rTop = document.getElementById('dashRiskLevel');
        const rHuge = document.getElementById('dashRiskHuge');
        const rDesc = document.getElementById('dashRiskDesc');
        const banner = document.getElementById('dashBannerMsg');

        if (score < 30) {
            rTop.textContent = 'CRITICAL LEVEL'; rHuge.textContent = 'HIGH RISK'; rHuge.className = 'dash-risk-huge miss';
            rDesc.textContent = `Immediate action required for ${stats.missing} shelves`;
            banner.textContent = `Your product is missing from ${stats.missing} out of ${stats.total} shelves. This represents a ${missPct}% discoverability gap.`;
        } else if (score < 70) {
            rTop.textContent = 'WARNING LEVEL'; rHuge.textContent = 'MODERATE'; rHuge.className = 'dash-risk-huge total';
            rDesc.textContent = `Discoverability lacking on ${stats.missing} shelves`;
            banner.textContent = `Moderate discoverability. Consider optimizing listings for the missing ${stats.missing} shelves.`;
        } else {
            rTop.textContent = 'OPTIMAL LEVEL'; rHuge.textContent = 'LOW RISK'; rHuge.className = 'dash-risk-huge healthy';
            rDesc.textContent = `Strong presence across ${stats.found} shelves`;
            banner.textContent = `Excellent! Your product is discoverable across a strong network of digital shelves.`;
        }
    }

    // -----------------------------------------------------------------------
    // Visibility Dashboard (v2 only) — corresponds to the "Walmart Digital Shelf".
    // Binary: On First Page vs Not on First Page, driven by the base-shelf
    // Visibility signal over all keyword returns checked.
    // -----------------------------------------------------------------------
    function renderVisibilityDashboardPanel(shelfResults) {
        const panel = document.getElementById('visibilityDashboardPanel');
        if (!panel) return;

        const all = shelfResults || [];
        const total = all.length;   // total keyword returns checked (denominator)

        if (total === 0) {
            panel.classList.add('hidden');
            return;
        }
        panel.classList.remove('hidden');

        // Visibility is a page-1 base-shelf signal (no pagination), so a visible
        // product is "On First Page" and everything else is "Not on First Page".
        const isVisible = (s) => (s.visibility !== undefined ? s.visibility : s.found) === true;
        const onFirst  = all.filter(isVisible).length;
        const notFirst = total - onFirst;

        const onPct  = ((onFirst  / total) * 100).toFixed(1);
        const notPct = ((notFirst / total) * 100).toFixed(1);
        const visScore = (onFirst / total) * 100;

        // --- Bars ---
        document.getElementById('visBarP1Pct').textContent = `${onPct}%`;
        document.getElementById('visFillP1').style.width    = `${onPct}%`;
        document.getElementById('visTextP1').textContent    = `${onFirst} / ${total}`;

        document.getElementById('visBarP2Pct').textContent = `${notPct}%`;
        document.getElementById('visFillP2').style.width    = `${notPct}%`;
        document.getElementById('visTextP2').textContent    = `${notFirst} / ${total}`;

        // --- Stat boxes ---
        document.getElementById('visStatP1').textContent = onFirst;
        document.getElementById('visStatP2').textContent = notFirst;
        document.getElementById('visStatP3').textContent = total;

        // --- Score bar ---
        document.getElementById('visScoreHuge').textContent = `${visScore.toFixed(1)}%`;
        document.getElementById('visScoreBar').style.width  = `${Math.min(visScore, 100)}%`;

        // --- Risk level + placement quality ---
        const pill   = document.getElementById('visStatusPill');
        const rLevel = document.getElementById('visRiskLevel');
        const qHuge  = document.getElementById('visQualityHuge');
        const qDesc  = document.getElementById('visQualityDesc');
        const banner = document.getElementById('visBannerMsg');

        if (visScore >= 80) {
            pill.textContent  = 'PRIME';    pill.className  = 'dash-pill healthy';
            rLevel.textContent = 'PRIME PLACEMENT';  rLevel.style.color = '#10b981';
            qHuge.textContent = 'EXCELLENT'; qHuge.className = 'dash-risk-huge healthy';
            qDesc.textContent = `${onFirst} of ${total} keyword returns on the first page`;
            banner.textContent = `Excellent visibility! ${onFirst} of ${total} keyword returns place your product on the first page — maximum shopper exposure.`;
        } else if (visScore >= 50) {
            pill.textContent  = 'MODERATE'; pill.className  = 'dash-pill moderate';
            rLevel.textContent = 'MODERATE VISIBILITY';   rLevel.style.color = '#f59e0b';
            qHuge.textContent = 'MODERATE'; qHuge.className = 'dash-risk-huge moderate';
            qDesc.textContent = `${onFirst} of ${total} keyword returns on the first page`;
            banner.textContent = `Moderate visibility. ${onFirst} on the first page, ${notFirst} not on the first page. Improving first-page presence will boost shopper reach.`;
        } else {
            pill.textContent  = 'LOW';   pill.className  = 'dash-pill critical';
            rLevel.textContent = 'LOW VISIBILITY';   rLevel.style.color = '#ef4444';
            qHuge.textContent = 'BURIED';   qHuge.className = 'dash-risk-huge miss';
            qDesc.textContent = `Only ${onFirst} of ${total} keyword returns on the first page`;
            banner.textContent = `Low visibility. Your product is on the first page for only ${onFirst} of ${total} keyword returns — most shoppers will not find it. Prioritise first-page presence.`;
        }

        if (window.lucide) lucide.createIcons();
    }

    // -----------------------------------------------------------------------
    // Organic & sponsored placement dashboard (v2 only). Each result represents
    // one keyword and may independently contain organic and sponsored placements.
    // -----------------------------------------------------------------------
    function renderOrganicVisibilityDashboardPanel(shelfResults) {
        const panel = document.getElementById('organicVisibilityDashboardPanel');
        if (!panel) return;

        const all = shelfResults || [];
        const totalKeywords = all.length;

        if (totalKeywords === 0) {
            panel.classList.add('hidden');
            return;
        }
        panel.classList.remove('hidden');

        const hasOrganicPlacement = (s) => {
            if (typeof s.organic === 'boolean') return s.organic;
            if (Array.isArray(s.placements) && s.placements.length) {
                return s.placements.some(p => p.organic === true);
            }
            const visible = (s.visibility !== undefined ? s.visibility : s.found) === true;
            const discoverable = (s.discoverability !== undefined ? s.discoverability : s.brand_found) === true;
            return visible && discoverable;
        };
        const hasSponsoredPlacement = (s) => {
            if (typeof s.sponsored === 'boolean') return s.sponsored;
            if (Array.isArray(s.placements) && s.placements.length) {
                return s.placements.some(p => p.sponsored === true);
            }
            const visible = (s.visibility !== undefined ? s.visibility : s.found) === true;
            const discoverable = (s.discoverability !== undefined ? s.discoverability : s.brand_found) === true;
            return visible && !discoverable;
        };

        const organicKeywords = all.filter(hasOrganicPlacement).length;
        const sponsoredKeywords = all.filter(hasSponsoredPlacement).length;
        const foundPct = ((organicKeywords / totalKeywords) * 100).toFixed(1);
        const sponsoredPct = ((sponsoredKeywords / totalKeywords) * 100).toFixed(1);
        const score = (organicKeywords / totalKeywords) * 100;

        document.getElementById('organicBarFoundPct').textContent = `${foundPct}%`;
        document.getElementById('organicFillFound').style.width = `${foundPct}%`;
        document.getElementById('organicTextFound').textContent = `${organicKeywords} / ${totalKeywords}`;
        document.getElementById('organicBarMissingPct').textContent = `${sponsoredPct}%`;
        document.getElementById('organicFillMissing').style.width = `${sponsoredPct}%`;
        document.getElementById('organicTextMissing').textContent = `${sponsoredKeywords} / ${totalKeywords}`;
        document.getElementById('organicStatFound').textContent = organicKeywords;
        document.getElementById('organicStatMissing').textContent = sponsoredKeywords;
        document.getElementById('organicStatTotal').textContent = totalKeywords;
        document.getElementById('organicScoreHuge').textContent = `${score.toFixed(1)}%`;
        document.getElementById('organicScoreBar').style.width = `${Math.min(score, 100)}%`;

        const pill = document.getElementById('organicStatusPill');
        const risk = document.getElementById('organicRiskLevel');
        const quality = document.getElementById('organicQualityHuge');
        const description = document.getElementById('organicQualityDesc');
        const banner = document.getElementById('organicBannerMsg');

        if (score >= 80) {
            pill.textContent = 'STRONG'; pill.className = 'dash-pill healthy';
            risk.textContent = 'STRONG ORGANIC PRESENCE'; risk.style.color = '#10b981';
            quality.textContent = 'EXCELLENT'; quality.className = 'dash-risk-huge healthy';
            description.textContent = `${organicKeywords} of ${totalKeywords} keywords have organic placements`;
            banner.textContent = `Placement mix: ${organicKeywords} of ${totalKeywords} keywords have organic placements and ${sponsoredKeywords} have sponsored placements.`;
        } else if (score >= 50) {
            pill.textContent = 'MODERATE'; pill.className = 'dash-pill moderate';
            risk.textContent = 'GROWING ORGANIC REACH'; risk.style.color = '#f59e0b';
            quality.textContent = 'MODERATE'; quality.className = 'dash-risk-huge moderate';
            description.textContent = `${organicKeywords} of ${totalKeywords} keywords have organic placements`;
            banner.textContent = `Placement mix: ${organicKeywords} of ${totalKeywords} keywords have organic placements and ${sponsoredKeywords} have sponsored placements.`;
        } else {
            pill.textContent = 'LOW'; pill.className = 'dash-pill critical';
            risk.textContent = 'LOW ORGANIC PRESENCE'; risk.style.color = '#ef4444';
            quality.textContent = 'LIMITED'; quality.className = 'dash-risk-huge miss';
            description.textContent = `Only ${organicKeywords} of ${totalKeywords} keywords have organic placements`;
            banner.textContent = `Placement mix: ${organicKeywords} of ${totalKeywords} keywords have organic placements and ${sponsoredKeywords} have sponsored placements.`;
        }
    }

    // -----------------------------------------------------------------------
    // v1 output formatters (unchanged)
    // -----------------------------------------------------------------------
    function formatOutput(stepId, data) {
        if (stepId === 'scraping') {
            const featuresText = data.features?.length ? data.features.join('\n') : '';
            return `<div class="formatted-section" style="display:flex;gap:1rem;"><div style="flex:1;"><p class="formatted-label">Product ID (Editable)</p><input type="text" class="edit-title-input" id="edit-product-id" value="${data.id||''}" spellcheck="false"></div><div style="flex:2;"><p class="formatted-label">Brand (Editable)</p><input type="text" class="edit-title-input" id="edit-product-brand" value="${data.brand||''}" spellcheck="false"></div></div><div class="formatted-section"><p class="formatted-label">Product Title (Editable)</p><input type="text" class="edit-title-input" id="edit-product-title" value="${data.title||''}" spellcheck="false"></div><div class="formatted-section"><p class="formatted-label">Description snippet</p><p class="formatted-text">${data.description ? data.description.substring(0,150)+'...' : 'N/A'}</p></div><div class="formatted-section"><p class="formatted-label">Extracted Features (Editable)</p><textarea class="edit-keywords-area" id="edit-product-features" rows="4" spellcheck="false">${featuresText}</textarea></div>`;
        }
        if (stepId === 'keywords') {
            const unText = data.unbranded_keywords?.join('\n') || '';
            const combText = data.keywords?.join('\n') || '';
            if (!unText && combText) return `<div class="formatted-section"><p class="formatted-label">Keywords (Editable)</p><textarea class="edit-keywords-area" id="edit-keywords-input" rows="5" spellcheck="false">${combText}</textarea></div>`;
            return `<div class="formatted-section"><p class="formatted-label">Generic Keywords</p><textarea class="edit-keywords-area" id="edit-unbranded-keywords-input" rows="5" spellcheck="false">${unText}</textarea></div>`;
        }
        if (stepId === 'search') {
            const links = data.browse_pages?.length
                ? `<ul class="formatted-list clean-links">${data.browse_pages.map(item => { const u = getValidUrl(item); const pos = item.position ? `<span style="color:var(--text-secondary);font-size:0.85em;margin-right:0.5rem;">[Rank: ${item.position}]</span>` : ''; return `<li>${pos}<a href="${u}" target="_blank">${u}</a></li>`; }).join('')}</ul>`
                : '<p class="formatted-empty">No pages found.</p>';
            return `<div class="formatted-section"><p class="formatted-label">Discovered URLs</p>${links}</div>`;
        }
        if (stepId === 'evaluation') {
            const pct = Math.round(data.confidence_score * 100);
            const color = pct > 75 ? 'var(--success)' : pct > 40 ? 'var(--warning)' : 'var(--error)';
            const links = data.browse_pages?.length
                ? `<ul class="formatted-list clean-links">${data.browse_pages.map(item => { const u = getValidUrl(item); return `<li><a href="${u}" target="_blank">${u}</a></li>`; }).join('')}</ul>`
                : '<p class="formatted-empty">No valid pages.</p>';
            return `<div class="formatted-section"><p class="formatted-label">Similarity Confidence</p><p class="formatted-text" style="color:${color};font-weight:600;">${pct}% Match Quality</p></div><div class="formatted-section"><p class="formatted-label">Ranked URLs</p>${links}</div>`;
        }
        if (stepId === 'visibility' && data.shelf_stats) {
            return `<div class="formatted-section"><p class="formatted-label">Shelf Scan Complete</p><p class="formatted-text">Found on ${data.shelf_stats.found} / ${data.shelf_stats.total} shelves.</p></div>`;
        }
        return `<pre class="raw-fallback">${JSON.stringify(data, null, 2)}</pre>`;
    }
});
