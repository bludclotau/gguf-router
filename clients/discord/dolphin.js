const ROUTER_URL = process.env.GGUF_ROUTER_URL || "http://localhost:9000/route";

function buildPrompt(systemMessage, conversationHistory, userMessage) {
  let prompt = "";

  if (systemMessage) {
    prompt += `[System]\n${systemMessage}\n\n`;
  }

  if (Array.isArray(conversationHistory)) {
    for (const turn of conversationHistory) {
      if (turn.role === "user") {
        prompt += `[User]\n${turn.content}\n\n`;
      } else if (turn.role === "assistant") {
        prompt += `[Assistant]\n${turn.content}\n\n`;
      }
    }
  }

  if (userMessage) {
    prompt += `[User]\n${userMessage}\n\n`;
  }

  prompt += "[Assistant]\n";

  return prompt;
}

async function callTool(name, args, opts = {}) {
  const url = "http://localhost:9000/tool";
  const payload = {
    tool: name,
    args: args,
    user_id: opts.user_id || "unknown",
    bot_name: opts.bot_name || process.env.BOT_NAME || "discord"
  };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal
    });
    return await resp.json();
  } finally {
    clearTimeout(timeout);
  }
}

async function callRouter(payload) {
  const url = ROUTER_URL;
  const timeoutMs = payload && payload.agent ? 90000 : 30000;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);

      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal
      });

      clearTimeout(timeout);
      return await resp.json();
    } catch (e) {
      await new Promise(r => setTimeout(r, 500 * (attempt + 1)));
    }
  }
  return { clean: "Router timeout.", raw: null };
}

async function dolphinInfer(prompt, nPredict = 256, timeoutMs = 30000, opts = {}) {
  const persona = opts.persona || process.env.BOT_NAME || "unknown";
  const task = opts.task || process.env.BOT_TASK || process.env.TASK || "general";
  const botName = opts.bot_name || process.env.BOT_NAME || "discord";
  const userId = opts.user_id || "unknown";
  const data = await callRouter({
    prompt,
    persona,
    task,
    user_id: userId,
    bot_name: botName,
    n_predict: nPredict,
    stream: false,
    agent: !!opts.agent
  });

  return formatAgentReply(data);
}

function formatAgentReply(data) {
  if (!data) return "Router timeout.";
  if (data.stopped === "blocked" || data.blocked) {
    const msg =
      (typeof data.clean === "string" && data.clean.trim()) ||
      (typeof data.reply === "string" && data.reply.trim()) ||
      "";
    return msg || "I hit a wall on that page (login, CAPTCHA, or bot check) and stopped.";
  }
  const reply =
    (typeof data.clean === "string" && data.clean) ||
    (typeof data.reply === "string" && data.reply) ||
    (typeof data.content === "string" && data.content) ||
    data?.choices?.[0]?.message?.content ||
    "";
  if (typeof reply === "string" && reply.trim()) {
    return reply.trim();
  }
  return "Router timeout.";
}

function formatToolResult(result) {
  const body = result && result.result !== undefined ? result.result : result;
  if (body && typeof body === "object") {
    if (body.status === "blocked") {
      return body.message || `Blocked (${body.reason || "challenge"}) on ${body.url || "that page"}.`;
    }
    return body.error || body.text || body.content || JSON.stringify(body);
  }
  return String(body ?? "no result");
}

async function editWhenDone(pending, text, fallback) {
  const out = String(text || fallback || "Couldn't finish that.").slice(0, 1900);
  if (pending) {
    try {
      await pending.edit(out);
      return;
    } catch (err) {
      console.error("pending.edit failed:", err);
    }
  }
}

module.exports = {
  dolphinInfer,
  buildPrompt,
  callTool,
  callRouter,
  formatAgentReply,
  formatToolResult,
  editWhenDone
};
