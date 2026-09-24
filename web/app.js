'use strict';

const $ = (id) => document.getElementById(id);
const state = { config: null, files: [], queue: [], authenticated: false, processing: false, filesLoading: false, refreshSequence: 0, dragDepth: 0, deleteTarget: null, toastTimer: null, authGeneration: 0, renderedSignature: null };
const busyStates = new Set(['queued', 'uploading', 'retrying', 'finishing']);
const byteFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });
const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });

class ApiError extends Error {
  constructor(message, status = 0) { super(message); this.name = 'ApiError'; this.status = status; }
}

function formatBytes(value) {
  let number = Math.max(0, Number(value) || 0), unit = 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit++; }
  return `${byteFormat.format(number)} ${units[unit]}`;
}

function icon(name) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  element.classList.add('icon'); element.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`); element.append(use); return element;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function toast(message) {
  clearTimeout(state.toastTimer); $('toast').textContent = message; $('toast').hidden = false;
  state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}

function connection(mode, message) {
  $('connection').className = `connection ${mode}`; $('connectionText').textContent = message;
}

function showView(view) {
  for (const name of ['loadingView', 'loginView', 'appView']) $(name).hidden = name !== view;
  $('logoutButton').hidden = view !== 'appView' || !state.config?.auth_required;
}

function loseSession() {
  if (state.authenticated) {
    state.authenticated = false; state.authGeneration++;
    for (const item of state.queue) {
      if (busyStates.has(item.status)) {
        item.status = 'error'; item.error = '登录已过期，重新登录后点击继续'; item.controller?.abort(); item.xhr?.abort(); renderQueueItem(item);
      }
    }
  }
  if ($('deleteDialog').open) $('deleteDialog').close('cancel');
  if ($('detailsDialog').open) $('detailsDialog').close();
  $('accessCode').value = ''; showView('loginView'); connection('online', '等待验证');
}

async function api(path, options = {}) {
  const { timeoutMs = 30000, ...requestOptions } = options;
  const controller = new AbortController(), externalSignal = requestOptions.signal;
  let timedOut = false;
  const abort = () => controller.abort();
  if (externalSignal?.aborted) controller.abort();
  else externalSignal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  try {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...requestOptions, signal: controller.signal });
    let data;
    try { data = await response.json(); } catch (error) { if (controller.signal.aborted) throw error; data = {}; }
    if (!response.ok) {
      if (response.status === 401 && path !== '/api/login') loseSession();
      throw new ApiError(data.error || `请求失败（${response.status}）`, response.status);
    }
    return data;
  } catch (error) {
    if (timedOut) throw new ApiError('请求超时，请稍后重试');
    if (error instanceof ApiError || error.name === 'AbortError') throw error;
    throw new ApiError('网络连接失败，请检查网络后重试');
  } finally { clearTimeout(timer); externalSignal?.removeEventListener('abort', abort); }
}

function jsonRequest(method, value, signal) {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value), signal };
}

function applyConfig(config) {
  state.config = config;
  const limit = config.max_file_size ? `单个文件最大 ${formatBytes(config.max_file_size)}。` : '';
  $('uploadHint').textContent = `${limit}分片上传，网络中断后可继续。`;
  $('cleanupInfo').textContent = (config.cleanup_time ? `每天 ${config.cleanup_time} 清理已完成文件（服务器时间）。` : '未启用定时清理，管理员可随时移除文件。') + '未完成上传闲置 2 小时后清理。';
  $('endpointInfo').textContent = config.public_url ? `分享入口 · ${config.public_url}` : isLoopback() ? '仅本机访问 · 跨网络请使用公网启动器提供的链接' : isPrivateNetwork() ? '局域网入口 · 跨网络请使用公网链接' : `当前入口 · ${location.host}`;
  $('endpointInfo').title = config.public_url || location.origin;
}

async function initialize() {
  $('globalError').hidden = true; $('reconnectButton').hidden = true; showView('loadingView');
  $('loadingView').querySelector('.spinner').hidden = false;
  $('loadingView').querySelector('h1').textContent = '连接文件中转站'; $('loadingView').querySelector('p').textContent = '正在检查连接状态…';
  connection('', '正在连接');
  try {
    const config = await api('/api/status'); applyConfig(config);
    state.authenticated = Boolean(config.authenticated || !config.auth_required);
    if (state.authenticated) { showView('appView'); connection('online', '已连接'); await loadFiles(); }
    else { showView('loginView'); connection('online', '等待验证'); $('accessCode').focus(); }
  } catch (error) {
    connection('offline', '连接失败'); $('globalError').textContent = error.message; $('globalError').hidden = false; $('reconnectButton').hidden = false;
    $('loadingView').querySelector('.spinner').hidden = true;
    $('loadingView').querySelector('h1').textContent = '暂时无法连接'; $('loadingView').querySelector('p').textContent = '请确认中转站已启动，然后重试。';
  }
}

async function login(event) {
  event.preventDefault(); const button = $('loginButton'); button.disabled = true; $('loginError').hidden = true;
  try {
    await api('/api/login', jsonRequest('POST', { code: $('accessCode').value }));
    $('accessCode').value = '';
    const config = await api('/api/status'); applyConfig(config);
    state.authenticated = true; state.authGeneration++; showView('appView'); connection('online', '已连接');
    await loadFiles(); $('deviceName').focus();
  } catch (error) { $('loginError').textContent = error.message; $('loginError').hidden = false; }
  finally { button.disabled = false; }
}

async function logout() {
  $('logoutButton').disabled = true;
  try { await api('/api/logout', { method: 'POST' }); loseSession(); state.files = []; renderFiles(); toast('已退出文件中转站'); }
  catch (error) { toast(error.message); }
  finally { $('logoutButton').disabled = false; }
}

async function loadFiles({ quiet = false } = {}) {
  if (!state.authenticated) return;
  const sequence = ++state.refreshSequence, generation = state.authGeneration;
  state.filesLoading = true; $('refreshButton').disabled = true; $('filesError').hidden = true;
  if (!quiet && !state.files.length) { $('filesLoading').hidden = false; $('filesEmpty').hidden = true; $('fileTable').hidden = true; }
  try {
    const data = await api('/api/files');
    if (sequence !== state.refreshSequence || generation !== state.authGeneration || !state.authenticated) return;
    state.files = Array.isArray(data.files) ? data.files : [];
    const used = Number(data.used_bytes) || 0, total = Number(data.storage_limit) || 0;
    $('storageText').textContent = total ? `已用 ${formatBytes(used)} / ${formatBytes(total)}` : `已存储 ${formatBytes(used)}`;
    const percent = total ? Math.min(100, used / total * 100) : 0;
    $('storageMeter').hidden = !total; $('storageMeter').setAttribute('aria-valuenow', String(Math.round(percent)));
    $('storageMeter').setAttribute('aria-valuetext', $('storageText').textContent); $('storageMeter').classList.toggle('full', percent >= 90); $('storageBar').style.width = `${percent}%`;
    $('lastUpdated').textContent = `${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} 已更新`;
    connection(navigator.onLine ? 'online' : 'offline', navigator.onLine ? '已连接' : '网络已断开');
  } catch (error) {
    if (sequence !== state.refreshSequence) return;
    if (error.status !== 401) { $('filesError').textContent = `${error.message}。可点击右上角刷新重试。`; $('filesError').hidden = false; connection('offline', '连接异常'); }
  } finally {
    if (sequence === state.refreshSequence) { state.filesLoading = false; $('refreshButton').disabled = false; $('filesLoading').hidden = true; renderFiles(); }
  }
}

function fileDate(value) {
  const date = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function renderFiles() {
  const query = $('searchInput').value.trim().toLocaleLowerCase();
  const files = state.files.filter((file) => `${file.original_name} ${file.owner || ''}`.toLocaleLowerCase().includes(query));
  const sort = $('sortSelect').value;
  files.sort((a, b) => sort === 'name' ? collator.compare(a.original_name, b.original_name) : sort === 'size' ? b.size - a.size : ((fileDate(a.created_at)?.getTime() || 0) - (fileDate(b.created_at)?.getTime() || 0)) * (sort === 'oldest' ? 1 : -1));
  $('fileCount').textContent = query ? `${files.length} / ${state.files.length}` : String(state.files.length);
  $('filesEmpty').hidden = Boolean(files.length) || state.filesLoading || !$('filesError').hidden;
  $('fileTable').hidden = !files.length;
  $('emptyTitle').textContent = query ? '没有找到相关文件' : '这里，等你的第一份文件';
  $('emptyDescription').textContent = query ? '换一个文件名或设备名称试试。' : '选择或拖入文件，就能在其他设备上下载。';
  const signature = JSON.stringify(files);
  if (state.renderedSignature === signature) return;
  state.renderedSignature = signature;
  const focusedRow = document.activeElement?.closest('.file-row');
  const focusedId = focusedRow?.dataset.fileId, focusedAction = document.activeElement?.dataset.action;
  $('fileList').replaceChildren();
  const fragment = document.createDocumentFragment();
  for (const file of files) {
    const row = node('div', 'file-row'); row.setAttribute('role', 'row'); row.dataset.fileId = file.id;
    const main = node('div', 'file-main'); main.setAttribute('role', 'cell');
    const type = node('span', 'file-type'); type.append(icon('file'));
    const copy = node('div', 'file-copy'), name = node('button', 'file-name file-details-button', file.original_name);
    name.dataset.action = 'details'; name.title = `${file.original_name} · 查看详情与校验码`; name.type = 'button'; name.setAttribute('aria-label', `查看 ${file.original_name} 的详情与校验码`); name.addEventListener('click', () => showDetails(file)); copy.append(name, node('div', 'file-owner', `来自 ${file.owner || '未命名设备'}`)); main.append(type, copy);
    const size = node('div', 'file-size', formatBytes(file.size)); size.setAttribute('role', 'cell');
    const date = fileDate(file.created_at), dateCell = node('div', 'file-date', date ? date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) : '—');
    dateCell.setAttribute('role', 'cell');
    if (date) { dateCell.append(node('small', '', date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }))); dateCell.title = date.toLocaleString('zh-CN'); }
    const actions = node('div', 'file-actions'); actions.setAttribute('role', 'cell');
    const download = node('a', 'icon-button download-button'); download.dataset.action = 'download'; download.href = `/download/${encodeURIComponent(file.id)}`; download.setAttribute('download', file.original_name); download.setAttribute('aria-label', `下载 ${file.original_name}`); download.title = '下载文件'; download.append(icon('download'));
    const remove = node('button', 'icon-button delete-button'); remove.dataset.action = 'delete'; remove.type = 'button'; remove.setAttribute('aria-label', `删除 ${file.original_name}`); remove.title = '删除文件'; remove.append(icon('trash'));
    remove.addEventListener('click', () => { state.deleteTarget = file; $('deleteFileName').textContent = file.original_name; $('deleteDialog').showModal(); });
    actions.append(download, remove); row.append(main, size, dateCell, actions); fragment.append(row);
  }
  $('fileList').append(fragment);
  if (focusedId) {
    const replacement = [...$('fileList').children].find((row) => row.dataset.fileId === focusedId);
    const action = replacement && [...replacement.querySelectorAll('[data-action]')].find((element) => element.dataset.action === focusedAction);
    (action || $('refreshButton')).focus({ preventScroll: true });
  }
}

async function deleteFile(file) {
  if (!file) return;
  try { await api(`/api/files/${encodeURIComponent(file.id)}`, { method: 'DELETE' }); toast(`已删除 ${file.original_name}`); await loadFiles(); }
  catch (error) { toast(error.message); }
}

function showDetails(file) {
  $('detailsName').textContent = file.original_name;
  const date = fileDate(file.created_at);
  $('detailsMeta').textContent = `${formatBytes(file.size)} · 来自 ${file.owner || '未命名设备'}${date ? ` · ${date.toLocaleString('zh-CN')}` : ''}`;
  $('detailsChecksum').value = file.sha256 || '此文件暂无校验码';
  $('copyChecksumButton').disabled = !file.sha256;
  $('detailsDialog').showModal();
}

function queueFiles(files) {
  if (!state.authenticated) return;
  let accepted = 0;
  for (const file of files) {
    if (state.config?.max_file_size && file.size > state.config.max_file_size) { toast(`${file.name} 超过单个文件大小上限`); continue; }
    if (state.queue.some((item) => busyStates.has(item.status) && item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified)) continue;
    const item = { key: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`, file, owner: $('deviceName').value.trim() || '未命名设备', status: 'queued', offset: 0, displayed: 0, uploadId: null, speed: 0, error: '', controller: null, xhr: null, row: null };
    state.queue.push(item); createQueueItem(item); accepted++;
  }
  updateQueueSummary(); if (accepted) processQueue(); $('fileInput').value = '';
}

function createQueueItem(item) {
  const li = node('li', 'queue-item'), row = node('div', 'queue-row'), name = node('span', 'queue-name', item.file.name), action = node('button', 'queue-action'), cancelAction = node('button', 'queue-action');
  cancelAction.append(icon('close')); cancelAction.setAttribute('aria-label', `取消上传 ${item.file.name}`); cancelAction.title = '取消上传并释放空间'; cancelAction.addEventListener('click', () => cancelUpload(item));
  name.title = item.file.name; row.append(icon('file'), name, action, cancelAction);
  const progress = node('div', 'queue-progress'), bar = node('span'); progress.append(bar);
  progress.setAttribute('role', 'progressbar'); progress.setAttribute('aria-label', `${item.file.name} 上传进度`); progress.setAttribute('aria-valuemin', '0'); progress.setAttribute('aria-valuemax', '100');
  const detail = node('p', 'queue-detail'); li.append(row, progress, detail); item.row = { li, action, cancelAction, progress, bar, detail };
  action.addEventListener('click', () => {
    if (item.status === 'error') { item.status = 'queued'; item.error = ''; renderQueueItem(item); updateQueueSummary(); processQueue(); }
    else cancelUpload(item);
  });
  $('uploadQueue').append(li); renderQueueItem(item);
}

function renderQueueItem(item) {
  if (!item.row) return;
  const { li, action, cancelAction, progress, bar, detail } = item.row;
  li.className = `queue-item ${item.status}`;
  const percent = item.status === 'done' ? 100 : item.file.size ? Math.min(100, item.displayed / item.file.size * 100) : 0;
  bar.style.width = `${percent}%`; progress.setAttribute('aria-valuenow', String(Math.round(percent)));
  action.replaceChildren(); action.hidden = ['done', 'cancelled', 'finishing'].includes(item.status);
  cancelAction.hidden = item.status !== 'error';
  if (item.status === 'error') { action.append(icon('refresh'), document.createTextNode('继续')); action.setAttribute('aria-label', `继续上传 ${item.file.name}`); }
  else { action.append(icon('close')); action.setAttribute('aria-label', `取消上传 ${item.file.name}`); }
  const messages = { queued: `${formatBytes(item.file.size)} · 等待上传`, uploading: `${formatBytes(item.displayed)} / ${formatBytes(item.file.size)}${item.speed > 0 ? ` · ${formatBytes(item.speed)}/s` : ''}`, retrying: '连接中断，正在尝试恢复…', finishing: '分片已传完，正在校验文件…', done: `${formatBytes(item.file.size)} · 上传完成`, cancelled: '已取消上传', error: item.error || '上传失败，点击继续重试' };
  detail.textContent = messages[item.status];
}

function updateQueueSummary() {
  $('queueSection').hidden = !state.queue.length;
  const active = state.queue.filter((item) => busyStates.has(item.status)).length;
  $('queueCount').textContent = active ? `${active} 个待完成` : `${state.queue.length} 个`;
  $('clearQueueButton').hidden = !state.queue.some((item) => ['done', 'cancelled'].includes(item.status));
}

function ensureActive(item) {
  if (item.status === 'cancelled' || item.controller.signal.aborted || !state.authenticated) throw new DOMException('已中止', 'AbortError');
}

function wait(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('已中止', 'AbortError')); return; }
    const aborted = () => { clearTimeout(timer); reject(new DOMException('已中止', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', aborted); resolve(); }, ms);
    signal.addEventListener('abort', aborted, { once: true });
  });
}

function uploadChunk(item, chunk) {
  return new Promise((resolve, reject) => {
    ensureActive(item);
    const xhr = new XMLHttpRequest(); item.xhr = xhr;
    xhr.open('PUT', `/api/uploads/${encodeURIComponent(item.uploadId)}`);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream'); xhr.setRequestHeader('Upload-Offset', String(item.offset)); xhr.timeout = 120000;
    let lastTime = performance.now(), lastBytes = 0;
    xhr.upload.onprogress = (event) => {
      const now = performance.now(), elapsed = (now - lastTime) / 1000;
      if (elapsed > .25) { item.speed = (event.loaded - lastBytes) / elapsed; lastTime = now; lastBytes = event.loaded; }
      item.displayed = item.offset + event.loaded; renderQueueItem(item);
    };
    xhr.onload = () => {
      item.xhr = null; let data;
      try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else { if (xhr.status === 401) loseSession(); reject(new ApiError(data.error || `上传失败（${xhr.status}）`, xhr.status)); }
    };
    xhr.onerror = () => { item.xhr = null; reject(new ApiError('网络连接中断，点击继续恢复上传')); };
    xhr.ontimeout = () => { item.xhr = null; reject(new ApiError('上传超时，点击继续恢复上传')); };
    xhr.onabort = () => { item.xhr = null; reject(new DOMException('已中止', 'AbortError')); };
    xhr.send(chunk);
  });
}

async function synchronizeOffset(item) {
  const info = await api(`/api/uploads/${encodeURIComponent(item.uploadId)}`, { signal: item.controller.signal });
  ensureActive(item);
  const offset = Number(info.offset);
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > item.file.size) throw new ApiError('服务端返回了无效的传输进度，请取消后重新选择文件');
  item.offset = offset; item.displayed = offset; renderQueueItem(item);
  item.completedFile = info.completed ? info.file : null;
}

async function runUpload(item) {
  item.controller = new AbortController(); item.status = 'uploading'; item.speed = 0; renderQueueItem(item);
  try {
    if (item.uploadId) {
      try { await synchronizeOffset(item); }
      catch (error) { if (error.status === 404) { item.uploadId = null; item.offset = 0; item.displayed = 0; } else throw error; }
      if (item.completedFile) { item.status = 'done'; item.displayed = item.file.size; renderQueueItem(item); await loadFiles({ quiet: true }); return; }
    }
    if (!item.uploadId) {
      const upload = await api('/api/uploads', jsonRequest('POST', { name: item.file.name, size: item.file.size, owner: item.owner }, item.controller.signal));
      item.uploadId = upload.id; item.chunkSize = Number(upload.chunk_size) || state.config.chunk_size || 8388608;
      ensureActive(item);
    }
    let failures = 0;
    while (item.offset < item.file.size) {
      ensureActive(item); item.status = 'uploading'; renderQueueItem(item);
      try {
        const end = Math.min(item.offset + (item.chunkSize || state.config.chunk_size || 8388608), item.file.size);
        const result = await uploadChunk(item, item.file.slice(item.offset, end));
        ensureActive(item);
        if (Number(result.offset) !== end) throw new ApiError('传输进度需要重新同步', 409);
        item.offset = end; item.displayed = end; failures = 0; renderQueueItem(item);
      } catch (error) {
        ensureActive(item);
        if (![0, 408, 409, 429, 500, 502, 503, 504].includes(error.status) || failures >= 3) throw error;
        item.status = 'retrying'; item.speed = 0; item.displayed = item.offset; renderQueueItem(item);
        await wait(1000 * 2 ** failures++, item.controller.signal);
        await synchronizeOffset(item);
      }
    }
    ensureActive(item); item.status = 'finishing'; renderQueueItem(item);
    await api(`/api/uploads/${encodeURIComponent(item.uploadId)}/complete`, { method: 'POST', signal: item.controller.signal, timeoutMs: 120000 });
    ensureActive(item); item.status = 'done'; item.displayed = item.file.size; renderQueueItem(item); await loadFiles();
  } catch (error) {
    if (item.status !== 'cancelled') { item.status = 'error'; item.displayed = item.offset; item.error = error.name === 'AbortError' ? '上传已暂停，重新登录后点击继续' : error.message; renderQueueItem(item); }
  } finally { item.xhr = null; item.controller = null; updateQueueSummary(); }
}

async function processQueue() {
  if (state.processing || !state.authenticated) return;
  state.processing = true;
  try {
    let next;
    while (state.authenticated && (next = state.queue.find((item) => item.status === 'queued'))) await runUpload(next);
  } finally { state.processing = false; updateQueueSummary(); }
}

async function cancelUpload(item) {
  if (!busyStates.has(item.status) && item.status !== 'error') return;
  item.status = 'cancelled'; item.controller?.abort(); item.xhr?.abort(); renderQueueItem(item); updateQueueSummary();
  if (item.uploadId) {
    try { await api(`/api/uploads/${encodeURIComponent(item.uploadId)}`, { method: 'DELETE' }); }
    catch (error) { if (![404, 401].includes(error.status)) toast('已停止上传；未完成分片将由服务器清理'); }
  }
}

function isLoopback() { return /^(localhost|127(?:\.\d+){3}|\[?::1\]?)$/i.test(location.hostname); }
function isPrivateNetwork() { return /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(location.hostname) || /\.local$/i.test(location.hostname); }

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
  else {
    const input = node('textarea', 'sr-only', value); input.setAttribute('readonly', '');
    const parent = document.querySelector('dialog[open]') || document.body; parent.append(input); input.select();
    const copied = document.execCommand('copy'); input.remove(); if (!copied) throw new Error('复制失败');
  }
}

async function copyShareLink() {
  const url = state.config?.public_url || `${location.origin}/`;
  try {
    await copyText(url);
    toast(!state.config?.public_url && isLoopback() ? '已复制本机链接；其他网络请使用公网启动器提供的链接' : !state.config?.public_url && isPrivateNetwork() ? '已复制局域网链接；跨网络请使用公网链接' : '分享链接已复制，请单独告知对方访问码');
  } catch { toast(`请复制此链接：${url}`); }
}

function defaultDeviceName() {
  const ua = navigator.userAgent;
  return /iPhone|iPad/.test(ua) ? '我的 iPhone / iPad' : /Android/.test(ua) ? '我的 Android 手机' : /Macintosh/.test(ua) ? '我的 Mac' : /Windows/.test(ua) ? '我的 Windows 电脑' : '我的设备';
}

try { $('deviceName').value = localStorage.getItem('fileHubDeviceName') || localStorage.getItem('lanHubName') || defaultDeviceName(); } catch { $('deviceName').value = defaultDeviceName(); }
$('deviceName').addEventListener('input', () => { try { localStorage.setItem('fileHubDeviceName', $('deviceName').value); } catch { /* Device naming also works when storage is unavailable. */ } });
$('loginForm').addEventListener('submit', login);
$('logoutButton').addEventListener('click', logout);
$('reconnectButton').addEventListener('click', initialize);
$('refreshButton').addEventListener('click', loadFiles);
$('copyLinkButton').addEventListener('click', copyShareLink);
$('copyChecksumButton').addEventListener('click', async () => { try { await copyText($('detailsChecksum').value); toast('SHA-256 校验码已复制'); } catch { $('detailsChecksum').focus(); $('detailsChecksum').select(); toast('请手动复制已选中的校验码'); } });
$('searchInput').addEventListener('input', renderFiles);
$('sortSelect').addEventListener('change', renderFiles);
$('dropZone').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', (event) => queueFiles(event.target.files));
$('dropZone').addEventListener('dragenter', (event) => { event.preventDefault(); state.dragDepth++; $('dropZone').classList.add('dragging'); });
$('dropZone').addEventListener('dragover', (event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; });
$('dropZone').addEventListener('dragleave', (event) => { event.preventDefault(); if (--state.dragDepth <= 0) { state.dragDepth = 0; $('dropZone').classList.remove('dragging'); } });
$('dropZone').addEventListener('drop', (event) => { event.preventDefault(); state.dragDepth = 0; $('dropZone').classList.remove('dragging'); queueFiles(event.dataTransfer.files); });
window.addEventListener('dragover', (event) => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
window.addEventListener('drop', (event) => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
$('clearQueueButton').addEventListener('click', () => { state.queue = state.queue.filter((item) => { if (['done', 'cancelled'].includes(item.status)) { item.row.li.remove(); return false; } return true; }); updateQueueSummary(); });
$('deleteDialog').addEventListener('close', () => { const file = state.deleteTarget; state.deleteTarget = null; if ($('deleteDialog').returnValue === 'delete') deleteFile(file); });
window.addEventListener('offline', () => { $('networkNotice').hidden = false; connection('offline', '网络已断开'); });
window.addEventListener('online', () => { $('networkNotice').hidden = true; if (state.authenticated) loadFiles(); else connection('', '网络已恢复'); });
window.addEventListener('beforeunload', (event) => { if (state.queue.some((item) => busyStates.has(item.status))) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.authenticated && !state.filesLoading) loadFiles({ quiet: true }); });
setInterval(() => { if (!document.hidden && state.authenticated && !state.filesLoading && navigator.onLine) loadFiles({ quiet: true }); }, 10000);
initialize();
