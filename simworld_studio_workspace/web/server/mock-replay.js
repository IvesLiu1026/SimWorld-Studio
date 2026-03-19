"use strict";
const fs = require("fs");
const path = require("path");

/**
 * Parse mock responses from a text file
 * Format:
 * === MESSAGE N ===
 * THINKING: <thinking text>
 * TOOL_START: <tool_name>
 * TOOL_INPUT: <json_input>
 * TOOL_RESULT: <json_result>
 * TEXT: <response text>
 * === END MESSAGE N ===
 */
class MockReplay {
    constructor(filePath) {
        this.filePath = filePath;
        this.messages = [];
        this.currentIndex = 0;
        this.load();
    }

    load() {
        if (!fs.existsSync(this.filePath)) {
            console.warn(`[mock-replay] File not found: ${this.filePath}`);
            return;
        }

        const content = fs.readFileSync(this.filePath, "utf-8");
        const messageBlocks = content.split(/=== MESSAGE \d+ ===/).filter(b => b.trim());

        for (const block of messageBlocks) {
            const message = this.parseMessageBlock(block);
            if (message) {
                this.messages.push(message);
            }
        }

        console.log(`[mock-replay] Loaded ${this.messages.length} mock messages from ${this.filePath}`);
    }

    parseMessageBlock(block) {
        const lines = block.split("\n").map(l => l.trim()).filter(l => l);
        // steps preserves the original interleaved order of thinking/tool/text
        const message = {
            input: null,
            thinking: [],
            tools: [],
            text: [],
            steps: []
        };

        let currentTool = null;

        for (const line of lines) {
            if (line.startsWith("=== END MESSAGE")) break;

            if (line.startsWith("USER_INPUT:")) {
                message.input = line.replace("USER_INPUT:", "").trim();
            } else if (line.startsWith("THINKING:")) {
                const text = line.replace("THINKING:", "").trim();
                message.thinking.push(text);
                message.steps.push({ type: "thinking", text });
            } else if (line.startsWith("TOOL_START:")) {
                if (currentTool) {
                    message.tools.push(currentTool);
                    message.steps.push({ type: "tool", ...currentTool });
                }
                currentTool = { name: line.replace("TOOL_START:", "").trim(), input: null, result: null };
            } else if (line.startsWith("TOOL_INPUT:") && currentTool) {
                try { currentTool.input = JSON.parse(line.replace("TOOL_INPUT:", "").trim()); }
                catch (e) { currentTool.input = line.replace("TOOL_INPUT:", "").trim(); }
            } else if (line.startsWith("TOOL_RESULT:") && currentTool) {
                try { currentTool.result = JSON.parse(line.replace("TOOL_RESULT:", "").trim()); }
                catch (e) { currentTool.result = line.replace("TOOL_RESULT:", "").trim(); }
                message.tools.push(currentTool);
                message.steps.push({ type: "tool", ...currentTool });
                currentTool = null;
            } else if (line.startsWith("TEXT:")) {
                const content = line.replace("TEXT:", "").trim();
                message.text.push(content);
                message.steps.push({ type: "text", content });
            } else if (currentTool) {
                // Multi-line tool input/result continuation
                if (currentTool.input !== null && currentTool.result === null && typeof currentTool.input === "string") {
                    currentTool.input += "\n" + line;
                } else if (currentTool.result !== null && typeof currentTool.result === "string") {
                    currentTool.result += "\n" + line;
                }
            }
        }

        if (currentTool) {
            message.tools.push(currentTool);
            message.steps.push({ type: "tool", ...currentTool });
        }

        return message;
    }

    getNextMessage() {
        if (this.currentIndex >= this.messages.length) {
            return null;
        }
        const message = this.messages[this.currentIndex];
        this.currentIndex++;
        return message;
    }

    reset() {
        this.currentIndex = 0;
    }

    hasMore() {
        return this.currentIndex < this.messages.length;
    }

    peekNextInput() {
        if (this.currentIndex >= this.messages.length) return null;
        return this.messages[this.currentIndex].input || null;
    }
}

module.exports = { MockReplay };
