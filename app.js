// ===== STATE =====
const state = {
  step: 'welcome',   // welcome | askGoal | customGoal | askStart | askBreaks | done
  goalHours: null,
  startTime: null,
  breakMinutes: 0,
  liveInterval: null,
};

// ===== STORAGE =====
function getDateKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function getHistory() {
  try { return JSON.parse(localStorage.getItem('workbot_history') || '{}'); }
  catch { return {}; }
}

function saveDay() {
  if (!state.startTime || !state.goalHours) return;
  const r = calcResult();
  const key = getDateKey(state.startTime);
  const history = getHistory();
  history[key] = {
    goalHours: state.goalHours,
    workedMinutes: r.worked,
    breakMinutes: state.breakMinutes,
    startTime: fmtTime(state.startTime),
    pct: Math.min(110, r.pct)
  };
  localStorage.setItem('workbot_history', JSON.stringify(history));
}

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
    saveDay(); // persist progress every 30s
  }, 30000);
}

// ===== REPORTS =====
let reportState = { tab: 'weekly', monthOffset: 0 };

function openReports() {
  const overlay = document.getElementById('reportsOverlay');
  overlay.style.display = 'flex';
  requestAnimationFrame(() => overlay.classList.add('reports-open'));
  renderReports();
}

function closeReports() {
  const overlay = document.getElementById('reportsOverlay');
  overlay.classList.remove('reports-open');
  setTimeout(() => { overlay.style.display = 'none'; }, 280);
}

function switchReportTab(tab) {
  reportState.tab = tab;
  document.getElementById('tabWeekly').classList.toggle('active', tab === 'weekly');
  document.getElementById('tabMonthly').classList.toggle('active', tab === 'monthly');
  renderReports();
}

function changeMonth(delta) {
  reportState.monthOffset = Math.max(-24, Math.min(0, reportState.monthOffset + delta));
  renderReports();
}

function renderReports() {
  const body = document.getElementById('reportsBody');
  body.innerHTML = reportState.tab === 'weekly' ? renderWeekly() : renderMonthly();
}

// ===== WEEKLY REPORT =====
function getWeekDays(refDate = new Date()) {
  const d = new Date(refDate);
  const dow = d.getDay();
  const diff = dow === 0 ? -6 : 1 - dow;
  d.setDate(d.getDate() + diff);
  const days = [];
  for (let i = 0; i < 7; i++) {
    const dd = new Date(d);
    dd.setDate(d.getDate() + i);
    days.push(dd);
  }
  return days;
}

function renderWeekly() {
  const history = getHistory();
  const days = getWeekDays();
  const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const today = getDateKey(new Date());

  const dayData = days.map((d, i) => ({
    key: getDateKey(d), date: d, entry: history[getDateKey(d)] || null, name: dayNames[i]
  }));

  const workedDays = dayData.filter(d => d.entry && d.key <= today);
  const totalWorked = workedDays.reduce((s, d) => s + d.entry.workedMinutes, 0);
  const goalMetDays = workedDays.filter(d => d.entry.pct >= 100).length;
  const avgMins = workedDays.length > 0 ? Math.round(totalWorked / workedDays.length) : 0;

  let streak = 0;
  for (const { key, entry } of [...dayData].filter(d => d.key <= today).reverse()) {
    if (entry && entry.pct >= 100) streak++;
    else break;
  }

  const maxMins = Math.max(...dayData.map(d => d.entry ? d.entry.workedMinutes : 0), 60);
  const weekRange = `${days[0].toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} \u2013 ${days[6].toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`;

  const bars = dayData.map(({ key, entry, name }) => {
    const isToday = key === today;
    const isFuture = key > today;
    const h = entry ? Math.max(4, Math.round((entry.workedMinutes / maxMins) * 100)) : 0;
    const pct = entry ? entry.pct : 0;
    const cls = isFuture ? 'bar-future' : (!entry ? 'bar-empty' : pct >= 100 ? 'bar-met' : pct >= 60 ? 'bar-mid' : 'bar-low');
    return `
      <div class="week-bar-col${isToday ? ' is-today' : ''}">
        <div class="week-bar-val">${entry ? fmtDuration(entry.workedMinutes) : (isFuture ? '' : '\u2014')}</div>
        <div class="week-bar-track"><div class="week-bar-fill ${cls}" style="height:${h}%"></div></div>
        <div class="week-bar-label">${name}</div>
        ${isToday ? '<div class="today-pip"></div>' : ''}
      </div>`;
  }).join('');

  return `
    <div class="report-section-title">
      <span>\uD83D\uDCC5 This Week</span>
      <span class="report-range">${weekRange}</span>
    </div>
    <div class="week-bars">${bars}</div>
    <div class="report-stats-row">
      <div class="rstat"><span class="rstat-val">${fmtDuration(totalWorked)}</span><span class="rstat-label">Total</span></div>
      <div class="rstat"><span class="rstat-val">${avgMins > 0 ? fmtDuration(avgMins) : '\u2014'}</span><span class="rstat-label">Daily Avg</span></div>
      <div class="rstat"><span class="rstat-val${goalMetDays > 0 ? ' val-green' : ''}">${goalMetDays} / ${workedDays.length}</span><span class="rstat-label">Goals Met</span></div>
      <div class="rstat"><span class="rstat-val${streak > 0 ? ' val-orange' : ''}">${streak}${streak > 0 ? ' \uD83D\uDD25' : ''}</span><span class="rstat-label">Streak</span></div>
    </div>`;
}

// ===== MONTHLY HEATMAP =====
function renderMonthly() {
  const history = getHistory();
  const now = new Date();
  const target = new Date(now.getFullYear(), now.getMonth() + reportState.monthOffset, 1);
  const year = target.getFullYear();
  const month = target.getMonth();
  const monthName = target.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
  const today = getDateKey(now);
  const daysInMonth = new Date(year, month + 1, 0).getDate();

  let startDow = new Date(year, month, 1).getDay();
  startDow = startDow === 0 ? 6 : startDow - 1;

  const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const cells = [];
  for (let i = 0; i < startDow; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);
  while (cells.length % 7 !== 0) cells.push(null);

  let totalWorked = 0, daysGoalMet = 0, workedCount = 0;
  let bestDay = { mins: 0, label: '' };

  const cellsHtml = cells.map(dayNum => {
    if (dayNum === null) return '<div class="heat-cell heat-pad"></div>';
    const d = new Date(year, month, dayNum);
    const key = getDateKey(d);
    const entry = history[key];
    const isFuture = key > today;
    const isToday = key === today;
    const shortDate = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    let level = 0, tip = shortDate;
    if (entry) {
      totalWorked += entry.workedMinutes;
      workedCount++;
      if (entry.pct >= 100) daysGoalMet++;
      if (entry.workedMinutes > bestDay.mins) bestDay = { mins: entry.workedMinutes, label: shortDate };
      if (entry.pct >= 110) level = 5;
      else if (entry.pct >= 100) level = 4;
      else if (entry.pct >= 60)  level = 3;
      else if (entry.pct >= 30)  level = 2;
      else level = 1;
      tip = `${shortDate}: ${fmtDuration(entry.workedMinutes)} \u00B7 ${entry.pct}% of goal`;
    } else if (!isFuture) {
      tip = `${shortDate}: No data`;
    }
    return `<div class="heat-cell level-${level}${isToday ? ' heat-today' : ''}${isFuture ? ' heat-future' : ''}" title="${tip}"><span class="heat-num">${dayNum}</span></div>`;
  }).join('');

  const avgMins = workedCount > 0 ? Math.round(totalWorked / workedCount) : 0;
  const canPrev = reportState.monthOffset > -24;
  const canNext = reportState.monthOffset < 0;

  return `
    <div class="monthly-nav">
      <button class="month-nav-btn" onclick="changeMonth(-1)" ${canPrev ? '' : 'disabled'}>&#8249;</button>
      <span class="monthly-title">${monthName}</span>
      <button class="month-nav-btn" onclick="changeMonth(1)" ${canNext ? '' : 'disabled'}>&#8250;</button>
    </div>
    <div class="heat-wrap">
      <div class="heat-day-labels">${dayLabels.map(l => `<div class="heat-dlabel">${l}</div>`).join('')}</div>
      <div class="heat-grid">${cellsHtml}</div>
    </div>
    <div class="heat-legend">
      <span class="legend-label">Less</span>
      ${[0,1,2,3,4,5].map(l => `<div class="heat-cell level-${l} legend-cell"></div>`).join('')}
      <span class="legend-label">More</span>
    </div>
    <div class="report-stats-row">
      <div class="rstat"><span class="rstat-val">${fmtDuration(totalWorked)}</span><span class="rstat-label">Total</span></div>
      <div class="rstat"><span class="rstat-val">${avgMins > 0 ? fmtDuration(avgMins) : '\u2014'}</span><span class="rstat-label">Daily Avg</span></div>
      <div class="rstat"><span class="rstat-val${daysGoalMet > 0 ? ' val-green' : ''}">${daysGoalMet} days</span><span class="rstat-label">Goals Met</span></div>
      <div class="rstat"><span class="rstat-val">${bestDay.mins > 0 ? fmtDuration(bestDay.mins) : '\u2014'}</span><span class="rstat-label">Best Day</span></div>
    </div>`;
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
  saveDay(); // save when session starts
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
  if (val === 'custom') {
    state.step = 'customGoal';
    document.getElementById('btn6h').classList.remove('active');
    document.getElementById('btn8h').classList.remove('active');
    await addMessage('user', 'Custom hours');
    await botSay(`✏️ Sure! How many hours would you like to set as your goal? Type any number (e.g. <code>7</code>, <code>6.5</code>).`, 400);
    document.getElementById('userInput').focus();
    return;
  }

  if (state.step !== 'askGoal' && state.step !== 'customGoal') return;

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
  saveDay(); // final save before clearing
  state.step = 'askGoal';
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
