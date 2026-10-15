const MarkedPrompts = { items: [], selected: new Set(), offset: 0, limit: 30, total: 0, request: 0 };

function selectSettingsPane(name) {
    document.querySelectorAll('[data-settings-panel]').forEach(panel => {
        panel.hidden = panel.dataset.settingsPanel !== name;
    });
    document.querySelectorAll('[data-settings-target]').forEach(button => {
        const active = button.dataset.settingsTarget === name;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
    });
    if (name === 'marked') loadMarkedPrompts();
}

function updateSettingsState(text, dirty = false) {
    const label = document.getElementById('settingsSaveState');
    label.textContent = text;
    label.classList.toggle('dirty', dirty);
}

document.getElementById('configForm').addEventListener('input', event => {
    if (!event.target.matches('.config-input, .config-checkbox') ||
        event.target.closest('[data-settings-panel="diagnostics"]')) return;
    AppState.settingsRevision = (AppState.settingsRevision || 0) + 1;
    updateSettingsState('有未保存的修改', true);
});

function updateMarkedPromptSelection() {
    const selected = MarkedPrompts.selected;
    document.getElementById('markedPromptSelection').textContent = `已选 ${selected.size} 项`;
    document.getElementById('markedPromptDownload').disabled = selected.size === 0;
    const checked = MarkedPrompts.items.filter(item => selected.has(item.id)).length;
    const all = document.getElementById('markedPromptSelectAll');
    all.checked = checked > 0 && checked === MarkedPrompts.items.length;
    all.indeterminate = checked > 0 && checked < MarkedPrompts.items.length;
    all.disabled = MarkedPrompts.items.length === 0;
}

function toggleMarkedPromptPage(checked) {
    MarkedPrompts.items.forEach(item => {
        if (checked && MarkedPrompts.selected.size < 100) MarkedPrompts.selected.add(item.id);
        if (!checked) MarkedPrompts.selected.delete(item.id);
    });
    renderMarkedPrompts();
}

function renderMarkedPrompts() {
    const list = document.getElementById('markedPromptList');
    list.replaceChildren();
    if (!MarkedPrompts.items.length) {
        const empty = document.createElement('div');
        empty.className = 'marked-empty';
        empty.innerHTML = `${panelIcon('shield')}<strong>暂未记录被标记的提示词</strong><p>触发指定错误后会自动记录，无需手动维护。</p>`;
        list.append(empty);
    }
    MarkedPrompts.items.forEach(item => {
        const row = document.createElement('article');
        row.className = 'marked-row';
        const label = document.createElement('label');
        label.className = 'marked-row-select';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = MarkedPrompts.selected.has(item.id);
        checkbox.setAttribute('aria-label', `选择提示词 ${item.id.slice(0, 12)}`);
        checkbox.addEventListener('change', () => {
            if (checkbox.checked && MarkedPrompts.selected.size >= 100) {
                checkbox.checked = false;
                showStatus('每次最多选择 100 条提示词', 'info');
                return;
            }
            if (checkbox.checked) MarkedPrompts.selected.add(item.id);
            else MarkedPrompts.selected.delete(item.id);
            updateMarkedPromptSelection();
        });
        const content = document.createElement('div');
        content.className = 'marked-row-content';
        const title = document.createElement('strong');
        title.textContent = `提示词 ${item.id.slice(0, 12)}`;
        const preview = document.createElement('p');
        preview.className = 'marked-preview';
        preview.textContent = item.preview;
        const metadata = document.createElement('small');
        metadata.textContent = `${item.chars.toLocaleString()} 字符 · ${item.model} · ${new Date(item.created_at * 1000).toLocaleString()}`;
        content.append(title, preview, metadata);
        label.append(checkbox, content);
        const download = document.createElement('button');
        download.type = 'button';
        download.className = 'refresh-btn';
        download.textContent = '下载 TXT';
        download.addEventListener('click', () => downloadMarkedPrompts([item.id]));
        row.append(label, download);
        list.append(row);
    });
    document.getElementById('markedPromptTotal').textContent = `共 ${MarkedPrompts.total} 条 · 第 ${Math.floor(MarkedPrompts.offset / MarkedPrompts.limit) + 1} 页`;
    document.getElementById('markedPromptNavCount').textContent = MarkedPrompts.total;
    document.getElementById('markedPromptPrev').disabled = MarkedPrompts.offset === 0;
    document.getElementById('markedPromptNext').disabled = MarkedPrompts.offset + MarkedPrompts.limit >= MarkedPrompts.total;
    updateMarkedPromptSelection();
}

async function loadMarkedPrompts() {
    const request = ++MarkedPrompts.request;
    const list = document.getElementById('markedPromptList');
    list.setAttribute('aria-busy', 'true');
    try {
        const response = await fetch(`./marked-prompts?offset=${MarkedPrompts.offset}&limit=${MarkedPrompts.limit}`, { headers: getAuthHeaders(), cache: 'no-store' });
        if (!response.ok) throw new Error('列表加载失败，请重试');
        const data = await response.json();
        if (request !== MarkedPrompts.request) return;
        MarkedPrompts.items = data.items;
        MarkedPrompts.total = data.total;
        renderMarkedPrompts();
    } catch (error) {
        if (request === MarkedPrompts.request) {
            list.textContent = error.message;
            showStatus(error.message, 'error');
        }
    } finally {
        if (request === MarkedPrompts.request) list.setAttribute('aria-busy', 'false');
    }
}

function changeMarkedPromptPage(direction) {
    MarkedPrompts.offset = Math.max(0, MarkedPrompts.offset + direction * MarkedPrompts.limit);
    loadMarkedPrompts();
}

async function downloadMarkedPrompts(ids = [...MarkedPrompts.selected]) {
    if (!ids.length) return;
    const button = document.getElementById('markedPromptDownload');
    button.disabled = true;
    try {
        const single = ids.length === 1;
        const response = await fetch(single ? `./marked-prompts/${ids[0]}/download` : './marked-prompts/download', {
            method: single ? 'GET' : 'POST', headers: getAuthHeaders(),
            ...(single ? {} : { body: JSON.stringify({ ids }) }),
        });
        if (!response.ok) throw new Error('下载失败，请刷新列表后重试');
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement('a');
        link.href = url;
        link.download = single ? `prompt-${ids[0].slice(0, 12)}.txt` : 'marked-prompts.zip';
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
        showStatus(error.message, 'error');
    } finally {
        updateMarkedPromptSelection();
    }
}
