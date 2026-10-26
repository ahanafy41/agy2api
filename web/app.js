/**
 * agy2api Web Studio - Client Application
 * Zero-dependency, accessible, real-time streaming, multimodal AI interface.
 */

(function () {
    'use strict';

    // In-memory fallback for resilient storage in restricted WebViews or private browsing
    const memoryStore = {};
    const safeStorage = {
        getItem(key, fallback = null) {
            try {
                const val = localStorage.getItem(key);
                return val !== null ? val : fallback;
            } catch {
                return key in memoryStore ? memoryStore[key] : fallback;
            }
        },
        setItem(key, value) {
            try {
                localStorage.setItem(key, String(value));
            } catch {
                memoryStore[key] = String(value);
            }
        }
    };

    // Application State
    const state = {
        apiKey: safeStorage.getItem('agy2api_key', 'pwd'),
        apiMode: safeStorage.getItem('agy2api_mode', 'openai'),
        selectedModel: safeStorage.getItem('agy2api_model', 'gemini-3.8-flash-low'),
        effort: safeStorage.getItem('agy2api_effort', 'medium'),
        systemPrompt: safeStorage.getItem('agy2api_system_prompt', ''),
        streamEnabled: safeStorage.getItem('agy2api_stream', 'true') !== 'false',
        messages: [],
        pendingAttachments: [],
        isGenerating: false,
        abortController: null,
        lastMetrics: { latency: 0, tokens: 0, status: 'جاهز' },
        lastPayloads: { curl: '', request: {}, response: {} }
    };

    // DOM Elements
    const elements = {
        statusDot: document.getElementById('status-dot'),
        statusText: document.getElementById('status-text'),
        serverStatus: document.getElementById('server-status'),
        modelSelect: document.getElementById('model-select'),
        effortGroup: document.getElementById('effort-group'),
        effortSelect: document.getElementById('effort-select'),
        newChatBtn: document.getElementById('new-chat-btn'),
        openSettingsBtn: document.getElementById('open-settings-btn'),
        toggleInspectorBtn: document.getElementById('toggle-inspector-btn'),
        chatMessages: document.getElementById('chat-messages'),
        chatViewport: document.querySelector('.chat-viewport'),
        attachmentsBar: document.getElementById('attachments-bar'),
        mediaFileInput: document.getElementById('media-file-input'),
        attachBtn: document.getElementById('attach-btn'),
        ytBtn: document.getElementById('yt-btn'),
        promptInput: document.getElementById('prompt-input'),
        sendBtn: document.getElementById('send-btn'),
        stopBtn: document.getElementById('stop-btn'),
        srAnnouncer: document.getElementById('sr-announcer'),

        // Inspector Drawer
        inspectorDrawer: document.getElementById('inspector-drawer'),
        closeInspectorBtn: document.getElementById('close-inspector-btn'),
        metricLatency: document.getElementById('metric-latency'),
        metricTokens: document.getElementById('metric-tokens'),
        metricStatus: document.getElementById('metric-status'),
        codeCurl: document.getElementById('code-curl'),
        codeReq: document.getElementById('code-req'),
        codeRes: document.getElementById('code-res'),
        tabBtns: document.querySelectorAll('.tab-btn'),
        tabPanels: document.querySelectorAll('.tab-panel'),

        // Settings Dialog
        settingsDialog: document.getElementById('settings-dialog'),
        apiKeyInput: document.getElementById('api-key-input'),
        apiModeSelect: document.getElementById('api-mode-select'),
        systemPromptInput: document.getElementById('system-prompt-input'),
        streamToggle: document.getElementById('stream-toggle'),
        saveSettingsBtn: document.getElementById('save-settings-btn'),
        cancelSettingsBtn: document.getElementById('cancel-settings-btn'),
        closeSettingsBtn: document.getElementById('close-settings-btn'),

        // YouTube Dialog
        ytDialog: document.getElementById('yt-dialog'),
        ytUrlInput: document.getElementById('yt-url-input'),
        addYtBtn: document.getElementById('add-yt-btn'),
        cancelYtBtn: document.getElementById('cancel-yt-btn'),
        closeYtBtn: document.getElementById('close-yt-btn')
    };

    /** Announce status to screen readers */
    function announce(text) {
        if (!elements.srAnnouncer) return;
        elements.srAnnouncer.textContent = '';
        setTimeout(() => {
            elements.srAnnouncer.textContent = text;
        }, 50);
    }

    /** Initialize application */
    function init() {
        // Load initial state into inputs
        elements.apiKeyInput.value = state.apiKey;
        elements.apiModeSelect.value = state.apiMode;
        elements.systemPromptInput.value = state.systemPrompt;
        elements.streamToggle.checked = state.streamEnabled;
        elements.effortSelect.value = state.effort;

        bindEvents();
        checkServerHealth();
        fetchModels();
        updateEffortVisibility();
    }

    /** Update effort selector visibility based on selected model */
    function updateEffortVisibility() {
        const model = elements.modelSelect.value;
        const supportsEffort = model.includes('claude') || model.includes('flash') || model.includes('oss');
        elements.effortGroup.style.display = supportsEffort ? 'flex' : 'none';
    }

    /** Bind DOM events */
    function bindEvents() {
        // Model change
        elements.modelSelect.addEventListener('change', () => {
            state.selectedModel = elements.modelSelect.value;
            safeStorage.setItem('agy2api_model', state.selectedModel);
            updateEffortVisibility();
            announce(`تم اختيار النموذج ${state.selectedModel}`);
        });

        // Effort change
        elements.effortSelect.addEventListener('change', () => {
            state.effort = elements.effortSelect.value;
            safeStorage.setItem('agy2api_effort', state.effort);
            announce(`تم تغيير مستوى التفكير إلى ${state.effort}`);
        });

        // New Chat
        elements.newChatBtn.addEventListener('click', () => {
            if (state.messages.length > 0 && confirm('هل تريد بدء محادثة جديدة وتفريغ السجل؟')) {
                resetChat();
            }
        });

        // Prompt input auto-resize & keyboard shortcuts
        elements.promptInput.addEventListener('input', () => {
            elements.promptInput.style.height = 'auto';
            elements.promptInput.style.height = Math.min(elements.promptInput.scrollHeight, 180) + 'px';
        });

        elements.promptInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                if (e.isComposing) return;
                // Desktop/External keyboard: Enter sends, Shift+Enter adds newline
                // Ctrl+Enter or Meta+Enter also sends
                if (e.ctrlKey || e.metaKey || !e.shiftKey) {
                    e.preventDefault();
                    handleSubmit();
                }
            }
        });

        // Form Submission
        document.getElementById('chat-form').addEventListener('submit', (e) => {
            e.preventDefault();
            handleSubmit();
        });

        // Stop generation
        elements.stopBtn.addEventListener('click', () => {
            if (state.abortController) {
                state.abortController.abort();
                announce('تم إيقاف التوليد');
            }
        });

        // Media attachment
        elements.attachBtn.addEventListener('click', () => elements.mediaFileInput.click());
        elements.mediaFileInput.addEventListener('change', handleFileSelection);

        // YouTube dialog
        elements.ytBtn.addEventListener('click', () => {
            elements.ytUrlInput.value = '';
            elements.ytDialog.showModal();
            elements.ytUrlInput.focus();
        });
        elements.addYtBtn.addEventListener('click', handleAddYouTube);
        elements.cancelYtBtn.addEventListener('click', () => elements.ytDialog.close());
        elements.closeYtBtn.addEventListener('click', () => elements.ytDialog.close());

        // Settings Dialog
        elements.openSettingsBtn.addEventListener('click', () => {
            elements.settingsDialog.showModal();
            elements.apiKeyInput.focus();
        });
        elements.closeSettingsBtn.addEventListener('click', () => elements.settingsDialog.close());
        elements.cancelSettingsBtn.addEventListener('click', () => elements.settingsDialog.close());
        elements.saveSettingsBtn.addEventListener('click', handleSaveSettings);

        // Developer Inspector Drawer
        elements.toggleInspectorBtn.addEventListener('click', toggleInspector);
        elements.closeInspectorBtn.addEventListener('click', () => toggleInspector(false));

        // Inspector tabs
        elements.tabBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                const targetPanelId = btn.getAttribute('aria-controls');
                elements.tabBtns.forEach(b => {
                    b.classList.remove('active');
                    b.setAttribute('aria-selected', 'false');
                });
                elements.tabPanels.forEach(p => {
                    p.classList.remove('active');
                    p.hidden = true;
                });
                btn.classList.add('active');
                btn.setAttribute('aria-selected', 'true');
                const activePanel = document.getElementById(targetPanelId);
                if (activePanel) {
                    activePanel.classList.add('active');
                    activePanel.hidden = false;
                }
            });
        });

        // Copy buttons
        document.querySelectorAll('.copy-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const targetId = btn.getAttribute('data-target');
                const targetEl = document.getElementById(targetId);
                if (targetEl) {
                    navigator.clipboard.writeText(targetEl.textContent).then(() => {
                        const originalText = btn.textContent;
                        btn.textContent = 'تم النسخ!';
                        btn.style.color = 'var(--color-accent)';
                        setTimeout(() => {
                            btn.textContent = originalText;
                            btn.style.color = '';
                        }, 2000);
                        announce('تم نسخ المحتوى إلى الحافظة');
                    });
                }
            });
        });

        // Global Escape key
        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                if (elements.settingsDialog.open) elements.settingsDialog.close();
                if (elements.ytDialog.open) elements.ytDialog.close();
                if (!elements.inspectorDrawer.hidden) toggleInspector(false);
            }
        });
    }

    /** Reset chat state and UI */
    function resetChat() {
        state.messages = [];
        state.pendingAttachments = [];
        renderAttachments();
        elements.chatMessages.innerHTML = `
            <article class="welcome-card" aria-label="رسالة ترحيب ومعلومات الاستخدام">
                <h2>مرحباً بك في agy2api Web Studio</h2>
                <p>تم بدء جلسة محادثة جديدة. يمكنك إرسال استفسارك أو إرفاق وسائط الآن.</p>
            </article>
        `;
        announce('تم تفريغ المحادثة والبدء من جديد');
    }

    /** Check server connection health */
    async function checkServerHealth() {
        try {
            const res = await fetch('/health');
            if (res.ok) {
                elements.statusDot.className = 'status-dot status-online';
                elements.statusText.textContent = 'متصل';
                elements.serverStatus.setAttribute('aria-label', 'حالة السيرفر: متصل وجاهز للعمل');
            } else {
                throw new Error('Health check failed');
            }
        } catch {
            elements.statusDot.className = 'status-dot status-offline';
            elements.statusText.textContent = 'غير متصل';
            elements.serverStatus.setAttribute('aria-label', 'حالة السيرفر: غير متصل أو لا يستجيب');
        }
    }

    /** Fetch available models catalog */
    async function fetchModels() {
        try {
            const headers = state.apiKey ? { 'Authorization': `Bearer ${state.apiKey}` } : {};
            const res = await fetch('/v1/models', { headers });
            if (res.ok) {
                const data = await res.json();
                if (data.data && Array.isArray(data.data)) {
                    elements.modelSelect.innerHTML = '';
                    data.data.forEach(m => {
                        const opt = document.createElement('option');
                        opt.value = m.id;
                        opt.textContent = m.id;
                        if (m.id === state.selectedModel) opt.selected = true;
                        elements.modelSelect.appendChild(opt);
                    });
                    state.selectedModel = elements.modelSelect.value;
                    updateEffortVisibility();
                }
            }
        } catch (err) {
            console.debug('Failed to fetch dynamic models list; using defaults', err);
        }
    }

    /** Toggle developer inspector drawer */
    function toggleInspector(forceOpen) {
        const isOpen = typeof forceOpen === 'boolean' ? !forceOpen : elements.inspectorDrawer.hidden;
        elements.inspectorDrawer.hidden = !isOpen;
        elements.toggleInspectorBtn.setAttribute('aria-expanded', String(isOpen));
        if (isOpen) {
            elements.inspectorDrawer.focus();
            announce('تم فتح لوحة فحص الـ API للمطورين');
        } else {
            elements.toggleInspectorBtn.focus();
            announce('تم إغلاق لوحة فحص الـ API');
        }
    }

    /** Handle file selection & Base64 conversion */
    function handleFileSelection(e) {
        const files = Array.from(e.target.files);
        if (!files.length) return;

        files.forEach(file => {
            const reader = new FileReader();
            reader.onload = () => {
                state.pendingAttachments.push({
                    name: file.name,
                    type: file.type || 'application/octet-stream',
                    dataUrl: reader.result,
                    size: (file.size / 1024).toFixed(1) + ' KB'
                });
                renderAttachments();
                announce(`تم إرفاق الملف ${file.name}`);
            };
            reader.readAsDataURL(file);
        });

        elements.mediaFileInput.value = '';
    }

    /** Handle adding YouTube URL */
    function handleAddYouTube() {
        const url = elements.ytUrlInput.value.trim();
        if (!url) return;

        if (!url.includes('youtube.com') && !url.includes('youtu.be')) {
            alert('يرجى إدخال رابط يوتيوب صحيح');
            return;
        }

        state.pendingAttachments.push({
            name: 'فيديو يوتيوب: ' + url,
            type: 'video/youtube',
            url: url
        });

        renderAttachments();
        elements.ytDialog.close();
        announce('تم إرفاق رابط اليوتيوب بنجاح');
    }

    /** Render attachment preview chips */
    function renderAttachments() {
        if (!state.pendingAttachments.length) {
            elements.attachmentsBar.hidden = true;
            elements.attachmentsBar.innerHTML = '';
            return;
        }

        elements.attachmentsBar.hidden = false;
        elements.attachmentsBar.innerHTML = '';

        state.pendingAttachments.forEach((att, idx) => {
            const chip = document.createElement('div');
            chip.className = 'attachment-chip';
            chip.innerHTML = `
                <span>${escapeHtml(att.name)}</span>
                <button type="button" class="attachment-chip-remove" aria-label="حذف المرفق ${escapeHtml(att.name)}">&times;</button>
            `;
            chip.querySelector('.attachment-chip-remove').addEventListener('click', () => {
                state.pendingAttachments.splice(idx, 1);
                renderAttachments();
                announce(`تم حذف المرفق ${att.name}`);
            });
            elements.attachmentsBar.appendChild(chip);
        });
    }

    /** Save settings from dialog */
    function handleSaveSettings() {
        state.apiKey = elements.apiKeyInput.value.trim();
        state.apiMode = elements.apiModeSelect.value;
        state.systemPrompt = elements.systemPromptInput.value.trim();
        state.streamEnabled = elements.streamToggle.checked;

        safeStorage.setItem('agy2api_key', state.apiKey);
        safeStorage.setItem('agy2api_mode', state.apiMode);
        safeStorage.setItem('agy2api_system_prompt', state.systemPrompt);
        safeStorage.setItem('agy2api_stream', state.streamEnabled);

        elements.settingsDialog.close();
        announce('تم حفظ الإعدادات بنجاح');
        fetchModels();
    }

    /** Handle prompt submission */
    async function handleSubmit() {
        if (state.isGenerating) return;

        const text = elements.promptInput.value.trim();
        if (!text && state.pendingAttachments.length === 0) return;

        const attachments = [...state.pendingAttachments];
        state.pendingAttachments = [];
        renderAttachments();

        elements.promptInput.value = '';
        elements.promptInput.style.height = 'auto';

        // Add user message to state & UI
        const userMsg = { role: 'user', content: text, attachments };
        state.messages.push(userMsg);
        renderUserMessage(userMsg);

        // Prepare assistant message in UI
        const assistantMsgEl = renderAssistantPlaceholder();

        // Initiate API request
        state.isGenerating = true;
        elements.sendBtn.hidden = true;
        elements.stopBtn.hidden = false;
        elements.stopBtn.focus();
        announce('جاري إرسال الطلب واستلام الرد...');

        state.abortController = new AbortController();
        const startTime = performance.now();

        try {
            await executeChatRequest(assistantMsgEl, startTime);
        } catch (err) {
            if (err.name === 'AbortError') {
                assistantMsgEl.querySelector('.message-text').innerHTML += '<p class="text-muted"><em>(تم إيقاف التوليد من المستخدم)</em></p>';
                announce('تم إيقاف التوليد');
            } else {
                console.error('Chat request error:', err);
                assistantMsgEl.querySelector('.message-text').innerHTML = `<p style="color: var(--color-danger);">خطأ: ${escapeHtml(err.message)}</p>`;
                announce(`حدث خطأ: ${err.message}`);
                state.lastMetrics.status = 'Error';
                updateInspectorMetrics(performance.now() - startTime, 0, 'Error');
            }
        } finally {
            state.isGenerating = false;
            elements.sendBtn.hidden = false;
            elements.stopBtn.hidden = true;
            elements.promptInput.focus();
        }
    }

    /** Execute OpenAI or Gemini compatible request */
    async function executeChatRequest(assistantMsgEl, startTime) {
        const isGemini = state.apiMode === 'gemini';
        let endpoint = '/v1/chat/completions';
        let payload = {};
        const headers = { 'Content-Type': 'application/json' };
        if (state.apiKey) headers['Authorization'] = `Bearer ${state.apiKey}`;

        if (!isGemini) {
            // OpenAI Format
            const formattedMessages = [];
            if (state.systemPrompt) {
                formattedMessages.push({ role: 'system', content: state.systemPrompt });
            }

            state.messages.forEach(m => {
                if (m.attachments && m.attachments.length > 0) {
                    const parts = [];
                    if (m.content) parts.push({ type: 'text', text: m.content });
                    m.attachments.forEach(att => {
                        if (att.url) {
                            parts.push({ type: 'text', text: `رابط يوتيوب: ${att.url}` });
                        } else if (att.dataUrl) {
                            parts.push({ type: 'image_url', image_url: { url: att.dataUrl } });
                        }
                    });
                    formattedMessages.push({ role: m.role, content: parts });
                } else {
                    formattedMessages.push({ role: m.role, content: m.content });
                }
            });

            payload = {
                model: state.selectedModel,
                messages: formattedMessages,
                stream: state.streamEnabled,
                reasoning_effort: state.effort
            };
        } else {
            // Gemini Format
            endpoint = `/v1beta/models/${state.selectedModel}:generateContent`;
            const contents = state.messages.map(m => {
                const parts = [];
                if (m.content) parts.push({ text: m.content });
                if (m.attachments) {
                    m.attachments.forEach(att => {
                        if (att.url) parts.push({ text: `YouTube: ${att.url}` });
                    });
                }
                return { role: m.role === 'assistant' ? 'model' : 'user', parts };
            });
            payload = { contents };
        }

        // Generate cURL for inspector
        const curlCmd = `curl -X POST "http://127.0.0.1:7862${endpoint}" \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${state.apiKey}" \\\n  -d '${JSON.stringify(payload, null, 2)}'`;
        state.lastPayloads.curl = curlCmd;
        state.lastPayloads.request = payload;
        elements.codeCurl.textContent = curlCmd;
        elements.codeReq.textContent = JSON.stringify(payload, null, 2);

        const res = await fetch(endpoint, {
            method: 'POST',
            headers,
            body: JSON.stringify(payload),
            signal: state.abortController.signal
        });

        if (!res.ok) {
            const errJson = await res.json().catch(() => ({}));
            const errMsg = (errJson.error && errJson.error.message) || `HTTP ${res.status}: ${res.statusText}`;
            throw new Error(errMsg);
        }

        let fullAnswer = '';
        let fullReasoning = '';
        let tokenCount = 0;

        if (state.streamEnabled && res.body) {
            // Real-time SSE Stream Reader
            const reader = res.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';

            const reasoningBox = assistantMsgEl.querySelector('.reasoning-box');
            const reasoningPre = assistantMsgEl.querySelector('.reasoning-content');
            const messageText = assistantMsgEl.querySelector('.message-text');

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop(); // keep last unfinished line

                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed || trimmed.startsWith(':')) continue; // comments/heartbeats
                    if (trimmed === 'data: [DONE]') continue;

                    if (trimmed.startsWith('data: ')) {
                        try {
                            const json = JSON.parse(trimmed.slice(6));
                            state.lastPayloads.response = json;
                            elements.codeRes.textContent = JSON.stringify(json, null, 2);

                            const choice = json.choices && json.choices[0];
                            if (choice && choice.delta) {
                                const delta = choice.delta;
                                if (delta.reasoning_content) {
                                    fullReasoning += delta.reasoning_content;
                                    reasoningBox.hidden = false;
                                    reasoningPre.textContent = fullReasoning;
                                }
                                if (delta.content) {
                                    fullAnswer += delta.content;
                                    messageText.innerHTML = renderMarkdown(fullAnswer);
                                }
                            }
                            if (json.usage && json.usage.total_tokens) {
                                tokenCount = json.usage.total_tokens;
                            }
                        } catch (e) {
                            // Non-json chunk
                        }
                    }
                }
                scrollChatToBottom();
            }
        } else {
            // Non-streaming response
            const json = await res.json();
            state.lastPayloads.response = json;
            elements.codeRes.textContent = JSON.stringify(json, null, 2);

            const choice = json.choices && json.choices[0];
            if (choice && choice.message) {
                fullAnswer = choice.message.content || '';
                fullReasoning = choice.message.reasoning_content || '';
            }
            if (json.usage && json.usage.total_tokens) {
                tokenCount = json.usage.total_tokens;
            }

            const reasoningBox = assistantMsgEl.querySelector('.reasoning-box');
            const reasoningPre = assistantMsgEl.querySelector('.reasoning-content');
            if (fullReasoning) {
                reasoningBox.hidden = false;
                reasoningPre.textContent = fullReasoning;
            }
            assistantMsgEl.querySelector('.message-text').innerHTML = renderMarkdown(fullAnswer);
        }

        const latency = Math.round(performance.now() - startTime);
        state.lastMetrics = { latency, tokens: tokenCount || fullAnswer.length, status: '200 OK' };
        updateInspectorMetrics(latency, tokenCount || fullAnswer.length, '200 OK');

        // Save assistant reply to state
        state.messages.push({ role: 'assistant', content: fullAnswer, reasoning: fullReasoning });
        announce('اكتمل توليد الرد بنجاح');
        scrollChatToBottom();
    }

    /** Update Inspector metric pills */
    function updateInspectorMetrics(latency, tokens, status) {
        elements.metricLatency.textContent = `${latency} ms`;
        elements.metricTokens.textContent = `${tokens} tokens`;
        elements.metricStatus.textContent = status;
    }

    /** Render a user message card */
    function renderUserMessage(msg) {
        const row = document.createElement('div');
        row.className = 'message-row user';

        let attachmentsHtml = '';
        if (msg.attachments && msg.attachments.length) {
            attachmentsHtml = `<div class="attachments-bar" style="margin-bottom: 6px;">` +
                msg.attachments.map(att => `<span class="attachment-chip">${escapeHtml(att.name)}</span>`).join('') +
                `</div>`;
        }

        row.innerHTML = `
            <span class="message-author">أنت</span>
            <div class="message-bubble">
                ${attachmentsHtml}
                <div class="message-text">${escapeHtml(msg.content)}</div>
            </div>
        `;
        elements.chatMessages.appendChild(row);
        scrollChatToBottom();
    }

    /** Render assistant message placeholder */
    function renderAssistantPlaceholder() {
        const row = document.createElement('div');
        row.className = 'message-row assistant';
        row.innerHTML = `
            <span class="message-author">${escapeHtml(state.selectedModel)}</span>
            <div class="message-bubble">
                <details class="reasoning-box" hidden>
                    <summary class="reasoning-summary">
                        <svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
                        <span>خطوات التفكير (Reasoning)</span>
                    </summary>
                    <pre class="reasoning-content"></pre>
                </details>
                <div class="message-text"><em>جاري التفكير والتوليد...</em></div>
            </div>
        `;
        elements.chatMessages.appendChild(row);
        scrollChatToBottom();
        return row;
    }

    /** Scroll chat viewport to bottom smoothly */
    function scrollChatToBottom() {
        elements.chatViewport.scrollTop = elements.chatViewport.scrollHeight;
    }

    /** Simple, safe Markdown to HTML converter */
    function renderMarkdown(text) {
        if (!text) return '';
        let escaped = escapeHtml(text);

        // Code blocks: ```lang\ncode\n```
        escaped = escaped.replace(/```([a-zA-Z0-9_\-\.]*)\n([\s\S]*?)```/g, (match, lang, code) => {
            return `
                <div class="code-container">
                    <div class="code-header">
                        <span>${lang || 'code'}</span>
                        <button type="button" class="btn btn-sm btn-secondary copy-btn" onclick="navigator.clipboard.writeText(this.closest('.code-container').querySelector('code').textContent)">نسخ</button>
                    </div>
                    <pre class="code-body"><code>${code}</code></pre>
                </div>
            `;
        });

        // Inline code `code`
        escaped = escaped.replace(/`([^`]+)`/g, '<code style="background: rgba(255,255,255,0.1); padding: 2px 4px; border-radius: 4px; font-family: var(--font-mono);">$1</code>');

        // Bold **text**
        escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // Italic *text*
        escaped = escaped.replace(/\*([^*]+)\*/g, '<em>$1</em>');

        // Line breaks
        escaped = escaped.replace(/\n/g, '<br>');

        return escaped;
    }

    /** Escape HTML characters */
    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // Launch application
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
