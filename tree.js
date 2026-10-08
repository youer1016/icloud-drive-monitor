(() => {
  const section = document.getElementById('localTreeSection');
  if (!section) return;
  section.innerHTML = `
    <h2 id="tree-heading">本机已下载的 iCloud Drive 内容</h2>
    <div class="group">
      <div class="tree-controls">
        <p>手动扫描文件名和本地驻留标记；扫描线程禁止云端占位项目实体化。</p>
        <div class="tree-actions">
          <button class="tree-action primary" id="startTree" type="button">扫描本机元数据</button>
          <button class="tree-action" id="stopTree" type="button" disabled>停止</button>
        </div>
      </div>
      <div class="tree-progress" id="treeProgress" role="status">尚未扫描。打开页面不会遍历 iCloud Drive。</div>
      <div class="tree-filter-bar">
        <label><input id="onlyAllocated" type="checkbox" checked> 只显示有本机占用的文件（略过 0 B）</label>
        <span id="treeSelectionCount">未选择文件</span>
        <button class="tree-action" id="evictSelected" type="button" disabled>移除所选本地下载</button>
      </div>
      <div class="tree-depth-bar" role="group" aria-label="文件夹展开层级">
        <span>展开层级</span>
        <button class="tree-depth-button" type="button" data-depth="1" disabled>一级</button>
        <button class="tree-depth-button" type="button" data-depth="2" disabled>二级</button>
        <button class="tree-depth-button" type="button" data-depth="3" disabled>三级</button>
        <button class="tree-depth-button" type="button" data-collapse disabled>全部收起</button>
      </div>
      <div class="tree-results" id="treeResults" aria-label="iCloud Drive 本机内容树"></div>
      <div class="tree-evict-results" id="treeEvictResults" role="status" hidden></div>
    </div>
    <p class="footnote">文件夹大小是扫描时确认的本机分配量，已选大小为估算值。Finder 图标与扫描结果都可能滞后；处理前会重新核对本机占位状态。“无法确认归属”的项目会跳过，可点击结果路径在 Finder 核对。0 B 与未枚举项目不会成为移除目标；已确认的批量任务在关闭页面后仍会继续。</p>`;

  const start = document.getElementById('startTree');
  const stop = document.getElementById('stopTree');
  const progress = document.getElementById('treeProgress');
  const results = document.getElementById('treeResults');
  const filter = document.getElementById('onlyAllocated');
  const selectionLabel = document.getElementById('treeSelectionCount');
  const evictButton = document.getElementById('evictSelected');
  const evictionResults = document.getElementById('treeEvictResults');
  const depthButtons = [...document.querySelectorAll('.tree-depth-button')];
  const selected = new Map();
  let scanId = null;
  let scanTimer = null;
  let evictionTimer = null;
  let evictionRunning = false;
  let expanding = false;
  let treeRevision = 0;

  function formatBytes(value) {
    if (value < 1024) return `${value} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let index = -1;
    do { value /= 1024; index += 1; } while (value >= 1024 && index < units.length - 1);
    return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${units[index]}`;
  }
  async function json(url, options) {
    const response = await fetch(url, {cache: 'no-store', ...options});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }
  function setSelection(path, bytes, checked) {
    if (path) {
      if (checked && bytes > 0 && !selected.has(path) && selected.size >= 200) {
        progress.textContent = '一次最多选择 200 个文件，请分批处理。';
      } else if (checked && bytes > 0) selected.set(path, bytes);
      else selected.delete(path);
    }
    refreshSelection();
  }
  function refreshSelection() {
    document.querySelectorAll('.tree-select-file').forEach(input => {
      input.checked = selected.has(input.dataset.path);
    });
    const folderTotals = new Map();
    for (const [path, bytes] of selected) {
      let parent = path.lastIndexOf('/');
      while (parent >= 0) {
        const relative = path.slice(0, parent);
        const current = folderTotals.get(relative) || {count: 0, bytes: 0};
        current.count += 1; current.bytes += bytes;
        folderTotals.set(relative, current);
        parent = path.lastIndexOf('/', parent - 1);
      }
    }
    results.querySelectorAll('details.tree-folder').forEach(folder => {
      const current = folderTotals.get(folder.dataset.path) || {count: 0, bytes: 0};
      const checkbox = folder.querySelector(':scope > summary .tree-select-folder');
      const eligible = Number(checkbox.dataset.eligible);
      checkbox.checked = eligible > 0 && current.count === eligible;
      checkbox.indeterminate = current.count > 0 && current.count < eligible;
      const status = folder.querySelector(':scope > summary .tree-selected-summary');
      status.textContent = current.count ? `已选 ${current.count} 个 · ${formatBytes(current.bytes)}` : '';
      status.hidden = !current.count;
    });
    const total = [...selected.values()].reduce((sum, amount) => sum + amount, 0);
    selectionLabel.textContent = selected.size
      ? `已选 ${selected.size} 个文件 · 预计本机占用 ${formatBytes(total)}`
      : '未选择文件';
    evictButton.disabled = !selected.size || evictionRunning;
  }
  async function toggleFolderSelection(item, checkbox) {
    if (!checkbox.checked) {
      for (const path of selected.keys()) {
        if (path.startsWith(`${item.relative}/`)) selected.delete(path);
      }
      refreshSelection();
      return;
    }
    checkbox.disabled = true;
    try {
      const data = await json(`/api/scan/files?prefix=${encodeURIComponent(item.relative)}`);
      if (data.error) throw new Error(data.error);
      const newCount = data.files.filter(file => !selected.has(file.path)).length;
      if (data.count > 200 || newCount + selected.size > 200) {
        throw new Error('一次最多选择 200 个文件，请展开文件夹分批选择');
      }
      data.files.forEach(file => selected.set(file.path, file.bytes));
      refreshSelection();
    } catch (error) {
      progress.textContent = `选择失败：${error.message}`;
      refreshSelection();
    } finally { checkbox.disabled = (item.selectable_files ?? item.files) <= 0; }
  }
  function setDepthButtons(enabled) {
    depthButtons.forEach(button => { button.disabled = !enabled || expanding; });
  }
  async function revealInFinder(item) {
    try {
      const response = await json('/api/reveal', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: item.relative})
      });
      if (!response.ok) throw new Error(response.error || 'Finder 未能定位此项目');
      progress.textContent = `已在 Finder 中定位：iCloud Drive/${item.relative}`;
    } catch (error) { progress.textContent = `Finder 定位失败：${error.message}`; }
  }
  function finderButton(item) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'tree-inline-action tree-reveal';
    button.textContent = '在 Finder 中显示';
    button.title = `在 Finder 中显示 iCloud Drive/${item.relative}`;
    button.setAttribute('aria-label', button.title);
    button.addEventListener('click', event => {
      event.preventDefault(); event.stopPropagation();
      revealInFinder(item);
    });
    return button;
  }
  function folderRow(item) {
    if (item.unscanned) {
      const row = document.createElement('div'); row.className = 'tree-unscanned';
      const name = document.createElement('span'); name.className = 'tree-name'; name.textContent = item.name;
      const state = document.createElement('span'); state.className = 'tree-size'; state.textContent = '未枚举';
      row.append(name, state); return row;
    }
    const details = document.createElement('details'); details.className = 'tree-folder';
    details.dataset.path = item.relative;
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    checkbox.className = 'tree-select-folder';
    checkbox.dataset.eligible = String(item.selectable_files ?? item.files);
    checkbox.disabled = (item.selectable_files ?? item.files) <= 0;
    checkbox.setAttribute('aria-label', `选择 ${item.name} 内已下载的文件`);
    checkbox.addEventListener('click', event => event.stopPropagation());
    checkbox.addEventListener('change', () => toggleFolderSelection(item, checkbox));
    const summary = document.createElement('summary');
    const label = document.createElement('span'); label.className = 'tree-folder-label';
    const name = document.createElement('span'); name.className = 'tree-name'; name.textContent = item.name; name.title = item.relative;
    const selectedStatus = document.createElement('span'); selectedStatus.className = 'tree-selected-summary'; selectedStatus.hidden = true;
    label.append(name, selectedStatus);
    const size = document.createElement('span'); size.className = 'tree-size'; size.textContent = formatBytes(item.local_bytes);
    const count = document.createElement('span'); count.className = 'tree-count'; count.textContent = `${item.files} 个本地文件`;
    summary.append(checkbox, label, size, count);
    const actions = document.createElement('div'); actions.className = 'tree-folder-actions';
    actions.append(finderButton(item));
    const children = document.createElement('div'); children.className = 'tree-children';
    details.append(summary, actions, children);
    details.addEventListener('toggle', () => {
      if (details.open) ensureLoaded(details);
    });
    return details;
  }
  function fileRow(item) {
    const row = document.createElement('div'); row.className = 'tree-file';
    const label = document.createElement('label'); label.className = 'tree-file-label';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    checkbox.className = 'tree-select-file'; checkbox.dataset.path = item.relative;
    checkbox.checked = selected.has(item.relative); checkbox.disabled = item.local_bytes <= 0;
    checkbox.addEventListener('change', () => setSelection(item.relative, item.local_bytes, checkbox.checked));
    const name = document.createElement('span'); name.className = 'tree-name';
    name.textContent = item.name; name.title = item.relative;
    label.append(checkbox, name);
    const size = document.createElement('span'); size.className = 'tree-size'; size.textContent = formatBytes(item.local_bytes);
    const action = document.createElement('button'); action.type = 'button'; action.className = 'tree-inline-action';
    action.textContent = '移除本地'; action.disabled = item.local_bytes <= 0;
    action.setAttribute('aria-label', `移除 ${item.name} 的本地下载`);
    action.addEventListener('click', () => beginEviction([item.relative], item.local_bytes));
    const actions = document.createElement('div'); actions.className = 'tree-file-actions';
    actions.append(finderButton(item), action);
    row.append(label, size, actions); return row;
  }
  function makeRow(item) {
    if (filter.checked && item.local_bytes <= 0) return null;
    return item.directory ? folderRow(item) : fileRow(item);
  }
  async function loadFolder(path, host) {
    host.textContent = '正在读取已完成的扫描结果…';
    try {
      const data = await json(`/api/scan/tree?path=${encodeURIComponent(path)}`);
      if (data.error) throw new Error(data.error);
      const nodes = data.children.map(makeRow).filter(Boolean);
      host.replaceChildren(...nodes);
      refreshSelection();
      if (!nodes.length) host.textContent = '当前筛选条件下没有可显示的项目。';
      return true;
    } catch (error) { host.textContent = `无法显示：${error.message}`; return false; }
  }
  function ensureLoaded(details) {
    details.open = true;
    if (details.dataset.loaded) return Promise.resolve();
    if (details._loadPromise) return details._loadPromise;
    const host = details.querySelector(':scope > .tree-children');
    details._loadPromise = loadFolder(details.dataset.path, host).then(ok => {
      if (ok) details.dataset.loaded = '1';
    }).finally(() => { details._loadPromise = null; });
    return details._loadPromise;
  }
  async function expandTo(depth) {
    if (!scanId || expanding) return;
    expanding = true; setDepthButtons(true);
    results.querySelectorAll('details.tree-folder[open]').forEach(folder => { folder.open = false; });
    const revision = treeRevision;
    let hosts = [results];
    let opened = 0;
    try {
      for (let level = 1; level <= depth; level += 1) {
        if (revision !== treeRevision) return;
        const folders = hosts.flatMap(host => [...host.children].filter(child => child.matches('details.tree-folder')));
        if (opened + folders.length > 500) throw new Error('文件夹过多，请手动展开部分目录');
        for (let index = 0; index < folders.length; index += 12) {
          if (revision !== treeRevision) return;
          await Promise.all(folders.slice(index, index + 12).map(ensureLoaded));
        }
        opened += folders.length;
        hosts = folders.map(folder => folder.querySelector(':scope > .tree-children'));
      }
      progress.textContent = `已展开至第 ${depth} 级文件夹（${opened} 个）。`;
    } catch (error) { progress.textContent = `展开中止：${error.message}`; }
    finally { expanding = false; setDepthButtons(Boolean(scanId)); }
  }
  async function updateScan(forceReload = false) {
    try {
      const state = await json('/api/scan/status');
      if (state.phase === 'running') {
        start.disabled = true; stop.disabled = false;
        setDepthButtons(false);
        progress.textContent = `已检查 ${state.items.toLocaleString()} 项；确认 ${state.files.toLocaleString()} 个本地文件；跳过 ${(state.unknown_folders ?? 0).toLocaleString()} 个未展开文件夹。`;
        scanTimer ||= setInterval(updateScan, 900);
      } else {
        if (scanTimer) clearInterval(scanTimer); scanTimer = null;
        start.disabled = evictionRunning; stop.disabled = true;
        if (state.phase === 'complete') {
          const changed = scanId !== state.scan_id;
          scanId = state.scan_id;
          if (changed) selected.clear();
          setSelection('', 0, false);
          setDepthButtons(true);
          progress.textContent = `完成于 ${state.scanned_at} · ${state.files.toLocaleString()} 个本地文件 · 已分配 ${formatBytes(state.local_bytes)} · ${(state.unknown_folders ?? 0).toLocaleString()} 个文件夹未枚举`;
          if (changed || forceReload) {
            treeRevision += 1;
            await loadFolder('', results);
          }
        } else if (state.phase === 'cancelled') progress.textContent = '扫描已停止，未保存不完整结果。';
        else if (state.phase === 'error') progress.textContent = `扫描失败：${state.error}`;
      }
    } catch (error) { progress.textContent = `读取扫描状态失败：${error.message}`; }
  }
  function showEvictionResults(state) {
    evictionResults.hidden = false;
    evictionResults.replaceChildren();
    const heading = document.createElement('strong');
    const alreadyAbsent = (state.results || []).filter(item => item.status === 'already_absent').length;
    const failed = (state.results || []).filter(item => !item.ok).length;
    heading.textContent = `已处理 ${state.done}/${state.total} 个 · 已移除 ${state.succeeded} 个 · 已无本地副本 ${alreadyAbsent} 个 · 未移除 ${failed} 个`;
    evictionResults.append(heading);
    for (const item of state.results || []) {
      const line = document.createElement('div');
      line.className = item.ok ? 'tree-success' : 'tree-failure';
      const outcome = document.createElement('span');
      outcome.textContent = item.status === 'already_absent' ? '已无本地副本' : item.ok ? '已移除' : '未移除';
      const path = document.createElement('button');
      path.type = 'button'; path.className = 'tree-result-path';
      path.textContent = item.path;
      path.title = `在 Finder 中显示 iCloud Drive/${item.path}`;
      path.setAttribute('aria-label', path.title);
      path.addEventListener('click', () => revealInFinder({relative: item.path}));
      const message = document.createElement('span'); message.textContent = item.message;
      line.append(outcome, path, message);
      evictionResults.append(line);
    }
  }
  async function updateEviction() {
    try {
      const state = await json('/api/evict/status');
      if (state.phase === 'running') {
        evictionRunning = true; start.disabled = true; evictButton.disabled = true;
        showEvictionResults(state);
        evictionTimer ||= setInterval(updateEviction, 900);
      } else {
        if (evictionTimer) clearInterval(evictionTimer); evictionTimer = null;
        evictionRunning = false; start.disabled = false;
        if (state.phase === 'complete') {
          showEvictionResults(state);
          (state.results || []).filter(item => item.ok).forEach(item => selected.delete(item.path));
          setSelection('', 0, false);
          await updateScan(true);
        }
      }
    } catch (error) { evictionResults.hidden = false; evictionResults.textContent = `读取移除结果失败：${error.message}`; }
  }
  async function beginEviction(paths, totalBytes) {
    if (!scanId || !paths.length || evictionRunning) return;
    const message = `将移除 ${paths.length} 个文件的本地下载，预计涉及 ${formatBytes(totalBytes)}。\n\niCloud 中的文件会保留；程序会尝试取消原有“保留下载”标记。归属或上传状态无法确认、文件正被使用时会跳过。批量任务启动后，即使关闭页面也会继续。\n\n继续吗？`;
    if (!window.confirm(message)) return;
    evictionResults.hidden = false;
    evictionResults.textContent = '正在提交移除请求…';
    try {
      const state = await json('/api/evict/start', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: scanId, paths})
      });
      if (state.phase === 'error') throw new Error(state.error);
      await updateEviction();
    } catch (error) { evictionResults.textContent = `无法开始移除：${error.message}`; }
  }

  start.addEventListener('click', async () => {
    results.replaceChildren(); selected.clear(); scanId = null; treeRevision += 1;
    setSelection('', 0, false); setDepthButtons(false);
    start.disabled = true; progress.textContent = '正在开始元数据扫描…';
    try {
      const state = await json('/api/scan/start', {method: 'POST'});
      if (state.phase === 'error') throw new Error(state.error);
      await updateScan();
    } catch (error) { start.disabled = false; progress.textContent = `启动扫描失败：${error.message}`; }
  });
  stop.addEventListener('click', async () => {
    stop.disabled = true;
    try { await json('/api/scan/cancel', {method: 'POST'}); progress.textContent = '正在停止扫描…'; }
    catch (error) { progress.textContent = `停止失败：${error.message}`; }
  });
  filter.addEventListener('change', () => {
    if (scanId) { treeRevision += 1; loadFolder('', results); }
  });
  depthButtons.forEach(button => button.addEventListener('click', () => {
    if (button.dataset.collapse !== undefined) {
      results.querySelectorAll('details.tree-folder[open]').forEach(folder => { folder.open = false; });
      progress.textContent = '已收起所有文件夹。';
    } else expandTo(Number(button.dataset.depth));
  }));
  evictButton.addEventListener('click', () => {
    const files = [...selected];
    if (files.length > 200) { progress.textContent = '一次最多处理 200 个文件，请分批选择。'; return; }
    beginEviction(files.map(([path]) => path), files.reduce((sum, [, size]) => sum + size, 0));
  });
  updateScan();
  updateEviction();
  window.addEventListener('pagehide', () => {
    if (scanTimer) clearInterval(scanTimer);
    if (evictionTimer) clearInterval(evictionTimer);
  });
})();
