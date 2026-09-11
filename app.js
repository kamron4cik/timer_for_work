// ===== STATE =====
const state = {
  step: 'welcome',   // welcome | askGoal | customGoal | askStart | askBreaks | done
  goalHours: null,
  startTime: null,
  breakMinutes: 0,
  liveInterval: null,
};

// ===== LIVE CLOCK =====
function updateClock() {
  const now = new Date();
  document.getElementById('liveClock').textContent =
    now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}
updateClock();
setInterval(updateClock, 1000);

// ===== CHAT HELPERS =====
function addMessage(who, html, delay = 0) {
  return new Promise(resolve => {
    setTimeout(() => {
      const win = document.getElementById('chatWindow');
      const wrap = document.createElement('div');
      wrap.className = `msg ${who}`;
      if (who === 'bot') {
        wrap.innerHTML = `
          <div class="msg-avatar">🤖</div>
          <div class="bubble">${html}</div>`;
      } else {
        wrap.innerHTML = `
          <div class="bubble">${html}</div>
          <div class="msg-avatar" style="background:linear-gradient(135deg,#f75fa0,#facc15)">👤</div>`;
      }
      win.appendChild(wrap);
      win.scrollTop = win.scrollHeight;
      resolve();
    }, delay);
  });
}

function showTyping() {
  const win = document.getElementById('chatWindow');
  const wrap = document.createElement('div');
  wrap.id = 'typing';
  wrap.className = 'msg bot';
  wrap.innerHTML = `
    <div class="msg-avatar">🤖</div>
    <div class="bubble">
      <div class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
    </div>`;
  win.appendChild(wrap);
  win.scrollTop = win.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById('typing');
  if (t) t.remove();
}

async function botSay(html, typingMs = 700) {
  showTyping();
  await delay(typingMs);
  removeTyping();
  await addMessage('bot', html);
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

// ===== TIME PARSING =====
function parseTime(input) {
  // Accepts: 9, 9:00, 09:00, 9am, 9:30am, 14:30, etc.
  input = input.trim().toLowerCase();
  let h, m = 0;

  // hh:mm or h:mm
  const colonMatch = input.match(/^(\d{1,2}):(\d{2})\s*(am|pm)?$/);
  if (colonMatch) {
    h = parseInt(colonMatch[1]);
    m = parseInt(colonMatch[2]);
    if (colonMatch[3] === 'pm' && h < 12) h += 12;
    if (colonMatch[3] === 'am' && h === 12) h = 0;
  } else {
    // plain number or with am/pm
    const plainMatch = input.match(/^(\d{1,2})\s*(am|pm)?$/);
    if (plainMatch) {
      h = parseInt(plainMatch[1]);
      if (plainMatch[2] === 'pm' && h < 12) h += 12;
      if (plainMatch[2] === 'am' && h === 12) h = 0;
    } else {
      return null;
    }
  }
  if (h < 0 || h > 23 || m < 0 || m > 59) return null;
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d;
}

function parseMinutes(input) {
  const n = parseInt(input.trim());
  if (isNaN(n) || n < 0 || n > 480) return null;
  return n;
}

function fmtTime(d) {
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
}

function fmtDuration(totalMins) {
  const h = Math.floor(totalMins / 60);
  const m = totalMins % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

// ===== RESULT CALCULATION =====
function calcResult() {
  const now = new Date();
  const startMs = state.startTime.getTime();
  const elapsed = Math.max(0, Math.round((now - startMs) / 60000));
  const worked = Math.max(0, elapsed - state.breakMinutes);
  const goalMins = state.goalHours * 60;
  const remaining = Math.max(0, goalMins - worked);

  let endTime = null;
  if (remaining > 0) {
    endTime = new Date(now.getTime() + remaining * 60000);
  }

  const pct = Math.min(100, Math.round((worked / goalMins) * 100));

  return { elapsed, worked, goalMins, remaining, endTime, pct };
}

function updateProgress(r) {
  const sec = document.getElementById('progressSection');
  sec.style.display = 'block';
  document.getElementById('progressPct').textContent = `${r.pct}%`;
  document.getElementById('progressFill').style.width = `${r.pct}%`;
  document.getElementById('workedStat').textContent = `Worked: ${fmtDuration(r.worked)}`;
  document.getElementById('remainStat').textContent = r.remaining > 0
    ? `Remaining: ${fmtDuration(r.remaining)}`
    : `Remaining: 🎉 Done!`;
  document.getElementById('goalStat').textContent = `Goal: ${fmtDuration(r.goalMins)}`;
}

function resultCard(r) {
  const doneClass = r.remaining === 0 ? 'green' : (r.pct >= 75 ? 'yellow' : 'cyan');
  const workedClass = r.pct >= 100 ? 'green' : 'violet';
  return `
    <div class="result-card">
      <div class="stat">
        <span class="stat-label">Started</span>
        <span class="stat-value cyan">${fmtTime(state.startTime)}</span>
      </div>
      <div class="stat">
        <span class="stat-label">Current Time</span>
        <span class="stat-value violet">${fmtTime(new Date())}</span>
      </div>
      <div class="stat">
        <span class="stat-label">Break Taken</span>
        <span class="stat-value yellow">${fmtDuration(state.breakMinutes)}</span>
      </div>
      <div class="stat">
        <span class="stat-label">Net Worked</span>
        <span class="stat-value ${workedClass}">${fmtDuration(r.worked)}</span>
      </div>
      <div class="stat">
        <span class="stat-label">Goal</span>
        <span class="stat-value">${fmtDuration(r.goalMins)}</span>
      </div>
      <div class="stat">
        <span class="stat-label">${r.remaining > 0 ? 'Finish Time' : 'Status'}</span>
        <span class="stat-value ${doneClass}">${r.remaining > 0 ? fmtTime(r.endTime) : '🎉 Goal Met!'}</span>
      </div>
    </div>`;
}

// ===== LIVE UPDATE INTERVAL =====
function startLiveTracking() {
  if (state.liveInterval) clearInterval(state.liveInterval);
  state.liveInterval = setInterval(() => {
    const r = calcResult();
    updateProgress(r);
  }, 30000); // update every 30s
}

// ===== FLOW =====
async function startFlow() {
  await botSay(`👋 Hey there! I'm <strong>WorkBot</strong> — your daily hours assistant.<br><br>
    I'll help you track your working hours for today and tell you exactly when to finish. Let's go! 💪`, 800);
  await delay(400);
  await askGoal();
}

async function askGoal() {
  state.step = 'askGoal';
  await botSay(`⏱️ <strong>How many hours is your work goal today?</strong><br><br>
    Click a quick option below, or type a custom number (e.g. <code>7</code> or <code>7.5</code>).`, 600);
}

async function askStart() {
  state.step = 'askStart';
  await botSay(`🕗 <strong>What time did you start working today?</strong><br><br>
    Type your start time — e.g. <code>09:00</code>, <code>8:30am</code>, or just <code>9</code>.`, 600);
}

async function askBreaks() {
  state.step = 'askBreaks';
  await botSay(`☕ <strong>How many minutes of breaks have you taken?</strong><br><br>
    Enter <code>0</code> if none, or something like <code>30</code> for a 30-minute break.`, 600);
}

async function showResult() {
  state.step = 'done';
  const r = calcResult();
  updateProgress(r);
  startLiveTracking();

  const summary = r.remaining === 0
    ? `🎉 You've already hit your <strong>${fmtDuration(r.goalMins)}</strong> goal! Amazing work today!`
    : `Great! Here's your work summary for today. Keep it up — you're <strong>${r.pct}%</strong> there!`;

  await botSay(`${summary}${resultCard(r)}`, 800);
  await delay(400);
  await botSay(`💡 <em>Progress bar above updates every 30 seconds. Hit <strong>Reset</strong> to start over anytime.</em>`, 500);
}

// ===== HANDLE INPUT =====
async function handleSend(e) {
  e.preventDefault();
  const inp = document.getElementById('userInput');
  const val = inp.value.trim();
  if (!val) return;
  inp.value = '';
  await addMessage('user', val);

  if (state.step === 'askGoal' || state.step === 'customGoal') {
    const hours = parseFloat(val.replace('h', '').replace('hours', '').trim());
    if (isNaN(hours) || hours <= 0 || hours > 24) {
      await botSay(`🤔 That doesn't look right. Please enter a number like <code>6</code>, <code>7.5</code>, or <code>8</code>.`, 500);
      return;
    }
    state.goalHours = hours;
    document.getElementById('btn6h').classList.toggle('active', hours === 6);
    document.getElementById('btn8h').classList.toggle('active', hours === 8);
    await botSay(`✅ Goal set to <strong>${hours} hour${hours !== 1 ? 's' : ''}</strong>!`, 400);
    await delay(300);
    await askStart();
    return;
  }

  if (state.step === 'askStart') {
    const t = parseTime(val);
    if (!t) {
      await botSay(`🤔 I couldn't read that time. Try formats like <code>09:00</code>, <code>8:30am</code>, or just <code>9</code>.`, 500);
      return;
    }
    if (t > new Date()) {
      await botSay(`⚠️ That time is in the future! Please enter a start time that has already passed.`, 500);
      return;
    }
    state.startTime = t;
    await botSay(`✅ Start time set to <strong>${fmtTime(t)}</strong>!`, 400);
    await delay(300);
    await askBreaks();
    return;
  }

  if (state.step === 'askBreaks') {
    const mins = parseMinutes(val);
    if (mins === null) {
      await botSay(`🤔 Please enter break minutes as a number, e.g. <code>0</code>, <code>15</code>, or <code>60</code>.`, 500);
      return;
    }
    state.breakMinutes = mins;
    await botSay(`✅ Break time noted: <strong>${mins > 0 ? fmtDuration(mins) : 'no breaks'}</strong>.`, 400);
    await delay(300);
    await showResult();
    return;
  }

  if (state.step === 'done') {
    const lower = val.toLowerCase();
    if (lower.includes('update') || lower.includes('refresh') || lower.includes('now') || lower.includes('check')) {
      const r = calcResult();
      updateProgress(r);
      await botSay(`🔄 Updated! Here's your latest summary:${resultCard(r)}`, 500);
    } else if (lower.includes('reset') || lower.includes('restart')) {
      resetAll();
    } else {
      await botSay(`ℹ️ You can say <em>"update"</em> to refresh your stats, or click <strong>Reset</strong> to start a new session.`, 400);
    }
    return;
  }
}

// ===== QUICK SELECTS =====
async function quickSelect(val) {
  if (state.step === 'done' && val !== 'custom') {
    // Allow changing goal mid-session from done state
  }

  if (val === 'custom') {
    state.step = 'customGoal';
    document.getElementById('btn6h').classList.remove('active');
    document.getElementById('btn8h').classList.remove('active');
    await addMessage('user', 'Custom hours');
    await botSay(`✏️ Sure! How many hours would you like to set as your goal? Type any number (e.g. <code>7</code>, <code>6.5</code>).`, 400);
    document.getElementById('userInput').focus();
    return;
  }

  if (state.step !== 'askGoal' && state.step !== 'customGoal' && state.step !== 'welcome') return;

  state.goalHours = val;
  document.getElementById('btn6h').classList.toggle('active', val === 6);
  document.getElementById('btn8h').classList.toggle('active', val === 8);
  await addMessage('user', `${val} hour goal`);
  await botSay(`✅ Goal set to <strong>${val} hours</strong>!`, 400);
  await delay(300);
  await askStart();
}

// ===== RESET =====
function resetAll() {
  if (state.liveInterval) clearInterval(state.liveInterval);
  state.step = 'askGoal'; // set immediately so quickSelect guard works right away
  state.goalHours = null;
  state.startTime = null;
  state.breakMinutes = 0;
  document.getElementById('chatWindow').innerHTML = '';
  document.getElementById('progressSection').style.display = 'none';
  document.getElementById('btn6h').classList.remove('active');
  document.getElementById('btn8h').classList.remove('active');
  startFlow();
}

// ===== INIT =====
startFlow();
