#!/usr/bin/env node
"use strict";
/**
 * Patch index.js to add mock mode support
 * This script modifies the compressed index.js to add mock mode handling
 */

const fs = require("fs");
const path = require("path");

const indexFile = path.join(__dirname, "index.js");

if (!fs.existsSync(indexFile)) {
    console.error(`[patch-mock-mode] File not found: ${indexFile}`);
    process.exit(1);
}

let content = fs.readFileSync(indexFile, "utf-8");

// Check if already patched
if (content.includes("MOCK_MODE&&mockReplay")) {
    console.log("[patch-mock-mode] Already patched, skipping");
    process.exit(0);
}

// Find the app.post("/api/chat" section and add mock check
// Look for: e.flushHeaders();function a(d,r){e.writableEnded||e.write(`event: ${d}

if (!content.includes('e.flushHeaders();function a(d,r){e.writableEnded||e.write(`event: ${d}')) {
    console.error("[patch-mock-mode] Could not find flushHeaders pattern");
    process.exit(1);
}

// Add mock mode check right after flushHeaders
const mockCheck = `if(MOCK_MODE&&mockReplay){const mockMsg=mockReplay.getNextMessage();if(mockMsg){logToFile("chat",\`[MOCK] User: "\${t.slice(0,200)}"\`);function sendMock(d,r){e.writableEnded||e.write(\`event: \${d}\\ndata: \${JSON.stringify(r)}\\n\\n\`)}sendMock("system",{sessionId:n||"mock-session",mcpServers:[{name:"simworld",status:"connected"}]});setTimeout(()=>{mockMsg.thinking.forEach(think=>{sendMock("text",{delta:think})});mockMsg.tools.forEach((tool,idx)=>{setTimeout(()=>{const toolId=\`mock-tool-\${idx}-\${Date.now()}\`;sendMock("tool_start",{id:toolId,name:tool.name,displayName:tool.name.replace(/^mcp__\\w+__/,"")});if(tool.input){sendMock("tool_input",{delta:typeof tool.input==="string"?tool.input:JSON.stringify(tool.input)})}setTimeout(()=>{sendMock("tool_result",{toolUseId:toolId,result:typeof tool.result==="string"?tool.result:JSON.stringify(tool.result),isError:!1})},300)},idx*600)});setTimeout(()=>{mockMsg.text.forEach(text=>{sendMock("text",{delta:text})});sendMock("done",{sessionId:n||"mock-session",isError:!1,costUsd:0,latestScreenshot:null});e.end()},mockMsg.tools.length*600+500)},500);return}}e.status(500).json({error:"No more mock messages"})}`;

// Insert mock check after flushHeaders - use a more flexible pattern
content = content.replace(
    /e\.flushHeaders\(\);function a\(d,r\)\{e\.writableEnded\|\|e\.write\(`event: \$\{d\}/,
    `e.flushHeaders();${mockCheck}function a(d,r){e.writableEnded||e.write(\`event: \${d}`
);

// Backup original file
const backupFile = indexFile + ".backup";
if (!fs.existsSync(backupFile)) {
    fs.writeFileSync(backupFile, fs.readFileSync(indexFile));
    console.log(`[patch-mock-mode] Created backup: ${backupFile}`);
}

// Write patched content
fs.writeFileSync(indexFile, content);
console.log("[patch-mock-mode] Successfully patched index.js for mock mode support");
