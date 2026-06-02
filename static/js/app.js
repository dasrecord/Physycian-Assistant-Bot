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
let analyserPt          = null;
let animFrame           = null;
let currentNote         = null;
let autoGenerateTimeout = null;
let lastAudioBlob       = null;
let loadedAudioFilename = null;
let isGenerating        = false;
let isPhoneCall         = true;    // phone call mode — capture patient audio from browser tab
let phoneCallSucceeded  = null;    // null=N/A, true=stereo merged OK, false=fallback mono
let patientStream       = null;    // patient audio stream for waveform
let mergeCtx            = null;    // Web Audio context used for stereo merge
let isPaused            = false;   // recording is paused
let cancelPending       = false;   // recording is being cancelled (discard data)

const $ = id => document.getElementById(id);

// ── Socket events ─────────────────────────────────────────────
socket.on('connect',    () => updatePills());
socket.on('disconnect', () => setStatus('Disconnected.', 'error'));
socket.on('status', d => setStatus(d.msg, d.level || 'info'));

socket.on('transcript_corrections', d => {
  if (d && d.count) {
    setStatus(`Vocabulary corrections applied: ${d.count}`, 'info');
    if (window.console) console.log('[vocab] corrections:', d.changes);
  }
});

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
  const isMeeting = document.body.classList.contains('meeting-mode');
  $('btn-generate').textContent = isMeeting ? '📝 Generate Meeting Notes' : '\u{1F4C4} Generate SOAP Note';
  currentNote = note;
  renderNote(note);
  $('btn-copy').disabled    = false;
  $('btn-oscar').disabled   = false;
  $('btn-billing').disabled = false;
  cancelAutoGenerate();
  checkBillingUpgrade();
  setStatus('Note ready. Review, edit, then post.', 'success');
  loadHistory();
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

// ── Phone call mode ──────────────────────────────────────────
function toggleCallMode() {
  isPhoneCall = !isPhoneCall;
  const btn = $('btn-call-mode');
  btn.textContent = isPhoneCall ? '📞 Phone call: ON' : '📞 Phone call: OFF';
  btn.classList.toggle('call-mode-active', isPhoneCall);
  if (isPhoneCall) {
    setStatus('Phone call mode ON — when recording starts, select your calling tab for patient audio.', 'info');
  } else {
    setStatus('Phone call mode OFF — mono recording with pause-based diarization.', 'info');
  }
}

/** Try to capture patient audio from a browser tab (getDisplayMedia). Returns a MediaStream or null. */
async function capturePatientAudio() {
  try {
    // Try video:false first (Chrome 107+, all Firefox)
    return await navigator.mediaDevices.getDisplayMedia({ audio: true, video: false });
  } catch (_) {}
  try {
    // Fallback: Chrome <107 requires video:true — request minimal resolution and drop tracks
    const s = await navigator.mediaDevices.getDisplayMedia({
      audio: true,
      video: { width: 1, height: 1, frameRate: 1 },
    });
    s.getVideoTracks().forEach(t => t.stop());
    return s;
  } catch (_) {}
  return null;
}

// ── Recording ────────────────────────────────────────────────
$('btn-record').addEventListener('click', startRecording);
$('btn-pause').addEventListener('click', togglePause);
$('btn-stop').addEventListener('click', stopRecording);
$('btn-cancel').addEventListener('click', cancelRecording);

function togglePause() {
  if (!isRecording) return;
  if (!isPaused) {
    mediaRecorder.pause();
    clearInterval(timerInterval);
    clearInterval(liveInterval);
    isPaused = true;
    $('btn-pause').textContent = '▶ Resume';
    $('btn-pause').classList.add('resuming');
    setStatus('Recording paused. Hit Resume to continue.', 'warn');
  } else {
    mediaRecorder.resume();
    // Restart timer from current count (don't reset timerSeconds)
    timerInterval = setInterval(() => {
      timerSeconds++;
      const m = String(Math.floor(timerSeconds / 60)).padStart(2, '0');
      const s = String(timerSeconds % 60).padStart(2, '0');
      $('el-timer').textContent = m + ':' + s;
      if (timerSeconds >= 1200) $('el-timer').classList.add('amber');
    }, 1000);
    liveInterval = setInterval(sendLiveChunk, 15000);
    isPaused = false;
    $('btn-pause').textContent = '❚❚ Pause';
    $('btn-pause').classList.remove('resuming');
    setStatus('Recording resumed.', 'success');
  }
}

async function cancelRecording() {
  if (!isRecording) return;
  cancelPending = true;
  isRecording = false;
  isPaused = false;
  clearInterval(liveInterval);
  stopTimer();
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  audioStream.getTracks().forEach(t => t.stop());
  if (mergeCtx) { mergeCtx.close(); mergeCtx = null; }
  patientStream = null;
  audioChunks = [];
  cancelPending = false;
  $('btn-record').disabled = false;
  $('btn-record').classList.remove('active');
  $('btn-stop').disabled = true;
  $('btn-pause').classList.add('hidden');
  $('btn-pause').textContent = '❚❚ Pause';
  $('btn-pause').classList.remove('resuming');
  $('btn-cancel').classList.add('hidden');
  $('el-timer').textContent = '00:00';
  $('el-timer').classList.remove('amber');
  setStatus('Recording cancelled.', 'info');
}

async function startRecording() {
  try { audioStream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { setStatus('Microphone access denied.', 'error'); return; }

  let recordingStream = audioStream;   // default: mic only (mono)
  phoneCallSucceeded = null;

  if (isPhoneCall) {
    setStatus('Select the calling tab to capture patient audio…', 'info');
    const displayStream = await capturePatientAudio();
    const patTrack = displayStream && displayStream.getAudioTracks()[0];
    if (patTrack) {
      // Merge mic → L channel, patient → R channel into a single stereo stream
      mergeCtx = new AudioContext();
      const merger = mergeCtx.createChannelMerger(2);
      mergeCtx.createMediaStreamSource(audioStream).connect(merger, 0, 0);    // mic → left
      mergeCtx.createMediaStreamSource(new MediaStream([patTrack])).connect(merger, 0, 1);  // patient → right
      const dest = mergeCtx.createMediaStreamDestination();
      merger.connect(dest);
      recordingStream = dest.stream;
      patientStream = new MediaStream([patTrack]);
      phoneCallSucceeded = true;
      setStatus('📞 Phone call mode active — capturing both channels (Dr=L, Pt=R).', 'success');
    } else {
      phoneCallSucceeded = false;
      patientStream = null;
      setStatus('Could not capture patient audio — recording mic only, no speaker labels.', 'warn');
    }
  }

  audioChunks = [];
  isRecording = true;
  isPaused = false;
  cancelPending = false;
  $('btn-record').disabled = true;
  $('btn-record').classList.add('active');
  $('btn-pause').classList.remove('hidden');
  $('btn-pause').textContent = '❚❚ Pause';
  $('btn-pause').classList.remove('resuming');
  $('btn-stop').disabled = false;
  $('btn-cancel').classList.remove('hidden');
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
  startWaveform(audioStream, patientStream);   // Dr = webcam mic, Pt = tab audio
  liveInterval = setInterval(sendLiveChunk, 15000);

  mediaRecorder = new MediaRecorder(recordingStream, { mimeType: 'audio/webm' });
  mediaRecorder.ondataavailable = e => { if (e.data.size > 0 && !cancelPending) audioChunks.push(e.data); };
  mediaRecorder.start(1000);
}

async function sendLiveChunk() {
  if (!audioChunks.length) return;
  // Skip live preview in phone-call mode: stereo transcription runs two full
  // whisper passes per chunk and causes memory exhaustion over long sessions.
  if (phoneCallSucceeded === true) return;
  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  // Safety cap: skip if accumulated audio exceeds ~20 MB to prevent MLX OOM.
  if (blob.size > 20 * 1024 * 1024) return;
  fetch('/api/transcribe-chunk', { method: 'POST', body: blob, headers: { 'Content-Type': 'audio/webm' } });
}

async function stopRecording() {
  if (!isRecording) return;
  isRecording = false;
  isPaused = false;
  clearInterval(liveInterval);
  stopTimer();
  if (mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  audioStream.getTracks().forEach(t => t.stop());
  if (mergeCtx) { mergeCtx.close(); mergeCtx = null; }
  patientStream = null;
  $('btn-record').disabled = false;

  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  lastAudioBlob = blob;
  loadedAudioFilename = null;  // fresh recording — blob is authoritative
  $('audio-player').src = URL.createObjectURL(blob);
  $('audio-player').classList.remove('hidden');
  $('post-record-actions').style.display = 'flex';
  $('post-record-actions').classList.remove('hidden');

  setStatus('Processing audio…', 'info');
  const stopUrl = (isPhoneCall && phoneCallSucceeded === false)
    ? '/api/stop-recording?no_diarize=1'
    : '/api/stop-recording';
  await fetch(stopUrl, { method: 'POST', body: blob, headers: { 'Content-Type': 'audio/webm' } });
  if (typeof loadAudioList === 'function') loadAudioList();
}

$('btn-retranscribe').addEventListener('click', async () => {
  if (loadedAudioFilename) {
    // Server-side retranscribe (works after refresh / loaded from history)
    setStatus('Re-transcribing…', 'info');
    await fetch('/api/retranscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: loadedAudioFilename }),
    });
  } else if (lastAudioBlob) {
    setStatus('Re-transcribing…', 'info');
    await fetch('/api/stop-recording', { method: 'POST', body: lastAudioBlob, headers: { 'Content-Type': 'audio/webm' } });
  }
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
// ── Patient info import (clipboard / paste) ─────────────────
function _parsePatientText(raw) {
  // Normalize: strip markdown links [text](url) → text, collapse runs of whitespace
  const text = raw.trim()
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')   // [email](mailto:...) → email
    .replace(/\r/g, '');
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  // Also work on the full text as one line (WELL Health pastes as one blob)
  const flat = text.replace(/\n/g, ' ');
  const out = {};

  // Name + DOB: "First Last , 25 (12/4/2001)"
  const nal = flat.match(/^(.+?)\s*,\s*\d+\s*\((\d{1,2})\/(\d{1,2})\/(\d{4})\)/);
  if (nal) {
    out.name = nal[1].trim();
    // nal[2]=DD, nal[3]=MM, nal[4]=YYYY  →  YYYY-MM-DD
    out.dob  = `${nal[4]}-${nal[3].padStart(2,'0')}-${nal[2].padStart(2,'0')}`;
  } else {
    const nameOnly = (lines[0] || '').replace(/\s*,.*$/, '').trim();
    if (nameOnly && !/^[\d*@]/.test(nameOnly)) out.name = nameOnly;
    const dob = flat.match(/\((\d{1,2})\/(\d{1,2})\/(\d{4})\)/);
    if (dob) out.dob = `${dob[3]}-${dob[2].padStart(2,'0')}-${dob[1].padStart(2,'0')}`;
  }

  // Health card (BC: exactly 10 digits)
  const hc = flat.match(/Health\s*Card[:\s]+([0-9]{10})/i);
  if (hc) out.healthCard = hc[1];

  // Email — plain or extracted from markdown
  const em = flat.match(/[\w.+-]+@[\w-]+\.[\w.]+/);
  if (em) out.email = em[0];

  // Service date + time: "May 31, 2:05 PM" or "May 31, 2026, 2:05 PM"
  const dtMatch = flat.match(/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s*(?:\d{4},?\s*)?(\d{1,2}:\d{2}\s*[AP]M)/i);
  if (dtMatch) {
    const monthMap = {jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11};
    const mo = monthMap[dtMatch[1].toLowerCase()];
    const dy = parseInt(dtMatch[2]);
    const yr = new Date().getFullYear();
    const d  = new Date(yr, mo, dy);
    out.serviceDate = `${yr}-${String(mo+1).padStart(2,'0')}-${String(dy).padStart(2,'0')}`;
    // Parse time to HH:MM 24h
    const tm = dtMatch[3].trim();
    const [hhmm, ampm] = tm.split(/\s+/);
    let [hh, mm] = hhmm.split(':').map(Number);
    if (ampm.toUpperCase() === 'PM' && hh !== 12) hh += 12;
    if (ampm.toUpperCase() === 'AM' && hh === 12) hh = 0;
    out.serviceTime = `${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}`;
  }

  // Visit type
  if      (/prolonged/i.test(flat))                            out.visitType = 'prolonged';
  else if (/counsel/i.test(flat))                              out.visitType = 'counselling';
  else if (/urgent|acute|emerg/i.test(flat))                   out.visitType = 'urgent';
  else if (/general|standard|routine|appointment/i.test(flat)) out.visitType = 'standard';

  return out;
}

function _applyPatientData(p) {
  if (p.name)        $('patient-name').value  = p.name;
  if (p.dob)         $('patient-dob').value   = p.dob;
  if (p.healthCard)  $('health-card').value   = p.healthCard;
  if (p.visitType)   $('visit-type').value    = p.visitType;
  if (p.email)       $('patient-email').value = p.email;
  if (p.serviceDate) $('service-date').value  = p.serviceDate;
  if (p.serviceTime) $('service-time').value  = p.serviceTime;
  const filled = [p.name && 'name', p.dob && 'DOB', p.healthCard && 'HC', p.email && 'email', p.serviceDate && 'date'].filter(Boolean);
  if (filled.length) setStatus('Imported: ' + filled.join(', ') + '.', 'success');
  else               setStatus('Nothing recognized — check format.', 'warn');
  fetch('/api/update-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      patient_name: $('patient-name').value,
      patient_dob:  $('patient-dob').value,
      health_card:  $('health-card').value,
      visit_type:   $('visit-type').value,
    }),
  }).catch(() => {});
}

async function importPatientFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) { setStatus('Clipboard is empty.', 'warn'); return; }
    const parsed = _parsePatientText(text);
    if (parsed.name || parsed.healthCard) {
      _applyPatientData(parsed);
    } else {
      $('pt-paste-area').value = text;
      $('pt-paste-box').classList.remove('hidden');
      setStatus('Could not auto-parse — review and click Apply.', 'warn');
    }
  } catch (_) {
    // Clipboard permission denied — show paste box
    $('pt-paste-area').value = '';
    $('pt-paste-box').classList.remove('hidden');
    $('pt-paste-area').focus();
    setStatus('Paste patient info into the box below.', 'info');
  }
}

function applyPatientPaste() {
  const text = $('pt-paste-area').value;
  if (!text.trim()) return;
  _applyPatientData(_parsePatientText(text));
  $('pt-paste-box').classList.add('hidden');
  $('pt-paste-area').value = '';
}

function cancelAutoGenerate() {
  if (autoGenerateTimeout) { clearTimeout(autoGenerateTimeout); autoGenerateTimeout = null; }
  $('auto-generate-bar').classList.add('hidden');
  $('countdown-progress').style.animation = 'none';
}


async function clearForNextPatient() {
  if (isRecording) return;   // safety: don't clear mid-recording
  cancelAutoGenerate();

  // Gather current patient info
  const patientInfo = {
    patient_name: $('patient-name').value.trim(),
    patient_dob:  $('patient-dob').value.trim(),
    health_card:  $('health-card').value.trim(),
    patient_email: $('patient-email').value.trim(),
    service_date: $('service-date').value.trim(),
    service_time: $('service-time').value.trim(),
    visit_type:   $('visit-type').value
  };
  // Only save if at least one field is filled
  const hasData = Object.values(patientInfo).some(v => v && v !== '' && v !== 'standard');
  if (hasData) {
    try {
      await fetch('/api/update-session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patientInfo),
      });
      setStatus('Patient info saved.', 'success');
    } catch (e) {
      setStatus('Failed to save patient info.', 'error');
    }
  }

  // Patient fields
  $('patient-name').value  = '';
  $('patient-dob').value   = '';
  $('health-card').value   = '';
  $('patient-email').value = '';
  $('service-date').value  = '';
  $('service-time').value  = '';
  // Patient-submitted info
  if ($('patient-submitted-info')) $('patient-submitted-info').value = '';
  // Transcript + note
  $('transcript').value = '';
  $('note-full').value  = '';
  // ICD9 chips
  $('icd9-chips').innerHTML = '';
  $('icd9-row').classList.add('hidden');
  // Audio player
  $('audio-player').src = '';
  $('audio-player').classList.add('hidden');
  $('post-record-actions').classList.add('hidden');
  // Buttons
  $('btn-generate').disabled = true;
  $('btn-copy').disabled     = true;
  $('btn-oscar').disabled    = true;
  if ($('btn-billing')) $('btn-billing').disabled = true;
  // State
  currentNote         = null;
  lastAudioBlob       = null;
  loadedAudioFilename = null;
  $('el-timer').textContent = '00:00';
  $('el-timer').classList.remove('amber');
  setStatus('Ready for next patient.', 'info');
  $('patient-name').focus();
}

// ── Generate note ─────────────────────────────────────────────
$('btn-generate').addEventListener('click', () => { cancelAutoGenerate(); generateNote(); });

async function generateNote() {
  if (isGenerating) return;
  const transcript = $('transcript').value.trim();
  const patientSubmittedInfo = $('patient-submitted-info').value.trim();
  if (!transcript) { setStatus('No transcript to generate from.', 'warn'); return; }
  isGenerating = true;
  $('btn-generate').disabled = true;
  $('btn-generate').textContent = '\u23F3 Generating...';
  $('note-full').value = '';
  setStatus('Sending to local AI\u2026', 'info');
  const res = await fetch('/api/generate-note', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      transcript,
      patient_submitted_info: patientSubmittedInfo,
      template_id:  $('template-select').value,
      socket_id:    socket.id,
      patient_name: $('patient-name').value.trim(),
      patient_dob:  $('patient-dob').value.trim(),
      health_card:  $('health-card').value.trim(),
      visit_type:   $('visit-type').value,
    }),
  });
  if (!res.ok) {
    isGenerating = false;
    $('btn-generate').disabled = false;
    $('btn-generate').textContent = '\u{1F4C4} Generate SOAP Note';
    setStatus('Server busy, try again.', 'warn');
  }
}

// ── Render note ───────────────────────────────────────────────
// Ensure common medical sub-headings always start on their own line
const _SUBHEADINGS = [
  'HPI','PMHx','FHx','SHx','ROS','Allergies','Allergy','Medications','Medication','Meds',
  'Vitals','Vital signs',
  'Physical examination','Physical Examination','Physical exam','Physical Exam',
  'HEENT','Cardiovascular','Respiratory','Abdomen','MSK','Musculoskeletal','Skin','Neuro','Neurological',
  'Neck','Chest','Lungs','Heart','Back','Extremities','Lymph nodes',
  'Investigations','Labs','Imaging','Bloodwork',
  'Referrals','Referral','Follow-up','Follow up','Followup',
  'Patient education','Return precautions','Sick note','DDx',
  'Mental status','Mental Status','Cognitive',
];
const _SUBHEAD_RE = new RegExp(
  '([^\\n])[ \\t]*((?:' + _SUBHEADINGS.map(s => s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|') + '):)',
  'gi'
);
function _fixSubheadings(text) {
  // Run twice in case two adjacent subheadings need fixing after first pass
  return text.replace(_SUBHEAD_RE, '$1\n$2').replace(_SUBHEAD_RE, '$1\n$2');
}

function renderNote(note) {
  // Freeform (meeting / non-medical) note: dump full_text and hide ICD-9 chips.
  if (note && note.note_format === 'freeform') {
    $('note-full').value = (note.full_text || note.raw || '').trim();
    $('icd9-chips').innerHTML = '';
    $('icd9-row').classList.add('hidden');
    return;
  }
  let text = '';
  // Helper to bullet each line of content
  if (note.subjective) text += 'S:\n' + note.subjective + '\n\n';
  if (note.objective)  text += 'O:\n' + note.objective  + '\n\n';
  for (const [key, content] of Object.entries(note.extra_sections || {})) {
    const label = key.replace(/_/g, ' ').toUpperCase();
    text += label + ':\n' + content + '\n\n';
  }
  if (note.assessment) text += 'A:\n' + note.assessment + '\n\n';
  if (note.plan)       text += 'P:\n' + note.plan;
  $('note-full').value = _fixSubheadings(text.trim());

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
      soap_note:    currentNote,
      patient_name: $('patient-name').value,
      patient_dob:  $('patient-dob').value,
      health_card:  $('health-card').value,
      visit_type:   $('visit-type').value,
    }),
  });
}
function checkBillingUpgrade() {
  if (document.body.classList.contains('meeting-mode')) return;
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
function startWaveform(stream, ptStream) {
  const ac = new AudioContext();
  const source = ac.createMediaStreamSource(stream);
  analyser = ac.createAnalyser(); analyser.fftSize = 256;
  source.connect(analyser);
  if (ptStream) {
    analyserPt = ac.createAnalyser(); analyserPt.fftSize = 256;
    ac.createMediaStreamSource(ptStream).connect(analyserPt);
  } else {
    analyserPt = null;
  }
  drawWave();
}
function drawOneWave(canvas, node, color) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#1a1d29';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!node) {
    // no signal — draw flat line
    ctx.strokeStyle = '#333648';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(0, canvas.height / 2);
    ctx.lineTo(canvas.width, canvas.height / 2);
    ctx.stroke();
    return;
  }
  const data = new Uint8Array(node.frequencyBinCount);
  node.getByteTimeDomainData(data);
  ctx.lineWidth = 1.5; ctx.strokeStyle = color;
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
function drawWave() {
  animFrame = requestAnimationFrame(drawWave);
  drawOneWave($('waveform'),    analyser,   '#4a7df5');  // Dr — blue
  drawOneWave($('waveform-pt'), analyserPt, '#3ecf8e');  // Pt — green
}

// ── Templates ─────────────────────────────────────────────────
let _templateCache = [];
async function loadTemplates() {
  const list = await fetch('/api/templates').then(r => r.json());
  _templateCache = list;
  const sel = $('template-select');
  sel.innerHTML = '';
  list.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.id; opt.textContent = t.name;
    if (t.category) opt.dataset.category = t.category;
    sel.appendChild(opt);
  });
  const saved = localStorage.getItem('defaultTemplate');
  if (saved && [...sel.options].some(o => o.value === saved)) sel.value = saved;
  renderTemplateList(list);
  applyTemplateMode();
}
function _currentTemplate() {
  const id = $('template-select').value;
  return _templateCache.find(t => t.id === id) || null;
}
function applyTemplateMode() {
  const t = _currentTemplate();
  const isMeeting = !!(t && t.category === 'meeting');
  document.body.classList.toggle('meeting-mode', isMeeting);
  // Relabel & re-placeholder the primary subject field
  const nameInput = $('patient-name');
  if (isMeeting) {
    nameInput.placeholder = 'Meeting title / subject';
    nameInput.title = 'Meeting title or subject';
  } else {
    nameInput.placeholder = 'Patient name';
    nameInput.title = '';
  }
  // Hide medical-only inputs in meeting mode
  ['patient-dob', 'health-card', 'patient-email', 'visit-type'].forEach(id => {
    const el = $(id); if (el) el.classList.toggle('hidden', isMeeting);
  });
  // Disable EMR / billing actions in meeting mode (no clinical context)
  const oscar = $('btn-oscar'); if (oscar) oscar.classList.toggle('hidden', isMeeting);
  const bill  = $('btn-billing'); if (bill)  bill.classList.toggle('hidden', isMeeting);
  // Update note-area placeholder
  const note = $('note-full');
  if (note) note.placeholder = isMeeting
    ? 'Meeting notes will stream here as the AI generates them…\n\nPaste a transcript on the left and click Generate, or record the meeting.'
    : 'SOAP note will stream here as the AI generates it…\n\nPaste a transcript on the left and click Generate, or record a consultation.';
  // Update generate button label
  const gen = $('btn-generate');
  if (gen && !isGenerating) gen.textContent = isMeeting ? '📝 Generate Meeting Notes' : '📄 Generate SOAP Note';
}
$('template-select').addEventListener('change', () => {
  localStorage.setItem('defaultTemplate', $('template-select').value);
  applyTemplateMode();
});
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

// ── Keyboard shortcuts ────────────────────────────────────────
document.addEventListener('keydown', e => {
  const tag = document.activeElement ? document.activeElement.tagName : '';
  const inInput = ['INPUT', 'TEXTAREA', 'SELECT'].includes(tag);
  const modal = !$('template-modal').classList.contains('hidden');
  if (modal || inInput) return;

  if (e.key === ' ') {
    e.preventDefault();
    if (!isRecording && !$('btn-record').disabled) startRecording();
    else if (isRecording && !$('btn-stop').disabled) stopRecording();
  }
  if (e.key === 'p' || e.key === 'P') {
    if (isRecording) togglePause();
  }
  if (e.key === 'Escape') {
    if (isRecording) cancelRecording();
  }
  if (e.key === 'g' || e.key === 'G') {
    if (!$('btn-generate').disabled) { cancelAutoGenerate(); generateNote(); }
  }
  if (e.key === 'c' || e.key === 'C') {
    if (!$('btn-copy').disabled)
      navigator.clipboard.writeText($('note-full').value)
        .then(() => setStatus('Copied to clipboard.', 'success'));
  }
  if (e.key === 'Escape') cancelAutoGenerate();
});

// ── Patient history ───────────────────────────────────────────
let _historyOpen = false;
const _today = (() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; })();
let _historyDate = _today; // YYYY-MM-DD local date; empty = all

function _escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Init date picker
(function () {
  const pick = $('history-date-pick');
  if (!pick) return;
  pick.value = _historyDate;
  pick.addEventListener('change', () => {
    _historyDate = pick.value; // '' means all dates
    if (_historyOpen) loadHistory();
  });
})();

function toggleEncounters() {
  _historyOpen = !_historyOpen;
  $('history-list').classList.toggle('hidden', !_historyOpen);
  $('history-chevron').textContent = _historyOpen ? '▴' : '▾';
  if (_historyOpen) loadEncounters();
}
// Back-compat alias (any callers still using the old name)
const toggleHistory = toggleEncounters;
window.toggleEncounters = toggleEncounters;

async function loadEncounters() {
  try {
    const dateParam = _historyDate || 'all';
    const [notes, audioRes] = await Promise.all([
      fetch('/api/history?date=' + encodeURIComponent(dateParam)).then(r => r.json()).catch(() => []),
      fetch('/api/audio').then(r => r.json()).catch(() => ({ entries: [], total_bytes: 0 })),
    ]);
    const audioEntries = (audioRes && audioRes.entries) || [];
    // Index audio by filename for joining with notes
    const audioByName = {};
    audioEntries.forEach(a => { if (a.filename) audioByName[a.filename] = a; });

    // Build merged encounter list
    const showingAll = !_historyDate;
    const encounters = [];
    const usedAudio = new Set();

    (notes || []).forEach(n => {
      const audio = n.audio_filename ? audioByName[n.audio_filename] : null;
      if (audio) usedAudio.add(n.audio_filename);
      encounters.push({
        kind: 'note',
        ts: n.timestamp || (audio && audio.mtime) || '',
        note: n,
        audio,
      });
    });

    // Orphan audio (no matching note). "Matching only" date filter:
    // when a specific date is selected, only include orphans whose mtime
    // falls on that date; when "all dates", include every orphan.
    audioEntries.forEach(a => {
      if (usedAudio.has(a.filename)) return;
      if (!showingAll) {
        const aDate = a.mtime ? new Date(a.mtime).toISOString().slice(0, 10) : '';
        if (aDate !== _historyDate) return;
      }
      encounters.push({
        kind: 'orphan',
        ts: a.mtime || '',
        note: null,
        audio: a,
      });
    });

    // Sort newest first
    encounters.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));

    // Header badges
    const countBadge = $('history-count');
    if (encounters.length) { countBadge.textContent = encounters.length; countBadge.classList.remove('hidden'); }
    else { countBadge.classList.add('hidden'); }
    const sizeLabel = $('audio-size');
    if (sizeLabel) sizeLabel.textContent = audioEntries.length ? _fmtBytes(audioRes.total_bytes) : '';

    if (!_historyOpen) return;
    const list = $('history-list');
    list.innerHTML = '';
    if (!encounters.length) {
      list.innerHTML = '<div class="history-empty">No encounters found.</div>';
      return;
    }

    let lastDate = null;
    encounters.forEach(enc => {
      const entryDate = enc.ts ? enc.ts.slice(0, 10) : '';
      if (showingAll && entryDate && entryDate !== lastDate) {
        const sep = document.createElement('div');
        sep.className = 'history-date-sep';
        const today     = new Date().toISOString().slice(0, 10);
        const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
        if (entryDate === today)           sep.textContent = 'Today';
        else if (entryDate === yesterday)  sep.textContent = 'Yesterday';
        else {
          const d = new Date(entryDate + 'T12:00:00');
          sep.textContent = d.toLocaleDateString(undefined, { weekday:'short', month:'short', day:'numeric' });
        }
        list.appendChild(sep);
        lastDate = entryDate;
      }
      list.appendChild(_renderEncounterRow(enc));
    });
  } catch(_) {}
}

// Back-compat aliases
const loadHistory = loadEncounters;
const loadAudioList = loadEncounters;
window.loadEncounters = loadEncounters;

function _renderEncounterRow(enc) {
  const row = document.createElement('div');
  const isOrphan = enc.kind === 'orphan';
  row.className = 'history-row enc-row' + (isOrphan ? ' orphan' : '');

  const n = enc.note;
  const a = enc.audio;
  const time = enc.ts ? enc.ts.substring(11, 16) : '';

  let name, summary;
  if (n) {
    name = n.patient_name || 'Unknown';
    summary = n.summary || (n.icd9_codes || []).slice(0, 2).join(', ') || '';
  } else {
    name = '(orphan audio)';
    summary = a ? a.filename : '';
  }

  const sizeStr = a ? _fmtBytes(a.size_bytes) : '';

  // Action buttons (conditional)
  const audioActions = a ? (
    `<button class="hrow-btn enc-play"  title="Play audio inline">&#9654;</button>` +
    `<button class="hrow-btn enc-load"  title="Load audio into main player">&#8682;</button>` +
    `<button class="hrow-btn enc-dl"    title="Download audio">&#11015;</button>` +
    `<button class="hrow-btn enc-adel"  title="Delete audio file">&#127908;&#10005;</button>`
  ) : '';
  const noteActions = n ? (
    `<button class="hrow-btn hrow-edit" title="Rename patient">&#9998;</button>` +
    `<button class="hrow-btn hrow-del"  title="Delete note">&#128465;</button>`
  ) : '';

  row.innerHTML =
    `<div class="hrow-top">` +
      `<span class="hrow-name" title="${_escHtml(a ? a.filename : (n && n.filename) || '')}">${_escHtml(name)}</span>` +
      `<span class="hrow-time">${_escHtml(time)}</span>` +
      (sizeStr ? `<span class="arow-size">${_escHtml(sizeStr)}</span>` : '') +
      `<span class="hrow-actions">${audioActions}${noteActions}</span>` +
    `</div>` +
    (summary ? `<div class="hrow-summary">${_escHtml(summary)}</div>` : '');

  // Note actions
  if (n) {
    row.querySelector('.hrow-edit').addEventListener('click', ev => { ev.stopPropagation(); _historyRename(row, n); });
    row.querySelector('.hrow-del' ).addEventListener('click', ev => { ev.stopPropagation(); _historyDelete(n.filename, row); });
    row.addEventListener('click', () => loadHistoryNote(n.filename));
  }

  // Audio actions
  if (a) {
    row.querySelector('.enc-play').addEventListener('click', ev => {
      ev.stopPropagation();
      const existing = row.querySelector('.arow-audio');
      if (existing) { existing.remove(); return; }
      const audio = document.createElement('audio');
      audio.className = 'arow-audio';
      audio.controls = true;
      audio.src = '/api/audio/' + encodeURIComponent(a.filename);
      row.appendChild(audio);
      audio.play().catch(() => {});
    });
    row.querySelector('.enc-load').addEventListener('click', ev => {
      ev.stopPropagation();
      loadedAudioFilename = a.filename;
      lastAudioBlob = null;
      $('audio-player').src = '/api/audio/' + encodeURIComponent(a.filename);
      $('audio-player').classList.remove('hidden');
      $('post-record-actions').style.display = 'flex';
      $('post-record-actions').classList.remove('hidden');
      setStatus('Loaded ' + a.filename + ' into player.', 'info');
    });
    row.querySelector('.enc-dl').addEventListener('click', ev => {
      ev.stopPropagation();
      const link = document.createElement('a');
      link.href = '/api/audio/' + encodeURIComponent(a.filename);
      link.download = a.filename;
      document.body.appendChild(link); link.click(); link.remove();
    });
    row.querySelector('.enc-adel').addEventListener('click', async ev => {
      ev.stopPropagation();
      const msg = n
        ? `Delete this audio file?\n\nNote for "${n.patient_name || 'patient'}" will be kept but audio playback will be removed.`
        : 'Delete this orphan audio file? This cannot be undone.';
      if (!confirm(msg)) return;
      try {
        const r = await fetch('/api/audio/' + encodeURIComponent(a.filename), { method: 'DELETE' });
        if (r.ok) { setStatus('Audio file deleted.', 'info'); loadEncounters(); }
        else { setStatus('Delete failed.', 'error'); }
      } catch(_) { setStatus('Delete failed.', 'error'); }
    });
  }

  return row;
}

function _historyRename(row, entry) {
  const nameSpan = row.querySelector('.hrow-name');
  const original = nameSpan.textContent;
  const input = document.createElement('input');
  input.className = 'hrow-rename-input';
  input.value = original;
  nameSpan.replaceWith(input);
  input.focus(); input.select();
  let saved = false;
  async function _save() {
    if (saved) return; saved = true;
    const newName = input.value.trim() || original;
    const span = document.createElement('span');
    span.className = 'hrow-name';
    span.textContent = newName;
    input.replaceWith(span);
    if (newName === original) return;
    try {
      await fetch('/api/history/' + encodeURIComponent(entry.filename), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patient_name: newName }),
      });
      entry.patient_name = newName;
      setStatus('Patient name updated.', 'success');
    } catch(_) { setStatus('Rename failed.', 'error'); }
  }
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { input.value = original; input.blur(); }
  });
  input.addEventListener('blur', _save, { once: true });
}

async function _historyDelete(filename, row) {
  if (!confirm('Delete this note? This cannot be undone.')) return;
  try {
    const r = await fetch('/api/history/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (r.ok) {
      row.remove();
      const list = $('history-list');
      const remaining = list.querySelectorAll('.history-row').length;
      if (!remaining) list.innerHTML = '<div class="history-empty">No encounters found.</div>';
      const badge = $('history-count');
      if (remaining > 0) badge.textContent = remaining;
      else badge.classList.add('hidden');
      setStatus('Note deleted.', 'info');
    }
  } catch(_) { setStatus('Delete failed.', 'error'); }
}


async function loadHistoryNote(filename) {
  try {
    const data = await fetch('/api/history/' + encodeURIComponent(filename)).then(r => r.json());
    if (data.patient_name) $('patient-name').value = data.patient_name;
    if (data.patient_dob)  $('patient-dob').value  = data.patient_dob;
    if (data.health_card)  $('health-card').value  = data.health_card;
    if (data.transcript)   $('transcript').value   = data.transcript;
    if (data.soap_note) {
      currentNote = data.soap_note;
      renderNote(data.soap_note);
      $('btn-copy').disabled    = false;
      $('btn-oscar').disabled   = false;
      $('btn-billing').disabled = false;
    }
    // Restore audio player if the note has an associated audio file
    if (data.audio_filename) {
      loadedAudioFilename = data.audio_filename;
      lastAudioBlob = null;
      $('audio-player').src = '/api/audio/' + encodeURIComponent(data.audio_filename);
      $('audio-player').classList.remove('hidden');
      $('post-record-actions').style.display = 'flex';
      $('post-record-actions').classList.remove('hidden');
    }
    setStatus('Loaded: ' + (data.patient_name || 'patient') + '.', 'success');
  } catch(_) { setStatus('Failed to load note.', 'error'); }
}

// ── Restore latest audio on load ─────────────────────────────
async function loadLatestAudio() {
  try {
    const data = await fetch('/api/audio/latest').then(r => r.json());
    if (!data.filename) return;
    // Only show if no fresh blob from this session
    if (lastAudioBlob) return;
    loadedAudioFilename = data.filename;
    $('audio-player').src = '/api/audio/' + encodeURIComponent(data.filename);
    $('audio-player').classList.remove('hidden');
    $('post-record-actions').style.display = 'flex';
    $('post-record-actions').classList.remove('hidden');
    setStatus('Last recording restored. Hit Re-transcribe to re-run.', 'info');
  } catch(_) {}
}

// ── Init ──────────────────────────────────────────────────────
(async () => {
  await loadTemplates();
  updatePills();
  loadHistory();
  // loadLatestAudio(); // Removed auto-restore
  setInterval(updatePills, 30000);
})();

// Add restore button logic
window.restoreLastRecording = async function() {
  await loadLatestAudio();
}

// ── Audio files panel (list / play / delete / purge) ─────────
let _audioOpen = false;

function _fmtBytes(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + ' MB';
  return (n/1024/1024/1024).toFixed(2) + ' GB';
}

window.purgeAudio = async function(mode) {
  let confirmMsg = '';
  let body = { mode };
  if (mode === 'all') {
    confirmMsg = 'PURGE ALL session audio files? This cannot be undone.\n\n(Notes are kept, but audio playback is removed.)';
  } else if (mode === 'orphans') {
    confirmMsg = 'Delete all audio files that are NOT linked to a saved note?';
  } else if (mode === 'older_than') {
    const days = prompt('Delete audio older than how many days?', '30');
    if (days === null) return;
    body.days = parseInt(days, 10);
    if (isNaN(body.days) || body.days < 0) { alert('Invalid days value.'); return; }
    confirmMsg = `Delete all audio older than ${body.days} days?`;
  }
  if (!confirm(confirmMsg)) return;
  try {
    const res = await fetch('/api/audio/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => r.json());
    if (res.status === 'ok') {
      setStatus(`Purged ${res.deleted_count} file(s), freed ${_fmtBytes(res.freed_bytes)}.`, 'success');
      loadAudioList();
    } else {
      setStatus('Purge failed: ' + (res.error || 'unknown'), 'error');
    }
  } catch(e) { setStatus('Purge failed.', 'error'); }
}
