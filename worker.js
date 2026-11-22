const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type',
  'Access-Control-Max-Age': '86400'
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...corsHeaders }
  });
}

function authorized(request, env) {
  const header = request.headers.get('Authorization') || '';
  return !!env.ADMIN_TOKEN && header === `Bearer ${env.ADMIN_TOKEN}`;
}

function cleanChannels(value) {
  if (!Array.isArray(value)) return null;
  const result = [...new Set(value
    .filter(x => typeof x === 'string')
    .map(x => x.trim().toLowerCase())
    .filter(x => /^[a-z0-9_]{1,25}$/.test(x)))];
  if (result.length > 90) return null;
  return result;
}

async function publishNtfy(env, title, message, clickUrl = 'https://www.twitch.tv/') {
  if (!env.NTFY_TOPIC) throw new Error('NTFY_TOPIC is not configured');
  const response = await fetch(`https://ntfy.sh/${encodeURIComponent(env.NTFY_TOPIC)}`, {
    method: 'POST',
    headers: {
      'Title': title,
      'Priority': 'high',
      'Tags': 'tada',
      'Click': clickUrl,
      'Content-Type': 'text/plain; charset=utf-8'
    },
    body: message
  });
  if (!response.ok) throw new Error(`ntfy returned HTTP ${response.status}`);
}

async function checkChannels(env) {
  const channels = await env.DATA.get('channels', { type: 'json' }) || [];
  if (!channels.length) return { checked: 0, alerts: 0 };
  const statuses = await env.DATA.get('statuses', { type: 'json' }) || {};
  let checked = 0;
  let alerts = 0;

  // DecAPI recommends limiting request frequency. One status request per channel.
  for (const login of channels) {
    try {
      const response = await fetch(`https://decapi.me/twitch/viewercount/${encodeURIComponent(login)}`, {
        headers: { 'User-Agent': 'TwitchLiveWatch-Background/1.0' },
        cf: { cacheTtl: 0, cacheEverything: false }
      });
      if (!response.ok) continue;
      const body = (await response.text()).trim();
      let online;
      if (/^\d+$/.test(body)) online = true;
      else if (/\boffline\b|not live|isn't live/i.test(body)) online = false;
      else continue; // Keep previous state if DecAPI returns an unexpected/error message.

      const previous = statuses[login];
      statuses[login] = online;
      checked++;
      // Notify only on a known OFFLINE -> ONLINE transition; initial scan is silent.
      if (previous === false && online === true) {
        try {
          await publishNtfy(env, 'Стример начал эфир', `${login} сейчас в эфире на Twitch!`, `https://www.twitch.tv/${login}`);
          alerts++;
        } catch (error) {
          console.error(`ntfy notification failed for ${login}:`, error);
        }
      }
    } catch (error) {
      console.error(`Status check failed for ${login}:`, error);
    }
    // Stay comfortably below public endpoint request limits.
    await new Promise(resolve => setTimeout(resolve, 750));
  }

  await env.DATA.put('statuses', JSON.stringify(statuses));
  await env.DATA.put('last_run', JSON.stringify({ at: new Date().toISOString(), checked, alerts }));
  return { checked, alerts };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: corsHeaders });

    if (url.pathname === '/api/health' && request.method === 'GET') {
      return json({ ok: true, service: 'Twitch Live Watch Background' });
    }

    if (!authorized(request, env)) return json({ error: 'Unauthorized: check ADMIN_TOKEN' }, 401);

    if (url.pathname === '/api/config' && request.method === 'GET') {
      const channels = await env.DATA.get('channels', { type: 'json' }) || [];
      const lastRun = await env.DATA.get('last_run', { type: 'json' }) || null;
      return json({ topic: env.NTFY_TOPIC || '', channels, lastRun });
    }

    if (url.pathname === '/api/channels' && request.method === 'POST') {
      let body;
      try { body = await request.json(); } catch { return json({ error: 'Invalid JSON' }, 400); }
      const channels = cleanChannels(body.channels);
      if (!channels) return json({ error: 'channels must be an array of up to 90 Twitch logins' }, 400);
      await env.DATA.put('channels', JSON.stringify(channels));
      // New channels start with unknown state: do not notify immediately if already live.
      const statuses = await env.DATA.get('statuses', { type: 'json' }) || {};
      for (const login of channels) {
        if (!(login in statuses)) statuses[login] = null;
      }
      for (const login of Object.keys(statuses)) {
        if (!channels.includes(login)) delete statuses[login];
      }
      await env.DATA.put('statuses', JSON.stringify(statuses));
      return json({ ok: true, count: channels.length });
    }

    if (url.pathname === '/api/test' && request.method === 'POST') {
      try {
        await publishNtfy(env, 'Twitch Live Watch — тест', 'Фоновое уведомление работает. Оно придёт, даже если страница приложения закрыта.');
        return json({ ok: true });
      } catch (error) {
        return json({ error: error.message || 'Could not send test notification' }, 502);
      }
    }

    if (url.pathname === '/api/check-now' && request.method === 'POST') {
      try { return json({ ok: true, ...(await checkChannels(env)) }); }
      catch (error) { return json({ error: error.message || 'Background check failed' }, 500); }
    }

    return json({ error: 'Not found' }, 404);
  },

  async scheduled(controller, env, ctx) {
    ctx.waitUntil(checkChannels(env));
  }
};
