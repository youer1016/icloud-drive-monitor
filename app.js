(() => {
  const rows = document.getElementById('eventRows');
  const raw = document.getElementById('rawLog');
  const paths = new Set();
  let events = [];
  const text = (id, value) => { document.getElementById(id).textContent = value; };
  const escapeHtml = value => String(value).replace(/[&<>'"]/g, character =>
    ({'&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'}[character]));
  function stateLabel(state) {
    if (state === 'T') return ['已暂停', 'paused'];
    if (state === 'absent') return ['未找到', 'absent'];
    return ['运行中', 'running'];
  }
  function render() {
    text('pathCount', String(paths.size));
    if (!events.length) return;
    rows.innerHTML = events.slice(0, 250).map(event =>
      `<tr><td class="time">${escapeHtml(event.time)}</td>` +
      `<td class="process">${escapeHtml(event.process || '未知')}</td>` +
      `<td class="operation">${escapeHtml(event.operation)}</td>` +
      `<td class="kind ${event.kind === '写入候选' ? 'write' : ''}">${escapeHtml(event.kind)}</td>` +
      `<td class="path"><button type="button" class="copy" data-path="${encodeURIComponent(event.path)}" title="复制完整路径">${escapeHtml(event.path)}</button></td></tr>`
    ).join('');
    raw.textContent = events.slice(0, 100).map(event => event.raw).join('\n');
    rows.querySelectorAll('.copy').forEach(button => button.addEventListener('click', async () => {
      await navigator.clipboard.writeText(decodeURIComponent(button.dataset.path));
      button.title = '已复制';
    }));
  }
  async function refresh() {
    try {
      const response = await fetch('/api/status', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const [label, klass] = stateLabel(data.bird.state);
      text('processState', label);
      text('pid', data.bird.pid ? `PID ${data.bird.pid} · ${data.bird.state}` : 'bird 进程未运行');
      document.getElementById('stateDot').className = `dot ${klass}`;
      text('listening', data.monitoring ? '正在监听' : '等待连接');
      text('updated', `更新于 ${data.server_time}`);
      if (data.bird.state === 'T') {
        text('answerTitle', 'bird 当前已暂停');
        text('answerText', '同步进程恢复后才能继续观察新的文件系统事件。');
      }
      if (data.events?.length) {
        events = data.events.slice().reverse();
        events.forEach(event => paths.add(event.path));
        render();
      }
    } catch {
      const notice = document.getElementById('notice');
      notice.textContent = '无法连接本机监视服务。请通过桌面的启动器打开页面。';
      notice.classList.add('show');
    }
  }
  const source = new EventSource('/events');
  source.addEventListener('event', message => {
    const event = JSON.parse(message.data);
    events.unshift(event);
    paths.add(event.path);
    text('answerTitle', event.kind === '写入候选' ? '发现文件写入候选' : '发现文件路径活动');
    text('answerText', event.path);
    render();
  });
  source.onerror = () => {
    text('listening', '连接中断');
    const notice = document.getElementById('notice');
    notice.textContent = '监听连接已中断；刷新页面可重新连接。';
    notice.classList.add('show');
  };
  window.addEventListener('pagehide', () => source.close());
  refresh();
  setInterval(refresh, 4000);
})();
