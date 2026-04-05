'use strict';

const fs = require('fs');
const path = require('path');

const LOG_DIR = path.resolve(__dirname, '../../logs');
fs.mkdirSync(LOG_DIR, { recursive: true });

// Log level: 'debug' | 'info' | 'warn' | 'error'
const LOG_LEVEL = process.env.LOG_LEVEL || 'debug'; // dev default
const LEVELS = { debug: 0, info: 1, warn: 2, error: 3 };

function shouldLog(level) {
  return (LEVELS[level] || 0) >= (LEVELS[LOG_LEVEL] || 0);
}

function timestamp() {
  return new Date().toISOString();
}

// Separate log files per category
const streams = {};

function getStream(category) {
  const date = new Date().toISOString().slice(0, 10);
  const key = `${category}_${date}`;
  if (!streams[key]) {
    const filePath = path.join(LOG_DIR, `${category}_${date}.log`);
    streams[key] = fs.createWriteStream(filePath, { flags: 'a' });
  }
  return streams[key];
}

/**
 * Log a message.
 * @param {string} category - Log category: 'chat', 'agent', 'ucv', 'mcp', 'system'
 * @param {string} level - 'debug' | 'info' | 'warn' | 'error'
 * @param {string} message - Log message
 * @param {object} [data] - Optional structured data
 */
function log(category, level, message, data) {
  if (!shouldLog(level)) return;

  const ts = timestamp();
  const dataStr = data ? ' ' + JSON.stringify(data) : '';
  const line = `[${ts}] [${level.toUpperCase()}] [${category}] ${message}${dataStr}\n`;

  // Write to category-specific file
  try { getStream(category).write(line); } catch {}

  // Also write to combined log
  try { getStream('combined').write(line); } catch {}

  // Console output (colored for dev)
  const colors = { debug: '\x1b[90m', info: '\x1b[36m', warn: '\x1b[33m', error: '\x1b[31m' };
  const reset = '\x1b[0m';
  const color = colors[level] || '';
  process.stderr.write(`${color}[${category}] ${message}${dataStr}${reset}\n`);
}

// Convenience methods
const logger = {
  debug: (cat, msg, data) => log(cat, 'debug', msg, data),
  info:  (cat, msg, data) => log(cat, 'info', msg, data),
  warn:  (cat, msg, data) => log(cat, 'warn', msg, data),
  error: (cat, msg, data) => log(cat, 'error', msg, data),

  // Category-specific loggers
  chat:   (level, msg, data) => log('chat', level, msg, data),
  agent:  (level, msg, data) => log('agent', level, msg, data),
  ucv:    (level, msg, data) => log('ucv', level, msg, data),
  mcp:    (level, msg, data) => log('mcp', level, msg, data),
  system: (level, msg, data) => log('system', level, msg, data),
  ctx:    (level, msg, data) => log('ctx', level, msg, data),
};

module.exports = logger;
