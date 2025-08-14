// Telegram MCQ scheduler bot (Render-ready, 30-second interval, keep-alive HTTP server)
// Upload format (private chat):
// /upload QUESTION || OPT1 || OPT2 || ... || CORRECT_INDEX   (0-based index)

const { Telegraf } = require('telegraf');
const fs = require('fs').promises;
const path = require('path');
const express = require('express');

// ===== ENV =====
const BOT_TOKEN = process.env.BOT_TOKEN;
const TARGET_CHAT_ID = process.env.TARGET_CHAT_ID;
const DB_FILE = process.env.DB_PATH || path.join(__dirname, 'mcq_store.json');
const PORT = process.env.PORT || 3000; // Render provides PORT env

if (!BOT_TOKEN) {
  console.error('ERROR: BOT_TOKEN not set');
  process.exit(1);
}
if (!TARGET_CHAT_ID) {
  console.error('ERROR: TARGET_CHAT_ID not set');
  process.exit(1);
}

const bot = new Telegraf(BOT_TOKEN);

// ===== JSON "DB" =====
async function loadDB() {
  try {
    const txt = await fs.readFile(DB_FILE, 'utf8');
    const db = JSON.parse(txt);
    if (!db.mcqs) db.mcqs = [];
    if (!db.nextId) db.nextId = db.mcqs.length ? Math.max(...db.mcqs.map(m => m.id)) + 1 : 1;
    return db;
  } catch {
    return { nextId: 1, mcqs: [] };
  }
}
async function saveDB(db) {
  await fs.writeFile(DB_FILE, JSON.stringify(db, null, 2), 'utf8');
}
async function addMCQ(question, options, correctIndex, uploadedBy) {
  const db = await loadDB();
  const entry = {
    id: db.nextId++,
    question,
    options,
    correct_index: correctIndex,
    uploaded_by: uploadedBy || null,
    uploaded_at: Date.now(),
    posted_at: null,
    telegram_poll_id: null
  };
  db.mcqs.push(entry);
  await saveDB(db);
  return entry;
}
async function getNextUnpostedMCQ() {
  const db = await loadDB();
  return db.mcqs.find(m => m.posted_at === null) || null;
}
async function markMCQPosted(id, pollId) {
  const db = await loadDB();
  const idx = db.mcqs.findIndex(m => m.id === id);
  if (idx !== -1) {
    db.mcqs[idx].posted_at = Date.now();
    db.mcqs[idx].telegram_poll_id = pollId || null;
    await saveDB(db);
  }
}
async function countPendingMCQs() {
  const db = await loadDB();
  return db.mcqs.filter(m => m.posted_at === null).length;
}

// ===== Scheduler (30 seconds) =====
let schedulerHandle = null;
let schedulerPaused = true;
const INTERVAL_MS = 30 * 1000;

async function postNextMCQIfAny() {
  const mcq = await getNextUnpostedMCQ();
  if (!mcq) {
    console.log('[scheduler] Queue empty -> pausing');
    pauseScheduler();
    return;
  }
  try {
    const res = await bot.telegram.sendPoll(TARGET_CHAT_ID, mcq.question, mcq.options, {
      type: 'quiz',
      correct_option_id: mcq.correct_index,
      is_anonymous: false
    });
    const pollId = res?.poll?.id || null;
    await markMCQPosted(mcq.id, pollId);
    console.log(`[scheduler] Posted MCQ id=${mcq.id}`);
  } catch (err) {
    console.error('[scheduler] sendPoll failed:', err?.message || err);
    // leave unposted to retry next tick
  }
}
function startScheduler() {
  if (schedulerHandle) { schedulerPaused = false; return; }
  schedulerPaused = false;
  postNextMCQIfAny();
  schedulerHandle = setInterval(postNextMCQIfAny, INTERVAL_MS);
  console.log('[scheduler] started (30s interval)');
}
function pauseScheduler() {
  if (schedulerHandle) clearInterval(schedulerHandle);
  schedulerHandle = null;
  schedulerPaused = true;
}

(async () => {
  if (await countPendingMCQs() > 0) startScheduler();
  else console.log('[init] No pending MCQs — scheduler paused');
})();

// ===== Bot Commands =====
bot.start(ctx => ctx.replyWithMarkdown(
`Hi! I post MCQs as quiz polls to *${TARGET_CHAT_ID}* every 30 seconds.

Private chat commands:
/upload QUESTION || OPT1 || OPT2 || ... || INDEX
/preview  - preview next MCQ
/pending  - pending count`
));

bot.command('upload', async ctx => {
  if (ctx.chat.type !== 'private') return ctx.reply('⚠️ Uploads only in private chat.');
  const payload = ctx.message.text.replace('/upload', '').trim();
  if (!payload) return ctx.reply('Usage: /upload QUESTION || OPT1 || OPT2 || ... || INDEX');

  const parts = payload.split('||').map(p => p.trim()).filter(Boolean);
  if (parts.length < 3) return ctx.reply('Need: question + 2+ options + index.');

  const correctIndex = Number(parts.pop());
  const question = parts.shift();
  const options = parts;

  if (!Number.isInteger(correctIndex) || correctIndex < 0 || correctIndex >= options.length)
    return ctx.reply('Invalid INDEX (0-based, within options count).');
  if (options.length < 2 || options.length > 10)
    return ctx.reply('Options must be 2..10.');

  const entry = await addMCQ(question, options, correctIndex, ctx.from.id);
  await ctx.reply(`✅ MCQ queued (id: ${entry.id}).`);
  if (schedulerPaused) {
    startScheduler();
    await ctx.reply('▶️ Scheduler restarted (posting every 30s).');
  }
});

bot.command('preview', async ctx => {
  if (ctx.chat.type !== 'private') return ctx.reply('Preview only in private chat.');
  const mcq = await getNextUnpostedMCQ();
  if (!mcq) return ctx.reply('No queued MCQs.');
  let text = `*Next MCQ (id: ${mcq.id}):*\n${mcq.question}\n\n`;
  mcq.options.forEach((o, i) => {
    const ok = i === mcq.correct_index ? ' ✅' : '';
    text += `${i}. ${o}${ok}\n`;
  });
  return ctx.replyWithMarkdown(text);
});

bot.command('pending', async ctx => {
  return ctx.reply(`Pending: ${await countPendingMCQs()}`);
});

// Convenience: if user sends "||" line without /upload, treat it as upload
bot.on('message', async ctx => {
  if (ctx.chat.type === 'private') {
    const t = (ctx.message.text || '').trim();
    if (t.includes('||') && !t.startsWith('/')) {
      ctx.message.text = '/upload ' + t;
      return bot.handleUpdate({ update_id: 0, message: ctx.message });
    }
  }
});

// ===== Keep-alive HTTP server for Render & UptimeRobot =====
const app = express();
app.get('/', (_, res) => res.send('Bot is running'));
app.get('/healthz', async (_, res) => {
  res.json({
    ok: true,
    schedulerPaused,
    pending: await countPendingMCQs(),
    time: new Date().toISOString()
  });
});
app.listen(PORT, () => console.log(`HTTP keep-alive on :${PORT}`));

// Launch bot
bot.launch().then(() => console.log('Bot launched'));

// Graceful shutdown (Render)
process.on('SIGTERM', async () => {
  console.log('SIGTERM received, shutting down...');
  try { await bot.stop('SIGTERM'); } catch {}
  process.exit(0);
});
