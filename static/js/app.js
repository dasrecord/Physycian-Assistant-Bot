// static/js/app.js  --  PhysAI frontend

const socket = io();

// ── State ────────────────────────────────────────────────────
let mediaRecorder       = null;
let audioStream         = null;
let audioChunks         = [];
let isRecording         = false;
let timerInterval       = null;
let timerSeconds        = 0;
let liveInterval        = null;
let analyser            = null;
let animFrame           = null;
let currentNote         = null;
let autoGenerateTimeout = null;
let lastAudioBlob       = null;
let isGenerating        = false;

const $ = id => document.getElementById(id);

// ── Socket events ─────────────────────────────────────────────
socket.on('connect',    () => updatePills());
socket.on('disconnect', () => setStatus('Disconnected.', 'error'));
socket.on('status', d => setStatus(d.msg, d.level || 'info'));

socket.on('partial_transcript', d => {
  $('transcript').value = d.transcript;
});

socket.on('transcript_ready', d => {
  $('transcript').value = d.transcript;
  $('btn-generate').disabled = false;
  setStatus('Transcript ready. Generating in 3s…', 'success');
  startAutoGenerate();
});

socket.on('note_streaming', d => {
  const el = $('note-full');
  el.value += d.token;
  el.scrollTop = el.scrollHeight;
});

socket.on('note_ready', note => {
  isGenerating = false;
  $('btn-generate').disabled = false;
  $('btn-generate').textContent = '\u{1F4C4} Generate SOAP Note';
  currentNote = note;
  renderNote(note);
  $('btn-copy').disabled    = false;
  $('btn-oscar').disabled   = false;
  $('btn-billing').disabled = false;
  cancelAutoGenerate();
  checkBillingUpgrade();
  setStatus('Note ready. Review, edit, then post.', 'success');
});

socket.on('oscar_result',   r => setStatus(r.success ? 'Posted to OSCAR.' : 'OSCAR error: ' + r.error, r.success ? 'success' : 'error'));
socket.on('billing_result', r => setStatus(r.success ? 'Billing submitted.' : 'Billing error: ' + r.error, r.success ? 'success' : 'error'));

// ── Status ────────────────────────────────────────────────────
function setStatus(msg, level) {
  const el = $('status-message');
  el.textContent = msg;
  el.className = 'status-msg' + (level ? ' status-' + level : '');
}

// ── Timer ─────────────────────────────────────────────────────
function startTimer() {
  timerSeconds = 0;
  timerInterval = setInterval(() => {
    timerSeconds++;
    const m = String(Math.floor(timerSeconds / 60)).padStart(2, '0');
    const s = String(timerSeconds % 60).padStart(2, '0');
    $('el-timer').textContent = m + ':' + s;
    if (timerSeconds >= 1200) $('el-timer').classList.add('amber');
  }, 1000);
}
function stopTimer() { clearInterval(timerInterval); }

// ── Recording ────────────────────────────────────────────────
$('btn-record').addEventListener('click', startRecording);
$('btn-stop').addEventListener('click', stopRecording);

async function startRecording() {
  try { audioStream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { setStatus('Microphone access denied.', 'error'); return; }

  audioChunks = [];
  isRecording = true;
  $('btn-record').disabled = true;
  $('btn-record').classList.add('active');
  $('btn-stop').disabled = false;
  $('btn-generate').disabled = true;
  $('audio-player').classList.add('hidden');
  $('post-record-actions').classList.add('hidden');

  await fetch('/api/start-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      patient_name: $('patient-name').value,
      patient_dob:  $('patient-dob').value,
      health_card:  $('health-card').value,
      visit_type:   $('visit-type').value,
    }),
  });

  startTimer();
  startWaveform(audioStream);
  liveInterval = setInterval(sendLiveChunk, 15000);

  mediaRecorder = new MediaRecorder(audioStream, { mimeType: 'audio/webm' });
  mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
  mediaRecorder.start(1000);
}

async function sendLiveChunk() {
  if (!audioChunks.length) return;
  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  fetch('/api/transcribe-chunk', { method: 'POST', body: blob, headers: { 'Content-Type': 'audio/webm' } });
}

async function stopRecording() {
  if (!isRecording) return;
  isRecording = false;
  clearInterval(liveInterval);
  stopTimer();
  mediaRecorder.stop();
  audioStream.getTracks().forEach(t => t.stop());
  $('btn-record').disabled = false;
  $('btn-record').classList.remove('active');
  $('btn-stop').disabled = true;
  cancelAnimationFrame(animFrame);

  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  lastAudioBlob = blob;
  $('audio-player').src = URL.createObjectURL(blob);
  $('audio-player').classList.remove('hidden');
  $('post-record-actions').style.display = 'flex';
  $('post-record-actions').classList.remove('hidden');

  setStatus('Processing audio…', 'info');
  await fetch('/api/stop-recording', { method: 'POST', body: blob, headers: { 'Content-Type': 'audio/webm' } });
}

$('btn-retranscribe').addEventListener('click', async () => {
  if (!lastAudioBlob) return;
  setStatus('Re-transcribing…', 'info');
  await fetch('/api/stop-recording', { method: 'POST', body: lastAudioBlob, headers: { 'Content-Type': 'audio/webm' } });
});
$('btn-regenerate').addEventListener('click', generateNote);

// ── Auto-generate countdown ───────────────────────────────────
function startAutoGenerate() {
  const bar = $('auto-generate-bar');
  bar.classList.remove('hidden');
  const fill = $('countdown-progress');
  fill.style.animation = 'none'; fill.offsetHeight;
  fill.style.animation = 'countdown-shrink 3s linear forwards';
  let n = 3;
  $('auto-generate-text').textContent = 'Generating in ' + n + 's…';
  const tick = setInterval(() => {
    n--;
    if (n > 0) $('auto-generate-text').textContent = 'Generating in ' + n + 's…';
    else clearInterval(tick);
  }, 1000);
  autoGenerateTimeout = setTimeout(() => {
    bar.classList.add('hidden');
    generateNote();
  }, 3000);
}
function cancelAutoGenerate() {
  if (autoGenerateTimeout) { clearTimeout(autoGenerateTimeout); autoGenerateTimeout = null; }
  $('auto-generate-bar').classList.add('hidden');
  $('countdown-progress').style.animation = 'none';
}

// ── Generate note ─────────────────────────────────────────────
$('btn-generate').addEventListener('click', () => { cancelAutoGenerate(); generateNote(); });

async function generateNote() {
  if (isGenerating) return;
  const transcript = $('transcript').value.trim();
  if (!transcript) { setStatus('No transcript to generate from.', 'warn'); return; }
  isGenerating = true;
  $('btn-generate').disabled = true;
  $('btn-generate').textContent = '\u23F3 Generating...';
  $('note-full').value = '';
  setStatus('Sending to local AI\u2026', 'info');
  const res = await fetch('/api/generate-note', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transcript, template_id: $('template-select').value, socket_id: socket.id }),
  });
  if (!res.ok) {
    isGenerating = false;
    $('btn-generate').disabled = false;
    $('btn-generate').textContent = '\u{1F4C4} Generate SOAP Note';
    setStatus('Server busy, try again.', 'warn');
  }
}

// ── Render note ───────────────────────────────────────────────
function renderNote(note) {
  let text = '';
  if (note.subjective) text += 'S:\n' + note.subjective + '\n\n';
  if (note.objective)  text += 'O:\n' + note.objective  + '\n\n';
  // Extra sections (MSE, NEURO EXAM) between O and A
  for (const [key, content] of Object.entries(note.extra_sections || {})) {
    const label = key.replace(/_/g, ' ').toUpperCase();
    text += label + ':\n' + content + '\n\n';
  }
  if (note.assessment) text += 'A:\n' + note.assessment + '\n\n';
  if (note.plan)       text += 'P:\n' + note.plan;
  $('note-full').value = text.trim();

  // ICD9 chips
  const chips = $('icd9-chips');
  chips.innerHTML = '';
  if (note.icd9_codes && note.icd9_codes.length) {
    note.icd9_codes.forEach(code => {
      const span = document.createElement('span');
      span.className = 'icd9-chip';
      span.textContent = code;
      chips.appendChild(span);
    });
    $('icd9-row').classList.remove('hidden');
  } else {
    $('icd9-row').classList.add('hidden');
  }
}

// ── Copy ──────────────────────────────────────────────────────
$('btn-copy').addEventListener('click', () => {
  navigator.clipboard.writeText($('note-full').value)
    .then(() => setStatus('Copied to clipboard.', 'success'));
});

// ── OSCAR ─────────────────────────────────────────────────────
$('btn-oscar').addEventListener('click', async () => {
  if (!currentNote) return;
  currentNote.full_text = $('note-full').value;
  await fetch('/api/post-to-oscar', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ soap_note: currentNote, patient_name: $('patient-name').value, patient_dob: $('patient-dob').value }),
  });
});

// ── Billing ───────────────────────────────────────────────────
$('btn-billing').addEventListener('click', submitBilling);
async function submitBilling() {
  if (!currentNote) return;
  await fetch('/api/submit-billing', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      soap_note:        currentNote,
      patient_name:     $('patient-name').value,
      patient_dob:      $('patient-dob').value,
      health_card:      $('health-card').value,
      visit_type:       $('visit-type').value,
      duration_minutes: Math.floor(timerSeconds / 60),
    }),
  });
}
function checkBillingUpgrade() {
  const words = $('note-full').value.split(/\s+/).filter(Boolean).length;
  if (timerSeconds > 1200 || words > 400) $('billing-upgrade-bar').classList.remove('hidden');
}

// ── Pills ─────────────────────────────────────────────────────
async function updatePills() {
  try {
    const data = await fetch('/api/health').then(r => r.json());
    $('pill-whisper').className = 'pill ' + (data.whisper === 'ready' ? 'pill-on' : 'pill-off');
    $('pill-ollama').className  = 'pill ' + (data.ollama  === 'ok'    ? 'pill-on' : 'pill-off');
    $('pill-oscar').className   = 'pill pill-off';
    $('pill-model-name').textContent = (data.current_model || 'Ollama').replace(':latest','');
    const better = data.recommended_model && data.recommended_model !== data.current_model;
    $('pill-model-badge').classList.toggle('hidden', !better);
    if (data.available_gb !== null) $('ram-note').textContent = 'RAM free: ' + data.available_gb + ' GB | Recommended: ' + data.recommended_model;
    const list = $('model-list');
    list.innerHTML = '';
    (data.available_models || []).forEach(name => {
      const btn = document.createElement('button');
      btn.className = 'model-btn' + (name === data.current_model ? ' active' : '');
      btn.textContent = name;
      btn.onclick = () => selectModel(name);
      list.appendChild(btn);
    });
  } catch(_) {}
}
function toggleModelPopover() { $('model-popover').classList.toggle('hidden'); }
document.addEventListener('click', e => { if (!e.target.closest('.model-wrap')) $('model-popover').classList.add('hidden'); });
async function selectModel(name) {
  $('model-popover').classList.add('hidden');
  setStatus('Switching to ' + name + '…', 'info');
  await fetch('/api/set-model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: name }) });
  await updatePills();
  setStatus('Model set to ' + name + '. Warming up…', 'success');
}

// ── Waveform ──────────────────────────────────────────────────
function startWaveform(stream) {
  const ac = new AudioContext();
  analyser = ac.createAnalyser();
  analyser.fftSize = 256;
  ac.createMediaStreamSource(stream).connect(analyser);
  drawWave();
}
function drawWave() {
  animFrame = requestAnimationFrame(drawWave);
  const canvas = $('waveform');
  const ctx = canvas.getContext('2d');
  const data = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteTimeDomainData(data);
  ctx.fillStyle = '#1a1d29';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.lineWidth = 1.5; ctx.strokeStyle = '#4a7df5';
  ctx.beginPath();
  const slice = canvas.width / data.length;
  let x = 0;
  data.forEach((v, i) => {
    const y = (v / 128) * (canvas.height / 2);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    x += slice;
  });
  ctx.stroke();
}

// ── Templates ─────────────────────────────────────────────────
async function loadTemplates() {
  const list = await fetch('/api/templates').then(r => r.json());
  const sel = $('template-select');
  sel.innerHTML = '';
  list.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.id; opt.textContent = t.name;
    sel.appendChild(opt);
  });
  const saved = localStorage.getItem('defaultTemplate');
  if (saved && [...sel.options].some(o => o.value === saved)) sel.value = saved;
  renderTemplateList(list);
}
$('template-select').addEventListener('change', () => localStorage.setItem('defaultTemplate', $('template-select').value));
function renderTemplateList(list) {
  const tl = $('template-list'); tl.innerHTML = '';
  list.forEach(t => {
    const row = document.createElement('div'); row.className = 'tpl-row';
    row.innerHTML = '<span class="tpl-name">' + t.name + '</span>'
      + '<span class="tpl-desc">' + (t.description || '') + '</span>'
      + (t.is_builtin
        ? '<span class="tpl-tag">built-in</span>'
        : '<button class="btn-danger" onclick="deleteTemplate(\'' + t.id + '\')">Delete</button>');
    tl.appendChild(row);
  });
}
function openTemplateModal()  { $('template-modal').classList.remove('hidden'); loadTemplates(); }
function closeTemplateModal(e) { if (!e || e.target === $('template-modal') || !e.target.closest('.modal-box')) $('template-modal').classList.add('hidden'); }
async function saveCustomTemplate() {
  const id = $('tpl-id').value.trim().replace(/\s+/g,'_');
  const name = $('tpl-name').value.trim();
  if (!id || !name) { setStatus('Template id and name required.', 'warn'); return; }
  const r = await fetch('/api/templates', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, name, description: $('tpl-desc').value, system_prompt_extra: $('tpl-extra').value, output_format: $('tpl-format').value }) });
  if (r.ok) { setStatus('Template saved.', 'success'); loadTemplates(); }
  else { const e = await r.json(); setStatus(e.error || 'Save failed.', 'error'); }
}
async function deleteTemplate(id) {
  if (!confirm('Delete this template?')) return;
  await fetch('/api/templates/' + id, { method: 'DELETE' });
  loadTemplates();
}

// ── Enable Generate from paste/type ──────────────────────────
$('transcript').addEventListener('input', () => {
  $('btn-generate').disabled = !$('transcript').value.trim();
});

// ── Init ──────────────────────────────────────────────────────
(async () => {
  await loadTemplates();
  updatePills();
  setInterval(updatePills, 30000);
})();
