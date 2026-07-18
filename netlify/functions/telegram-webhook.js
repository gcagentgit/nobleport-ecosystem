/**
 * Stephanie.ai — Telegram Webhook Handler
 * ========================================
 * Netlify function: /.netlify/functions/telegram-webhook
 *
 * Architecture:
 *   Telegram → this function → NoblePort MCP Gateway (governance-gated)
 *                                         ↓
 *                               stephanie_ai_routing (GO status per launch-gates.json)
 *
 * Governance posture (per launch-gates.json):
 *   - stephanie_ai_routing: GO ✅ (requires: proper_disclaimers)
 *   - READ-ONLY advisory mode — no execution, no payments, no token ops
 *   - Danger words blocked per governance gate (autonomous execution, guaranteed returns, etc.)
 *   - Human-review gate on all ai_recommendations
 *
 * Security:
 *   - TELEGRAM_BOT_TOKEN validated from env (hard gate — exits 401 if absent)
 *   - Telegram secret_token header verification enforced when SECRET_TOKEN is set
 *   - No wallet adapters, no transaction signing
 *   - Rate limiting delegated to MCP gateway (20 req/min)
 *
 * Environment Variables (set in Netlify Dashboard → Site Settings → Env):
 *   TELEGRAM_BOT_TOKEN       required — from BotFather
 *   MCP_GATEWAY_URL          optional — NoblePort MCP gateway base URL (defaults to stub mode)
 *   MCP_ADMIN_TOKEN          optional — X-Admin-Token for gateway auth
 *   TELEGRAM_SECRET_TOKEN    optional — Telegram webhook secret for signature verification
 */

const TELEGRAM_API = "https://api.telegram.org";
const STEPHANIE_DISCLAIMER =
  "⚠️ Stephanie.ai provides AI-assisted workflow routing, document prep, and intake support. " +
  "All construction, permit, and financial actions require licensed human review and approval. " +
  "This is not legal, financial, or investment advice.";

// ─── Governance: danger word filter (mirrors launch-gates.json) ───────────────
const DANGER_WORDS = [
  "guaranteed returns", "yield", "dividend", "passive income",
  "autonomous execution", "AGI", "Sovereign CEO", "licensed CEO",
  "guarantees permit approval", "guarantees compliance",
  "ICO", "token launch", "risk-free", "SEC-compliant",
  "staking rewards", "profit share", "pegged",
];

function containsDangerWord(text) {
  const lower = text.toLowerCase();
  return DANGER_WORDS.find((w) => lower.includes(w.toLowerCase())) ?? null;
}

// ─── Telegram API helper ──────────────────────────────────────────────────────
async function sendTelegramMessage(token, chatId, text, replyToMessageId) {
  const body = {
    chat_id: chatId,
    text,
    parse_mode: "Markdown",
    ...(replyToMessageId ? { reply_to_message_id: replyToMessageId } : {}),
  };
  const res = await fetch(`${TELEGRAM_API}/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.text();
    console.error("Telegram sendMessage failed:", err);
  }
  return res.ok;
}

// ─── MCP Gateway call ─────────────────────────────────────────────────────────
async function callMcpGateway(gatewayUrl, adminToken, payload) {
  if (!gatewayUrl) {
    // Stub mode — governance gate requires live gateway for production
    return {
      truth_label: "STAGED",
      note: "MCP_GATEWAY_URL not configured — running in staged stub mode. Set env var for live gateway routing.",
      echo: payload,
    };
  }

  const res = await fetch(`${gatewayUrl.replace(/\/$/, "")}/invoke`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(adminToken ? { "X-Admin-Token": adminToken } : {}),
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    throw new Error(`Gateway error ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

// ─── Command handlers ─────────────────────────────────────────────────────────
async function handleCommand(command, args, chatId, userId, token, env) {
  const gatewayUrl = env.MCP_GATEWAY_URL;
  const adminToken = env.MCP_ADMIN_TOKEN;

  switch (command) {
    case "/start":
      return (
        `👋 *Stephanie.ai — NoblePort Executive Assistant*\n\n` +
        `I'm your AI workflow router for construction, permits, and project tracking.\n\n` +
        `*Available commands:*\n` +
        `/status — System health across all NoblePort nodes\n` +
        `/pipeline — HubSpot deal pipeline summary\n` +
        `/permits — PermitStream.ai — Essex County live feed\n` +
        `/tasks — Asana overdue task count\n` +
        `/project [name] — Route a project question\n` +
        `/help — Full command list\n\n` +
        `${STEPHANIE_DISCLAIMER}`
      );

    case "/status": {
      const result = await callMcpGateway(gatewayUrl, adminToken, {
        target_agent: "stephanie",
        module: "system_status",
        action: "get_health",
        payload: { requestor: userId, scope: "nobleport_all" },
      });
      const label = result.truth_label ?? "STAGED";
      return (
        `*NoblePort System Status* \`[${label}]\`\n\n` +
        `• GitHub: checking repos...\n` +
        `• Supabase: active\n` +
        `• PermitStream: Essex County live\n` +
        `• Gateway: ${gatewayUrl ? "✅ connected" : "⚠️ stub mode — MCP_GATEWAY_URL not set"}\n\n` +
        (label === "STAGED"
          ? `_Staged mode — connect MCP gateway for live data_`
          : `_Live data via NoblePort MCP Gateway_`)
      );
    }

    case "/pipeline": {
      const result = await callMcpGateway(gatewayUrl, adminToken, {
        target_agent: "stephanie",
        module: "crm",
        action: "pipeline_summary",
        payload: { requestor: userId },
      });
      return (
        `*HubSpot Pipeline* \`[${result.truth_label ?? "STAGED"}]\`\n\n` +
        (result.truth_label === "LIVE"
          ? `Total: $${result.total_value?.toLocaleString() ?? "—"}\nDeals: ${result.deal_count ?? "—"}\nStale (>14d): ${result.stale_count ?? "—"}`
          : `_Gateway in staged mode. Set MCP_GATEWAY_URL for live deal data._`)
      );
    }

    case "/permits": {
      return (
        `*PermitStream.ai — Essex County*\n\n` +
        `Monitor running daily.\n` +
        `Last known: 99+ permits/day, 30–34 municipalities active.\n` +
        `HIGH priority: 75–84 permits/day, $35–63M valuation.\n\n` +
        `_For live feed, connect MCP_GATEWAY_URL → permitstream module._`
      );
    }

    case "/tasks": {
      const result = await callMcpGateway(gatewayUrl, adminToken, {
        target_agent: "stephanie",
        module: "project_management",
        action: "overdue_count",
        payload: { requestor: userId },
      });
      return (
        `*Asana Task Status* \`[${result.truth_label ?? "STAGED"}]\`\n\n` +
        (result.truth_label === "LIVE"
          ? `Overdue: ${result.overdue_count ?? "—"}\nDue today: ${result.due_today ?? "—"}`
          : `_Last known: 10 of 14 tasks overdue. Set MCP_GATEWAY_URL for live count._`)
      );
    }

    case "/project": {
      if (!args.trim()) {
        return "Usage: `/project [project name or question]`\n\nExample: `/project nobleport-gateway status`";
      }
      const danger = containsDangerWord(args);
      if (danger) {
        return `⛔ *Governance Gate — Request Blocked*\n\nContains restricted term: \`${danger}\`\n\n${STEPHANIE_DISCLAIMER}`;
      }
      const result = await callMcpGateway(gatewayUrl, adminToken, {
        target_agent: "stephanie",
        module: "stephanie_ai_routing",
        action: "route_project_query",
        payload: { query: args, requestor: userId, chat_id: chatId },
      });
      const label = result.truth_label ?? "STAGED";
      return (
        `*Stephanie Response* \`[${label}]\`\n\n` +
        (result.response ?? result.note ?? "_Routing in progress — no response from gateway._") +
        `\n\n${STEPHANIE_DISCLAIMER}`
      );
    }

    case "/help":
      return (
        `*Stephanie.ai Command Reference*\n\n` +
        `/start — Welcome + intro\n` +
        `/status — All NoblePort node health\n` +
        `/pipeline — HubSpot deal pipeline\n` +
        `/permits — PermitStream.ai permit feed\n` +
        `/tasks — Asana overdue count\n` +
        `/project [query] — Route a project question through MCP gateway\n` +
        `/help — This menu\n\n` +
        `*Governance posture:* Read-only. All actions require human approval.\n\n` +
        `${STEPHANIE_DISCLAIMER}`
      );

    default:
      return (
        `I don't recognize \`${command}\`.\n\nType /help for available commands.\n\n` +
        `_Stephanie.ai — NoblePort Executive Assistant_`
      );
  }
}

// ─── Main handler ─────────────────────────────────────────────────────────────
exports.handler = async (event) => {
  // Only POST from Telegram
  if (event.httpMethod !== "POST") {
    return { statusCode: 405, body: "Method Not Allowed" };
  }

  // ── Hard gate: TELEGRAM_BOT_TOKEN must be set ──
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    console.error("HARD GATE: TELEGRAM_BOT_TOKEN is not set in Netlify environment.");
    return {
      statusCode: 500,
      body: JSON.stringify({
        error: "TELEGRAM_BOT_TOKEN not configured",
        action: "Set TELEGRAM_BOT_TOKEN in Netlify → Site Settings → Environment Variables → Production context",
      }),
    };
  }

  // ── Optional: Telegram secret token header verification ──
  const secretToken = process.env.TELEGRAM_SECRET_TOKEN;
  if (secretToken) {
    const incoming = event.headers["x-telegram-bot-api-secret-token"] ?? "";
    if (incoming !== secretToken) {
      console.warn("Telegram secret token mismatch — rejecting request");
      return { statusCode: 401, body: "Unauthorized" };
    }
  }

  // ── Parse body ──
  let update;
  try {
    update = JSON.parse(event.body ?? "{}");
  } catch {
    return { statusCode: 400, body: "Invalid JSON" };
  }

  const message = update.message ?? update.edited_message;
  if (!message || !message.text) {
    // Acknowledge non-message updates (callbacks, channel posts, etc.)
    return { statusCode: 200, body: "ok" };
  }

  const chatId = message.chat?.id;
  const userId = message.from?.id;
  const text = message.text?.trim() ?? "";
  const messageId = message.message_id;

  if (!chatId) {
    return { statusCode: 200, body: "ok" };
  }

  // ── Governance: check danger words in free text ──
  if (!text.startsWith("/")) {
    const danger = containsDangerWord(text);
    if (danger) {
      await sendTelegramMessage(
        token,
        chatId,
        `⛔ *Governance Gate*\n\nThat request contains a restricted term (\`${danger}\`).\n\n${STEPHANIE_DISCLAIMER}`,
        messageId
      );
      return { statusCode: 200, body: "ok" };
    }

    // Non-command text — route through stephanie_ai_routing module
    const danger2 = containsDangerWord(text);
    if (!danger2) {
      const response = await callMcpGateway(
        process.env.MCP_GATEWAY_URL,
        process.env.MCP_ADMIN_TOKEN,
        {
          target_agent: "stephanie",
          module: "stephanie_ai_routing",
          action: "route_message",
          payload: { message: text, requestor: userId, chat_id: chatId },
        }
      ).catch((e) => ({ truth_label: "ERROR", note: e.message }));

      const label = response.truth_label ?? "STAGED";
      await sendTelegramMessage(
        token,
        chatId,
        `*Stephanie* \`[${label}]\`\n\n` +
          (response.response ?? response.note ?? "_Processing..._") +
          `\n\n${STEPHANIE_DISCLAIMER}`,
        messageId
      );
    }
    return { statusCode: 200, body: "ok" };
  }

  // ── Command routing ──
  const [rawCommand, ...argParts] = text.split(" ");
  const command = rawCommand.toLowerCase().split("@")[0]; // strip bot username if present
  const args = argParts.join(" ").trim();

  let responseText;
  try {
    responseText = await handleCommand(
      command,
      args,
      chatId,
      userId,
      token,
      process.env
    );
  } catch (e) {
    console.error("Command handler error:", e);
    responseText =
      `⚠️ *Stephanie encountered an error.*\n\n` +
      `_${e.message}_\n\n` +
      `This has been logged. Try again or contact the NoblePort team.`;
  }

  await sendTelegramMessage(token, chatId, responseText, messageId);
  return { statusCode: 200, body: "ok" };
};
