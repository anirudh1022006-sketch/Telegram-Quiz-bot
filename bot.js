const { Telegraf } = require('telegraf');
const fs = require('fs').promises;
const path = require('path');
const express = require('express');

// Env variables
const BOT_TOKEN = process.env.BOT_TOKEN;
const CHANNEL_ID = process.env.TARGET_CHAT_ID; // Channel username or ID
const GROUP_ID = process.env.DISCUSSION_GROUP_ID; // Linked discussion group ID
const DB_FILE = process.env.DB_PATH || path.join(__dirname, 'mcq_store.json');

if (!BOT_TOKEN || !CHANNEL_ID || !GROUP_ID) {
  console.error("❌ Missing environment variables. Check BOT_TOKEN, TARGET_CHAT_ID, DISCUSSION_GROUP_ID.");
  process.exit(1);
}

const bot = new Telegraf(BOT_TOKEN);

// ===== Database =====
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
async function addMCQ(question, options, correctIndex, uploader) {
  const db = await loadDB();
  const entry = { id: db.nextId++, question, options, correctIndex, uploader };
  db.mcqs.push(entry);
  await saveDB(db);
  return entry;
}
async function getNextMCQ() {
  const db = await loadDB();
  return db.mcqs.length ? db.mcqs[0] : null;
}
async function removeMCQ(id) {
  const db = await loadDB();
  db.mcqs = db.mcqs.filter(q => q.id !== id);
  await saveDB(db);
}

// ===== Upload Command =====
bot.command('upload', async ctx => {
  if (ctx.chat.type !== 'private') return ctx.reply('Private chat only.');

  const payload = ctx.message.text.replace('/upload', '').trim();
  if (!payload) return ctx.reply('Usage: /upload QUESTION, OPT1, OPT2, ..., INDEX');

  const parts = payload.split(/\|\||\||,/).map(p => p.trim()).filter(Boolean);

  if (parts.length < 3) return ctx.reply('Need: question + 2+ options + index.');

  const correctIndex = Number(parts.pop());
  const question = parts.shift();
  const options = parts;

  if (!Number.isInteger(correctIndex) || correctIndex < 0 || correctIndex >= options.length)
    return ctx.reply('Invalid index.');

  const entry = await addMCQ(question, options, correctIndex, ctx.from.id);
  ctx.reply(`✅ MCQ queued (id: ${entry.id}).`);
  if (schedulerPaused) startScheduler();
});

// ===== Preview Command =====
bot.command('preview', async ctx => {
  const db = await loadDB();
  if (!db.mcqs.length) return ctx.reply('No questions queued.');
  const q = db.mcqs[0];
  ctx.reply(`Next Q: ${q.question}\nOptions: ${q.options.join(', ')}\nCorrect index: ${q.correctIndex}`);
});

// ===== Scheduler =====
let schedulerPaused = true;
let schedulerTimer = null;

async function postNextQuestion() {
  const mcq = await getNextMCQ();
  if (!mcq) {
    schedulerPaused = true;
    console.log('No more questions, scheduler paused.');
    return;
  }

  // 1. Post question text to channel (works in channels)
  await bot.telegram.sendMessage(
    CHANNEL_ID,
    `📢 *New MCQ:*\n${mcq.question}\n\n${mcq.options.map((opt, i) => `${i + 1}. ${opt}`).join('\n')}`,
    { parse_mode: 'Markdown' }
  );

  // 2. Post interactive quiz poll to discussion group (works there)
  await bot.telegram.sendPoll(
    GROUP_ID,
    mcq.question,
    mcq.options,
    {
      type: 'quiz',
      correct_option_id: mcq.correctIndex,
      is_anonymous: false
    }
  );

  await removeMCQ(mcq.id);
}

function startScheduler() {
  schedulerPaused = false;
  if (schedulerTimer) clearInterval(schedulerTimer);
  schedulerTimer = setInterval(postNextQuestion, 30 * 1000); // 30 seconds
  console.log('Scheduler started (30s interval)');
}

// ===== Keep-alive Server for Render =====
const app = express();
app.get('/', (req, res) => res.send('Bot is running.'));
app.listen(process.env.PORT || 3000, () => console.log('Keep-alive server running.'));

// ===== Start Bot =====
bot.launch();
console.log('Bot started.');
