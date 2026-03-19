"use strict";
const { MockReplay } = require("./mock-replay");

/**
 * Handle mock chat requests
 * This function should be called instead of the real Claude API when MOCK_MODE is enabled
 */
function handleMockChat(req, res, mockReplay) {
    const { message, sessionId } = req.body;
    
    if (!mockReplay) {
        return res.status(500).json({ error: "Mock replay not initialized" });
    }

    const mockMsg = mockReplay.getNextMessage();
    if (!mockMsg) {
        return res.status(500).json({ error: "No more mock messages available" });
    }

    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no");
    res.flushHeaders();

    function sendEvent(event, data) {
        if (!res.writableEnded) {
            res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
        }
    }

    const pingInterval = setInterval(() => {
        if (!res.writableEnded) {
            res.write(`: ping\n\n`);
        }
    }, 15000);

    // Send system init
    sendEvent("system", {
        sessionId: sessionId || "mock-session",
        mcpServers: [{ name: "simworld", status: "connected" }]
    });

    // Simulate thinking
    setTimeout(() => {
        mockMsg.thinking.forEach(think => {
            sendEvent("text", { delta: think });
        });

        // Simulate tool calls
        mockMsg.tools.forEach((tool, idx) => {
            setTimeout(() => {
                const toolId = `mock-tool-${idx}-${Date.now()}`;
                const displayName = tool.name.replace(/^mcp__\w+__/, "");
                
                sendEvent("tool_start", {
                    id: toolId,
                    name: tool.name,
                    displayName: displayName
                });

                if (tool.input) {
                    sendEvent("tool_input", {
                        delta: typeof tool.input === "string" ? tool.input : JSON.stringify(tool.input)
                    });
                }

                // Send tool result after a delay
                setTimeout(() => {
                    const result = typeof tool.result === "string" 
                        ? tool.result 
                        : JSON.stringify(tool.result);
                    
                    sendEvent("tool_result", {
                        toolUseId: toolId,
                        result: result.slice(0, 2000),
                        isError: false
                    });
                }, 300);
            }, idx * 600);
        });

        // Send text responses
        setTimeout(() => {
            mockMsg.text.forEach(text => {
                sendEvent("text", { delta: text });
            });

            // End the stream
            clearInterval(pingInterval);
            sendEvent("done", {
                sessionId: sessionId || "mock-session",
                isError: false,
                costUsd: 0,
                latestScreenshot: null
            });
            res.end();
        }, mockMsg.tools.length * 600 + 500);
    }, 500);
}

module.exports = { handleMockChat };
