/**
 * register-webhook — One-time Telegram webhook registration utility
 * =================================================================
 * Netlify function: /.netlify/functions/register-webhook
 *
 * Call this ONCE after setting TELEGRAM_BOT_TOKEN in Netlify env to register
 * the telegram-webhook endpoint with Telegram's servers.
 *
 * Usage:
 *   GET /.netlify/functions/register-webhook?admin=YOUR_ADMIN_TOKEN
 *
 * Security: requires NETLIFY_ADMIN_REGISTER_TOKEN env var to prevent
 * unauthorized webhook re-registration.
 *
 * After calling this, verify with:
 *   https://api.telegram.org/bot<TOKEN>/getWebhookInfo
 */

exports.handler = async (event) => {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    return {
      statusCode: 500,
      body: JSON.stringify({ error: "TELEGRAM_BOT_TOKEN not set. Cannot register webhook." }),
    };
  }

  // Simple admin gate — require a secret query param to prevent accidental calls
  const adminToken = process.env.NETLIFY_ADMIN_REGISTER_TOKEN ?? "";
  const provided = event.queryStringParameters?.admin ?? "";
  if (adminToken && provided !== adminToken) {
    return {
      statusCode: 401,
      body: JSON.stringify({ error: "Unauthorized. Pass ?admin=YOUR_NETLIFY_ADMIN_REGISTER_TOKEN" }),
    };
  }

  // Derive the webhook URL from the calling request host
  const host = event.headers?.host ?? "";
  const proto = event.headers?.["x-forwarded-proto"] ?? "https";
  const webhookUrl = `${proto}://${host}/.netlify/functions/telegram-webhook`;

  const secretParam = process.env.TELEGRAM_SECRET_TOKEN
    ? `&secret_token=${encodeURIComponent(process.env.TELEGRAM_SECRET_TOKEN)}`
    : "";

  const registerUrl =
    `https://api.telegram.org/bot${token}/setWebhook` +
    `?url=${encodeURIComponent(webhookUrl)}` +
    `&allowed_updates=${encodeURIComponent(JSON.stringify(["message", "edited_message"]))}` +
    secretParam;

  try {
    const res = await fetch(registerUrl);
    const data = await res.json();

    if (data.ok) {
      // Verify it took
      const infoRes = await fetch(`https://api.telegram.org/bot${token}/getWebhookInfo`);
      const info = await infoRes.json();
      return {
        statusCode: 200,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          success: true,
          registered_url: webhookUrl,
          telegram_response: data,
          webhook_info: info.result,
        }),
      };
    } else {
      return {
        statusCode: 400,
        body: JSON.stringify({ error: "Telegram rejected registration", telegram_response: data }),
      };
    }
  } catch (e) {
    return {
      statusCode: 500,
      body: JSON.stringify({ error: e.message }),
    };
  }
};
